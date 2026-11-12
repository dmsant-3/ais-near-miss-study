"""Request pacing and persistent cooldown for unattended AIS runs."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

class DeferredRequest(Exception):
    """Stop this batch and retry on a later scheduled run."""

_state = None
_gap = 5
_last = {}

def configure(directory: Path, gap: float):
    global _state, _gap
    directory.mkdir(parents=True, exist_ok=True)
    _state, _gap = directory, gap

def open_request(request, **kwargs):
    if _state is None:
        return urlopen(request, **kwargs)
    host = urlsplit(getattr(request, 'full_url', request)).hostname
    cooldown = _state / (host + '.cooldown')
    if cooldown.exists() and float(cooldown.read_text()) > time.time():
        raise DeferredRequest(f'{host} is still in cooldown')
    time.sleep(max(0, _gap - (time.monotonic() - _last.get(host, 0))))
    _last[host] = time.monotonic()
    try:
        return urlopen(request, **kwargs)
    except HTTPError as exc:
        if exc.code not in (408, 429) and not 500 <= exc.code <= 599:
            raise
        delay = 3600 if exc.code == 429 else 900
        retry = exc.headers.get('Retry-After')
        if retry:
            try:
                seconds = int(retry) if retry.isdigit() else (
                    parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                delay = max(delay, seconds)
            except (ValueError, TypeError, OverflowError):
                pass
        message = f'{host} returned HTTP {exc.code}. Retry after {delay:g} seconds.'
    except (URLError, TimeoutError) as exc:
        if isinstance(exc, URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError):
            # Let the weather client try its verified system TLS fallback.
            raise
        delay = 900
        message = f'{host} request failed ({exc}). Retry after {delay} seconds.'
    temp = cooldown.with_suffix('.part')
    temp.write_text(str(time.time() + delay))
    temp.replace(cooldown)
    raise DeferredRequest(message)
