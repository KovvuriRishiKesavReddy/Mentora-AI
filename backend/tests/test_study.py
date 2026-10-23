import json
import unittest
from datetime import datetime, timedelta, timezone

from app import db
from app.errors import ApiError
from app.services import gap_tracker, llm, memory, quiz, revision, study
from app.services.llm import LLMError
from tests.base import BaseCase, NOTES, OWNER_A, OWNER_B

CHAPTERS = ["Introduction to Operating Systems", "Operating System Structures", "Process Management", "CPU Scheduling",
            "Process Synchronization", "Deadlocks", "Memory Management", "File Systems", "I/O Management",
            "Disk Management", "System Calls"]


class SummaryLLM:
    def __init__(self):
        self.calls = []
        self.fail_after = None          # fail every call after N successful ones
        self.always_fail = False

    def __call__(self, system, messages, max_tokens=None):
        self.calls.append((system, messages[0]["content"]))
        if self.always_fail or (self.fail_after is not None and len(self.calls) > self.fail_after):
            raise LLMError("upstream", "boom")
        text = messages[0]["content"]
        if "KEY TERMS" in text:
            return "OVERVIEW: These notes cover operating systems end to end.\nKEY TERMS: kernel, process, deadlock, paging"
        return "- point one about this part\n- point two about this part"


class TestOutlineAndPassage(BaseCase):
    def setUp(self):
        super().setUp()
        self.doc = self.upload(OWNER_A, NOTES)["doc_id"]

    def test_outline_lists_all_eleven_chapters_in_order_with_sections(self):
        o = study.outline(OWNER_A, self.doc)
        titles = [c["title"] for c in o["chapters"]]
        self.assertEqual([t.split(". ", 1)[1] for t in titles], CHAPTERS)
        sched = next(c for c in o["chapters"] if "CPU Scheduling" in c["title"])
        names = [s["title"] for s in sched["sections"]]
        self.assertIn("Round Robin (RR)", names)
        self.assertIn("Scheduling Criteria", names)

    def test_passage_returns_the_exact_section_text(self):
        p = study.get_passage(OWNER_A, self.doc, "4. CPU Scheduling > Round Robin (RR)")
        self.assertIn("time quantum", p["text"].lower())
        self.assertEqual(p["filename"], "os_notes.docx")

    def test_passage_and_outline_are_owner_scoped(self):
        for fn in (lambda: study.outline(OWNER_B, self.doc),
                   lambda: study.get_passage(OWNER_B, self.doc, "4. CPU Scheduling > Round Robin (RR)")):
            with self.assertRaises(ApiError) as cm:
                fn()
            self.assertEqual(cm.exception.status, 404)

    def test_unknown_heading_is_404(self):
        with self.assertRaises(ApiError) as cm:
            study.get_passage(OWNER_A, self.doc, "No such heading")
        self.assertEqual(cm.exception.code, "passage_not_found")


class TestSummary(BaseCase):
    def setUp(self):
        super().setUp()
        self.doc = self.upload(OWNER_A, NOTES)["doc_id"]
        self.sllm = SummaryLLM()
        llm.set_backend(self.sllm)

    def test_every_chapter_is_summarised_and_every_section_text_is_sent_to_the_model(self):
        r = study.summarize_document(OWNER_A, self.doc)
        self.assertEqual(len(r["chapters"]), 11)
        self.assertTrue(r["complete"])
        self.assertIn("operating systems", r["overview"].lower())
        self.assertIn("deadlock", r["key_terms"])
        sent = "\n".join(c[1] for c in self.sllm.calls)
        for rr in ["Round Robin (RR)", "RAID Levels", "Mechanism of System Calls", "Page Replacement Algorithms",
                   "Necessary Conditions for Deadlock", "Directory Structures"]:
            self.assertIn(rr, sent)                       # nothing was skipped
        self.assertIn("# Summary of os_notes.docx", r["markdown"])

    def test_second_request_uses_cache_and_makes_no_model_calls(self):
        study.summarize_document(OWNER_A, self.doc)
        n = len(self.sllm.calls)
        again = study.summarize_document(OWNER_A, self.doc)
        self.assertTrue(again["cached"])
        self.assertEqual(len(self.sllm.calls), n)
        self.assertEqual(len(study.get_cached_summary(OWNER_A, self.doc)["chapters"]), 11)
        study.summarize_document(OWNER_A, self.doc, refresh=True)
        self.assertGreater(len(self.sllm.calls), n)

    def test_model_down_from_the_start_fails_clearly_and_caches_nothing(self):
        self.sllm.always_fail = True
        with self.assertRaises(ApiError) as cm:
            study.summarize_document(OWNER_A, self.doc)
        self.assertEqual(cm.exception.status, 502)
        self.assertIsNone(study.get_cached_summary(OWNER_A, self.doc))

    def test_failure_midway_falls_back_to_notes_text_and_is_not_cached(self):
        self.sllm.fail_after = 3
        r = study.summarize_document(OWNER_A, self.doc)
        self.assertFalse(r["complete"])
        self.assertIn("extractive", {c["method"] for c in r["chapters"]})
        self.assertTrue(all(c["bullets"] for c in r["chapters"]))
        self.assertIsNone(study.get_cached_summary(OWNER_A, self.doc))

    def test_call_count_is_bounded(self):
        from app import config
        study.summarize_document(OWNER_A, self.doc)
        self.assertLessEqual(len(self.sllm.calls), config.SUMMARY_MAX_CALLS + 12)

    def test_deleting_the_document_removes_its_summary(self):
        from app.services import rag
        study.summarize_document(OWNER_A, self.doc)
        rag.delete_file(OWNER_A, self.doc)
        with db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM summaries WHERE doc_id=?", (self.doc,)).fetchone())


class TestFlashcards(BaseCase):
    def setUp(self):
        super().setUp()
        self.doc = self.upload(OWNER_A, NOTES)["doc_id"]

    def test_parse_handles_noise_duplicates_and_bad_items(self):
        raw = 'Sure!\n[{"front":"What is RR?","back":"Round robin uses a quantum."},{"front":"what is rr?","back":"dup"},' \
              '{"front":"x","back":"y"},"junk",{"front":"Define PCB","back":"Process control block."}]'
        cards = study._parse_cards(raw)
        self.assertEqual([c["front"] for c in cards], ["What is RR?", "Define PCB"])
        self.assertEqual(study._parse_cards("no json here"), [])

    def test_generate_from_topic_and_from_document(self):
        self.fake.chat_reply = json.dumps([{"front": "Q1 term", "back": "Answer one."}, {"front": "Q2 term", "back": "Answer two."}])
        r = study.generate_flashcards(OWNER_A, topic="round robin scheduling", n=2)
        self.assertEqual(len(r["cards"]), 2)
        prompt = self.fake.calls[-1]["messages"][0]["content"]
        self.assertIn("time quantum", prompt.lower())
        r2 = study.generate_flashcards(OWNER_A, doc_id=self.doc, n=5)
        self.assertEqual(r2["topic"], "os_notes.docx")
        sample = self.fake.calls[-1]["messages"][0]["content"]
        self.assertIn("System Calls", sample)             # evenly spread: reaches the last chapter
        self.assertIn("Introduction to Operating Systems", sample)

    def test_absent_topic_and_bad_requests(self):
        with self.assertRaises(ApiError) as cm:
            study.generate_flashcards(OWNER_A, topic="quantum chromodynamics gluon confinement")
        self.assertEqual(cm.exception.code, "no_content")
        with self.assertRaises(ApiError):
            study.generate_flashcards(OWNER_A)

    def test_unparseable_model_output_is_an_error_not_empty_success(self):
        self.fake.chat_reply = "I cannot do that"
        with self.assertRaises(ApiError) as cm:
            study.generate_flashcards(OWNER_A, doc_id=self.doc)
        self.assertEqual(cm.exception.code, "flashcards_failed")


class TestRevisionPlan(BaseCase):
    def _attempts(self, topic, scores, days_ago=0):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
        with db.connect() as c:
            for i, s in enumerate(scores):
                c.execute("INSERT INTO quiz_attempts(owner,session_id,quiz_id,question_idx,topic_key,topic_display,question,"
                          "student_answer,correct,score,graded_by,result_json,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (OWNER_A, "s", f"{topic}-{ts}-{i}", i, gap_tracker.topic_key(topic), topic, "q", "a",
                           int(s >= 0.6), s, "answer_key", "{}", ts))

    def test_empty_plan_says_so(self):
        p = revision.build_plan(OWNER_A)
        self.assertEqual(p["today"], [])
        self.assertIn("take a quiz", p["summary"].lower())

    def test_weak_topic_due_after_one_day_mastered_waits_a_week(self):
        self._attempts("Deadlocks", [0, 0, 0], days_ago=2)
        self._attempts("Paging", [1, 1], days_ago=2)
        p = revision.build_plan(OWNER_A)
        self.assertEqual([t["topic"] for t in p["today"]], ["Deadlocks"])
        self.assertEqual(p["today"][0]["status"], "weak")
        self.assertTrue(p["today"][0]["overdue"])
        up = {t["topic"]: t for t in p["upcoming"]}
        self.assertIn("Paging", up)
        self.assertGreater(up["Paging"]["due_in_days"], 4)
        self.assertEqual(p["mastered_count"], 1)

    def test_freshly_quizzed_weak_topic_is_not_due_yet(self):
        self._attempts("Deadlocks", [0, 0, 0], days_ago=0)
        p = revision.build_plan(OWNER_A)
        self.assertEqual(p["today"], [])
        self.assertEqual(p["upcoming"][0]["topic"], "Deadlocks")

    def test_lowest_mastery_comes_first(self):
        self._attempts("A topic", [0.4, 0.5], days_ago=5)
        self._attempts("B topic", [0, 0, 0], days_ago=5)
        p = revision.build_plan(OWNER_A)
        self.assertEqual(p["today"][0]["topic"], "B topic")

    def test_repeated_confusion_without_quiz_is_planned(self):
        sid = memory.create_session(OWNER_A)
        for _ in range(3):
            gap_tracker.record_confusion(OWNER_A, sid, "process scheduling")
        p = revision.build_plan(OWNER_A)
        self.assertEqual(p["today"][0]["topic"], "Process Scheduling")
        self.assertIn("did not understand", p["today"][0]["reason"])

    def test_plan_is_owner_scoped(self):
        self._attempts("Deadlocks", [0, 0, 0], days_ago=3)
        self.assertEqual(revision.build_plan(OWNER_B)["today"], [])


class TestAdaptiveQuiz(BaseCase):
    MCQ = ("Q: Which scheduling uses a quantum?\nA) FCFS\nB) Round Robin\nC) SJF\nD) Priority\nANSWER: B\n"
           "EXPLANATION: RR uses a time quantum.\nWRONG OPTIONS: A: FCFS has no quantum; C: SJF picks the shortest job; D: Priority uses ranks")

    def setUp(self):
        super().setUp()
        self.upload(OWNER_A, NOTES)
        self.sid = memory.create_session(OWNER_A)
        self.fake.chat_reply = self.MCQ

    def test_new_topic_is_medium_and_explains_why(self):
        q = quiz.generate_quiz(OWNER_A, self.sid, "CPU scheduling", "mcq", 1)
        self.assertEqual(q["difficulty"], "medium")
        self.assertIn("DIFFICULTY: medium", self.fake.calls[-1]["messages"][0]["content"])

    def test_weak_topic_gets_easy_and_mastered_gets_hard(self):
        for i in range(3):
            gap_tracker.record_attempt(OWNER_A, self.sid, f"q{i}", 0, "CPU scheduling", "q", "a", False, 0.0, "answer_key", {})
        self.assertEqual(quiz.generate_quiz(OWNER_A, self.sid, "CPU scheduling", "mcq", 1)["difficulty"], "easy")
        for i in range(3, 9):
            gap_tracker.record_attempt(OWNER_A, self.sid, f"q{i}", 0, "CPU scheduling", "q", "a", True, 1.0, "answer_key", {})
        self.assertEqual(quiz.generate_quiz(OWNER_A, self.sid, "CPU scheduling", "mcq", 1)["difficulty"], "hard")

    def test_explicit_difficulty_wins(self):
        q = quiz.generate_quiz(OWNER_A, self.sid, "CPU scheduling", "mcq", 1, "hard")
        self.assertEqual((q["difficulty"], q["difficulty_reason"]), ("hard", "chosen by you"))

    def test_wrong_answer_returns_note_for_each_wrong_option_but_never_the_correct_one(self):
        q = quiz.generate_quiz(OWNER_A, self.sid, "CPU scheduling", "mcq", 1)
        r = quiz.grade_answer(OWNER_A, q["quiz_id"], 0, "A")
        self.assertFalse(r["correct"])
        self.assertEqual(set(r["option_notes"]), {"A", "C", "D"})
        self.assertIn("no quantum", r["option_notes"]["A"])
        self.assertNotIn("quiz_id", json.dumps(q["questions"]))
        self.assertNotIn("WRONG", json.dumps(q))


class TestTeachingStrategy(unittest.TestCase):
    def test_every_mood_has_a_strategy_and_bored_gets_a_challenge_prompt(self):
        from app.services.mood import MoodResult
        for m in ("frustrated", "confused", "confident", "bored", "neutral"):
            s = llm.get_teaching_strategy(MoodResult(mood_label=m))
            self.assertTrue(s["label"] and s["detail"])
        bored = llm.build_system_prompt(llm.get_tone_profile(MoodResult(mood_label="bored")), "paging", "strong")
        calm = llm.build_system_prompt(llm.get_tone_profile(MoodResult(mood_label="confused")), "paging", "strong")
        self.assertIn("harder application question", bored)
        self.assertNotIn("harder application question", calm)


if __name__ == "__main__":
    unittest.main()
