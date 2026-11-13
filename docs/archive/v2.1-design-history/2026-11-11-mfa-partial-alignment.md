# MFA Partial Alignment and Native Artifact Recovery

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

Status: implemented on the v2.1.0 development branch; requires PR review before
integration. The default non-MFA path is unchanged.

The next target is [post-translation windowed MFA](2026-11-13-post-translation-windowed-mfa-design.md),
now available experimentally through --mfa. This document describes the legacy partial-result parser;
its structural checks do not establish acoustic position accuracy or validate
the old full-recording segmentation strategy.

## Problem

A completed MFA TextGrid can contain word intervals for only some prepared
utterances. Comparing that flattened word list with the entire adopted draft
discards healthy intervals along with missing ones. A successful command exit is
not sufficient evidence of complete alignment.

## Validation Contract

The tool-environment exporter uses praatio to read both word tiers and original
utterance tiers, including speaker-prefixed tier names. Phone tiers are never
treated as words. The CLI environment does not import MFA or praatio.

The canonical original utterance labels must cover the entire adopted draft
exactly, in order. Character ranges are derived from that complete coverage,
never fuzzy substring matching. Missing, duplicated or reordered labels reject
the mapping globally. Timestamp structure and unique word ownership must also
be valid before local recovery is allowed.

Each utterance then independently requires complete word-text equivalence and
valid ordered timestamps. Missing words, lexical mismatches or unknown-word
labels invalidate that utterance, not its healthy neighbors. The result status
is `available`, `partial` or `unavailable`; diagnostics record total, available
and unavailable utterances and exported/retained word counts. Legacy exports
without utterance metadata retain strict whole-draft validation.

Consecutive healthy intervals are mapped together so contractions crossing an
utterance boundary remain indivisible. Display words retain adopted spelling
and global character ranges. No missing word times are interpolated.

## Downstream Behavior

pySBD still organizes the complete adopted draft into sentence contexts, not MFA
utterances or original ASR segments. A context touching any invalid interval has
no usable word axis and cannot be automatically split or scene-snapped. It keeps
the adopted text, coarse bounds from the verified prepared intervals and an
explicit manual-review requirement. Its translation remains a normal whole
sentence. Healthy contexts use validated words normally.

One failed utterance may intersect several sentences; the unavailable sentence
count therefore need not equal the failed utterance count. A sentence with some
good words is not advertised as fully aligned. `_asr` records alignment status
and diagnostics, and the normalizer identity changes to rebuild old work JSON.

## Native Artifact Reuse

Full result identity includes mapping policy and exporter source hash. Reuse of
a completed native stage may ignore only those two parser-level fields. It must
retain exact audio/text hashes, model files, VAD identity, runtime commands,
profile, version, execution policy and a freshly probed tool environment.
The explicitly recognized pre-policy recipe is `segment-align-cpu-v1`.

Candidates are restricted to the current project's alignment workspace. Eligible
records are available/partial results or the known legacy
`word_mapping_failed` result. Corpus audio and transcript are checked again.
The existing TextGrid is re-exported with the current parser; its word intervals
must equal the old export exactly and the file hash must remain stable. A new
generation stores the revalidated result and copied TextGrid. Old artifacts are
not modified or deleted. Incompatible or corrupt candidates are cache misses.

Available/partial result cache hits still probe and validate the environment and
exported payload. Normal reconstruction can invalidate downstream event and
translation caches; native reuse does not promise zero LLM calls for a normal
translate invocation. Cancellation/admission guards and process timeout handling
remain in force, including during re-export.

## Invariants and Verification

This extends D1/D3/D6/D7 data-source, cache, protocol and responsibility behavior.
It does not change original WhisperX JSON, batch sidecars, locks, generation
publication, concurrency admission, provider choice or the source-editing stage.
Alignment remains timing evidence, not acoustic verification of the transcript.

Regression tests cover partial intervals, unknown/mismatched words, repeated
text, full-draft coverage rejection, contractions across interval boundaries,
manual degradation, partial cache hits and native re-export with provenance
checks. Network, LLM and native tools are mocked in automated tests. Local
TextGrid replay independently verifies mapping and split-boundary validation;
it does not establish translation quality or resolve intervals without words.
