# CLAUDE.md

This repository is a subtitle pipeline project centered on:

`download -> whisper -> beautify -> glossary -> translate/proofread -> burn`

## Overview

- Windows host, PowerShell is the primary operator surface
- Linux / WSL bash scripts are also maintained in parallel
- The project uses local tools for download / ASR / burn, and remote LLM APIs for translation and proofreading

## Current Architecture

```text
Subtitle translation/
├── pipeline.ps1              # Windows super pipeline
├── pipeline.sh               # Linux/WSL pipeline
├── download.ps1              # Windows download only
├── download.sh               # Linux download only
├── whisper.ps1               # Windows WhisperX
├── whisper.sh                # Linux WhisperX
├── beautify_srt.py           # Scene-based SRT beautify
├── glossary_builder.py       # Glossary builder with optional Tavily search
├── translate_srt.py          # Split + translate + proofread + ASS export
├── ffmpeg-burn.ps1           # Windows burn
├── ffmpeg-burn.sh            # Linux burn
├── mpv-burn.ps1              # Optional advanced burn
├── mpv-burn.sh               # Optional advanced burn
├── template.ass
├── .env.example
├── providers.example.json
└── <Video Title>/
    ├── <Video Title>.<ext>
    ├── <Video Title>.srt
    ├── <Video Title>.json
    ├── <Video Title>.beautified.srt
    ├── <Video Title>.split.srt
    ├── <Video Title>.proofread.srt
    ├── <Video Title>.zh.srt
    ├── <Video Title>.zh.ass
    ├── <Video Title>.zh-en.ass
    ├── <Video Title>.zh.description
    ├── <Video Title>.png
    ├── <Video Title>.info.json
    ├── <Video Title>.description
    ├── <Video Title>.tags.txt
    └── glossary.md
```

## Primary Flow

### pipeline.ps1

1. `download.ps1`
2. `whisper.ps1`
3. `beautify_srt.py`
4. `glossary_builder.py`
5. `translate_srt.py`
6. `ffmpeg-burn.ps1`

Artifact chain:

`video -> srt -> beautified.srt -> glossary.md -> zh-en.ass -> burned.mkv`

### pipeline.sh

1. `download.sh`
2. `whisper.sh`
3. `beautify_srt.py`
4. `glossary_builder.py`
5. `translate_srt.py`
6. `ffmpeg-burn.sh`

## Important Behavior

### download

- Output filename is normalized to `<folder>/<folder>.<ext>`
- Thumbnail is downloaded as `.png`
- Metadata, description and tags are preserved alongside the video

### whisper

- Existing `.srt` causes automatic skip
- Video is first converted to mono `16k wav`
- WhisperX is run with `--output_format all`
- Word timestamp `.json` is later consumed by `translate_srt.py`
- `WHISPER_DEVICE` replaces old compute-type handling

### beautify

- Default output is `.beautified.srt`
- Original `.srt` is not overwritten unless explicitly requested
- Uses scene-based snapping, not keyframe snapping by default
- Current defaults in code are the source of truth

### glossary

- Runs after beautify, before translate
- Reads subtitles, description, tags, and info.json
- Uses Tavily when `TAVILY_API_KEY` is configured
- Falls back to offline generation when Tavily is unavailable
- Requires `TRANSLATE_PROVIDER` to be configured because it reuses the project LLM stack

### translate

- `.split.srt` caches the LLM sentence splitting result
- `.zh.srt` caches translated Chinese subtitles
- `.proofread.srt` stores proofread English subtitles
- `glossary.md` is automatically injected into both translation and proofreading when present
- `.zh.description` is generated from title + description + tags

### burn

- ffmpeg burn is the default pipeline path
- Cover art is preserved
- Resolution override keeps aspect ratio and pads with black bars

## Config

### .env

Key variables currently used:

- `WHISPER_MODEL`
- `WHISPER_ALIGN_MODEL`
- `WHISPER_DEVICE`
- `TRANSLATE_PROVIDER`
- `TRANSLATE_MODEL`
- `PROOFREAD`
- `PROOFREAD_PROVIDER`
- `PROOFREAD_MODEL`
- `PIPELINE_SKIP_DOWNLOAD`
- `PIPELINE_SKIP_WHISPER`
- `PIPELINE_SKIP_BEAUTIFY`
- `PIPELINE_SKIP_KNOWLEDGE`
- `PIPELINE_SKIP_TRANSLATE`
- `PIPELINE_SKIP_BURN`
- `BURN_OVC`
- `BURN_OVCOPTS`
- `BURN_OAC`
- `BURN_RES`
- `OPENROUTER_API_KEY`
- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY`
- `TAVILY_API_KEY`
- `TAVILY_MAX_RESULTS`

### providers.json

- Base URL should be OpenAI SDK compatible
- Do not include `/chat/completions` at the end

## Skills

Project skills are stored in:

- `.claude/skills/download/SKILL.md`
- `.claude/skills/whisper/SKILL.md`
- `.claude/skills/beautify/SKILL.md`
- `.claude/skills/knowledge/SKILL.md`
- `.claude/skills/translate/SKILL.md`

The expected layout is `skill-dir/SKILL.md`.

## Working Notes

- Prefer reading `.env` rather than hardcoding tool paths
- Keep PowerShell and bash entry points behaviorally aligned
- When updating docs, make them match actual code paths and actual flags, not historical ones
- The repo may contain user-local `.env`, `providers.json`, cookies, and generated video folders; do not revert user data
