"""P3 startup contracts: SessionStart is recorded, the startup package carries live Git and capture health, and it stays bounded."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import hook, notes
from agentmem.manifest import Manifest
from fixtures import make_vault

SCRATCH = Path(__file__).resolve().parents[1] / '.audit'


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *args], cwd=str(cwd),
                          capture_output=True, text=True, check=True).stdout.strip()


class StartupBase(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_vault(self.root)
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)
        self.repo = self.root / 'alpha'
        self.repo.mkdir()
        self.projects = {'projects': [{'name': 'Alpha', 'roots': [str(self.repo)], 'status': 'active'}]}
        self.cfg.projects_map.write_text(json.dumps(self.projects), encoding='utf-8')

    def write_state(self, state):
        note = self.cfg.vault / '1-Projects' / 'Alpha' / 'Alpha.md'
        note.parent.mkdir(parents=True, exist_ok=True)
        notes.update_project_note(note, 'Alpha', '2026-10-23', state=state)


class SessionStartRecordingTests(StartupBase):
    def test_session_start_is_recorded_as_a_start_event_without_a_capture(self):
        out = hook.session_start(self.cfg, 'claude', 'start-0001', str(self.repo))
        self.assertEqual([e['event'] for e in self.man.events('claude', 'start-0001')], ['start'])
        self.assertEqual(list(self.cfg.queue_dir.glob('*.json')), [])
        self.assertIn('hookSpecificOutput', out)
        self.assertEqual([r['session_id'] for r in self.man.unended()], ['start-0001'])


class StartupPackageTests(StartupBase):
    def test_live_git_block_reports_branch_head_and_dirty_state(self):
        git(self.repo, 'init', '-q', '-b', 'feature/p3')
        (self.repo / 'a.txt').write_text('one', encoding='utf-8')
        git(self.repo, 'add', 'a.txt')
        git(self.repo, 'commit', '-q', '-m', 'first')
        head = git(self.repo, 'rev-parse', '--short', 'HEAD')

        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertIn(f'branch feature/p3, HEAD {head}, clean', package)

        (self.repo / 'a.txt').write_text('two', encoding='utf-8')
        self.assertIn(f'branch feature/p3, HEAD {head}, dirty', hook.startup_package(self.cfg, str(self.repo)))

    def test_git_block_is_omitted_outside_a_repository(self):
        package = hook.startup_package(self.cfg, str(self.root / 'nowhere'))
        self.assertNotIn('HEAD', package)

    def test_capture_health_line_names_pending_and_failed_sessions(self):
        self.man.touch('claude', 'waiting-0001', now=0)
        self.man.touch('codex', 'broken-0002', end=True, now=0)
        for _ in range(5):
            self.man.fail('codex', 'broken-0002', 'FileNotFoundError: no transcript for codex:broken-0002', now=0)
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertIn('1 session not yet captured', package)
        self.assertIn('1 failed: FileNotFoundError: no transcript for codex:broken-0002', package)

    def test_capture_health_line_is_absent_when_everything_is_captured(self):
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertNotIn('not yet captured', package)
        self.assertNotIn('failed:', package)

    def test_package_stays_within_the_bound_and_keeps_git_and_health(self):
        self.write_state('### Current state\n' + ('- long status line that goes on and on. ' * 200))
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'a.txt').write_text('x', encoding='utf-8')
        git(self.repo, 'add', 'a.txt')
        git(self.repo, 'commit', '-q', '-m', 'first')
        self.man.touch('claude', 'waiting-0003', now=0)
        package = hook.startup_package(self.cfg, str(self.repo))
        self.assertLessEqual(len(package), hook.CONTEXT_CAP)
        self.assertIn('branch main', package)
        self.assertIn('1 session not yet captured', package)

    def test_startup_package_writes_nothing_to_the_vault(self):
        self.write_state('### Current state\n- Working on the P3 startup package')
        before = {p: p.read_bytes() for p in self.cfg.vault.rglob('*') if p.is_file()}
        hook.startup_package(self.cfg, str(self.repo))
        after = {p: p.read_bytes() for p in self.cfg.vault.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
