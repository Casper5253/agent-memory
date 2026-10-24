"""Ingestion manifest (SQLite, WAL). Identity of a session is (agent, session_id).

sessions: one row per agent session, tracks activity, settle state, capture cursor and retries.
captures: one row per immutable session note (a resumed session that grows adds part 2, 3, ...).
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  agent TEXT NOT NULL, session_id TEXT NOT NULL,
  project TEXT, cwd TEXT, source TEXT,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | captured | skipped | error | legacy
  captured_until INTEGER NOT NULL DEFAULT 0,   -- transcript cursor already turned into notes
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
CREATE TABLE IF NOT EXISTS hook_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL, session_id TEXT NOT NULL, event TEXT NOT NULL, ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS hook_events_session ON hook_events(agent, session_id, id);
CREATE TABLE IF NOT EXISTS metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class Manifest:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def discovery_since(self, now=None):
        """Return the persisted start time for orphan discovery, initializing it once."""
        now = time.time() if now is None else float(now)
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('discovery_since', ?)", (str(now),))
            row = db.execute("SELECT value FROM metadata WHERE key='discovery_since'").fetchone()
        return float(row["value"])

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ sessions
    def touch(self, agent, session_id, *, cwd=None, source=None, end=False, now=None):
        """Record activity. A captured/skipped session that shows new activity becomes pending again."""
        now = now or time.time()
        with self.tx() as db:
            row = db.execute("SELECT status FROM sessions WHERE agent=? AND session_id=?", (agent, session_id)).fetchone()
            if row is None:
                db.execute("INSERT INTO sessions(agent, session_id, cwd, source, end_seen, last_activity, created, updated)"
                           " VALUES (?,?,?,?,?,?,?,?)", (agent, session_id, cwd, source, int(end), now, now, now))
                return
            # New activity reopens a finished session; the capture cursor guarantees nothing is re-captured.
            status = "pending" if row["status"] in ("captured", "skipped") else row["status"]
            db.execute("UPDATE sessions SET cwd=COALESCE(?, cwd), source=COALESCE(?, source), last_activity=?,"
                       " end_seen=CASE WHEN ? THEN 1 ELSE end_seen END, status=?, updated=? WHERE agent=? AND session_id=?",
                       (cwd, source, now, int(end), status, now, agent, session_id))

    def record_event(self, agent, session_id, event, ts=None):
        """Keep every hook event so a session's start and end can be reconstructed later."""
        with self.tx() as db:
            db.execute("INSERT INTO hook_events(agent, session_id, event, ts) VALUES (?,?,?,?)",
                       (agent, session_id, event, ts or time.time()))

    def events(self, agent, session_id):
        return self.db.execute("SELECT agent, session_id, event, ts FROM hook_events WHERE agent=? AND session_id=?"
                               " ORDER BY id", (agent, session_id)).fetchall()

    def unended(self):
        """Sessions whose latest recorded hook event is not an end: started (or resumed) and never closed."""
        return self.db.execute(
            "SELECT e.agent, e.session_id, e.event AS last_event, e.ts AS last_ts FROM hook_events e"
            " JOIN (SELECT agent, session_id, max(id) AS last_id FROM hook_events GROUP BY agent, session_id) m"
            " ON e.id = m.last_id WHERE e.event != 'end' ORDER BY e.ts").fetchall()

    def get(self, agent, session_id):
        return self.db.execute("SELECT * FROM sessions WHERE agent=? AND session_id=?", (agent, session_id)).fetchone()

    def ready(self, settle_s: float, _max_attempts=None, now=None):
        """Sessions to capture now: ended or idle past the settle window, and not in retry backoff.

        Errors are retried indefinitely under the capped backoff (see fail). The old attempt cap is gone: a
        transient failure, such as a transcript that is not written yet, must not be given up on. Use failed()
        to list the sessions that have exhausted a given number of attempts.
        """
        now = now or time.time()
        return self.db.execute(
            "SELECT * FROM sessions WHERE status IN ('pending','error') AND COALESCE(next_try,0) <= ?"
            " AND (end_seen = 1 OR last_activity <= ?) ORDER BY last_activity", (now, now - settle_s)).fetchall()

    def failed(self, max_attempts: int):
        """Sessions in error after at least max_attempts failures, with the reason from the latest failure."""
        return self.db.execute(
            "SELECT agent, session_id, attempts, last_error, next_try, updated FROM sessions"
            " WHERE status='error' AND attempts >= ? ORDER BY updated DESC", (max_attempts,)).fetchall()

    def mark(self, agent, session_id, **fields):
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE sessions SET {cols} WHERE agent=? AND session_id=?", (*fields.values(), agent, session_id))

    def reopen_for_growth(self, agent, session_id, *, end, source, cwd, modified):
        """Reopen a completed scan only when the transcript extends beyond its cursor."""
        with self.tx() as db:
            changed = db.execute(
                "UPDATE sessions SET status='pending', source=COALESCE(?, source), cwd=COALESCE(?, cwd), "
                "last_activity=?, end_seen=0, updated=? WHERE agent=? AND session_id=? "
                "AND status IN ('captured','skipped') AND captured_until < ?",
                (source, cwd, modified, time.time(), agent, session_id, end)).rowcount
            return changed == 1

    def fail(self, agent, session_id, error: str, now=None):
        now = now or time.time()
        with self.tx() as db:
            row = db.execute("SELECT attempts FROM sessions WHERE agent=? AND session_id=?", (agent, session_id)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            backoff = min(3600, 60 * 2 ** (attempts - 1))
            db.execute("UPDATE sessions SET status='error', attempts=?, next_try=?, last_error=?, updated=? WHERE agent=? AND session_id=?",
                       (attempts, now + backoff, error[:500], now, agent, session_id))

    def health(self, max_attempts: int):
        """Counts for the session-start hook: sessions not yet captured, retrying errors, exhausted errors, unended."""
        rows = self.db.execute(
            "SELECT"
            " sum(status='pending') AS pending,"
            " sum(status='error' AND attempts < ?) AS retrying,"
            " sum(status='error' AND attempts >= ?) AS failed"
            " FROM sessions", (max_attempts, max_attempts)).fetchone()
        return {"pending": rows["pending"] or 0, "retrying": rows["retrying"] or 0, "failed": rows["failed"] or 0,
                "unended": len(self.unended())}

    # ------------------------------------------------------------------ captures
    def capture(self, agent, session_id, part):
        return self.db.execute("SELECT * FROM captures WHERE agent=? AND session_id=? AND part=?",
                               (agent, session_id, part)).fetchone()

    def record_capture(self, *, agent, session_id, part, note_path, project, title, date, start, end, summarizer,
                       cost, sha, consolidated, summary_json, error=None):
        now = time.time()
        with self.tx() as db:
            existing = db.execute("SELECT * FROM captures WHERE agent=? AND session_id=? AND part=?",
                                  (agent, session_id, part)).fetchone()
            if existing:
                evidence = ("note_path", "project", "title", "date", "start_offset", "end_offset", "summarizer", "digest_sha", "summary_json")
                expected = (note_path, project, title, date, start, end, summarizer, sha, summary_json)
                if tuple(existing[k] for k in evidence) != expected:
                    raise ValueError("capture_identity_conflict: manifest evidence differs")
                return
            db.execute("INSERT INTO captures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (agent, session_id, part, note_path, project, title, date, start, end, summarizer,
                        int(consolidated), cost, sha, now, summary_json))
            db.execute("UPDATE sessions SET status='captured', captured_until=?, parts=?, project=?, attempts=0, skip_reason=NULL,"
                       " next_try=0, last_error=?, end_seen=0, updated=? WHERE agent=? AND session_id=?",
                       (end, part, project, error, now, agent, session_id))

    def set_consolidated(self, agent, session_id, part, value=1, cost=0.0):
        with self.tx() as db:
            db.execute("UPDATE captures SET consolidated=?, cost_usd=cost_usd+? WHERE agent=? AND session_id=? AND part=?",
                       (value, cost, agent, session_id, part))

    def insert_legacy(self, *, agent, session_id, note_path, project, title, date):
        """Register a migrated pre-agentmem note so `sessions` and dedupe know about it."""
        now = time.time()
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO sessions(agent, session_id, project, status, parts, created, updated)"
                       " VALUES (?,?,?,'legacy',1,?,?)", (agent, session_id, project, now, now))
            db.execute("INSERT OR IGNORE INTO captures(agent, session_id, part, note_path, project, title, date,"
                       " summarizer, consolidated, captured_at) VALUES (?,?,1,?,?,?,?,'legacy',1,?)",
                       (agent, session_id, note_path, project, title, date, now))

    def unconsolidated(self):
        return self.db.execute("SELECT * FROM captures WHERE consolidated=0 AND project IS NOT NULL"
                               " AND project != '' ORDER BY captured_at").fetchall()

    def query_captures(self, project=None, agent=None, since=None, until=None, limit=50):
        sql, args = "SELECT * FROM captures WHERE 1=1", []
        for col, op, val in (("project", "=", project), ("agent", "=", agent), ("date", ">=", since), ("date", "<=", until)):
            if val:
                sql += f" AND {col} {op} ?" if col != "project" else " AND lower(project) = lower(?)"
                args.append(val)
        sql += " ORDER BY date DESC, captured_at DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def stats(self):
        s = {r[0]: r[1] for r in self.db.execute("SELECT status, count(*) FROM sessions GROUP BY status")}
        c = self.db.execute("SELECT count(*), coalesce(sum(cost_usd),0), max(captured_at), sum(consolidated=0) FROM captures").fetchone()
        errors = self.db.execute("SELECT agent, session_id, attempts, last_error FROM sessions WHERE status='error'"
                                 " ORDER BY updated DESC LIMIT 5").fetchall()
        return {"sessions": s, "notes": c[0], "cost_usd": round(c[1], 4), "last_capture": c[2],
                "unconsolidated": c[3] or 0, "errors": [dict(e) for e in errors]}
