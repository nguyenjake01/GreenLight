# Resume Verifier

Resume Verifier checks a job applicant's resume claims against public evidence. When a candidate applies through
the ATS (Greenhouse, or the bundled mock), it:
1. Reads the resume and extracts each claim with a local LLM.
2. Links the candidate's GitHub account and checks every project, skill and open-source claim against it.
3. Runs the candidate's code in a sandbox to see whether it works as claimed.
4. Posts an evidence card to Slack.
5. Writes an annotated resume and a note back to the ATS.

Recruiters can ask the Slack bot about any claim. Everything runs locally on one NVIDIA GB10: the model, the sandboxes
and the agent.

Each claim gets one of four labels: 🟢 supported, 🟡 partially supported, 🔴 contradicted, ⚪ no public evidence.
⚪ never means "false". The tool never calls a candidate fake and never recommends hiring or rejecting: the human decides.

`docs/design-doc.md` is the source of truth for behavior, contracts and module specs.

## How it works

```
ATS (mock or Greenhouse) ──signed webhook──▶ verifier service (:8200 / :8201)
                                               │ read resume → extract claims (vLLM, Qwen3.6-35B)
                                               │ link GitHub → fetch evidence (live / replay cache)
                                               │ rubric → verdict (labels + reasons)
                                               │ run check: OpenShell sandbox + agent + judge
                                               ├──▶ Slack card + thread (live run lines, annotated PDF)
                                               └──▶ ATS write-back (private note + annotated PDF)
Slack buttons / @-mentions ──▶ OpenClaw (NemoClaw sandbox `rv-openclaw`) ──plugin──▶ verifier /internal/*
```

- **Card actions:** *Re-check now*, *Open in Greenhouse*, *Open annotated resume*, and a per-claim menu with
  *More context*, *Suggest questions* and *Edit claim*.
- **The Slack bot** (`@greenlight_helper`) answers questions such as "why is Aidan Hong's portfolio claim red?" in
  the thread, using only the verifier's tools.
- **The mock ATS** sends the same signed `new_candidate_application` webhook as Greenhouse, serves the Harvest API for
  the write-back, and shows a candidate page with the annotated resume.

## Status (Oct 25, 2026)

- **Built:** all 20 build stages in `docs/coding-prompts.md`, and every v1 Must and Should (design doc §4).
- **Verified live on the GB10:**
  - real Slack cards and threads;
  - OpenShell run checks;
  - OpenClaw button clicks and @-mentions;
  - the mock ATS end to end.
- **Demo candidates:** Henry Chen (🟢 6 🟡 1 ⚪ 2) and Aidan Hong (🟢 8 🔴 1 ⚪ 2). Aidan's resume has one deliberately
  overstated project, for the 🔴 beat.
- **Not done:** the Stretch items (framework/manifest check, Word resumes), the `contracts-v1` tag, and the video
  recording.
- **Live state:** see `docs/handoff.md` for the current machine state, open items and the manual checklist.

## Repository layout

```
verifier/
  contracts/       types, JSON schemas, stubs, registry (frozen after the contracts-v1 tag)
  core/            resume reading, GitHub fetch + matching, rubric, renderers, annotated PDF,
                   run harness (Docker backend), pipeline, verifier service (FastAPI)
  stack/           LLM client, claim extraction, Slack transport, run-check agent + judge,
                   OpenShell sandbox backend, prompts
    openclaw/      OpenClaw plugin `resume-verifier` (TypeScript), NemoClaw policy preset,
                   always-on agent rules block
    openshell/     local OpenShell gateway script, config and run-check policy
  ats/             mock ATS (FastAPI + Jinja templates, pdf.js viewer with hover cards)
  demo.py          python -m verifier.demo
docker/            runcheck.Dockerfile (the run-check sandbox image)
fixtures/          candidates, resumes, recorded GitHub cache, repo archives, golden verdicts
scripts/           full_run.sh, demo_up.sh, demo_down.sh, refresh_card.sh,
                   openclaw_onboard.sh, openclaw_install_plugin.sh
tests/             core/, stack/, ats/ (pytest)
docs/              design-doc.md, coding-prompts.md, handoff.md, demo-runbook.md
```

## Setup

You need Python 3.12 and, for the full system, Docker. The GB10 also runs these:

- **vLLM:** container `vllm-qwen` on `:8000`, serving `nvidia/Qwen3.6-35B-A3B-NVFP4`.
- **Node 22, OpenShell and NemoClaw**, installed under `~/.local`. Run
  `export PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH` first.
- **The run-check image:** `resume-verifier-runcheck:latest`, built from `docker/runcheck.Dockerfile`.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Fill in `.env`. Never commit it.

| Variable | What it does |
|---|---|
| `GITHUB_TOKEN` | GitHub API token (needed for live fetches) |
| `GITHUB_FETCH_MODE` | `replay` (default: recorded cache in `fixtures/cache`), `live`, or `record` |
| `LLM_BASE_URL`, `LLM_MODEL` | The vLLM endpoint, e.g. `http://localhost:8000/v1` and `nvidia/Qwen3.6-35B-A3B-NVFP4` |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` | Slack Web API bot token (`xoxb-`) and the card channel |
| `SLACK_APP_TOKEN` | Socket Mode token (`xapp-`) for OpenClaw's Slack connection. The verifier itself never uses it |
| `GREENHOUSE_WEBHOOK_SECRET` | Shared secret for the signed webhook |
| `INTERNAL_TOKEN` | Shared token between the OpenClaw plugin and the verifier's `/internal/*` API |
| `VERIFIER_REAL` | Modules that run real (comma list or `all`); the rest are stubs |
| `SANDBOX_BACKEND` | `openshell` (the demo backend) or `docker` |
| `RUN_CHECK` | `on`, `baseline_only` or `off` |
| `ATS_PUBLIC_URL` | Where *Open in Greenhouse* and file links point |

The full list with defaults is in `.env.example` and design doc §6.2.

## Running

**Everything stubbed** (no network, Slack or model):

```bash
.venv/bin/python -m verifier.demo --stub                  # Henry
.venv/bin/python -m verifier.demo --stub --candidate aidan
```

**Tests** (about 1,100 Python tests and the plugin's tests):

```bash
RUNS_DIR=$(mktemp -d) .venv/bin/python -m pytest -q
cd verifier/stack/openclaw/resume-verifier && npm run check
```

**One full-system run** (mock ATS + verifier with real modules; one candidate applies; prints the verdict; stops):

```bash
SANDBOX_BACKEND=openshell bash scripts/full_run.sh henry      # Slack stubbed
SANDBOX_BACKEND=openshell SLACK=real bash scripts/full_run.sh aidan
```

**The demo stack** (long-running; runs start from the mock ATS):

```bash
bash scripts/demo_up.sh                 # rehearsal: Slack stubbed, ports 8100 (ATS) / 8200 (verifier)
SLACK=real bash scripts/demo_up.sh      # the demo: real Slack, ports 8101 / 8201
bash scripts/demo_down.sh               # stop it (or Ctrl-C)
```

The real-Slack stack uses 8101/8201 because the OpenClaw plugin calls the verifier on 8201. Stub stacks keep
8100/8200, so both can run side by side. A stub stack's mock ATS shows a banner saying Slack is off.
`docs/demo-runbook.md` covers the click path for each beat, recovery steps and shutdown.

## Mock ATS

Open `http://localhost:8101/` (or `:8100` for a stub stack).

| Page | What it does |
|---|---|
| `/` | Applications list with live status chips, and *Simulate application* buttons for the recorded candidates |
| `/candidates/<id>?application_id=<id>` | Candidate page: annotated resume (pdf.js; hover a mark to see why it has that colour), a processing stepper, attachments and notes |
| `/apply` | Careers page: a candidate uploads a resume PDF (≤ 5 MB) with name, email and optional GitHub profile |
| `/_mock/apply`, `/_mock/status` | Scripted apply (JSON `{"fixture": "henry"}`) and the status feed the pages poll |
| `/token`, `/v3/notes`, `/v3/attachments`, `/files/...` | Harvest-style API the verifier writes back to, and file downloads |

Options:
- **`MOCK_ATS_HIDE_SIMULATE=1`** hides the Simulate buttons and links to `/apply` instead.
- **New candidates need `GITHUB_FETCH_MODE=live`.** The replay cache only has the recorded ones.
- **`MOCK_ATS_PASSCODE`** puts a passcode in front of visitors coming through a Cloudflare tunnel. Local traffic,
  including the verifier's own calls, is never gated.
- **`MOCK_ATS_INTERNAL_URL`** lets the verifier download resumes locally while public links use `ATS_PUBLIC_URL`.
- **Sharing a public link with judges:** see "Public link for judges" in `docs/demo-runbook.md`.

## Slack bot (OpenClaw)

The bot runs in a NemoClaw sandbox (`rv-openclaw`) with OpenClaw, the local vLLM and Slack Socket Mode.

1. **Slack app setup.** In the app's settings, turn on Socket Mode and Interactivity, add the bot scopes and events,
   and put `SLACK_APP_TOKEN` in `.env`. The handoff lists the exact scopes and events.
2. **Onboarding.** Run `bash scripts/openclaw_onboard.sh` yourself: you accept NemoClaw's terms in its wizard.
   If *Apply* fails with `providers_v2_enabled`, run the `openshell settings set` command the script prints, then
   `nemoclaw onboard --resume`.
3. **Plugin install.** Run `bash scripts/openclaw_install_plugin.sh rv-openclaw`. It builds and tests the plugin,
   applies the `rv-verifier` network preset, installs and configures the plugin, writes the always-on answering
   rules into the agent's `AGENTS.md`, sets thread replies, restarts the gateway, and checks Slack and the verifier.
   `--verify-only` repeats the checks.

The verifier must listen on `0.0.0.0` (`demo_up.sh` does this) so the sandbox can reach it.

Two cautions:
- **Don't click card buttons while OpenClaw restarts or reloads.** A click with no plugin loaded makes OpenClaw
  strip the card's buttons. Restore them with `RUNS_DIR=<runs dir> bash scripts/refresh_card.sh <application_id>`.
- **If the machine reboots,** the sandbox stops. Start it again with `nemoclaw rv-openclaw start`.

## Project rules

- **`verifier/contracts/` is frozen** after the `contracts-v1` tag. Change it only when Jake and Henry both agree.
- **Before pushing to `main`:** the module's tests and `python -m verifier.demo --stub` must pass. Run
  `git pull --rebase` first.
- **Never commit** `runs/` or `.env`.

## Docs

| File | What's in it |
|---|---|
| `docs/design-doc.md` | Design, decisions, contracts and module specs (the source of truth) |
| `docs/coding-prompts.md` | The prompt for each build stage |
| `docs/handoff.md` | Current state, open items, machine gotchas |
| `docs/demo-runbook.md` | Recording-day checklist, click path, public link, recovery |
