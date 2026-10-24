import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import hook, notes, pipeline, status
from test_p3_startup import StartupBase


class GitFilterReviewTests(StartupBase):
    def test_durable_prose_and_numeric_counts_are_kept(self):
        for text in ['The reasoning behind SQLite is crash safety', 'Schedule fell behind by a week', 'Count at 2026102 rows']:
            with self.subTest(text=text):
                page = status.empty()
                page['purpose'] = [status.Item('i1', text, '[[old#e1]]')]
                result = status.reconcile(page, {'sections': status.empty(), 'displaced': []}, set())
                self.assertEqual(result.displaced, [])
                self.assertEqual(result.sections['purpose'][0].text, text)

    def test_git_context_and_explicit_numeric_hashes_are_displaced(self):
        for text in ['ahead of origin/main', 'behind upstream', 'behind remote main', 'behind main',
                     'ahead by 2 commits', 'behind by 1 commit', 'at abc1234', 'HEAD at 2026102',
                     'commit 2026102', 'sha 2026102', 'currently on branch feature/x at 1a2b3c4']:
            with self.subTest(text=text):
                page = status.empty()
                page['purpose'] = [status.Item('i1', text, '[[old#e1]]')]
                result = status.reconcile(page, {'sections': status.empty(), 'displaced': []}, set())
                self.assertEqual([d.reason for d in result.displaced], ['git-live'])


class ReplayReviewTests(StartupBase):
    def test_stale_journal_quarantined_and_new_capture_succeeds(self):
        page = status.empty()
        page['purpose'] = [status.Item('i1', 'Human current claim', '[[old#e1]]')]
        self.write_state(status.render(page))
        path = self.cfg.projects_dir / 'Alpha' / 'Alpha.md'
        journal = notes._status_journal(path)
        stale = json.dumps(dict(before='stale before', after='stale after', records=[], project='Alpha', date='2026-10-24'))
        journal.write_text(stale, encoding='utf-8')
        update = dict(sections={**status.empty(), 'latest': [{'text': 'New captured claim', 'ref': '[[new#e1]]'}]}, displaced=[])
        converter = Mock(return_value=(update, 0))
        with patch.object(pipeline, 'log') as log:
            result = pipeline.consolidate_into_project(self.cfg, 'Alpha', {'citations': [{'claim': 'New captured claim', 'quote': 'New captured claim'}]},
                        'new', datetime(2026, 10, 24, tzinfo=timezone.utc), 'claude-haiku', converter)
        self.assertEqual(result, (0, True))
        self.assertFalse(journal.exists())
        refused = list(path.parent.glob(journal.name + '.refused-*'))
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].read_text(encoding='utf-8'), stale)
        text = path.read_text(encoding='utf-8')
        self.assertIn('Human current claim', notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
        self.assertIn('New captured claim', notes.get_block(text, notes.STATE_BEGIN, notes.STATE_END))
        self.assertIn('status replay refused', notes.get_block(text, notes.PENDING_BEGIN, notes.PENDING_END))
        self.assertTrue(any(call.args[1] == 'status-replay-refused' for call in log.call_args_list))
        self.assertIn('status replay refused', hook.startup_package(self.cfg, str(self.repo)))


class StartupReviewTests(StartupBase):
    def recent(self):
        for n in range(4):
            self.man.db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, consolidated, captured_at) VALUES ('claude',?,1,?,'Alpha',?,?,1,?)",
                        (f's{n}', f'0-Sessions/s{n}.md', f'recent title {n}', f'2026-10-2{n}', n))
        self.man.db.commit()

    def test_three_recent_sessions_follow_items_and_stay_bounded(self):
        self.recent()
        page = status.empty()
        page['purpose'] = [status.Item('i1', 'Purpose claim', '[[old#e1]]')]
        self.write_state(status.render(page))
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertIn('Recent sessions:', package)
        self.assertLess(package.index('Purpose claim'), package.index('Recent sessions:'))
        for n in (3, 2, 1):
            self.assertIn(f'recent title {n}', package)
        self.assertNotIn('recent title 0', package)
        self.assertLessEqual(len(package), hook.CONTEXT_CAP)

    def test_4000_budget_fits_seven_sections_whole_items_and_pointer(self):
        self.assertEqual(hook.CONTEXT_CAP, 4000)
        self.recent()
        page = status.empty()
        for n, key in enumerate(status.SECTIONS):
            page[key] = [status.Item(f'i{n + 1}', key + ' ' + 'x' * (279 - len(key)), '[[old#e1]]')]
        page['active'].append(status.Item('i8', 'oversized' * 100, '[[old#e1]]'))
        self.write_state(status.render(page))
        package = hook.startup_package(self.cfg, str(self.repo))
        for items in page.values():
            self.assertIn(f'- {items[0].text} {items[0].ref}', package)
        self.assertIn('1 more items in', package)
        self.assertIn('Recent sessions:', package)
        self.assertLessEqual(len(package), 4000)

    def test_legacy_current_state_excerpt_is_read_only_and_health_explains_it(self):
        self.write_state('### Current state\n- Legacy durable claim\n\n### Active decisions\n- Not current-state excerpt')
        path = self.cfg.projects_dir / 'Alpha' / 'Alpha.md'
        before = path.read_bytes()
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertIn('Legacy durable claim', package)
        self.assertIn('page not migrated', package)
        self.assertNotIn('Not current-state excerpt', package)
        self.assertEqual(path.read_bytes(), before)
        self.assertLessEqual(len(package), 4000)
