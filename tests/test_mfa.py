import copy
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import translate_srt as t
from subtitle_translation import mfa, mfa_export, process
from subtitle_translation.artifacts import atomic_write_json


class MfaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.audio = self.root / "input.wav"
        with wave.open(str(self.audio), "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(16000)
            file.writeframes(b"\0\0" * 16000)
        self.files = {}
        for name in ("mfa.exe", "python.exe", "dictionary.dict", "acoustic.zip"):
            path = self.root / name
            path.write_text("mock local resource", encoding="utf-8")
            self.files[name] = path
        self.config = {"schema_version": 1, "mfa": {
            "profile": mfa.MFA_PROFILE, "version": "3.4.3", "command": [str(self.files["mfa.exe"])],
            "python_command": [str(self.files["python.exe"])], "dictionary": str(self.files["dictionary.dict"]),
            "acoustic_model": str(self.files["acoustic.zip"]),
        }}
        self.config_path = self.root / "asr_runtime.json"
        atomic_write_json(self.config_path, self.config)
        self.runtime = replace(mfa.MfaRuntime.from_file(self.config_path), work_directory=self.root / "native work")
        self.calls = []
        self.output = {"schema_version": 1, "mfa_version": "3.4.3", "praatio_version": "mock-6",
                       "words": [{"word": "hello", "start": 0, "end": 0.4},
                                 {"word": "world", "start": 0.5, "end": 0.9}]}

    def fake_run(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        if "--expected-version" in arguments:
            target = Path(arguments[arguments.index("--output") + 1])
            result = {
                "schema_version": 1, "mfa_version": "3.4.3", "praatio_version": self.output["praatio_version"],
            }
            if "--aligned-directory" in arguments:
                ids = json.loads(Path(arguments[arguments.index("--ids-file") + 1]).read_text())["ids"]
                result = {**result, "items": [{"id": item_id, "words": copy.deepcopy(self.output["words"])} for item_id in ids]}
            atomic_write_json(target, result)
        else:
            target = Path(arguments[5])
            target.mkdir(parents=True)
            (target / "media.TextGrid").write_text("mock TextGrid", encoding="utf-8")
        return process.CommandResult(tuple(arguments), 0)


    def corpus(self):
        return mfa.align_window_corpus([
            {"id": "a" * 64, "audio": self.audio, "text": "Hello world."},
            {"id": "b" * 64, "audio": self.audio, "text": "Hello world."},
        ], self.runtime, self.root / ".asr_work", t.text_tokens)

    def test_noncanonical_corpus_audio_is_rejected_before_tool_execution(self):
        with wave.open(str(self.audio), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0\0\0" * 16000)
        with patch.object(mfa, "run_command") as run, self.assertRaises(ValueError):
            self.corpus()
        run.assert_not_called()

    def test_closed_admission_propagates_without_caching_business_failure(self):
        def guard():
            if self.calls:
                raise RuntimeError("admission closed")
        entries = [{"id": "a" * 64, "audio": self.audio, "text": "Hello world."}]
        with patch.object(mfa, "run_command", side_effect=self.fake_run), self.assertRaisesRegex(RuntimeError, "admission closed"):
            mfa.align_window_corpus(entries, self.runtime, self.root / "work", t.text_tokens, admission_guard=guard)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(list((self.root / "work").rglob("current.json")))

    def test_corpus_has_one_align_and_one_export_regardless_of_window_count(self):
        with patch.object(mfa, "run_command", side_effect=self.fake_run):
            first = self.corpus()
            self.assertEqual(len(self.calls), 3)
            self.assertEqual(sum("align" in args for args, _ in self.calls), 1)
            self.assertFalse(any("segment" in args for args, _ in self.calls))
            command = next(args for args, _ in self.calls if "align" in args)
            self.assertEqual(command[command.index("--num_jobs") + 1], "1")
            self.calls.clear()
            second = self.corpus()
        self.assertEqual(first, second)
        self.assertTrue(all(r["status"] == "available" for r in second.values()))
        self.assertEqual(len(self.calls), 1)

    def test_corpus_bad_window_is_cached_locally_without_poisoning_good_window(self):
        def run(arguments, **kwargs):
            result = self.fake_run(arguments, **kwargs)
            if "--aligned-directory" in arguments:
                path = Path(arguments[arguments.index("--output") + 1])
                data = json.loads(path.read_text())
                data["items"][1]["words"] = []
                atomic_write_json(path, data)
            return result
        with patch.object(mfa, "run_command", side_effect=run):
            first = self.corpus()
            self.calls.clear()
            second = self.corpus()
        self.assertEqual(first, second)
        self.assertEqual(first["a" * 64]["status"], "available")
        self.assertEqual(first["b" * 64]["status"], "unavailable")
        self.assertEqual(len(self.calls), 1)

    def test_corpus_model_change_realigns_all_windows_as_one_adaptation_unit(self):
        with patch.object(mfa, "run_command", side_effect=self.fake_run):
            self.corpus()
            self.calls.clear()
            self.files["dictionary.dict"].write_text("different model", encoding="utf-8")
            self.corpus()
        self.assertEqual(len(self.calls), 3)

    def test_native_windows_sqlite_work_is_short_and_cache_identity_keeps_full_hashes(self):
        with patch.object(mfa.platform, "system", return_value="Windows"), patch.object(mfa, "run_command", side_effect=self.fake_run):
            first = self.corpus()
            command, kwargs = next((args, kw) for args, kw in self.calls if "align" in args)
            execution = Path(kwargs["cwd"])
            self.assertEqual(execution.parent, self.runtime.work_directory / "r")
            self.assertLessEqual(len(str(execution)) + 100, 240)
            self.assertEqual(kwargs["env"]["MFA_ROOT_DIR"], str(execution / "mfa_root"))
            self.assertEqual(command[command.index("--temporary_directory") + 1], str(execution / "align_temp"))
            self.assertEqual({p.stem for p in (execution / "corpus").glob("*.wav")}, {"a" * 64, "b" * 64})
            cache = next((self.root / ".asr_work/corpus_alignment").glob("*/current.json"))
            self.assertEqual(len(cache.parent.name), 64)
            record = json.loads((cache.parent / json.loads(cache.read_text())["generation"] / "alignment.json").read_text())
            self.assertEqual([item["id"] for item in record["identity"]["inputs"]], ["a" * 64, "b" * 64])
            directories = list((self.runtime.work_directory / "r").iterdir())
            self.calls.clear()
            second = self.corpus()
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list((self.runtime.work_directory / "r").iterdir()), directories)

    def test_linux_and_wsl_execution_directory_layout_is_unchanged(self):
        root = self.root / "cache generation"
        with patch.object(mfa.platform, "system", return_value="Linux"):
            self.assertEqual(mfa.corpus_execution_directory(self.runtime, root), root)
        with patch.object(mfa.platform, "system", return_value="Windows"):
            self.assertEqual(mfa.corpus_execution_directory(replace(self.runtime, transport="wsl"), root), root)

    def test_long_windows_work_root_uses_short_temp_without_shortening_identifiers(self):
        runtime = replace(self.runtime, work_directory=self.root / ("long" * 40))
        with patch.object(mfa.platform, "system", return_value="Windows"), \
             patch.object(mfa.tempfile, "gettempdir", return_value=str(self.root)):
            root = mfa.corpus_execution_directory(runtime, self.root / "cache generation")
        self.assertEqual(root.parent, self.root / "subtitle-mfa/r")
        self.assertEqual(len(root.name), 32)
        self.assertLessEqual(len(str(root)) + 100, 240)

    def test_corpus_rejects_duplicate_export_ids_and_does_not_publish_cache(self):
        def run(arguments, **kwargs):
            result = self.fake_run(arguments, **kwargs)
            if "--aligned-directory" in arguments:
                path = Path(arguments[arguments.index("--output") + 1])
                data = json.loads(path.read_text())
                data["items"][1]["id"] = data["items"][0]["id"]
                atomic_write_json(path, data)
            return result
        with patch.object(mfa, "run_command", side_effect=run):
            result = self.corpus()
        self.assertTrue(all(r["status"] == "unavailable" for r in result.values()))
        self.assertFalse(list((self.root / ".asr_work" / "corpus_alignment").rglob("current.json")))

    def test_corpus_cancellation_and_input_change_never_publish_success(self):
        with patch.object(mfa, "run_command", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.corpus()
        self.assertFalse(list((self.root / ".asr_work").rglob("current.json")))
        def run(arguments, **kwargs):
            result = self.fake_run(arguments, **kwargs)
            if "align" in arguments:
                self.files["acoustic.zip"].write_text("changed during native alignment", encoding="utf-8")
            return result
        with patch.object(mfa, "run_command", side_effect=run):
            result = self.corpus()
        self.assertTrue(all(r["reason"] == "mfa_corpus_input_changed" for r in result.values()))
        self.assertFalse(list((self.root / ".asr_work").rglob("current.json")))

    def test_corpus_cache_digest_corruption_is_not_silently_accepted(self):
        with patch.object(mfa, "run_command", side_effect=self.fake_run):
            self.corpus()
            record = next((self.root / ".asr_work" / "corpus_alignment").rglob("alignment.json"))
            data = json.loads(record.read_text())
            data["results"]["a" * 64]["words"] = []
            atomic_write_json(record, data)
            self.calls.clear()
            self.corpus()
        self.assertEqual(len(self.calls), 3)


    def test_invalid_or_incomplete_word_axis_never_becomes_available(self):
        for kind in ("nan", "negative", "overlap", "outside", "unknown", "missing", "boolean"):
            output = copy.deepcopy(self.output)
            if kind == "nan":
                output["words"][0]["end"] = float("nan")
            elif kind == "negative":
                output["words"][0]["start"] = -1
            elif kind == "overlap":
                output["words"][1]["start"] = 0.2
            elif kind == "outside":
                output["words"][1]["end"] = 2
            elif kind == "unknown":
                output["words"][0]["word"] = "<unk>"
            elif kind == "missing":
                output["words"].pop()
            else:
                output["words"][0]["start"] = False
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                mfa.validate_alignment(output, "Hello world.", 1.0, "3.4.3", t.text_tokens)

    def test_runtime_requires_local_files_and_a_locked_supported_profile(self):
        for field, value in (("version", "4.0.0"), ("command", ["mfa"]),
                             ("dictionary", "english_us_mfa"), ("python_command", ["python"]),
                             ("profile", "unknown")):
            config = copy.deepcopy(self.config)
            config["mfa"][field] = value
            atomic_write_json(self.config_path, config)
            with self.subTest(field=field), self.assertRaises(ValueError):
                mfa.MfaRuntime.from_file(self.config_path)

    def test_wsl_unc_path_mapping_preserves_spaces_without_shell_interpolation(self):
        runtime = replace(self.runtime, transport="wsl", distribution="Debian",
                          command=(str(self.files["mfa.exe"]), "--distribution", "Debian", "--exec", "/tool/mfa"))
        with patch.object(mfa, "capture_command") as capture:
            actual = runtime.tool_path(r"\\wsl.localhost\Debian\home\user\folder with spaces\audio.wav")
        self.assertEqual(actual, "/home/user/folder with spaces/audio.wav")
        capture.assert_not_called()
        with patch.object(mfa, "capture_command", return_value=SimpleNamespace(returncode=0, stdout="/mnt/g/folder with spaces/file.wav\n")) as capture:
            actual = runtime.tool_path(r"G:\folder with spaces\file.wav")
        self.assertEqual(actual, "/mnt/g/folder with spaces/file.wav")
        self.assertEqual(capture.call_args.args[0][-1], r"G:\folder with spaces\file.wav")

    def test_wsl_invocation_sets_explicit_cpu_environment_without_exporting_api_keys(self):
        runtime = replace(self.runtime, transport="wsl", distribution="Debian")
        command = ["wsl.exe", "--distribution", "Debian", "--exec", "/tool/mfa", "align"]
        environment = {key: "1" for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "CUDA_VISIBLE_DEVICES")}
        environment["OPENAI_API_KEY"] = "not-forwarded"
        with patch.object(mfa.MfaRuntime, "tool_path", side_effect=lambda path: "/linux/" + Path(path).name):
            result, cwd = runtime.invocation(command, self.root, environment)
        self.assertIsNone(cwd)
        self.assertIn("--cd", result)
        self.assertIn("OMP_NUM_THREADS=1", result)
        self.assertFalse(any("OPENAI_API_KEY" in arg for arg in result))


    def test_exporter_uses_praatio_word_tiers_and_never_parses_phone_tiers_as_words(self):
        class Tier:
            def __init__(self, word):
                self.entries = [SimpleNamespace(label=word, start=0, end=1)]
        tiers = {"speaker - words": Tier("hello"), "speaker - phones": Tier("h")}
        grid = SimpleNamespace(tierNames=list(tiers), getTier=tiers.__getitem__)
        parser = SimpleNamespace(IntervalTier=Tier, openTextgrid=Mock(return_value=grid))
        with patch.dict(sys.modules, {"praatio": SimpleNamespace(textgrid=parser)}):
            words = mfa_export.export_word_tiers(self.root / "synthetic.TextGrid")
        self.assertEqual(words, [{"word": "hello", "start": 0, "end": 1}])
        parser.openTextgrid.assert_called_once()


    def test_exporter_corpus_preserves_ids_and_missing_grid_is_a_local_error(self):
        (self.root / ("a" * 64 + ".TextGrid")).write_text("synthetic", encoding="utf-8")
        with patch.object(mfa_export, "export_word_tiers", return_value=self.output["words"]):
            result = mfa_export.export_corpus_words(self.root, ["a" * 64, "b" * 64])
        self.assertEqual(result[0]["words"], self.output["words"])
        self.assertNotIn("error", result[0])
        self.assertEqual(result[1]["id"], "b" * 64)
        self.assertTrue(result[1]["error"])
        with self.assertRaises(ValueError):
            mfa_export.export_corpus_words(self.root, ["../unsafe"])


class ManagedProcessTests(unittest.TestCase):
    def test_timeout_terminates_own_tree_and_removes_registry_entry(self):
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired(["tool"], 1), -9]
        with patch.object(process.subprocess, "Popen", return_value=child), \
             patch.object(process, "terminate_process_tree") as kill:
            result = process.run_command(["tool"], timeout=1)
        self.assertEqual(result.returncode, 124)
        kill.assert_called_once_with(child)
        self.assertNotIn(child, process._active_processes)

    def test_keyboard_interrupt_kills_own_tree_then_propagates(self):
        child = Mock()
        child.wait.side_effect = [KeyboardInterrupt, -9]
        with patch.object(process.subprocess, "Popen", return_value=child), \
             patch.object(process, "terminate_process_tree") as kill, self.assertRaises(KeyboardInterrupt):
            process.run_command(["tool"])
        kill.assert_called_once_with(child)
        self.assertNotIn(child, process._active_processes)


if __name__ == "__main__":
    unittest.main()
