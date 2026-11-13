import copy
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import translate_srt as t
from subtitle_translation import mfa_pipeline
from subtitle_translation.config import ProjectConfig
from fakes import FakeLLM


def write_wav(path):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)


class MfaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = ProjectConfig(self.root, {})

    def test_canonical_asr_wav_is_reused_without_ffmpeg(self):
        path = self.root / "video.wav"
        write_wav(path)
        with patch.object(mfa_pipeline, "run_command") as decode:
            actual = mfa_pipeline.canonical_audio(path, self.config, self.root / "work", {})
        self.assertEqual(actual, path)
        decode.assert_not_called()

    def test_media_audio_cache_checks_generation_and_payload_checksum(self):
        path = self.root / "video.mkv"
        path.write_bytes(b"synthetic media")
        calls = []
        def decode(command, **kwargs):
            calls.append(command)
            write_wav(Path(command[-1]))
            return SimpleNamespace(returncode=0)
        with patch.object(ProjectConfig, "resolve_tool", return_value="ffmpeg"), \
             patch.object(mfa_pipeline, "run_command", side_effect=decode):
            first = mfa_pipeline.canonical_audio(path, self.config, self.root / "work", {"media_generation": "one"})
            second = mfa_pipeline.canonical_audio(path, self.config, self.root / "work", {"media_generation": "one"})
            third = mfa_pipeline.canonical_audio(path, self.config, self.root / "work", {"media_generation": "two"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(calls), 2)

    def test_media_changes_during_decode_do_not_publish_audio_cache(self):
        media = self.root / "video.mkv"
        media.write_bytes(b"media")
        def decode(command, **kwargs):
            write_wav(Path(command[-1]))
            media.write_bytes(b"changed media")
            return SimpleNamespace(returncode=0)
        with patch.object(ProjectConfig, "resolve_tool", return_value="ffmpeg"), \
             patch.object(mfa_pipeline, "run_command", side_effect=decode), self.assertRaisesRegex(ValueError, "media changed"):
            mfa_pipeline.canonical_audio(media, self.config, self.root / "work", {})
        self.assertFalse(list((self.root / "work").rglob("audio.json")))

    def test_cli_mfa_flow_translates_before_alignment_and_does_not_repeat_glossary(self):
        from subtitle_translation import windowed_mfa
        from subtitle_translation import bilingual_translation
        source = t.Transcript(str(self.root / "video.json"), "en", [t.TranscriptSegment(1, 0, 1, "Hello.")])
        t.save_transcript(source, source.path)
        template = self.root / "template.ass"
        template.write_text("synthetic template", encoding="utf-8")
        prepared = copy.deepcopy(source)
        prepared.segments[0].words = [t.TranscriptWord("Hello.", 0, 1)]
        prepared.segments[0].word_timing = {"status": "available", "aligner": "mfa"}
        order = []
        with patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fake"}), \
             patch.object(t, "translate_llm_from_env", return_value=FakeLLM()), \
             patch.object(t, "prepare_mfa_review", return_value=({}, self.root / "video.wav", None)), \
             patch.object(windowed_mfa, "load_text_checkpoint", return_value=(prepared, self.root / "text.json")), \
             patch.object(windowed_mfa, "finish_timing", side_effect=lambda *a: order.extend(["mfa", "scene_detection"]) or (prepared, None, None)), \
             patch.object(t, "build_glossary") as glossary, \
             patch.object(bilingual_translation, "translate_events", side_effect=lambda *a, **k: order.append("translate") or False), \
             patch.object(t, "write_ass"), patch.object(t, "load_prompt", return_value="style"):
            t.main([source.path, "--template", str(template)], stage_overrides={"MFA_ENABLED": "1", "PROOFREAD": "0", })
        self.assertEqual(order, ["translate", "mfa", "scene_detection"])
        glossary.assert_not_called()

    def test_missing_mfa_runtime_is_not_a_silent_legacy_fallback(self):
        source = t.Transcript(str(self.root / "video.json"), "en", [t.TranscriptSegment(1, 0, 1, "Hello.")])
        t.save_transcript(source, source.path)
        with patch.object(t, "_config_root", return_value=str(self.root)), \
             patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fake"}), \
             patch("subtitle_translation.bilingual_translation.translate_events") as translate:
            result = t.main([source.path], stage_overrides={"MFA_ENABLED": "1", })
        self.assertEqual(result, 1)
        translate.assert_not_called()

    def test_pipeline_checks_mfa_before_downloading_or_running_asr(self):
        from subtitle_translation import pipeline
        args = pipeline.build_parser().parse_args(["url", "--project-dir", str(self.root)])
        with patch.object(pipeline, "download_video") as download, \
             patch.object(ProjectConfig, "load", return_value=ProjectConfig(self.root, {"MFA_ENABLED": "1"})):
            self.assertEqual(pipeline.run_pipeline(args), 2)
        download.assert_not_called()

    def test_dry_run_shows_new_order_without_requiring_tool_installation(self):
        from subtitle_translation import pipeline
        args = pipeline.build_parser().parse_args(["url"])
        args.mfa = True
        plan = pipeline.pipeline_stage_plan(args)
        self.assertLess(plan.index("glossary/source_review"), plan.index("mfa"))
        self.assertLess(plan.index("mfa"), plan.index("scene_detection"))
        self.assertLess(plan.index("translate"), plan.index("mfa"))


if __name__ == "__main__":
    unittest.main()
