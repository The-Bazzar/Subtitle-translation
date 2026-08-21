import dataclasses
import json
import multiprocessing
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import batch_cache
import whisper_worker
from batch_cache import (
    _fsync_parent_directory,
    asr_sidecar_path,
    bind_wav_artifact,
    build_asr_fingerprint,
    build_asr_fingerprint_for_artifact,
    read_valid_asr_cache,
    write_prepare_state,
    write_asr_cache,
)
from whisper_worker import (
    AsrWorkerConfig,
    AsrWorkerController,
    WorkerCommand,
    WorkerExitedError,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash")


def bash_path(path):
    resolved = pathlib.Path(path).resolve()
    if os.name != "nt":
        return str(resolved)
    if BASH and "system32" in BASH.lower():
        drive, tail = os.path.splitdrive(str(resolved))
        return f"/mnt/{drive[0].lower()}{tail.replace(os.sep, '/')}"
    return subprocess.run(
        [BASH, "-lc", 'cygpath -u "$1"', "cygpath", str(resolved)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class FakeBackend:
    def __init__(self, config):
        self.config = config
        self.identity = f"{os.getpid()}:{id(self)}"
        if config.hf_token:
            assert os.environ["HF_TOKEN"] == config.hf_token
            assert os.environ["HUGGING_FACE_HUB_TOKEN"] == config.hf_token
        assert os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1"
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
        self._record("unload")
        if os.environ.get("WHISPER_WORKER_TEST_UNLOAD_ERROR") == "1":
            raise RuntimeError("fake unload failure")


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
        self.prepared_state = write_prepare_state(
            self.render_video,
            self.edit_video,
        )
        self.artifact = bind_wav_artifact(
            self.edit_video,
            self.wav_path,
            self.prepared_state.generation,
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
        return build_asr_fingerprint_for_artifact(self.artifact, **values)

    def write_cache(self, fingerprint, result):
        return write_asr_cache(
            self.edit_video,
            fingerprint,
            result,
            media_generation=self.artifact.media_generation,
            wav_snapshot=self.artifact.wav_snapshot,
        )

    def read_cache(self, fingerprint):
        return read_valid_asr_cache(
            self.edit_video,
            fingerprint,
            self.artifact.media_generation,
        )

    @staticmethod
    def asr_result(text="hello"):
        return {
            "segments": [{"start": 0.0, "end": 1.0, "text": text}],
            "language": "en",
        }

    def test_fingerprint_is_immutable_and_contains_all_asr_inputs(self):
        fingerprint = self.fingerprint()

        with self.assertRaises(dataclasses.FrozenInstanceError):
            fingerprint.model = "changed"

        serialized = fingerprint.to_dict()
        self.assertEqual(serialized["edit_video_path"], str(self.edit_video.resolve()))
        self.assertEqual(serialized["edit_video_size"], self.edit_video.stat().st_size)
        self.assertEqual(serialized["edit_video_mtime_ns"], self.edit_video.stat().st_mtime_ns)
        self.assertEqual(serialized["media_generation"], self.artifact.media_generation)
        self.assertEqual(serialized["wav_snapshot"], self.artifact.wav_snapshot.to_dict())
        self.assertEqual(serialized["model"], "large-v3-turbo")
        self.assertEqual(serialized["compute_type"], "float16")
        self.assertEqual(serialized["source_language"], "en")
        self.assertEqual(
            serialized["asr_options"],
            {"batch_size": 16, "vad": {"offset": 0.363, "onset": 0.5}},
        )

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

    def test_cache_lock_normalizes_persistent_file_to_one_byte(self):
        lock_path = batch_cache.asr_cache_lock_path(self.edit_video)
        lock_path.write_bytes(b"oversized-lock")

        with batch_cache.asr_cache_lock(self.edit_video):
            self.assertEqual(lock_path.stat().st_size, 1)

        self.assertEqual(lock_path.stat().st_size, 1)

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

    def test_sidecar_write_rejects_wav_changed_after_transcribe(self):
        fingerprint = self.fingerprint()
        self.wav_path.write_bytes(b"changed-wav")

        with self.assertRaisesRegex(
            batch_cache.MediaGenerationMismatch,
            "WAV artifact changed",
        ):
            batch_cache.write_asr_cache_for_artifact(
                self.artifact,
                fingerprint,
                self.asr_result(),
            )

        self.assertFalse(asr_sidecar_path(self.edit_video).exists())


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
        prepared_state = write_prepare_state(render_video, edit_video)
        self.artifacts[wav_path.resolve()] = bind_wav_artifact(
            edit_video,
            wav_path,
            prepared_state.generation,
        )
        return edit_video, wav_path

    def artifact_for(self, wav_path):
        return self.artifacts[pathlib.Path(wav_path).resolve()]

    def controller(self, **overrides):
        return AsrWorkerController(
            self.config,
            backend_factory="tests.test_whisper_worker:FakeBackend",
            **overrides,
        )

    def test_worker_command_values_are_stable(self):
        self.assertEqual(
            [command.value for command in WorkerCommand],
            ["load_asr", "transcribe", "unload_asr", "shutdown"],
        )

    def test_default_worker_config_matches_standalone_cpu_defaults(self):
        config = AsrWorkerConfig()

        self.assertEqual(config.model, "large-v3-turbo")
        self.assertEqual(config.device, "cpu")
        self.assertEqual(config.compute_type, "float32")
        self.assertEqual(config.options_dict(), {"batch_size": 8})

    def test_start_failure_terminates_process_and_closes_every_ipc_resource(self):
        controller = self.controller()
        request_queue = mock.Mock()
        receive_connection = mock.Mock(closed=False)
        send_connection = mock.Mock(closed=False)
        process = mock.Mock(pid=1234)
        process.start.side_effect = RuntimeError("start failed")
        process.is_alive.side_effect = (True, False)
        context = mock.Mock()
        context.Queue.return_value = request_queue
        context.Pipe.return_value = (receive_connection, send_connection)
        context.Process.return_value = process
        controller._context = context

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            controller.start()

        process.terminate.assert_called_once_with()
        process.join.assert_called()
        process.close.assert_called_once_with()
        receive_connection.close.assert_called_once_with()
        send_connection.close.assert_called_once_with()
        request_queue.close.assert_called_once_with()
        request_queue.join_thread.assert_called_once_with()
        self.assertIsNone(controller._process)
        self.assertIsNone(controller._request_queue)
        self.assertIsNone(controller._response_connection)

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

        first_artifact = self.artifact_for(first_wav)
        second_artifact = self.artifact_for(second_wav)
        first_fingerprint = build_asr_fingerprint_for_artifact(
            first_artifact,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language="en",
            asr_options=self.config.options_dict(),
        )
        second_fingerprint = build_asr_fingerprint_for_artifact(
            second_artifact,
            model=self.config.model,
            compute_type=self.config.compute_type,
            source_language="ja",
            asr_options=self.config.options_dict(),
        )
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
                side_effect=(0.0, 1.0),
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
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name)
        self.video = self.root / "sample.mkv"
        self.video.write_bytes(b"video")

    def run_powershell(self, *, ffmpeg_exit=0, whisper_exit=0):
        if PWSH is None:
            self.skipTest("PowerShell 7 is unavailable")
        shutil.copy2(ROOT / "whisper.ps1", self.root / "whisper.ps1")
        shutil.copy2(ROOT / ".env.ps1", self.root / ".env.ps1")
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        ffmpeg = tools / "ffmpeg.cmd"
        ffmpeg.write_text(
            "@echo off\n"
            "set wav=\n"
            ":args\n"
            "if \"%~1\"==\"\" goto done\n"
            "if /I \"%~x1\"==\".wav\" set wav=%~1\n"
            "shift\n"
            "goto args\n"
            ":done\n"
            "if defined wav >\"%wav%\" echo wav\n"
            "exit /b %FAKE_FFMPEG_EXIT%\n",
            encoding="ascii",
        )
        whisperx = tools / "whisperx.cmd"
        whisperx.write_text(
            "@echo off\n"
            "if not \"%FAKE_WHISPER_EXIT%\"==\"0\" exit /b %FAKE_WHISPER_EXIT%\n"
            ">\"%~dpn1.json\" echo {\"segments\":[],\"language\":\"en\"}\n",
            encoding="ascii",
        )
        (self.root / ".env").write_text(
            f"WHISPERX_PATH_WIN={whisperx}\nWHISPER_DEVICE=cpu\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{tools}{os.pathsep}{env.get('PATH', '')}",
                "WHISPERX_PATH_WIN": str(whisperx),
                "WHISPER_DEVICE": "cpu",
                "FAKE_FFMPEG_EXIT": str(ffmpeg_exit),
                "FAKE_WHISPER_EXIT": str(whisper_exit),
            }
        )
        return subprocess.run(
            [PWSH, "-NoProfile", "-File", str(self.root / "whisper.ps1"), str(self.video)],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def run_bash(self, *, ffmpeg_exit=0, whisper_exit=0):
        if BASH is None:
            self.skipTest("bash is unavailable")
        shutil.copy2(ROOT / "whisper.sh", self.root / "whisper.sh")
        venv_bin = self.root / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        python_bin = venv_bin / "python"
        python_bin.write_bytes(b"#!/bin/sh\nexit 0\n")
        python_bin.chmod(0o755)
        tools = self.root / "tools"
        tools.mkdir(exist_ok=True)
        ffmpeg = tools / "ffmpeg"
        ffmpeg.write_bytes((
            "#!/bin/sh\n"
            "wav=\n"
            "for arg in \"$@\"; do case \"$arg\" in *.wav) wav=$arg;; esac; done\n"
            "[ -n \"$wav\" ] && printf wav > \"$wav\"\n"
            "exit \"${FAKE_FFMPEG_EXIT:-0}\"\n"
        ).encode("ascii"))
        ffmpeg.chmod(0o755)
        whisperx = tools / "whisperx"
        whisperx.write_bytes((
            "#!/bin/sh\n"
            "[ \"${FAKE_WHISPER_EXIT:-0}\" -eq 0 ] || exit \"$FAKE_WHISPER_EXIT\"\n"
            "printf '{\"segments\":[],\"language\":\"en\"}\\n' > \"${1%.wav}.json\"\n"
        ).encode("ascii"))
        whisperx.chmod(0o755)
        command = (
            f"PATH={shlex.quote(bash_path(tools))}:$PATH "
            f"WHISPERX_PATH_LINUX={shlex.quote(bash_path(whisperx))} "
            f"WHISPER_DEVICE=cpu FAKE_FFMPEG_EXIT={ffmpeg_exit} "
            f"FAKE_WHISPER_EXIT={whisper_exit} "
            f"{shlex.quote(bash_path(self.root / 'whisper.sh'))} "
            f"{shlex.quote(bash_path(self.video))}"
        )
        return subprocess.run(
            [BASH, "-lc", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_real_wrappers_emit_marker_and_cleanup_wav_on_success(self):
        for runner in (self.run_powershell, self.run_bash):
            with self.subTest(runner=runner.__name__):
                (self.root / "sample.json").unlink(missing_ok=True)
                (self.root / "sample.wav").unlink(missing_ok=True)
                result = runner()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("OUTPUT_JSON=", result.stdout)
                self.assertTrue((self.root / "sample.json").is_file())
                self.assertFalse((self.root / "sample.wav").exists())
                (self.root / "sample.json").unlink()

    def test_real_wrappers_propagate_ffmpeg_failure_without_marker(self):
        for runner in (self.run_powershell, self.run_bash):
            with self.subTest(runner=runner.__name__):
                (self.root / "sample.json").unlink(missing_ok=True)
                (self.root / "sample.wav").unlink(missing_ok=True)
                result = runner(ffmpeg_exit=29)
                self.assertEqual(result.returncode, 29, result.stdout + result.stderr)
                self.assertNotIn("OUTPUT_JSON=", result.stdout)
                self.assertFalse((self.root / "sample.wav").exists())

    def test_real_wrappers_propagate_whisper_failure_and_cleanup_wav(self):
        for runner in (self.run_powershell, self.run_bash):
            with self.subTest(runner=runner.__name__):
                (self.root / "sample.json").unlink(missing_ok=True)
                (self.root / "sample.wav").unlink(missing_ok=True)
                result = runner(whisper_exit=23)
                self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
                self.assertNotIn("OUTPUT_JSON=", result.stdout)
                self.assertFalse((self.root / "sample.wav").exists())

    def test_powershell_and_bash_share_asr_defaults_and_final_json_contract(self):
        powershell = (ROOT / "whisper.ps1").read_text(encoding="utf-8")
        bash = (ROOT / "whisper.sh").read_text(encoding="utf-8")

        self.assertIn("$BatchSize = 8", powershell)
        self.assertIn("BATCH_SIZE=8", bash)
        self.assertIn("'--batch_size', $BatchSize", powershell)
        self.assertIn('--batch_size "$BATCH_SIZE"', bash)
        self.assertIn("'--compute_type', $ComputeType", powershell)
        self.assertIn('--compute_type "$COMPUTE_TYPE"', bash)
        self.assertIn("OUTPUT_JSON=", powershell)
        self.assertIn("OUTPUT_JSON=", bash)
        self.assertNotIn(".asr.json", powershell)
        self.assertNotIn(".asr.json", bash)


if __name__ == "__main__":
    unittest.main()
