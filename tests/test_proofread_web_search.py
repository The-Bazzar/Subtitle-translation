import copy
import json
import os
import tempfile
import threading
import time
import unittest
from urllib import error as urllib_error
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

    def test_cached_query_reuses_evidence_after_budget_is_exhausted(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=1,
        )
        evidence = [{"url": "https://official.example/a", "title": "A", "content": "evidence"}]
        with patch.object(t, "tavily_search", return_value=evidence) as search:
            runtime.execute_search({"query": "official name", "item_ids": [1]})
            result = runtime.execute_search({"query": "OFFICIAL NAME", "item_ids": [2]})

        search.assert_called_once()
        self.assertTrue(result["reused_evidence"])
        self.assertEqual(runtime.used_queries, 1)
        self.assertEqual(runtime.sidecar.records[0].item_ids, [1, 2])

    def test_failed_singleflight_marks_all_waiters_unresolved_without_deadlock(self):
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"), max_queries=1,
        )
        entered = threading.Event(); release = threading.Event(); calls = []

        def empty_search(*_args, **_kwargs):
            calls.append(1); entered.set(); release.wait(1); return []

        results = {}
        with patch.object(t, "tavily_search", side_effect=empty_search):
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

    def test_enhanced_proofread_requires_explicit_model_not_provider(self):
        self.assertFalse(t.explicit_proofread_model_configured({}))
        self.assertFalse(t.explicit_proofread_model_configured({"PROOFREAD_PROVIDER": "custom"}))
        self.assertTrue(t.explicit_proofread_model_configured({"PROOFREAD_MODEL": "review-model"}))

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

    def test_exa_search_uses_rest_api_and_normalizes_highlights(self):
        captured = {}

        class FakeHTTPResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "results": [
                            {
                                "url": "https://example.com/work",
                                "title": "Official work",
                                "highlights": ["Official Chinese title", "Creator page"],
                            }
                        ]
                    }
                ).encode()

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeHTTPResponse()

        with patch.object(t.urllib_request, "urlopen", side_effect=fake_urlopen):
            results = t.exa_search(
                "work official Chinese title",
                "secret",
                max_results=2,
                preferred_domains=["example.com"],
            )

        self.assertEqual(captured["url"], "https://api.exa.ai/search")
        self.assertEqual(captured["payload"]["includeDomains"], ["example.com"])
        self.assertEqual(captured["payload"]["numResults"], 2)
        self.assertNotIn("secret", json.dumps(captured["payload"]))
        self.assertEqual(results[0]["content"], "Official Chinese title Creator page")

    def test_exa_network_failure_returns_no_results(self):
        with patch.object(
            t.urllib_request,
            "urlopen",
            side_effect=urllib_error.URLError("offline"),
        ):
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
        self.assertEqual(existing.records[0].item_ids, [2])
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

    def test_ordinary_language_edit_does_not_search(self):
        final = json.dumps(
            {"items": [{"id": 1, "en": "That works", "zh": "这样就行", "review": {}}]},
            ensure_ascii=False,
        )
        llm = FakeLLM([FakeResponse(FakeMessage(content=final))])
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="t"),
            max_queries=2,
        )
        request = t.LLMBatchRequest([t.LLMBatchItem(1, {"en": "That works", "zh": "那个工作"})])

        with patch.object(t, "tavily_search") as search:
            result = t.llm_numbered_batch_with_web_search(
                request,
                t.ChatSession(llm, "system", disable_response_format=True),
                runtime,
                quiet=True,
            )

        search.assert_not_called()
        self.assertEqual(result[0]["zh"], "这样就行")

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

    def test_proofread_evidence_does_not_change_glossary_fingerprint(self):
        transcript = t.Transcript("video.json", "en", [t.TranscriptSegment(1, 0.0, 1.0, "source")])
        ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")
        glossary_url = "https://example.com/term"
        proofread_url = "https://example.com/meme"
        glossary_record = t.WebEvidenceRecord(
            query="term",
            provider="tavily",
            search_stage="glossary_tool",
            results=[t.WebEvidenceEntry(url=glossary_url, content="term evidence")],
        )
        glossary_term = t.ConfirmedTermEvidence(
            "Global", "全局", evidence_urls=[glossary_url]
        )
        before = t.glossary_cache_fingerprint(
            transcript,
            ctx,
            {},
            t.WebEvidenceSidecar(records=[glossary_record], confirmed_terms=[glossary_term]),
        )
        after = t.glossary_cache_fingerprint(
            transcript,
            ctx,
            {},
            t.WebEvidenceSidecar(
                records=[
                    glossary_record,
                    t.WebEvidenceRecord(
                        query="meme",
                        provider="exa",
                        search_stage="proofread_tool",
                        item_ids=[1],
                        results=[t.WebEvidenceEntry(url=proofread_url, content="meme evidence")],
                    ),
                ],
                confirmed_terms=[
                    glossary_term,
                    t.ConfirmedTermEvidence("Local", "局部", evidence_urls=[proofread_url]),
                ],
            ),
        )
        self.assertEqual(before, after)
        filtered = t.glossary_web_evidence(
            t.WebEvidenceSidecar(
                records=[
                    glossary_record,
                    t.WebEvidenceRecord(
                        query="meme",
                        provider="exa",
                        search_stage="proofread_tool",
                        item_ids=[1],
                        results=[t.WebEvidenceEntry(url=proofread_url, content="meme evidence")],
                    ),
                ],
                confirmed_terms=[
                    glossary_term,
                    t.ConfirmedTermEvidence("Local", "局部", evidence_urls=[proofread_url]),
                ],
            )
        )
        self.assertEqual([term.source for term in filtered.confirmed_terms], ["Global"])


if __name__ == "__main__":
    unittest.main()
