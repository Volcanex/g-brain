"""Behavioral memory update tests; every write uses a temporary corpus."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import frontmatter
import brain_commit as commit
import brain_index as index
import brain_reader as reader
import brain_verify as verify


class MemoryUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module, name, value in (
            (commit, 'BRAIN_ROOT', self.root),
            (commit, 'COMMITS_LOG', self.root / 'commits.log'),
            (index, 'BRAIN_ROOT', self.root),
            (index, 'INDEXES_DIR', self.root / 'indexes'),
            (verify, 'BRAIN_ROOT', self.root),
        ):
            p = patch.object(module, name, value)
            p.start()
            self.addCleanup(p.stop)

    def note(self, **kwargs):
        return commit.brain_note('Transport uses polling.', title='Transport', **kwargs)

    def load(self):
        return frontmatter.load(self.root / 'findings/transport.md')

    def flag(self):
        post = self.load()
        post['stale_dep'] = [{'note': 'designs/upstream', 'since': '2020-01-01',
                              'stamp': 'revision-one', 'reason': 'upstream rewritten'}]
        (self.root / 'findings/transport.md').write_text(frontmatter.dumps(post))

    def test_correction_updates_summary_index_and_returns_persisted_title(self):
        self.note()
        result = commit.brain_note('Correction: transport now uses SSE.',
            target='findings/transport', summary='Transport now uses SSE.')
        self.assertEqual(result['title'], 'Transport')
        self.assertEqual(self.load()['summary'], 'Transport now uses SSE.')
        self.assertIn('Transport now uses SSE.', (self.root / 'indexes/findings.md').read_text())
        self.assertIn('Transport uses polling.', self.load().content)
        self.assertTrue(list((self.root / 'findings/.history').rglob('*.md')))

    def test_legacy_append_stops_advertising_old_summary(self):
        self.note()
        result = commit.brain_note('Correction: transport now uses SSE.', target='findings/transport')
        self.assertEqual(result['title'], 'Transport')
        self.assertTrue(self.load()['summary_needs_review'])
        self.assertEqual(self.load()['summary_previous'], 'Transport uses polling.')
        advertised = (self.root / 'indexes/findings.md').read_text()
        self.assertIn('Needs review after new information: Transport uses polling.', advertised)
        commit.brain_note('Additional details.', target='findings/transport')
        self.assertEqual(self.load()['summary'],
                         'Needs review after new information: Transport uses polling.')
        self.assertEqual(self.load()['summary_previous'], 'Transport uses polling.')
        commit.brain_note('SSE verified.', target='findings/transport', summary='Transport uses SSE.')
        self.assertFalse(self.load()['summary_needs_review'])

    def test_unrelated_append_does_not_clear_dependency(self):
        self.note()
        self.flag()
        commit.brain_note('Unrelated operational detail.', target='findings/transport')
        self.assertEqual(verify.sweep()['outstanding'], 1)
        self.assertEqual(len(self.load()['stale_dep']), 1)

    def test_explicit_review_is_exact_and_versioned(self):
        self.note()
        self.flag()
        before = self.load()['version']
        result = commit.review_dependency('findings/transport', 'designs/upstream',
            'revision-one', 'Checked upstream; transport contract remains compatible.')
        self.assertTrue(result['ok'], result)
        self.assertNotEqual(self.load()['version'], before)
        self.assertEqual(verify.sweep()['cleared'], 1)
        self.assertNotIn('stale_dep', self.load())
        self.assertIn('Checked upstream', (self.root / 'commits.log').read_text())

    def test_new_stamp_invalidates_old_review_even_on_same_day(self):
        self.note()
        self.flag()
        commit.review_dependency('findings/transport', 'designs/upstream', 'revision-one',
                                 'Checked upstream contract against this note.')
        with patch.object(reader, 'dependents', return_value=[{'path': 'findings/transport'}]):
            commit._stamp_dependents('designs/upstream', 'rewritten again', 'agent')
        self.assertNotEqual(self.load()['stale_dep'][0]['stamp'], 'revision-one')
        self.assertEqual(verify.sweep()['outstanding'], 1)
        result = commit.review_dependency('findings/transport', 'designs/upstream',
                                         'revision-one', 'Checked previous revision only.')
        self.assertFalse(result['ok'])

    def test_review_is_specific_and_legacy_dates_need_explicit_review(self):
        self.note()
        self.flag()
        post = self.load()
        post['stale_dep'].append({'note': 'designs/other', 'since': '2020-01-01'})
        (self.root / 'findings/transport.md').write_text(frontmatter.dumps(post))
        commit.review_dependency('findings/transport', 'designs/other', '2020-01-01',
                                 'Read both notes; this contract is still compatible.')
        report = verify.sweep()
        self.assertEqual(report['cleared'], 1)
        self.assertEqual(report['outstanding'], 1)
        self.assertEqual(self.load()['stale_dep'][0]['note'], 'designs/upstream')

    def test_auto_slug_collision_uses_same_safe_append_path(self):
        self.note(summary='Current transport uses polling.')
        result = commit.brain_note('SSE replaces polling.', title='Transport')
        self.assertEqual(result['action'], 'appended')
        self.assertTrue(result['summary_needs_review'])
        self.assertIn('Needs review after new information: Current transport uses polling.',
                      (self.root / 'indexes/findings.md').read_text())

    def test_invalid_summary_is_rejected_without_mutation(self):
        self.note()
        before = (self.root / 'findings/transport.md').read_text()
        result = commit.brain_note('New information.', target='findings/transport', summary='  ')
        self.assertFalse(result['ok'])
        self.assertEqual((self.root / 'findings/transport.md').read_text(), before)

    def test_refuses_unattended_or_empty_review(self):
        self.note()
        self.flag()
        for actor, reason in [('ai-auto', 'Checked contract against upstream.'), ('agent', '')]:
            result = commit.review_dependency('findings/transport', 'designs/upstream',
                                             'revision-one', reason, actor=actor)
            self.assertFalse(result['ok'])
        self.assertEqual(verify.sweep()['outstanding'], 1)

    def test_rebuild_indexes_runs_the_index_module_from_any_cwd(self):
        self.note()
        for f in (self.root / 'indexes').glob('*.md'):
            f.unlink()
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            try:
                result = commit.rebuild_indexes()
            finally:
                os.chdir(cwd)
        self.assertTrue(result['ok'], result)
        self.assertTrue((self.root / 'indexes/master.md').exists())
        self.assertIn('Transport', (self.root / 'indexes/findings.md').read_text())


if __name__ == '__main__':
    unittest.main()
