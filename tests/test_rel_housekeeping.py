import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentmem import llm, status

LIVE_PROCESS = [
    ('next', 'Stay idle until dev-claude assigns a new task'),
    ('next', 'Get the ff86e83 review verdict from Haiku'),
    ('working_now', 'Started a final full-suite run, reported as in progress'),
    ('latest', 'Ran unittest discovery for test_p6_supersede.py and test_p2_status.py'),
    ('active', 'Observed the P7 replay still advancing'),
]

PROJECT_FACTS = [
    ('next', 'Run the full suite before release.'),
    ('working_now', 'Startup context shows compact refs and fits every section.'),
    ('questions', 'Should the status page cite session IDs or only dates?'),
    ('decisions', 'Keep the status page as the only current-state record; reason: session notes are history.'),
    ('active', 'Quantlab holdout stays unopened until the C1 campaign closes.'),
]


def update_with(section, text):
    sections = {**status.empty(), section: [dict(text=text, ref='[[s#e1]]')]}
    return dict(sections=sections, displaced=[]), {'[[s#e1]]': text}


class AgentProcessFilterTests(unittest.TestCase):
    def test_live_process_items_are_refused_and_logged(self):
        for section, text in LIVE_PROCESS:
            with self.subTest(text=text):
                update, allowed = update_with(section, text)
                result = status.reconcile(status.empty(), update, allowed)
                self.assertEqual(result.sections, status.empty())
                self.assertEqual(result.events, [('rejected', f'{section}[0]: agent process')])

    def test_real_project_facts_are_kept(self):
        sections = {key: [] for key in status.SECTIONS}
        allowed = {}
        for n, (section, text) in enumerate(PROJECT_FACTS, 1):
            sections[section].append(dict(text=text, ref=f'[[s#e{n}]]'))
            allowed[f'[[s#e{n}]]'] = text
        result = status.reconcile(status.empty(), dict(sections={**status.empty(), **sections}, displaced=[]), allowed)
        self.assertEqual([i.text for i in result.sections['next']], ['Run the full suite before release.'])
        self.assertEqual([i.text for i in result.sections['working_now']],
                         ['Startup context shows compact refs and fits every section.'])
        self.assertEqual([i.text for i in result.sections['questions']],
                         ['Should the status page cite session IDs or only dates?'])
        self.assertEqual([i.text for i in result.sections['decisions']],
                         ['Keep the status page as the only current-state record; reason: session notes are history.'])
        self.assertEqual([i.text for i in result.sections['active']],
                         ['Quantlab holdout stays unopened until the C1 campaign closes.'])
        self.assertEqual(result.events, [])


class TeamRoleFactTests(unittest.TestCase):
    def test_team_role_name_is_a_kept_project_fact(self):
        update = dict(sections={**status.empty(), 'purpose': [dict(text='dev-claude is the default lead', ref='[[s#e1]]')]},
                      displaced=[])
        result = status.reconcile(status.empty(), update, {'[[s#e1]]': 'dev-claude is the default lead'})
        self.assertEqual([i.text for i in result.sections['purpose']], ['dev-claude is the default lead'])
        self.assertEqual(result.events, [])


class KeepDestinationTests(unittest.TestCase):
    def test_decision_moved_to_next_then_superseded_by_next_stays_a_decision(self):
        old = status.empty()
        old['decisions'] = [status.Item('i1', 'Use SQLite for storage', '[[old#e1]]')]
        first = status.reconcile(old, dict(sections={**status.empty(), 'next': [dict(keep='i1')]}, displaced=[]), {})
        self.assertEqual([i.text for i in first.sections['decisions']], ['Use SQLite for storage'])
        self.assertEqual(first.sections['next'], [])
        self.assertEqual(first.events, [('refused', 'move i1 to next; stays in decisions')])

        claim = 'P7 design first: define how storage fits together.'
        second = status.reconcile(first.sections, dict(
            sections={**status.empty(), 'next': [dict(text=claim, ref='[[next#e1]]')]},
            displaced=[dict(id='i1', reason='superseded', ref='[[next#e1]]', claim=claim)]), {'[[next#e1]]': claim})
        self.assertEqual(second.sections['decisions'], [status.Item('i1', 'Use SQLite for storage', '[[old#e1]]')])
        self.assertEqual(second.displaced, [])
        self.assertEqual([i.text for i in second.sections['next']], [claim])
        self.assertIn(('refused', 'incompatible superseded: i1'), second.events)

    def test_questions_move_among_next_active_and_questions(self):
        old = status.empty()
        old['questions'] = [status.Item('i1', 'Which backend should the status page use?', '[[old#e1]]')]
        result = status.reconcile(old, dict(sections={**status.empty(), 'next': [dict(keep='i1')]}, displaced=[]), {})
        self.assertEqual([i.text for i in result.sections['next']], ['Which backend should the status page use?'])
        self.assertEqual(result.sections['questions'], [])
        self.assertEqual(result.events, [])

    def test_working_now_and_latest_stay_in_place(self):
        old = status.empty()
        old['working_now'] = [status.Item('i1', 'Startup context fits every section', '[[old#e1]]')]
        result = status.reconcile(old, dict(sections={**status.empty(), 'latest': [dict(keep='i1')]}, displaced=[]), {})
        self.assertEqual([i.text for i in result.sections['working_now']], ['Startup context fits every section'])
        self.assertEqual(result.sections['latest'], [])
        self.assertEqual(result.events, [('refused', 'move i1 to latest; stays in working_now')])


class AgentProcessPromptTests(unittest.TestCase):
    def test_consolidation_and_summary_prompts_state_the_rule(self):
        for prompt in (llm.CONSOLIDATE_SYSTEM, llm.SUMMARY_SYSTEM):
            with self.subTest(prompt=prompt[:40]):
                self.assertIn('Never record agent process', prompt)
                self.assertIn('Run the full suite before release', prompt)


if __name__ == '__main__':
    unittest.main()
