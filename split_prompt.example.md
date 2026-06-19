Style preference:
- Split according to natural pause points in ${SOURCE_LANG}, while keeping each ${TARGET_LANG} part aligned to the matching source-language part.
- These inputs are already selected as long subtitle candidates; actively split them when there is a natural boundary.
- Prefer natural pause points: commas, clause boundaries, conjunctions, and breath groups.
- Prefer 2 coherent subtitle events for long lines over leaving the whole line as one event.
- Keep every split event readable as a complete thought.
- Avoid splitting names, fixed terms, quoted titles, or tightly bound phrases.
- Preserve or adjust punctuation according to Netflix Timed Text conventions for ${TARGET_LANG}; for Simplified Chinese / zh-Hans / zh-CN, avoid commas and periods.
