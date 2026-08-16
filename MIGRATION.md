# Migration notes

## Stable first-pass context replaces split-specific neighbor context

The split-stage `context_before` / `context_after` request fields and the
`--split-context-window` CLI option have been intentionally removed. This is a
pipeline boundary change, not an accidental omission:

- first-pass translation now owns cross-segment semantic understanding;
- each translation item receives stable neighboring source segments frozen
  from the complete transcript;
- split only divides the current, already translated complete bilingual
  segment and does not receive ordinary neighboring segments.

Configure the new first-pass window in `.env`:

```ini
TRANSLATE_CONTEXT_WINDOW=1
```

`0` disables explicit ordinary neighbors. `1` (the default) supplies at most
one previous and one next segment; `2` supplies at most two on each side, and
so on. Transcript boundaries truncate naturally. Batch size, recursive batch
recovery, request concurrency, thinking settings and RAG do not change the
frozen neighbor set.

If an older invocation used `--split-context-window`, remove that CLI option
and choose `TRANSLATE_CONTEXT_WINDOW` for first-pass translation instead. The
new setting is not a stage-for-stage equivalent: it moves context reasoning to
translation while deliberately keeping split focused on the current bilingual
segment. Existing local prompt files are never overwritten by setup; compare
them with `translate_prompt.example.md` and adopt the `sentence_context`
guidance when upgrading.
