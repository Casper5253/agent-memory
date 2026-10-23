"""Majority-vote multi-term search.

Tests that multi-term queries use per-term QMD searches with strict-majority
filtering, single-term queries are unchanged, the fallback follows the same
rule, and the term cap holds.

Uses a real isolated QMD index under .audit/tmp (same isolation pattern as
tools/evaluate_knowledge.py).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentmem.config import Config
from agentmem.search import (
    _build_hit, _majority_merge, _qmd_search, _split_terms,
    fallback_search, qmd_cmd, search,
)
import fixtures as fx


def _qmd_base():
    shim = shutil.which("qmd")
    node = shutil.which("node")
    if not shim:
        return None
    js = Path(shim).parent / "node_modules" / "@tobilu" / "qmd" / "bin" / "qmd"
    if node and js and js.exists():
        return [node, str(js)]
    if shim and not shim.lower().endswith((".cmd", ".bat")):
        return [shim]
    return None


def _make_isolated_env(base):
    base = Path(base)
    dirs = {
        "HOME": base / "os-home", "USERPROFILE": base / "os-home",
        "APPDATA": base / "os-home/AppData/Roaming", "LOCALAPPDATA": base / "os-home/AppData/Local",
        "XDG_CACHE_HOME": base / "cache", "XDG_CONFIG_HOME": base / "config",
        "XDG_DATA_HOME": base / "data", "QMD_CONFIG_DIR": base / "config/qmd",
        "TMP": base / "tmp", "TEMP": base / "tmp", "TMPDIR": base / "tmp",
        "NODE_COMPILE_CACHE": base / "cache/node",
    }
    env = dict(os.environ)
    for key, path in dirs.items():
        Path(path).mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    for key in ("AGENTMEM_CAPTURE", "NODE_OPTIONS", "QMD_WRAPPER_CAPTURE"):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    return env


def _run_qmd(env, args, index_name="testidx", timeout=60):
    base = _qmd_base()
    if base is None:
        raise unittest.SkipTest("qmd not installed")
    cmd = base + ["--index", index_name] + args
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    return r


class SplitTermsTest(unittest.TestCase):
    def test_dedup_and_cap(self):
        self.assertEqual(_split_terms("alpha beta alpha gamma"), ["alpha", "beta", "gamma"])

    def test_cap_at_six(self):
        result = _split_terms("a b c d e f g h i")
        self.assertEqual(len(result), 6)
        self.assertEqual(result, ["a", "b", "c", "d", "e", "f"])

    def test_case_insensitive_dedup(self):
        self.assertEqual(_split_terms("Alpha alpha BETA beta"), ["Alpha", "BETA"])

    def test_empty(self):
        self.assertEqual(_split_terms(""), [])
        self.assertEqual(_split_terms("   "), [])


class MajorityMergeTest(unittest.TestCase):
    def test_strict_majority_keeps_and_ranks(self):
        per_term = [
            [{"_disk_path": "/a", "score": 5}, {"_disk_path": "/b", "score": 3}],
            [{"_disk_path": "/a", "score": 4}, {"_disk_path": "/c", "score": 2}],
            [{"_disk_path": "/a", "score": 6}],
            [{"_disk_path": "/d", "score": 7}],
        ]
        result = _majority_merge(per_term, 4, 10)
        paths = [it["_disk_path"] for it in result]
        self.assertIn("/a", paths)
        self.assertNotIn("/b", paths)
        self.assertNotIn("/c", paths)
        self.assertNotIn("/d", paths)

    def test_threshold_is_strict(self):
        per_term = [
            [{"_disk_path": "/x", "score": 1}],
            [{"_disk_path": "/x", "score": 1}],
            [{"_disk_path": "/x", "score": 1}],
            [{"_disk_path": "/x", "score": 1}],
        ]
        result = _majority_merge(per_term, 4, 10)
        self.assertEqual(len(result), 1)

        per_term_half = [
            [{"_disk_path": "/x", "score": 1}],
            [{"_disk_path": "/x", "score": 1}],
            [{"_disk_path": "/y", "score": 1}],
            [{"_disk_path": "/y", "score": 1}],
        ]
        result2 = _majority_merge(per_term_half, 4, 10)
        self.assertEqual(len(result2), 0)

    def test_ranking_by_count_then_score_then_path(self):
        per_term = [
            [{"_disk_path": "/a", "score": 1}, {"_disk_path": "/b", "score": 10}],
            [{"_disk_path": "/a", "score": 1}, {"_disk_path": "/b", "score": 1}],
            [{"_disk_path": "/a", "score": 1}],
        ]
        result = _majority_merge(per_term, 3, 10)
        paths = [it["_disk_path"] for it in result]
        self.assertEqual(paths[0], "/a")

    def test_n_cap(self):
        per_term = [
            [{"_disk_path": f"/p{i}", "score": i} for i in range(10)],
            [{"_disk_path": f"/p{i}", "score": i} for i in range(10)],
            [{"_disk_path": f"/p{i}", "score": i} for i in range(10)],
        ]
        result = _majority_merge(per_term, 3, 3)
        self.assertEqual(len(result), 3)


class QMDMajorityTest(unittest.TestCase):
    """Integration tests using a real isolated QMD index."""

    @classmethod
    def setUpClass(cls):
        if _qmd_base() is None:
            raise unittest.SkipTest("qmd not installed")
        audit_tmp = ROOT / ".audit" / "tmp"
        audit_tmp.mkdir(parents=True, exist_ok=True)
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="majority-test-", dir=str(audit_tmp))
        cls.base = Path(cls._tmpdir.name)
        cls.vault = cls.base / "vault"
        for d in ("_meta/queue", "_meta/locks", "_meta/logs", "0-Sessions", "1-Projects",
                  "2-Areas", "3-Resources", "4-Archive"):
            (cls.vault / d).mkdir(parents=True, exist_ok=True)

        (cls.vault / "3-Resources" / "three-match.md").write_text(
            "# Three terms\n\nThis note covers alpha beta and gamma topics in depth. " * 10, encoding="utf-8")
        (cls.vault / "3-Resources" / "one-match.md").write_text(
            "# One term\n\nThis note only mentions alpha nowhere near the others. " * 10, encoding="utf-8")
        (cls.vault / "3-Resources" / "all-four.md").write_text(
            "# All terms\n\nDocument covering alpha beta gamma delta comprehensively. " * 10, encoding="utf-8")
        (cls.vault / "3-Resources" / "distractor.md").write_text(
            "# Noise\n\nCompletely unrelated content about other topics entirely. " * 10, encoding="utf-8")

        cls.env = _make_isolated_env(cls.base)
        cls.index_name = "testidx"

        for folder, name in [("1-Projects", "vault-projects"), ("0-Sessions", "vault-sessions"),
                             ("2-Areas", "vault-areas"), ("3-Resources", "vault-resources"),
                             ("4-Archive", "vault-archive")]:
            _run_qmd(cls.env, ["collection", "add", str(cls.vault / folder),
                               "--name", name, "--mask", "**/*.md"], cls.index_name)
        _run_qmd(cls.env, ["update"], cls.index_name, timeout=120)

        cls.cfg = Config(
            vault=cls.vault, summarizer="none", auto_index=False,
            embeddings=False, qmd_index=cls.index_name,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _search(self, terms, n=8, **kwargs):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return search(self.cfg, terms, n=n, **kwargs)

    def test_three_of_four_returned_and_ranked_first(self):
        hits = self._search("alpha beta gamma delta", n=8)
        paths = [h["path"] for h in hits]
        three_match = str(self.vault / "3-Resources" / "three-match.md")
        all_four = str(self.vault / "3-Resources" / "all-four.md")
        self.assertIn(three_match, paths, "note matching 3/4 terms must be returned")
        self.assertIn(all_four, paths, "note matching 4/4 terms must be returned")
        three_idx = paths.index(three_match) if three_match in paths else 999
        all_idx = paths.index(all_four) if all_four in paths else 999
        self.assertLess(all_idx, three_idx,
                        "4/4 match must rank above 3/4 match (count is primary rank)")

    def test_one_of_four_distractor_not_returned(self):
        hits = self._search("alpha beta gamma delta", n=8)
        paths = [h["path"] for h in hits]
        one_match = str(self.vault / "3-Resources" / "one-match.md")
        self.assertNotIn(one_match, paths,
                         "note matching only 1/4 terms must be excluded by majority filter")

    def test_unrelated_note_not_returned(self):
        hits = self._search("alpha beta gamma delta", n=8)
        paths = [h["path"] for h in hits]
        distractor = str(self.vault / "3-Resources" / "distractor.md")
        self.assertNotIn(distractor, paths)

    def test_single_term_returns_all_matching(self):
        hits = self._search("alpha", n=8)
        paths = [h["path"] for h in hits]
        self.assertIn(str(self.vault / "3-Resources" / "three-match.md"), paths)
        self.assertIn(str(self.vault / "3-Resources" / "one-match.md"), paths)
        self.assertIn(str(self.vault / "3-Resources" / "all-four.md"), paths)

    def test_single_term_output_shape(self):
        hits = self._search("alpha", n=8)
        self.assertTrue(len(hits) > 0)
        expected_keys = {"label", "path", "title", "score", "line", "docid", "snippet"}
        for h in hits:
            self.assertEqual(set(h.keys()), expected_keys,
                             f"single-term hit has unexpected keys: {set(h.keys()) - expected_keys}")

    def test_multi_term_output_shape(self):
        hits = self._search("alpha beta gamma delta", n=8)
        self.assertTrue(len(hits) > 0)
        expected_keys = {"label", "path", "title", "score", "line", "docid", "snippet"}
        for h in hits:
            self.assertEqual(set(h.keys()), expected_keys,
                             f"multi-term hit has unexpected keys: {set(h.keys()) - expected_keys}")

    def test_term_cap_holds(self):
        many_terms = "alpha beta gamma delta " + " ".join(f"uniqueterm{i}" for i in range(6))
        words = _split_terms(many_terms)
        self.assertEqual(len(words), 6)
        hits = self._search(many_terms, n=8)
        paths = [h["path"] for h in hits]
        all_four = str(self.vault / "3-Resources" / "all-four.md")
        self.assertIn(all_four, paths,
                      "4/6 matches passes strict majority (4 > 3) even with 6-term cap")
        one_match = str(self.vault / "3-Resources" / "one-match.md")
        self.assertNotIn(one_match, paths,
                         "1/6 matches excluded even when query exceeds term cap")


class FallbackMajorityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = fx.tmpdir()
        self.tmp = Path(self._tmp.name)
        self.cfg = fx.make_vault(self.tmp)
        (self.cfg.vault / "3-Resources" / "three-words.md").write_text(
            "# Three words\n\nThis covers storage migration and backup strategies. " * 10, encoding="utf-8")
        (self.cfg.vault / "3-Resources" / "one-word.md").write_text(
            "# One word\n\nThis only talks about storage and nothing else relevant. " * 10, encoding="utf-8")
        (self.cfg.vault / "3-Resources" / "all-words.md").write_text(
            "# All words\n\nComplete coverage of storage migration backup compression topics. " * 10, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_majority_rule_four_words(self):
        hits = fallback_search(self.cfg, "storage migration backup compression", 10)
        paths = [h["path"] for h in hits]
        three = str(self.cfg.vault / "3-Resources" / "three-words.md")
        one = str(self.cfg.vault / "3-Resources" / "one-word.md")
        allw = str(self.cfg.vault / "3-Resources" / "all-words.md")
        self.assertIn(three, paths, "3/4 matches passes strict majority")
        self.assertIn(allw, paths, "4/4 matches passes strict majority")
        self.assertNotIn(one, paths, "1/4 matches excluded by strict majority")

    def test_single_word_fallback_unchanged(self):
        hits = fallback_search(self.cfg, "storage", 10)
        paths = [h["path"] for h in hits]
        self.assertTrue(len(paths) >= 2, "single-word fallback returns all matching notes")

    def test_fallback_two_words_both_required(self):
        (self.cfg.vault / "3-Resources" / "only-first.md").write_text(
            "# Only first\n\nJust mentions storage everywhere. " * 10, encoding="utf-8")
        hits = fallback_search(self.cfg, "storage migration", 10)
        paths = [h["path"] for h in hits]
        only_first = str(self.cfg.vault / "3-Resources" / "only-first.md")
        self.assertNotIn(only_first, paths, "1/2 does not pass strict majority (1 > 1 is false)")

    def test_fallback_has_reason_field(self):
        hits = fallback_search(self.cfg, "storage migration backup", 10, reason="test reason")
        self.assertTrue(all("fallback" in h for h in hits))


if __name__ == "__main__":
    unittest.main()
