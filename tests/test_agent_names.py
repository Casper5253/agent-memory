"""Every agent a shipped integration names must be accepted by the mem CLI.

20 Nov 2026: the OpenCode plugin called `mem hook opencode` while `hook` only
accepted claude, codex and devin, so OpenCode never got startup context.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentmem.cli import build_parser
from agentmem.config import AGENTS

ROOT = Path(__file__).resolve().parents[1]


class AgentNames(unittest.TestCase):
    def test_hook_accepts_every_agent(self):
        for agent in AGENTS:
            with self.subTest(agent=agent):
                self.assertEqual(build_parser().parse_args(["hook", agent]).agent, agent)

    def test_opencode_plugin_calls_hook_with_a_known_agent(self):
        plugin = (ROOT / "integrations" / "opencode" / "agentmem.js").read_text(encoding="utf-8")
        named = re.findall(r'"hook",\s*"([a-z]+)"', plugin)
        self.assertEqual(named, ["opencode"])
        self.assertLessEqual(set(named), set(AGENTS))
