# Batch Resource Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace whole-pipeline batch parallelism with a process-local, stage-aware scheduler that enforces CPU/IO, NVENC, ASR, alignment, and burn ordering while keeping task failures isolated.

**Architecture:** `batch.py` becomes the sole scheduler entry, backed by focused scheduler and Whisper worker modules. PowerShell and bash use one shared `py_launcher` whitelist, download and edit-video preparation become separate scripts, and the scheduler runs strict acquisition, ASR, alignment, post-processing, and burn waves.

**Tech Stack:** Python 3.10+, `asyncio`, `concurrent.futures`, `multiprocessing` with `spawn`, PowerShell 7, bash, ffmpeg, WhisperX, `unittest`.

---

## Delivery Rules

- Implement each task as a separate PR based on the latest `main` after the previous PR merges.
- Create the required `[design-change]` Issue before Task 2 because that task changes the explicit pipeline stage graph.
- Do not modify `*_prompt.example.md`.
- Run the complete unittest suite before every PR handoff.
- Preserve task notification semantics and PowerShell/bash parity in every intermediate state.

### Task 1: Shared Python Launcher

**Files:**
- Create: `py_launcher.ps1`
- Create: `py_launcher.sh`
- Modify: `translate_srt.ps1`
- Modify: `translate_srt.sh`
- Modify: `merge_ass.ps1`
- Modify: `merge_ass.sh`
- Modify: `tests/test_project_launchers.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Replace launcher tests with whitelist expectations**

Add tests that assert the shared launchers map only the three approved targets and that the existing wrappers delegate to them:

```python
def test_shared_launchers_whitelist_python_targets(self):
    powershell = read_script("py_launcher.ps1")
    shell = read_script("py_launcher.sh")
    for target, script in (
        ("translate_srt", "translate_srt.py"),
        ("merge_ass", "merge_ass.py"),
        ("batch", "batch.py"),
    ):
        self.assertIn(target, powershell)
        self.assertIn(script, powershell)
        self.assertIn(target, shell)
        self.assertIn(script, shell)

def test_existing_wrappers_delegate_to_shared_launcher(self):
    for wrapper, target in (
        ("translate_srt.ps1", "translate_srt"),
        ("merge_ass.ps1", "merge_ass"),
    ):
        content = read_script(wrapper)
        self.assertIn("py_launcher.ps1", content)
        self.assertIn(target, content)
    for wrapper, target in (
        ("translate_srt.sh", "translate_srt"),
        ("merge_ass.sh", "merge_ass"),
    ):
        content = read_script(wrapper)
        self.assertIn("py_launcher.sh", content)
        self.assertIn(target, content)
```

- [ ] **Step 2: Run the launcher tests and verify failure**

Run:

```powershell
uv run python -m unittest tests.test_project_launchers -v
```

Expected: FAIL because `py_launcher.ps1/.sh` do not exist and wrappers still contain duplicated venv resolution.

- [ ] **Step 3: Create the PowerShell launcher**

Implement a strict target map and preserve the existing interpreter override behavior:

```powershell
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('translate_srt', 'merge_ass', 'batch')]
    [string] $Target,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]] $PythonArgs
)

$targets = @{
    translate_srt = 'translate_srt.py'
    merge_ass = 'merge_ass.py'
    batch = 'batch.py'
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$configuredPython = $env:PYTHON_PATH_WIN
if (-not $configuredPython -and (Test-Path (Join-Path $PSScriptRoot '.env.ps1'))) {
    . (Join-Path $PSScriptRoot '.env.ps1')
    $configuredPython = Get-EnvValue 'PYTHON_PATH_WIN' ''
}
if ($configuredPython) { $python = $configuredPython }
if (-not (Test-Path $python -PathType Leaf)) {
    Write-Error "Python executable not found: $python. Run setup.ps1 first."
    exit 1
}
$script = Join-Path $PSScriptRoot $targets[$Target]
& $python $script @PythonArgs
exit $LASTEXITCODE
```

- [ ] **Step 4: Create the bash launcher**

Use a `case` whitelist and the current `.env` interpreter lookup:

```bash
target="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
case "$target" in
  translate_srt) script_name="translate_srt.py" ;;
  merge_ass) script_name="merge_ass.py" ;;
  batch) script_name="batch.py" ;;
  *) echo "Error: unsupported Python target: $target" >&2; exit 2 ;;
esac
exec "$PYTHON_BIN" "$SCRIPT_DIR/$script_name" "$@"
```

- [ ] **Step 5: Reduce existing wrappers to delegation**

PowerShell wrappers call:

```powershell
& (Join-Path $PSScriptRoot 'py_launcher.ps1') translate_srt @PythonArgs
exit $LASTEXITCODE
```

Bash wrappers call:

```bash
exec bash "$SCRIPT_DIR/py_launcher.sh" translate_srt "$@"
```

Use the corresponding `merge_ass` target in merge wrappers. Do not migrate `batch.ps1` yet because its CLI conversion belongs with Task 3.

- [ ] **Step 6: Run targeted and full tests**

Run:

```powershell
uv run python -m unittest tests.test_project_launchers -v
uv run python -m unittest discover -s tests
```

Expected: launcher tests PASS; full suite reports `OK`.

- [ ] **Step 7: Commit Task 1**

```powershell
git add py_launcher.ps1 py_launcher.sh translate_srt.ps1 translate_srt.sh merge_ass.ps1 merge_ass.sh tests/test_project_launchers.py README.md AGENTS.md
git commit -m "refactor(cli): share project python launcher"
```

### Task 2: Split Download and Edit Preparation

**Files:**
- Create: `prepare-video.ps1`
- Create: `prepare-video.sh`
- Modify: `download.ps1`
- Modify: `download.sh`
- Modify: `pipeline.ps1`
- Modify: `pipeline.sh`
- Modify: `tests/test_download_pipeline_scripts.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Create the design-change Issue**

Use title:

```text
[design-change] split download and edit-video preparation
```

The Issue must identify D2 and D8, preserve `OUTPUT_RENDER_VIDEO` and `OUTPUT_VIDEO`, describe migration for direct download users, and link the approved design spec.

- [ ] **Step 2: Rewrite script contract tests first**

Replace the combined download expectations with separate contracts:

```python
def test_download_scripts_only_emit_render_video(self):
    for script in ("download.ps1", "download.sh"):
        content = read_script(script)
        self.assertIn("OUTPUT_RENDER_VIDEO=", content)
        self.assertNotIn("OUTPUT_VIDEO=", content)
        self.assertNotIn("h264_nvenc", content)

def test_prepare_scripts_emit_edit_video_and_keep_encoder_policy(self):
    for script in ("prepare-video.ps1", "prepare-video.sh"):
        content = read_script(script)
        self.assertIn("OUTPUT_VIDEO=", content)
        self.assertIn("h264_nvenc", content)
        self.assertIn("libx264", content)
        self.assertIn("aresample=async=1:out_sample_fmt=s16", content)
```

Add assertions that pipeline invokes prepare after download and parses both markers.

- [ ] **Step 3: Run the tests and verify failure**

```powershell
uv run python -m unittest tests.test_download_pipeline_scripts -v
```

Expected: FAIL because prepare scripts do not exist and download still performs reencoding.

- [ ] **Step 4: Move reencoding code without behavioral changes**

Move encoder detection, ffmpeg argument construction, nonzero-NVENC-output handling, and edit-path creation from each download script into its matching prepare script. The new scripts accept exactly one original video path and print exactly one success marker:

```text
OUTPUT_VIDEO=<absolute edit mkv path>
```

Keep the current CPU decode, `h264_nvenc`/`libx264` choice, FLAC audio, metadata removal, and nonempty-output success rule.

- [ ] **Step 5: Remove prepare behavior from download**

Download scripts stop after metadata and original-media handling, then print:

```text
OUTPUT_RENDER_VIDEO=<absolute original path>
```

They must not invoke prepare scripts or create edit `.mkv` files.

- [ ] **Step 6: Add the explicit pipeline step**

Both pipelines parse `OUTPUT_RENDER_VIDEO`, invoke the platform prepare script with that path, parse `OUTPUT_VIDEO`, and use the edit path for Whisper/translation while retaining the original path for burn.

- [ ] **Step 7: Run targeted and full tests**

```powershell
uv run python -m unittest tests.test_download_pipeline_scripts -v
uv run python -m unittest tests.test_burn_scripts -v
uv run python -m unittest discover -s tests
```

Expected: all tests PASS and the full suite reports `OK`.

- [ ] **Step 8: Commit Task 2**

```powershell
git add prepare-video.ps1 prepare-video.sh download.ps1 download.sh pipeline.ps1 pipeline.sh tests/test_download_pipeline_scripts.py README.md AGENTS.md
git commit -m "refactor(download): separate edit video preparation"
```

### Task 3: Stage-aware Batch Scheduler Core

**Files:**
- Create: `batch_scheduler.py`
- Create: `batch.sh`
- Modify: `batch.py`
- Modify: `batch.ps1`
- Modify: `tests/test_task_notifications.py`
- Create: `tests/test_batch_scheduler.py`
- Modify: `tests/test_project_launchers.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write scheduler capacity and state tests**

Start with these public types and invariants:

```python
from batch_scheduler import BatchTask, ResourceLimits, TaskState

def test_resource_limits_are_automatic():
    assert ResourceLimits.detect(logical_cpus=32).cpu_io == 8
    assert ResourceLimits.detect(logical_cpus=2).cpu_io == 1
    assert ResourceLimits.detect(logical_cpus=None).cpu_io == 1
    assert ResourceLimits.detect(logical_cpus=32).nvenc == 4

def test_task_failure_is_terminal_without_failing_other_tasks():
    first = BatchTask(index=1, url="bad")
    second = BatchTask(index=2, url="good")
    first.fail(stage="download", detail="network error")
    assert first.state is TaskState.FAILED
    assert second.state is TaskState.PENDING
```

Add a CLI test asserting `-j`, `--jobs`, `--io-jobs`, and PowerShell `MaxJobs` are absent.

- [ ] **Step 2: Run the new tests and verify failure**

```powershell
uv run python -m unittest tests.test_batch_scheduler -v
```

Expected: FAIL because `batch_scheduler.py` does not exist.

- [ ] **Step 3: Implement scheduler domain types**

Define:

```python
class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    BLOCKED_BY_WORKER_FAILURE = "blocked_by_worker_failure"

@dataclass(frozen=True)
class ResourceLimits:
    cpu_io: int
    nvenc: int = 4

    @classmethod
    def detect(cls, logical_cpus: int | None = None) -> "ResourceLimits":
        count = logical_cpus if logical_cpus is not None else os.cpu_count()
        return cls(cpu_io=max(1, (count or 1) // 4))
```

`BatchTask` stores URL, index, stage, state, output paths, error detail, and timestamps. State-changing methods reject transitions from terminal states.

- [ ] **Step 4: Implement the acquisition scheduler with injected runners**

Use injected async callables for download, prepare, and audio extraction so tests never launch real tools. Download uses an `asyncio.Semaphore(cpu_io)`, prepare uses `asyncio.Semaphore(4)`, and extraction uses the CPU/IO semaphore.

Do not load Whisper in this task. The scheduler should stop after successful WAV preparation and return task states.

- [ ] **Step 5: Replace whole-pipeline batch execution**

Remove `ThreadPoolExecutor` calls that launch `pipeline.sh`. `batch.py` constructs the scheduler and stage runners for the current platform. Remove all jobs arguments and print detected capacities at startup.

- [ ] **Step 6: Migrate batch wrappers to the shared launcher**

`batch.ps1` and new `batch.sh` become argument-forwarding wrappers around:

```text
py_launcher batch
```

Update `batch.py` CLI so the existing URL, burn, report, dry-run, provider, and model behaviors remain representable without PowerShell-only scheduling code.

- [ ] **Step 7: Preserve notification behavior**

Update notification tests so each terminal task failure rings once and aggregate completion rings once. Remove assertions for `PIPELINE_BATCH_CHILD` and `__PIPELINE_BATCH_EXIT__`; batch no longer launches child pipelines.

- [ ] **Step 8: Run tests and commit**

```powershell
uv run python -m unittest tests.test_batch_scheduler -v
uv run python -m unittest tests.test_task_notifications -v
uv run python -m unittest tests.test_project_launchers -v
uv run python -m unittest discover -s tests
git add batch.py batch_scheduler.py batch.ps1 batch.sh tests/test_batch_scheduler.py tests/test_task_notifications.py tests/test_project_launchers.py README.md AGENTS.md
git commit -m "feat(batch): add stage aware resource scheduler"
```

Expected: full suite reports `OK`.

### Task 4: Persistent ASR Worker and Recovery Sidecar

**Files:**
- Create: `whisper_worker.py`
- Create: `batch_cache.py`
- Modify: `batch_scheduler.py`
- Create: `tests/test_whisper_worker.py`
- Modify: `tests/test_batch_scheduler.py`
- Modify: `whisper.ps1`
- Modify: `whisper.sh`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write worker protocol and cache tests**

Define tests against these protocol values:

```python
class WorkerCommand(str, Enum):
    LOAD_ASR = "load_asr"
    TRANSCRIBE = "transcribe"
    UNLOAD_ASR = "unload_asr"
    SHUTDOWN = "shutdown"
```

Test that one ASR load serves multiple transcription commands, each task writes through a temporary file, an invalid fingerprint is ignored, and an unexpected worker exit is reported without restart.

- [ ] **Step 2: Run tests and verify failure**

```powershell
uv run python -m unittest tests.test_whisper_worker -v
```

Expected: FAIL because worker and cache modules do not exist.

- [ ] **Step 3: Implement `.asr.json` fingerprint support**

Create immutable fingerprint data containing edit-video path, size, mtime, Whisper model, compute type, source language, and ASR options. Write JSON to a sibling temporary file, flush and close it, then replace `<base>.asr.json` atomically.

- [ ] **Step 4: Implement the spawned worker process**

Use `multiprocessing.get_context("spawn")`. The worker imports WhisperX inside the child process, loads ASR exactly once per wave, receives path-only commands, catches task exceptions into structured results, and exits nonzero on an uncaught process-level exception.

- [ ] **Step 5: Add audio extraction scheduling**

Move WAV extraction outside the worker lease. Reuse the same ffmpeg arguments as current whisper scripts and submit only completed WAV paths to ASR.

- [ ] **Step 6: Integrate the ASR wave**

The scheduler waits until acquisition tasks are terminal, starts one worker, queues all valid uncached WAV tasks, writes `.asr.json`, unloads ASR once, and retains the worker process for Task 5 alignment support.

- [ ] **Step 7: Run tests and commit**

```powershell
uv run python -m unittest tests.test_whisper_worker -v
uv run python -m unittest tests.test_batch_scheduler -v
uv run python -m unittest discover -s tests
git add whisper_worker.py batch_cache.py batch_scheduler.py whisper.ps1 whisper.sh tests/test_whisper_worker.py tests/test_batch_scheduler.py README.md AGENTS.md
git commit -m "feat(whisper): add persistent batch asr worker"
```

### Task 5: Alignment Wave and Post-processing Pipeline

**Files:**
- Modify: `whisper_worker.py`
- Modify: `batch_scheduler.py`
- Modify: `tests/test_whisper_worker.py`
- Modify: `tests/test_batch_scheduler.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Add alignment protocol tests**

Extend `WorkerCommand` expectations with:

```python
LOAD_ALIGN = "load_align"
ALIGN = "align"
UNLOAD_ALIGN = "unload_align"
```

Test source-language grouping, one active alignment command, model reuse within a language group, model replacement between groups, `.asr.json` deletion only after final JSON succeeds, and immediate post-processing admission after each aligned task.

- [ ] **Step 2: Implement alignment commands**

The worker unloads ASR before loading alignment. It loads one language alignment model at a time, reads `.asr.json`, writes the final WhisperX-compatible `.json` atomically, and returns the detected language and output path.

- [ ] **Step 3: Implement grouped alignment scheduling**

Sort successful ASR tasks by detected ISO language while preserving original task order inside each group. Submit one task at a time. As soon as one alignment succeeds, enqueue that task's beautify/glossary/translate runner under the CPU/IO semaphore.

- [ ] **Step 4: Close the worker after alignment terminal states**

After all alignment tasks succeed or fail, issue unload and shutdown commands, join the worker, and set an explicit `worker_released` event. Burn scheduling must depend on this event.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run python -m unittest tests.test_whisper_worker -v
uv run python -m unittest tests.test_batch_scheduler -v
uv run python -m unittest discover -s tests
git add whisper_worker.py batch_scheduler.py tests/test_whisper_worker.py tests/test_batch_scheduler.py README.md AGENTS.md
git commit -m "feat(batch): pipeline alignment and translation stages"
```

### Task 6: Burn Wave, Failure Drain, and Real-time Logs

**Files:**
- Modify: `batch_scheduler.py`
- Modify: `batch.py`
- Modify: `tests/test_batch_scheduler.py`
- Modify: `tests/test_task_notifications.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write burn barrier and worker-failure tests**

Test that burn never starts while `worker_released` is false, no more than four burns overlap, worker exit closes admission, worker-dependent tasks become `blocked_by_worker_failure`, and already aligned tasks continue through translation and burn.

- [ ] **Step 2: Write serialized log tests**

Inject interleaved output events and assert complete lines are emitted in queue order with prefixes such as `[02][prepare]`. Assert the worker crash log is created under the invocation `Path.cwd()` and contains task, queue, exit-code, traceback, stdout, and stderr sections.

- [ ] **Step 3: Implement burn scheduling**

Wait for `worker_released`, then submit ready ASS tasks through the four-slot NVENC semaphore. Tasks whose translation finishes later may join the same burn executor without waiting for all translations.

- [ ] **Step 4: Implement worker failure draining**

On unexpected worker exit, close admission, mark current and worker-dependent tasks, keep downstream futures alive, permit their later burn work, write a timestamped diagnostic log, and return aggregate exit code `1` after drain completion.

- [ ] **Step 5: Implement two-stage interruption**

The first interrupt closes admission and stage advancement while awaiting active external commands. A second interrupt terminates current child process trees and preserves completed outputs and `.asr.json` files.

- [ ] **Step 6: Implement the terminal event queue**

Read stdout and stderr asynchronously, convert each complete line into a `LogEvent(task_index, stage, stream, text)`, and let one printer coroutine own terminal writes. Strip color codes only when writing raw failure logs.

- [ ] **Step 7: Run tests and commit**

```powershell
uv run python -m unittest tests.test_batch_scheduler -v
uv run python -m unittest tests.test_task_notifications -v
uv run python -m unittest discover -s tests
git add batch_scheduler.py batch.py tests/test_batch_scheduler.py tests/test_task_notifications.py README.md AGENTS.md
git commit -m "feat(batch): finalize gpu waves and failure drain"
```

### Task 7: Cross-platform Contract and Release Readiness

**Files:**
- Modify: `tests/test_download_pipeline_scripts.py`
- Modify: `tests/test_project_launchers.py`
- Modify: `tests/test_setup_scripts.py`
- Modify: `README.md`
- Modify: `AGENTS.md`
- Create: `MIGRATION.md`

- [ ] **Step 1: Add complete PowerShell/bash parity assertions**

Assert matching script names, `OUTPUT_*` markers, launcher targets, exit handling, help behavior, download/prepare separation, worker-independent standalone pipeline order, and absence of jobs parameters.

- [ ] **Step 2: Document migration**

Document that direct `download.*` calls no longer create edit `.mkv`, show the new `prepare-video.*` command, explain automatic capacities, `.asr.json` recovery, and the new batch launcher behavior.

- [ ] **Step 3: Run static and full validation**

```powershell
git diff --check
uv run python -m unittest discover -s tests
```

Expected: full suite reports `OK` with no test failures or errors.

- [ ] **Step 4: Perform Windows smoke validation**

Use mocked or tiny local media to verify:

```text
download -> prepare-video -> extract WAV -> ASR -> alignment -> translate -> burn
```

Record the command, exit code, peak NVENC concurrency, Whisper model load count, and output paths in the PR description.

- [ ] **Step 5: Perform WSL smoke validation**

Run the matching bash entry with the same fixture and record equivalent evidence. Any platform behavior mismatch blocks merge.

- [ ] **Step 6: Commit documentation and parity tests**

```powershell
git add tests/test_download_pipeline_scripts.py tests/test_project_launchers.py tests/test_setup_scripts.py README.md AGENTS.md MIGRATION.md
git commit -m "docs(batch): document staged resource scheduling"
```

## Final Verification

- [ ] Confirm each implementation PR contains one independently reversible logical change.
- [ ] Confirm all review threads are resolved before entering merge queue.
- [ ] Confirm no `.env`, provider config, cookies, prompts, generated subtitle, media, or local runtime log is staged.
- [ ] Confirm the final branch contains no `MaxJobs`, `--jobs`, `--io-jobs`, RunspacePool batch scheduler, or whole-pipeline ThreadPool execution.
- [ ] Confirm the final runtime enforces prepare NVENC, Whisper, and burn NVENC as strict waves.
