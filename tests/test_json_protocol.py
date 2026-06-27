import json
import os
import tempfile
import unittest
from unittest.mock import patch

import translate_srt as t


class JsonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")

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
        class FakeLLM:
            provider = "fake"

        class FakeRetriever:
            def retrieve_texts(self, texts, top_k=None):
                self.texts = texts
                return [[{"id": "transcript:1", "text": "important context", "score": 0.9}]]

        captured = {}

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
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
                t.build_glossary(transcript, ctx, FakeLLM(), quiet=True, retriever=retriever)

            self.assertEqual(captured["retrieved_context"], [{"id": "transcript:1", "text": "important context", "score": 0.9}])
            self.assertTrue(retriever.texts[0])

    def test_build_glossary_prefixes_local_video_metadata(self):
        class FakeLLM:
            provider = "fake"

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
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
                glossary = t.build_glossary(transcript, ctx, FakeLLM(), quiet=True)

            self.assertIn("## 视频元信息", glossary)
            self.assertIn("原标题：Original Title", glossary)
            self.assertIn("原作者：Original Channel", glossary)
            self.assertIn("上传时间：2025-01-02", glossary)
            self.assertIn("原简介：", glossary)
            self.assertIn("A long-form discussion", glossary)
            self.assertIn("标签：philosophy, discipline", glossary)
            self.assertIn("## 核心术语", glossary)

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
        class FakeLLM:
            provider = "fake"

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
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
                glossary = t.build_glossary(transcript, ctx, FakeLLM(), quiet=True)

            self.assertTrue(os.path.isfile(ctx.glossary))
            with open(ctx.glossary, "r", encoding="utf-8") as f:
                saved = f.read()

        self.assertIn("## 视频元信息", glossary)
        self.assertIn("原标题：Original Title", saved)
        self.assertIn("原作者：Original Channel", saved)

    def test_build_glossary_system_prompt_includes_strict_json_protocols(self):
        class FakeLLM:
            provider = "fake"

        captured = {}

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
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
                t.build_glossary(transcript, ctx, FakeLLM(), quiet=True)

        self.assertIn("MANDATORY JSON PROTOCOL", captured["system_prompt"])
        self.assertIn("Return a JSON object.", captured["system_prompt"])
        self.assertIn("MANDATORY GLOSSARY JSON PROTOCOL", captured["system_prompt"])
        self.assertIn('Return exactly one top-level key: "markdown".', captured["system_prompt"])

    def test_build_glossary_uses_agent_queries_for_tavily_search(self):
        class FakeLLM:
            provider = "fake"

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

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
            prompts.append(system_prompt)
            if "TAVILY SEARCH QUERY JSON PROTOCOL" in system_prompt:
                data = request.to_json_value()
                query_request.update(data)
                return {"queries": ["counterfeit version altar of convenience", "discipline motivation therapist"]}
            return {"markdown": "# 术语知识库"}

        def fake_tavily_search(query, api_key, max_results=5):
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

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once), patch.object(
                t, "tavily_search", side_effect=fake_tavily_search
            ):
                retriever = FakeRetriever()
                t.build_glossary(transcript, ctx, FakeLLM(), tavily_key="tk", quiet=True, retriever=retriever)

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
        class FakeLLM:
            provider = "fake"

        def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
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
                queries = t.build_tavily_queries(transcript, ctx, FakeLLM(), quiet=True)

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
        class FakeLLM:
            provider = "fake"
            batch_size = 10

            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

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
            t.translate_segments(transcript, self.ctx, FakeLLM(), "system", quiet=True, retriever=None)

        self.assertNotIn("retrieved_context", captured["request"]["items"][0])
        self.assertNotIn("RETRIEVED CONTEXT:", captured["system_prompt"])

    def test_translate_segments_adds_retrieved_context(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 10

            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

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
            t.translate_segments(transcript, self.ctx, FakeLLM(), "system", quiet=True, retriever=retriever)

        self.assertEqual(captured["items"][0]["retrieved_context"], [{"id": "transcript:1", "text": "translation memory"}])
        self.assertEqual(retriever.texts, ["source text"])

    def test_proofread_split_events_adds_retrieved_context(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 10
            api_key = None
            proofread_retrieval_top_k = 1

            def pr_provider(self):
                return "fake"

            def pr_model(self):
                return "fake-model"

            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

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
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", quiet=True, retriever=retriever)

        self.assertEqual(captured["items"][0]["retrieved_context"], [{"id": "transcript:1", "text": "proofread memory"}])
        self.assertEqual(retriever.texts, ["source\n译文"])
        self.assertEqual(retriever.top_k, 1)

    def test_proofread_split_events_respects_small_batch_without_token_limit(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 10
            api_key = None
            proofread_batch_size = 2
            proofread_retrieval_top_k = 1

            def pr_provider(self):
                return "fake"

            def pr_model(self):
                return "fake-model"

            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

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
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", quiet=True)

        self.assertEqual(calls, [2, 1])

    def test_proofread_split_events_splits_batch_on_context_length_error(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 10
            api_key = None
            proofread_batch_size = 2
            proofread_retrieval_top_k = 1

            def pr_provider(self):
                return "fake"

            def pr_model(self):
                return "fake-model"

            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

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
            changed = t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", quiet=True)

        self.assertTrue(changed)
        self.assertEqual(calls, [2, 1, 1])
        self.assertEqual(transcript.segments[0].split_events[0].en, "source one fixed")
        self.assertEqual(transcript.segments[0].split_events[1].zh, "译文二 fixed")

    def test_proofread_split_events_drops_retrieved_context_when_single_item_is_too_large(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 2
            api_key = None
            proofread_batch_size = 1
            proofread_retrieval_top_k = 1

            def pr_provider(self):
                return "fake"

            def pr_model(self):
                return "fake-model"

            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

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
                FakeLLM(),
                "system",
                quiet=True,
                retriever=FakeRetriever(),
            )

        self.assertTrue(changed)
        self.assertEqual(saw_context, [True, False])
        self.assertEqual(transcript.segments[0].split_events[0].en, "source one fixed")

    def test_chat_session_passes_provider_response_format(self):
        calls = []

        class FakeMessage:
            content = '{"markdown": "ok"}'

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        class FakeLLM:
            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {"response_format": {"type": "json_object"}}

            def _client(self):
                return FakeClient()

        t.ChatSession(FakeLLM(), "system").ask("{}")

        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(set(calls[0]), {"model", "messages", "temperature", "response_format"})

    def test_chat_session_empty_content_error_includes_provider_details(self):
        class FakeUsageDetails:
            reasoning_tokens = 128

        class FakeUsage:
            prompt_tokens = 100
            completion_tokens = 200
            total_tokens = 300
            completion_tokens_details = FakeUsageDetails()

        class FakeMessage:
            content = ""
            reasoning_content = "model thought but produced no content"
            refusal = "blocked by provider"

        class FakeChoice:
            finish_reason = "length"
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]
            usage = FakeUsage()

        class FakeCompletions:
            def create(self, **kwargs):
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        class FakeLLM:
            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {}

            def _client(self):
                return FakeClient()

        try:
            t.ChatSession(FakeLLM(), "system").ask("{}")
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
        chunk = t.EmbeddingChunk("a", "transcript", "discipline and motivation", 1.0, 2.0, {"segment": 1})

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
            },
        )

    def test_langchain_docs_convert_to_retrieved_context(self):
        doc = t.Document(
            page_content="discipline and motivation",
            metadata={"id": "transcript:1", "source": "transcript", "start": 1.0, "end": 2.0},
        )

        context = t.documents_to_retrieved_context([doc])

        self.assertEqual(
            context,
            [
                {
                    "id": "transcript:1",
                    "source": "transcript",
                    "text": "discipline and motivation",
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
        self.assertEqual(chunks[0].metadata["segment_ids"], [1, 2])
        self.assertEqual(chunks[0].start, 0.0)
        self.assertEqual(chunks[0].end, 2.0)
        self.assertEqual(chunks[1].metadata["segment_ids"], [2, 3])

    def test_build_embedding_chunks_overlaps_transcript_boundaries(self):
        transcript = t.Transcript(
            path="video.json",
            language="en",
            segments=[
                t.TranscriptSegment(1, 0.0, 1.0, "alpha beta"),
                t.TranscriptSegment(2, 1.0, 2.0, "gamma delta"),
                t.TranscriptSegment(3, 2.0, 3.0, "epsilon zeta"),
                t.TranscriptSegment(4, 3.0, 4.0, "eta theta"),
            ],
        )

        chunks = t.build_embedding_chunks(transcript, chunk_chars=32)

        self.assertEqual([chunk.metadata["segment_ids"] for chunk in chunks], [[1, 2], [2, 3], [3, 4]])
        self.assertEqual([chunk.chunk_id for chunk in chunks], ["transcript:1-2", "transcript:2-3", "transcript:3-4"])

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
        source_candidates = t.response_key_candidates(self.ctx, "source")
        target_candidates = t.response_key_candidates(self.ctx, "target")

        self.assertEqual(t.get_language_keyed_value({"en": "source text"}, source_candidates), "source text")
        self.assertEqual(t.get_language_keyed_value({"zh": "target text"}, target_candidates), "target text")
        self.assertIsNone(t.get_language_keyed_value({"source": "legacy"}, source_candidates))
        self.assertIsNone(t.get_language_keyed_value({"target": "legacy"}, target_candidates))

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

            def fake_llm_json_once(llm, system_prompt, request, temperature=0.3):
                captured_request.update(request.to_json_value())
                return {
                    "title": "译后标题",
                    "description": "译后简介。",
                    "tags": ["人工智能", "哲学"],
                }

            class FakeLLM:
                pass

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                result = t.translate_description(ctx, FakeLLM(), quiet=True)

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
