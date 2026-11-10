"""Tests for the compact response cache."""
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ais_compact_cache import CompactCache


class CompactCacheTests(unittest.TestCase):
    def test_historical_archives_reuse_within_week_catalogs_refresh_daily(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = CompactCache(root / 'cache', root / 'scratch')
            calls = []
            def download(url, target, reserve_bytes=0):
                if target.exists():
                    return json.loads(target.with_suffix('.download.json').read_text())
                calls.append(url)
                target.write_bytes(b'body')
                info = {'url': url, 'sha256': 'fixture', 'bytes': 4}
                target.with_suffix('.download.json').write_text(json.dumps(info))
                return info
            archive = 'https://www.ndbc.noaa.gov/data/historical/stdmet/44025h2020.txt.gz'
            catalog = 'https://www.ndbc.noaa.gov/activestations.xml'
            with patch('ais_compact_cache.download', side_effect=download), \
                 patch('ais_compact_cache.datetime') as clock:
                clock.now.return_value = datetime(2026, 11, 9, tzinfo=timezone.utc)
                cache.fetch(archive)
                cache.fetch(catalog)
                clock.now.return_value = datetime(2026, 11, 10, tzinfo=timezone.utc)
                cache.fetch(archive)
                cache.fetch(catalog)
            self.assertEqual(calls.count(archive), 1)
            self.assertEqual(calls.count(catalog), 2)

    def test_reuses_evicts_and_keeps_oversized_response_in_scratch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = CompactCache(root / 'cache', root / 'scratch', max_bytes=4)
            calls = []

            def download(url, target, reserve_bytes=0):
                if target.exists():
                    return json.loads(target.with_suffix('.download.json').read_text())
                calls.append(url)
                body = url.encode()
                target.write_bytes(body)
                info = {'url': url, 'sha256': 'fixture', 'bytes': len(body)}
                target.with_suffix('.download.json').write_text(json.dumps(info))
                return info

            with patch('ais_compact_cache.download', side_effect=download):
                first, _ = cache.fetch('aa')
                self.assertEqual(cache.fetch('aa')[0], first)
                self.assertEqual(calls, ['aa'])
                cache.fetch('bbb')
                self.assertFalse(first.exists())
                large, _ = cache.fetch('12345')
                self.assertEqual(large.parent, root / 'scratch')
                self.assertEqual(large.read_bytes(), b'12345')
                bodies = [p for p in (root / 'cache').iterdir() if not p.name.endswith('.json')]
                self.assertLessEqual(sum(p.stat().st_size for p in bodies), 4)


if __name__ == '__main__':
    unittest.main()
