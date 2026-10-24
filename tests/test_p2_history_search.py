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
