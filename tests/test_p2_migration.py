import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import notes, status
from fixtures import make_vault
from tools import migrate_status


class MigrationTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.artifacts'
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_vault(self.root)
        self.path = self.cfg.projects_dir / 'Demo App' / 'Demo App.md'
        self.source = '\n\n'.join(['### Current state'] + [f'- occurrence {n}' for n in range(36)] + ['- duplicate', '- duplicate', '- parent\n  continuation\n  - nested child', 'Prose paragraph\ncontinued prose'])
        notes.update_project_note(self.path, 'Demo App', '2026-10-24', state=self.source)

    def converter(self, entries, date):
        return {'sections': {**status.empty(), 'working_now': [{'keep': e.id} for e in entries]}, 'displaced': []}

    def test_40_occurrence_dry_run_accounts_every_original_without_vault_writes(self):
        before = {p: p.read_bytes() for p in self.cfg.vault.rglob('*') if p.is_file()}
        report = migrate_status.plan_vault(self.cfg.vault, self.root / 'out', converter=self.converter, date='2026-10-24')
        self.assertEqual(report[0]['old_entries'], 40)
        self.assertEqual(report[0]['unaccounted'], 0)
        self.assertEqual(report[0]['placed'] + sum(report[0]['displaced'].values()), 40)
        plan = json.loads((self.root / 'out' / report[0]['plan']).read_text(encoding='utf-8'))
        joined = plan['after'] + '\n' + '\n'.join(record['text'] for record in plan['records'])
        for entry in migrate_status.inventory(self.source):
            self.assertIn(entry.raw, joined)
        self.assertEqual(joined.count('- duplicate'), 2)
        self.assertEqual(before, {p: p.read_bytes() for p in self.cfg.vault.rglob('*') if p.is_file()})

    def test_apply_requires_backup_and_rechecks_page_and_history(self):
        report = migrate_status.plan_vault(self.cfg.vault, self.root / 'out', converter=self.converter, date='2026-10-24')
        plan = self.root / 'out' / report[0]['plan']
        with self.assertRaisesRegex(ValueError, 'backup'):
            migrate_status.apply_plan(self.cfg.vault, plan, None)
        backup = self.root / 'backup'
        relative = self.path.relative_to(self.cfg.vault)
        (backup / relative).parent.mkdir(parents=True)
        (backup / relative).write_bytes(self.path.read_bytes())
        self.path.with_name('History.md').write_text('changed history', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'history changed'):
            migrate_status.apply_plan(self.cfg.vault, plan, backup)
        self.path.with_name('History.md').unlink()
        self.path.write_text(self.path.read_text(encoding='utf-8') + '\nchanged', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            migrate_status.apply_plan(self.cfg.vault, plan, backup)

    def test_apply_fixture_and_backup_hash(self):
        report = migrate_status.plan_vault(self.cfg.vault, self.root / 'out', converter=self.converter, date='2026-10-24')
        plan = self.root / 'out' / report[0]['plan']
        backup = self.root / 'backup'
        relative = self.path.relative_to(self.cfg.vault)
        (backup / relative).parent.mkdir(parents=True)
        (backup / relative).write_text('wrong', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'backup'):
            migrate_status.apply_plan(self.cfg.vault, plan, backup)
        (backup / relative).write_bytes(self.path.read_bytes())
        migrate_status.apply_plan(self.cfg.vault, plan, backup)
        status.parse(notes.get_block(self.path.read_text(encoding='utf-8'), notes.STATE_BEGIN, notes.STATE_END))
        self.assertIn('continuation', self.path.with_name('History.md').read_text(encoding='utf-8'))

    def test_legacy_handoff_history_classified_without_model(self):
        folder = self.cfg.projects_dir / 'Other'
        folder.mkdir()
        for name, text in [('Legacy.md', '# old'), ('Handoff.md', '---\ntype: handoff\n---\n# handoff'), ('History.md', '# history')]:
            (folder / name).write_text(text, encoding='utf-8')
        report = migrate_status.plan_vault(self.cfg.vault, self.root / 'out', converter=self.converter)
        classes = {row['path']: row['classification'] for row in report}
        self.assertEqual(classes['1-Projects/Other/Legacy.md'], 'legacy')
        self.assertEqual(classes['1-Projects/Other/Handoff.md'], 'handoff')
        self.assertEqual(classes['1-Projects/Other/History.md'], 'legacy')

    def test_uncertain_conversion_archives_exact_original(self):
        report = migrate_status.plan_vault(self.cfg.vault, self.root / 'out', converter=lambda *a: {'sections': status.empty(), 'displaced': []})
        self.assertEqual(report[0]['placed'], 0)
        self.assertEqual(report[0]['displaced'], {'migration': 40})

    def test_source_hash_is_taken_before_model_call(self):
        original_hash = migrate_status._hash(self.path)
        def change_during_model(entries, date):
            self.path.write_text(self.path.read_text(encoding='utf-8') + '\nconcurrent edit', encoding='utf-8')
            return self.converter(entries, date)
        plan = migrate_status.build_plan(self.cfg.vault, self.path, change_during_model, '2026-10-24')
        self.assertEqual(plan['source_hash'], original_hash)

    def test_nested_bullet_after_blank_line_belongs_to_parent(self):
        raw = '- parent\n\n  continuation\n  - nested'
        entries = migrate_status.inventory(raw)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].raw, raw)
