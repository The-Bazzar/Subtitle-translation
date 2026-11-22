import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import translate_srt as t
from subtitle_translation import model_context as mc
from subtitle_translation.artifacts import atomic_write_json, read_json_object
from subtitle_translation.translation_context import window_for


class ModelContextTests(unittest.TestCase):
    def setUp(self):
        mc._cache.clear()
        mc._public_cache = (0, [])
        network = patch.object(mc, "urlopen", side_effect=OSError("offline fixture"))
        self.public_request = network.start()
        self.addCleanup(network.stop)

    def model(self, payload, url="https://gateway.invalid/v1", model="fixture"):
        client = MagicMock()
        client.__enter__.return_value = client
        client.with_options.return_value = client
        client.get.return_value = payload
        llm = SimpleNamespace(cfg=lambda: {"url": url}, model_name=lambda: model,
                              resolve_key=lambda: "fixture-key", _client=lambda: client)
        return llm, client

    def local_config(self, root, config, model="fixture"):
        path = Path(root) / "providers.json"
        atomic_write_json(path, {"fixture": config, "other": {"default_model": "unrelated"}})
        return path, t.LLMConfig("fixture", model=model, api_key="secret-fixture-key")

    def test_discovery_fills_missing_window_and_reuses_persisted_value(self):
        cfg = {"url": "https://gateway.invalid/v1", "default_model": "fixture",
               "model_info": [{"id": "mirror/model", "note": "keep this"}]}
        _, client = self.model({"data": [{"id": "mirror/model", "context_length": 12345}]})
        with tempfile.TemporaryDirectory() as root, patch.object(t, "_config_root", return_value=root):
            path, llm = self.local_config(root, cfg, "mirror/model")
            identity = t.source_first_model_identity(llm)
            with patch.object(t.LLMConfig, "_client", return_value=client):
                self.assertEqual(llm.context_window(), 12345)
            mc._cache.clear()
            saved = read_json_object(path)
            with patch.object(t.LLMConfig, "_client") as factory:
                self.assertEqual(t.LLMConfig("fixture", model="mirror/model").context_window(), 12345)
                factory.assert_not_called()
            self.assertEqual(t.source_first_model_identity(llm), identity)
            self.assertNotIn("secret-fixture-key", path.read_text(encoding="utf-8"))
        self.assertEqual(saved["other"], {"default_model": "unrelated"})
        self.assertEqual(saved["fixture"]["default_model"], "fixture")
        entry, = saved["fixture"]["model_info"]
        self.assertEqual(entry["id"], "mirror/model")
        self.assertEqual(entry["note"], "keep this")
        self.assertEqual(entry["context_length"], 12345)
        self.assertEqual(entry["context_length_source"], "provider")
        client.get.assert_called_once()

    def test_catalogue_result_persists_with_provenance_but_not_across_endpoints(self):
        cfg = {"url": "https://gateway.invalid/v1"}
        _, client = self.model({})
        self.catalogue([{"id": "catalogue/model", "context_length": 64000}])
        with tempfile.TemporaryDirectory() as root, patch.object(t, "_config_root", return_value=root):
            path, llm = self.local_config(root, cfg, "mirror/model")
            with patch.object(t.LLMConfig, "_client", return_value=client):
                self.assertEqual(llm.context_window(), 64000)
            saved = read_json_object(path)
            entry, = saved["fixture"]["model_info"]
            self.assertEqual(entry["id"], "mirror/model")
            self.assertEqual(entry["context_length_source"], "openrouter")
            changed = {**saved["fixture"], "url": "https://other.invalid/v1"}
            self.assertIsNone(mc.configured_limit(changed, "mirror/model"))
            mc._cache.clear()
            with patch.object(t.LLMConfig, "_client") as factory:
                self.assertEqual(llm.context_window(), 64000)
                factory.assert_not_called()
        self.public_request.assert_called_once()

    def test_manual_window_and_unrelated_model_saved_during_lookup_are_preserved(self):
        cfg = {"url": "https://gateway.invalid/v1"}
        _, client = self.model({})
        with tempfile.TemporaryDirectory() as root, patch.object(t, "_config_root", return_value=root):
            path, llm = self.local_config(root, cfg)
            def metadata(*args, **kwargs):
                new = read_json_object(path)
                new["fixture"]["model_info"] = [{"id": "fixture", "context_length": 4096},
                                                {"id": "second", "context_length": 8192}]
                atomic_write_json(path, new)
                return {"data": [{"id": "fixture", "context_length": 64000}]}
            client.get.side_effect = metadata
            with patch.object(t.LLMConfig, "_client", return_value=client), \
                 patch.object(mc, "atomic_write_json", wraps=atomic_write_json) as write:
                self.assertEqual(llm.context_window(), 4096)
                write.assert_not_called()
            self.assertEqual(len(read_json_object(path)["fixture"]["model_info"]), 2)

    def test_late_merge_does_not_drop_another_models_update(self):
        cfg = {"url": "https://gateway.invalid/v1"}
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.local_config(root, cfg)
            mc.persist_limit(path, "fixture", cfg, None, "model-a", 4096, "provider")
            mc.persist_limit(path, "fixture", cfg, None, "model-b", 8192, "provider")
            entries = read_json_object(path)["fixture"]["model_info"]
            self.assertEqual([(entry["id"], entry["context_length"]) for entry in entries],
                             [("model-a", 4096), ("model-b", 8192)])

    def test_invalid_config_is_never_overwritten(self):
        cfg = {"url": "https://gateway.invalid/v1"}
        with tempfile.TemporaryDirectory() as root:
            path, _ = self.local_config(root, cfg)
            path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(mc.persist_limit(path, "fixture", cfg, cfg, "fixture", 4096, "provider"), 4096)
            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid")

    def test_fallback_is_not_written_and_write_failure_does_not_discard_metadata(self):
        cfg = {"url": "https://gateway.invalid/v1"}
        _, client = self.model({})
        with tempfile.TemporaryDirectory() as root, patch.object(t, "_config_root", return_value=root):
            path, llm = self.local_config(root, cfg)
            original = path.read_bytes()
            with patch.object(t.LLMConfig, "_client", return_value=client):
                self.assertEqual(llm.context_window(), 32768)
                mc._cache.clear()
                client.get.return_value = {"data": [{"id": "fixture", "context_length": 64000}]}
                with patch.object(mc, "atomic_write_json", side_effect=PermissionError):
                    self.assertEqual(llm.context_window(), 64000)
            self.assertEqual(path.read_bytes(), original)

    def test_metadata_field_variants_and_wrong_model(self):
        for key in ("context_window", "context_length", "max_input_tokens", "inputTokenLimit"):
            self.assertEqual(mc.metadata_limit({"data": [{"id": "x", key: 32000}]}, "x"), 32000)
        self.assertIsNone(mc.metadata_limit({"data": [{"id": "other", "context_window": 32000}]}, "x"))
        for value in (True, -1, 0, "32000", None):
            self.assertIsNone(mc.metadata_limit({"context_window": value}, "x"))

    def test_model_info_is_optional_exact_and_provider_scoped(self):
        for config in ({}, {"model_info": []}, {"model_info": [{"id": "fixture"}]},
                       {"model_info": [{"id": "fixture", "context_length": None}]}):
            self.assertIsNone(mc.configured_limit(config, "fixture"))
        first = {"model_info": [{"id": "fixture", "context_length": 4096}]}
        second = {"model_info": [{"id": "fixture", "context_length": 8192}]}
        self.assertEqual(mc.configured_limit(first, "fixture"), 4096)
        self.assertEqual(mc.configured_limit(second, "fixture"), 8192)
        self.assertIsNone(mc.configured_limit(first, "fixture-other"))
        self.assertIsNone(mc.configured_limit(first, "vendor/fixture"))

    def test_invalid_model_info_is_not_silently_ignored(self):
        for entries in ({}, [None], [{"id": ""}], [{"id": 1}], [{"id": "x", "context_length": True}],
                        [{"id": "x", "context_length": "4096"}], [{"id": "x", "context_length": 0}],
                        [{"id": "x"}, {"id": "x"}]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                mc.configured_limit({"model_info": entries}, "x")

    def test_actual_request_model_uses_its_entry_without_network(self):
        llm, client = self.model({})
        llm.cfg = lambda: {"request_kwargs": {"model": "override"},
                          "model_info": [{"id": "fixture", "context_length": 8192},
                                         {"id": "override", "context_length": 4096}]}
        self.assertEqual(window_for(llm), 4096)
        client.get.assert_not_called()
        self.public_request.assert_not_called()

    def test_removed_env_settings_cannot_override_provider_model_info(self):
        llm = t.LLMConfig("fake", model="fixture")
        with patch.object(llm, "cfg", return_value={"model_info": [{"id": "fixture", "context_length": 4096}]}), \
             patch.object(t, "load_env", side_effect=AssertionError("window config must not read env")), \
             patch.dict("os.environ", {"LLM_CONTEXT_TOKENS": "90000", "TRANSLATE_CONTEXT_TOKENS": "90000"}):
            self.assertEqual(llm.context_window(), 4096)

    def test_template_covers_default_models_and_keeps_local_window_automatic(self):
        from importlib.resources import files
        examples = files("subtitle_translation").joinpath("examples")
        config = json.loads(examples.joinpath("providers.example.json").read_text(encoding="utf-8"))
        for name, provider in config.items():
            self.assertEqual([entry["id"] for entry in provider["model_info"]], [provider["default_model"]])
            limit = mc.configured_limit(provider, provider["default_model"])
            if name == "llama":
                self.assertIsNone(limit)
            else:
                self.assertGreater(limit, 0)
        env = examples.joinpath(".env.example").read_text(encoding="utf-8")
        self.assertNotIn("LLM_CONTEXT_TOKENS", env)
        self.assertNotIn("TRANSLATE_CONTEXT_TOKENS", env)

    def test_discovery_overrides_builtin_and_is_cached_by_endpoint_model_credentials(self):
        llm, client = self.model({"data": [{"id": "fixture", "context_window": 12345}]})
        self.assertEqual(window_for(llm), 12345)
        self.assertEqual(window_for(llm), 12345)
        client.get.assert_called_once_with("models", cast_to=dict)
        client.with_options.assert_called_once_with(timeout=5.0, max_retries=0)
        llm.resolve_key = lambda: "different-fixture-key"
        self.assertEqual(window_for(llm), 12345)
        self.assertEqual(client.get.call_count, 2)

    def test_failure_negative_cache_and_explicit_override(self):
        llm, client = self.model({})
        client.get.side_effect = TimeoutError
        self.assertEqual(window_for(llm), 32768)
        self.assertEqual(window_for(llm), 32768)
        llm.cfg = lambda: {"model_info": [{"id": "fixture", "context_length": 4096}]}
        self.assertEqual(window_for(llm), 4096)
        client.get.assert_called_once()

    def test_gemini_native_metadata_uses_same_host_and_header_auth(self):
        llm, client = self.model({"inputTokenLimit": 64000},
                                "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-test")
        self.assertEqual(mc.discover(llm), 64000)
        self.assertEqual(client.get.call_args.args[0], "models/gemini-test")
        self.assertEqual(client.get.call_args.kwargs["options"]["headers"]["x-goog-api-key"], "fixture-key")

    def test_cancel_not_cached_or_swallowed(self):
        llm, client = self.model({})
        client.get.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            mc.discover(llm)
        self.assertFalse(mc._cache)

    def session(self, window=1000):
        llm = SimpleNamespace(context_window=lambda: window)
        session = t.ChatSession(llm, "FULL GLOSSARY")
        session.messages += [{"role": "user", "content": "old" * 1000},
                             {"role": "assistant", "content": "old target"}]
        return session

    def test_current_tool_group_is_not_orphaned(self):
        s = self.session()
        current = [{"role": "user", "content": "current"},
                   {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
                   {"role": "tool", "tool_call_id": "a", "content": "evidence"}]
        s.messages += copy.deepcopy(current)
        self.assertTrue(mc.compact(s, {"messages": s.messages, "tools": [{"name": "search"}]}))
        self.assertEqual(s.messages, [{"role": "system", "content": "FULL GLOSSARY"}, *current])

    def test_single_tool_turn_is_never_trimmed(self):
        s = self.session()
        s.messages = [s.messages[0], {"role": "user", "content": "current" * 1000},
                      {"role": "assistant", "tool_calls": [{"id": "a"}]},
                      {"role": "tool", "tool_call_id": "a", "content": "full evidence"}]
        before = copy.deepcopy(s.messages)
        self.assertFalse(mc.compact(s, {"messages": s.messages}, force=True))
        self.assertEqual(s.messages, before)

    def test_feedback_retains_original_current_request(self):
        s = self.session()
        s.request_start = len(s.messages)
        tail = [{"role": "user", "content": "original current"},
                {"role": "assistant", "content": "invalid JSON"},
                {"role": "user", "content": "please fix JSON"}]
        s.messages += copy.deepcopy(tail)
        mc.compact(s, {"messages": s.messages})
        self.assertEqual(s.messages[1:], tail)

    def test_shared_sdk_entry_budgets_create_and_ask_and_records_usage(self):
        for method in ("create", "ask"):
            with self.subTest(method=method):
                s = self.session()
                calls = []
                def create(**kwargs):
                    calls.append(copy.deepcopy(kwargs["messages"]))
                    return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=50), choices=[
                        SimpleNamespace(message=SimpleNamespace(content='{"items":[]}'))])
                s.llm.cfg = lambda: {}
                s.llm.model_name = lambda: "fixture"
                s.llm._client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                if method == "create":
                    s.messages.append({"role": "user", "content": "current"})
                    s.create()
                else:
                    s.ask("current")
                self.assertEqual(len(calls[0]), 2)
                self.assertEqual(calls[0][-1]["content"], "current")
                self.assertEqual(s.last_prompt_tokens, 50)

    def test_output_reserve_and_usage_trigger_earlier_compaction(self):
        s = self.session(10000)
        s.messages += [{"role": "user", "content": "current"}]
        s.last_prompt_tokens = 9000
        s.usage_messages = copy.deepcopy(s.messages[:3])
        self.assertTrue(mc.compact(s, {"messages": s.messages, "max_completion_tokens": 5000}))

    def test_shared_entry_recovers_overflow_without_repeating_current_request(self):
        s = self.session(100000)
        calls = []
        def create(**kwargs):
            calls.append(copy.deepcopy(kwargs["messages"]))
            if len(calls) == 1:
                raise RuntimeError("context_length_exceeded")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        s.llm.cfg = lambda: {}
        s.llm.model_name = lambda: "fixture"
        s.llm._client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        self.assertEqual(s.ask("current"), "ok")
        self.assertEqual([len(c) for c in calls], [4, 2])
        self.assertEqual(calls[1], [calls[0][0], calls[0][-1]])

    def test_metadata_cache_expires(self):
        llm, client = self.model({"data": [{"id": "fixture", "context_length": 64000}]})
        with patch.object(mc.time, "monotonic", return_value=0):
            self.assertEqual(mc.discover(llm), 64000)
        client.get.return_value = {"data": [{"id": "fixture", "context_length": 32000}]}
        with patch.object(mc.time, "monotonic", return_value=3601):
            self.assertEqual(mc.discover(llm), 32000)

    def catalogue(self, rows):
        self.public_request.side_effect = None
        self.public_request.return_value.__enter__.return_value.read.return_value = json.dumps({"data": rows}).encode()

    def test_public_lookup_is_anonymous_and_shared_between_models(self):
        self.catalogue([{"id": "vendor/model-a", "context_length": 64000},
                        {"id": "vendor/model-b", "context_length": 128000}])
        llm, _ = self.model({}, model="model-a")
        self.assertEqual(mc.discover(llm), 64000)
        other, _ = self.model({}, model="private/vendor/model-b")
        self.assertEqual(mc.discover(other), 128000)
        self.public_request.assert_called_once()
        request = self.public_request.call_args.args[0]
        self.assertEqual(request.full_url, mc.OPENROUTER_MODELS_URL)
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json"})
        self.assertIsNone(request.data)

    def test_deployment_metadata_wins_and_does_not_query_catalogue(self):
        llm, _ = self.model({"data": [{"id": "fixture", "context_length": 4096}]})
        self.assertEqual(mc.discover(llm), 4096)
        self.public_request.assert_not_called()

    def test_vendor_is_ignored_even_for_full_id_and_conflicts_are_not_guessed(self):
        self.catalogue([{"id": "a/model", "context_length": 4000},
                        {"id": "b/model", "context_length": 8000},
                        {"id": "a/model:free", "context_length": 2000}])
        self.assertIsNone(mc.public_model_limit("model"))
        self.assertIsNone(mc.public_model_limit("a/model"))
        self.assertIsNone(mc.public_model_limit("model-latest"))
        self.assertIsNone(mc.public_model_limit("unknown/model"))
        self.assertIsNone(mc.public_model_limit("mirror/model:batch"))
        self.assertEqual(mc.public_model_limit("mirror/model:free"), 2000)

    def test_same_model_id_can_reuse_agreeing_catalogue_values(self):
        self.catalogue([{"id": "a/model", "context_length": 64000},
                        {"id": "b/model", "context_length": 64000}])
        self.assertEqual(mc.public_model_limit("mirror/model"), 64000)

    def test_unknown_window_reports_effective_model_and_provider(self):
        llm, _ = self.model({})
        llm.provider = "gateway"
        llm.cfg = lambda: {"request_kwargs": {"model": "mirror/missing"}}
        with patch("subtitle_translation.translation_context.log") as log:
            self.assertEqual(window_for(llm), 32768)
        log.assert_called_once_with("INFO", "Unknown model context length for provider='gateway' "
                                          "model='mirror/missing'; using a 32768-token planning budget")

    def test_public_failure_is_negative_cached_and_cancellation_propagates(self):
        self.assertIsNone(mc.public_model_limit("a"))
        self.assertIsNone(mc.public_model_limit("b"))
        self.public_request.assert_called_once()
        mc._public_cache = (0, [])
        self.public_request.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            mc.public_model_limit("a")
        self.assertEqual(mc._public_cache, (0, []))

    def test_public_catalogue_refresh_and_invalid_limits(self):
        self.catalogue([{"id": "a/model", "context_length": 64000}])
        with patch.object(mc.time, "monotonic", return_value=0):
            self.assertEqual(mc.public_model_limit("model"), 64000)
        self.catalogue([{"id": "a/model", "context_length": True}])
        with patch.object(mc.time, "monotonic", return_value=3601):
            self.assertIsNone(mc.public_model_limit("model"))
        self.assertEqual(self.public_request.call_count, 2)

    def test_invalid_or_oversized_public_catalogue_is_not_used(self):
        for content in (b"[]", b"not JSON", b"x" * (16 * 1024 * 1024 + 1)):
            with self.subTest(size=len(content)):
                mc._public_cache = (0, [])
                self.public_request.side_effect = None
                self.public_request.return_value.__enter__.return_value.read.return_value = content
                self.assertIsNone(mc.public_model_limit("fixture"))
