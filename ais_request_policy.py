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


def defer_http_status(url: str, status: int, retry_after: str | None = None):
    """Persist a transient HTTP cooldown for a verified alternate client."""
    if _state is None or (status not in (408, 429) and not 500 <= status <= 599):
        return
    host = urlsplit(url).hostname
    delay = 3600 if status == 429 else 900
    if retry_after:
        try:
            seconds = int(retry_after) if retry_after.isdigit() else (
                parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            delay = max(delay, seconds)
        except (ValueError, TypeError, OverflowError):
            pass
    cooldown = _state / (host + '.cooldown')
    temporary = cooldown.with_suffix('.part')
    temporary.write_text(str(time.time() + delay))
    temporary.replace(cooldown)
    raise DeferredRequest(f'{host} returned HTTP {status}. Retry after {delay:g} seconds.')


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
        defer_http_status(getattr(request, 'full_url', request), exc.code,
                          exc.headers.get('Retry-After') if exc.headers else None)
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
