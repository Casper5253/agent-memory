import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentmem import llm, notes, remember, status
from test_p3_startup import StartupBase


class RememberProvenanceTests(StartupBase):
    def test_remember_has_real_citation_and_matching_anchor(self):
        seen = {}
        def convert(state, summary, link, date, timeout=0):
            seen.update(summary)
            data = {'sections': {**status.empty(), 'decisions': [{'text': 'Use SQLite', 'ref': link[:-2] + '#e1]]'}]}, 'displaced': []}
            with patch.object(llm, '_run', return_value=data):
                return llm.consolidate(state, summary, link, date)
        result = remember.remember_item(self.cfg, 'Use SQLite', kind='decision', rationale='Local data', cwd=str(self.repo), consolidator=convert)
        self.assertTrue(result['consolidated'])
        self.assertEqual(seen['citations'][0]['claim'], 'Use SQLite')
        evidence = Path(result['evidence']).read_text(encoding='utf-8')
        self.assertIn('## Statement\n\nUse SQLite', evidence)
        self.assertIn('### E1\n\n- Claim: Use SQLite', evidence)
        self.assertIn('### E2\n\n- Claim: Local data', evidence)
        self.assertIn('Use SQLite [[', Path(result['updated']).read_text(encoding='utf-8'))
