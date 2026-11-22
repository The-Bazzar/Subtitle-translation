import unittest
from types import SimpleNamespace

from subtitle_translation.translation_context import TranslationContext, estimate, window_for


class TranslationContextTests(unittest.TestCase):
    def test_exact_deployment_window_and_override(self):
        cfg = {"url": "https://api.deepseek.com", "model_info": []}
        llm = SimpleNamespace(cfg=lambda: cfg, model_name=lambda: "deepseek-flash")
        self.assertEqual(window_for(llm), 1_000_000)
        cfg["url"] = "https://gateway.invalid/v1"
        self.assertEqual(window_for(llm), 32768)
        cfg["model_info"] = [{"id": "deepseek-flash", "context_length": 16000}]
        self.assertEqual(window_for(llm), 16000)
        for invalid in ("-1", 0, "many", True):
            cfg["model_info"][0]["context_length"] = invalid
            with self.assertRaises(ValueError):
                window_for(llm)

    def test_budget_without_usage_reserves_output_and_preserves_system(self):
        policy = TranslationContext(1000, "en", "zh", {"max_completion_tokens": 500})
        system = {"role": "system", "content": "glossary"}
        history = [system, {"role": "user", "content": "x" * 500}, {"role": "assistant", "content": "a" * 300}]
        session = SimpleNamespace(messages=history, last_prompt_tokens=None)
        policy.remember(1, "hello", "你好")
        policy.prepare(session, "short")
        self.assertEqual(len(session.messages[1]["content"]), 500)
        policy.prepare(session, "文" * 130)
        self.assertIs(session.messages[0], system)
        self.assertIn("hello", session.messages[1]["content"])
        policy.prepare(session, "文" * 400)
        self.assertEqual(session.messages, [system])
        self.assertGreater(estimate("文" * 130), estimate("a" * 130))
