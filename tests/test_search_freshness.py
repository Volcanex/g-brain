"""Read-only Basic Memory canary checks against temporary SQLite fixtures."""
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import brain_status as status


class SearchFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.canary = self.root / 'canary.md'
        self.canary.write_text('Current knowledge.\n')
        os.utime(self.canary, (time.time() - 600, time.time() - 600))
        self.db = self.root / 'memory.db'
        self.watch = self.root / 'watch-status.json'
        self.watch.write_text(json.dumps({'running': True, 'last_scan': '2000-01-01'}))
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE project (id INTEGER PRIMARY KEY, path TEXT)')
            conn.execute('CREATE TABLE entity (project_id INTEGER, file_path TEXT, checksum TEXT)')
            conn.execute('INSERT INTO project VALUES (1, ?)', (str(self.root),))
            conn.execute('INSERT INTO entity VALUES (1, ?, ?)', ('canary.md', self.digest()))
        for p in (patch.object(status, 'BRAIN_ROOT', self.root), patch.dict(os.environ, {
            'BRAIN_MEMORY_DB': str(self.db), 'BRAIN_MEMORY_CANARY': 'canary.md',
            'BRAIN_MEMORY_WATCH_STATUS': str(self.watch), 'BRAIN_MEMORY_FRESHNESS_GRACE_SECONDS': '120',
        })):
            p.start()
            self.addCleanup(p.stop)

    def digest(self):
        return hashlib.sha256(self.canary.read_bytes()).hexdigest()

    def test_current_checksum_is_healthy_despite_old_watcher_scan(self):
        before = self.db.read_bytes()
        self.assertEqual(status._check_search_freshness()[0], 'up')
        self.assertEqual(self.db.read_bytes(), before)

    def test_old_mismatch_warns_and_reindex_recovers(self):
        self.canary.write_text('Corrected knowledge.\n')
        os.utime(self.canary, (time.time() - 600, time.time() - 600))
        state, detail = status._check_search_freshness()
        self.assertEqual(state, 'warn')
        self.assertIn('checksum differs', detail)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE entity SET checksum = ?', (self.digest(),))
        self.assertEqual(status._check_search_freshness()[0], 'up')

    def test_recent_change_gets_grace_without_false_green(self):
        self.canary.write_text('Just corrected.\n')
        state, detail = status._check_search_freshness()
        self.assertEqual(state, 'unknown')
        self.assertIn('sync grace', detail)

    def test_unsupported_schema_is_unknown(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('DROP TABLE entity')
        self.assertEqual(status._check_search_freshness()[0], 'unknown')

    def test_missing_source_index_or_watcher_warns(self):
        for path in (self.canary, self.db, self.watch):
            moved = path.with_suffix('.bak')
            path.rename(moved)
            try:
                self.assertEqual(status._check_search_freshness()[0], 'warn', path)
                self.assertFalse(path.exists())
            finally:
                moved.rename(path)

    def test_unindexed_old_canary_warns(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('DELETE FROM entity')
        state, detail = status._check_search_freshness()
        self.assertEqual(state, 'warn')
        self.assertIn('not indexed', detail)

    def test_empty_invalid_or_malformed_watcher_state_warns(self):
        for contents in ('', '{broken', '{}', '[]', '{"running": "yes"}'):
            with self.subTest(contents=contents):
                self.watch.write_text(contents)
                state, detail = status._check_search_freshness()
                self.assertEqual(state, 'warn')
                self.assertIn('watcher state', detail)

    def test_stopped_watcher_warns_even_when_checksum_matches(self):
        self.watch.write_text(json.dumps({'running': False}))
        self.assertEqual(status._check_search_freshness()[0], 'warn')

    def test_remote_unknown_and_liveness_failure_preserved(self):
        svc = {'id': 'basic-memory', 'port': 8765, 'host': 'remote'}
        with patch.object(status, 'BRAIN_HOST', 'here'), \
                patch.object(status, '_check_tcp', return_value=('up', 'TCP open')):
            result = status.probe(svc, {})
        self.assertEqual(result['state'], 'unknown')
        self.assertIn('remote', result['detail'])
        svc['host'] = 'here'
        with patch.object(status, 'BRAIN_HOST', 'here'), \
                patch.object(status, '_check_tcp', return_value=('down', 'connection refused')):
            result = status.probe(svc, {})
        self.assertEqual(result['state'], 'down')

    def test_unset_host_checks_every_service_locally(self):
        svc = {'id': 'basic-memory', 'port': 8765, 'host': 'remote'}
        with patch.object(status, 'BRAIN_HOST', ''), \
                patch.object(status, '_check_tcp', return_value=('up', 'TCP open')):
            result = status.probe(svc, {})
        self.assertEqual(result['method'], 'port+freshness')

    def test_service_row_reports_lag_even_when_process_healthy(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE entity SET checksum = 'old'")
        with patch.object(status, '_check_tcp', return_value=('up', 'TCP open')):
            result = status.probe({'id': 'basic-memory', 'port': 8765, 'host': status.BRAIN_HOST}, {})
        self.assertEqual(result['state'], 'warn')
        self.assertEqual(result['method'], 'port+freshness')
        self.assertIn('checksum differs', result['detail'])


if __name__ == '__main__':
    unittest.main()
