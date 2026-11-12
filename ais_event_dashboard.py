"""Export hourly traffic and a self-contained event dashboard per port/day."""
import csv
import json
from collections import defaultdict
from datetime import timedelta
from bisect import bisect_left, bisect_right
from math import cos, radians, hypot
from pathlib import Path
from ais_request_policy import open_request as urlopen

from count_ais_near_misses import open_csv, timestamp, utc
from ais_weather_pipeline import weather_url, download
from confirm_near_misses import excluded_vessel_type
from ais_environment import EnvironmentalDownloader, distance_km
from types import SimpleNamespace


def select_replay_tracks(eligible, tracks):
    """Select involved vessels and nearby anchored vessels for each replay.

    Input points use the dashboard tuple format. Each track is deduplicated and
    sorted once, even when multiple encounter windows overlap.
    """
    tracks = {ship: sorted(set(points)) for ship, points in tracks.items()}
    times = {ship: [point[0] for point in points] for ship, points in tracks.items()}
    for event, (begin, end) in eligible:
        x, y = float(event['first_flag_longitude']), float(event['first_flag_latitude'])
        selected = {}
        for ship, points in tracks.items():
            ticks = times[ship]
            window = points[bisect_left(ticks, begin):bisect_right(ticks, end)]
            involved = ship in (event['mmsi_a'], event['mmsi_b'])
            if not window or not (involved or any(
                point[4] and hypot((point[1]-x)*cos(radians(y)), point[2]-y) <= 1/60
                for point in window
            )):
                continue
            selected[ship] = window
        yield event, begin, selected


def collect_replay_tracks(day, bbox, ais, scored, node, adjacent=None,
                          include_sources=False):
    """Read AIS and select replay candidates, preserving source dates when requested."""
    west, south, east, north = bbox
    start = timestamp(str(day) + 'T00:00:00')
    traffic = defaultdict(set)
    tracks = defaultdict(list)
    point_sources = defaultdict(set)
    tugboats = set()
    events = []
    if scored.exists():
        with scored.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                if int(row['likely_near_miss_score_8']) >= 6 and not row['likely_near_miss_classification'].startswith('excluded_') and row['likely_near_miss_classification'] != 'separate_tugboat_review' and row['distance_time_utc']:
                    events.append(row)
    windows = [(timestamp(e['first_flag_utc'])-1800,
                timestamp(e['distance_time_utc'])+600) for e in events]
    sources = [(day, ais)]
    if adjacent and windows:
        for offset, needed in ((-1, min(a for a,b in windows) < start),
                               (1, max(b for a,b in windows) >= start+86400)):
            if needed:
                source = adjacent(day + timedelta(days=offset))
                if source:
                    sources.append((day + timedelta(days=offset), source))
    for source_day, source in sources:
        with open_csv(source, node) as stream:
            for raw in csv.DictReader(stream):
                row = {k.lower().replace('_', ''): v for k, v in raw.items() if k}
                try:
                    x, y = float(row.get('lon', row.get('longitude'))), float(row.get('lat', row.get('latitude')))
                    if not (west <= x <= east and south <= y <= north):
                        continue
                    t = timestamp(row['basedatetime'])
                    speed = float(row['sog'])
                    ship = row['mmsi'].strip()
                    if not ship.isdigit() or not start-1800 <= t < start+87001:
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                vessel_type = (row.get('vesseltype') or '').strip().casefold()
                if excluded_vessel_type(vessel_type):
                    tugboats.add(ship)
                anchored = row.get('status') in ('1', '1.0') and 0 <= speed < 1
                state = 'anchored' if anchored else 'moving' if 1 <= speed < 100 else ''
                if state and start <= t < start+86400:
                    traffic[int((t-start)//3600), state].add(ship)
                point = (t, x, y, row.get('vesselname') or ship, anchored)
                tracks[ship].append(point)
                if include_sources:
                    point_sources[(ship, point)].add(source_day.isoformat())
    if events and not tracks:
        raise ValueError('Scored encounters exist but no AIS tracks were loaded for the port')
    eligible = [(event, window) for event, window in zip(events, windows)
                if not tugboats.intersection((event['mmsi_a'], event['mmsi_b']))]
    print(f'Excluded {len(events)-len(eligible)} barge or tug encounters from replay', flush=True)
    return traffic, eligible, tracks, point_sources


def compact_replay_records(eligible, tracks, point_sources):
    """Build store-ready encounter memberships and deduplicated source samples."""
    encounters = []
    samples = {}
    for event, begin, selected in select_replay_tracks(eligible, tracks):
        vessels = []
        for ship, points in selected.items():
            role = 'involved' if ship in (event['mmsi_a'], event['mmsi_b']) else 'anchored_neighbor'
            vessels.append({'mmsi': ship, 'role': role})
            for point in points:
                source_dates = point_sources.get((ship, point))
                if not source_dates:
                    raise ValueError('Replay point is missing source provenance')
                for source_date in source_dates:
                    tick, lon, lat, name, anchored = point
                    key = (source_date, ship, *point)
                    samples[key] = {'source_date': source_date, 'mmsi': ship,
                                    'tick': tick, 'longitude': lon, 'latitude': lat,
                                    'name': name, 'anchored': anchored}
        encounters.append({
            'mmsi_a': event['mmsi_a'], 'mmsi_b': event['mmsi_b'],
            'first_flag_utc': event['first_flag_utc'],
            'window_start': begin, 'window_end': timestamp(event['distance_time_utc']) + 600,
            'details': dict(event), 'vessels': vessels,
        })
    return encounters, list(samples.values())


def collect_wind(day, bbox, cache_policy=None, source_report=None):
    west, south, east, north = bbox
    try:
        url = weather_url(day-timedelta(days=1), (west+east)/2, (south+north)/2)
        url = url.replace('end_date='+str(day), 'end_date='+str(day+timedelta(days=1)))
        if source_report is not None:
            source_report['url'] = url
        if cache_policy is None:
            with urlopen(url, timeout=30) as response:
                hourly = json.load(response)['hourly']
        else:
            body, info = cache_policy.fetch(url)
            if source_report is not None:
                source_report.update(info)
            hourly = json.loads(body.read_text(encoding='utf-8'))['hourly']
        if source_report is not None:
            source_report['status'] = 'available'
        return dict(zip(hourly['time'], zip(hourly['wind_speed_10m'], hourly['wind_direction_10m'])))
    except (OSError, ValueError, KeyError) as exc:
        if source_report is not None:
            source_report.update(status='unavailable', error=str(exc))
        return {}


def export(day, bbox, ais, folder, scored, node, adjacent=None):
    start = timestamp(str(day) + 'T00:00:00')
    traffic, eligible, tracks, _ = collect_replay_tracks(day, bbox, ais, scored, node, adjacent)
    with (folder/'traffic_hourly.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['window_start_utc', 'unique_moving', 'unique_anchored'])
        for i in range(24):
            writer.writerow([utc(start+i*3600), len(traffic[i,'moving']), len(traffic[i,'anchored'])])
    wind = collect_wind(day, bbox)
    marine = hourly_environment(day, bbox, start, folder, wind)
    with (folder/'event_trajectories.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(['event_id','closest_utc','score','mmsi','ship_name','time_utc',
                         'longitude','latitude','role','wind_mps','wind_from_deg'])
        for index, (event, begin, selected) in enumerate(select_replay_tracks(eligible, tracks), 1):
            for ship, window in selected.items():
                involved = ship in (event['mmsi_a'], event['mmsi_b'])
                for t, lon, lat, name, _ in window:
                    writer.writerow([index,event['distance_time_utc'],event['likely_near_miss_score_8'],
                                     ship,name,utc(t),lon,lat,
                                     'involved' if involved else 'anchored_neighbor',
                                     *wind.get(round(t/3600)*3600, ('',''))])
            event['tracks'] = selected
            event['window_start_utc'] = utc(begin)
    data = {'events': [event for event, _ in eligible], 'wind': wind, 'marine': marine}
    payload = json.dumps(data).replace('<', r'\u003c')
    (folder/'event_dashboard.html').write_text(HTML.replace('DATA_PLACEHOLDER',payload), encoding='utf-8')

def hourly_environment(day, bbox, start, folder, wind):
    marine, report = collect_hourly_environment(day, bbox, start, wind)
    (folder/'environment_sources.json').write_text(json.dumps(report, indent=2),encoding='utf-8')
    fields = ['hour_utc','measurement','station_id','latitude','longitude','observation_utc',
              'speed_mps','direction_deg','direction_convention','wave_height_m','current_depth_m']
    with (folder/'environment_hourly.csv').open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        for hour, rows in marine.items():
            if start <= hour < start+86400:
                writer.writerows(rows)
    return marine


def collect_hourly_environment(day, bbox, start, wind, cache_policy=None):
    """Collect the hourly replay observations and their source report."""
    args = SimpleNamespace(date=day, bbox=bbox, cache_dir=Path('ais_weather_cache'),
        cache_policy=cache_policy,
        environment=['waves','currents'], current_stations=None, wave_stations=None,
        max_environment_stations=5, max_waves_distance_km=50,
        max_currents_distance_km=0, current_bin=None)

    class PortDownloader(EnvironmentalDownloader):
        def select(self, kind, catalog):
            if kind != 'currents':
                return super().select(kind, catalog)
            w,s,e,n = self.args.bbox
            center = ((w+e)/2, (s+n)/2)
            pool = [r for r in catalog if w <= r['longitude'] <= e and s <= r['latitude'] <= n]
            selected = []
            def distance(row, point):
                return distance_km(row['longitude'], row['latitude'], *point)
            while pool and len(selected) < 5:
                if selected:
                    station = max(pool, key=lambda r: min(distance(r, (q['longitude'],q['latitude']))
                                                           for q in selected))
                else:
                    station = min(pool, key=lambda r: distance(r, center))
                selected.append(station)
                pool = [r for r in pool if distance(r,(station['longitude'],station['latitude'])) > .1]
            return selected

    observations, report = PortDownloader(args, download).run()
    stations = report['sources']['currents']['stations']
    report['current_location_target'] = 5
    report['current_locations_selected'] = len(stations)
    buckets = defaultdict(list)
    for row in observations:
        if row['usable']:
            tick = timestamp(row['timestamp_utc'])
            buckets[int(tick//3600)*3600].append((tick,row))
    marine = {}
    for hour in range(int(start)-3600,int(start)+90000,3600):
        rows = []
        for kind, station in [('waves',{}), *[('currents', r) for r in stations]]:
            candidates = [(t,r) for t,r in buckets[hour] if r['kind'] == kind
                          and (not station or r['station_id'] == station['id'])]
            sample = min(candidates,key=lambda item:abs(item[0]-(hour+1800)))[1] if candidates else {}
            rows.append(dict(hour_utc=utc(hour),measurement=kind,
                station_id=sample.get('station_id',station.get('id')),
                latitude=sample.get('latitude',station.get('latitude')),
                longitude=sample.get('longitude',station.get('longitude')),
                observation_utc=sample.get('timestamp_utc'),
                speed_mps=sample.get('current_speed_mps'),direction_deg=sample.get('current_direction_deg'),
                direction_convention='toward' if kind=='currents' else None,
                wave_height_m=sample.get('significant_wave_height_m'),current_depth_m=sample.get('current_depth_m')))
        speed, direction = wind.get(hour,(None,None))
        rows.append(dict(hour_utc=utc(hour),measurement='modeled_wind',speed_mps=speed,
                         direction_deg=direction,direction_convention='from'))
        marine[hour] = rows
    return marine, report


HTML = """<!doctype html><meta charset="utf-8"><title>AIS encounter review</title>
<style>body{font:16px system-ui;margin:2rem;background:#eef4f8;color:#142536}canvas{background:white;max-width:100%}select,input{margin:.5rem}</style>
<h1>AIS encounter review</h1><p>Scores 7–8 · UTC · 30 minutes before first flag through 10 minutes after closest approach</p>
<select id="events" aria-label="Encounter"></select><input id="time" type="range" min="-30" max="10" value="10" aria-label="Minutes from closest approach">
<button id="play" type="button">Play</button><p id="details"></p><canvas id="plot" width="1000" height="650"></canvas>
<p>Anchored neighbors are shown when recorded within 1 nautical mile of the first flag position during the window. AIS gaps are left blank, including missing adjacent-day coverage. Green arrow shows where wind blows. Blue arrows show where currents flow at NOAA stations. Hourly wave and current values use the usable observation nearest the middle of the hour.</p>
<script type="application/json" id="data">DATA_PLACEHOLDER</script>
<script>
const d=JSON.parse(document.querySelector('#data').textContent), sel=document.querySelector('#events'), slider=document.querySelector('#time'), details=document.querySelector('#details'), c=document.querySelector('#plot').getContext('2d');
for(const [i,e] of d.events.entries()) sel.add(new Option(e.distance_time_utc+' | score '+e.likely_near_miss_score_8+' | '+e.mmsi_a+' / '+e.mmsi_b,i));
function draw(){
 c.clearRect(0,0,1000,650);
 const e=d.events[sel.value]; if(!e){details.textContent='No score 7–8 encounters with a closest approach timestamp.';return;}
 const t=Date.parse(e.distance_time_utc)/1000, begin=Date.parse(e.window_start_utc)/1000, end=t+60*Number(slider.value);
 const x=Number(e.first_flag_longitude), y=Number(e.first_flag_latitude), scale=Math.cos(y*Math.PI/180);
 const near=p=>Math.hypot((p[1]-x)*scale,p[2]-y)<=1/60;
 const visible=[];
 for(const [id,track] of Object.entries(e.tracks)){
  const pts=track.filter(p=>p[0]>=begin&&p[0]<=t+600);
  const involved=id===e.mmsi_a||id===e.mmsi_b;
  if(pts.length&&(involved||pts.some(p=>p[4]&&near(p)))) visible.push([id,pts,involved]);
 }
 const points=visible.flatMap(v=>v[1]), radius=Math.max(.02,...points.map(p=>Math.hypot((p[1]-x)*scale,p[2]-y)));
 const xy=p=>[500+(p[1]-x)*scale*280/radius,325-(p[2]-y)*280/radius];
 for(const [id,pts,involved] of visible){
  const shown=pts.filter(p=>p[0]<=end); if(!shown.length)continue;
  c.strokeStyle=c.fillStyle=id===e.mmsi_a?'#007c9e':id===e.mmsi_b?'#c04424':'#666';
  c.lineWidth=involved?3:1;c.beginPath();
  shown.forEach((p,i)=>{const [a,b]=xy(p);if(!i||p[0]-shown[i-1][0]>120)c.moveTo(a,b);else c.lineTo(a,b);});c.stroke();
  const p=shown.at(-1),[a,b]=xy(p);c.beginPath();c.arc(a,b,4,0,2*Math.PI);c.fill();
  c.font='14px system-ui';c.fillText(p[3]+' · '+new Date(p[0]*1000).toISOString().slice(11,19),a+6,b);
 }
 const hour=Math.floor(end/3600)*3600;
 const w=d.wind[hour], marine=d.marine?.[hour] || [];
 function arrow(x,y,degrees,color){
  c.save();c.translate(x,y);c.rotate(degrees*Math.PI/180);
  c.strokeStyle=color;c.lineWidth=3;c.beginPath();c.moveTo(0,14);c.lineTo(0,-14);
  c.lineTo(-5,-7);c.moveTo(0,-14);c.lineTo(5,-7);c.stroke();c.restore();
 }
 if(w && w.every(Number.isFinite)) arrow(960,35,w[1]+180,'#146c48');
 for(const r of marine){
  if(r.measurement!=='currents'||!Number.isFinite(r.direction_deg)) continue;
  const [px,py]=xy([0,r.longitude,r.latitude]);
  arrow(px,py,r.direction_deg,'#224ac0');
  c.fillStyle='#224ac0';c.font='13px system-ui';
  c.fillText(r.station_id+' '+r.speed_mps.toFixed(2)+' m/s',px+12,py);
 }
 const wave=marine.find(r=>r.measurement==='waves')?.wave_height_m;
 details.textContent=new Date(end*1000).toISOString()+' · '+(w&&w.every(v=>v!==null)?'Wind '+w[0]+' m/s from '+w[1]+'°':'Wind unavailable')+' · Wave height '+(wave==null?'unavailable':wave+' m');
 if(e.first_flag_utc) details.textContent += ' · First flag '+e.first_flag_utc;
 if(e.distance_time_utc) details.textContent += ' · Closest approach '+e.distance_time_utc;
 c.fillStyle='#142536';c.font='16px system-ui';c.fillText(details.textContent,20,25);
}
function selectEvent(){const e=d.events[sel.value];if(e){slider.min=(Date.parse(e.window_start_utc)-Date.parse(e.distance_time_utc))/60000;slider.value=10;}draw();}
const play=document.querySelector('#play');
let timer;
function pause(){clearInterval(timer);timer=undefined;play.textContent='Play';}
play.onclick=()=>{
 if(timer!==undefined){pause();return;}
 if(Number(slider.value)>=Number(slider.max)) slider.value=slider.min;
 draw();play.textContent='Pause';
 timer=setInterval(()=>{
  slider.value=Math.min(Number(slider.max),Number(slider.value)+1);
  draw();
  if(Number(slider.value)>=Number(slider.max))pause();
 },1000);
};
sel.onchange=()=>{pause();selectEvent();};
slider.oninput=()=>{pause();draw();};
selectEvent();
</script>"""




