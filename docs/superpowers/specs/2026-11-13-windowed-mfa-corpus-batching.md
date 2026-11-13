# Windowed MFA Corpus Batching

Status: implemented on codex/v2.1.0, enabled by MFA_ENABLED=1. Requires PR
review before integration. This extends D3/D6/D7 cache, protocol and process
ownership contracts without changing source editing or semantic stage order.

## Execution

Windowed audio does not require a separate MFA process per window. Prepare each
eligible window as its own canonical WAV and adopted-text .lab in one project
corpus. Run one managed `mfa align` with `num_jobs=1`, then one tool-environment
exporter invocation for all requested TextGrids. Model setup, feature/adaptation
workflow and environment discovery are shared across the corpus.

Every window keeps a deterministic hexadecimal ID and local time origin. The
exporter reads praatio word tiers by requested ID, not filesystem order. The
caller applies the original sample offset once and retains existing word/text,
range, gap and event-boundary checks. Batching does not concatenate the clips
into a full-recording utterance, enlarge source windows or add a parallel pool.

## Failure and Cache Boundaries

Missing, ambiguous or malformed TextGrids generate local unavailable results;
other valid windows remain usable. Unknown/duplicate/missing exported item IDs
or version/environment mismatch are global protocol failures. Native command
failure/timeout affects the corpus as a whole; do not silently restart the
expensive pipeline once per failed window. Cancellation/admission closure
propagates and cannot publish a fallback cache.

The `direct-window-corpus-align-v1` identity includes the complete ordered window
set, each ID/audio/text/duration, runtime/model/dictionary and exporter policy.
Fresh environment/package/VAD probing remains mandatory. Cache hits require a
matching manifest and result digest, independently revalidated exported words,
and unchanged input/model snapshots. A completed native corpus may cache local
unavailable windows; command/protocol failure does not publish a success pointer.

Shared speaker adaptation changes the dependency unit: changing a window or
model invalidates the corpus, not only that window. Do not mix old independently
adapted results with a new corpus. Old files remain local and are not deleted.
Text-event planning and translation identities are unchanged, so migration does
not require new LLM calls for otherwise valid text checkpoints.

## Verification

Mocked regressions cover one align/export for multiple windows, probe-only cache
hits, local bad-window isolation, exact ID coverage, changed models, corrupt
cache digests, input mutation, cancellation and original sample-offset mapping.
Real WSL validation uses four separate inputs copied from the same public short
sample: 108 validated words, one align invocation, about 35.7 seconds including
probe/export, and about 1.7 seconds for a cache hit. This validates execution and
cache structure, not diverse acoustic quality or a universal speedup factor.

Full-length benchmarks must report cold versus warm caches, audio/window count,
coarse fallbacks and alignment coverage as well as time. Do not hide slow
initialization by removing project budgets or claiming invalid timestamps as
successful alignment.
