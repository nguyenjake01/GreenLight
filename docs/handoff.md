# Handoff: Resume Verifier build (checkpoint 3, Oct 25 2026, ~15:00 CDT)

Read this first in the next session, then `CLAUDE.md`, then the design-doc sections a task cites.
Setup: one machine (the GB10 itself, arm64, Python 3.12). The lead (Opus) plans and reviews;
Sonnet `coder` agents write the code, several at once on separate files.

## Where things stand

**Every module in the plan is on `main`**, and the full system now runs end to end with **real Slack**.
1108 tests and `demo --stub` pass at `89f4254`.

| Commit | What |
|---|---|
| c5fef2f … 19299ba | J1–J8, J10, J11, H2, H3, J2 recording, J3, J5, J7 (see git log) |
| f0e660d | J9 internal API |
| f9922f4 | J13 `oss_contribution` checks |
| 6709859 | J12 Edit claim; J14 More context / Suggest questions |
| 9c711bc | H4+H5 OpenClaw plugin; service edit-submit fixes |
| 75f816e | Fixture refresh: Henry's claims from live extraction, real replay snapshot; `tests/conftest.py` |
| 79f82aa | Rubric: framework names ("Dart/Flutter") aren't checked as languages |
| 2192067 | H6 OpenShell backend verified live; `verifier/stack/openshell/gateway.sh` |
| 7a4420a | cubesolver run trace fixture is now a real live trace; plugin `package-lock.json` (real `npm install`) |
| 89f4254 | `scripts/full_run.sh`: one-command full-system run |

`contracts-v1` is **not tagged** yet.

## In progress at the pause: Aidan replaces the fake candidate (uncommitted)

The user's friend **Aidan Hong, GitHub `aidanhong8`**, replaces the placeholder `rv-fake-candidate` / "Alex Rivera".
The user approved one deliberately overstated claim on his demo resume, for the demo's 🔴 beat (design doc ~line 2608).
A coder was working on this at the pause. **Nothing of it is committed. Check `git status` first.**

Resume plan (placeholder style, `fixtures/resumes/make_placeholder_resumes.py`):
- **True projects:** `care360` (JavaScript, 2025), `HalloweenTracker` (Swift, 2025, Instructables Halloween contest), `quiz` (TypeScript, 2025).
- **Overstated project:** `portfolio-aidan-hong` "started 2021, open-sourced as a template with 500+ stars".
  The real repo was created 2024-03-31 and has 1★, so the target is 🔴 with hard `start_date` and `stars`.
- **True contribution:** 2 merged PRs to `boomty/CameraDetectionforTraffic` (target 🟢 with PR links).
  He also has merged PRs to `EricWang812/TimbreGT` (2) and `Tepah/CodingStemAcademyRegistrationSystem` (1).
- **Skills:** JavaScript, TypeScript, Python, Swift. **Unchecked:** Diamond Bar High School; FBLA.

Planned file renames: key `aidan` in `fixtures/candidates.json`; `fixtures/resumes/aidan.pdf` and `aidan.resume.json`;
`fixtures/claims/aidanhong8.json` (from live extraction); `fixtures/expected/aidanhong8.labels.json`;
`fixtures/golden/aidan/` (replaces `golden/fake/`); webhook fixture; about 26 test references to the `fake` key.
Record with `python -m verifier.core.fetch record --login aidanhong8 --claims fixtures/claims/aidanhong8.json --clone`.
It should only add to `fixtures/cache/`; check that Henry's entries are unchanged.

**To finish:**
1. Review what's on disk (`git status`, `git diff --stat`).
2. Check no `rv-fake-candidate` references remain:
   `grep -rn rv-fake-candidate tests fixtures verifier --include=*.py --include=*.json`
3. Run the real rubric on Aidan's snapshot. The portfolio claim must be 🔴 with both hard reasons, and the contribution 🟢.
4. Full suite and `demo --stub`, then commit.
5. Then `SLACK=real bash scripts/full_run.sh aidan` for a live 🔴 card.
6. If the work is half-done or broken, `git checkout -- fixtures tests` plus removing the new untracked files resets it; then redo with one coder.

## Verified live today (GB10)
- **vLLM** (`vllm-qwen` container, `nvidia/Qwen3.6-35B-A3B-NVFP4` on :8000). Extraction takes ~24 s per resume.
- **Slack** (bot `greenlight_helper`, workspace "GreenLight Demo Workspace"). `tests/stack/live_slack_check.py` passes:
  post, update in place, thread reply, PDF upload, permalink.
- **Full system with real Slack:** `SLACK=real bash scripts/full_run.sh` takes ~1 min, with no stage errors.
  Henry's card is 🟢 6 🟡 1 🔴 0 ⚪ 2, with the annotated PDF in the thread. The Greenhouse note and PDF were written back.
  Live judge results: cubesolver "works as claimed"; Polytrack-ML and CAC24 "could not attempt".
- **Run-check image `resume-verifier-runcheck:latest`** was built from `docker/runcheck.Dockerfile` (uid 1000, `/work`,
  `iproute2`, cmake). It's the default `RUNCHECK_IMAGE`, so there's no need to set one.
- **OpenShell backend** with the local gateway: same results as Docker on cubesolver and `cpp_broken`.
  It wasn't rerun with the newly built image and the repo policy; that's the remaining check
  (`bash verifier/stack/openshell/gateway.sh setup|start`, then `SANDBOX_BACKEND=openshell`).
- **OpenClaw plugin:** typecheck, 35 tests and build pass, with real `node_modules` and a lockfile. It isn't installed in a gateway yet.

## Decisions made (keep consistent)
- Placeholder resumes list one language per project (the live model keeps only the first).
  Henry's claim ids: c1 Polytrack-ML, c2 CAC24, c3 cubesolver, c4–c7 skills, c8 employment, c9 education.
- The Edit modal's `callback_id` is `openclaw:rv:edit_claim:<app>:<claim>:<version>`, because OpenClaw drops `private_metadata`.
  The plugin sends `X-Verifier-Ack: none`, and the service then reports invalid edits in the thread.
- J13 `pr_count` counts merged PRs only (literal spec).
- Coders run tests with `RUNS_DIR=$(mktemp -d)`. Commit only a module's own paths, `git pull --rebase`, push.
  The user approved pushing each finished module to `main`.
- `scripts/full_run.sh` sets `LLM_BASE_URL`/`LLM_MODEL` itself and refuses to start if 8100/8200 are busy.

## Stack / machine state and gotchas
- Venv `.venv`; Node 22 + OpenShell in `~/.local` (not on PATH: `export PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH`).
  Docker needs `sg docker -c "<cmd>"` in older shells.
- **`.env` lost its `LLM_*` lines twice today**, most likely from an editor saving an older copy.
  They're re-added at the end of the file; check with
  `.venv/bin/python -c "from dotenv import dotenv_values as d; print(d('.env').get('LLM_BASE_URL'))"`.
  `.env` now has `GITHUB_TOKEN` (valid, 5000/h), `SLACK_BOT_TOKEN` (xoxb, works), `SLACK_CHANNEL_ID`,
  `GREENHOUSE_WEBHOOK_SECRET`, `INTERNAL_TOKEN`, and the two `LLM_*` lines.
- `load_dotenv()` searches from the calling file's folder; scripts outside the repo must pass the `.env` path.
- Internet was reachable this afternoon (GitHub, npm, PyPI, Debian, Docker Hub).
- A forked session ("Project handoff continuation") ran `apply` against our ports once and left a stray Henry card
  in Slack that reads "couldn't read claims". The user may want to delete it. Don't run two full-system runs on the same ports.
- Leftover local images: `rv-runcheck-test:arm64` (from the OpenShell check) and `functional-run-sandbox:arm64`. Both are safe to remove.
- vLLM restart if needed: `sg docker -c "bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh"`.

## Next steps (in order)
1. Finish and commit the Aidan swap (above), then a live Aidan run with real Slack.
2. **OpenClaw install** (internet is up). Steps from the H4 coder:
   1. Build the plugin.
   2. Add a NemoClaw network-policy preset allowing `host.openshell.internal:8200` `/internal/**`.
   3. `nemoclaw <name> exec -- openclaw plugins install <dir>`.
   4. Set `verifierUrl=http://host.openshell.internal:8200`, `token`, `slackActions`.
   5. `nemoclaw <name> gateway restart`.
   6. Click Re-check on a card. Fall back to `slackActions=agent`, then `off`.
   This needs `nemoclaw onboard` first; ask the user before connecting it to their Slack app.
3. OpenShell rerun with `resume-verifier-runcheck:latest` and the repo policy. If it passes, decide whether the demo uses
   `SANDBOX_BACKEND=openshell` (the design's intent) or `docker`.
4. Small gaps:
   - Thread lines show Slack user ids when OpenClaw gives no username.
   - `service.py` calls `render` directly rather than through `registry.get("render")`.
   - J14's context prompt now asks the model to mention notes and flags. That wasn't rechecked live.
5. Demo prep (design doc ~line 2608 beat sheet): record in replay mode; J11's *Simulate application* button starts a run.

## Open items for the user
- Tag `contracts-v1` (Jake and Henry both agree).
- Whether to delete the stray failed Henry card in Slack.
- OK to install the OpenClaw plugin against the real Slack app.
