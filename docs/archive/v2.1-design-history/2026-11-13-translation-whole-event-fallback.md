# Whole-Event Fallback for Translation Reconstruction Mismatch

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

Status: development implementation on codex/v2.1.0; requires PR review before
integration. The response schema and provider prompt examples are unchanged.

The [post-translation windowed MFA target](2026-11-13-post-translation-windowed-mfa-design.md)
is now implemented experimentally through --mfa. It keeps whole-event fallback
before alignment and defers precise times until MFA; the existing-time behavior
below still describes the default non-MFA path.

## Scope

The translate response still supplies a whole target sentence and every planned
target event. Validate structure, IDs and nonempty strings before testing whether
the ordered event parts reconstruct the whole target text. An otherwise valid
response with only a reconstruction mismatch is accepted as a whole sentence,
not retried or treated as a translation failure.

Keep the complete source sentence and the whole translation from that same
response. Discard its target fragments and replace the sentence's event list with
one event using the existing segment start/end and full segment-local source
range. Do not edit source text, word timing, adopted draft or upstream artifacts.
The replacement event gets the deterministic whole-event ID for the source plan.

The resulting split status is `unsplit`. Clear split reason/detail for this
replacement and do not add a manual-review flag or fallback diagnostic to JSON.
Emit one console Warning identifying the sentence and explaining that it remains
unsplit; the warning is visible even with quiet progress output. Existing
upstream provenance/manual-review metadata is not removed.

## Validation Boundaries

Missing or malformed fields, missing/unknown/duplicate IDs, empty whole or event
translations, and attempted source/time edits retain bounded retry and failure
behavior. A valid whole string alone does not authorize accepting an otherwise
malformed response. No partial translation from another response is reused.

Target-only proofreading keeps its no-event-edit contract. A reconstruction
mismatch there leaves the valid pre-proofreading translation and events intact;
it does not merge events. A whole fallback can subsequently be proofread normally
as one event.

## Cache and Publication

Preserve the source planning identity so an unchanged source-split invocation can
reuse the valid whole event instead of splitting it again. Compute translation
identity using the effective whole-event input, not the discarded multipart
request. Keep context, languages, actual retrieval evidence, model and prompt in
that identity. Changed planning inputs or explicit work-cache reconstruction
still trigger normal replanning.

Existing valid multipart caches need not be invalidated. The whole event and
whole translation pass the original strict cache validator and serialize with
the existing schema. Clear the old proofreading key when accepting translation.
Checkpoint the source/target event replacement and translation together, then
continue later sentences/batches. Export one bilingual event at the original
sentence bounds; long subtitles remain available for final human editing.

Cancellation/admission checks run before publication. Do not mutate a sentence
inside the response validator or convert cancellation into a whole fallback.
Batch locks, candidate publication, generation sidecars and resource admission
are unchanged.

## Invariants and Verification

This is a bounded exception to the fixed-event translation policy, affecting
D2/D3/D6/D7 execution, work-cache, response and responsibility contracts. It does
not introduce another translation/split request or change ASR/MFA behavior.

Mocked regressions cover immediate fallback, Chinese boundary spaces, unchanged
source/timing, log-only diagnostics, missing/invalid content rejection, whole
event cache roundtrips including high-precision times, changed-plan invalidation,
later-batch continuation, proofreading, cancellation and single-event bilingual
ASS export. Live LLM output quality is outside these structural checks.
