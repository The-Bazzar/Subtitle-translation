import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import translate_srt as t
from subtitle_translation import chapters, process


class DescriptionChapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="chapters with spaces ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ctx = t.TranscriptContext.from_json(str(self.root / "film name.beautified.json"), source_lang="en", target_lang="zh")
        self.source_chapters = [chapters.Chapter(1, 0.0, "Opening"), chapters.Chapter(2, 3723.75, "A new lens")]
        self.llm = SimpleNamespace()

    def response(self, values=None):
        return {"title": "Translated title", "description": "Translated description.", "tags": ["cinema"],
                "chapters": values if values is not None else [{"id": 1, "title": "\u5f00\u573a"}, {"id": 2, "title": "\u65b0\u955c\u5934"}]}

    def test_extracts_original_mkv_only_and_passes_path_as_one_argument(self):
        original = self.root / "film name.original.mkv"
        original.write_bytes(b"synthetic media placeholder")
        output = {"chapters": [{"start_time": "0.000000", "tags": {"title": "Opening"}}]}
        with patch.object(chapters.ProjectConfig, "load") as load, \
             patch.object(chapters, "_ffprobe_path", return_value="configured ffprobe.exe"), \
             patch.object(chapters, "capture_command", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(output))) as capture:
            load.return_value.resolve_tool.return_value = "configured ffmpeg.exe"
            result = chapters.read_original_chapters(self.ctx.dir, self.ctx.base)
        self.assertEqual(result, [chapters.Chapter(1, 0, "Opening")])
        args = capture.call_args.args[0]
        self.assertEqual(args[-1], str(original))
        self.assertEqual(args[0], "configured ffprobe.exe")
        self.assertIn("-show_chapters", args)
        self.assertEqual(capture.call_args.kwargs["cwd"], self.root)
        self.assertEqual(capture.call_args.kwargs["timeout"], 30)

    def test_missing_original_does_not_probe_edited_mkv_or_other_projects(self):
        (self.root / "film name.mkv").write_bytes(b"edited file")
        (self.root / "another.original.mkv").write_bytes(b"another project")
        with patch.object(chapters, "capture_command") as probe:
            self.assertEqual(chapters.read_original_chapters(self.ctx.dir, self.ctx.base), [])
        probe.assert_not_called()

    def test_parser_sorts_times_and_handles_untitled_or_uppercase_tags(self):
        result = chapters.parse_chapters({"chapters": [
            {"start_time": "61.25", "tags": {"TITLE": " Second\nchapter "}},
            {"start_time": "0", "tags": {}},
            {"start_time": "62", "tags": {"title": None}},
        ]})
        self.assertEqual([value.title for value in result], ["Chapter 1", "Second chapter", "Chapter 3"])
        self.assertEqual([value.id for value in result], [1, 2, 3])
        self.assertEqual([value.timecode for value in result], ["00:00:00", "00:01:01", "00:01:02"])

    def test_invalid_start_times_are_not_rendered_or_sent_to_llm(self):
        result = chapters.parse_chapters({"chapters": [
            {"start_time": value} for value in (None, True, "nan", "inf", "-1", "bad", [])
        ] + [{"start_time": "0", "tags": {"title": "Valid"}}]})
        self.assertEqual(result, [chapters.Chapter(1, 0, "Valid")])

    def test_probe_failure_or_invalid_json_reports_extraction_failure(self):
        (self.root / "film name.original.mkv").write_bytes(b"synthetic")
        for result in (SimpleNamespace(returncode=124, stdout=""), SimpleNamespace(returncode=0, stdout="bad json")):
            with self.subTest(result=result), patch.object(chapters.ProjectConfig, "load"), \
                 patch.object(chapters, "_ffprobe_path", return_value="ffprobe"), \
                 patch.object(chapters, "capture_command", return_value=result), self.assertRaises(ValueError):
                chapters.read_original_chapters(self.ctx.dir, self.ctx.base)

    def test_empty_chapters_do_not_create_placeholder_entries(self):
        self.assertEqual(chapters.parse_chapters({"chapters": []}), [])
        with self.assertRaises(ValueError):
            chapters.parse_chapters({"chapters": "invalid"})

    def test_duplicate_start_times_are_preserved_in_original_order(self):
        result = chapters.parse_chapters({"chapters": [
            {"start_time": "0", "tags": {"title": "first"}},
            {"start_time": "0", "tags": {"title": "second"}},
        ]})
        self.assertEqual([value.title for value in result], ["first", "second"])

    def test_translation_keeps_local_timestamps_and_orders_response_by_id(self):
        output = self.response([{"id": 2, "title": "\u65b0\u955c\u5934", "start_time": 999},
                                {"id": 1, "title": "\u5f00\u573a", "start_time": 888}])
        with patch.object(t, "read_original_chapters", return_value=self.source_chapters), \
             patch.object(t, "llm_json_once", return_value=output) as request:
            t.translate_description(self.ctx, self.llm, )
        payload = request.call_args.args[2].to_json_value()
        self.assertEqual(payload["chapters"], [value.to_request() for value in self.source_chapters])
        self.assertEqual(request.call_count, 1)
        text = Path(self.ctx.target_desc).read_text(encoding="utf-8")
        self.assertIn("\u7ae0\u8282\uff1a\n00:00:00 \u5f00\u573a\n01:02:03 \u65b0\u955c\u5934\n", text)
        self.assertNotIn("999", text)
        self.assertIn("Translated description.", text)

    def test_missing_duplicate_unknown_or_empty_titles_fall_back_per_chapter(self):
        for values in (None, [], [{"id": 1, "title": ""}], [{"id": 99, "title": "invented"}],
                       [{"id": True, "title": "not a valid ID"}],
                       [{"id": 1, "title": "first"}, {"id": 1, "title": "duplicate"}]):
            with self.subTest(values=values):
                result = chapters.translated_chapters(self.source_chapters, values)
                self.assertEqual(result, self.source_chapters)
        result = chapters.translated_chapters(self.source_chapters, [{"id": 2, "title": "new\n title"}])
        self.assertEqual(result[0], self.source_chapters[0])
        self.assertEqual(result[1].title, "new title")

    def test_missing_chapter_response_keeps_original_titles(self):
        response = self.response()
        response.pop("chapters")
        with patch.object(t, "read_original_chapters", return_value=self.source_chapters), \
             patch.object(t, "llm_json_once", return_value=response):
            t.translate_description(self.ctx, self.llm, )
        text = Path(self.ctx.target_desc).read_text(encoding="utf-8")
        self.assertIn("00:00:00 Opening", text)
        self.assertIn("01:02:03 A new lens", text)

    def test_no_chapters_preserves_old_description_without_empty_heading(self):
        with patch.object(t, "read_original_chapters", return_value=[]), \
             patch.object(t, "llm_json_once", return_value=self.response()) as request:
            t.translate_description(self.ctx, self.llm, )
        self.assertNotIn("chapters", request.call_args.args[2].to_json_value())
        self.assertNotIn("\u7ae0\u8282\uff1a", Path(self.ctx.target_desc).read_text(encoding="utf-8"))

    def test_probe_failure_does_not_block_other_description_translation(self):
        with patch.object(t, "read_original_chapters", side_effect=ValueError("probe unavailable")), \
             patch.object(t, "llm_json_once", return_value=self.response()):
            t.translate_description(self.ctx, self.llm, )
        text = Path(self.ctx.target_desc).read_text(encoding="utf-8")
        self.assertIn("Translated description.", text)
        self.assertNotIn("\u7ae0\u8282\uff1a", text)

    def test_request_failure_preserves_existing_target_description(self):
        Path(self.ctx.target_desc).write_text("existing translated description", encoding="utf-8")
        with patch.object(t, "read_original_chapters", return_value=self.source_chapters), \
             patch.object(t, "llm_json_once", side_effect=RuntimeError("offline")):
            t.translate_description(self.ctx, self.llm, )
        self.assertEqual(Path(self.ctx.target_desc).read_text(encoding="utf-8"), "existing translated description")

    def test_main_translates_container_chapters_without_source_description_file(self):
        source = self.root / "film name.json"
        source.write_text(json.dumps({"language": "en", "segments": [{"text": "hello", "start": 0, "end": 1}]}), encoding="utf-8")
        template = self.root / "template.ass"
        template.write_text("synthetic template", encoding="utf-8")
        (self.root / "film name.original.mkv").write_bytes(b"synthetic original")
        with patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fake"}), \
             patch.object(t, "translate_llm_from_env", return_value=self.llm), \
             patch.object(t, "load_prompt", return_value="style"), \
             patch("subtitle_translation.bilingual_translation.translate_events", return_value=False), \
             patch.object(t, "write_ass"), \
             patch.object(t, "read_original_chapters", return_value=self.source_chapters), \
             patch.object(t, "llm_json_once", return_value=self.response()) as translate:
            t.main([str(source), "--template", str(template)], stage_overrides={"PIPELINE_SKIP_SCENE_DETECTION": "1", "PIPELINE_SKIP_KNOWLEDGE": "1", "PROOFREAD": "0", })
        self.assertEqual(translate.call_count, 1)
        self.assertTrue(Path(self.ctx.target_desc).exists())
        self.assertFalse(Path(self.ctx.desc).exists())

    def test_ffprobe_timeout_is_a_failure_not_partial_json(self):
        with patch.object(process.subprocess, "run", side_effect=subprocess.TimeoutExpired(["ffprobe"], 30)):
            result = process.capture_command(["ffprobe"], timeout=30)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
