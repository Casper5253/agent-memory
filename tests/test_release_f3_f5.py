import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem import hook, search, status
from test_p3_startup import StartupBase, git


class ReleaseFixTests(StartupBase):
    def test_unlocated_current_search_excerpt_is_unverified(self):
        self.write_state(status.render({
            **status.empty(),
            "working_now": [status.Item("new", "Storage now uses SQLite", "migration:2026-10-24")],
        }))
        path = self.cfg.vault / "1-Projects" / "Alpha" / "Alpha.md"

        label = search._located_label(path, "CURRENT", "Storage still uses the retired backend")

        self.assertEqual("UNVERIFIED", label)

    def test_excerpt_in_state_block_is_current_when_hit_line_is_stale(self):
        self.write_state(status.render({
            **status.empty(),
            "working_now": [status.Item("new", "Storage now uses SQLite", "migration:2026-10-24")],
        }))
        path = self.cfg.vault / "1-Projects" / "Alpha" / "Alpha.md"

        label = search._located_label(path, "CURRENT", "Storage now uses SQLite", line=1)

        self.assertEqual("CURRENT", label)

    def test_absent_excerpt_with_stale_hit_line_stays_unverified(self):
        self.write_state(status.render({
            **status.empty(),
            "working_now": [status.Item("new", "Storage now uses SQLite", "migration:2026-10-24")],
        }))
        path = self.cfg.vault / "1-Projects" / "Alpha" / "Alpha.md"

        label = search._located_label(path, "CURRENT", "Storage still uses the retired backend", line=1)

        self.assertEqual("UNVERIFIED", label)

    def test_startup_keeps_first_item_from_every_section_with_health_and_recent_links(self):
        git(self.repo, "init", "-q", "-b", "release-test")
        (self.repo / "seed.txt").write_text("seed", encoding="utf-8")
        git(self.repo, "add", "seed.txt")
        git(self.repo, "commit", "-q", "-m", "seed")

        page = status.empty()
        for index, key in enumerate(status.STARTUP_ORDER, 1):
            text = (key + " " + "x" * 280)[:280]
            page[key] = [status.Item(key, text, f"[[0-Sessions/2026/10/2026-10-{index:02d}-x#e{index}]]")]
        self.write_state(status.render(page))

        for index in range(3):
            session_id = f"recent-{index}"
            self.man.touch("claude", session_id, cwd=str(self.repo), end=True, now=100 + index)
            note_path = "0-Sessions/2026/10/" + ("x" * 198) + f"-{index}.md"
            self.assertLessEqual(len(note_path.removesuffix(".md")), 220)
            self.man.record_capture(agent="claude", session_id=session_id, part=1, note_path=note_path,
                project="Alpha", title=f"Recent session {index} " + ("title " * 10), date=f"2026-10-{20 + index:02d}",
                start=0, end=1, summarizer="test", cost=0, sha=f"sha-{index}", consolidated=1,
                summary_json="{}")
        self.man.touch("claude", "pending-session", cwd=str(self.repo), now=200)
        self.man.touch("claude", "broken-session", cwd=str(self.repo), end=True, now=201)
        for _ in range(self.cfg.max_attempts):
            self.man.fail("claude", "broken-session", "transient failure " + "detail " * 20, now=202)

        package = hook.startup_package(self.cfg, str(self.repo))

        self.assertLessEqual(len(package), hook.CONTEXT_CAP)
        self.assertIn("1 session not yet captured", package)
        for key, (heading, _) in status.SECTIONS.items():
            self.assertIn(f"### {heading}", package)
            self.assertIn(page[key][0].text, package)

    def test_startup_health_reports_retrying_failures(self):
        class RetryingOnly:
            @staticmethod
            def health(_max_attempts):
                return {"pending": 0, "retrying": 1, "failed": 0}

            @staticmethod
            def unconsolidated():
                return []

        line = hook._health_line(RetryingOnly(), 5)

        self.assertIn("1 session retrying", line)

