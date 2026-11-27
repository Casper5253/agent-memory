import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import notes, search, status
from test_p3_startup import StartupBase


class ProjectHistorySearchTests(StartupBase):
    def setUp(self):
        super().setUp()
        self.project_note = self.cfg.projects_dir / 'Alpha' / 'Alpha.md'
        self.history_note = notes.project_history_path(self.project_note)
        page = status.empty()
        page['decisions'] = [status.Item('i1', 'Storage uses Postgres instead of SQLite', '[[new#e1]]')]
        self.write_state(status.render(page))
        self.history_note.write_text('# Alpha history\n' + '- Storage used SQLite\n' * 20, encoding='utf-8')
        self.qmd_hits = [
            dict(file='qmd://vault-projects/Alpha/History.md', score=100, snippet='Storage used SQLite'),
            dict(file='qmd://vault-projects/Alpha/Alpha.md', score=1, snippet='Storage uses Postgres instead of SQLite'),
        ]

    def check_hits(self, hits):
        self.assertEqual(Path(hits[0]['path']), self.project_note)
        self.assertEqual(hits[0]['label'], 'CURRENT')
        self.assertEqual(Path(hits[1]['path']), self.history_note)
        self.assertEqual(hits[1]['label'], 'HISTORICAL')

    def test_fallback_labels_history_and_ranks_project_above_it(self):
        self.check_hits(search.fallback_search(self.cfg, 'Storage SQLite', 10))

    def test_lexical_labels_history_and_ranks_project_above_it(self):
        for query in ('SQLite', 'Storage SQLite'):
            with self.subTest(query=query), patch.object(search, '_qmd_search', return_value=self.qmd_hits):
                self.check_hits(search.search(self.cfg, query, n=10))

    def test_layer_filters_cannot_return_history_as_current(self):
        for lexical in (False, True):
            for layer, expected in [('CURRENT', self.project_note), ('HISTORICAL', self.history_note)]:
                with self.subTest(lexical=lexical, layer=layer), patch.object(search, '_qmd_search', return_value=self.qmd_hits):
                    find = search.search if lexical else search.fallback_search
                    hits = find(self.cfg, 'SQLite', 10, layers=[layer])
                    self.assertEqual([Path(hit['path']) for hit in hits], [expected])
                    self.assertEqual(hits[0]['label'], layer)

HISTORY_TEXT = "\n".join([
    "# Alpha history",
    "<!-- agentmem:history:aaaa -->",
    "- 2026-11-01 [decisions; limit] Removed the daily delegation XP cap so accepted agent items pay 25 percent of their value. [[0-Sessions/2026/11/2026-11-01-a#e15]] (evidence [[0-Sessions/2026/11/2026-11-01-a#e99]]; claim: accepted items pay.)",
    "<!-- agentmem:history:bbbb -->",
    "- 2026-10-20 [decisions; limit] The daily delegation XP cap stays in force for agent items. [[0-Sessions/2026/10/2026-10-20-b]]",
    "<!-- agentmem:history:cccc -->",
    "- 2026-11-02 [notes; minor] Unrelated release notes mention daily palette colours. [[0-Sessions/2026/11/2026-11-02-c]]",
    "<!-- agentmem:history:dddd -->",
    "- [ ] 2026-10-16 [[0-Sessions/2026/10/2026-10-16-d]] — Legacy agent-completed tasks pay the same as tasks the user completes, with no daily cap. capture-ref:ab15c0",
    "<!-- agentmem:history:eeee -->",
    "- 2026-10-15 — Legacy entry without a checkbox cites a session ([[0-Sessions/2026/10/2026-10-15-e]])",
    "<!-- agentmem:history:ffff -->",
    "- 2026-10-31 [pending-overflow]",
    "<!-- agentmem:pending-overflow:hash -->",
    "- [ ] 2026-11-01 [[0-Sessions/2026/11/2026-11-01-f]] — Agent-completed tasks pay the same as tasks the user completes, with no daily cap. capture-ref:ab15c0",
    "",
])


class HistoryEntryParsingTests(StartupBase):
    def test_history_entries_parse_every_real_format(self):
        by_line = {entry.line: entry for entry in search.history_entries(HISTORY_TEXT)}
        self.assertEqual(sorted(by_line), [3, 5, 7, 9, 11, 15])

        decision = by_line[3]
        self.assertEqual(decision.date, "2026-11-01")
        self.assertEqual(decision.section, "decisions")
        self.assertEqual(decision.reason, "limit")
        self.assertEqual(decision.citation, "[[0-Sessions/2026/11/2026-11-01-a#e99]]")
        self.assertTrue(decision.text.startswith("- 2026-11-01 [decisions; limit]"))
        self.assertIn("Removed the daily delegation XP cap", decision.text)

        legacy = by_line[9]
        self.assertEqual(legacy.date, "2026-10-16")
        self.assertEqual(legacy.section, "")
        self.assertEqual(legacy.reason, "")
        self.assertEqual(legacy.citation, "[[0-Sessions/2026/10/2026-10-16-d]]")
        self.assertIn("Legacy agent-completed tasks", legacy.text)
        self.assertNotIn("capture-ref", legacy.text)
        self.assertNotIn("[ ]", legacy.text)

        plain = by_line[11]
        self.assertEqual(plain.date, "2026-10-15")
        self.assertIn("Legacy entry without a checkbox", plain.text)
        self.assertEqual(plain.citation, "[[0-Sessions/2026/10/2026-10-15-e]]")

        overflow = by_line[15]
        self.assertEqual(overflow.date, "2026-11-01")
        self.assertEqual(overflow.line, 15)
        self.assertEqual(overflow.section, "pending-overflow")
        self.assertEqual(overflow.reason, "")
        self.assertEqual(overflow.citation, "[[0-Sessions/2026/11/2026-11-01-f]]")
        self.assertIn("Agent-completed tasks pay the same", overflow.text)
        self.assertNotIn("pending-overflow", overflow.text)
        self.assertNotIn("agentmem", overflow.text)
        self.assertNotIn("capture-ref", overflow.text)

    def test_prefix_matching_covers_plural_forms(self):
        self.assertEqual(search._entry_score("The task list is empty.", ["tasks"]), 1)
        self.assertEqual(search._entry_score("The minified bundle is served.", ["minify"]), 1)

    def test_citations_do_not_count_as_evidence(self):
        text = "Unrelated note [[0-Sessions/2026/11/2026-11-03-minified-and-cap]]"
        self.assertEqual(search._entry_score(text, ["minified", "cap"]), 0)


class HistorySearchTests(StartupBase):
    def setUp(self):
        super().setUp()
        self.project_note = self.cfg.projects_dir / "Alpha" / "Alpha.md"
        self.history_note = notes.project_history_path(self.project_note)
        self.history_note.parent.mkdir(parents=True, exist_ok=True)
        self.history_note.write_text(HISTORY_TEXT, encoding="utf-8")

    def qmd_hit(self, line=3):
        return dict(file="qmd://vault-projects/Alpha/History.md", score=100, line=line,
                    snippet="raw middle window from the history file")

    def history_hit(self, hits):
        return next(hit for hit in hits if Path(hit["path"]) == self.history_note)

    def test_search_returns_whole_matching_entries_not_a_raw_window(self):
        with patch.object(search, "_qmd_search", return_value=[self.qmd_hit()]):
            hit = self.history_hit(search.search(self.cfg, "delegation cap", n=10))
        self.assertNotIn("raw middle window", hit["snippet"])
        self.assertIn("Removed the daily delegation XP cap", hit["snippet"])
        self.assertIn("stays in force", hit["snippet"])
        self.assertTrue(hit["snippet"].startswith("2 matching entries, newest first."))
        self.assertEqual(hit["line"], 3)

    def test_conflicting_decisions_come_newest_first_and_a_newer_weak_match_is_ignored(self):
        with patch.object(search, "_qmd_search", return_value=[self.qmd_hit()]):
            hit = self.history_hit(search.search(self.cfg, "delegation cap", n=10))
        self.assertLess(hit["snippet"].index("Removed the daily"), hit["snippet"].index("stays in force"))
        self.assertNotIn("palette", hit["snippet"])

    def test_qmd_line_fallback_returns_the_containing_entry_when_no_term_scores(self):
        with patch.object(search, "_qmd_search", return_value=[self.qmd_hit(15)]):
            hit = self.history_hit(search.search(self.cfg, "zzzzzz", n=10))
        self.assertEqual(hit["line"], 15)
        self.assertIn("Agent-completed tasks pay the same", hit["snippet"])
        self.assertNotIn("raw middle window", hit["snippet"])

    def test_fallback_search_returns_entries_when_qmd_fails(self):
        with patch.object(search, "_qmd_search", side_effect=FileNotFoundError("qmd not installed")):
            hit = self.history_hit(search.search(self.cfg, "delegation cap", n=10))
        self.assertTrue(hit["snippet"].startswith("2 matching entries, newest first."))
        self.assertIn("Removed the daily delegation XP cap", hit["snippet"])
        self.assertIn("stays in force", hit["snippet"])


class HistorySnippetCapTests(StartupBase):
    def test_render_keeps_three_entries_within_the_character_cap(self):
        lines = ["# Alpha history"]
        for day in range(1, 5):
            lines.append(f"- 2026-11-0{day} [notes; capacity] capacity entry{day} " + "word " * 60)
        text = "\n".join(lines) + "\n"
        self.assertEqual(len(search.history_entries(text)), 4)
        selected = search._history_selection(search._parse_history(text), "capacity", None)
        self.assertEqual([entry.date for entry in selected], ["2026-11-04", "2026-11-03", "2026-11-02"])
        snippet, line = search._history_snippet(selected)
        self.assertTrue(snippet.startswith("3 matching entries, newest first."))
        self.assertLessEqual(len(snippet), 1200)
        self.assertEqual(line, 5)

    def test_render_drops_whole_entries_to_stay_under_1200(self):
        lines = ["# Alpha history"]
        for day in range(1, 5):
            lines.append(f"- 2026-11-0{day} [notes; capacity] capacity entry{day} " + "word " * 90)
        text = "\n".join(lines) + "\n"
        selected = search._history_selection(search._parse_history(text), "capacity", None)
        snippet, _ = search._history_snippet(selected)
        self.assertLessEqual(len(snippet), 1200)
        self.assertTrue(snippet.startswith("2 matching entries, newest first."))
        self.assertIn("entry4", snippet)
        self.assertIn("entry3", snippet)
        self.assertNotIn("entry2", snippet)
        self.assertNotIn("…", snippet)

    def test_a_single_oversize_entry_is_truncated_with_an_ellipsis(self):
        text = "# Alpha history\n- 2026-11-01 [notes; capacity] capacity " + "word " * 300 + "\n"
        entries = search.history_entries(text)
        self.assertEqual(len(entries), 1)
        snippet, line = search._history_snippet(entries)
        self.assertLessEqual(len(snippet), 1200)
        self.assertTrue(snippet.endswith("…"))
        self.assertEqual(line, 2)
