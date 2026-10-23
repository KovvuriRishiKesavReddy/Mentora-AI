import json
import threading
import unittest

from app import chat_service, db
from app.errors import ApiError
from app.services import gap_tracker, memory, quiz
from tests.base import BaseCase, NOTES, OWNER_A, OWNER_B

MCQ = "\n---\n".join(
    f"Q: Question {i} about CPU scheduling?\nA) one\nB) two\nC) three\nD) four\nANSWER: B\nEXPLANATION: because {i}"
    for i in range(1, 4))
SHORT = "Q: Explain round robin scheduling\nMODEL ANSWER: Each process gets a fixed time quantum in a cyclic order.\n---"


class QuizCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.upload(OWNER_A, NOTES)
        self.sid = memory.create_session(OWNER_A)

    def gen(self, topic="CPU scheduling", qtype="mcq", reply=MCQ, n=3):
        orig = self.fake.chat_reply
        self.fake.chat_reply = reply
        try:
            return quiz.generate_quiz(OWNER_A, self.sid, topic, qtype, n)
        finally:
            self.fake.chat_reply = orig

    def grade_all(self, q, letters):
        return [quiz.grade_answer(OWNER_A, q["quiz_id"], i, l) for i, l in enumerate(letters)]

    def status(self, topic="CPU scheduling"):
        k = gap_tracker.topic_key(topic)
        return next((t for t in gap_tracker.get_topic_stats(OWNER_A) if t["key"] == k), None)


class TestMastery(unittest.TestCase):
    def test_policy_table(self):
        c = gap_tracker.compute_mastery
        self.assertEqual(c([])["status"], "not_assessed")
        self.assertEqual(c([1.0])["status"], "learning")
        self.assertEqual(c([0.0])["status"], "learning")          # one answer never decides
        self.assertEqual(c([1.0, 1.0])["status"], "mastered")
        self.assertEqual(c([0.0, 0.0])["status"], "needs_revision")
        self.assertEqual(c([0.0, 0.0, 0.0])["status"], "weak")
        self.assertEqual(c([1.0, 0.0, 1.0])["status"], "needs_revision")
        self.assertGreater(c([0.0, 1.0])["mastery"], c([0.0, 0.0])["mastery"])   # a correct answer raises mastery
        self.assertGreater(c([0, 0, 1, 1, 1])["mastery"], c([0, 0, 1, 1])["mastery"])


class TestQuiz(QuizCase):
    def test_generation_hides_answers_and_stores_topic(self):
        q = self.gen()
        self.assertEqual(len(q["questions"]), 3)
        blob = json.dumps(q)
        self.assertNotIn("ANSWER", blob)
        self.assertNotIn("because", blob)
        self.assertNotIn("correct_answer", blob)

    def test_all_correct_is_not_weak(self):
        q = self.gen()
        res = self.grade_all(q, ["B", "B", "B"])
        self.assertTrue(all(r["correct"] and r["score"] == 1.0 for r in res))
        st = self.status()
        self.assertEqual(st["status"], "mastered")
        self.assertEqual(gap_tracker.get_weak_topics(OWNER_A), [])

    def test_wrong_answers_flag_revision_then_weak(self):
        q = self.gen()
        self.grade_all(q, ["A", "A"])  # 2 wrong, third left ungraded
        self.assertEqual(self.status()["status"], "needs_revision")
        self.assertEqual([t["topic"] for t in gap_tracker.get_weak_topics(OWNER_A)], ["CPU scheduling"])
        quiz.grade_answer(OWNER_A, q["quiz_id"], 2, "C")
        self.assertEqual(self.status()["status"], "weak")

    def test_duplicate_and_concurrent_submissions_count_once(self):
        q = self.gen()
        a = quiz.grade_answer(OWNER_A, q["quiz_id"], 0, "B")
        b = quiz.grade_answer(OWNER_A, q["quiz_id"], 0, "B")
        c = quiz.grade_answer(OWNER_A, q["quiz_id"], 0, "A")          # even a changed answer is not re-scored
        self.assertFalse(a["already_graded"])
        self.assertTrue(b["already_graded"] and c["already_graded"])
        self.assertEqual(c["score"], 1.0)
        q2 = self.gen()
        out = []
        ts = [threading.Thread(target=lambda: out.append(quiz.grade_answer(OWNER_A, q2["quiz_id"], 0, "A"))) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sum(not r["already_graded"] for r in out), 1)
        with db.connect() as cn:
            self.assertEqual(cn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 2)

    def test_chat_questions_and_mood_do_not_mark_weak(self):
        self.fake.rewrite = None
        for m in ["What is process scheduling?", "Explain process scheduling from my notes", "this is the worst explanation ever",
                  "How does round robin work?"]:
            chat_service.handle_chat(OWNER_A, self.sid, m)
        self.assertEqual(gap_tracker.get_weak_topics(OWNER_A), [])
        self.assertEqual(gap_tracker.get_topic_stats(OWNER_A), [])

    def test_confusion_is_a_separate_signal(self):
        q = self.gen()
        self.grade_all(q, ["B", "B", "B"])
        chat_service.handle_chat(OWNER_A, self.sid, "Explain CPU scheduling")
        chat_service.handle_chat(OWNER_A, self.sid, "I don't understand cpu scheduling")
        st = self.status("cpu scheduling")
        self.assertEqual(st["confusion_signals"], 1, gap_tracker.get_topic_stats(OWNER_A))
        self.assertEqual(st["status"], "mastered")                       # quiz correctness untouched
        self.assertEqual(st["attempts"], 3)
        self.assertFalse(st["needs_attention"])
        # unassessed topic: 3 explicit statements flag it, still with no quiz data invented
        for _ in range(3):
            chat_service.handle_chat(OWNER_A, self.sid, "I'm still confused about deadlock handling strategies")
        dl = next(t for t in gap_tracker.get_topic_stats(OWNER_A) if "deadlock" in t["key"])
        self.assertTrue(dl["needs_attention"])
        self.assertEqual(dl["attempts"], 0)
        self.assertEqual(dl["status"], "not_assessed")
        self.assertEqual([t["reason"] for t in gap_tracker.get_weak_topics(OWNER_A) if "deadlock" in t["key"]], ["repeated confusion"])

    def test_multi_topic_quiz_history_and_persistence(self):
        q1 = self.gen("CPU scheduling")
        q2 = self.gen("deadlock handling strategies")
        self.grade_all(q1, ["B", "B", "B"])
        self.grade_all(q2, ["A", "A", "A"])
        db.configure(self.tmp / "data" / "t.db")                         # restart
        stats = {t["key"]: t["status"] for t in gap_tracker.get_topic_stats(OWNER_A)}
        self.assertEqual(stats["cpu scheduling"], "mastered")
        self.assertEqual(stats["deadlock handling strategies"], "weak")

    def test_short_answer_grading_and_grader_outage(self):
        q = self.gen("round robin scheduling", "short", SHORT, 1)
        self.fake.chat_reply = json.dumps({"concepts_present": ["time quantum"], "concepts_missing": [], "misconceptions": [],
                                           "clarity_score": 0.9, "completeness_score": 0.9, "feedback": "Good."})
        r = quiz.grade_answer(OWNER_A, q["quiz_id"], 0, "Each process gets a time quantum in turn.")
        self.assertTrue(r["correct"])
        q2 = self.gen("round robin scheduling", "short", SHORT, 1)
        self.fake.chat_reply = "not json at all"
        with self.assertRaises(ApiError) as cm:
            quiz.grade_answer(OWNER_A, q2["quiz_id"], 0, "something")
        self.assertEqual(cm.exception.status, 503)
        with db.connect() as cn:                                          # outage recorded nothing
            self.assertEqual(cn.execute("SELECT COUNT(*) FROM quiz_attempts").fetchone()[0], 1)

    def test_ownership_and_validation(self):
        q = self.gen()
        with self.assertRaises(ApiError) as cm:
            quiz.grade_answer(OWNER_B, q["quiz_id"], 0, "B")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(ApiError):
            quiz.grade_answer(OWNER_A, q["quiz_id"], 9, "B")
        with self.assertRaises(ApiError):
            quiz.grade_answer(OWNER_A, q["quiz_id"], 0, "Z")
        with self.assertRaises(ApiError):
            quiz.generate_quiz(OWNER_B, self.sid, "CPU scheduling")        # foreign session
        with self.assertRaises(ApiError) as cm:
            quiz.generate_quiz(OWNER_A, self.sid, "quantum chromodynamics gluon")
        self.assertEqual(cm.exception.status, 404)

    def test_malformed_mcq_dropped(self):
        bad = "Q: no options\nANSWER: B\n---\nQ: ok?\nA) x\nB) y\nANSWER: C\n---\n" + MCQ.split("\n---\n")[0]
        q = self.gen(reply=bad)
        self.assertEqual(len(q["questions"]), 1)


if __name__ == "__main__":
    unittest.main()
