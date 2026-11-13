# Scene Detection Without Timeline Beautification

Status: codex/v2.1.0 development implementation. Supersedes earlier display-time
padding and scene-snapping policies; D1/D2/D3/D7 changes require PR review.

Scene detection only reads video and writes .scenes.json / .scenechange.txt.
It never changes words, segments, event times, text, IDs or translations. The
MFA_ENABLED=0 compatibility path uses ASR times; template-default MFA_ENABLED=1 uses native windowed alignment.
Coarse-overlap coordination and removal of stale root word_segments remain.

Remove --snap-frames, --end-offset-frames, --min-duration, --min-gap,
--max-gap-merge, --aggressive and --no-scene-snap, including implementations and
defaults. Replace --only-beautify with --only-scenes and control skipping via
PIPELINE_SKIP_SCENE_DETECTION. Replace PIPELINE_SKIP_BEAUTIFY with
PIPELINE_SKIP_SCENE_DETECTION; existing local configuration is not overwritten.
Detector tuning uses SCENE_THRESHOLD and SCENE_MIN_INTERVAL_FRAMES in .env;
there are no scene tuning or skip CLI flags. Only-scenes requires video,
overrides the pipeline skip setting, and calls no LLM/MFA.

Detection caches bind video path/size/mtime and detector settings. Both sidecars
must exist for a hit. Detector changes refresh only the sidecars, never language
or timestamp caches. The .beautified.json working filename remains solely for
batch recovery compatibility; it no longer implies timestamp modification.

The default direct-ASR timing policy rebuilds old retimed derived JSON from its
original input. MFA uses coarse-output-scene-only-v3 to discard old display
projections while retaining semantic/native corpus caches. No native words are
scaled or shifted; there is no scene-derived subtitle boundary adjustment.

Tests mock external dependencies and cover unchanged times/content, detector
cache refresh, skip/only modes, batch forwarding and removed-option rejection.
Original media, ASR inputs, generation sidecars, resource pools and prompt
examples remain unchanged.
