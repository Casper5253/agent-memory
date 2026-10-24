import ast
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentmem import notes  # noqa: E402

WRITER_GATES = {
    "agentmem/notes.py": {
        "render_session": {"redact"},
        "update_project_note": {"redact"},
    },
    "agentmem/pipeline.py": {
        "_finalize_locked": {"_redact_values", "redact_session_note"},
        "_redact_values": {"redact"},
    },
    "agentmem/remember.py": {
        "_evidence_note": {"redact_session_note"},
        "_remembered_list": {"redact"},
        "remember_item": {"redact"},
    },
    "agentmem/knowledge/sources.py": {
        "_packet_dict": {"redact"},
        "build_receipt_packets": {"redact"},
        "_legacy_source": {"redact"},
    },
    "agentmem/knowledge/store.py": {
        "import_capture": {"redact"},
        "save_proposal": {"_redact_data"},
        "_redact_data": {"redact"},
    },
    "agentmem/extract.py": {"build": {"redact"}},
    "agentmem/llm.py": {"summarize": {"redact"}, "consolidate": {"redact"}},
}


def _called_names(function):
    return {node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(function) if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))}


class RedactionGateTests(unittest.TestCase):
    def test_every_enumerated_vault_writer_routes_text_through_redact(self):
        failures = []
        for relative, functions in WRITER_GATES.items():
            source = ROOT / relative
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            definitions = {node.name: node for node in ast.walk(tree)
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for name, required_calls in functions.items():
                called = _called_names(definitions[name])
                missing = required_calls - called
                if missing:
                    failures.append(f"{relative}:{name} missing {sorted(missing)}")
        self.assertEqual(failures, [])

    def test_state_order_token_survives_project_note_redaction(self):
        order = (1792799155000000, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Demo App.md"
            notes.update_project_note(path, "Demo App", "2026-10-24", state="### Current state\n- Use SQLite for storage",
                                      state_order_value=order)
            text = path.read_text(encoding="utf-8")
        self.assertIn("agentmem_state_order: 1792799155000000:1", text)
        self.assertEqual(notes.state_order(text), order)


if __name__ == "__main__":
    unittest.main()
