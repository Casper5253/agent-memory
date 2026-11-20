"""Every agent a shipped integration names must be accepted by the mem CLI.

20 Nov 2026: the OpenCode plugin called `mem hook opencode` while `hook` only
accepted claude, codex and devin, so OpenCode never got startup context.
"""
import re
from pathlib import Path

import pytest

from agentmem.cli import build_parser
from agentmem.config import AGENTS

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("agent", AGENTS)
def test_hook_accepts_every_agent(agent):
    assert build_parser().parse_args(["hook", agent]).agent == agent


def test_opencode_plugin_calls_hook_with_a_known_agent():
    plugin = (ROOT / "integrations" / "opencode" / "agentmem.js").read_text(encoding="utf-8")
    named = re.findall(r'"hook",\s*"([a-z]+)"', plugin)
    assert named == ["opencode"]
    assert set(named) <= set(AGENTS)
