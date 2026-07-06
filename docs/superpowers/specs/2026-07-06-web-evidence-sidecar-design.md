# Web Evidence Sidecar Design

**Goal:** Persist Tavily web-search evidence as a project sidecar and index it into the existing embedding memory without polluting `glossary.md`.

## Summary

The pipeline already uses Tavily during glossary generation, but raw web evidence is ephemeral: it is available only inside the current glossary request or as temporary fallback search text. We will add a dedicated `web_evidence` sidecar file that captures normalized Tavily results and lets all later stages retrieve them through the existing Chroma-based memory.

## Design

1. Add a `<base>.web_evidence.json` sidecar path to `TranscriptContext`.
2. Introduce dataclasses for normalized web evidence records and round-trip JSON serialization.
3. Capture Tavily results in both glossary paths:
   - tool-session Tavily calls
   - fallback query-agent Tavily searches
4. Save normalized results to the sidecar even though `glossary.md` remains the only hard prompt rule file.
5. Build `web_evidence:*` embedding chunks from the sidecar and index them alongside:
   - `transcript:*`
   - `glossary:*`
   - `translation_memory:*`
6. Keep retrieval behavior unchanged; once indexed, glossary, description, translate, and proofread automatically gain access through the existing retriever calls.

## Guardrails

- `glossary.md` keeps its role as curated, always-resident prompt context.
- `web_evidence.json` stores structured evidence, not LLM-rewritten conclusions.
- Duplicate URLs should be collapsed before indexing.
- If `glossary.md` is cached but `web_evidence.json` is missing and Tavily is available, regenerate the sidecar without forcing a glossary rewrite.

## Verification

- Add tests for sidecar serialization, glossary-side sidecar persistence, cache backfill behavior, and embedding chunk creation/index clearing.
