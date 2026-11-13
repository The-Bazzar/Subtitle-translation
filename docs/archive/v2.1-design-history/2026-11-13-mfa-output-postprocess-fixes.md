# MFA Output Projection and Display Timing Fixes

> 历史设计归档，不作为当前运行说明；当前规范见 docs/v2.1-architecture.md。

Status: codex/v2.1.0 development implementation. Extends D1/D3/D6/D7 output,
derived-cache and responsibility contracts; requires PR review before release.
Native corpus execution, source editing and semantic translation are unchanged.

Update: display padding/scene snapping and their parameters have since been
removed entirely. Only coarse-overlap grouping and root-word cleanup below
remain active; see [scene detection only](../../superpowers/specs/2026-11-13-scene-detection-only.md).
The old display-bounds policy is superseded by coarse-output-scene-only-v3.

## Overlapping Coarse Output

Several sentence contexts may share the same ASR source interval. When native
alignment fails, emitting one full-interval subtitle per context overlaps the
display. The timed output now merges overlapping neighboring contexts whenever
at least one is coarse. Backtracking handles chained overlaps, including an
aligned context covered by a neighboring coarse envelope.

The merged context preserves the exact adopted source span, ordered whole
translations separated by a space, source references and the time envelope. It
has one coarse event, no fabricated words, deterministic composite identities
and a cleared proofreading key. The immutable text checkpoint is not changed,
no extra LLM request is made, and nonoverlapping neighbors remain independent.
The merge is logged, not written as a human-facing reason in subtitle text.

## One Word-Axis Representation

Derived subtitle saves omit the legacy root `word_segments` array; only
`segments[].words[]` describes the working axis. MFA source normalization also
removes the copied array. Compatible old text checkpoints are cleaned on load,
and default derived-cache hits migrate the redundant field without discarding
valid translations. Original ASR files and in-memory input snapshots are not
rewritten. Legacy MFA normalization has the same output cleanup.

## Display Bounds, Not Word Retiming

Post-MFA beautify implements min_duration, min_gap and max_gap_merge on subtitle
display bounds. Scene snapping cannot trim spoken words. Short free gaps may be
closed up to min_gap; short events may extend into available preceding/following
time, bounded by neighboring displays, scene cuts and media duration. Display
padding may exceed the original coarse search range; native word times remain
inside the aligned source window and are never scaled or shifted.

Gap enforcement may remove display padding, never speech. Coarse events are not
retimed. If the requested duration/separation cannot be achieved safely, keep
the sound-backed timing and log aggregate unmet counts rather than fabricating
time, moving words, resplitting text or invoking the model.

The coarse-groups-display-bounds-v2 postprocess identity invalidates only old
timed projections. Unchanged semantic checkpoints and native corpus caches
remain reusable. Regression tests cover shared failed intervals, independent
neighbors, exact bilingual preservation, legacy JSON cleanup, checkpoint reuse,
display extension, gap merging, impossible spacing, scene/media limits and word
immutability.
