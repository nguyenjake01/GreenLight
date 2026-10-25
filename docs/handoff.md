# Handoff: Resume Verifier build (checkpoint 4, Oct 25 2026, ~15:45 CDT)

Read this first in the next session, then `CLAUDE.md`, then the design-doc sections a task cites.
Setup: one machine (the GB10 itself, arm64, Python 3.12). The lead (Opus) plans and reviews;
Sonnet `coder` agents write the code, several at once on separate files.
Since checkpoint 4 this session is the **master coding session**: it alone writes tracked files. Other sessions may
read anything but write only to gitignored folders (they may still run `full_run.sh`, so check ports 8100/8200 first).

## Where things stand

**Every module in the plan is on `main`**, and the full system now runs end to end with **real Slack**.
1109 tests and `demo --stub` pass at HEAD.

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
| 25ae0aa | Second candidate Aidan Hong (`aidanhong8`) replaces the fake candidate |
| 7bd6a77 | Thread lines name Slack users by `<@U…>` mention when OpenClaw gives no username |
| 75ba301 | Judge: reasoning on (missing weights/datasets → `could_not_attempt`), `MAX_TOKENS` 6000 |
| 2261c77 | `scripts/openclaw_onboard.sh`, `scripts/openclaw_install_plugin.sh`, NemoClaw preset `rv-verifier` |
| (this) | `full_run.sh`: OpenShell backend starts its gateway; `VERIFIER_HOST` |

`contracts-v1` is **not tagged** yet.

## Second candidate: Aidan Hong (committed, 25ae0aa)

The user's friend **Aidan Hong, GitHub `aidanhong8`**, replaced the placeholder `rv-fake-candidate` / "Alex Rivera".
The user approved one deliberately overstated claim on his demo resume, for the demo's 🔴 beat (design doc ~line 2608).
Fixture key `aidan` (`python -m verifier.ats.apply aidan`, `demo --stub --candidate aidan`). He has live-extracted
claims, recorded evidence (66 cache files), 4 repo archives (portfolio is 15.5 MB), golden case `fixtures/golden/aidan/`,
and `fixtures/expected/aidanhong8.labels.json`.

| Claim | Label | top_reason |
|---|---|---|
| c1 care360, c2 HalloweenTracker, c3 quiz | 🟢 | all commits by the candidate; language is the main one |
| c4 portfolio-aidan-hong (overstated) | 🔴 | "Created Mar 2024; claim says 2021" (+ hard `stars`: "1 stars; claim says 500") |
| c5 contribution, boomty/CameraDetectionforTraffic | 🟢 | 2 merged PRs and 17 commits by @aidanhong8 |
| c6–c9 skills JavaScript, TypeScript, Python, Swift | 🟢 | |
| c10 education, c11 FBLA | ⚪ | not verifiable from public code |

**Live with real Slack and OpenShell (done, 15:24):** `SANDBOX_BACKEND=openshell SLACK=real bash scripts/full_run.sh aidan`
gave the labels above, both hard reasons on c4, 0 stage errors. Run checks: quiz "works as claimed", the other three
"couldn't test the behavior here". Card: https://greenlight-hum1820.slack.com/archives/C0CC26ZSBH7/p1792959822242999

**History note:** the handoff commit `3b01001` accidentally carried the coder's staged deletions of the old fake-candidate
files, so it's a broken in-between commit (tests fail there). `25ae0aa` completes the swap; HEAD is green.
Recorder quirk worth fixing: GitHub returns 204 (empty body) for an empty repo's `contributors`. The recorder logs that
as a miss; it should treat it like 409/404.

## Karpathy candidate (uncommitted, user said "leave it for now")
Another session started a third candidate, **Andrej Karpathy** (`karpathy`). Its judge changes were reviewed and
committed (75ba301). Still uncommitted on disk: the `karpathy` entry in `fixtures/candidates.json`, `KARPATHY` in
`make_placeholder_resumes.py`, `fixtures/resumes/karpathy.*`, `fixtures/webhooks/karpathy.json`. No claims, recording
or golden case yet. A session was test-running it (Slack stubbed) at 15:29. Don't commit it until the user decides.
Also untracked: `docs/video-script.md` (from another session; not reviewed).

## Verified live today (GB10)
- **vLLM** (`vllm-qwen` container, `nvidia/Qwen3.6-35B-A3B-NVFP4` on :8000). Extraction takes ~24 s per resume.
- **Slack** (bot `greenlight_helper`, workspace "GreenLight Demo Workspace"). `tests/stack/live_slack_check.py` passes:
  post, update in place, thread reply, PDF upload, permalink.
- **Full system with real Slack:** `SLACK=real bash scripts/full_run.sh` takes ~1 min, with no stage errors.
  Henry's card is 🟢 6 🟡 1 🔴 0 ⚪ 2, with the annotated PDF in the thread. The Greenhouse note and PDF were written back.
  Live judge results: cubesolver "works as claimed"; Polytrack-ML and CAC24 "could not attempt".
- **Run-check image `resume-verifier-runcheck:latest`** was built from `docker/runcheck.Dockerfile` (uid 1000, `/work`,
  `iproute2`, cmake). It's the default `RUNCHECK_IMAGE`, so there's no need to set one.
- **OpenShell backend, rerun with `resume-verifier-runcheck:latest` and the repo policy (15:20):** same outcomes as Docker
  (cubesolver `works_as_claimed` ~25 s, `cpp_broken` `failed_to_run` ~17 s). No network (proxy 403, raw sockets and
  DNS fail), PID limit 256 holds against a fork bomb, `/usr` and `/etc` read-only, sandboxes deleted after each run.
  **The demo uses `SANDBOX_BACKEND=openshell`.** `full_run.sh` now starts the gateway itself on that backend
  (state in `~/.rv-openshell`; stop with `bash verifier/stack/openshell/gateway.sh stop`).
  Gotcha: `run_check` swallows an LLM connection failure (agent "stopped: error", judge null) if `.env` isn't loaded.
- **J14 live:** More context mentions every note and flag.
- **OpenClaw plugin:** typecheck, 35 tests and build pass; a local install rehearsal (throwaway `HOME`) loaded it with
  all 4 tools. Not yet installed in a NemoClaw sandbox: waiting on the Slack app's Socket Mode token (see next steps).

## Decisions made (keep consistent)
- Placeholder resumes list one language per project (the live model keeps only the first).
  Henry's claim ids: c1 Polytrack-ML, c2 CAC24, c3 cubesolver, c4–c7 skills, c8 employment, c9 education.
- The Edit modal's `callback_id` is `openclaw:rv:edit_claim:<app>:<claim>:<version>`, because OpenClaw drops `private_metadata`.
  The plugin sends `X-Verifier-Ack: none`, and the service then reports invalid edits in the thread.
- J13 `pr_count` counts merged PRs only (literal spec).
- Coders run tests with `RUNS_DIR=$(mktemp -d)`. Commit only a module's own paths, `git pull --rebase`, push.
  The user approved pushing each finished module to `main`.
- `scripts/full_run.sh` sets `LLM_BASE_URL`/`LLM_MODEL` itself and refuses to start if 8100/8200 are busy.
  `VERIFIER_HOST=0.0.0.0` makes the service reachable from the OpenClaw sandbox (the internal API needs `INTERNAL_TOKEN`).
- The edit modal stays on the real `render` (not the registry): the service parses its `callback_id` suffix, which the stub lacks.
- With no username from OpenClaw, `by` is `<@U…>`, which Slack shows as the name (the bot has no `users:read`).

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
1. **OpenClaw: installed (16:00).** Sandbox `rv-openclaw` (NemoClaw v0.0.137, OpenClaw 2026.9.2, Local vLLM, Balanced
   policy + `slack` + `rv-verifier` presets). Onboarding needed `openshell settings set -g nemoclaw --global --key
   providers_v2_enabled --value true --yes` on NemoClaw's own gateway (:8080), then `nemoclaw onboard --resume`.
   `scripts/openclaw_install_plugin.sh rv-openclaw` passed: plugin loaded with 4 tools, Slack Socket Mode connected,
   sandbox → `host.openshell.internal:8200` HTTP 200. The verifier must listen on 0.0.0.0
   (`VERIFIER_HOST=0.0.0.0 KEEP=1 SANDBOX_BACKEND=openshell SLACK=real bash scripts/full_run.sh`).
   **A real Re-check click from Slack works end to end** (21:04 UTC: Slack → OpenClaw → plugin → `/internal/actions` 200 →
   "Re-check started/done" in the thread). Still to check: an @-mention, the overflow menu (More context, Edit),
   (Solved) The buttons vanished at 20:53 UTC because a click landed before the plugin was installed: with no
   handler, OpenClaw's Slack fallback replaces the clicked `actions` block with "✅ … selected by @user" and wakes
   its agent (the "⚠️ Memory Search blocked" reply is just a tool-not-found warning). No config disables it; with
   the plugin loaded, clicks are `handled:true` and nothing is stripped. **Demo rule: don't click during a
   gateway restart or plugin reload.** Restore a stripped card: `RUNS_DIR=<dir> bash scripts/refresh_card.sh <app_id>`.
   Handled clicks log only in the sandbox's `/sandbox/.openclaw/logs/gateway-persistent.log`, not `nemoclaw logs`.
2. Demo prep (design doc ~line 2608 beat sheet): record in replay mode; J11's *Simulate application* button starts a run.
3. Recorder quirk: treat GitHub's 204 for an empty repo's `contributors` like 404/409.

## Open items for the user
- Tag `contracts-v1` (Jake and Henry both agree).
- Whether to delete the stray failed Henry card in Slack.
- Whether to keep the Karpathy candidate.
