import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentmem import notes
from agentmem.manifest import Manifest
from tools import retire_fragments as retire


class RetirementTests(unittest.TestCase):
    def setUp(self):
        audit = retire.ROOT / '.audit'
        audit.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name).resolve()
        self.mapping = {'Fragment': 'Parent'}
        self.page = ('---\r\nproject: Fragment\r\n---\r\n# Fragment\r\n'
                     f'{notes.STATE_BEGIN}\r\n- Keep this decision.\r\n{notes.STATE_END}\r\n'
                     f'{notes.PENDING_BEGIN}\r\n- Keep this pending line.\r\n{notes.PENDING_END}\r\n'
                     'Other nonempty text: ``` fenced.\r\n[[1-Projects/Fragment/History]]\r\n')
        self.history = '# Original history\r\n- Older event.\r\n'
        self.write('1-Projects/Parent/Parent.md', '# Parent\n')
        self.write('1-Projects/Parent/History.md', '# Parent history\r\nExisting text.\r\n')
        self.write('1-Projects/Fragment/Fragment.md', self.page)
        self.write('1-Projects/Fragment/History.md', self.history)
        self.write('1-Projects/Fragment/nested/extra.bin', b'\x00\xffextra')
        self.link = '[[1-Projects/Fragment/Fragment.md#Decision|fragment]]'
        self.write('2-Resources/Incoming.md', self.link + '\n')
        self.write('0-Sessions/session.md', self.link + '\n[[1-Projects/Fragment/History]]\n')
        manifest = Manifest(self.vault / '_meta/manifest.sqlite')
        manifest.db.execute("INSERT INTO sessions(agent, session_id, project) VALUES ('test','sid','Fragment')")
        manifest.db.execute("INSERT INTO captures(agent,session_id,part,note_path,project) VALUES ('test','sid',1,'0-Sessions/session.md','Fragment')")
        manifest.close()

    def write(self, relative, content):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes()
                for p in self.vault.rglob('*') if p.is_file() and '_meta' not in p.relative_to(self.vault).parts}

    def plan(self):
        return retire.build_plan(self.vault, self.mapping, '2026-10-25')

    def projects(self):
        with closing(sqlite3.connect(self.vault / '_meta/manifest.sqlite')) as db:
            return [db.execute(f'SELECT project FROM {table}').fetchone()[0] for table in ('sessions', 'captures')]

    def test_dry_run_then_lossless_apply_and_repeat(self):
        before = self.snapshot()
        db_before = (self.vault / '_meta/manifest.sqlite').read_bytes()
        plan = self.plan()
        self.assertFalse(plan['errors'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(db_before, (self.vault / '_meta/manifest.sqlite').read_bytes())
        counts = retire.summary(plan)['fragments'][0]
        self.assertEqual((1, 1, 3), (counts['state_items_copied'], counts['pending_items_copied'], counts['files_moved']))
        self.assertEqual(1, counts['links_rewritten'])
        self.assertGreaterEqual(counts['links_at_risk'], 1)  # History.md is not a unique filename.
        retire.apply_plan(plan)
        parent = (self.vault / '1-Projects/Parent/History.md').read_bytes().decode('utf-8')
        self.assertIn(self.page, parent)
        self.assertIn(self.history, parent)
        self.assertIn('Merged from fragment Fragment (2026-10-25)', parent)
        self.assertIn('Source: 1-Projects/Fragment/Fragment.md', parent)
        self.assertFalse((self.vault / '1-Projects/Fragment').exists())
        for relative, content in before.items():
            if relative.startswith('1-Projects/Fragment/'):
                archived = relative.replace('1-Projects/Fragment/', '4-Archive/merged-fragments/Fragment/', 1)
                self.assertEqual(content, (self.vault / archived).read_bytes())
        self.assertEqual(before['0-Sessions/session.md'], (self.vault / '0-Sessions/session.md').read_bytes())
        self.assertEqual('[[4-Archive/merged-fragments/Fragment/Fragment.md#Decision|fragment]]\n',
                         (self.vault / '2-Resources/Incoming.md').read_text(encoding='utf-8'))
        self.assertEqual(['Parent', 'Parent'], self.projects())
        after = self.snapshot()
        retire.apply_plan(self.plan())
        self.assertEqual(after, self.snapshot())

    def test_move_failure_rolls_back_history_links_and_manifest(self):
        before, plan = self.snapshot(), self.plan()
        with patch.object(Path, 'rename', side_effect=OSError('synthetic move failure')):
            with self.assertRaisesRegex(OSError, 'synthetic move failure'):
                retire.apply_plan(plan)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(['Fragment', 'Fragment'], self.projects())

    def test_stale_plan_refused_before_writes(self):
        plan = self.plan()
        self.write('1-Projects/Fragment/History.md', 'New event after planning.\n')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'source files changed'):
            retire.apply_plan(plan)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(['Fragment', 'Fragment'], self.projects())


if __name__ == '__main__':
    unittest.main()
