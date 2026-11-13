import asyncio
import io
import inspect
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import translate_srt as t
import batch_runtime
from batch_scheduler import AcquisitionRunners, AcquisitionScheduler, BatchTask, ResourceLimits
from subtitle_translation.project_logging import LOG_FILENAME, log, logged_stage, project_run


class ProjectLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_console_always_uses_level_colors_even_when_redirected(self):
        console = io.StringIO()
        self.assertFalse(console.isatty())
        with patch("sys.stderr", console), patch.dict(os.environ, {"NO_COLOR": "1"}), project_run(self.root):
            log("WARNING", "color-warning")
            log("ERROR", "color-error")
        lines = console.getvalue().splitlines()
        warning = next(line for line in lines if "color-warning" in line)
        error = next(line for line in lines if "color-error" in line)
        ansi = r"\x1b\[[0-9;]*m"
        self.assertTrue(re.findall(ansi, warning))
        self.assertNotEqual(re.findall(ansi, warning), re.findall(ansi, error))
        self.assertIn("\x1b[0m", warning)

    def test_single_and_batch_log_files_remain_plain_without_ansi(self):
        for filename in (LOG_FILENAME, "batch-result.log"):
            with self.subTest(filename=filename), patch("sys.stderr", io.StringIO()), project_run(self.root, filename=filename):
                log("INFO", "<red>literal subtitle</red>")
                log("ERROR", "\x1b[31muntrusted-control\x1b[0m")
            text = (self.root / filename).read_text(encoding="utf-8")
            self.assertNotIn("\x1b", text)
            self.assertIn("<red>literal subtitle</red>", text)
            self.assertIn("\\x1b[31m", text)

    def test_log_level_filters_file_and_console_consistently_and_labels_stages(self):
        @logged_stage("proofread")
        def stage():
            for level in ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"):
                log(level, "record-" + level)
        console = io.StringIO()
        with patch("sys.stderr", console), project_run(self.root, "WARNING"):
            stage()
        text = (self.root / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertRegex(text, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} [+-]\d\d:\d\d")
        for level in ("WARNING", "ERROR"):
            self.assertIn("record-" + level, text)
        for level in ("TRACE", "DEBUG", "INFO"):
            self.assertNotIn("record-" + level, text)
            self.assertNotIn("record-" + level, console.getvalue())
        self.assertIn("record-WARNING", console.getvalue())
        self.assertIn(" | proofread | ", text)

    def test_trace_level_enables_detailed_records_in_both_sinks(self):
        console = io.StringIO()
        with patch("sys.stderr", console), project_run(self.root, "TRACE"):
            log("TRACE", "trace-record")
            log("DEBUG", "debug-record")
        text = (self.root / LOG_FILENAME).read_text(encoding="utf-8")
        for message in ("trace-record", "debug-record"):
            self.assertIn(message, text)
            self.assertIn(message, console.getvalue())

    def test_quiet_configuration_and_internal_arguments_are_removed(self):
        from dataclasses import fields
        from subtitle_translation import bilingual_translation, pipeline, windowed_mfa

        for function in (bilingual_translation.translate_events, t.proofread_target_events, t.build_source_review,
                         t.translate_description, t.build_embedding_index, t.get_scene_changes, windowed_mfa.finish_timing):
            with self.subTest(function=function.__name__):
                self.assertNotIn("quiet", inspect.signature(function).parameters)
        for options in (t.GlossaryBuildOptions, t.CompletionRetryTemplate):
            self.assertNotIn("quiet", [field.name for field in fields(options)])
        self.assertNotIn("quiet", vars(pipeline.build_parser().parse_args(["fixture"])))
        template = Path(t.__file__).parent / "subtitle_translation/examples/.env.example"
        self.assertNotIn("QUIET", template.read_text(encoding="utf-8"))

    def test_nested_stages_share_file_and_next_run_overwrites_it(self):
        with patch("sys.stderr", io.StringIO()):
            with project_run(self.root):
                log("INFO", "first-stage")
                with project_run(self.root):
                    log("INFO", "second-stage")
            text = (self.root / LOG_FILENAME).read_text(encoding="utf-8")
            self.assertEqual(text.count("Run started"), 1)
            self.assertIn("first-stage", text)
            self.assertIn("second-stage", text)
            with project_run(self.root):
                log("INFO", "replacement-run")
        text = (self.root / LOG_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("first-stage", text)
        self.assertIn("replacement-run", text)
        self.assertEqual([p.name for p in self.root.iterdir()], [LOG_FILENAME])

    def test_invalid_level_does_not_truncate_existing_log(self):
        path = self.root / LOG_FILENAME
        path.write_text("previous", encoding="utf-8")
        with self.assertRaises(ValueError), project_run(self.root, "invalid"):
            self.fail("invalid level must not enter the run")
        self.assertEqual(path.read_text(encoding="utf-8"), "previous")

    def test_exception_releases_sink_and_does_not_dump_exception_locals(self):
        fail = Mock(side_effect=RuntimeError("private-exception-body"))
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(RuntimeError), project_run(self.root):
                fail()
            text = (self.root / LOG_FILENAME).read_text(encoding="utf-8")
            self.assertIn("RuntimeError", text)
            self.assertNotIn("private-exception-body", text)
            with project_run(self.root):
                log("INFO", "reopened")
        self.assertIn("reopened", (self.root / LOG_FILENAME).read_text(encoding="utf-8"))

    def test_separate_async_projects_do_not_cross_write_and_thread_context_is_inherited(self):
        async def execute():
            async def run(name):
                directory = self.root / name
                directory.mkdir()
                with project_run(directory):
                    await asyncio.sleep(.01)
                    await asyncio.to_thread(log, "INFO", "marker-" + name)
            await asyncio.gather(run("alpha"), run("beta"))
        with patch("sys.stderr", io.StringIO()):
            asyncio.run(execute())
        for name, other in (("alpha", "beta"), ("beta", "alpha")):
            text = (self.root / name / LOG_FILENAME).read_text(encoding="utf-8")
            self.assertIn("marker-" + name, text)
            self.assertNotIn("marker-" + other, text)

    def test_batch_uses_one_central_file_for_child_projects_and_threads(self):
        async def execute():
            async def run(name):
                directory = self.root / name
                directory.mkdir()
                with project_run(directory):
                    await asyncio.to_thread(log, "INFO", "marker-" + name)
            with project_run(self.root, filename="batch-result.log", capture_children=True):
                await asyncio.gather(run("alpha"), run("beta"))
        with patch("sys.stderr", io.StringIO()):
            asyncio.run(execute())
        text = (self.root / "batch-result.log").read_text(encoding="utf-8")
        self.assertIn(" | alpha | ", text)
        self.assertIn(" | beta | ", text)
        self.assertIn("marker-alpha", text)
        self.assertIn("marker-beta", text)
        self.assertEqual([p.name for p in self.root.rglob("*.log")], ["batch-result.log"])

    def test_overlapping_run_on_same_project_cannot_truncate_active_file(self):
        def overlap():
            with project_run(self.root):
                self.fail("overlapping run must not start")
        with patch("sys.stderr", io.StringIO()), project_run(self.root):
            log("INFO", "active-marker")
            with ThreadPoolExecutor(max_workers=1) as pool, self.assertRaises(ValueError):
                pool.submit(overlap).result()
        self.assertIn("active-marker", (self.root / LOG_FILENAME).read_text(encoding="utf-8"))

    def test_batch_worker_failure_diagnostics_share_the_central_log(self):
        scheduler = AcquisitionScheduler([], ResourceLimits(cpu_io=1), AcquisitionRunners(None, None, None),
                                         failure_log_dir=self.root)
        with patch("sys.stderr", io.StringIO()), project_run(self.root, filename="batch-result.log", capture_children=True):
            scheduler._write_worker_failure_log(BatchTask(1, "fixture"), "asr", RuntimeError("fixture-crash"))
        self.assertEqual(scheduler.failure_log_path, self.root / "batch-result.log")
        text = (self.root / "batch-result.log").read_text(encoding="utf-8")
        self.assertIn("Worker failed", text)
        self.assertIn("fixture-crash", text)
        self.assertEqual([p.name for p in self.root.glob("*.log")], ["batch-result.log"])

    def test_batch_entry_overwrites_central_log_without_creating_video_logs(self):
        video_directory = self.root / "video"
        video_directory.mkdir()
        for marker in ("previous-batch", "current-batch"):
            def acquire(*args, **kwargs):
                with project_run(video_directory):
                    log("INFO", marker)
                return []
            with patch.object(batch_runtime, "load_project_environment", return_value={}), \
                 patch.object(batch_runtime, "run_acquisition", side_effect=acquire), \
                 patch.object(batch_runtime, "write_report"), patch.object(batch_runtime, "emit_task_bell"), \
                 patch.object(batch_runtime.Path, "cwd", return_value=self.root), patch("sys.stderr", io.StringIO()):
                self.assertEqual(batch_runtime.main(["fixture"]), 0)
        text = (self.root / "batch-result.log").read_text(encoding="utf-8")
        self.assertIn("current-batch", text)
        self.assertNotIn("previous-batch", text)
        self.assertFalse((video_directory / LOG_FILENAME).exists())

    def test_translate_log_goes_to_explicit_video_folder_not_configuration_or_json_folder(self):
        config, inputs, videos = [self.root / name for name in ("config", "inputs", "videos")]
        for directory in (config, inputs, videos):
            directory.mkdir()
        source, video = inputs / "film.json", videos / "film.mkv"
        source.write_text("{}", encoding="utf-8")
        video.write_bytes(b"fixture")
        with patch.object(t, "_config_root", return_value=str(config)), patch.object(t, "load_env", return_value={}), \
             patch.object(t, "_run_translate_command", side_effect=lambda *args: log("INFO", "video-log")), \
             patch("sys.stderr", io.StringIO()):
            t.main([str(source), "--video", str(video)])
        self.assertTrue((videos / LOG_FILENAME).is_file())
        self.assertFalse((config / LOG_FILENAME).exists())
        self.assertFalse((inputs / LOG_FILENAME).exists())

    def test_output_path_only_does_not_create_log(self):
        source = self.root / "film.json"
        source.write_text("{}", encoding="utf-8")
        with patch.object(t, "load_env", return_value={}), patch.object(t, "_run_translate_command", return_value=0):
            t.main([str(source), "--only-output-path"])
        self.assertFalse((self.root / LOG_FILENAME).exists())
