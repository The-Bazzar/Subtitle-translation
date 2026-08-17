# Task Completion Bells Design

## Goal

Add distinct success and error sounds to the PowerShell and Linux/WSL pipeline and batch entry points without adding runtime dependencies.

## Scope

- `pipeline.ps1` and `pipeline.sh` notify after a real task finishes or exits with an error.
- `batch.ps1` and `batch.py` notify once after the aggregate batch result is known.
- A pipeline launched by a batch suppresses its own sounds. The batch runner emits one error sound as each failed result arrives, including timeout and launch failures. Three failed child pipelines therefore produce three error sounds, followed by one aggregate batch error sound.
- Help and dry-run paths do not notify.
- Any batch containing a failed task exits nonzero.

## Sound Design

PowerShell uses the built-in .NET console beep API. Success is a short ascending two-tone pattern; error is a longer descending two-tone pattern.

Linux/WSL uses the terminal BEL character with different cadences because it is available without an audio package. Success is two short bells; error is three slower bells. Audible output depends on the terminal's bell setting. Notification failures are best-effort and never replace the task's original exit code.

## Process Coordination

Batch runners set the process-local `PIPELINE_BATCH_CHILD=1` marker for child pipelines. This marker is not a user configuration variable and is not read from `.env`. PowerShell child pipelines also return an internal `__PIPELINE_BATCH_EXIT__=<code>` output marker because runspace `EndInvoke()` does not expose a script's `exit` code; `batch.ps1` consumes this marker before recording the aggregate result.

Pipeline notification rules:

| Invocation | Success | Error |
|---|---|---|
| Standalone pipeline | success sound | error sound |
| Batch child pipeline | silent | batch runner emits one error sound |

Batch notification rules:

| Aggregate result | Sound | Exit code |
|---|---|---:|
| All tasks succeed | success sound | 0 |
| Any task fails | error sound | 1 |

## Error Handling

Each script routes real task completion through one notification boundary. The boundary receives or observes the original exit code, attempts the appropriate sound, and preserves that code. PowerShell sound calls are wrapped so unsupported hosts cannot turn a successful task into a failure. Bash exit handling uses a single `EXIT` trap whose notification flag is disabled before configuration loading when help was requested.

## Testing

Script contract tests will assert:

- both sound patterns exist in PowerShell and Linux/WSL implementations;
- help and dry-run paths remain silent;
- batch runners mark child pipelines;
- child success is suppressed while child failure remains audible;
- batch aggregate failure produces an error sound and nonzero exit status;
- the PowerShell and Linux/WSL entry points expose matching behavior.

Network, video, LLM, and audio-device access are not used by tests.

## Design Alignment

This change preserves D1-D12 from `DISCIPLINE.md`. It does not alter pipeline stages, caches, JSON contracts, prompt files, media processing, or language behavior. `PIPELINE_BATCH_CHILD` is internal process coordination rather than persistent configuration, so setup and `.env` migration are unchanged.
