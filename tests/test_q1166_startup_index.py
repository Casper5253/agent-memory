"""The startup package advertises a project note's sections, inside the same cap and never as a cut item."""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import hook, notes, status
from test_p3_startup import StartupBase


class StartupIndexTests(StartupBase):
    def note_path(self):
        return self.cfg.projects_dir / 'Alpha' / 'Alpha.md'

    def test_index_names_sections_with_counts_and_newest_dates(self):
        page = status.empty()
        page['purpose'] = [status.Item('i1', 'Direction one', '[[2026-10-01-s#e1]]'),
                           status.Item('i2', 'Direction two', '[[2026-10-02-s#e2]]')]
        page['working_now'] = [status.Item('i3', 'Works item', '[[2026-10-03-s#e3]]')]
        page['latest'] = [status.Item('i4', 'Latest item', '[[2026-10-04-s#e4]]')]
        page['active'] = [status.Item('i5', 'Active item', '[[2026-10-05-s#e5]]')]
        page['questions'] = [status.Item('i6', 'Question item', '[[2026-10-06-s#e6]]')]
        page['decisions'] = [status.Item('i7', 'Decision item', '[[2026-10-07-s#e7]]')]
        page['next'] = [status.Item('i8', 'Next item', '[[2026-10-08-s#e8]]')]
        self.write_state(status.render(page))
        notes.update_project_note(self.note_path(), 'Alpha', '2026-10-24',
                                  session_link='s', session_line='- 2026-10-24 [[s|A session]]')

        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertLessEqual(len(package), hook.CONTEXT_CAP)
        index = next(line for line in package.splitlines() if line.startswith('Note sections'))
        self.assertIn('mem note --section', index)
        for expected in ('Purpose 2 2026-10-02', 'Works now 1 2026-10-03', 'Latest 1 2026-10-04',
                         'Active 1 2026-10-05', 'Questions 1 2026-10-06', 'Decisions 1 2026-10-07',
                         'Next steps 1 2026-10-08', 'Sessions 1 2026-10-24'):
            self.assertIn(expected, index)

    def test_index_is_trimmed_before_status_items(self):
        page = status.empty()
        for number, key in enumerate(status.SECTIONS):
            page[key] = [status.Item(f'i{number}', key + ' ' + 'x' * (278 - len(key)),
                                    f'[[2026-10-{number + 1:02d}-s#e1]]')]
        self.write_state(status.render(page))

        full = hook.startup_package(self.cfg, str(self.repo))
        full_items = [line for line in full.splitlines() if line.startswith('- ')]
        full_index = next(line for line in full.splitlines() if line.startswith('Note sections'))
        self.assertIn('2026-10-', full_index)
        names = hook._note_index_variants(self.note_path().read_text(encoding='utf-8'))[-1]
        cap = len(full) - len(full_index) - 1 + len(names) + 1

        with patch.object(hook, 'CONTEXT_CAP', cap):
            trimmed = hook.startup_package(self.cfg, str(self.repo))
        self.assertLessEqual(len(trimmed), cap)
        self.assertEqual(full_items, [line for line in trimmed.splitlines() if line.startswith('- ')])
        self.assertIn(names, trimmed)
        self.assertNotIn('2026-10-', next(line for line in trimmed.splitlines()
                                          if line.startswith('Note sections')))

    def test_cap_is_held_with_a_huge_note_and_items_stay_whole(self):
        page = status.empty()
        for key, (_, limit) in status.SECTIONS.items():
            page[key] = [status.Item(f'{key}{n}', key + ' ' + ('detail ' * 34) + 'end',
                                    f'[[2026-10-{n + 1:02d}-s#e{n}]]')
                         for n in range(1, limit + 1)]
        self.write_state(status.render(page))
        items = {item.text for values in page.values() for item in values}

        with patch.object(hook, 'CONTEXT_CAP', 3200):
            package = hook.startup_package(self.cfg, str(self.repo))
        self.assertLessEqual(len(package), 3200)
        for line in package.splitlines():
            if line.startswith('- '):
                self.assertIn(line[2:line.rindex(' (')], items)  # whole stored item, no cut text
        if 'Note sections' in package:
            index = next(line for line in package.splitlines() if line.startswith('Note sections'))
            self.assertIn(index, hook._note_index_variants(self.note_path().read_text(encoding='utf-8')))

    def test_head_overflow_drops_whole_lines_never_slices_them(self):
        self.write_state(status.render(status.empty()))
        full = hook.startup_package(self.cfg, str(self.repo))
        whole = set(full.splitlines())
        with patch.object(hook, 'CONTEXT_CAP', 300):
            package = hook.startup_package(self.cfg, str(self.repo))
        self.assertLessEqual(len(package), 300)
        for line in package.splitlines():
            self.assertIn(line, whole)

    def test_long_pending_fragment_is_omitted_not_cut(self):
        long_claim = ('Design decision (architect pass, inline since it is small): a market registry '
                      'that maps district names to forecast areas and keeps the newest date per item')
        self.man.db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, consolidated) "
                            "VALUES ('claude','s',1,'s.md','Alpha','t','2026-10-27',0)")
        self.man.db.commit()
        notes.update_project_note(self.note_path(), 'Alpha', '2026-10-27',
                                  pending_add=[f'- [ ] 2026-10-27 [[s]] - {long_claim}'])
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertNotIn(long_claim[:60], package)
        self.assertNotIn('pending:', package)

    def test_short_pending_item_stays_complete(self):
        claim = 'Use SQLite for storage'
        self.man.db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, consolidated) "
                            "VALUES ('claude','s',1,'s.md','Alpha','t','2026-10-27',0)")
        self.man.db.commit()
        notes.update_project_note(self.note_path(), 'Alpha', '2026-10-27',
                                  pending_add=[f'- [ ] 2026-10-27 [[s]] - {claim}'])
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertIn(f'pending: {claim}', package)

    def test_index_labels_resolve_with_mem_note_section(self):
        page = status.empty()
        for key in status.SECTIONS:
            page[key] = [status.Item(key, key + ' item', '[[2026-10-01-s#e1]]')]
        self.write_state(status.render(page))
        notes.update_project_note(self.note_path(), 'Alpha', '2026-10-24',
                                  session_link='s', session_line='- 2026-10-24 [[s|A session]]')
        text = self.note_path().read_text(encoding='utf-8')
        for label in ('Purpose', 'Works now', 'Latest', 'Active', 'Questions', 'Decisions',
                      'Next steps', 'Pending', 'Sessions', 'Notes'):
            self.assertRegex(text, re.compile(r'(?im)^#{1,4} [^\n]*' + re.escape(label)),
                             f'mem note --section {label!r} finds no heading')

    def test_project_without_a_note_has_no_index(self):
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertNotIn('Note sections', package)
        self.assertIn('Project: Alpha', package)

    def test_empty_note_has_no_index(self):
        self.note_path().parent.mkdir(parents=True, exist_ok=True)
        self.note_path().write_text(notes.new_project_note('Alpha', '2026-10-27'), encoding='utf-8')
        self.assertNotIn('Note sections', hook.startup_package(self.cfg, str(self.repo)))


if __name__ == '__main__':
    unittest.main()
