# PROGRESS.md — done-work log (append-only, newest first)

> One line per completed unit of work. Enough to answer "did we already do X?"
> without reading git history or re-exploring. Newest at top.

## 2026-07-16
- Set up token-optimization docs: `CLAUDE.md` (auto-loaded project map + symptom→file
  table), `CURRENT_TASK.md`, `PROGRESS.md`. Goal: stop re-discovering the tree each session.
- Added token-discipline conventions to `CLAUDE.md` (delegate broad exploration to
  subagents; one-task-per-session + /clear; targeted tests). Auto-update cadence confirmed.
- Added `.claude/settings.json` — read-only allowlist (git read cmds, pytest, ls/cat/head/
  tail/wc) to cut permission prompts. Writes/pushes/deletes still prompt by design.
- User preference: terse responses by default (lead with answer/diff).

## Baseline (pre-existing, from git history)
- `fa8a831` Update features
- `f8569ea` Initial commit: iPad/iPhone music recovery + `finder/` lossless engine + tests.
