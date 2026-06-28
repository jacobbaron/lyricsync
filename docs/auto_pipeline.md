# Autonomous perception-pipeline runner — playbook

A scheduled agent works a backlog of perception tickets (GitHub issues titled
`[PERCEPTION][Tn] …`) to completion, ~every 4 hours, one ticket per run. This doc
is the operating procedure each run follows. Tracking issue: **#90**. Research
basis: `docs/visual_perception_research.md`. Ticket specs: issues #84–#89.

## Ticket model

- **Source of truth = GitHub issues.** A ticket is identified by its title prefix
  `[PERCEPTION][Tn]`. Its dependencies are the `Depends on:` line in the body
  (ticket codes, e.g. `T1, T2`, or `none`). **Done = issue closed.**
- **Dependency graph** (also in #90):
  ```
  T1(#84) ──┬── T2(#85) ── T3(#86)
            └── T5(#88) ── T6(#89)
  T4(#87)  (independent)
  ```
- A ticket is **actionable** when *every* dependency ticket is **closed** and the
  ticket has no open PR and no `🤖 claimed` comment newer than 6h (the claim TTL —
  lets a stalled claim be retried).

## One run = one iteration

1. **Survey.** List open `[PERCEPTION]` issues and their `Depends on`. Mark each
   closed/open. Compute the actionable set.
2. **Select.** If the actionable set is non-empty, pick **one** in topological
   order, lowest issue number first (so T1/T4 go before their dependents). This
   is the "pick the next open ticket" behaviour — a blocked ticket is simply
   never in the actionable set, so the run naturally moves to the next one.
3. **If the actionable set is empty:** every remaining ticket is blocked by a dep
   that's still in flight. **Defer** — do nothing this cycle and end the run. Do
   not start blocked work. (Next run, 4h later, re-evaluates; the in-flight dep
   will usually have merged by then.)
4. **Claim.** Comment `🤖 claimed <UTC timestamp> by auto-pipeline` on the issue
   and open a **draft** PR early, so a concurrent run sees the claim and skips it.
5. **Implement** per the issue's spec, following repo conventions:
   - Branch `auto/t<n>-<slug>` off `main`.
   - Pure helper module + `tests/test_*.py` (mirror `audio_analysis.py` /
     `visual.py`); Modal worker + endpoint in `app.py`; web route under
     `web/src/app/api/clips/[id]/…`; migration in `supabase/migrations/`.
   - **Mount every new local helper module in the Modal image** (CLAUDE.md
     image-notes gotcha — the container crash-loops otherwise).
6. **CI gate.** Push; ensure `ci.yml` / `test.yml` pass on the PR. Fix until green.
   This is the pre-merge correctness gate (unit tests, lint, web build).
7. **Merge.** Mark the PR ready and squash-merge to `main` once CI is green. This
   triggers `deploy-modal.yml` (Modal), `db-migrate.yml` (migration), and the
   Vercel deploy.
8. **Prod e2e — the acceptance test.** Wait for the Modal deploy + migration to
   land (check `actions_list` for `deploy-modal.yml` / `db-migrate.yml` on the
   merge commit), then exercise the new endpoint against the **live app** with
   `LYRICSYNC_API_KEY` / `LYRICSYNC_BASE_URL` on a real clip (discover one via
   `GET /api/projects`). Verify the documented acceptance criteria. Post the
   result as a PR/issue comment.
   - **Modal note:** Modal deploys only from `main`, and there is no Modal token
     in this environment, so a Modal-backed feature genuinely cannot be prod-
     tested before merge. Hence: merge → deploy → verify → **revert on failure**.
9. **Close or revert.**
   - Prod e2e **passes** → close the issue (`completed`), un-draft done, remove
     claim. Update #90 if you track status there.
   - Prod e2e **fails** → **revert the merge commit** on `main` (restore prod),
     reopen/keep the issue open with a comment describing the failure, and stop.
     Do **not** leave prod broken and do **not** thrash (no more than one
     implement→revert cycle per ticket per run).

## Guardrails

- **One ticket per run.** Don't batch; keep blast radius small and the history
  legible.
- **Never force-push `main`; never push to `main` directly** — only via squash-
  merging a green PR.
- **Revert, don't hotfix-in-place** when prod e2e fails after merge.
- **Stop and comment** (don't retry blindly) on: a real test failure you can't fix
  in one pass, a destructive migration (drops/`delete`/`truncate`), anything that
  would touch data outside the ticket, or auth/secret changes. Leave it for a
  human.
- **Scope = the perception tickets only.** Don't refactor unrelated code.
- Respect the claim TTL so two runs never implement the same ticket.

## Scheduling

The durable runner is `.github/workflows/auto-pipeline.yml` (cron every 4h on
`main`). It invokes Claude Code headless with the prompt "execute one iteration of
`docs/auto_pipeline.md`." It requires a repo secret holding Claude credentials
(`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`) and `contents: write` +
`pull-requests: write` permissions. Until that secret exists the workflow is a
no-op. An in-session `CronCreate` can bridge the gap while a Claude session is
live, but it is **not** durable across container reclaim — the Action is the
real scheduler.
