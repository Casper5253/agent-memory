"""P1b lifecycle contracts: hook event history, retry without a final skip, failure visibility, manifest migration."""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import worker
from agentmem.manifest import Manifest
from fixtures import make_vault

# The schema as shipped before P1b. Frozen here so the migration test builds a real v0 manifest.
V0_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  agent TEXT NOT NULL, session_id TEXT NOT NULL,
  project TEXT, cwd TEXT, source TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  captured_until INTEGER NOT NULL DEFAULT 0,
  parts INTEGER NOT NULL DEFAULT 0,
  end_seen INTEGER NOT NULL DEFAULT 0,
  last_activity REAL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL DEFAULT 0, last_error TEXT,
  skip_reason TEXT, created REAL, updated REAL,
  PRIMARY KEY (agent, session_id)
);
CREATE TABLE IF NOT EXISTS captures (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, part INTEGER NOT NULL,
  note_path TEXT NOT NULL, project TEXT, title TEXT, date TEXT,
  start_offset INTEGER, end_offset INTEGER, summarizer TEXT,
  consolidated INTEGER NOT NULL DEFAULT 0, cost_usd REAL DEFAULT 0, digest_sha TEXT, captured_at REAL,
  summary_json TEXT,
  PRIMARY KEY (agent, session_id, part)
);
CREATE INDEX IF NOT EXISTS captures_project ON captures(project, date);
CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status, last_activity);
"""


class HookEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.audit')
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_vault(Path(self.tmp.name))
        self.man = Manifest(self.cfg.manifest_path)
        self.addCleanup(self.man.close)

    def enqueue(self, sid, event, ts, agent='claude'):
        job = worker.enqueue(self.cfg, agent, sid, event=event, cwd='C:/work/demo')
        import json
        data = json.loads(job.read_text(encoding='utf-8'))
        data['ts'] = ts
        job.write_text(json.dumps(data), encoding='utf-8')

    def test_drain_records_each_event_with_session_agent_name_and_time(self):
        self.enqueue('sess-aaaa-1111', 'turn', 1000.0)
        self.enqueue('sess-aaaa-1111', 'end', 2000.0)
        worker.drain_queue(self.cfg, self.man)
        events = [(e['event'], e['ts']) for e in self.man.events('claude', 'sess-aaaa-1111')]
        self.assertEqual(events, [('turn', 1000.0), ('end', 2000.0)])

    def test_repeated_events_are_each_kept(self):
        for ts in (1000.0, 1100.0, 1200.0):
            self.enqueue('sess-bbbb-2222', 'turn', ts)
        worker.drain_queue(self.cfg, self.man)
        self.assertEqual(len(self.man.events('claude', 'sess-bbbb-2222')), 3)

    def test_session_started_but_never_ended_is_listed(self):
        self.enqueue('open-cccc-3333', 'turn', 1000.0)
        self.enqueue('done-dddd-4444', 'turn', 1000.0)
        self.enqueue('done-dddd-4444', 'end', 1500.0)
        worker.drain_queue(self.cfg, self.man)
        unended = {(r['agent'], r['session_id']) for r in self.man.unended()}
        self.assertEqual(unended, {('claude', 'open-cccc-3333')})

    def test_a_turn_after_an_end_makes_the_session_unended_again(self):
        self.enqueue('again-eeee-5555', 'end', 1000.0)
        self.enqueue('again-eeee-5555', 'turn', 2000.0)
        worker.drain_queue(self.cfg, self.man)
        self.assertEqual([r['session_id'] for r in self.man.unended()], ['again-eeee-5555'])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.audit')
        self.addCleanup(self.tmp.cleanup)
        self.man = Manifest(Path(self.tmp.name) / 'manifest.sqlite')
        self.addCleanup(self.man.close)
        self.man.touch('codex', 'missing-tx-0001', end=True, now=0)

    def fail_times(self, times, error='FileNotFoundError: no transcript for codex:missing-tx-0001', now=0):
        for _ in range(times):
            self.man.fail('codex', 'missing-tx-0001', error, now=now)
            now += 10_000  # beyond the largest backoff, so the next fail is not gated
        return now

    def test_session_past_five_attempts_is_still_retried(self):
        self.fail_times(5)
        ready = [r['session_id'] for r in self.man.ready(0, 5, now=10_000_000)]
        self.assertEqual(ready, ['missing-tx-0001'])

    def test_backoff_still_gates_the_next_try(self):
        self.man.fail('codex', 'missing-tx-0001', 'FileNotFoundError: x', now=100)
        self.assertEqual(self.man.ready(0, 5, now=130), [])
        self.assertEqual([r['session_id'] for r in self.man.ready(0, 5, now=161)], ['missing-tx-0001'])

    def test_backoff_is_capped_at_one_hour(self):
        self.fail_times(20, now=0)
        self.man.fail('codex', 'missing-tx-0001', 'FileNotFoundError: x', now=1_000)
        self.assertEqual(self.man.get('codex', 'missing-tx-0001')['next_try'], 1_000 + 3600)

    def test_missing_transcript_is_never_marked_skipped(self):
        self.fail_times(5)
        row = self.man.get('codex', 'missing-tx-0001')
        self.assertEqual(row['status'], 'error')
        self.assertIsNone(row['skip_reason'])

    def test_failed_listing_names_each_session_with_its_reason(self):
        self.fail_times(4)
        self.assertEqual(self.man.failed(5), [])
        self.fail_times(1, now=50_000)
        failed = self.man.failed(5)
        self.assertEqual([(r['session_id'], r['attempts']) for r in failed], [('missing-tx-0001', 5)])
        self.assertEqual(failed[0]['last_error'], 'FileNotFoundError: no transcript for codex:missing-tx-0001')


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / '.audit')
        self.addCleanup(self.tmp.cleanup)
        self.man = Manifest(Path(self.tmp.name) / 'manifest.sqlite')
        self.addCleanup(self.man.close)

    def test_health_counts_each_state_once(self):
        self.man.touch('claude', 'waiting-0001', now=0)
        self.man.record_event('claude', 'waiting-0001', 'turn', 0)
        self.man.touch('codex', 'retrying-0002', end=True, now=0)
        self.man.fail('codex', 'retrying-0002', 'FileNotFoundError: x', now=0)
        self.man.touch('codex', 'failed-0003', end=True, now=0)
        for _ in range(5):
            self.man.fail('codex', 'failed-0003', 'FileNotFoundError: y', now=0)
        self.man.touch('claude', 'done-0004', end=True, now=0)
        self.man.mark('claude', 'done-0004', status='captured')
        self.assertEqual(self.man.health(5), {'pending': 1, 'retrying': 1, 'failed': 1, 'unended': 1})


class MigrationTests(unittest.TestCase):
    def test_v0_manifest_keeps_every_row(self):
        tmp = Path(tempfile.mkdtemp(dir=Path(__file__).resolve().parents[1] / '.audit'))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / 'manifest.sqlite'
        db = sqlite3.connect(path)
        db.executescript(V0_SCHEMA)
        db.execute("INSERT INTO sessions(agent, session_id, project, status, captured_until, parts, attempts, last_error,"
                   " created, updated) VALUES ('claude','v0-sess-0001','Agent Memory','captured',500,2,0,NULL,1,1)")
        db.execute("INSERT INTO sessions(agent, session_id, status, attempts, last_error, end_seen, last_activity)"
                   " VALUES ('codex','v0-sess-0002','error',5,'FileNotFoundError: x',1,1)")
        db.execute("INSERT INTO captures(agent, session_id, part, note_path, project, title, date, end_offset, summarizer,"
                   " consolidated, summary_json) VALUES ('claude','v0-sess-0001',1,'0-Sessions/a.md','Agent Memory','t',"
                   " '2026-10-20',500,'claude-haiku',1,'{}')")
        db.commit()
        db.close()

        man = Manifest(path)
        self.addCleanup(man.close)
        self.assertEqual(man.get('claude', 'v0-sess-0001')['parts'], 2)
        self.assertEqual(man.get('codex', 'v0-sess-0002')['last_error'], 'FileNotFoundError: x')
        self.assertEqual(man.capture('claude', 'v0-sess-0001', 1)['note_path'], '0-Sessions/a.md')
        man.record_event('claude', 'v0-sess-0001', 'turn', 3000.0)
        self.assertEqual(len(man.events('claude', 'v0-sess-0001')), 1)


if __name__ == '__main__':
    unittest.main()
