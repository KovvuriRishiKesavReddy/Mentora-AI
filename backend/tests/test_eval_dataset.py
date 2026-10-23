"""The evaluation question set must match the notes it was written for (so the ground truth is real)."""
import importlib.util
import unittest
from pathlib import Path

from tests.base import NOTES

_spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).parent.parent / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


class TestEvalDataset(unittest.TestCase):
    def test_every_expected_section_and_keyword_exists_in_the_notes(self):
        self.assertEqual(run_eval.validate(NOTES, run_eval.load_questions()), [])

    def test_dataset_shape(self):
        qs = run_eval.load_questions()
        self.assertGreaterEqual(len(qs["direct"]), 40)
        self.assertGreaterEqual(len(qs["followups"]), 4)
        self.assertGreaterEqual(len(qs["out_of_scope"]), 10)
        ids = [q["id"] for q in qs["direct"]]
        self.assertEqual(len(ids), len(set(ids)))
