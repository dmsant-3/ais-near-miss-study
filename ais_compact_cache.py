"""Bounded response cache for one locked compact study."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import os

from ais_weather_pipeline import download


class CompactCache:
    def __init__(self, root: Path, scratch: Path, max_bytes: int = 2_000_000_000,
                 reserve_bytes: int = 0):
        if max_bytes <= 0:
            raise ValueError('Cache limit must be positive')
        self.root = root
        self.scratch = scratch
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes
        root.mkdir(parents=True, exist_ok=True)
        scratch.mkdir(parents=True, exist_ok=True)
        self._evict(0)
        self.high_water_bytes = self._body_size()

    def _body_size(self) -> int:
        return sum(path.stat().st_size for path in self.root.iterdir()
                   if path.is_file() and not path.name.endswith('.json'))

    def _evict(self, required: int) -> None:
        bodies = [path for path in self.root.iterdir()
                  if path.is_file() and not path.name.endswith('.json')]
        total = sum(path.stat().st_size for path in bodies)
        for body in sorted(bodies, key=lambda path: (path.stat().st_mtime_ns, path.name)):
            if total + required <= self.max_bytes:
                break
            total -= body.stat().st_size
            body.unlink()
            body.with_suffix('.download.json').unlink(missing_ok=True)

    def fetch(self, url: str):
        """Return a verified body and manifest, retaining it when it fits."""
        snapshot = datetime.now(timezone.utc).date().isoformat()
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        target = self.root / f'{snapshot}_{key}'
        manifest = target.with_suffix('.download.json')
        if target.exists() and not manifest.exists():
            target.unlink()
        if manifest.exists() and not target.exists():
            manifest.unlink()
        if target.exists():
            info = download(url, target)
            os.utime(target, None)
            return target, info
        temporary = self.scratch / f'{snapshot}_{key}'
        info = download(url, temporary, reserve_bytes=self.reserve_bytes)
        if info['bytes'] > self.max_bytes:
            return temporary, info
        self._evict(info['bytes'])
        temporary.replace(target)
        temporary.with_suffix('.download.json').replace(manifest)
        self.high_water_bytes = max(self.high_water_bytes, self._body_size())
        return target, info
