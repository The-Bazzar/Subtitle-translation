import json
import os
import tempfile
import unittest
from unittest.mock import patch

import translate_srt as t


class TranslationQualityInfrastructureTests(unittest.TestCase):
    def test_lexical_retriever_exactly_hits_named_term_without_unrelated_chunk(self):
        chunks = [
            t.EmbeddingChunk(
                "glossary:electrochemistry",
                "glossary",
                "Electrochemistry -> 食髓知味",
                context_text="Official translation for the named term Electrochemistry.",
            ),
            t.EmbeddingChunk(
                "glossary:rhetoric",
                "glossary",
                "Rhetoric impossible -> 能说会道",
                context_text="A separate terminology entry.",
            ),
        ]
        retriever = t.LexicalEvidenceRetriever(chunks, top_k=2)

        results = retriever.retrieve_texts(["Electrochemistry"])[0]

        self.assertEqual([item["id"] for item in results], ["glossary:electrochemistry"])
        self.assertNotIn("glossary:rhetoric", {item["id"] for item in results})

    def test_split_event_original_en_round_trips_only_when_different(self):
        unchanged = t.SplitEvent(0.0, 1.0, "same source", "译文")
        changed = t.SplitEvent(
            0.0,
            1.0,
            "corrected source",
            "译文",
            original_en="original source",
        )

        self.assertNotIn("original_en", unchanged.to_json())
        serialized = changed.to_json()
        self.assertEqual(serialized["original_en"], "original source")
        restored = t.SplitEvent.from_json(json.loads(json.dumps(serialized)))
        self.assertEqual(restored.en, "corrected source")
        self.assertEqual(restored.original_en, "original source")

    def test_glossary_cache_reconciles_evidence_once_then_reuses_metadata(self):
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="Electrochemistry translation",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.com/electrochemistry",
                            title="Official term",
                            content="Electrochemistry is translated as 食髓知味.",
                        )
                    ],
                )
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump({"language": "en", "segments": []}, handle)
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")
            with open(ctx.glossary, "w", encoding="utf-8") as handle:
                handle.write("# Existing glossary\n")
            t.write_web_evidence_sidecar(ctx, sidecar)
            transcript = t.Transcript(
                json_path,
                "en",
                [t.TranscriptSegment(1, 0.0, 1.0, "Electrochemistry")],
            )
            finalize_calls = []

            def fake_finalize(request_fields, received_sidecar, received_ctx, llm, options):
                finalize_calls.append(
                    {
                        "request_fields": request_fields,
                        "sidecar": received_sidecar,
                    }
                )
                return t.GlossaryBuildArtifact(
                    markdown="# Refreshed glossary\n- Electrochemistry：食髓知味",
                    web_evidence=received_sidecar,
                )

            with patch.object(t, "finalize_glossary_from_evidence", side_effect=fake_finalize):
                first = t.build_glossary(
                    transcript,
                    ctx,
                    t.LLMConfig(provider="fake"),
                    t.GlossaryBuildOptions(quiet=True),
                )
                second = t.build_glossary(
                    transcript,
                    ctx,
                    t.LLMConfig(provider="fake"),
                    t.GlossaryBuildOptions(quiet=True),
                )

            self.assertIn("Refreshed glossary", first)
            self.assertEqual(second, first)
            self.assertEqual(len(finalize_calls), 1)
            self.assertTrue(finalize_calls[0]["sidecar"].has_records())

            with open(ctx.glossary_cache_json, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
            self.assertEqual(metadata["version"], t.GLOSSARY_CACHE_VERSION)
            self.assertEqual(
                metadata["fingerprint"],
                t.glossary_cache_fingerprint(
                    transcript,
                    ctx,
                    t.read_video_metadata_fields(ctx),
                    t.load_web_evidence_sidecar(ctx.web_evidence_json),
                ),
            )


if __name__ == "__main__":
    unittest.main()
