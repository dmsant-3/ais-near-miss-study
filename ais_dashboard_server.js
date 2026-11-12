const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');
const zlib = require('node:zlib');

const reportRoot = path.resolve(process.argv[2] || 'historical_dashboard_pilot_v3');
const root = path.join(reportRoot, 'daily');
const execFileAsync = promisify(execFile);
const reader = path.join(__dirname, 'ais_compact_reader.py');
const database = path.join(reportRoot, 'study.sqlite');
const ports = ['Port_of_NY_NJ', 'Port_of_LA', 'Port_of_Houston'];

function acceptsGzip(header) {
    return String(header || '').split(',').some(entry => {
        const [name, ...parameters] = entry.trim().split(';');
        if (name.trim().toLowerCase() !== 'gzip') return false;
        const quality = parameters.find(value => /^\s*q\s*=/i.test(value));
        return quality === undefined || Number(quality.split('=')[1]) > 0;
    });
}

async function compactReader(action, args = []) {
    const python = process.env.AIS_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
    const result = await execFileAsync(python, [reader, database, action, ...args],
        { maxBuffer: 64 * 1024 * 1024, timeout: 60000, env: { ...process.env, PYTHONUTF8: '1' } });
    return result.stdout;
}

async function isCompact() {
    try {
        return JSON.parse(await fs.readFile(path.join(reportRoot, 'run_config.json'), 'utf8')).output_format === 'compact';
    } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw error;
    }
}
const theme = `<meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}
body{margin:0 auto;padding:clamp(16px,4vw,48px);max-width:1120px;background:#101e2d;color:#edf5ff;font:16px/1.5 system-ui}
h1{font-size:clamp(30px,5vw,52px);line-height:1.1;letter-spacing:-.035em;margin:16px 0}
p{color:#bacddd} a{color:#83e2ce}
.replay-controls{display:flex;flex-wrap:wrap;align-items:center;gap:12px;background:#192d40;border:1px solid #34516a;border-radius:16px;padding:16px;margin:18px 0}
.replay-controls #events{flex:1 1 100%;min-width:0}
.replay-controls #time{flex:1 1 100%}
.replay-controls label{display:inline-flex;align-items:center;gap:8px}
button,select{font:inherit;border:1px solid #53738f;border-radius:8px;padding:8px 12px;background:#233e56;color:#fff}
button{cursor:pointer} button:hover{background:#345c79} input[type=range]{accent-color:#83e2ce;max-width:100%;min-width:0}
:focus-visible{outline:2px solid #83e2ce;outline-offset:3px}
#details{font-variant-numeric:tabular-nums;background:#192d40;border-left:3px solid #83e2ce;padding:10px 14px}
canvas{display:block;width:100%;height:auto;background:#f3f8fc;border:1px solid #53738f;border-radius:16px}
.ship-legend{margin:14px 0;padding:14px 18px;background:#192d40;border:1px solid #34516a;border-radius:12px}
.ship-legend ul{display:flex;flex-wrap:wrap;gap:8px 24px;list-style:none;padding:0;margin:8px 0 0}
.ship-legend li{overflow-wrap:anywhere}.ship-key{display:inline-block;width:12px;height:12px;border:1px solid #fff;border-radius:50%;margin-right:8px}
</style>`;
const playback = `<script>
const scoreNote = document.createElement('p');
scoreNote.textContent = 'Scores prioritize encounters for human review.';
sel.before(scoreNote);
for (const option of sel.options) option.textContent = option.textContent.replace(/ \| score [78] \| /, ' | ');
const button = document.querySelector('#play') || document.createElement('button');
if (!button.isConnected) slider.after(button);
button.textContent = 'Play';
const label = document.createElement('label');
label.innerHTML = ' Playback speed <input type="range" min="0.25" max="4" step="0.25" value="1"><output>1×</output>';
const restart = document.createElement('button');
restart.type = 'button';
restart.textContent = 'Restart';
button.after(restart);
restart.after(label);
restart.onclick = () => {
 stopSpeedPlayback();
 slider.value = slider.min;
 button.onclick();
};
const zoomLabel = document.createElement('label');
zoomLabel.innerHTML = ' Site zoom <input id="site-zoom" type="range" min="1" max="16" step="0.5" value="1"><output>1×</output>';
label.after(zoomLabel);
const controls = document.createElement('section');
controls.className = 'replay-controls';
controls.setAttribute('aria-label', 'Replay controls');
sel.before(controls);
controls.append(sel, slider, button, restart, label, zoomLabel);
const plot = document.querySelector('canvas');
const legend = document.createElement('section');
legend.className = 'ship-legend';
const legendTitle = document.createElement('strong');
legendTitle.textContent = 'Vessels in this replay';
const legendItems = document.createElement('ul');
legend.append(legendTitle, legendItems);
plot.after(legend);
function updateShipLegend(){
 legendItems.replaceChildren();
 const encounter = d.events[sel.value];
 for(const [id, track] of Object.entries(encounter?.tracks || {})){
  const first = id === encounter.mmsi_a;
  const second = id === encounter.mmsi_b;
  const item = document.createElement('li');
  const key = document.createElement('span');
  key.className = 'ship-key';
  key.style.backgroundColor = first ? '#007c9e' : second ? '#c04424' : '#666';
  const name = track.find(point => point[3] && point[3] !== id)?.[3] || id;
  item.append(key, name + ' · ' + (first ? 'Vessel A' : second ? 'Vessel B' : 'Nearby anchored vessel'));
  legendItems.append(item);
 }
 legend.hidden = !legendItems.children.length;
}
sel.addEventListener('change', updateShipLegend);
updateShipLegend();
let drag; window.aisPanX = window.aisPanY = 0;
plot.style.cursor = 'grab';
plot.style.touchAction = 'none';
plot.onpointerdown = event => {
 if(event.button !== 0) return;
 drag = event;
 plot.setPointerCapture(event.pointerId);
 plot.style.cursor = 'grabbing';
};
plot.onpointermove = event => {
 if(!drag || drag.pointerId !== event.pointerId) return;
 const bounds = plot.getBoundingClientRect();
 window.aisPanX += (event.clientX-drag.clientX)*plot.width/bounds.width;
 window.aisPanY += (event.clientY-drag.clientY)*plot.height/bounds.height;
 drag = event;
 draw();
};
plot.onlostpointercapture = () => {drag = undefined;plot.style.cursor = 'grab';};
sel.addEventListener('change', () => {window.aisPanX = window.aisPanY = 0;draw();});
const zoom = zoomLabel.querySelector('input');
plot.addEventListener('wheel', event => {
 if (!event.deltaY || event.ctrlKey) return;
 event.preventDefault();
 const next = Number(zoom.value) - Math.sign(event.deltaY) * Number(zoom.step);
 zoom.value = Math.max(Number(zoom.min), Math.min(Number(zoom.max), next));
 zoom.dispatchEvent(new Event('input'));
}, {passive: false});
const fingers = new Map();
let pinch;
function touchDistance(){
 const [a,b] = fingers.values();
 return Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);
}
plot.addEventListener('pointerdown', event => {
 if(event.pointerType !== 'touch') return;
 fingers.set(event.pointerId,event);
 plot.setPointerCapture(event.pointerId);
 if(fingers.size === 2){
  drag = undefined;
  pinch = {distance: Math.max(1,touchDistance()), zoom: Number(zoom.value)};
 }
});
plot.addEventListener('pointermove', event => {
 if(!fingers.has(event.pointerId)) return;
 fingers.set(event.pointerId,event);
 if(!pinch || fingers.size < 2) return;
 zoom.value = Math.max(Number(zoom.min), Math.min(Number(zoom.max),
  pinch.zoom * touchDistance() / pinch.distance));
 zoom.dispatchEvent(new Event('input'));
});
for(const type of ['pointerup','pointercancel','lostpointercapture']){
 plot.addEventListener(type,event=>{
  if(!fingers.delete(event.pointerId)) return;
  if(fingers.size < 2) pinch = undefined;
 });
}
zoom.oninput = () => {
 zoomLabel.querySelector('output').textContent = zoom.value + '×';
 draw();
};
const speed = label.querySelector('input');
const output = label.querySelector('output');
let speedTimer;
function stopSpeedPlayback(){
 clearTimeout(speedTimer);speedTimer=undefined;button.textContent=+slider.value >= +slider.max ? 'Replay' : 'Play';
}
function scheduleStep(){
 speedTimer=setTimeout(()=>{
  slider.value=Math.min(+slider.max,+slider.value+1);
  draw();
  if(+slider.value>=+slider.max)stopSpeedPlayback();else scheduleStep();
 },1000/+speed.value);
}
button.onclick=()=>{
 if(speedTimer!==undefined){stopSpeedPlayback();return;}
 if(+slider.value>=+slider.max)slider.value=slider.min;
 draw();button.textContent='Pause';scheduleStep();
};
speed.oninput=()=>{
 output.textContent=speed.value+'×';
 if(speedTimer!==undefined){clearTimeout(speedTimer);scheduleStep();}
};
slider.addEventListener('input',stopSpeedPlayback);
sel.addEventListener('change',stopSpeedPlayback);
stopSpeedPlayback();
</script>`;

http.createServer(async (req, res) => {
    const route = req.url.split('?')[0];
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    try {
        const compact = await isCompact();
        if (compact && (route === '/' || route === '/progress.html' || route === '/api/progress')) {
            const data = JSON.parse(await compactReader('progress'));
            data.config = JSON.parse(await fs.readFile(path.join(reportRoot, 'run_config.json'), 'utf8'));
            try {
                data.status = JSON.parse(await fs.readFile(path.join(reportRoot, 'run_status.json'), 'utf8'));
            } catch (error) {
                if (error.code !== 'ENOENT') throw error;
                data.status = null;
            }
            data.completed_days = new Set(data.ports.map(row => row.ais_date)).size;
            if (route === '/api/progress') {
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.end(JSON.stringify(data));
            } else {
                const links = data.ports.map(row =>
                    '<li><a href="/' + row.ais_date + '/' + row.port + '">' +
                    row.ais_date + ' · ' + row.port.replaceAll('_', ' ') + '</a> — ' +
                    row.potential_events + ' screened, ' + row.likely_for_human_review +
                    ' for review' + (row.exclusion_total == null ? '' : ', ' + row.exclusion_total + ' excluded') +
                    ' · <a href="/api/export/daily?date=' + row.ais_date + '&amp;port=' + row.port +
                    '">daily CSV</a> · <a href="/api/export/candidates?date=' + row.ais_date +
                    '&amp;port=' + row.port + '">candidates CSV</a> · <a href="/api/export/likely?date=' +
                    row.ais_date + '&amp;port=' + row.port + '">review CSV</a> · <a href="/api/export/traffic?date=' +
                    row.ais_date + '&amp;port=' + row.port + '">traffic CSV</a> · <a href="/api/export/environment?date=' +
                    row.ais_date + '&amp;port=' + row.port + '">environment CSV</a> · <a href="/api/days/' +
                    row.ais_date + '/ports/' + row.port + '/provenance">provenance</a></li>');
                const escapeHtml = value => String(value).replace(/[&<>"']/g, char =>
                    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
                const status = data.status ? '<p>Latest run: ' + escapeHtml(data.status.state) +
                    (data.status.message ? ' — ' + escapeHtml(data.status.message) : '') + '</p>' : '';
                const active = data.status?.active_date ? '<p>Processing ' +
                    escapeHtml(data.status.active_date) +
                    (data.status.active_port ? ' · ' + escapeHtml(data.status.active_port.replaceAll('_', ' ')) : '') +
                    (data.status.stage ? ' · ' + escapeHtml(data.status.stage) : '') + '</p>' : '';
                const missing = data.status?.missing_count ? '<p>' +
                    Number(data.status.missing_count) + ' dates absent from the NOAA index. First dates: ' +
                    data.status.missing_dates.map(escapeHtml).join(', ') + '</p>' : '';
                const metrics = data.projection ? '<p>Projected retained output for 30 dates: ' +
                    (data.projection.average_30_dates_bytes / 1e9).toFixed(2) + ' GB using average growth, ' +
                    (data.projection.busiest_30_dates_bytes / 1e9).toFixed(2) +
                    ' GB using the busiest measured date. Measurements cover ' +
                    data.projection.measured_dates + ' dates.</p>' : '';
                const history = Array.isArray(data.status?.history) && data.status.history.length ?
                    '<details><summary>Recent run history</summary><ul>' +
                    data.status.history.slice().reverse().map(item => '<li>' + escapeHtml(item.utc) +
                        ' · ' + escapeHtml(item.event) + (item.date ? ' · ' + escapeHtml(item.date) : '') +
                        (item.message ? ' · ' + escapeHtml(item.message) : '') + '</li>').join('') +
                    '</ul></details>' : '';
                res.end('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>AIS progress</title><h1>AIS progress</h1><p>' +
                    data.completed_days + ' completed AIS dates of ' + Number(data.config.study_days) +
                    '</p>' + status + active + missing + metrics + '<ul>' + links.join('') + '</ul>' + history + theme);
            }
            return;
        }
        if (compact && route.startsWith('/api/')) {
            const exportMatch = /^\/api\/export\/(daily|candidates|likely|trajectories|traffic|environment)$/.exec(route);
            if (exportMatch) {
                const query = new URL(req.url, 'http://localhost').searchParams;
                const day = query.get('date');
                const port = query.get('port');
                const encounter = query.get('encounter');
                if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day) ||
                    (port && !ports.includes(port)) ||
                    (encounter && (!port || !/^[a-f0-9]{64}$/.test(encounter)))) {
                    res.writeHead(400).end('Invalid export parameters');
                    return;
                }
                const args = [reader, database, 'export', '--kind', exportMatch[1], '--date', day];
                if (port) args.push('--port', port);
                if (encounter) args.push('--encounter', encounter);
                const python = process.env.AIS_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
                const child = spawn(python, args,
                    { windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' } });
                let stderr = '';
                let started = false;
                child.stderr.on('data', chunk => { stderr += chunk.toString().slice(0, 4096); });
                child.on('error', () => {
                    if (!res.headersSent) {
                        res.removeHeader('Content-Encoding');
                        res.writeHead(500).end('Export unavailable');
                    }
                });
                child.on('close', code => {
                    if (!started && !res.headersSent) {
                        res.removeHeader('Content-Encoding');
                        const status = stderr.includes('KeyError') ? 404 :
                            stderr.includes('ValueError') ? 400 :
                            stderr.includes('database is locked') ? 503 : 500;
                        res.writeHead(code ? status : 200).end(code ? 'Export failed' : '');
                    }
                });
                res.on('close', () => { if (!res.writableEnded) child.kill(); });
                res.setHeader('Content-Type', 'text/csv; charset=utf-8');
                res.setHeader('Content-Disposition', 'attachment; filename="' + exportMatch[1] + '-' + day + '.csv"');
                const gzip = acceptsGzip(req.headers['accept-encoding']);
                if (gzip) {
                    res.setHeader('Content-Encoding', 'gzip');
                }
                child.stdout.once('data', chunk => {
                    started = true;
                    if (gzip) {
                        const compressor = zlib.createGzip();
                        compressor.pipe(res);
                        compressor.write(chunk);
                        child.stdout.pipe(compressor);
                    } else {
                        res.write(chunk);
                        child.stdout.pipe(res);
                    }
                });
                return;
            }
            const provenanceMatch = /^\/api\/days\/(\d{4}-\d{2}-\d{2})\/ports\/([A-Za-z_]+)\/provenance$/.exec(route);
            if (provenanceMatch && ports.includes(provenanceMatch[2])) {
                try {
                    const data = await compactReader('provenance', ['--date', provenanceMatch[1], '--port', provenanceMatch[2]]);
                    res.setHeader('Content-Type', 'application/json; charset=utf-8');
                    res.end(data);
                } catch (error) {
                    const stderr = String(error.stderr || '');
                    res.writeHead(stderr.includes('KeyError') ? 404 : stderr.includes('ValueError') ? 400 :
                        stderr.includes('database is locked') ? 503 : 500)
                        .end('Could not load provenance');
                }
                return;
            }
            const matchApi = /^\/api\/days\/(\d{4}-\d{2}-\d{2})\/ports\/([A-Za-z_]+)\/encounters(?:\/([a-f0-9]{64}))?$/.exec(route);
            if (!matchApi || !ports.includes(matchApi[2])) {
                res.writeHead(404).end('Not found');
                return;
            }
            const query = new URL(req.url, 'http://localhost').searchParams;
            const limit = query.get('limit') || '50';
            const cursor = query.get('cursor');
            if (!/^\d+$/.test(limit) || Number(limit) < 1 || Number(limit) > 100 || (cursor && cursor.length > 1024)) {
                res.writeHead(400).end('Invalid pagination');
                return;
            }
            const args = ['--date', matchApi[1], '--port', matchApi[2]];
            const action = matchApi[3] ? 'replay' : 'encounters';
            if (matchApi[3]) args.push('--encounter', matchApi[3]);
            else {
                args.push('--limit', limit);
                if (cursor) args.push('--cursor', cursor);
            }
            try {
                const data = await compactReader(action, args);
                res.setHeader('Content-Type', 'application/json; charset=utf-8');
                res.end(data);
            } catch (error) {
                const stderr = String(error.stderr || '');
                const status = stderr.includes('KeyError') ? 404 :
                    stderr.includes('ValueError') ? 400 :
                    stderr.includes('database is locked') ? 503 : 500;
                res.writeHead(status).end('Could not load compact data');
            }
            return;
        }
        if (route === '/') {
            const dates = (await fs.readdir(root)).filter(name => /^\d{4}-\d{2}-\d{2}$/.test(name)).sort().reverse();
            const links = dates.flatMap(day => ports.map(port =>
                '<li><a href="/' + day + '/' + port + '">' + day + ' · ' + port.replaceAll('_', ' ') + '</a></li>'));
            res.end('<!doctype html><meta charset="utf-8"><title>AIS timelapse</title><h1>AIS timelapse</h1><ul>' + links.join('') + '</ul>');
            return;
        }
        const match = /^\/(\d{4}-\d{2}-\d{2})\/([A-Za-z_]+)$/.exec(route);
        if (!match || !ports.includes(match[2])) {
            res.writeHead(404).end('Dashboard not found.');
            return;
        }
        const html = compact ? await compactReader('shell') :
            await fs.readFile(path.join(root, match[1], match[2], 'event_dashboard.html'), 'utf8');
        const location = {
            Port_of_NY_NJ: 'New York / New Jersey · New York Harbor & Atlantic approaches',
            Port_of_LA: 'Los Angeles, California · San Pedro Bay & Pacific approaches',
            Port_of_Houston: 'Houston, Texas · Houston Ship Channel & Galveston Bay'
        }[match[2]];
        const chartBackdrop = `
 c.save();
 const sea = c.createLinearGradient(0,0,0,650);
 sea.addColorStop(0,'#edfafa'); sea.addColorStop(.52,'#cce9ef'); sea.addColorStop(1,'#a4d0df');
 c.fillStyle=sea;c.fillRect(0,0,1000,650);
 c.save();
 c.strokeStyle='#fff';c.lineWidth=2;c.globalAlpha=.42;
 for(let band=0;band<7;band++){
  c.beginPath();
  for(let x=0;x<=1000;x+=8){
   const y=95+band*84+12*Math.sin(x/112+band*.72)+5*Math.sin(x/43);
   if(x===0)c.moveTo(x,y);else c.lineTo(x,y);
  }
  c.stroke();
 }
 c.restore();
 c.strokeStyle='#79b5ca';c.globalAlpha=.35;c.lineWidth=1.4;c.beginPath();
 for(let row=0;row<9;row++){
  for(let x=24+(row%2)*55;x<980;x+=110){
   const y=46+row*72;
   c.moveTo(x,y);c.quadraticCurveTo(x+7,y-5,x+14,y);
   c.quadraticCurveTo(x+21,y+5,x+28,y);
  }
 }
 c.stroke();c.globalAlpha=1;
 c.strokeStyle='#b7cfdf';c.lineWidth=0.7;c.beginPath();
 for(let i=50;i<1000;i+=50){c.moveTo(i,0);c.lineTo(i,650);}
 for(let i=50;i<650;i+=50){c.moveTo(0,i);c.lineTo(1000,i);}
 c.stroke();
 c.fillStyle='#16364d';c.font='bold 14px system-ui';
 c.fillText('N',950,565);
 c.font='bold 15px system-ui';
 const locationLabel = ${JSON.stringify(location)};
 const locationWidth = c.measureText(locationLabel).width;
 c.fillStyle='rgba(255,255,255,.92)';
 c.fillRect(1000-locationWidth-32,608,locationWidth+20,30);
 c.fillStyle='#16364d';
 c.fillText(locationLabel,1000-locationWidth-22,628);
 c.beginPath();c.moveTo(955,575);c.lineTo(947,597);c.lineTo(955,592);
 c.lineTo(963,597);c.closePath();c.fill();c.restore();
`;
        const zoomable = html.replace('c.clearRect(0,0,1000,650);', '$&' + chartBackdrop)
            .replace('c.fillText(p[3]', 'c.save();c.strokeStyle="#f6fbff";c.lineWidth=4;c.lineJoin="round";c.strokeText(p[3]+ " · "+new Date(p[0]*1000).toISOString().slice(11,19),a+33,b);c.restore();c.fillText(p[3]')
            .replace('c.arc(a,b,4,0,2*Math.PI);c.fill();', 'c.save();c.translate(a,b);const previous=shown.slice(0,-1).findLast(q=>q[1]!==p[1]||q[2]!==p[2]);if(previous&&!p[4]){const [px,py]=xy(previous);c.rotate(Math.atan2(b-py,a-px)+Math.PI/2);}c.moveTo(0,-29);c.quadraticCurveTo(14,-22,15,-8);c.lineTo(13,20);c.quadraticCurveTo(13,25,8,25);c.lineTo(-8,25);c.quadraticCurveTo(-13,25,-13,20);c.lineTo(-15,-8);c.quadraticCurveTo(-14,-22,0,-29);c.closePath();c.fill();c.strokeStyle="#fff";c.lineWidth=3;c.stroke();c.fillStyle="#f8fcff";c.fillRect(-9,-6,18,12);c.fillStyle="#16364d";c.fillRect(-7,-5,14,3);c.fillStyle="#f8fcff";c.fillRect(-6,11,12,8);c.restore();').replace('for(const [i,e] of d.events.entries())', 'd.events = d.events.filter(event => (Number(event.likely_near_miss_score_7) === 7 || Number(event.likely_near_miss_score_8) === 8)); for(const [i,e] of d.events.entries())').replaceAll('Scores 7–8', 'Review scores').replaceAll('score 7–8 encounters', 'review encounters').replace('=>[500+', '=>[500+(window.aisPanX || 0)+').replace(',325-', ',325+(window.aisPanY || 0)-').replaceAll('*280/radius', '*280/radius*(Number(document.querySelector("#site-zoom")?.value)||1)');
        const ruler = `
 const pixelsPerNm = 280 / radius * (Number(document.querySelector("#site-zoom")?.value) || 1) / 60;
 const target = 120 / pixelsPerNm;
 const unit = 10 ** Math.floor(Math.log10(target));
 const distance = ([5, 2, 1].find(n => n * unit <= target) || 1) * unit;
 const length = distance * pixelsPerNm;
 c.save();
 c.fillStyle = 'rgba(255,255,255,0.92)';
 c.fillRect(12, 598, 146, 42);
 c.fillStyle = c.strokeStyle = '#142536';
 c.font = '14px system-ui';
 c.fillText(Number(distance.toPrecision(3)) + ' nmi', 20, 615);
 c.lineWidth = 2;
 c.beginPath();
 c.moveTo(20, 621); c.lineTo(20, 631);
 c.moveTo(20, 626); c.lineTo(20 + length, 626);
 c.moveTo(20 + length, 621); c.lineTo(20 + length, 631);
 c.stroke();
 c.restore();`;
        const encounterEffect = `
 const flagTime = Date.parse(e.first_flag_utc) / 1000;
 if(Number.isFinite(flagTime) && end >= flagTime && end <= flagTime + 120){
  const [fx,fy] = xy([0,x,y]);
  c.save();c.strokeStyle='#ad5b00';c.fillStyle='#5c3000';c.lineWidth=3;
  c.beginPath();c.arc(fx,fy,16,0,2*Math.PI);c.stroke();
  c.font='bold 14px system-ui';c.fillText('First flag',fx+20,fy-15);c.restore();
 }
 const elapsed = end-t;
 if(elapsed >= 0 && elapsed <= 120){
  function closestPosition(id){
   let before, after;
   for(const point of e.tracks[id] || []){
    if(point[0]<=t)before=point;
    if(point[0]>=t){after=point;break;}
   }
   if(!before || !after || after[0]-before[0]>120)return null;
   const f=after[0]===before[0]?0:(t-before[0])/(after[0]-before[0]);
   return [0,before[1]+f*(after[1]-before[1]),before[2]+f*(after[2]-before[2])];
  }
  const a=closestPosition(e.mmsi_a),b=closestPosition(e.mmsi_b);
  if(a&&b){
   const [cx,cy]=xy([0,(a[1]+b[1])/2,(a[2]+b[2])/2]);
   c.save();c.strokeStyle='#a44b00';c.fillStyle='#ffb547';
   c.globalAlpha=.25;c.beginPath();c.arc(cx,cy,25+elapsed/5,0,2*Math.PI);c.fill();
   c.globalAlpha=1;c.lineWidth=3;
   for(const radius of [18,29+elapsed/5]){
    c.beginPath();c.arc(cx,cy,radius,0,2*Math.PI);c.stroke();
   }
   c.font='bold 15px system-ui';c.lineWidth=5;c.strokeStyle='#fff';
   c.strokeText('Closest approach',cx+20,cy-31);
   c.fillStyle='#793600';c.fillText('Closest approach',cx+20,cy-31);c.restore();
  }
 }
`;
        res.end(zoomable.replace(/ const w\s*=/, encounterEffect + '$&').replace('c.fillText(details.textContent,20,25);', '$&' + ruler) + theme + playback);
    } catch (error) {
        res.writeHead(error.code === 'ENOENT' ? 404 : 500).end('Could not load the saved AIS dashboard. Check the report directory.');
    }
}).listen(Number(process.env.AIS_DASHBOARD_PORT || 8765), '127.0.0.1',
    () => console.log('AIS dashboard is listening on 127.0.0.1'));



















