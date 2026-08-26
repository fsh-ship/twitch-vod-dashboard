# AGENTS.md

## Scope

These instructions apply to the entire Twitch VOD Dashboard repository.

Read the current code, tests, `README.md`, and relevant documentation before making assumptions. The task prompt defines the requested change; this file defines stable repository-wide working rules.

## Working principles

- Prefer the smallest coherent change that solves the requested problem.
- Do not perform broad rewrites or unrelated cleanup unless explicitly requested.
- Preserve existing behavior outside the requested scope.
- Investigate the actual cause before changing code, especially for bugs.
- Do not silently change persistence formats, public behavior, routes, settings semantics, file layouts, or deployment assumptions.
- Reuse existing project patterns and dependencies where practical.
- Do not add a dependency unless it is clearly justified by the task.
- Treat existing local/uncommitted work as user work: do not discard, reset, overwrite, or reformat unrelated changes.
- Do not commit, push, rebase, force-update, or modify Git history unless explicitly requested.

## Tests and verification

For behavior changes, add or update focused tests when practical.

For bug fixes, prefer a regression test that would fail before the fix and pass afterward.

Run the checks relevant to the change. The normal full verification is:

```console
python -m pytest -q
python -m compileall -q app.py cleanup-vods.py vod_dashboard tests
```

If JavaScript changed and Node.js is available:

```console
node --check static/app.js
```

Additional rules:

- Do not weaken, delete, or bypass a failing test merely to make the suite green.
- Keep routine tests offline; mock Twitch, yt-dlp subprocesses, Google/YouTube, browser launches, and OS integrations where appropriate.
- Do not perform real Twitch downloads, YouTube uploads, OAuth authorization, destructive file operations, or other external side effects unless the task explicitly requires them.
- If a relevant check cannot be run, state exactly why.
- Do not claim the task is complete when relevant tests are failing.

## Security and runtime data

Never expose, commit, print, or embed real secrets or credentials, including:

- `.env`
- Twitch cookie files
- `youtube-token.json`
- `client_secret*.json`
- API keys
- passwords
- access or refresh tokens
- session cookies

Preserve existing security boundaries unless the task explicitly requires a reviewed change. In particular, be careful around authentication, CSRF protection, allowed origins/hosts, media-root containment, path traversal, symlink containment, and file permissions.

Treat runtime and media data as valuable. Do not delete or reset persistent state, downloaded media, upload history, archives, settings, OAuth data, recording state, or job state unless explicitly requested.

Keep host paths and container paths distinct.

## Twitch, recording, jobs, and YouTube

Changes in these areas can have cross-module effects. Check callers, persisted state, tests, and UI state before editing.

When relevant, verify:

- Twitch/VOD identity normalization and canonical URLs
- cookie handling and yt-dlp options
- format selection and ffmpeg behavior
- exit codes, stdout/stderr, produced files, and archive behavior
- job lifecycle, retry/recovery behavior, concurrency, and restart handling
- live recording and automatic recording state
- automatic VOD discovery/download state
- YouTube OAuth persistence and refresh behavior
- upload history, duplicate-upload protection, playlist selection/order, and post-upload file handling

Do not infer success solely from a UI status; verify the underlying state transition where practical.

## UX and UI

UX/UI quality is part of feature completeness, not an optional polish step.

For user-visible changes, preserve or improve:

- clear visual hierarchy
- understandable labels and actions
- consistent interaction patterns
- useful loading, success, error, disabled, and intermediate states
- responsive layout without unnecessary overflow
- accessibility, including text cues beyond color and sensible focus/ARIA behavior

Avoid adding permanent visual clutter when progressive disclosure or a clearer grouping is sufficient.

For meaningful UI changes, perform a browser/workflow check when the available environment supports it.

## Scope expansion

If investigation reveals a larger architectural issue:

1. solve the requested task with the smallest safe change when possible;
2. do not expand into a large refactor automatically;
3. report the larger issue and a recommended follow-up separately.

For high-risk areas such as security, persistence, concurrency, recovery, or destructive file handling, stop short of speculative changes and explain the risk if the requested behavior cannot be safely established from the repository.

## Final report

At the end of an implementation task, report concisely:

1. what changed;
2. which files changed;
3. why this approach was chosen;
4. which tests/checks were run and their results;
5. any remaining risk, limitation, or recommended follow-up.

Separate verified facts from assumptions.
