---
name: release
description: Project release workflow for Subtitle translation. Use when the user asks to commit current changes, iterate or bump the latest tag, push commit/tag, or write release notes after verified project work.
---

# Release

## Workflow

Use this skill to finish a scoped project change with a clean commit, optional tag, optional push, and concise release notes.

1. Inspect the worktree first:
   - Run `git status --short`.
   - Review the relevant diff before staging.
   - Never stage `.env`, `providers.json`, `cookies.txt`, local prompt files, generated subtitles, videos, project outputs, `.venv`, or IDE files.

2. Verify before committing:
   - Run the narrowest meaningful tests for the touched code.
   - For burn/pipeline/script changes, run Python unit tests, PowerShell parse checks, bash syntax checks, and `git diff --check`.
   - If a verification step cannot run, say exactly why before committing.

3. Stage and commit:
   - Stage only intended tracked files and any intended new files.
   - Use a concise conventional commit message when possible, such as `feat: match burn bitrate to source video`.
   - Recheck `git status --short` after staging.

4. Iterate the latest tag when requested:
   - Find the newest semantic tag with `git tag --list "v*" --sort=-v:refname`.
   - Bump patch by default, for example `v1.6.3` -> `v1.6.4`.
   - Respect an explicit user-specified tag, major, minor, or patch request.
   - Create the tag after the commit succeeds.

5. Push only when requested:
   - Push the branch only if the user asks for push.
   - Push tags only if the user asks for pushing tags or publishing the release.

6. Write release notes:
   - Default to Chinese for this repository.
   - Keep notes practical and short: headline, changed behavior, compatibility note, and verification.
   - Mention user-visible defaults and migration notes, especially `.env` values that can override script defaults.

## Final Response

Report the commit hash, tag, and whether anything was pushed. Include release notes in the final answer when requested.
