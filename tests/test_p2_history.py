import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import notes, status


def fixture_dir():
    scratch = Path(__file__).resolve().parents[1] / '.artifacts'
    scratch.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=scratch)


class HistoryTests(unittest.TestCase):
    def test_history_first_crash_replay_same_page_once_per_occurrence(self):
        with fixture_dir() as tmp:
            path = Path(tmp) / 'Project.md'
            old = status.empty()
            old['purpose'] = [status.Item(f'i{n}', 'duplicate original', '[[old#e1]]') for n in (1, 2)]
            notes.update_project_note(path, 'Project', '2026-10-24', state=status.render(old))
            result = status.Result(status.empty(), [status.Displaced(i, 'purpose', 'limit') for i in old['purpose']])
            write = notes.atomic_write
            def crash(target, text):
                if target == path:
                    raise RuntimeError('page crash')
                write(target, text)
            with patch.object(notes, 'atomic_write', side_effect=crash):
                with self.assertRaisesRegex(RuntimeError, 'page crash'):
                    notes.update_project_note(path, 'Project', '2026-10-24', status_result=result, update_key='session:1')
            history = path.with_name('History.md').read_text(encoding='utf-8')
            self.assertEqual(history.count('duplicate original'), 2)
            self.assertIn('duplicate original', path.read_text(encoding='utf-8'))
            notes.recover_status_update(path)
            self.assertEqual(status.parse(notes.get_block(path.read_text(encoding='utf-8'), notes.STATE_BEGIN, notes.STATE_END)), result.sections)
            self.assertEqual(path.with_name('History.md').read_text(encoding='utf-8'), history)
            notes.recover_status_update(path)
            self.assertEqual(path.with_name('History.md').read_text(encoding='utf-8'), history)

    def test_limit_items_found_in_history(self):
        with fixture_dir() as tmp:
            path = Path(tmp) / 'Project.md'
            old = status.empty()
            old['purpose'] = [status.Item(f'i{n}', f'old {n}', f'[[2026-01-0{n}-s#e1]]') for n in range(1, 5)]
            result = status.reconcile(old, {'sections': status.empty(), 'displaced': []}, set())
            notes.update_project_note(path, 'Project', '2026-10-24', status_result=result, update_key='session:1')
            history = path.with_name('History.md').read_text(encoding='utf-8')
            self.assertIn('old 1', history)
            self.assertIn('limit', history)

    def test_older_capture_on_page_without_order_markers_keeps_newer_decisions(self):
        with fixture_dir() as tmp:
            path = Path(tmp) / 'Project.md'
            old = status.empty()
            old['decisions'] = [
                status.Item(f'i{index}', f'Decision {day}', f'[[0-Sessions/2026-11-{day:02d}-session#e1]]')
                for index, day in enumerate(range(3, 11), 1)
            ]
            notes.update_project_note(path, 'Project', '2026-11-10', state=status.render(old))
            before = path.read_text(encoding='utf-8')
            ref = '[[0-Sessions/2026-11-01-session#e7]]'
            update = dict(sections={**status.empty(), 'decisions': [
                dict(text='Use SQLite', ref=ref)]}, displaced=[])
            result = status.reconcile(old, update, {ref: 'Use SQLite'},
                                      source_orders=notes.evidence_orders(before, old), new_order=(10, 1))
            notes.update_project_note(path, 'Project', '2026-11-27', status_result=result, update_key='session:1')

            after = path.read_text(encoding='utf-8')
            state = notes.get_block(after, notes.STATE_BEGIN, notes.STATE_END)
            self.assertEqual(status.parse(state)['decisions'], old['decisions'])
            history = path.with_name('History.md').read_text(encoding='utf-8')
            self.assertIn(f'- 2026-11-27 [decisions; limit] Use SQLite {ref}', history.splitlines())

    def test_history_link_names_the_project_folder(self):
        with fixture_dir() as tmp:
            path = Path(tmp) / '1-Projects' / 'Alpha' / 'Alpha.md'
            notes.update_project_note(path, 'Alpha', '2026-10-24', status_result=status.Result(status.empty()), update_key='s:1')
            self.assertIn('History: [[1-Projects/Alpha/History]]', path.read_text(encoding='utf-8'))
