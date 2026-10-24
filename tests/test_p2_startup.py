import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import hook, notes, status
from test_p3_startup import StartupBase


class StructuredStartupTests(StartupBase):
    def test_cut_whole_items_pointer_and_priority(self):
        page = status.empty()
        for n, key in enumerate(status.SECTIONS):
            page[key] = [status.Item(f'i{n}', key + ' ' + 'x' * 250, '[[s#e1]]')]
        self.write_state(status.render(page))
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertLessEqual(len(package), hook.CONTEXT_CAP)
        self.assertIn('more items in', package)
        self.assertIn('Alpha.md', package)
        self.assertIn('purpose ' + 'x' * 250 + ' [[s#e1]]', package)
        for key in status.SECTIONS:
            if key + ' ' in package:
                self.assertIn(key + ' ' + 'x' * 250 + ' [[s#e1]]', package)

    def test_old_huge_item_omitted_health_retained(self):
        page = status.empty()
        page['purpose'] = [status.Item('i1', 'uniqueold' * 100, '[[old#e1]]')]
        self.write_state(status.render(page))
        self.man.touch('claude', 'waiting', now=0)
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertNotIn('uniqueold', package)
        self.assertIn('1 more items in', package)
        self.assertIn('1 session not yet captured', package)

    def test_captured_unconsolidated_count_and_pending_reason(self):
        self.man.db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, consolidated) VALUES ('claude','s',1,'s.md','Alpha','t','2026-10-24',0)")
        self.man.db.commit()
        path = self.cfg.vault / '1-Projects' / 'Alpha' / 'Alpha.md'
        notes.update_project_note(path, 'Alpha', '2026-10-24', pending_add=['- [ ] [[s]] status update pending (page not migrated; base=x)'])
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertIn('1 captured session awaiting consolidation', package)
        self.assertIn('page not migrated', package)
        self.assertIn('Alpha.md', package)
        self.assertNotIn('- [ ]', package)

    def test_sections_in_requested_order(self):
        page = status.empty()
        for n, key in enumerate(status.SECTIONS):
            page[key] = [status.Item(f'i{n}', key + ' item', '[[s#e1]]')]
        self.write_state(status.render(page))
        package = hook.startup_package(self.cfg, str(self.repo))
        offsets = [package.index(key + ' item') for key in status.STARTUP_ORDER]
        self.assertEqual(offsets, sorted(offsets))
