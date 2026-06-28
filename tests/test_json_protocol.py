import json
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import translate_srt as t


class FakeSDKMessage:
    def __init__(self, content="", tool_calls=None, role="assistant", **extra):
        self.content = content
        self.tool_calls = tool_calls or []
        self.role = role
        for key, value in extra.items():
            setattr(self, key, value)


class FakeSDKChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class FakeSDKResponse:
    def __init__(self, message=None, finish_reason="stop", usage=None):
        self.choices = [FakeSDKChoice(message or FakeSDKMessage(), finish_reason)]
        if usage is not None:
            self.usage = usage


class FakeSDKCompletions:
    def __init__(self, responses, calls):
        self.responses = list(responses or [FakeSDKResponse()])
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        return response(kwargs) if callable(response) else response


class FakeChatLLM:
    provider = "fake"
    batch_size = 50
    api_key = None

    def __init__(self, responses=None, cfg=None, calls=None, model="fake-model", provider="fake"):
        self.responses = responses or [FakeSDKResponse()]
        self.config = cfg or {}
        self.calls = calls if calls is not None else []
        self.model = model
        self.provider = provider

    def model_name(self):
        return self.model

    def cfg(self):
        return self.config

    def _client(self):
        return SimpleNamespace(
            chat=SimpleNamespace(completions=FakeSDKCompletions(self.responses, self.calls))
        )


class FakeProviderLLM:
    provider = "fake"
    batch_size = 50
    api_key = None

    def model_name(self):
        return "fake-model"

    def cfg(self):
        return {}


class FakeBatchLLM(FakeProviderLLM):
    def __init__(self, batch_size):
        self.batch_size = batch_size


def fake_tool_call(query: str, call_id: str = "call_1", topic_hints=None):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="tavily_search",
            arguments=json.dumps(
                {
                    "query": query,
                    "topic_hints": topic_hints or [],
                },
                ensure_ascii=False,
            ),
        ),
    )


class JsonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")

    def test_subtitle_layout_threshold_defaults_match_1080p_template(self):
        self.assertEqual(t.DEFAULT_SPLIT_MAX_CHARS, 72)
        self.assertEqual(t.DEFAULT_SPLIT_MAX_DURATION, 3.8)
        self.assertEqual(t.SplitConfig().max_chars, t.DEFAULT_SPLIT_MAX_CHARS)
        self.assertEqual(t.SplitConfig().max_duration, t.DEFAULT_SPLIT_MAX_DURATION)

    def test_extract_json_object_rejects_array_shape(self):
        data = t._extract_json_value('{"markdown": "# Glossary"}')
        self.assertEqual(data, {"markdown": "# Glossary"})

    def test_glossary_output_parses_json_object(self):
        result = t.GlossaryOutput.from_json_content('{"markdown": "# 术语知识库"}')
        self.assertEqual(result.markdown, "# 术语知识库")

    def test_glossary_output_rejects_plain_markdown(self):
        with self.assertRaisesRegex(ValueError, "not a JSON object"):
            t.GlossaryOutput.from_json_content("# 术语知识库\n\n## 核心术语")

    def test_batch_response_accepts_wrapped_items(self):
        response = t.LLMBatchResponse.from_json_value({"items": [{"id": 1, "zh": "译文"}]})
        self.assertEqual(response.to_items(), [{"id": 1, "zh": "译文"}])

    def test_batch_response_keeps_legacy_array_compatibility(self):
        response = t.LLMBatchResponse.from_json_value([{"id": 1, "zh": "译文"}])
        self.assertEqual(response.to_items(), [{"id": 1, "zh": "译文"}])

    def test_batch_request_serializes_to_items_object(self):
        request = t.LLMBatchRequest([t.LLMBatchItem(7, {"en": "source"})])
        self.assertEqual(request.to_json_value(), {"items": [{"id": 7, "en": "source"}]})

    def test_object_request_prunes_empty_values(self):
        request = t.LLMObjectRequest(
            {
                "title": "Title",
                "description": "",
                "tags": [],
                "metadata": {},
                "notes": None,
                "count": 0,
                "enabled": False,
                "nested": {
                    "keep": "value",
                    "drop_empty_string": "",
                    "drop_empty_list": [],
                },
            }
        )

        self.assertEqual(
            request.to_json_value(),
            {
                "title": "Title",
                "count": 0,
                "enabled": False,
                "nested": {"keep": "value"},
            },
        )

    def test_batch_item_prunes_empty_values(self):
        item = t.LLMBatchItem(
            7,
            {
                "en": "source",
                "zh": "",
                "retrieved_context": [],
                "context_before": [{"id": 6, "en": "", "zh": "上文"}],
            },
        )

        self.assertEqual(
            item.to_json_value(),
            {
                "id": 7,
                "en": "source",
                "context_before": [{"id": 6, "zh": "上文"}],
            },
        )

    def test_typed_translate_item_serializes_language_key(self):
        item = t.make_source_item(7, self.ctx, "source text")
        self.assertEqual(item.to_json_value(), {"id": 7, "en": "source text"})

    def test_typed_translate_item_serializes_retrieved_context(self):
        item = t.make_source_item(
            7,
            self.ctx,
            "source text",
            retrieved_context=[{"id": "transcript:1", "text": "related context"}],
        )
        self.assertEqual(
            item.to_json_value(),
            {
                "id": 7,
                "en": "source text",
                "retrieved_context": [{"id": "transcript:1", "text": "related context"}],
            },
        )

    def test_typed_split_result_parses_language_arrays(self):
        result = t.SplitOutputItem.from_json_value(
            {"id": 12, "en": ["a", "b"], "zh": ["甲", "乙"]},
            self.ctx,
        )
        self.assertEqual(result.id, 12)
        self.assertEqual(result.source_parts, ["a", "b"])
        self.assertEqual(result.target_parts, ["甲", "乙"])

    def test_typed_split_input_serializes_context(self):
        item = t.make_pair_item(
            12,
            self.ctx,
            "current source",
            "current target",
            context_before=[t.make_pair_json(11, self.ctx, "before source", "before target")],
            context_after=[t.make_pair_json(13, self.ctx, "after source", "after target")],
        )

        data = item.to_json_value()
        self.assertEqual(data["id"], 12)
        self.assertEqual(data["en"], "current source")
        self.assertEqual(data["zh"], "current target")
        self.assertEqual(data["context_before"], [{"id": 11, "en": "before source", "zh": "before target"}])
        self.assertEqual(data["context_after"], [{"id": 13, "en": "after source", "zh": "after target"}])

    def test_typed_proofread_result_parses_language_values(self):
        result = t.LanguageTextResult.from_json_value(
            {"id": 3, "en": "corrected source", "zh": "corrected target"},
            self.ctx,
        )
        self.assertEqual(result.source_text, "corrected source")
        self.assertEqual(result.target_text, "corrected target")

    def test_typed_proofread_item_serializes_retrieved_context(self):
        item = t.make_pair_item(
            3,
            self.ctx,
            "source text",
            "target text",
            retrieved_context=[{"id": "transcript:2", "text": "nearby context"}],
        )
        self.assertEqual(
            item.to_json_value(),
            {
                "id": 3,
                "en": "source text",
                "zh": "target text",
                "retrieved_context": [{"id": "transcript:2", "text": "nearby context"}],
            },
        )

    def test_build_glossary_adds_retrieved_context(self):
        class FakeRetriever:
            def retrieve_texts(self, texts, top_k=None):
                self.texts = texts
                return [[{"id": "transcript:1", "text": "important context", "score": 0.9}]]

        captured = {}

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            captured.update(request.to_json_value())
            return {"markdown": "# 术语知识库"}

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha topic")],
            )
            retriever = FakeRetriever()
            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                t.build_glossary(
                    transcript,
                    ctx,
                    FakeProviderLLM(),
                    t.GlossaryBuildOptions(quiet=True, retriever=retriever),
                )

            self.assertEqual(captured["retrieved_context"], [{"id": "transcript:1", "text": "important context", "score": 0.9}])
            self.assertTrue(retriever.texts[0])

    def test_build_glossary_prefixes_local_video_metadata(self):
        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            return {"markdown": "# 术语知识库\n\n## 核心术语\n\n- discipline：纪律"}

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "title": "Original Title",
                        "webpage_url": "https://youtu.be/example",
                        "uploader": "Original Channel",
                        "upload_date": "20250102",
                    },
                    f,
                )
            with open(ctx.desc, "w", encoding="utf-8") as f:
                f.write("A long-form discussion about discipline and convenience.")
            with open(ctx.tags, "w", encoding="utf-8") as f:
                f.write("['philosophy', 'discipline']")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                glossary = t.build_glossary(transcript, ctx, FakeProviderLLM(), t.GlossaryBuildOptions(quiet=True))

            self.assertIn("## 视频元信息", glossary)
            self.assertIn("原标题：Original Title", glossary)
            self.assertIn("原作者：Original Channel", glossary)
            self.assertIn("上传时间：2025-01-02", glossary)
            self.assertIn("原简介：", glossary)
            self.assertIn("A long-form discussion", glossary)
            self.assertIn("标签：philosophy, discipline", glossary)
            self.assertIn("## 核心术语", glossary)

    def test_build_glossary_reuses_existing_cache_by_default(self):
        def fake_llm_json_once(*args, **kwargs):
            raise AssertionError("cached glossary should not call LLM")

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as f:
                f.write("# 术语知识库\n\n- cached term：缓存术语")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                glossary = t.build_glossary(transcript, ctx, FakeProviderLLM(), t.GlossaryBuildOptions(quiet=True))

        self.assertIn("cached term", glossary)

    def test_build_glossary_force_overwrites_existing_cache(self):
        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            return {"markdown": "# 术语知识库\n\n## 核心术语\n\n- fresh term：新术语"}

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as f:
                f.write("# 术语知识库\n\n- stale term：旧术语")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                glossary = t.build_glossary(
                    transcript,
                    ctx,
                    FakeProviderLLM(),
                    t.GlossaryBuildOptions(quiet=True, force=True),
                )
            with open(ctx.glossary, "r", encoding="utf-8") as f:
                saved = f.read()

        self.assertIn("fresh term", glossary)
        self.assertIn("fresh term", saved)
        self.assertNotIn("stale term", saved)

    def test_local_glossary_metadata_filters_promotional_description_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title"}, f)
            with open(ctx.desc, "w", encoding="utf-8") as f:
                f.write(
                    "\n".join(
                        [
                            "This lecture explains discipline, agency, and daily practice.",
                            "https://example.com/newsletter",
                            "Follow me on Instagram: https://instagram.com/example",
                            "Use code SAVE20 for 20% off my merch.",
                            "Subscribe for more videos.",
                            "The second half compares these ideas with ancient philosophy.",
                        ]
                    )
                )

            section = t.build_local_glossary_metadata_section(ctx)

        self.assertIn("This lecture explains discipline", section)
        self.assertIn("The second half compares", section)
        self.assertNotIn("https://example.com", section)
        self.assertNotIn("Instagram", section)
        self.assertNotIn("SAVE20", section)
        self.assertNotIn("Subscribe", section)
        self.assertIn("已过滤简介中的推广链接、社媒链接、赞助信息和纯 URL 行。", section)

    def test_build_glossary_saves_local_metadata_when_llm_generation_fails(self):
        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            raise RuntimeError("bad json")

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title", "uploader": "Original Channel"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                glossary = t.build_glossary(transcript, ctx, FakeProviderLLM(), t.GlossaryBuildOptions(quiet=True))

            self.assertTrue(os.path.isfile(ctx.glossary))
            with open(ctx.glossary, "r", encoding="utf-8") as f:
                saved = f.read()

        self.assertIn("## 视频元信息", glossary)
        self.assertIn("原标题：Original Title", saved)
        self.assertIn("原作者：Original Channel", saved)

    def test_build_glossary_system_prompt_includes_strict_json_protocols(self):
        captured = {}

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            captured["system_prompt"] = system_prompt
            return {"markdown": "# 术语知识库"}

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                t.build_glossary(transcript, ctx, FakeProviderLLM(), t.GlossaryBuildOptions(quiet=True))

        self.assertIn("MANDATORY JSON PROTOCOL", captured["system_prompt"])
        self.assertIn("Return a JSON object.", captured["system_prompt"])
        self.assertIn("MANDATORY GLOSSARY JSON PROTOCOL", captured["system_prompt"])
        self.assertIn('Return exactly one top-level key: "markdown".', captured["system_prompt"])
        self.assertIn("Treat web search results as the primary evidence", captured["system_prompt"])
        self.assertIn("actively correct likely ASR errors", captured["system_prompt"])
        self.assertIn("Do not copy ASR mistakes into the glossary", captured["system_prompt"])
        self.assertNotIn("Keep under 100 lines", captured["system_prompt"])
        self.assertIn("Core terminology", captured["system_prompt"])
        self.assertIn("Key arguments", captured["system_prompt"])

    def test_build_glossary_tool_session_executes_tavily_in_same_session(self):
        calls = []
        searched = []
        llm = FakeChatLLM(
            calls=calls,
            cfg={"request_kwargs": {"response_format": {"type": "json_object"}}},
            responses=[
                FakeSDKResponse(FakeSDKMessage(tool_calls=[fake_tool_call("corrected term", topic_hints=["anime"])])),
                FakeSDKResponse(
                    FakeSDKMessage(
                        content='{"markdown": "# 术语知识库\\n\\n## 核心术语\\n- corrected term：校正术语"}'
                    )
                ),
            ],
        )

        def fake_tavily_search(query, api_key, max_results=5, preferred_domains=None):
            searched.append((query, api_key, max_results, preferred_domains))
            return [{"url": "https://zh.wikipedia.org/wiki/Corrected", "content": "百科证据"}]

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title", "uploader": "Original Channel"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "asr-ish source topic")],
            )

            with patch.object(t, "tavily_search", side_effect=fake_tavily_search), patch.object(
                t,
                "load_tavily_domain_preferences",
                return_value=t.TavilyDomainPreferences.from_json_value(
                    {
                        "global_domains": ["wikipedia.org"],
                        "topics": [
                            {
                                "name": "anime",
                                "keywords": ["anime"],
                                "domains": ["bgm.tv"],
                            }
                        ],
                    }
                ),
            ):
                glossary = t.build_glossary(
                    transcript,
                    ctx,
                    llm,
                    t.GlossaryBuildOptions(
                        tavily_key="tk",
                        tavily_max_results=6,
                        tavily_max_queries=2,
                        quiet=True,
                    ),
                )

        first_request = json.loads(calls[0]["messages"][1]["content"])
        self.assertIn("tavily_domain_preferences", first_request)
        self.assertEqual(searched[0], ("corrected term", "tk", 3, ["wikipedia.org", "bgm.tv"]))
        self.assertEqual(calls[1]["messages"][-1]["role"], "tool")
        self.assertEqual(calls[1]["messages"][-1]["tool_call_id"], "call_1")
        self.assertNotIn("response_format", calls[0])
        self.assertIn("## 视频元信息", glossary)
        self.assertIn("corrected term", glossary)

    def test_glossary_options_ignore_deprecated_tool_rounds_env(self):
        deprecated_env_key = "GLOSSARY_" + "TOOL_MAX_ROUNDS"
        deprecated_attr = "tavily_" + "tool_rounds"
        options = t.GlossaryBuildOptions.from_env(
            {
                "TAVILY_API_KEY": "tk",
                "TAVILY_MAX_QUERIES": "4",
                deprecated_env_key: "0",
            },
            quiet=True,
        )

        self.assertEqual(options.tavily_max_queries, 4)
        self.assertTrue(options.use_tool_session())
        self.assertFalse(hasattr(options, deprecated_attr))

    def test_build_glossary_tool_session_falls_back_when_query_budget_is_exhausted(self):
        calls = []
        llm = FakeChatLLM(
            calls=calls,
            cfg={"request_kwargs": {"response_format": {"type": "json_object"}}},
            responses=[
                FakeSDKResponse(
                    FakeSDKMessage(tool_calls=[fake_tool_call("still needs search")]),
                    finish_reason="tool_calls",
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Fallback Title", "uploader": "Fallback Channel"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )

            with patch.object(t, "tavily_search", return_value=[]), patch("sys.stderr", new_callable=io.StringIO) as err:
                glossary = t.build_glossary(
                    transcript,
                    ctx,
                    llm,
                    t.GlossaryBuildOptions(tavily_key="tk", tavily_max_queries=1, quiet=True),
                )

        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertEqual(calls[1]["tool_choice"], "none")
        self.assertNotIn("response_format", calls[0])
        self.assertNotIn("response_format", calls[1])
        self.assertIn("glossary Tavily query budget reached", err.getvalue())
        self.assertIn("## 视频元信息", glossary)
        self.assertIn("Fallback Title", glossary)

    def test_build_glossary_request_fields_prunes_empty_metadata_but_keeps_domain_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )
            fields = {
                "title": "Title",
                "uploader": "",
                "webpage_url": "",
                "upload_time": "",
                "description": "",
                "tags": [],
            }
            preferences = t.TavilyDomainPreferences.from_json_value(
                {"global_domains": ["wikipedia.org"], "topics": [{"name": "anime", "domains": ["bgm.tv"]}]}
            )

            request = t.LLMObjectRequest(
                t.build_glossary_request_fields(
                    transcript,
                    ctx,
                    t.GlossaryRequestArgs(metadata_fields=fields, tavily_preferences=preferences),
                )
            ).to_json_value()

        self.assertNotIn("description", request)
        self.assertNotIn("tags", request)
        self.assertEqual(request["tavily_domain_preferences"]["global_domains"], ["wikipedia.org"])
        self.assertEqual(request["tavily_domain_preferences"]["topics"][0]["domains"], ["bgm.tv"])
        self.assertIn("transcript_excerpt", request)

    def test_tavily_query_translation_prompt_prefers_active_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")

            prompt = t.tavily_query_translate_system_prompt(ctx)

        self.assertIn("Translate aggressively", prompt)
        self.assertIn("Translate concepts, claims, descriptive phrases, and genre/topic terms", prompt)
        self.assertIn("Preserve only", prompt)
        self.assertIn("Do not return a query that is merely the source query", prompt)
        self.assertIn("add target-language context", prompt)
        self.assertIn("Do not translate as subtitle prose", prompt)
        self.assertIn("Localize the search intent", prompt)
        self.assertIn("If the input includes topic_hints, return topic_hints", prompt)
        self.assertIn('"queries" is required', prompt)
        self.assertIn('"topic_hints" is required when the input contains topic_hints', prompt)
        self.assertIn("same number of queries in the same order", prompt)

    def test_tavily_query_prompt_requires_compact_diverse_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")

            prompt = t.tavily_query_system_prompt(ctx)

        self.assertIn("compact keyword queries", prompt)
        self.assertIn("Do not copy full transcript sentences", prompt)
        self.assertIn("2 to 6 important words or named entities", prompt)
        self.assertIn("one distinct search angle", prompt)
        self.assertIn("Do not create near-duplicates", prompt)
        self.assertIn("BAD", prompt)
        self.assertIn("full spoken sentence copied from transcript", prompt)

    def test_tavily_query_prompt_corrects_likely_asr_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")

            prompt = t.tavily_query_system_prompt(ctx)

        self.assertIn("WhisperX ASR", prompt)
        self.assertIn("correct likely ASR errors", prompt)
        self.assertIn("Do not preserve a suspicious ASR token", prompt)
        self.assertIn("metadata, neighboring context, and domain knowledge", prompt)
        self.assertIn("uncertain correction", prompt)

    def test_tavily_domain_preferences_merge_example_and_local_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "tavily_domains.example.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "global_domains": ["wikipedia.org", "baike.baidu.com"],
                        "topics": [
                            {
                                "name": "anime",
                                "keywords": ["anime", "动画"],
                                "domains": ["bgm.tv"],
                            }
                        ],
                    },
                    f,
                )
            with open(os.path.join(tmp, "tavily_domains.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "global": ["wikidata.org", "wikipedia.org"],
                        "topics": {
                            "anime": {
                                "keywords": ["番剧"],
                                "domains": ["bangumi.tv"],
                            },
                            "philosophy": {
                                "keywords": ["stoicism"],
                                "domains": ["plato.stanford.edu"],
                            },
                        },
                    },
                    f,
                )

            prefs = t.load_tavily_domain_preferences(tmp)

        self.assertEqual(prefs.global_domains, ("wikipedia.org", "baike.baidu.com", "wikidata.org"))
        anime = next(topic for topic in prefs.topics if topic.name == "anime")
        self.assertEqual(anime.keywords, ("anime", "动画", "番剧"))
        self.assertEqual(anime.domains, ("bgm.tv", "bangumi.tv"))
        self.assertTrue(any(topic.name == "philosophy" for topic in prefs.topics))

    def test_select_tavily_preferred_domains_matches_video_topic(self):
        prefs = t.TavilyDomainPreferences.from_json_value(
            {
                "global_domains": ["wikipedia.org", "baike.baidu.com"],
                "topics": [
                    {
                        "name": "anime",
                        "keywords": ["anime", "动画"],
                        "domains": ["bgm.tv", "bangumi.tv"],
                    },
                    {
                        "name": "game",
                        "keywords": ["game"],
                        "domains": ["wiki.gg"],
                    },
                ],
            }
        )
        fields = {
            "title": "Hunting for the Anime Genre That Doesn't Exist",
            "uploader": "",
            "description": "",
            "tags": ["video", "manga"],
        }

        domains = t.select_tavily_preferred_domains("lost anime genre", fields, prefs)

        self.assertEqual(domains, ["wikipedia.org", "baike.baidu.com", "bgm.tv", "bangumi.tv"])

    def test_select_tavily_preferred_domains_uses_query_agent_topic_hints(self):
        prefs = t.TavilyDomainPreferences.from_json_value(
            {
                "global_domains": ["wikipedia.org"],
                "topics": [
                    {
                        "name": "anime",
                        "keywords": ["anime", "manga", "动画"],
                        "domains": ["bgm.tv", "bangumi.tv"],
                    }
                ],
            }
        )
        fields = {
            "title": "A misleading abstract video title",
            "uploader": "",
            "description": "",
            "tags": [],
        }

        domains = t.select_tavily_preferred_domains(
            "genre taxonomy",
            fields,
            prefs,
            topic_hints=["manga criticism", "lost anime genre"],
        )

        self.assertEqual(domains, ["wikipedia.org", "bgm.tv", "bangumi.tv"])

    def test_build_tavily_search_plan_extracts_topic_hints_from_query_agent(self):
        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            if "TAVILY QUERY TRANSLATION JSON PROTOCOL" in system_prompt:
                return {"queries": ["标题", "概念"], "topic_hints": ["动漫"]}
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                return {
                    "queries": ["specific source concept"],
                    "topic_hints": ["anime", "manga criticism"],
                }
            raise AssertionError(system_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                plan = t.build_tavily_search_plan(transcript, ctx, FakeProviderLLM(), quiet=True, max_queries=2)

        self.assertEqual(plan.queries, ["Original Title", "标题", "specific source concept", "概念"])
        self.assertEqual(plan.topic_hints, ["anime", "manga criticism", "动漫"])

    def test_translate_tavily_query_output_prints_raw_response(self):
        raw_response = '{"queries": ["目标查询"], "topic_hints": ["目标题材"]}'

        class FakeChatSession:
            def __init__(self, llm, system_prompt, temperature=0.3, disable_response_format=False):
                pass

            def ask(self, content):
                return raw_response

        stderr = io.StringIO()
        with patch.object(t, "ChatSession", FakeChatSession), patch("sys.stderr", stderr):
            output = t.translate_tavily_query_output(["source query"], self.ctx, FakeProviderLLM(), quiet=False)

        self.assertEqual(output.queries, ["目标查询"])
        self.assertIn("translate_tavily_query_output raw response:", stderr.getvalue())
        self.assertIn(raw_response, stderr.getvalue())

    def test_build_tavily_search_plan_passes_function_names_as_raw_labels(self):
        raw_labels = []

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            raw_labels.append(raw_label)
            if "TAVILY QUERY TRANSLATION JSON PROTOCOL" in system_prompt:
                return {"queries": ["目标查询"]}
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                return {"queries": ["source query"]}
            raise AssertionError(system_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                t.build_tavily_search_plan(transcript, ctx, FakeProviderLLM(), quiet=False, max_queries=2)

        self.assertEqual(raw_labels, ["build_tavily_search_plan", "translate_tavily_query_output"])

    def test_build_glossary_uses_query_agent_topic_hints_for_domain_selection(self):
        preferred_domains_by_query = {}

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            if "TAVILY QUERY TRANSLATION JSON PROTOCOL" in system_prompt:
                return {"queries": []}
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                return {
                    "queries": ["genre taxonomy"],
                    "topic_hints": ["anime"],
                }
            return {"markdown": "# 术语知识库"}

        def fake_tavily_search(query, api_key, max_results=5, preferred_domains=None):
            preferred_domains_by_query[query] = preferred_domains or []
            return [{"url": f"https://example.com/{query}", "content": f"result for {query}"}]

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Misleading Abstract Title"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once), patch.object(
                t, "load_tavily_domain_preferences",
                return_value=t.TavilyDomainPreferences.from_json_value(
                    {
                        "global_domains": ["wikipedia.org"],
                        "topics": [
                            {
                                "name": "anime",
                                "keywords": ["anime"],
                                "domains": ["bgm.tv"],
                            }
                        ],
                    }
                ),
            ), patch.object(t, "tavily_search", side_effect=fake_tavily_search):
                t.build_glossary(
                    transcript,
                    ctx,
                    FakeProviderLLM(),
                    t.GlossaryBuildOptions(tavily_key="tk", quiet=True),
                )

        self.assertIn("bgm.tv", preferred_domains_by_query["genre taxonomy"])

    def test_tavily_search_prefers_domains_then_falls_back_to_general(self):
        calls = []

        class FakeClient:
            def __init__(self, api_key):
                self.api_key = api_key

            def search(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("include_domains"):
                    return {
                        "results": [
                            {"url": "https://zh.wikipedia.org/wiki/Topic", "content": "wiki"},
                        ]
                    }
                return {
                    "results": [
                        {"url": "https://example.com/topic", "content": "general"},
                        {"url": "https://zh.wikipedia.org/wiki/Topic", "content": "duplicate"},
                    ]
                }

        with patch.object(t, "TavilyClient", FakeClient):
            results = t.tavily_search(
                "topic",
                "tk",
                max_results=3,
                preferred_domains=["wikipedia.org", "baike.baidu.com"],
            )

        self.assertEqual([call.get("include_domains") for call in calls], [["wikipedia.org", "baike.baidu.com"], None])
        self.assertEqual([item["url"] for item in results], ["https://zh.wikipedia.org/wiki/Topic", "https://example.com/topic"])

    def test_tavily_search_skips_general_when_preferred_results_are_enough(self):
        calls = []

        class FakeClient:
            def __init__(self, api_key):
                self.api_key = api_key

            def search(self, **kwargs):
                calls.append(kwargs)
                return {
                    "results": [
                        {"url": "https://baike.baidu.com/item/one", "content": "one"},
                        {"url": "https://zh.wikipedia.org/wiki/Two", "content": "two"},
                    ]
                }

        with patch.object(t, "TavilyClient", FakeClient):
            results = t.tavily_search("topic", "tk", max_results=2, preferred_domains=["wikipedia.org", "baike.baidu.com"])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["include_domains"], ["wikipedia.org", "baike.baidu.com"])
        self.assertEqual(len(results), 2)

    def test_build_glossary_uses_agent_queries_for_tavily_search(self):
        class FakeRetriever:
            def __init__(self):
                self.calls = []

            def retrieve_texts(self, texts, top_k=None):
                self.texts = texts
                self.top_k = top_k
                self.calls.append((texts, top_k))
                return [[{"id": "transcript:1", "text": "[1] representative transcript context"}]]

        queries = []
        prompts = []
        query_request = {}

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            prompts.append(system_prompt)
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                data = request.to_json_value()
                query_request.update(data)
                return {"queries": ["counterfeit version altar of convenience", "discipline motivation therapist"]}
            return {"markdown": "# 术语知识库"}

        def fake_tavily_search(query, api_key, max_results=5, preferred_domains=None):
            queries.append(query)
            return [{"url": f"https://example.com/{len(queries)}", "content": f"result for {query}"}]

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title", "uploader": "Original Channel"}, f)
            with open(ctx.tags, "w", encoding="utf-8") as f:
                f.write("['generic tag']")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "We sacrifice ourselves on the altar of convenience.")],
            )

            with patch.object(t, "build_glossary_with_tools", side_effect=RuntimeError("tool unavailable")), patch.object(
                t, "llm_json_once", side_effect=fake_llm_json_once
            ), patch.object(t, "tavily_search", side_effect=fake_tavily_search):
                retriever = FakeRetriever()
                t.build_glossary(
                    transcript,
                    ctx,
                    FakeProviderLLM(),
                    t.GlossaryBuildOptions(
                        tavily_key="tk",
                        quiet=True,
                        retriever=retriever,
                    ),
                )

        self.assertEqual(
            queries,
            [
                "Original Title",
                "Original Title Original Channel",
                "counterfeit version altar of convenience",
                "discipline motivation therapist",
            ],
        )
        self.assertEqual(query_request["retrieved_transcript_context"], [{"id": "transcript:1", "text": "[1] representative transcript context"}])
        self.assertNotIn("transcript_excerpt", query_request)
        self.assertEqual(retriever.calls[0][1], 8)
        self.assertTrue(any("TAVILY SEARCH QUERY JSON PROTOCOL" in prompt for prompt in prompts))
        self.assertTrue(any("MANDATORY GLOSSARY JSON PROTOCOL" in prompt for prompt in prompts))

    def test_tavily_query_agent_failure_still_returns_metadata_fallbacks(self):
        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            raise RuntimeError("query failed")

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title", "uploader": "Original Channel"}, f)
            with open(ctx.tags, "w", encoding="utf-8") as f:
                f.write("['video', 'AI Ethics']")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source text")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                queries = t.build_tavily_search_plan(transcript, ctx, FakeProviderLLM(), quiet=True).queries

        self.assertEqual(
            queries,
            [
                "Original Title",
                "Original Title Original Channel",
                "Original Title AI Ethics",
            ],
        )

    def test_tavily_fallback_queries_prepend_title_author_and_substantive_tags(self):
        fields = {
            "title": "Original Title",
            "uploader": "Original Channel",
            "tags": ["video", "AI Ethics", "podcast", "Stoicism"],
        }

        queries = t.merge_tavily_queries_with_fallbacks(["agent query", "Original Title"], fields, max_queries=8)

        self.assertEqual(
            queries,
            [
                "Original Title",
                "Original Title Original Channel",
                "Original Title AI Ethics",
                "Original Title Stoicism",
                "agent query",
            ],
        )

    def test_tavily_query_limit_preserves_metadata_queries_first(self):
        fields = {
            "title": "Original Title",
            "uploader": "Original Channel",
            "tags": ["AI Ethics"],
        }

        queries = t.merge_tavily_queries_with_fallbacks(
            ["agent one", "agent two", "agent three"],
            fields,
            max_queries=2,
        )

        self.assertEqual(queries, ["Original Title", "Original Title Original Channel"])

    def test_tavily_queries_dedupe_with_whitespace_case_and_terminal_punctuation(self):
        fields = {
            "title": "Original Title",
            "uploader": "",
            "tags": ["AI Ethics"],
        }

        queries = t.merge_tavily_queries_with_fallbacks(
            ["  original   title ai ethics.  ", "Agent Useful Query"],
            fields,
            max_queries=8,
        )

        self.assertEqual(
            queries,
            [
                "Original Title",
                "Original Title AI Ethics",
                "Agent Useful Query",
            ],
        )

    def test_tavily_queries_dedupe_typographic_quotes_and_internal_punctuation(self):
        fields = {
            "title": "Anime Genre That Doesn’t Exist",
            "uploader": "",
            "tags": [],
        }

        queries = t.merge_tavily_queries_with_fallbacks(
            [
                "Anime Genre That Doesn't Exist",
                "Anime Genre That Doesnt Exist",
                "Anime--Genre That Doesn’t Exist",
                "distinctive source concept",
            ],
            fields,
            max_queries=8,
        )

        self.assertEqual(
            queries,
            [
                "Anime Genre That Doesn’t Exist",
                "distinctive source concept",
            ],
        )

    def test_tavily_source_target_merge_dedupes_before_language_limit(self):
        queries = t.merge_source_and_target_tavily_queries(
            ["alpha", "beta", "gamma"],
            ["alpha", "beta.", "目标甲", "目标乙", "目标丙"],
            max_queries_per_language=3,
        )

        self.assertEqual(queries, ["alpha", "目标甲", "beta", "目标乙", "gamma", "目标丙"])

    def test_build_tavily_search_plan_searches_source_and_target_language_queries(self):
        prompts = []

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            prompts.append(system_prompt)
            if "TAVILY QUERY TRANSLATION JSON PROTOCOL" in system_prompt:
                self.assertEqual(
                    request.to_json_value()["queries"],
                    ["Original Title", "Original Title Original Channel", "source concept"],
                )
                return {"queries": ["原标题", "原标题 原频道", "目标概念"]}
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                return {"queries": ["source concept"]}
            raise AssertionError(system_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title", "uploader": "Original Channel"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                queries = t.build_tavily_search_plan(transcript, ctx, FakeProviderLLM(), quiet=True, max_queries=3).queries

        self.assertEqual(
            queries,
            [
                "Original Title",
                "原标题",
                "Original Title Original Channel",
                "原标题 原频道",
                "source concept",
                "目标概念",
            ],
        )
        self.assertTrue(any("TAVILY QUERY TRANSLATION JSON PROTOCOL" in prompt for prompt in prompts))

    def test_build_tavily_search_plan_falls_back_to_source_queries_when_translation_fails(self):
        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
            if "TAVILY QUERY TRANSLATION JSON PROTOCOL" in system_prompt:
                raise RuntimeError("translation failed")
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                return {"queries": ["source concept"]}
            raise AssertionError(system_prompt)

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump({"title": "Original Title"}, f)
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source topic")],
            )

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                queries = t.build_tavily_search_plan(transcript, ctx, FakeProviderLLM(), quiet=True, max_queries=4).queries

        self.assertEqual(queries, ["Original Title", "source concept"])

    def test_glossary_prompt_context_skips_full_text_when_retriever_is_available(self):
        class FalseyRetriever:
            def __bool__(self):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            glossary = os.path.join(tmp, "glossary.md")
            with open(glossary, "w", encoding="utf-8") as f:
                f.write("# 术语知识库\n\n- discipline: 纪律")

            self.assertEqual(t.load_glossary_prompt_context(glossary, retriever=FalseyRetriever()), "")
            fallback = t.load_glossary_prompt_context(glossary, retriever=None)

        self.assertIn("# 术语知识库", fallback)

    def test_translate_segments_omits_retrieved_context_without_retriever(self):
        captured = {}

        def fake_llm_numbered_batch(request, session, quiet, retries=3):
            captured["request"] = request.to_json_value()
            captured["system_prompt"] = session.system_prompt
            return [{"id": 1, "zh": "译文"}]

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[t.TranscriptSegment(1, 0.0, 1.0, "source text")],
        )
        with patch.object(t, "llm_numbered_batch", side_effect=fake_llm_numbered_batch):
            t.translate_segments(transcript, self.ctx, FakeBatchLLM(10), "system", quiet=True, retriever=None)

        self.assertNotIn("retrieved_context", captured["request"]["items"][0])
        self.assertNotIn("RETRIEVED CONTEXT:", captured["system_prompt"])

    def test_translate_segments_adds_retrieved_context(self):
        class FakeRetriever:
            def __bool__(self):
                return False

            def retrieve_texts(self, texts, top_k=None):
                self.texts = texts
                return [[{"id": "transcript:1", "text": "translation memory"}]]

        captured = {}

        def fake_llm_numbered_batch(request, session, quiet, retries=3):
            captured.update(request.to_json_value())
            return [{"id": 1, "zh": "译文"}]

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[t.TranscriptSegment(1, 0.0, 1.0, "source text")],
        )
        retriever = FakeRetriever()
        with patch.object(t, "llm_numbered_batch", side_effect=fake_llm_numbered_batch):
            t.translate_segments(transcript, self.ctx, FakeBatchLLM(10), "system", quiet=True, retriever=retriever)

        self.assertEqual(captured["items"][0]["retrieved_context"], [{"id": "transcript:1", "text": "translation memory"}])
        self.assertEqual(retriever.texts, ["source text"])

    def test_proofread_split_events_adds_retrieved_context(self):
        class FakeRetriever:
            def retrieve_texts(self, texts, top_k=None):
                self.texts = texts
                self.top_k = top_k
                return [[{"id": "transcript:1", "text": "proofread memory"}]]

        captured = {}

        def fake_llm_numbered_batch(request, session, quiet, retries=3, raise_on_failure=False):
            captured.update(request.to_json_value())
            return [{"id": 1, "en": "source", "zh": "译文"}]

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(
                    1,
                    0.0,
                    1.0,
                    "source",
                    split_events=[t.SplitEvent(0.0, 1.0, "source", "译文")],
                )
            ],
        )
        retriever = FakeRetriever()
        with patch.object(t, "llm_numbered_batch", side_effect=fake_llm_numbered_batch):
            t.proofread_split_events(transcript, self.ctx, FakeBatchLLM(10), "system", quiet=True, retriever=retriever)

        self.assertEqual(captured["items"][0]["retrieved_context"], [{"id": "transcript:1", "text": "proofread memory"}])
        self.assertEqual(retriever.texts, ["source\n译文"])
        self.assertEqual(retriever.top_k, 1)

    def test_proofread_split_events_respects_small_batch_without_token_limit(self):
        calls = []

        def fake_llm_numbered_batch(request, session, quiet, retries=3, raise_on_failure=False):
            calls.append(len(request.items))
            return [
                {"id": item.id, "en": item.fields["en"], "zh": item.fields["zh"]}
                for item in request.items
            ]

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(
                    1,
                    0.0,
                    3.0,
                    "source",
                    split_events=[
                        t.SplitEvent(0.0, 1.0, "source one", "译文一"),
                        t.SplitEvent(1.0, 2.0, "source two", "译文二"),
                        t.SplitEvent(2.0, 3.0, "source three", "译文三"),
                    ],
                )
            ],
        )

        with patch.object(t, "llm_numbered_batch", side_effect=fake_llm_numbered_batch):
            t.proofread_split_events(transcript, self.ctx, FakeBatchLLM(2), "system", quiet=True)

        self.assertEqual(calls, [2, 1])

    def test_proofread_split_events_splits_batch_on_context_length_error(self):
        calls = []

        def fake_llm_numbered_batch(request, session, quiet, retries=3, raise_on_failure=False):
            calls.append(len(request.items))
            if len(request.items) > 1:
                raise RuntimeError("maximum context length exceeded")
            return [
                {"id": item.id, "en": item.fields["en"] + " fixed", "zh": item.fields["zh"] + " fixed"}
                for item in request.items
            ]

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(
                    1,
                    0.0,
                    2.0,
                    "source",
                    split_events=[
                        t.SplitEvent(0.0, 1.0, "source one", "译文一"),
                        t.SplitEvent(1.0, 2.0, "source two", "译文二"),
                    ],
                )
            ],
        )

        with patch.object(t, "llm_numbered_batch", side_effect=fake_llm_numbered_batch):
            changed = t.proofread_split_events(transcript, self.ctx, FakeBatchLLM(2), "system", quiet=True)

        self.assertTrue(changed)
        self.assertEqual(calls, [2, 1, 1])
        self.assertEqual(transcript.segments[0].split_events[0].en, "source one fixed")
        self.assertEqual(transcript.segments[0].split_events[1].zh, "译文二 fixed")

    def test_proofread_split_events_drops_retrieved_context_when_single_item_is_too_large(self):
        class FakeRetriever:
            def retrieve_texts(self, texts, top_k=None):
                return [[{"id": "transcript:1", "text": "x" * 1000}]]

        saw_context = []

        def fake_llm_numbered_batch(request, session, quiet, retries=3, raise_on_failure=False):
            has_context = bool(request.items[0].fields.get("retrieved_context"))
            saw_context.append(has_context)
            if has_context:
                raise RuntimeError("maximum context length exceeded")
            item = request.items[0]
            return [{"id": item.id, "en": item.fields["en"] + " fixed", "zh": item.fields["zh"]}]

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(
                    1,
                    0.0,
                    1.0,
                    "source",
                    split_events=[t.SplitEvent(0.0, 1.0, "source one", "译文一")],
                )
            ],
        )

        with patch.object(t, "llm_numbered_batch", side_effect=fake_llm_numbered_batch):
            changed = t.proofread_split_events(
                transcript,
                self.ctx,
                FakeBatchLLM(1),
                "system",
                quiet=True,
                retriever=FakeRetriever(),
            )

        self.assertTrue(changed)
        self.assertEqual(saw_context, [True, False])
        self.assertEqual(transcript.segments[0].split_events[0].en, "source one fixed")

    def test_chat_session_passes_provider_response_format(self):
        calls = []
        llm = FakeChatLLM(
            calls=calls,
            cfg={"response_format": {"type": "json_object"}},
            responses=[FakeSDKResponse(FakeSDKMessage(content='{"markdown": "ok"}'))],
        )

        t.ChatSession(llm, "system").ask("{}")

        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(set(calls[0]), {"model", "messages", "temperature", "response_format"})

    def test_chat_session_merges_provider_request_kwargs(self):
        calls = []
        llm = FakeChatLLM(
            calls=calls,
            cfg={
                "request_kwargs": {
                    "extra_body": {"google": {"tools": [{"google_search": {}}]}},
                    "seed": 7,
                }
            },
            responses=[FakeSDKResponse(FakeSDKMessage(content='{"markdown": "ok"}'))],
        )

        t.ChatSession(llm, "system").ask("{}")

        self.assertIn("extra_body", calls[0])
        self.assertEqual(
            calls[0]["extra_body"],
            {"google": {"tools": [{"google_search": {}}]}},
        )
        self.assertIn("seed", calls[0])
        self.assertEqual(calls[0]["seed"], 7)
        self.assertEqual(calls[0]["model"], "fake-model")
        self.assertEqual(calls[0]["temperature"], 0.3)

    def test_chat_session_disable_response_format_wins_after_extra_kwargs(self):
        calls = []
        llm = FakeChatLLM(
            calls=calls,
            cfg={
                "response_format": {"type": "json_object"},
                "request_kwargs": {"response_format": {"type": "json_object"}},
            },
        )

        session = t.ChatSession(llm, "system", disable_response_format=True)
        session.create(response_format={"type": "json_object"})

        self.assertNotIn("response_format", calls[0])

    def test_provider_example_uses_request_kwargs_for_sdk_options(self):
        with open("providers.example.json", "r", encoding="utf-8") as f:
            providers = json.load(f)

        deepseek = providers["deepseek"]
        self.assertNotIn("response_format", deepseek)
        self.assertEqual(
            deepseek["request_kwargs"]["response_format"],
            {"type": "json_object"},
        )

        gemini = providers["gemini"]
        self.assertEqual(
            gemini["request_kwargs"]["extra_body"]["extra_body"]["google"]["tools"],
            [{"google_search": {}}],
        )

    def test_chat_session_empty_content_error_includes_provider_details(self):
        class FakeUsageDetails:
            reasoning_tokens = 128

        class FakeUsage:
            prompt_tokens = 100
            completion_tokens = 200
            total_tokens = 300
            completion_tokens_details = FakeUsageDetails()

        llm = FakeChatLLM(
            responses=[
                FakeSDKResponse(
                    FakeSDKMessage(
                        content="",
                        reasoning_content="model thought but produced no content",
                        refusal="blocked by provider",
                    ),
                    finish_reason="length",
                    usage=FakeUsage(),
                )
            ]
        )

        try:
            t.ChatSession(llm, "system").ask("{}")
        except RuntimeError as e:
            message = str(e)
        else:
            self.fail("expected empty content RuntimeError")

        self.assertIn("finish_reason=length", message)
        self.assertIn("refusal=blocked by provider", message)
        self.assertIn("reasoning_chars=37", message)
        self.assertIn("prompt_tokens=100", message)
        self.assertIn("completion_tokens=200", message)
        self.assertIn("total_tokens=300", message)
        self.assertIn("reasoning_tokens=128", message)

    def test_load_providers_merges_local_config_with_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            providers_path = os.path.join(tmp, "providers.json")
            with open(providers_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "custom": {
                            "url": "https://example.test/v1",
                            "default_model": "custom-model",
                            "env_key": "CUSTOM_API_KEY",
                            "auth_header": "Bearer {api_key}",
                            "extra_headers": {},
                        }
                    },
                    f,
                )

            old_cache = t._providers_cache
            try:
                t._providers_cache = None
                with patch.object(t.os.path, "dirname", return_value=tmp):
                    providers = t.load_providers()
            finally:
                t._providers_cache = old_cache

            self.assertIn("openai", providers)
            self.assertIn("llama", providers)
            self.assertIn("custom", providers)

    def test_glossary_llm_from_env_uses_dedicated_provider_and_model(self):
        translate_llm = t.LLMConfig(provider="deepseek", model="deepseek-chat", api_key="shared-key", batch_size=17)

        glossary_llm = t.glossary_llm_from_env(
            {
                "GLOSSARY_PROVIDER": "openrouter",
                "GLOSSARY_MODEL": "anthropic/claude-opus-4.1",
            },
            translate_llm,
        )

        self.assertEqual(glossary_llm.provider, "openrouter")
        self.assertEqual(glossary_llm.model, "anthropic/claude-opus-4.1")
        self.assertIsNone(glossary_llm.api_key)
        self.assertEqual(glossary_llm.batch_size, 17)

    def test_glossary_llm_from_env_falls_back_to_translate_provider(self):
        translate_llm = t.LLMConfig(provider="deepseek", model="deepseek-v4-pro", api_key="shared-key")

        glossary_llm = t.glossary_llm_from_env({}, translate_llm)

        self.assertEqual(glossary_llm.provider, "deepseek")
        self.assertEqual(glossary_llm.model, "deepseek-v4-pro")
        self.assertEqual(glossary_llm.api_key, "shared-key")

    def test_translate_llm_from_env_does_not_carry_proofread_config(self):
        llm = t.translate_llm_from_env(
            {
                "TRANSLATE_PROVIDER": "deepseek",
                "TRANSLATE_MODEL": "deepseek-chat",
                "PROOFREAD_PROVIDER": "openrouter",
                "PROOFREAD_MODEL": "anthropic/claude-sonnet-4-6",
                "PROOFREAD_BATCH_SIZE": "3",
            },
            batch_size=12,
        )

        self.assertEqual(llm.provider, "deepseek")
        self.assertEqual(llm.model, "deepseek-chat")
        self.assertEqual(llm.batch_size, 12)
        self.assertFalse(hasattr(llm, "proofread_provider"))
        self.assertFalse(hasattr(llm, "proofread_model"))

    def test_proofread_llm_from_env_returns_dedicated_llm_config(self):
        translate_llm = t.LLMConfig(provider="deepseek", model="deepseek-chat", api_key="shared-key", batch_size=12)

        proofread_llm = t.proofread_llm_from_env(
            {
                "PROOFREAD_PROVIDER": "openrouter",
                "PROOFREAD_MODEL": "anthropic/claude-sonnet-4-6",
                "PROOFREAD_BATCH_SIZE": "3",
            },
            translate_llm,
            batch_size=12,
        )

        self.assertEqual(proofread_llm.provider, "openrouter")
        self.assertEqual(proofread_llm.model, "anthropic/claude-sonnet-4-6")
        self.assertIsNone(proofread_llm.api_key)
        self.assertEqual(proofread_llm.batch_size, 3)

    def test_proofread_llm_from_env_reuses_translate_provider_when_unset(self):
        translate_llm = t.LLMConfig(provider="deepseek", model="deepseek-chat", api_key="shared-key", batch_size=12)

        proofread_llm = t.proofread_llm_from_env({}, translate_llm, batch_size=12)

        self.assertEqual(proofread_llm.provider, "deepseek")
        self.assertEqual(proofread_llm.model, "deepseek-chat")
        self.assertEqual(proofread_llm.api_key, "shared-key")
        self.assertEqual(proofread_llm.batch_size, 6)

    def test_only_glossary_does_not_require_translate_provider(self):
        class Args:
            only_glossary = True
            skip_knowledge = False

        env = {
            "GLOSSARY_PROVIDER": "deepseek",
            "TRANSLATE_PROVIDER": "",
        }

        self.assertFalse(t.needs_translate_llm(Args))
        self.assertEqual(t.required_glossary_provider(env), "deepseek")

    def test_embedding_config_from_env_uses_project_chroma_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")
            cfg = t.EmbeddingConfig.from_env(
                {
                    "EMBEDDING_ENABLED": "1",
                    "EMBEDDING_PROVIDER": "openai",
                    "EMBEDDING_MODEL": "text-embedding-3-small",
                    "EMBEDDING_TOP_K": "8",
                    "EMBEDDING_CHUNK_CHARS": "900",
                },
                ctx,
            )

            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.provider, "openai")
            self.assertEqual(cfg.model, "text-embedding-3-small")
            self.assertEqual(cfg.store, "chroma")
            self.assertEqual(cfg.top_k, 8)
            self.assertEqual(cfg.chunk_chars, 900)
            self.assertEqual(cfg.chroma_dir, os.path.join(tmp, "chroma_db"))

    def test_embedding_config_ignores_legacy_pipeline_use_embedding(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "video.json"), "", "en", "zh")
            cfg = t.EmbeddingConfig.from_env({"PIPELINE_USE_EMBEDDING": "1"}, ctx)

            self.assertFalse(cfg.enabled)

    def test_embedding_chunk_converts_to_langchain_document(self):
        chunk = t.EmbeddingChunk(
            "a",
            "transcript",
            "discipline and motivation",
            1.0,
            2.0,
            {"segment": 1},
            context_text="[1 00:00:01.000-00:00:02.000] discipline and motivation",
        )

        doc = chunk.to_document()

        self.assertEqual(doc.page_content, "discipline and motivation")
        self.assertEqual(
            doc.metadata,
            {
                "id": "a",
                "source": "transcript",
                "start": 1.0,
                "end": 2.0,
                "segment": 1,
                "context_text": "[1 00:00:01.000-00:00:02.000] discipline and motivation",
            },
        )

    def test_langchain_docs_convert_to_retrieved_context(self):
        doc = t.Document(
            page_content="discipline and motivation",
            metadata={
                "id": "transcript:1",
                "source": "transcript",
                "start": 1.0,
                "end": 2.0,
                "context_text": "[1 00:00:01.000-00:00:02.000] discipline and motivation",
            },
        )

        context = t.documents_to_retrieved_context([doc])

        self.assertEqual(
            context,
            [
                {
                    "id": "transcript:1",
                    "source": "transcript",
                    "text": "[1 00:00:01.000-00:00:02.000] discipline and motivation",
                    "start": 1.0,
                    "end": 2.0,
                }
            ],
        )

    def test_embedding_function_uses_provider_config_and_disables_tiktoken(self):
        cfg = t.EmbeddingConfig(provider="llama", model="qwen3-embedding")
        env = {"OLLAMA_API_KEY": "not-needed"}

        with patch.object(
            t,
            "load_providers",
            return_value={
                "llama": {
                    "url": "http://localhost:8080/v1",
                    "env_key": "OLLAMA_API_KEY",
                    "extra_headers": {"X-Test": "1"},
                }
            },
        ), patch.object(t, "OpenAIEmbeddings") as fake_embeddings:
            t.embedding_function(cfg, env)

        fake_embeddings.assert_called_once_with(
            base_url="http://localhost:8080/v1",
            api_key="not-needed",
            model="qwen3-embedding",
            default_headers={"X-Test": "1"},
            check_embedding_ctx_length=False,
        )

    def test_embedding_stage_enabled_skips_only_beautify(self):
        self.assertFalse(t.embedding_enabled_for_stage(True, False))
        self.assertTrue(t.embedding_enabled_for_stage(False, True))
        self.assertTrue(t.embedding_enabled_for_stage(False, False))

    def test_build_embedding_chunks_groups_transcript_segments(self):
        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(1, 0.0, 1.0, "alpha beta"),
                t.TranscriptSegment(2, 1.0, 2.0, "gamma delta"),
                t.TranscriptSegment(3, 2.0, 3.0, "epsilon"),
            ],
        )

        chunks = t.build_embedding_chunks(transcript, chunk_chars=40)

        self.assertEqual([chunk.chunk_id for chunk in chunks], ["transcript:1-2", "transcript:2-3"])
        self.assertEqual(chunks[0].source, "transcript")
        self.assertEqual(chunks[0].text, "[1] alpha beta\n[2] gamma delta")
        self.assertEqual(
            chunks[0].context_text,
            "[1 00:00:00.000-00:00:01.000] alpha beta\n[2 00:00:01.000-00:00:02.000] gamma delta",
        )
        self.assertEqual(chunks[0].metadata["segment_ids"], [1, 2])
        self.assertEqual(chunks[0].start, 0.0)
        self.assertEqual(chunks[0].end, 2.0)
        self.assertEqual(chunks[1].metadata["segment_ids"], [2, 3])

    def test_build_embedding_chunks_uses_time_aware_overlap(self):
        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(1, 0.0, 10.0, "alpha beta"),
                t.TranscriptSegment(2, 10.0, 20.0, "gamma delta"),
                t.TranscriptSegment(3, 20.0, 30.0, "epsilon zeta"),
                t.TranscriptSegment(4, 30.0, 40.0, "eta theta"),
            ],
        )

        chunks = t.build_embedding_chunks(transcript, chunk_chars=52)

        self.assertEqual([chunk.metadata["segment_ids"] for chunk in chunks], [[1, 2, 3], [3, 4]])
        self.assertEqual([chunk.chunk_id for chunk in chunks], ["transcript:1-3", "transcript:3-4"])

    def test_build_embedding_chunks_splits_long_time_windows(self):
        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(1, 0.0, 30.0, "alpha"),
                t.TranscriptSegment(2, 30.0, 60.0, "beta"),
                t.TranscriptSegment(3, 60.0, 90.0, "gamma"),
            ],
        )

        chunks = t.build_embedding_chunks(transcript, chunk_chars=1000)

        self.assertEqual([chunk.metadata["segment_ids"] for chunk in chunks], [[1, 2], [2, 3]])

    def test_build_translation_memory_chunks_uses_split_events(self):
        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(
                    7,
                    1.0,
                    3.0,
                    "source sentence",
                    translation="目标句子",
                    split_events=[
                        t.SplitEvent(1.0, 2.0, "source part one", "目标一"),
                        t.SplitEvent(2.0, 3.0, "source part two", "目标二"),
                    ],
                )
            ],
        )

        chunks = t.build_translation_memory_chunks(transcript, self.ctx)

        self.assertEqual([chunk.chunk_id for chunk in chunks], ["translation_memory:7:1", "translation_memory:7:2"])
        self.assertEqual(chunks[0].source, "translation_memory")
        self.assertEqual(chunks[0].text, "[7.1]\nSOURCE(en): source part one\nTARGET(zh): 目标一")
        self.assertEqual(chunks[0].metadata["segment_id"], 7)
        self.assertEqual(chunks[0].metadata["event_index"], 1)
        self.assertEqual(chunks[0].metadata["source_lang"], "en")
        self.assertEqual(chunks[0].metadata["target_lang"], "zh")

    def test_build_glossary_chunks_reads_project_glossary(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as f:
                f.write("## 视频元信息\n\n原标题：Original Title\n\n## 核心术语\n\n- discipline：纪律")

            chunks = t.build_glossary_chunks(ctx, chunk_chars=200)

        self.assertEqual([chunk.chunk_id for chunk in chunks], ["glossary:1", "glossary:2"])
        self.assertEqual(chunks[0].source, "glossary")
        self.assertIn("原标题：Original Title", chunks[0].text)
        self.assertIn("- discipline：纪律", chunks[1].text)
        self.assertEqual(chunks[0].metadata["kind"], "project_glossary")

    def test_build_glossary_chunks_keeps_markdown_sections_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as f:
                f.write(
                    "## 视频元信息\n\n"
                    "原标题：Original Title\n\n"
                    "## 核心术语\n\n"
                    "- discipline：纪律\n"
                    "- agency：能动性\n\n"
                    "## 风格指南\n\n"
                    "保持自然口语。"
                )

            chunks = t.build_glossary_chunks(ctx, chunk_chars=200)

        self.assertEqual([chunk.metadata["heading"] for chunk in chunks], ["视频元信息", "核心术语", "风格指南"])
        self.assertEqual([chunk.chunk_id for chunk in chunks], ["glossary:1", "glossary:2", "glossary:3"])
        self.assertIn("- discipline：纪律", chunks[1].text)
        self.assertNotIn("## 风格指南", chunks[1].text)

    def test_build_embedding_index_uses_chroma_add_documents_without_manual_persist(self):
        class FakeStore:
            def __init__(self):
                self.documents = []
                self.ids = []

            def add_documents(self, documents, ids):
                self.documents = documents
                self.ids = ids

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha beta")],
        )
        cfg = t.EmbeddingConfig(enabled=True, chroma_dir="index")
        fake_store = FakeStore()

        with patch.object(t, "open_chroma_store", return_value=fake_store):
            result = t.build_embedding_index(transcript, cfg, {}, quiet=True)

        self.assertEqual(result, "index")
        self.assertEqual(fake_store.ids, ["transcript:1"])
        self.assertEqual(fake_store.documents[0].page_content, "[1] alpha beta")

    def test_build_embedding_index_adds_translation_memory_chunks(self):
        class FakeStore:
            def __init__(self):
                self.documents = []
                self.ids = []

            def add_documents(self, documents, ids):
                self.documents = documents
                self.ids = ids

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(
                    1,
                    0.0,
                    1.0,
                    "source",
                    translation="译文",
                    split_events=[t.SplitEvent(0.0, 1.0, "source", "译文")],
                )
            ],
        )
        cfg = t.EmbeddingConfig(enabled=True, chroma_dir="index")
        fake_store = FakeStore()

        with patch.object(t, "open_chroma_store", return_value=fake_store):
            t.build_embedding_index(transcript, cfg, {}, quiet=True, ctx=self.ctx)

        self.assertEqual(fake_store.ids, ["transcript:1", "translation_memory:1:1"])
        self.assertIn("SOURCE(en): source", fake_store.documents[1].page_content)
        self.assertIn("TARGET(zh): 译文", fake_store.documents[1].page_content)

    def test_build_embedding_index_adds_glossary_chunks(self):
        class FakeStore:
            def __init__(self):
                self.documents = []
                self.ids = []

            def add_documents(self, documents, ids):
                self.documents = documents
                self.ids = ids

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as f:
                f.write("## 视频元信息\n\n原标题：Original Title")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source")],
            )
            cfg = t.EmbeddingConfig(enabled=True, chroma_dir="index")
            fake_store = FakeStore()

            with patch.object(t, "open_chroma_store", return_value=fake_store):
                t.build_embedding_index(transcript, cfg, {}, quiet=True, ctx=ctx)

        self.assertEqual(fake_store.ids, ["transcript:1", "glossary:1"])
        self.assertIn("原标题：Original Title", fake_store.documents[1].page_content)

    def test_build_embedding_index_clears_project_chunks_before_rebuild(self):
        class FakeStore:
            def __init__(self):
                self.deleted = []
                self.ids = []

            def delete(self, ids):
                self.deleted.extend(ids)

            def add_documents(self, documents, ids):
                self.ids.extend(ids)

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[t.TranscriptSegment(1, 0.0, 1.0, "alpha beta")],
        )
        cfg = t.EmbeddingConfig(enabled=True, chroma_dir="index")
        fake_store = FakeStore()

        with patch.object(t, "open_chroma_store", return_value=fake_store):
            t.build_embedding_index(transcript, cfg, {}, quiet=True, existing_chunk_ids=["transcript:1", "transcript:2"])

        self.assertEqual(fake_store.deleted, ["transcript:1", "transcript:2"])
        self.assertEqual(fake_store.ids, ["transcript:1"])

    def test_refresh_embedding_retriever_rebuilds_with_existing_glossary(self):
        calls = []

        class FakeRetriever:
            def __init__(self, config, env):
                self.config = config
                self.env = env

        def fake_build_embedding_index(transcript, config, env, quiet=False, ctx=None):
            calls.append(os.path.isfile(ctx.glossary))
            return config.chroma_dir

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as f:
                f.write("## 视频元信息\n\n原标题：Original Title")
            transcript = t.Transcript(
                path=json_path,
                language="en",
                segments=[t.TranscriptSegment(1, 0.0, 1.0, "source")],
            )
            cfg = t.EmbeddingConfig(enabled=True, chroma_dir="index")

            with patch.object(t, "build_embedding_index", side_effect=fake_build_embedding_index), patch.object(
                t, "EmbeddingRetriever", FakeRetriever
            ):
                retriever = t.refresh_embedding_retriever(transcript, cfg, {}, quiet=True, ctx=ctx)

        self.assertEqual(calls, [True])
        self.assertIsInstance(retriever, FakeRetriever)

    def test_build_embedding_index_adds_documents_in_configured_batches(self):
        class FakeStore:
            def __init__(self):
                self.calls = []

            def add_documents(self, documents, ids):
                self.calls.append((documents, ids))

        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(1, 0.0, 1.0, "alpha"),
                t.TranscriptSegment(2, 1.0, 2.0, "beta"),
                t.TranscriptSegment(3, 2.0, 3.0, "gamma"),
            ],
        )
        cfg = t.EmbeddingConfig(enabled=True, chroma_dir="index", chunk_chars=1, batch_size=2)
        fake_store = FakeStore()

        with patch.object(t, "open_chroma_store", return_value=fake_store):
            t.build_embedding_index(transcript, cfg, {}, quiet=True)

        self.assertEqual([ids for _, ids in fake_store.calls], [["transcript:1", "transcript:1-2"], ["transcript:2-3"]])

    def test_response_keys_match_language_codes_only(self):
        fields = t.LanguageFields.from_ctx(self.ctx)

        self.assertEqual(fields.get_source({"en": "source text"}), "source text")
        self.assertEqual(fields.get_target({"zh": "target text"}), "target text")
        self.assertIsNone(fields.get_source({"source": "legacy"}))
        self.assertIsNone(fields.get_target({"target": "legacy"}))

    def test_parse_split_response_aligns_by_actual_ids(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 12, "en": ["source a", "source b"], "zh": ["target a", "target b"]},
                {"id": 44, "en": ["source c"], "zh": ["target c"]},
            ],
            [12, 44],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source[12], ["source a", "source b"])
        self.assertEqual(target[44], ["target c"])

    def test_parse_split_response_keeps_other_ids_when_one_item_has_part_count_mismatch(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 73, "en": ["source a"], "zh": ["target a"]},
                {"id": 74, "en": ["source b"], "zh": ["target b"]},
                {"id": 88, "en": ["source c", "source d"], "zh": ["target c"]},
            ],
            [73, 74, 88],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source[73], ["source a"])
        self.assertEqual(target[74], ["target b"])
        self.assertEqual(source[88], ["source c", "source d"])
        self.assertEqual(target[88], ["target c"])

    def test_parse_split_response_ignores_extra_context_items(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 34, "en": ["source a"], "zh": ["target a"]},
                {"id": 35, "en": ["source b"], "zh": ["target b"]},
                {"id": 33, "en": ["context before"], "zh": ["上下文前"]},
                {"id": 36, "en": ["context after"], "zh": ["上下文后"]},
            ],
            [34, 35],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source, {34: ["source a"], 35: ["source b"]})
        self.assertEqual(target, {34: ["target a"], 35: ["target b"]})

    def test_parse_split_response_keeps_present_ids_when_one_expected_id_is_missing(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 34, "en": ["source a"], "zh": ["target a"]},
                {"id": 36, "en": ["source c"], "zh": ["target c"]},
            ],
            [34, 35, 36],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source, {34: ["source a"], 36: ["source c"]})
        self.assertEqual(target, {34: ["target a"], 36: ["target c"]})

    def test_parse_split_response_keeps_other_ids_when_one_item_has_invalid_language_values(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 34, "en": ["source a"], "zh": ["target a"]},
                {"id": 35, "en": "not an array", "zh": ["target b"]},
                {"id": 36, "en": ["source c"], "zh": ["target c"]},
            ],
            [34, 35, 36],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source, {34: ["source a"], 36: ["source c"]})
        self.assertEqual(target, {34: ["target a"], 36: ["target c"]})

    def test_parse_proofread_response_aligns_by_actual_ids(self):
        pairs = t.parse_proofread_response(
            [{"id": 3, "en": "corrected source", "zh": "corrected target"}],
            [3],
            [("fallback source", "fallback target")],
            self.ctx,
        )

        self.assertEqual(pairs, [("corrected source", "corrected target")])

    def test_get_scene_changes_handles_missing_ffmpeg_stderr(self):
        class FakeCompletedProcess:
            stderr = None

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            with patch.object(t.subprocess, "run", return_value=FakeCompletedProcess()):
                self.assertEqual(t.get_scene_changes(video.name, 0.15, 0.1, quiet=True), [])

    def test_get_scene_changes_decodes_ffmpeg_stderr_bytes_with_replacement(self):
        class FakeCompletedProcess:
            stderr = b"bad-byte:\xa4\n[Parsed_showinfo] pts_time:1.25\n"

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            with patch.object(t.subprocess, "run", return_value=FakeCompletedProcess()):
                self.assertEqual(t.get_scene_changes(video.name, 0.15, 0.1, quiet=True), [1.25])

    def test_write_scene_change_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.json")
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            options = t.BeautifyOptions(scene_threshold=0.12, min_scene_interval_frames=2, fps=25.0)

            t.write_scene_change_sidecars(ctx, os.path.join(tmp, "video.webm"), options, [1.25, 2.5])

            with open(ctx.scenes_json, "r", encoding="utf-8") as f:
                scenes = json.load(f)
            with open(ctx.scenechange_txt, "r", encoding="utf-8") as f:
                scenechange = f.read()

            self.assertEqual(scenes["fps"], 25.0)
            self.assertEqual(scenes["threshold"], 0.12)
            self.assertEqual(scenes["min_interval_sec"], 0.08)
            self.assertEqual(
                scenes["scene_changes"],
                [
                    {"index": 1, "time": 1.25, "frame": 31, "timecode": "00:00:01.250"},
                    {"index": 2, "time": 2.5, "frame": 62, "timecode": "00:00:02.500"},
                ],
            )
            self.assertEqual(scenechange, "1.250000\n2.500000\n")

    def test_transcript_segment_round_trips_split_status(self):
        seg = t.TranscriptSegment.from_json(
            1,
            {
                "id": 1,
                "start": 0.0,
                "end": 1.0,
                "text": "source",
                "split_status": "fallback",
                "split_reason": "token_reconstruct_failed",
                "split_reason_detail": "test detail",
                "split_events": [{"start": 0.0, "end": 1.0, "en": "source", "zh": "target"}],
            },
        )

        data = seg.to_json()
        self.assertEqual(data["split_status"], "fallback")
        self.assertEqual(data["split_reason"], "token_reconstruct_failed")
        self.assertEqual(data["split_reason_detail"], "test detail")

    def test_translate_description_writes_metadata_header_and_translated_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")

            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "title": "Original Title",
                        "webpage_url": "https://example.test/watch",
                        "uploader": "Original Channel",
                        "upload_date": "20260620",
                    },
                    f,
                )
            with open(ctx.desc, "w", encoding="utf-8") as f:
                f.write("Original description.")
            with open(ctx.tags, "w", encoding="utf-8") as f:
                f.write("['AI', 'philosophy']")

            captured_request = {}

            def fake_llm_json_once(llm, system_prompt, request, temperature=0.3, raw_label=None, disable_response_format=False):
                captured_request.update(request.to_json_value())
                return {
                    "title": "译后标题",
                    "description": "译后简介。",
                    "tags": ["人工智能", "哲学"],
                }

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                result = t.translate_description(ctx, FakeProviderLLM(), quiet=True)

            self.assertEqual(result, ctx.target_desc)
            self.assertEqual(captured_request["tags"], ["AI", "philosophy"])
            with open(ctx.target_desc, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("译后标题\n\n", content)
            self.assertIn("原视频：https://example.test/watch\n", content)
            self.assertIn("原标题：Original Title\n", content)
            self.assertIn("原作者：Original Channel\n", content)
            self.assertIn("上传时间：2026-06-20\n", content)
            self.assertIn("=====\n\n译后简介。", content)
            self.assertIn("标签：人工智能, 哲学", content)

    def test_write_ass_uses_named_output_modes(self):
        template = os.path.abspath("template.ass")
        event = t.SplitEvent(1.0, 2.0, "source line", "目标行")

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, "source.ass")
            target_path = os.path.join(tmp, "target.ass")
            bilingual_path = os.path.join(tmp, "bilingual.ass")

            t.write_ass(source_path, template, "title", [event], t.AssOutputMode.SOURCE)
            t.write_ass(target_path, template, "title", [event], t.AssOutputMode.TARGET)
            t.write_ass(bilingual_path, template, "title", [event], t.AssOutputMode.BILINGUAL)

            with open(source_path, "r", encoding="utf-8") as f:
                source_ass = f.read()
            with open(target_path, "r", encoding="utf-8") as f:
                target_ass = f.read()
            with open(bilingual_path, "r", encoding="utf-8") as f:
                bilingual_ass = f.read()

        self.assertIn(",bi-en,,0,0,0,,source line", source_ass)
        self.assertNotIn(",bi-zh,,0,0,0,,目标行", source_ass)
        self.assertIn(",bi-zh,,0,0,0,,目标行", target_ass)
        self.assertNotIn(",zh,,0,0,0,,目标行", target_ass)
        self.assertNotIn(",bi-en,,0,0,0,,source line", target_ass)
        self.assertIn(",bi-en,,0,0,0,,source line", bilingual_ass)
        self.assertIn(",bi-zh,,0,0,0,,目标行", bilingual_ass)


if __name__ == "__main__":
    unittest.main()
