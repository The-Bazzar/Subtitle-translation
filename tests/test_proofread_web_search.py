import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import translate_srt as t


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or []


class FakeResponse:
    def __init__(self, message):
        self.choices = [SimpleNamespace(message=message, finish_reason="tool_calls" if message.tool_calls else "stop")]


class FakeCompletions:
    def __init__(self, responses, calls):
        self.responses = list(responses)
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return response(kwargs) if callable(response) else response


class FakeLLM:
    provider = "fake"
    model = "proofreader"
    batch_size = 10
    api_key = None

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def model_name(self):
        return self.model

    def cfg(self):
        return {"response_format": {"type": "json_object"}}

    def _client(self):
        return SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions(self.responses, self.calls))
        )


def web_tool_call(query="official name", item_ids=None):
    return SimpleNamespace(
        id="web_1",
        type="function",
        function=SimpleNamespace(
            name="web_search",
            arguments=json.dumps({"query": query, "item_ids": item_ids or [1]}),
        ),
    )


class ProofreadWebSearchTests(unittest.TestCase):
    def test_shared_runtime_singleflights_equivalent_concurrent_queries(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=2,
        )
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def search(*_args, **_kwargs):
            calls.append(1)
            entered.set()
            release.wait(1)
            return [{"url": "https://official.example/a", "title": "A", "content": "evidence"}]

        results = {}
        with patch.object(t, "tavily_search", side_effect=search):
            first = threading.Thread(target=lambda: results.setdefault(
                1, runtime.execute_search({"query": "Official   Name", "item_ids": [1]})
            ))
            second = threading.Thread(target=lambda: results.setdefault(
                2, runtime.execute_search({"query": "official name", "item_ids": [2]})
            ))
            first.start(); entered.wait(1); second.start(); time.sleep(0.02); release.set()
            first.join(1); second.join(1)

        self.assertEqual(len(calls), 1)
        self.assertEqual(runtime.used_queries, 1)
        self.assertEqual(runtime.singleflight_reuses, 1)
        self.assertEqual(runtime.sidecar.records[0].item_ids, [1, 2])
        self.assertTrue(results[1]["results"] and results[2]["results"])

    def test_singleflight_attributes_consumers_to_their_evidence_stage(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=1,
        )
        entered = threading.Event()
        release = threading.Event()

        def search(*_args, **_kwargs):
            entered.set()
            release.wait(1)
            return [{"url": "https://official.example/a", "content": "evidence"}]

        with patch.object(t, "tavily_search", side_effect=search):
            first = threading.Thread(
                target=lambda: runtime.execute_search(
                    {"query": "official name", "item_ids": [1]},
                    search_stage="glossary_tool",
                )
            )
            second = threading.Thread(
                target=lambda: runtime.execute_search(
                    {"query": "Official Name", "item_ids": [2]},
                    search_stage="proofread_tool",
                )
            )
            first.start(); entered.wait(1); second.start(); time.sleep(0.02); release.set()
            first.join(1); second.join(1)

        glossary = next(
            record for record in runtime.sidecar.records
            if record.search_stage == "glossary_tool"
        )
        proofread = next(
            record for record in runtime.sidecar.records
            if record.search_stage == "proofread_tool"
        )
        self.assertEqual(glossary.item_ids, [1])
        self.assertEqual(proofread.item_ids, [2])

    def test_cached_query_reuses_evidence_after_budget_is_exhausted(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=1,
        )
        evidence = [{"url": "https://official.example/a", "title": "A", "content": "evidence"}]
        with patch.object(t, "tavily_search", return_value=evidence) as search:
            runtime.execute_search({"query": "official name", "item_ids": [1]})
            result = runtime.execute_search({"query": "OFFICIAL NAME", "item_ids": [2]})
            third = runtime.execute_search({"query": "official-name", "item_ids": [3]})

        search.assert_called_once()
        self.assertTrue(result["reused_evidence"])
        self.assertEqual(third["item_ids"], [1, 2, 3])
        self.assertEqual(runtime.used_queries, 1)
        self.assertEqual(runtime.sidecar.records[0].item_ids, [1, 2, 3])

    def test_persisted_evidence_prewarms_normalized_query_cache(self):
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="Official Name",
                    provider="tavily",
                    item_ids=[1],
                    search_stage="proofread_tool",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://official.example/name",
                            title="Official name",
                            content="authoritative evidence",
                        )
                    ],
                ),
                t.WebEvidenceRecord(
                    query="official-name",
                    provider="exa",
                    item_ids=[3],
                    search_stage="glossary_tool",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://official.example/name",
                            title="Duplicate provider result",
                            content="duplicate evidence with a more complete official explanation",
                        )
                    ],
                ),
            ]
        )
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(), max_queries=0, sidecar=sidecar,
        )

        result = runtime.execute_search(
            {
                "query": "  OFFICIAL—NAME! ",
                "item_ids": [2],
                "topic_hints": ["current context"],
                "preferred_domains": ["official.example"],
            },
            search_stage="proofread_tool",
        )

        self.assertTrue(result["reused_evidence"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(
            result["results"][0]["content"],
            "duplicate evidence with a more complete official explanation",
        )
        self.assertEqual(runtime.used_queries, 0)
        self.assertEqual(runtime.cache_reuses, 1)
        self.assertEqual(runtime.sidecar.records[0].item_ids, [1, 2])
        self.assertEqual(runtime.sidecar.records[1].item_ids, [3])
        self.assertEqual(result["item_ids"], [2])
        self.assertEqual(result["query"], "OFFICIAL—NAME!")
        self.assertIn("current context", result["topic_hints"])
        self.assertIn("official.example", result["preferred_domains"])
        self.assertEqual(
            result["results"][0]["content"],
            "duplicate evidence with a more complete official explanation",
        )

    def test_sidecar_merge_coalesces_consumers_but_preserves_evidence_stage(self):
        def record(item_id, stage, url):
            return t.WebEvidenceRecord(
                query="Official   Name",
                provider="tavily",
                item_ids=[item_id],
                search_stage=stage,
                results=[t.WebEvidenceEntry(url=url, content=f"evidence {item_id}")],
            )

        original = t.WebEvidenceSidecar(
            records=[record(1, "proofread_tool", "https://example.com/a")]
        )
        merged = t.merge_web_evidence_sidecars(
            original,
            t.WebEvidenceSidecar(
                records=[
                    record(2, "proofread_tool", "https://example.com/b"),
                    record(3, "glossary_tool", "https://example.com/c"),
                ]
            ),
        )

        self.assertEqual(len(merged.records), 2)
        proofread = next(
            record for record in merged.records if record.search_stage == "proofread_tool"
        )
        glossary = next(
            record for record in merged.records if record.search_stage == "glossary_tool"
        )
        self.assertEqual(proofread.item_ids, [1, 2])
        self.assertEqual(len(proofread.results), 2)
        self.assertEqual(glossary.item_ids, [3])
        self.assertEqual(original.records[0].item_ids, [1])
        self.assertEqual(len(original.records[0].results), 1)

    def test_failed_singleflight_marks_all_waiters_unresolved_without_deadlock(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=1,
        )
        entered = threading.Event(); release = threading.Event(); calls = []

        def failing_search(*_args, **_kwargs):
            calls.append(1); entered.set(); release.wait(1)
            raise RuntimeError("provider unavailable")

        results = {}
        with patch.object(t, "tavily_search", side_effect=failing_search):
            first = threading.Thread(target=lambda: results.setdefault(
                1, runtime.execute_search({"query": "Unknown Name", "item_ids": [1]})
            ))
            second = threading.Thread(target=lambda: results.setdefault(
                2, runtime.execute_search({"query": "unknown name", "item_ids": [2]})
            ))
            first.start(); entered.wait(1); second.start(); time.sleep(0.02); release.set()
            first.join(1); second.join(1)

        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(runtime.used_queries, 1)
        self.assertEqual(runtime.unresolved_item_ids, {1, 2})
        self.assertTrue(results[1].get("error") and results[2].get("error"))

    def test_failed_earlier_work_unit_releases_later_dynamic_search_budget(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=2,
        )
        runtime.configure_work_units([(0, [1, 2]), (1, [3])])
        failed_entered = threading.Event()
        release_failure = threading.Event()
        later_called = threading.Event()
        calls = []

        def search(query, *_args, **_kwargs):
            calls.append(query)
            if "unknown" in query.casefold():
                failed_entered.set()
                release_failure.wait(1)
                raise RuntimeError("provider unavailable")
            later_called.set()
            return [{"url": "https://official.example/resolved", "content": "resolved"}]

        results = {}
        with patch.object(t, "tavily_search", side_effect=search):
            owner = threading.Thread(target=lambda: results.setdefault(
                1, runtime.execute_search({"query": "Unknown Name", "item_ids": [1]})
            ))
            waiter = threading.Thread(target=lambda: results.setdefault(
                2, runtime.execute_search({"query": "unknown name", "item_ids": [2]})
            ))
            later = threading.Thread(target=lambda: results.setdefault(
                3, runtime.execute_search({"query": "Known Name", "item_ids": [3]})
            ))
            owner.start(); failed_entered.wait(1); waiter.start(); later.start()
            time.sleep(0.02)
            self.assertFalse(later_called.is_set())
            release_failure.set()
            owner.join(1); waiter.join(1)
            runtime.mark_work_unit_done(0)
            later.join(1)

        self.assertFalse(owner.is_alive() or waiter.is_alive() or later.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertEqual(runtime.used_queries, 2)
        self.assertEqual(runtime.singleflight_reuses, 1)
        self.assertEqual(runtime.unresolved_item_ids, {1, 2})
        self.assertTrue(results[3]["results"])

    def test_enhanced_proofread_requires_an_explicit_feature_switch(self):
        self.assertFalse(t.explicit_proofread_model_configured({}))
        self.assertFalse(t.explicit_proofread_model_configured({"PROOFREAD_PROVIDER": "custom"}))
        self.assertFalse(t.explicit_proofread_model_configured({"PROOFREAD_MODEL": "review-model"}))
        self.assertTrue(t.explicit_proofread_model_configured({"PROOFREAD_ENHANCED": "1"}))

    def test_search_configuration_is_exposed_through_the_setup_chain(self):
        root = Path(__file__).resolve().parents[1]
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        agents = (root / "AGENTS.md").read_text(encoding="utf-8")
        setup_ps1 = (root / "setup.ps1").read_text(encoding="utf-8")
        setup_sh = (root / "setup.sh").read_text(encoding="utf-8")

        for key in (
            "PROOFREAD_ENHANCED",
            "PROOFREAD_SEARCH_MAX_QUERIES",
            "PROOFREAD_CONCURRENCY",
            "PROOFREAD_THINKING",
            "PROOFREAD_REASONING_EFFORT",
            "WEB_SEARCH_PROVIDER",
            "EXA_API_KEY",
            "EXA_MAX_RESULTS",
        ):
            self.assertIn(f"{key}=", env_example)
            self.assertIn(f"`{key}`", readme)
            self.assertIn(f"`{key}`", agents)
        self.assertIn("Update-EnvFromExample", setup_ps1)
        self.assertIn("update_env_from_example", setup_sh)

    def test_search_settings_cover_all_provider_combinations(self):
        self.assertEqual(t.WebSearchSettings.from_env({}).configured_providers(), [])
        self.assertEqual(
            t.WebSearchSettings.from_env({"TAVILY_API_KEY": "t"}).configured_providers(),
            ["tavily"],
        )
        self.assertEqual(
            t.WebSearchSettings.from_env({"EXA_API_KEY": "e"}).configured_providers(),
            ["exa"],
        )
        self.assertEqual(
            t.WebSearchSettings.from_env(
                {"TAVILY_API_KEY": "t", "EXA_API_KEY": "e"}
            ).configured_providers(),
            ["tavily", "exa"],
        )
        self.assertEqual(
            t.WebSearchSettings.from_env(
                {"TAVILY_API_KEY": "t", "WEB_SEARCH_PROVIDER": "exa"}
            ).configured_providers(),
            [],
        )

    def test_web_search_settings_do_not_expose_an_unenforced_timeout(self):
        settings = t.WebSearchSettings.from_env({})

        self.assertFalse(hasattr(settings, "timeout"))

    def test_exa_search_uses_official_sdk_and_normalizes_highlights(self):
        captured = {}

        class FakeExa:
            def __init__(self, api_key):
                captured["api_key"] = api_key

            def search(self, query, **kwargs):
                captured["query"] = query
                captured["kwargs"] = kwargs
                return SimpleNamespace(
                    results=[
                        SimpleNamespace(
                            url="https://example.com/work",
                            title="Official work",
                            highlights=["Official Chinese title", "Creator page"],
                        )
                    ]
                )

        with patch.object(t, "Exa", FakeExa):
            results = t.exa_search(
                "work official Chinese title",
                "secret",
                max_results=2,
                preferred_domains=["example.com"],
            )

        self.assertEqual(captured["api_key"], "secret")
        self.assertEqual(captured["query"], "work official Chinese title")
        self.assertEqual(captured["kwargs"]["include_domains"], ["example.com"])
        self.assertEqual(captured["kwargs"]["num_results"], 2)
        self.assertEqual(captured["kwargs"]["contents"], {"highlights": {"max_characters": 1200}})
        self.assertEqual(results[0]["content"], "Official Chinese title Creator page")

    def test_exa_network_failure_returns_no_results(self):
        class FailingExa:
            def __init__(self, api_key):
                pass

            def search(self, *_args, **_kwargs):
                raise TimeoutError("offline")

        with patch.object(t, "Exa", FailingExa):
            self.assertEqual(t.exa_search("query", "key"), [])

    def test_auto_provider_falls_back_from_empty_tavily_to_exa(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t", exa_key="e"),
            max_queries=2,
        )
        with patch.object(t, "tavily_search", return_value=[]) as tavily, patch.object(
            t,
            "exa_search",
            return_value=[{"url": "https://official.example/a", "title": "A", "content": "evidence"}],
        ) as exa:
            result = runtime.execute_search({"query": "uncertain proper noun", "item_ids": [3]})

        tavily.assert_called_once()
        exa.assert_called_once()
        self.assertEqual(runtime.used_queries, 2)
        self.assertEqual(result["results"][0]["provider"], "exa")
        self.assertEqual(runtime.sidecar.records[0].item_ids, [3])

    def test_exa_can_independently_build_glossary_evidence(self):
        transcript = t.Transcript(
            "video.json", "en", [t.TranscriptSegment(1, 0.0, 1.0, "named work")]
        )
        ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")
        options = t.GlossaryBuildOptions(
            exa_key="e",
            search_provider="exa",
            tavily_max_queries=1,
            quiet=True,
        )
        with patch.object(
            t,
            "build_tavily_search_plan",
            return_value=t.TavilySearchPlan(queries=["named work official title"], topic_hints=["film"]),
        ), patch.object(
            t,
            "exa_search",
            return_value=[{"url": "https://official.example/work", "content": "official title"}],
        ) as exa:
            sidecar = t.build_tavily_search_evidence(
                transcript, ctx, FakeLLM([]), {}, options
            )

        exa.assert_called_once()
        self.assertEqual(sidecar.records[0].provider, "exa")
        self.assertEqual(sidecar.records[0].search_stage, "glossary_fallback")

    def test_search_budget_and_exact_evidence_reuse(self):
        existing = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="known quote",
                    provider="tavily",
                    results=[t.WebEvidenceEntry(url="https://example.com/q", content="quote source")],
                )
            ]
        )
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"),
            max_queries=1,
            sidecar=existing,
        )
        with patch.object(t, "tavily_search") as search:
            cached = runtime.execute_search({"query": "Known   Quote", "item_ids": [2]})
            runtime.execute_search({"query": "new query"})
            exhausted = runtime.execute_search({"query": "another query"})

        self.assertEqual(search.call_count, 1)
        self.assertEqual(runtime.used_queries, 1)
        self.assertEqual(cached["results"][0]["content"], "quote source")
        self.assertEqual(existing.records[0].item_ids, [])
        self.assertEqual(runtime.sidecar.records[0].item_ids, [2])
        self.assertIn("budget", exhausted["error"])

    def test_existing_evidence_is_reused_without_any_api_key(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(),
            max_queries=2,
            sidecar=t.WebEvidenceSidecar(
                records=[
                    t.WebEvidenceRecord(
                        query="official quote",
                        provider="exa",
                        results=[
                            t.WebEvidenceEntry(
                                url="https://example.com/quote", content="authoritative quote"
                            )
                        ],
                    )
                ]
            ),
        )

        result = runtime.execute_search({"query": "Official Quote", "item_ids": [8]})

        self.assertTrue(result["reused_evidence"])
        self.assertEqual(result["results"][0]["provider"], "exa")
        self.assertEqual(runtime.used_queries, 0)

    def test_zero_budget_config_keeps_persisted_exact_cache_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")
            t.write_web_evidence_sidecar(
                ctx,
                t.WebEvidenceSidecar(records=[t.WebEvidenceRecord(
                    query="known name",
                    provider="tavily",
                    results=[t.WebEvidenceEntry(
                        url="https://example.com/name", content="Known Name - 已知名称"
                    )],
                )]),
            )
            runtime = t.proofread_search_runtime_from_env(
                {"PROOFREAD_ENHANCED": "1", "PROOFREAD_SEARCH_MAX_QUERIES": "0"},
                ctx,
                quiet=True,
            )
            self.assertIsNotNone(runtime)
            with patch.object(t, "tavily_search") as online:
                result = runtime.execute_search({"query": "Known   Name", "item_ids": [1]})

        self.assertTrue(result["reused_evidence"])
        self.assertEqual(result["results"][0]["content"], "Known Name - 已知名称")
        self.assertEqual(runtime.used_queries, 0)
        online.assert_not_called()

    def test_proofread_tool_loop_searches_only_when_model_requests_it(self):
        final = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "en": "official work",
                        "zh": "官方作品名",
                        "review": {
                            "needs_human": False,
                            "categories": [],
                            "reasons": [],
                            "alternatives": [],
                            "note": "",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )
        llm = FakeLLM(
            [
                FakeResponse(FakeMessage(tool_calls=[web_tool_call()])),
                FakeResponse(FakeMessage(content=final)),
            ]
        )
        event = t.SplitEvent(4.0, 5.5, "official work", "原译")
        transcript = t.Transcript(
            "video.json",
            "en",
            [t.TranscriptSegment(1, 4.0, 5.5, "official work", split_events=[event])],
        )
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"),
            max_queries=2,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            t,
            "tavily_search",
            return_value=[{"url": "https://example.com/official", "content": "official evidence"}],
        ) as search:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")
            changed = t.proofread_split_events(
                transcript,
                ctx,
                llm,
                "system",
                quiet=True,
                enhanced=True,
                search_runtime=runtime,
            )
            saved = t.load_web_evidence_sidecar(ctx.web_evidence_json)

        self.assertTrue(changed)
        search.assert_called_once()
        self.assertEqual((event.start, event.end), (4.0, 5.5))
        self.assertEqual(len(transcript.segments[0].split_events), 1)
        self.assertEqual(event.zh, "官方作品名")
        self.assertEqual(saved.records[0].search_stage, "proofread_tool")
        self.assertEqual(saved.records[0].item_ids, [1])
        self.assertIn("tools", llm.calls[0])
        self.assertNotIn("response_format", llm.calls[0])

    def test_legacy_proofread_path_never_receives_search_tools(self):
        event = t.SplitEvent(0.0, 1.0, "That works", "那个工作")
        transcript = t.Transcript(
            "video.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, "That works", split_events=[event])],
        )
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"),
            max_queries=2,
        )
        with patch.object(
            t,
            "llm_numbered_batch",
            return_value=[{"id": 1, "en": "That works", "zh": "这样就行", "review": {}}],
        ) as legacy, patch.object(t, "llm_numbered_batch_with_web_search") as enhanced:
            t.proofread_split_events(
                transcript,
                t.TranscriptContext.from_json("video.json", "", "en", "zh"),
                FakeLLM([]),
                "system",
                quiet=True,
                enhanced=False,
                search_runtime=runtime,
            )

        legacy.assert_called_once()
        enhanced.assert_not_called()

    def test_tool_incompatibility_falls_back_to_legacy_proofread(self):
        event = t.SplitEvent(0.0, 1.0, "source", "首译")
        transcript = t.Transcript(
            "video.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, "source", split_events=[event])],
        )
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"),
            max_queries=1,
        )
        with patch.object(
            t, "llm_numbered_batch_with_web_search", side_effect=RuntimeError("tools unsupported")
        ), patch.object(
            t,
            "llm_numbered_batch",
            return_value=[{"id": 1, "en": "source", "zh": "回退校对", "review": {}}],
        ) as legacy:
            t.proofread_split_events(
                transcript,
                t.TranscriptContext.from_json("video.json", "", "en", "zh"),
                FakeLLM([]),
                "system",
                quiet=True,
                enhanced=True,
                search_runtime=runtime,
            )

        legacy.assert_called_once()
        self.assertEqual(event.zh, "回退校对")

    def test_empty_search_results_do_not_block_conservative_final_answer(self):
        final = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "en": "possibly misheard name",
                        "zh": "疑似误识别的人名",
                        "review": {
                            "needs_human": True,
                            "categories": ["source_ASR"],
                            "reasons": ["联网无有效结果，无法确认人名"],
                            "alternatives": [],
                            "note": "请人工核对音频",
                        },
                    }
                ]
            },
            ensure_ascii=False,
        )
        llm = FakeLLM(
            [
                FakeResponse(FakeMessage(tool_calls=[web_tool_call("misheard name")])),
                FakeResponse(FakeMessage(content=final)),
            ]
        )
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"),
            max_queries=1,
        )
        request = t.LLMBatchRequest(
            [t.LLMBatchItem(1, {"en": "possibly misheard name", "zh": "疑似误识别的人名"})]
        )
        with patch.object(t, "tavily_search", return_value=[]):
            result = t.llm_numbered_batch_with_web_search(
                request,
                t.ChatSession(llm, "system", disable_response_format=True),
                runtime,
                quiet=True,
            )

        self.assertEqual(result[0]["en"], "possibly misheard name")
        self.assertTrue(result[0]["review"]["needs_human"])
        tool_payload = json.loads(llm.calls[1]["messages"][-1]["content"])
        self.assertIn("no valid results", tool_payload["error"])


if __name__ == "__main__":
    unittest.main()
