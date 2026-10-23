import unittest
from types import SimpleNamespace as NS

from app import config
from app.services import llm


class FakeClient:
    def __init__(self, ok_models, listed):
        self.ok, self.listed, self.used = ok_models, listed, []
        self.chat = NS(completions=NS(create=self._create))
        self.models = NS(list=lambda: NS(data=[NS(id=i) for i in self.listed]))

    def _create(self, model, **kw):
        self.used.append(model)
        if model not in self.ok:
            raise RuntimeError("Error code: 404 - model_not_found: The model `%s` does not exist" % model)
        return NS(choices=[NS(message=NS(content="hello"))])


class TestDefaults(unittest.TestCase):
    def test_defaults_are_openai_oss_models(self):
        self.assertEqual(config.GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertEqual(config.GROQ_FALLBACK_MODELS, ["openai/gpt-oss-20b"])

    def test_reasoning_options_only_for_gpt_oss(self):
        seen = {}
        def create(model, **kw):
            seen[model] = kw
            return NS(choices=[NS(message=NS(content="ok"))])
        c = NS(chat=NS(completions=NS(create=create)))
        llm._try_model(c, "openai/gpt-oss-120b", [], 300)
        llm._try_model(c, "some-other-model", [], 300)
        self.assertEqual(seen["openai/gpt-oss-120b"]["extra_body"], {"reasoning_effort": config.REASONING_EFFORT})
        self.assertEqual(seen["openai/gpt-oss-120b"]["max_tokens"], 300 + config.REASONING_HEADROOM)
        self.assertNotIn("extra_body", seen["some-other-model"])
        self.assertEqual(seen["some-other-model"]["max_tokens"], 300)


class TestModelFallback(unittest.TestCase):
    def setUp(self):
        llm.set_backend(None)
        llm._discovered = None
        self._orig = llm.get_client

    def tearDown(self):
        llm.get_client = self._orig
        llm._discovered = None

    def run_with(self, client):
        llm.get_client = lambda: client
        return llm.call_llm("sys", [{"role": "user", "content": "hi"}])

    def test_fallback_model_used(self):
        c = FakeClient({"openai/gpt-oss-20b"}, [])
        self.assertEqual(self.run_with(c), "hello")
        self.assertEqual(c.used, [config.GROQ_MODEL, "openai/gpt-oss-20b"])

    def test_auto_discovery_when_configured_models_retired(self):
        c = FakeClient({"llama-9-new"}, ["whisper-large-v3", "llama-guard-4", "llama-9-new", "openai/gpt-oss-safeguard-20b", "other-model"])
        self.assertEqual(self.run_with(c), "hello")
        self.assertIn("llama-9-new", c.used)
        self.assertNotIn("whisper-large-v3", c.used)

    def test_clear_error_when_nothing_works(self):
        c = FakeClient(set(), ["llama-x"])
        with self.assertRaises(llm.LLMError) as cm:
            self.run_with(c)
        self.assertEqual(cm.exception.kind, "model")
        self.assertIn("check_groq.py", cm.exception.message)

    def test_auth_error_not_retried_and_key_redacted(self):
        class C(FakeClient):
            def _create(self, model, **kw):
                self.used.append(model)
                raise RuntimeError("401 invalid api key gsk_ABC123")
        c = C(set(), [])
        with self.assertRaises(llm.LLMError) as cm:
            self.run_with(c)
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(c.used), 1)
        self.assertNotIn("ABC123", llm._safe_detail(RuntimeError("x gsk_ABC123")))


if __name__ == "__main__":
    unittest.main()
