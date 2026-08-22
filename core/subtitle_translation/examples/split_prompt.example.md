Style preference:
- Split according to natural pause points in ${SOURCE_LANG}, while keeping each ${TARGET_LANG} part aligned to the matching source-language part.
- These inputs are already selected as long subtitle candidates; actively split them when there is a natural boundary.
- Prefer natural pause points: commas, clause boundaries, conjunctions, and breath groups.
- Use as many split parts as the sentence naturally needs. There is no two-part limit; long multi-clause segments may become 3, 4, 5, or more subtitle events.
- Prefer coherent subtitle events over tiny fragments, but do not keep a long multi-clause segment under-split just to avoid more than two parts.
- Keep every split event readable as a complete thought.
- For the source-language array, split by copying exact contiguous spans from the input source text. Do not correct, remove, add, or paraphrase source-language words.
- Avoid splitting names, fixed terms, quoted titles, or tightly bound phrases.
- Preserve or adjust punctuation according to Netflix Timed Text conventions for ${TARGET_LANG}; for Simplified Chinese / zh-Hans / zh-CN, avoid commas and periods.
