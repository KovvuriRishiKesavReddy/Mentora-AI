import unittest

from app import chat_service, db
from app.errors import ApiError
from app.services import llm, memory, query
from app.services.mood import classify_mood
from tests.base import BaseCase, NOTES, OWNER_A, OWNER_B


def rewrite_rules(msg, topic):
    m = msg.lower()
    if "remaining" in m:
        return f"Explain the remaining types of {topic} not yet covered"
    if "second" in m:
        return f"Explain the second type of {topic}"
    if "example" in m:
        return f"Give an example of {topic}"
    if "types" in m:
        return f"What are the types of {topic}?"
    return msg


class TestQueryUnderstanding(unittest.TestCase):
    def test_followup_detection(self):
        follow = ["What are its types?", "Explain the second one.", "Continue.", "Give me an example.",
                  "Continue with the remaining types.", "What are its advantages?", "and the third?", "Why?",
                  "What are the advantages?", "Give an example of that."]
        fresh = ["Explain process scheduling from my notes.", "What is deadlock?", "Explain the types of scheduling algorithms",
                 "What are the advantages of a microkernel?", "Define virtual memory and demand paging in detail"]
        for m in follow:
            self.assertTrue(query.is_followup(m, True), m)
        for m in fresh:
            self.assertFalse(query.is_followup(m, True), m)
        self.assertFalse(query.is_followup("What are its types?", False))        # no history => cannot be a follow-up

    def test_broad_detection(self):
        for m in ["Explain all six types of operating systems", "list every scheduling algorithm", "Give an overview of memory management",
                  "Continue with the remaining types", "compare FCFS and SJF", "What are the 6 types of OS?"]:
            self.assertTrue(query.is_broad(m), m)
        for m in ["What is a page fault?", "Define deadlock"]:
            self.assertFalse(query.is_broad(m), m)

    def test_topic_extraction_is_not_first_six_words(self):
        self.assertEqual(query.extract_topic("Explain process scheduling from my notes."), "process scheduling")
        self.assertEqual(query.extract_topic("What is a deadlock?"), "deadlock")
        self.assertEqual(query.extract_topic("Can you please explain virtual memory and demand paging to me in great detail with examples"), "")
        self.assertEqual(query.extract_topic("tell me about the Banker's algorithm"), "Banker's algorithm")

    def test_confusion_vs_plain_questions(self):
        for m in ["I don't understand paging", "I'm still confused about this", "explain again please", "this doesn't make sense"]:
            self.assertTrue(query.is_confusion_statement(m), m)
        for m in ["What is process scheduling?", "How does paging work?", "Explain deadlock", "help me with FCFS", "this is the worst explanation"]:
            self.assertFalse(query.is_confusion_statement(m), m)

    def test_mood_keywords_do_not_misfire(self):
        self.assertNotEqual(classify_mood("What is the worst-case time of quicksort?").mood_label, "frustrated")
        self.assertNotEqual(classify_mood("Continue with the remaining types, then move on to the next topic").mood_label, "bored")
        self.assertEqual(classify_mood("this is the worst explanation ever").mood_label, "frustrated")
        self.assertEqual(classify_mood("I don't understand this at all").mood_label, "confused")


class TestMoodNearTie(unittest.TestCase):
    def test_near_tie_semantic_result_falls_back_to_neutral(self):
        import numpy as np
        from app.services import mood as M

        class Fake:
            def encode(self, xs, show_progress_bar=False):
                return np.array([[1.0, 0.0, 0.0] for _ in xs])
        refs = {m: np.array([[1.0, 0.0, 0.0]]) for m in M.MOOD_REFS}
        refs["frustrated"] = np.array([[1.0, 0.01, 0.0]])
        old = (M._embedder, M._ref_embs)
        M._embedder, M._ref_embs = Fake(), refs
        try:
            r = M.classify_mood("worst case time complexity")
            self.assertEqual(r.mood_label, "neutral")
        finally:
            M._embedder, M._ref_embs = old


class TestChatFlow(BaseCase):
    def setUp(self):
        super().setUp()
        self.upload(OWNER_A, NOTES)
        self.sid = memory.create_session(OWNER_A)

    def say(self, text, owner=OWNER_A, sid=None):
        return chat_service.handle_chat(owner, sid or self.sid, text)

    def last_user_payload(self):
        return self.fake.chat_calls()[-1]["messages"][-1]["content"]

    def test_five_step_follow_up_sequence_keeps_context(self):
        self.fake.rewrite = rewrite_rules
        r1 = self.say("Explain process scheduling from my notes.")
        self.assertEqual(r1["active_topic"], "process scheduling")
        self.assertIsNone(r1["interpreted_as"])
        self.assertTrue(r1["retrieval"]["is_grounded"])
        self.assertIn("Scheduling", " ".join(s["chapter"] for s in r1["retrieval"]["sources"]))

        r2 = self.say("What are its types?")
        self.assertEqual(r2["interpreted_as"], "What are the types of process scheduling?")
        self.assertEqual(r2["active_topic"], "process scheduling")
        self.assertIn("Scheduling", " ".join(s["chapter"] for s in r2["retrieval"]["sources"]))
        self.assertNotEqual(r2["retrieval"]["sources"][0]["chapter"].split(" > ")[-1], "Types of Operating Systems")

        r3 = self.say("Explain the second one.")
        self.assertEqual(r3["interpreted_as"], "Explain the second type of process scheduling")
        r4 = self.say("Give me an example.")
        self.assertEqual(r4["active_topic"], "process scheduling")
        r5 = self.say("Continue with the remaining types.")
        self.assertIn("remaining types of process scheduling", r5["interpreted_as"])
        self.assertTrue(r5["retrieval"]["is_grounded"])

        # the tutor sees the real conversation, in chronological order, each message once
        msgs = self.fake.chat_calls()[-1]["messages"]
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["user", "assistant"] * 4 + ["user"])
        users = [m["content"] for m in msgs if m["role"] == "user"]
        self.assertTrue(users[0].startswith("Explain process scheduling"))
        self.assertTrue(users[1].startswith("What are its types?"))
        current = users[-1]
        self.assertEqual(current.count("Continue with the remaining types."), 1)       # current message only once
        self.assertEqual(sum("Continue with the remaining types." in m["content"] for m in msgs), 1)

    def test_followup_works_without_llm_rewriter_via_heuristic_anchor(self):
        self.fake.rewrite = None                              # rewriter returns garbage -> heuristic fallback
        self.say("Explain process scheduling from my notes.")
        r = self.say("What are its types?")
        self.assertIn("process scheduling", r["interpreted_as"])
        self.assertIn("Scheduling", " ".join(s["chapter"] for s in r["retrieval"]["sources"]))

    def test_explicit_topic_change_is_respected(self):
        self.fake.rewrite = rewrite_rules
        self.say("Explain process scheduling from my notes.")
        self.say("What are its types?")
        r = self.say("Now explain deadlock handling strategies")
        self.assertIsNone(r["interpreted_as"])                # not treated as a follow-up
        self.assertEqual(r["active_topic"], "deadlock handling strategies")
        self.assertIn("Deadlock", r["retrieval"]["sources"][0]["chapter"])
        self.assertEqual(memory.get_session(OWNER_A, self.sid)["active_topic"], "deadlock handling strategies")

    def test_new_session_does_not_inherit_private_history(self):
        self.say("Explain process scheduling from my notes.")
        sid2 = memory.create_session(OWNER_A)
        self.say("What are its types?", sid=sid2)
        sent = self.fake.chat_calls()[-1]["messages"]
        self.assertEqual(len(sent), 1)                         # no history from the first session
        self.assertEqual(self.fake.chat_calls()[-1]["messages"][0]["role"], "user")

    def test_history_is_ordered_and_bounded(self):
        for i in range(14):
            self.say(f"Explain topic number {i} about paging")
        msgs = self.fake.chat_calls()[-1]["messages"]
        self.assertLessEqual(len(msgs), 13)                    # window (12) + current
        contents = [m["content"] for m in msgs if m["role"] == "user"][:-1]
        nums = [int(c.split("number ")[1].split()[0]) for c in contents]
        self.assertEqual(nums, sorted(nums))
        self.assertEqual(nums[-1], 12)

    def test_failed_llm_call_persists_nothing(self):
        self.say("Explain deadlock handling strategies")
        before = len(memory.get_messages(OWNER_A, self.sid))
        self.fake.fail = True
        with self.assertRaises(ApiError) as cm:
            self.say("What are its types?")
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(len(memory.get_messages(OWNER_A, self.sid)), before)
        self.fake.fail = False
        self.say("What are its types?")
        self.assertEqual(len(memory.get_messages(OWNER_A, self.sid)), before + 2)

    def test_foreign_or_unknown_session_is_not_found(self):
        with self.assertRaises(ApiError) as cm:
            chat_service.handle_chat(OWNER_B, self.sid, "hello there")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(ApiError):
            memory.get_messages(OWNER_B, self.sid)
        with self.assertRaises(ApiError):
            chat_service.handle_chat(OWNER_A, "0" * 36, "hello")
        self.assertEqual(memory.list_sessions(OWNER_B), [])

    def test_history_survives_restart(self):
        self.say("Explain deadlock handling strategies")
        db.configure(self.tmp / "data" / "t.db")               # fresh connections, same file
        msgs = memory.get_messages(OWNER_A, self.sid)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(memory.list_sessions(OWNER_A)[0]["id"], self.sid)

    def test_ungrounded_answer_prompt_has_no_fixed_opener_and_is_flagged(self):
        r = self.say("Explain quantum chromodynamics and gluon confinement")
        self.assertFalse(r["retrieval"]["is_grounded"])
        self.assertEqual(r["retrieval"]["grounding"], "none")
        system = self.fake.chat_calls()[-1]["system"]
        self.assertNotIn("I couldn't find this in your notes — here is what I know generally", system)
        self.assertIn("not covered in their notes", system)
        self.assertIn("do not repeat", system.lower())

    def test_confusion_after_an_answer_switches_to_short_clarification_mode(self):
        self.say("Explain process scheduling from my notes.")
        self.assertNotIn("CLARIFICATION MODE", self.fake.chat_calls()[-1]["system"])
        self.say("I don't understand it.")
        system = self.fake.chat_calls()[-1]["system"]
        self.assertIn("CLARIFICATION MODE", system)
        self.assertIn("No tables", system)
        self.say("What is deadlock?")
        self.assertNotIn("CLARIFICATION MODE", self.fake.chat_calls()[-1]["system"])

    def test_bare_confusion_is_about_the_topic_not_the_last_example(self):
        self.fake.rewrite = lambda *a, **k: '{"standalone": "Explain the Round-Robin example again", "topic": "Round Robin"}'
        self.say("Explain process scheduling from my notes.")
        self.say("Give an example")
        r = self.say("I don't understand it.")
        self.assertEqual(r["active_topic"], "process scheduling")
        self.assertEqual(r["interpreted_as"], "Explain process scheduling in simple words, from the basics.")
        self.assertIn("ACTIVE TOPIC OF THIS CONVERSATION: process scheduling", self.fake.chat_calls()[-1]["system"])
        self.assertIn("CLARIFICATION MODE", self.fake.chat_calls()[-1]["system"])

    def test_broad_request_sends_all_six_types_to_the_model(self):
        self.say("Explain all six types of operating systems from my notes")
        sent = self.last_user_payload()
        for t in ["Batch Operating System", "Time-Sharing Operating System", "Distributed Operating System",
                  "Real-Time Operating System", "Network Operating System", "Mobile Operating System"]:
            self.assertIn(t, sent)
        self.assertIn("EVERY item", self.fake.chat_calls()[-1]["system"])


class TestPromptQuality(unittest.TestCase):
    def prompt(self, label, grounding="strong"):
        mood = classify_mood({"frustrated": "this is the worst explanation ever", "confused": "I don't understand this",
                              "confident": "I understand now, got it", "bored": "this is too easy"}.get(label, "what is paging"))
        return llm.build_system_prompt(llm.get_tone_profile(mood), "paging", grounding)

    def test_removed_contradictory_or_fact_inventing_instructions(self):
        for label in ("frustrated", "confused", "confident", "bored", "neutral"):
            p = self.prompt(label)
            self.assertNotIn("which just means", p)
            self.assertNotIn("surprising fact", p)
            self.assertNotIn("Answer ONLY from the retrieved note chunks", p)
            self.assertNotIn("I couldn't find this in your notes", p)

    def test_mood_changes_presentation_but_not_grounding_rules(self):
        core = lambda p: p.split("PRESENTATION")[0]
        self.assertEqual(core(self.prompt("frustrated")), core(self.prompt("confident")))
        self.assertNotEqual(self.prompt("frustrated"), self.prompt("confident"))
        self.assertIn("never change facts", self.prompt("frustrated"))

    def test_grounding_status_text_matches_evidence(self):
        self.assertIn("Answer from them", self.prompt("neutral", "strong"))
        self.assertIn("loosely related", self.prompt("neutral", "weak"))
        self.assertIn("No relevant passage", self.prompt("neutral", "none"))


if __name__ == "__main__":
    unittest.main()
