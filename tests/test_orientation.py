"""Bounded orientation regressions; all corpus reads use fixtures."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import brain_api as B
from brain_reader import Note


def note(path, title='', **kwargs):
    return Note(path=path, title=title or path, kind='finding', status='active',
                version='1', lineage=[], tags=[], updated='', created='', **kwargs)


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(B, 'BRAIN_ROOT', self.root).start()
        patch.dict('os.environ', {'GCORE_MANIFEST': str(self.root / 'manifest.json')}).start()
        self.notes = patch.object(B, 'all_notes', return_value=[]).start()

    def test_home_does_not_invent_root_project(self):
        self.notes.return_value = [note('findings/root-cause', 'A root cause')]
        self.assertNotIn('findings/root-cause', B.render_orientation('/root'))

    def test_nested_repository(self):
        repo = self.root / 'widget'
        (repo / '.git').mkdir(parents=True)
        (repo / 'src').mkdir()
        self.notes.return_value = [note('findings/widget-design'), note('findings/src-design')]
        result = B.render_orientation(str(repo / 'src'))
        self.assertIn('findings/widget-design', result)
        self.assertNotIn('findings/src-design', result)

    def test_manifest_remote_path_without_local_checkout(self):
        (self.root / 'manifest.json').write_text(json.dumps({'projects': [{'id': 'widget'}]}))
        self.notes.return_value = [note('findings/widget-design')]
        result = B.render_orientation('/not-a-local-machine/widget/src')
        self.assertIn('findings/widget-design', result)
        self.assertNotIn('findings/widget-design', B.render_orientation('/unknown/widgetish/src'))

    def test_worktree_origin_identifies_project(self):
        repo = self.root / 'temporary-worktree'
        repo.mkdir()
        git_dir = self.root / 'main' / '.git'
        worktree_dir = git_dir / 'worktrees' / 'temporary'
        worktree_dir.mkdir(parents=True)
        (repo / '.git').write_text(f'gitdir: {worktree_dir}\n')
        (worktree_dir / 'commondir').write_text('../..\n')
        (git_dir / 'config').write_text('[remote "origin"]\nurl = git@github.com:Volcanex/widget.git\n')
        self.notes.return_value = [note('findings/widget-design')]
        self.assertIn('findings/widget-design', B.render_orientation(str(repo)))

    def test_deterministic_ranking_and_excerpt(self):
        self.notes.return_value = [note('findings/widget-z', body='# Heading\nActual evidence.'),
                                  note('findings/widget-a')]
        first = B.render_orientation(project='widget')
        self.notes.return_value.reverse()
        self.assertEqual(first, B.render_orientation(project='widget'))
        self.assertLess(first.index('findings/widget-a'), first.index('findings/widget-z'))
        self.assertIn('Actual evidence.', first)

    def test_explicit_project_task_and_authored_priority(self):
        self.notes.return_value = [note('nerves/widget/CLAUDE', 'Widget authentication'),
                                  note('findings/widget-login', 'Widget authentication'),
                                  note('findings/widget-colour', 'Widget colours'),
                                  note('findings/gadget-login', 'Gadget authentication'),
                                  note('findings/widgetish', 'Widgetish unrelated')]
        result = B.render_orientation('/root', project='widget', task='fix authentication', limit=1)
        self.assertIn('findings/widget-login', result)
        self.assertNotIn('nerves/widget/CLAUDE', result)
        self.assertNotIn('findings/gadget-login', result)
        self.assertNotIn('findings/widgetish', result)
        self.assertIn('omitted', result)
        task_only = B.render_orientation(task='authentication')
        self.assertIn('findings/gadget-login', task_only)
        self.assertNotIn('findings/widget-colour', task_only)

    def test_caps_and_warnings_survive(self):
        self.notes.return_value = [note(f'findings/widget-{i}', 'Widget ' + 'x' * 4000)
                                  for i in range(40)]
        self.notes.return_value[0].status = 'broken'
        status = self.root / 'status'
        status.mkdir()
        (status / 'stale.json').write_text(json.dumps({'outstanding': 9}))
        (status / 'condense.json').write_text(json.dumps({'backlog': 8, 'counts': {'stale': 3}}))
        result = B.render_orientation(project='widget', limit=100000)
        self.assertLessEqual(len(result), 6000)
        self.assertLessEqual(result.count('\n- '), 12)
        self.assertIn('omitted', result)
        self.assertIn('flagged broken', result)
        self.assertIn('9 note(s) stand', result)
        self.assertIn('3 possibly outdated', result)
        self.assertIn('[BROKEN]', result)
        self.assertNotIn('\n- ', B.render_orientation(project='widget', limit=-1))

    def test_default_api_and_optional_inputs(self):
        self.notes.return_value = [note('findings/widget-login', 'Widget authentication')]
        with TestClient(B.create_api()) as client:
            response = client.get('/orient')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers['content-type'].startswith('text/plain'))
            self.assertIn('Brain (long-term corpus', response.text)
            self.assertIn('Writing is part', response.text)
            response = client.get('/orient', params={'cwd': '/root', 'project': 'widget', 'task': 'authentication'})
            self.assertIn('findings/widget-login', response.text)
            self.assertEqual(client.get('/orient', params={'task': 'x' * 1001}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
