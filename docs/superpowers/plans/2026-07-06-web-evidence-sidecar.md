# Web Evidence Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Tavily search evidence into a dedicated sidecar and make it retrievable from the existing embedding memory in every downstream stage.

**Architecture:** Extend the existing glossary/Tavily pipeline with a normalized sidecar layer, then treat that sidecar as another embedding source. Keep glossary prompt injection unchanged and use retrieval for the new evidence layer.

**Tech Stack:** Python, unittest/pytest, LangChain Chroma, existing Tavily/OpenAI-compatible SDK integration

---

### Task 1: Add tests for sidecar persistence and indexing

**Files:**
- Modify: `tests/test_json_protocol.py`

- [ ] Add failing tests that assert:
  - `TranscriptContext` exposes a `<base>.web_evidence.json` path
  - normalized web evidence can round-trip through JSON
  - glossary generation writes the sidecar from Tavily results
  - cached glossary can backfill a missing sidecar
  - embedding indexing includes `web_evidence:*` chunks and clears them on rebuild

### Task 2: Implement the sidecar model and Tavily capture

**Files:**
- Modify: `translate_srt.py`

- [ ] Add web-evidence dataclasses and JSON helpers
- [ ] Save normalized Tavily results from tool-session and fallback glossary paths
- [ ] Backfill the sidecar when glossary cache exists but Tavily evidence is missing

### Task 3: Index web evidence into memory

**Files:**
- Modify: `translate_srt.py`

- [ ] Add `web_evidence:*` chunk construction
- [ ] Include those chunks in `build_embedding_index()`
- [ ] Ensure rebuild cleanup recognizes `web_evidence:*` ids

### Task 4: Update docs

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`

- [ ] Document the new `<base>.web_evidence.json` artifact and explain that it is a retrievable evidence layer distinct from `glossary.md`

### Task 5: Verify

**Files:**
- None

- [ ] Run focused tests for the new sidecar/index behavior
- [ ] Confirm no unrelated regressions in nearby glossary/embedding tests
