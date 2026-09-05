import dataclasses
import json
import multiprocessing
import os
import pathlib
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import batch_cache
import whisper_worker
from batch_cache import (
    _fsync_parent_directory,
    asr_sidecar_path,
    bind_wav_artifact,
    build_asr_fingerprint,
    read_valid_asr_cache,
    write_asr_cache,
    write_prepare_state,
)
from whisper_worker import (
    AsrWorkerConfig,
    AsrWorkerController,
    WorkerCommand,
    WorkerExitedError,
    normalize_language_code,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeBackend:
    def __init__(self, config, alignment_language=""):
        self.config = config
        self.alignment_language = alignment_language
        self.identity = f"{os.getpid()}:{id(self)}"
        if config.hf_token:
            assert os.environ["HF_TOKEN"] == config.hf_token
            assert os.environ["HUGGING_FACE_HUB_TOKEN"] == config.hf_token
        assert os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1"
        if alignment_language:
            self._record(f"load_align:{alignment_language}:{config.align_model or 'auto'}")
        else:
            self._record("load")

    def _record(self, event):
        log_path = os.environ.get("WHISPER_WORKER_TEST_LOG")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"{event}\n")

    def transcribe(self, wav_path, source_language):
        wav_path = pathlib.Path(wav_path)
        self._record(f"transcribe:{wav_path.name}")
        if "slow" in wav_path.name:
            time.sleep(0.2)
        if "hang" in wav_path.name:
            threading.Event().wait()
        if "task-error" in wav_path.name:
            raise ValueError("fake transcription failure")
        if "crash" in wav_path.name:
            raise SystemExit(23)
        return {
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": wav_path.stem,
                    "backend_identity": self.identity,
                }
            ],
            "language": source_language,
        }

    def unload(self):
        event = "unload_align" if self.alignment_language else "unload"
        self._record(event)
        if os.environ.get("WHISPER_WORKER_TEST_UNLOAD_ERROR") == "1":
            raise RuntimeError("fake unload failure")

    def align(self, wav_path, segments):
        wav_path = pathlib.Path(wav_path)
        self._record(f"align:{wav_path.name}:{self.alignment_language}")
        aligned = {
            "segments": [
                {
                    **segment,
                    "words": [
                        {
                            "word": segment["text"],
                            "start": segment["start"],
                            "end": segment["end"],
                            "score": 0.99,
                        }
                    ],
                    "backend_identity": self.identity,
                }
                for segment in segments
            ]
        }
        if "slow-align" in wav_path.name:
            ready_path = os.environ.get("WHISPER_WORKER_TEST_ALIGN_READY")
            if ready_path:
                pathlib.Path(ready_path).write_text("ready", encoding="utf-8")
            time.sleep(0.75)
        if "align-error" in wav_path.name:
            raise ValueError("fake alignment failure")
        return aligned


class MutatingAlignmentBackend(FakeBackend):
    def align(self, wav_path, segments):
        wav_path = pathlib.Path(wav_path).resolve()
        pathlib.Path(os.environ["WHISPER_WORKER_TEST_ALIGN_PATH"]).write_text(
            str(wav_path),
            encoding="utf-8",
        )
        wav_path.write_bytes(wav_path.read_bytes() + b"-changed-during-align")
        return super().align(str(wav_path), segments)


def hang_on_command_target(
    request_queue,
    _response_connection,
    _config,
    _backend_factory_path,
    _heartbeat_interval,
):
    request_queue.get()
    threading.Event().wait()


class ScriptedReceiveConnection:
    def __init__(self, initial_messages, message_after_wait):
        self.messages = list(initial_messages)
        self.message_after_wait = message_after_wait
        self.closed = False

    def poll(self, timeout=0):
        if self.messages:
            return True
        if timeout > 0 and self.message_after_wait is not None:
            self.messages.append(self.message_after_wait)
            self.message_after_wait = None
            return True
        return False

    def recv(self):
        return self.messages.pop(0)

    def close(self):
        self.closed = True


class AsrCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name)
        self.edit_video = self.root / "episode.mkv"
        self.render_video = self.root / "episode.original.mkv"
        self.wav_path = self.root / "episode.wav"
        self.edit_video.write_bytes(b"edit-video")
        self.render_video.write_bytes(b"original-video")
        self.wav_path.write_bytes(b"wav")
        self.prepare_state = write_prepare_state(
            self.render_video,
            self.edit_video,
        )
        self.artifact = bind_wav_artifact(
            self.edit_video,
            self.wav_path,
            self.prepare_state.generation,
        )

    def fingerprint(self, **overrides):
        values = {
            "model": "large-v3-turbo",
            "compute_type": "float16",
            "source_language": "en",
            "asr_options": {
                "batch_size": 16,
                "vad": {"onset": 0.5, "offset": 0.363},
            },
        }
        values.update(overrides)
        return build_asr_fingerprint(self.edit_video, **values)

    @staticmethod
    def asr_result(text="hello"):
        return {
            "segments": [{"start": 0.0, "end": 1.0, "text": text}],
            "language": "en",
        }

    def write_cache(self, fingerprint, result):
        return write_asr_cache(
            self.edit_video,
            fingerprint,
            result,
            media_generation=self.prepare_state.generation,
            wav_snapshot=self.artifact.wav_snapshot,
        )

    def read_cache(self, fingerprint):
        return read_valid_asr_cache(
            self.edit_video,
            fingerprint,
            self.prepare_state.generation,
        )

    def test_fingerprint_is_immutable_and_contains_all_asr_inputs(self):
        fingerprint = self.fingerprint()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            fingerprint.model = "changed"

        serialized = fingerprint.to_dict()
        self.assertEqual(serialized["edit_video_path"], str(self.edit_video.resolve()))
        self.assertEqual(serialized["edit_video_size"], self.edit_video.stat().st_size)
        self.assertEqual(serialized["edit_video_mtime_ns"], self.edit_video.stat().st_mtime_ns)
        self.assertEqual(serialized["model"], "large-v3-turbo")
        self.assertEqual(serialized["compute_type"], "float16")
        self.assertEqual(serialized["source_language"], "en")
        self.assertEqual(
            serialized["asr_options"],
            {"batch_size": 16, "vad": {"offset": 0.363, "onset": 0.5}},
        )

    def test_prepare_state_rejects_render_size_change_with_preserved_mtime(self):
        write_prepare_state = getattr(batch_cache, "write_prepare_state", None)
        read_valid_prepare_state = getattr(
            batch_cache,
            "read_valid_prepare_state",
            None,
        )
        self.assertIsNotNone(write_prepare_state, "prepare state writer is missing")
        self.assertIsNotNone(
            read_valid_prepare_state,
            "prepare state validator is missing",
        )
        render_video = self.root / "episode.original.mkv"
        render_video.write_bytes(b"original")
        state = write_prepare_state(render_video, self.edit_video)
        original_mtime = render_video.stat().st_mtime_ns

        render_video.write_bytes(b"different-original-content")
        os.utime(render_video, ns=(original_mtime, original_mtime))

        self.assertIsNone(
            read_valid_prepare_state(render_video, self.edit_video)
        )
        self.assertTrue(state.generation)

    def test_asr_sidecar_write_rejects_edit_changed_after_wav_snapshot(self):
        write_prepare_state = getattr(batch_cache, "write_prepare_state", None)
        bind_wav_artifact = getattr(batch_cache, "bind_wav_artifact", None)
        write_for_artifact = getattr(
            batch_cache,
            "write_asr_cache_for_artifact",
            None,
        )
        build_from_snapshot = getattr(
            batch_cache,
            "build_asr_fingerprint_from_snapshot",
            None,
        )
        mismatch_error = getattr(batch_cache, "MediaGenerationMismatch", RuntimeError)
        for value, name in (
            (write_prepare_state, "prepare state writer"),
            (bind_wav_artifact, "WAV artifact binder"),
            (write_for_artifact, "artifact sidecar writer"),
            (build_from_snapshot, "snapshot fingerprint builder"),
        ):
            self.assertIsNotNone(value, f"{name} is missing")

        render_video = self.root / "episode.original.mkv"
        wav_path = self.root / "episode.wav"
        render_video.write_bytes(b"original")
        wav_path.write_bytes(b"wav")
        prepare_state = write_prepare_state(render_video, self.edit_video)
        artifact = bind_wav_artifact(
            self.edit_video,
            wav_path,
            prepare_state.generation,
        )
        fingerprint = build_from_snapshot(
            artifact.edit_snapshot,
            model="large-v3-turbo",
            compute_type="float16",
            source_language="en",
            asr_options={"batch_size": 16},
        )
        self.edit_video.write_bytes(b"changed-edit-generation")

        with self.assertRaisesRegex(mismatch_error, "media generation"):
            write_for_artifact(artifact, fingerprint, self.asr_result())

        self.assertFalse(asr_sidecar_path(self.edit_video).exists())

    def test_cache_round_trip_uses_atomic_sibling_replacement(self):
        fingerprint = self.fingerprint()
        result = self.asr_result()
        replace_calls = []
        real_replace = os.replace

        def replace_spy(source, destination):
            source_path = pathlib.Path(source)
            destination_path = pathlib.Path(destination)
            self.assertEqual(source_path.parent, destination_path.parent)
            self.assertTrue(source_path.is_file())
            replace_calls.append((source_path, destination_path))
            real_replace(source, destination)

        def fsync_parent_spy(path):
            self.assertEqual(len(replace_calls), 1)
            self.assertEqual(pathlib.Path(path), asr_sidecar_path(self.edit_video))

        with mock.patch("batch_cache.os.fsync", wraps=os.fsync) as fsync:
            with mock.patch("batch_cache.os.replace", side_effect=replace_spy):
                with mock.patch(
                    "batch_cache._fsync_parent_directory",
                    side_effect=fsync_parent_spy,
                ) as fsync_parent:
                    cache_path = self.write_cache(fingerprint, result)

        self.assertEqual(cache_path, asr_sidecar_path(self.edit_video))
        self.assertEqual(self.read_cache(fingerprint).result, result)
        self.assertEqual(replace_calls[0][1], cache_path)
        fsync.assert_called_once()
        fsync_parent.assert_called_once_with(cache_path)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_cache_write_uses_unique_valid_generation_and_loader_retains_it(self):
        fingerprint = self.fingerprint()

        cache_path = self.write_cache(fingerprint, self.asr_result("first"))
        first_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        first_cache = self.read_cache(fingerprint)
        self.write_cache(fingerprint, self.asr_result("second"))
        second_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        second_cache = self.read_cache(fingerprint)

        self.assertEqual(str(uuid.UUID(first_payload["generation"])), first_payload["generation"])
        self.assertEqual(str(uuid.UUID(second_payload["generation"])), second_payload["generation"])
        self.assertNotEqual(first_payload["generation"], second_payload["generation"])
        self.assertEqual(
            second_payload["media_generation"],
            self.prepare_state.generation,
        )
        self.assertEqual(
            second_payload["wav_snapshot"],
            self.artifact.wav_snapshot.to_dict(),
        )
        self.assertEqual(first_cache.generation, first_payload["generation"])
        self.assertEqual(second_cache.generation, second_payload["generation"])
        self.assertEqual(second_cache.result["segments"][0]["text"], "second")

    def test_cache_lock_artifact_is_persistent_and_gitignored(self):
        self.write_cache(self.fingerprint(), self.asr_result())

        lock_path = self.edit_video.with_suffix(".asr.lock")
        self.assertTrue(lock_path.is_file())
        self.assertLessEqual(lock_path.stat().st_size, 1)
        self.assertIn(
            "*.asr.lock",
            (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(),
        )
        self.assertIn(
            "*.prepare.json",
            (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(),
        )

    def test_matching_fingerprint_rejects_invalid_asr_result_schema(self):
        fingerprint = self.fingerprint()
        invalid_results = (
            {},
            {"segments": {}, "language": "en"},
            {"segments": [{"text": "missing timestamps"}], "language": "en"},
            {
                "segments": [{"start": True, "end": 1.0, "text": "bad start"}],
                "language": "en",
            },
            {
                "segments": [{"start": 2.0, "end": 1.0, "text": "reversed"}],
                "language": "en",
            },
            {"segments": [], "language": ""},
        )

        for result in invalid_results:
            with self.subTest(result=result):
                self.write_cache(fingerprint, result)
                self.assertIsNone(self.read_cache(fingerprint))

    def test_matching_fingerprint_accepts_empty_segments_with_language(self):
        fingerprint = self.fingerprint()
        result = {"segments": [], "language": "ja"}

        self.write_cache(fingerprint, result)

        self.assertEqual(self.read_cache(fingerprint).result, result)

    def test_cache_schema_handles_arbitrary_json_integers(self):
        fingerprint = self.fingerprint()
        timestamp = 10**1000
        result = {
            "segments": [{"start": timestamp, "end": timestamp, "text": "far"}],
            "language": "en",
        }

        self.write_cache(fingerprint, result)

        self.assertEqual(self.read_cache(fingerprint).result, result)

    def test_parent_directory_fsync_uses_directory_descriptor_on_posix(self):
        cache_path = asr_sidecar_path(self.edit_video)
        directory_flag = 0x10000

        with mock.patch.object(
            batch_cache.os,
            "O_DIRECTORY",
            directory_flag,
            create=True,
        ):
            with mock.patch("batch_cache.os.open", return_value=73) as open_directory:
                with mock.patch("batch_cache.os.fsync") as fsync:
                    with mock.patch("batch_cache.os.close") as close:
                        _fsync_parent_directory(cache_path, platform="posix")

        open_directory.assert_called_once_with(
            str(cache_path.parent),
            os.O_RDONLY | directory_flag,
        )
        fsync.assert_called_once_with(73)
        close.assert_called_once_with(73)

    def test_parent_directory_fsync_failure_is_best_effort(self):
        cache_path = asr_sidecar_path(self.edit_video)

        with mock.patch.object(
            batch_cache.os,
            "O_DIRECTORY",
            0x10000,
            create=True,
        ):
            with mock.patch("batch_cache.os.open", return_value=73):
                with mock.patch(
                    "batch_cache.os.fsync",
                    side_effect=OSError("unsupported"),
                ):
                    with mock.patch("batch_cache.os.close") as close:
                        _fsync_parent_directory(cache_path, platform="posix")

        close.assert_called_once_with(73)

    def test_invalid_corrupt_or_old_fingerprint_cache_is_ignored(self):
        expected = self.fingerprint()
        cache_path = asr_sidecar_path(self.edit_video)

        invalid_payloads = (
            {},
            {"schema_version": 1, "fingerprint": {}, "result": []},
            {
                "schema_version": 1,
                "fingerprint": expected.to_dict(),
                "result": {"segments": [], "language": "en"},
            },
            {
                "schema_version": 1,
                "generation": "not-a-uuid",
                "fingerprint": expected.to_dict(),
                "result": {"segments": [], "language": "en"},
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(self.read_cache(expected))

        cache_path.write_text("{not-json", encoding="utf-8")
        self.assertIsNone(self.read_cache(expected))

        old_fingerprint = self.fingerprint(model="large-v2")
        self.write_cache(old_fingerprint, {"segments": [], "language": "en"})
        self.assertIsNone(self.read_cache(expected))


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name)
        self.log_path = self.root / "worker.log"
        self.config = AsrWorkerConfig(
            model="fake-model",
            device="cpu",
            compute_type="int8",
            asr_options={"batch_size": 2},
            hf_token="fake-hf-token",
        )
        self.artifacts = {}

    def media_pair(self, name, language="en"):
        render_video = self.root / f"{name}.original.mkv"
        edit_video = self.root / f"{name}.mkv"
        wav_path = self.root / f"{name}.wav"
        render_video.write_bytes(b"original-video")
        edit_video.write_bytes(b"edit-video")
        wav_path.write_bytes(b"wav")
        (self.root / f"{name}.info.json").write_text(
            json.dumps({"language": language}),
            encoding="utf-8",
        )
        prepare_state = write_prepare_state(render_video, edit_video)
        self.artifacts[edit_video.resolve()] = bind_wav_artifact(
            edit_video,
            wav_path,
            prepare_state.generation,
        )
        return edit_video, wav_path

    def artifact_for(self, path):
        return self.artifacts[pathlib.Path(path).with_suffix(".mkv").resolve()]

    def write_cache(self, edit_video, result):
        artifact = self.artifacts[pathlib.Path(edit_video).resolve()]
        return write_asr_cache(
            edit_video,
            build_asr_fingerprint(
                edit_video,
                model=self.config.model,
                compute_type=self.config.compute_type,
                source_language=normalize_language_code(result["language"]),
                asr_options=self.config.options_dict(),
            ),
            result,
            media_generation=artifact.media_generation,
            wav_snapshot=artifact.wav_snapshot,
        )

    def controller(self, **overrides):
        return AsrWorkerController(
            self.config,
            backend_factory="tests.test_whisper_worker:FakeBackend",
            **overrides,
        )

    def test_worker_command_values_are_stable(self):
        self.assertEqual(
            [command.value for command in WorkerCommand],
            "load_asr transcribe unload_asr load_align align unload_align shutdown".split(),
        )

    def test_normal_close_removes_worker_capture_files(self):
        controller = self.controller()
        controller.start()
        try:
            stdout_path, stderr_path = controller.capture_paths
            self.assertTrue(stdout_path.is_file())
            self.assertTrue(stderr_path.is_file())
            controller.shutdown()
        finally:
            controller.close()

        self.assertFalse(stdout_path.exists())
        self.assertFalse(stderr_path.exists())

    def test_default_worker_config_matches_standalone_cpu_defaults(self):
        config = AsrWorkerConfig()

        self.assertEqual(config.model, "large-v3-turbo")
        self.assertEqual(config.device, "cpu")
        self.assertEqual(config.compute_type, "float32")
        self.assertEqual(config.options_dict(), {"batch_size": 8})
        self.assertEqual(config.align_model, "")
        configured = whisper_worker.asr_worker_config_from_environment(
            {
                "TORCH_BACKEND": "cpu",
                "WHISPER_ALIGN_MODEL": "custom-align-model",
                "SOURCE_LANG": "eng",
            }
        )
        self.assertEqual(configured.align_model, "custom-align-model")
        self.assertEqual(configured.source_language, "eng")

    def test_language_normalization_uses_valid_iso_and_bcp47_codes(self):
        expected = {
            "en": "en",
            "eng": "en",
            "deu": "de",
            "cmn": "cmn",
            "ja-JP": "ja",
            "zh_Hant": "zh",
        }

        for language, normalized in expected.items():
            with self.subTest(language=language):
                self.assertEqual(normalize_language_code(language), normalized)

    def test_language_normalization_rejects_unknown_codes_without_valid_fallback(self):
        for language in ("zz", "zzz", "und", "unknown", ""):
            with self.subTest(language=language):
                with self.assertRaises(ValueError):
                    normalize_language_code(language)

        self.assertEqual(
            normalize_language_code("und", fallback="eng"),
            "en",
        )
        self.assertEqual(
            normalize_language_code("unknown", fallback="en-US"),
            "en",
        )
        with self.assertRaises(ValueError):
            normalize_language_code("und", fallback="zzz")

    def test_one_asr_load_serves_multiple_path_only_transcriptions(self):
        first_video, first_wav = self.media_pair("first", language="en-US")
        second_video, second_wav = self.media_pair("second", language="ja")

        with mock.patch.dict(
            os.environ,
            {"WHISPER_WORKER_TEST_LOG": str(self.log_path)},
        ):
            with self.controller() as controller:
                self.assertTrue(controller.load_asr().ok)
                first_result = controller.transcribe(self.artifact_for(first_wav))
                second_result = controller.transcribe(self.artifact_for(second_wav))
                self.assertTrue(controller.unload_asr().ok)

        self.assertTrue(first_result.ok)
        self.assertTrue(second_result.ok)
        self.assertEqual(first_result.path, str(first_wav.resolve()))
        self.assertEqual(second_result.path, str(second_wav.resolve()))
        self.assertEqual(
            self.log_path.read_text(encoding="utf-8").splitlines(),
            ["load", "transcribe:first.wav", "transcribe:second.wav", "unload"],
        )

        first_fingerprint = build_asr_fingerprint(
            first_video,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language="en",
            asr_options=self.config.options_dict(),
        )
        second_fingerprint = build_asr_fingerprint(
            second_video,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language="ja",
            asr_options=self.config.options_dict(),
        )
        first_artifact = self.artifact_for(first_video)
        second_artifact = self.artifact_for(second_video)
        first_cache = read_valid_asr_cache(
            first_video,
            first_fingerprint,
            first_artifact.media_generation,
        )
        second_cache = read_valid_asr_cache(
            second_video,
            second_fingerprint,
            second_artifact.media_generation,
        )
        self.assertEqual(first_cache.result["language"], "en")
        self.assertEqual(second_cache.result["language"], "ja")
        self.assertEqual(
            first_cache.result["segments"][0]["backend_identity"],
            second_cache.result["segments"][0]["backend_identity"],
        )

    def test_asr_is_unloaded_before_path_only_alignment(self):
        edit_video, wav_path = self.media_pair("aligned", language="en-US")

        with mock.patch.dict(
            os.environ,
            {"WHISPER_WORKER_TEST_LOG": str(self.log_path)},
        ):
            with self.controller() as controller:
                controller.load_asr()
                artifact = self.artifact_for(wav_path)
                transcription = controller.transcribe(artifact)
                with self.assertRaises(RuntimeError):
                    controller.load_align("en")
                controller.unload_asr()
                controller.load_align("en")
                sidecar_payload = json.loads(
                    pathlib.Path(transcription.output_path).read_text(encoding="utf-8")
                )
                alignment = controller.align(
                    transcription.output_path,
                    sidecar_payload["generation"],
                    artifact,
                )
                controller.unload_align()

        final_path = edit_video.with_suffix(".json")
        candidate_path = pathlib.Path(alignment.output_path)
        self.assertTrue(alignment.ok)
        self.assertEqual(alignment.path, str(pathlib.Path(transcription.output_path)))
        self.assertNotEqual(candidate_path, final_path)
        self.assertIn(sidecar_payload["generation"], candidate_path.name)
        self.assertTrue(candidate_path.name.endswith(".candidate.json"))
        self.assertEqual(alignment.generation, sidecar_payload["generation"])
        self.assertTrue(pathlib.Path(transcription.output_path).exists())
        self.assertFalse(final_path.exists())
        candidate_result = json.loads(candidate_path.read_text(encoding="utf-8"))
        self.assertEqual(candidate_result["language"], "en")
        self.assertEqual(candidate_result["segments"][0]["words"][0]["word"], "aligned")
        self.assertEqual(
            self.log_path.read_text(encoding="utf-8").splitlines(),
            "load transcribe:aligned.wav unload load_align:en:auto "
            "align:aligned.wav:en unload_align".split(),
        )

    def test_alignment_failure_keeps_recovery_sidecar(self):
        _edit_video, wav_path = self.media_pair("align-error", language="ja")

        with self.controller() as controller:
            controller.load_asr()
            artifact = self.artifact_for(wav_path)
            transcription = controller.transcribe(artifact)
            controller.unload_asr()
            controller.load_align("ja")
            generation = json.loads(
                pathlib.Path(transcription.output_path).read_text(encoding="utf-8")
            )["generation"]
            alignment = controller.align(
                transcription.output_path,
                generation,
                artifact,
            )
            controller.unload_align()

        self.assertFalse(alignment.ok)
        self.assertEqual(alignment.error_type, "ValueError")
        self.assertTrue(pathlib.Path(transcription.output_path).is_file())

    def test_alignment_accepts_region_tagged_detected_language_in_iso_group(self):
        edit_video, _wav_path = self.media_pair("region-tagged", language="en-US")
        sidecar_path = self.write_cache(
            edit_video,
            {
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "region tagged"}
                ],
                "language": "en-US",
            },
        )

        with self.controller() as controller:
            controller.load_align("en")
            generation = json.loads(sidecar_path.read_text(encoding="utf-8"))[
                "generation"
            ]
            alignment = controller.align(
                sidecar_path,
                generation,
                self.artifact_for(edit_video),
            )
            controller.unload_align()

        self.assertTrue(alignment.ok)
        self.assertEqual(alignment.language, "en")
        final_result = json.loads(
            pathlib.Path(alignment.output_path).read_text(encoding="utf-8")
        )
        self.assertEqual(final_result["language"], "en")
        self.assertFalse(edit_video.with_suffix(".json").exists())

    def test_alignment_rejects_invalid_sidecar_schema_without_deleting_it(self):
        edit_video, _wav_path = self.media_pair("invalid-align", language="en")
        sidecar_path = edit_video.with_suffix(".asr.json")
        sidecar_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": str(uuid.uuid4()),
                    "fingerprint": {},
                    "result": {
                        "language": "en",
                        "segments": [{"start": 0.0, "end": 1.0}],
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.controller() as controller:
            controller.load_align("en")
            generation = json.loads(sidecar_path.read_text(encoding="utf-8"))[
                "generation"
            ]
            result = controller.align(
                sidecar_path,
                generation,
                self.artifact_for(edit_video),
            )
            controller.unload_align()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ValueError")
        self.assertTrue(sidecar_path.is_file())
        self.assertFalse(edit_video.with_suffix(".json").exists())

    def test_alignment_rejects_stale_expected_generation_before_backend_work(self):
        edit_video, _wav_path = self.media_pair("stale-generation", language="en")
        sidecar_path = self.write_cache(
            edit_video,
            {
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "stale"}
                ],
                "language": "en",
            },
        )

        with mock.patch.dict(
            os.environ,
            {"WHISPER_WORKER_TEST_LOG": str(self.log_path)},
        ):
            with self.controller() as controller:
                controller.load_align("en")
                result = controller.align(
                    sidecar_path,
                    str(uuid.uuid4()),
                    self.artifact_for(edit_video),
                )
                controller.unload_align()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ValueError")
        self.assertIn("generation changed", result.error)
        self.assertNotIn(
            "align:stale-generation.wav:en",
            self.log_path.read_text(encoding="utf-8").splitlines(),
        )
        self.assertTrue(sidecar_path.is_file())

    def test_alignment_uses_bound_wav_and_rejects_post_backend_snapshot_change(self):
        render_video = self.root / "snapshot.original.mkv"
        edit_video = self.root / "snapshot.mkv"
        bound_wav = self.root / "immutable-input.custom.wav"
        observed_path = self.root / "alignment-input.txt"
        render_video.write_bytes(b"original-video")
        edit_video.write_bytes(b"edit-video")
        bound_wav.write_bytes(b"bound-wav")
        (self.root / "snapshot.info.json").write_text(
            json.dumps({"language": "en"}),
            encoding="utf-8",
        )
        prepare_state = write_prepare_state(render_video, edit_video)
        artifact = bind_wav_artifact(
            edit_video,
            bound_wav,
            prepare_state.generation,
        )
        sidecar_path = write_asr_cache(
            edit_video,
            build_asr_fingerprint(
                edit_video,
                model=self.config.model,
                compute_type=self.config.compute_type,
                source_language="en",
                asr_options=self.config.options_dict(),
            ),
            {
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "snapshot"}
                ],
                "language": "en",
            },
            media_generation=artifact.media_generation,
            wav_snapshot=artifact.wav_snapshot,
        )
        generation = json.loads(sidecar_path.read_text(encoding="utf-8"))[
            "generation"
        ]

        with mock.patch.dict(
            os.environ,
            {"WHISPER_WORKER_TEST_ALIGN_PATH": str(observed_path)},
        ):
            with AsrWorkerController(
                self.config,
                backend_factory=(
                    "tests.test_whisper_worker:MutatingAlignmentBackend"
                ),
            ) as controller:
                controller.load_align("en")
                result = controller.align(
                    sidecar_path,
                    generation,
                    artifact,
                )
                controller.unload_align()

        self.assertEqual(
            pathlib.Path(observed_path.read_text(encoding="utf-8")),
            bound_wav.resolve(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "MediaGenerationMismatch")
        self.assertIn("WAV artifact changed", result.error)
        self.assertTrue(sidecar_path.is_file())
        self.assertFalse(edit_video.with_suffix(".json").exists())
        self.assertEqual(
            list(self.root.glob(f".snapshot.json.{generation}.*.candidate.json")),
            [],
        )

    def test_atomic_candidate_write_keeps_sidecar_for_parent_scheduler(self):
        edit_video, _wav_path = self.media_pair("atomic-align", language="en")
        sidecar_path = edit_video.with_suffix(".asr.json")
        sidecar_path.write_text("recovery", encoding="utf-8")
        generation = str(uuid.uuid4())
        candidate_path = whisper_worker.alignment_candidate_path(
            sidecar_path,
            generation,
        )
        aligned_result = {
            "language": "en",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hello",
                    "words": [{"word": "hello", "start": 0.0, "end": 1.0}],
                }
            ],
        }

        with mock.patch(
            "whisper_worker.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                whisper_worker.write_aligned_candidate_json(
                    sidecar_path,
                    generation,
                    candidate_path,
                    aligned_result,
                    media_generation=self.artifact_for(edit_video).media_generation,
                )

        self.assertTrue(sidecar_path.is_file())
        self.assertFalse(edit_video.with_suffix(".json").exists())
        self.assertFalse(candidate_path.exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])

        replace_calls = []
        real_replace = os.replace

        def replace_spy(source, destination):
            self.assertTrue(sidecar_path.is_file())
            replace_calls.append((pathlib.Path(source), pathlib.Path(destination)))
            real_replace(source, destination)

        with mock.patch("whisper_worker.os.replace", side_effect=replace_spy):
            written_candidate = whisper_worker.write_aligned_candidate_json(
                sidecar_path,
                generation,
                candidate_path,
                aligned_result,
                media_generation=self.artifact_for(edit_video).media_generation,
            )

        self.assertEqual(written_candidate.resolve(), candidate_path.resolve())
        self.assertEqual(replace_calls[0][0].parent, replace_calls[0][1].parent)
        self.assertTrue(sidecar_path.exists())
        self.assertFalse(edit_video.with_suffix(".json").exists())
        self.assertEqual(
            json.loads(candidate_path.read_text(encoding="utf-8")),
            {
                **aligned_result,
                "_batch_artifact": {
                    "media_generation": self.artifact_for(
                        edit_video
                    ).media_generation,
                    "alignment_generation": generation,
                },
            },
        )

    def test_final_schema_accepts_whisperx_words_without_timestamps(self):
        edit_video, _wav_path = self.media_pair("untimed-word", language="en")
        sidecar_path = edit_video.with_suffix(".asr.json")
        sidecar_path.write_text("recovery", encoding="utf-8")
        generation = str(uuid.uuid4())
        candidate_path = whisper_worker.alignment_candidate_path(
            sidecar_path,
            generation,
        )
        aligned_result = {
            "language": "en",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "123",
                    "words": [{"word": "123"}],
                }
            ],
        }

        written_candidate = whisper_worker.write_aligned_candidate_json(
            sidecar_path,
            generation,
            candidate_path,
            aligned_result,
            media_generation=self.artifact_for(edit_video).media_generation,
        )

        self.assertEqual(
            json.loads(written_candidate.read_text(encoding="utf-8")),
            {
                **aligned_result,
                "_batch_artifact": {
                    "media_generation": self.artifact_for(
                        edit_video
                    ).media_generation,
                    "alignment_generation": generation,
                },
            },
        )

    def test_task_exception_returns_structured_failure_and_worker_continues(self):
        _failed_video, failed_wav = self.media_pair("task-error")
        _good_video, good_wav = self.media_pair("after-error")

        with mock.patch.dict(
            os.environ,
            {"WHISPER_WORKER_TEST_LOG": str(self.log_path)},
        ):
            with self.controller() as controller:
                controller.load_asr()
                failed = controller.transcribe(self.artifact_for(failed_wav))
                succeeded = controller.transcribe(self.artifact_for(good_wav))
                controller.unload_asr()

        self.assertFalse(failed.ok)
        self.assertEqual(failed.command, WorkerCommand.TRANSCRIBE)
        self.assertEqual(failed.error_type, "ValueError")
        self.assertEqual(failed.error, "fake transcription failure")
        self.assertTrue(succeeded.ok)

    def test_heartbeats_allow_slow_transcription_past_max_silence(self):
        _edit_video, slow_wav = self.media_pair("slow")
        controller = self.controller(
            heartbeat_interval=0.01,
            max_heartbeat_silence=1.0,
            operation_timeout=2.0,
        )

        with controller:
            self.assertTrue(controller.load_asr().ok)
            controller._max_heartbeat_silence = 0.05
            result = controller.transcribe(self.artifact_for(slow_wav))
            self.assertTrue(controller.unload_asr().ok)

        self.assertTrue(result.ok)

    def test_queued_current_result_is_read_before_expired_deadlines(self):
        controller = self.controller(
            max_heartbeat_silence=0.5,
            operation_timeout=0.5,
        )
        request_queue = mock.Mock()
        receive_connection, send_connection = multiprocessing.get_context(
            "spawn"
        ).Pipe(duplex=False)
        process = mock.Mock(pid=1234)
        process.is_alive.return_value = True
        controller._request_queue = request_queue
        controller._response_connection = receive_connection
        controller._process = process
        send_connection.send(
            whisper_worker.WorkerResult(
                command=WorkerCommand.LOAD_ASR,
                ok=True,
                request_id=1,
            )
        )

        try:
            with mock.patch(
                "whisper_worker.time.monotonic",
                side_effect=lambda values=iter((0.0, 1.0)): next(values, 1.0),
            ):
                result = controller._request(WorkerCommand.LOAD_ASR)
        finally:
            receive_connection.close()
            send_connection.close()

        self.assertTrue(result.ok)
        request_queue.put.assert_called_once()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_queued_current_heartbeat_refreshes_silence_before_timeout(self):
        controller = self.controller(
            max_heartbeat_silence=0.5,
            operation_timeout=10.0,
        )
        request_queue = mock.Mock()
        response_connection = ScriptedReceiveConnection(
            initial_messages=[whisper_worker._WorkerHeartbeat(request_id=1)],
            message_after_wait=whisper_worker.WorkerResult(
                command=WorkerCommand.LOAD_ASR,
                ok=True,
                request_id=1,
            ),
        )
        process = mock.Mock(pid=1234)
        process.is_alive.return_value = True
        controller._request_queue = request_queue
        controller._response_connection = response_connection
        controller._process = process

        with mock.patch(
            "whisper_worker.time.monotonic",
            side_effect=(0.0, 1.0, 1.0, 1.0),
        ):
            result = controller._request(WorkerCommand.LOAD_ASR)

        self.assertTrue(result.ok)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_operation_timeout_reaps_hung_worker_despite_heartbeats(self):
        _edit_video, hang_wav = self.media_pair("heartbeat-hang")
        controller = self.controller(
            heartbeat_interval=0.01,
            max_heartbeat_silence=1.0,
            operation_timeout=2.0,
        )
        controller.start()
        controller.load_asr()
        controller._max_heartbeat_silence = 0.05
        controller._operation_timeout = 0.15

        with self.assertRaises(whisper_worker.WorkerUnresponsiveError) as raised:
            controller.transcribe(self.artifact_for(hang_wav))

        self.assertEqual(raised.exception.command, WorkerCommand.TRANSCRIBE)
        self.assertIn("operation timeout", str(raised.exception))
        self.assertFalse(controller.is_alive)
        controller.close()
        self.assertFalse(controller.is_alive)

    def test_heartbeat_silence_reaps_hung_worker(self):
        _edit_video, hang_wav = self.media_pair("silent-hang")
        controller = self.controller(
            heartbeat_interval=0.2,
            max_heartbeat_silence=1.0,
            operation_timeout=2.0,
        )
        controller.start()
        controller.load_asr()
        controller._max_heartbeat_silence = 0.05

        with self.assertRaises(whisper_worker.WorkerUnresponsiveError) as raised:
            controller.transcribe(self.artifact_for(hang_wav))

        self.assertEqual(raised.exception.command, WorkerCommand.TRANSCRIBE)
        self.assertIn("heartbeat silence", str(raised.exception))
        self.assertFalse(controller.is_alive)
        controller.close()

    def test_shutdown_and_close_are_idempotent_before_and_after_start(self):
        never_started = self.controller()

        self.assertTrue(never_started.shutdown().ok)
        never_started.close()
        never_started.close()

        controller = self.controller()
        controller.start()
        first_shutdown = controller.shutdown()
        second_shutdown = controller.shutdown()

        self.assertTrue(first_shutdown.ok)
        self.assertTrue(second_shutdown.ok)
        self.assertFalse(controller.is_alive)
        self.assertEqual(controller.exitcode, 0)
        controller.close()
        controller.close()

    def test_repeated_cycles_close_pipe_and_process_resources(self):
        baseline_children = {
            child.pid for child in multiprocessing.active_children()
        }
        worker_pids = set()

        for _index in range(3):
            controller = self.controller()
            with controller:
                receive_connection = controller._response_connection
                worker_pids.add(controller.pid)

            self.assertTrue(receive_connection.closed)
            self.assertEqual(controller.exitcode, 0)
            self.assertIsNone(controller._process)
            self.assertIsNone(controller._request_queue)

        active_pids = {child.pid for child in multiprocessing.active_children()}
        self.assertFalse(worker_pids & (active_pids - baseline_children))

    def test_hung_shutdown_is_force_reaped_and_later_close_is_noop(self):
        controller = AsrWorkerController(
            self.config,
            process_target=hang_on_command_target,
            heartbeat_interval=0.01,
            max_heartbeat_silence=0.05,
            operation_timeout=0.15,
        )
        controller.start()

        with self.assertRaises(whisper_worker.WorkerUnresponsiveError) as raised:
            controller.shutdown()

        self.assertEqual(raised.exception.command, WorkerCommand.SHUTDOWN)
        self.assertFalse(controller.is_alive)
        controller.close()
        controller.close()

    def test_close_force_reaps_hung_shutdown(self):
        controller = AsrWorkerController(
            self.config,
            process_target=hang_on_command_target,
            heartbeat_interval=0.01,
            max_heartbeat_silence=0.05,
            operation_timeout=0.15,
        )
        controller.start()

        with self.assertRaises(whisper_worker.WorkerUnresponsiveError) as raised:
            controller.close()

        self.assertEqual(raised.exception.command, WorkerCommand.SHUTDOWN)
        self.assertFalse(controller.is_alive)
        controller.close()

    def test_unexpected_exit_is_reported_without_restart(self):
        _edit_video, crash_wav = self.media_pair("crash")
        controller = self.controller()

        with mock.patch.dict(
            os.environ,
            {"WHISPER_WORKER_TEST_LOG": str(self.log_path)},
        ):
            controller.start()
            original_pid = controller.pid
            controller.load_asr()
            with self.assertRaises(WorkerExitedError) as raised:
                controller.transcribe(self.artifact_for(crash_wav))

            self.assertEqual(raised.exception.exitcode, 23)
            self.assertEqual(controller.pid, original_pid)
            with self.assertRaises(WorkerExitedError):
                controller.load_asr()
            with self.assertRaises(RuntimeError):
                controller.start()
            self.assertEqual(controller.pid, original_pid)
            controller.close()


class StandaloneWhisperScriptTests(unittest.TestCase):
    def test_powershell_and_bash_share_asr_defaults_and_final_json_contract(self):
        powershell = (ROOT / "scripts" / "whisper.ps1").read_text(encoding="utf-8")
        bash = (ROOT / "scripts" / "whisper.sh").read_text(encoding="utf-8")

        self.assertIn(" whisper ", powershell)
        self.assertIn(" whisper ", bash)
        stages = (ROOT / "core" / "subtitle_translation" / "stages.py").read_text(encoding="utf-8")
        self.assertIn("OUTPUT_JSON=", stages)
        self.assertNotIn(".asr.json", powershell)
        self.assertNotIn(".asr.json", bash)


if __name__ == "__main__":
    unittest.main()
