# Handoff: Resume Verifier build (checkpoint 2, Oct 25 2026, ~14:40 CDT)

Read this first in the next session, then `CLAUDE.md`, then the design-doc sections a task cites.
Setup: one machine (the GB10 itself, arm64, Python 3.12). The lead (Opus) plans and reviews;
Sonnet `coder` agents write the code, several at once on separate files.

## Where things stand

**Every module in the plan is on `main`** (pushed; 1108 tests and `demo --stub` pass at HEAD).

| Commit | Module |
|---|---|
| c5fef2f … 19299ba | J1–J8, J10, J11, H2, H3, H6 (first pass), J2 recording, J3, J5, J7 (see git log) |
| f0e660d | J9 internal API (`/internal/actions`, `verdict`, `recheck`, `explain`) |
| f9922f4 | J13 `oss_contribution` checks; `fake` golden case with hand-made contribution data |
| 6709859 | J12 Edit claim; J14 More context / Suggest questions (`core/generate.py`, `core/prompts/`) |
| 9c711bc | H4+H5 OpenClaw plugin (`verifier/stack/openclaw/resume-verifier/`); service edit-submit fixes |
| 75f816e | Fixture refresh: Henry's claims from live extraction, real replay snapshot, verdict, card golden; `tests/conftest.py` keeps `.env` out of tests |
| 79f82aa | Rubric: framework names ("Dart/Flutter", "Python/Django") aren't checked as languages |
| 2192067 | H6 OpenShell backend verified live; `verifier/stack/openshell/gateway.sh` |

`contracts-v1` is **not tagged** yet.

## Verified live today (GB10)
- **vLLM** (`vllm-qwen` container, `nvidia/Qwen3.6-35B-A3B-NVFP4` on :8000). Extraction takes ~24 s (Henry) and ~13 s (fake); same claim set as the fixtures.
- **Run checks** with the Docker backend and image `functional-run-sandbox:arm64`: cubesolver builds, `cpp_broken` fails with a compile error, Polytrack-ML is "could not run here", CAC24 passes its syntax step.
- **OpenShell backend** with a local offline gateway (`gateway.sh`): same cubesolver/cpp_broken results.
  It needs an image with `iproute2` and `/work`, and a policy `run_as_user` that matches the image user
  (repo policy and `docker/runcheck.Dockerfile` use uid 1000; `functional-run-sandbox` is uid 10001).
  The Dockerfile build needs internet. The coder's derived test image `rv-runcheck-test:arm64` is still loaded.
- **Full system** (§6.4): mock ATS :8100 + service :8200, `VERIFIER_REAL` = everything except `slack`,
  `SANDBOX_BACKEND=docker`, `python -m verifier.ats.apply henry`. It finishes in ~1 min with no stage errors.
  Card: 🟢 6 🟡 1 🔴 0 ⚪ 2 (cubesolver 🟡: 3 KB, 2 commits on 1 day). Live judge: cubesolver "works as claimed",
  the other two "could not attempt". Write-back note and PDF landed. `/internal/explain` and the More context
  overflow work. Live More context text is neutral and uses only the given facts.
- **OpenClaw plugin**: `npm run typecheck`, `npm test` (35), `npm run build` pass. Calls checked against the real service.

## Decisions made (keep consistent)
- Resumes are **placeholders**. Polytrack-ML is now "(JavaScript, 2026)", CAC24 "(Dart, Python, 2024)",
  because the live model keeps only the first listed language. Fake login placeholder: `rv-fake-candidate`.
  Claim ids: c1 Polytrack-ML, c2 CAC24, c3 cubesolver, c4–c7 skills, c8 employment, c9 education.
- The Edit modal's `callback_id` carries the ids, `openclaw:rv:edit_claim:<app>:<claim>:<version>`, because OpenClaw
  doesn't pass `private_metadata` to plugins. The service accepts both forms.
- OpenClaw acks Slack before the plugin handler runs. The plugin sends `X-Verifier-Ack: none`, and the service then
  also posts invalid or stale edits in the thread (§8.4 "caller duties").
- J13's `pr_count` counts merged PRs only (literal spec). Commits that landed with no merged PR plus `min_prs`
  set gives 🔴 on `pr_count`.
- Coders run tests with `RUNS_DIR=$(mktemp -d)`. Commit only a module's own paths, then `git pull --rebase` and push.
- `load_dotenv()` searches from the calling file's folder: scripts outside the repo must pass the `.env` path.

## Stack / machine state
- Venv `.venv`; Node 22 + OpenShell in `~/.local` (not on PATH: `export PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH`).
  Docker needs `sg docker -c "<cmd>"` in older shells.
- Plugin `node_modules/` are symlinks into an **old session's** `/tmp/claude-1000/…/5ba694d6-…/scratchpad/sdkdeps/`. If
  `/tmp` is cleaned, the plugin needs `npm install` (internet or that npm cache).
- `.env` now has `LLM_BASE_URL`, `LLM_MODEL`, `RUNCHECK_IMAGE=functional-run-sandbox:arm64`,
  `SANDBOX_BACKEND=docker`, and `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`, `GITHUB_TOKEN` (filled in since checkpoint 1).
- vLLM restart if needed: `sg docker -c "bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh"`.

## Next steps (in order)
1. **Live Slack check** (tokens are set): `.venv/bin/python tests/stack/live_slack_check.py`. It posts to the real
   channel, so ask the user first. Then rerun the full system with `slack` in `VERIFIER_REAL`.
2. **OpenClaw install** (`nemoclaw onboard` needs a short internet window). The H4 coder's install steps:
   build the plugin; add a network policy preset allowing `host.openshell.internal:8200` `/internal/**`;
   `nemoclaw <name> exec -- openclaw plugins install <dir>`; set `verifierUrl`, `token`, `slackActions`;
   `nemoclaw <name> gateway restart`; probe a Re-check click (fall back to `slackActions=agent`, then `off`).
3. **Fake candidate**: once its GitHub login is known, record it (`python -m verifier.core.fetch record …`, §14;
   `GITHUB_TOKEN` is set), then regenerate the `fake` golden snapshot and expected labels.
4. **Run-check image for OpenShell**: build `docker/runcheck.Dockerfile` when internet is available (it now has
   `iproute2`), or keep `SANDBOX_BACKEND=docker`.
5. Known small gaps:
   - Thread lines show Slack user ids when OpenClaw gives no username.
   - The cubesolver run trace in `fixtures/run_traces/c3.json` is still hand-made; a live trace exists from the full run.
   - `service.py` calls `render` directly rather than through `registry.get("render")`.

## Open items for the user
- OK to post test messages to the Slack channel (step 1).
- The fake candidate's GitHub login.
- Tag `contracts-v1`.
- Real resume PDFs, if they should replace the placeholders.
