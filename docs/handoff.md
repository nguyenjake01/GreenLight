# Handoff: Resume Verifier build (checkpoint 5, Oct 25 2026, ~16:45 CDT)

Read this first in the next session, then `CLAUDE.md`, then the design-doc sections a task cites.
Setup: one machine (the GB10 itself, arm64, Python 3.12). The lead (Opus) plans and reviews; Sonnet `coder` agents
write the code, several at once on separate files, and slow non-blocking work runs in background agents.
The lead session is the **master coding session**: only it writes tracked files. Other sessions may read anything and
write only to gitignored folders, but they may run `full_run.sh`, so check ports 8100/8200 before starting a stack.

## Where things stand

Every module in `docs/coding-prompts.md` is on `main`. The full system runs live end to end: mock ATS → verifier →
real Slack card → run checks in **OpenShell** → OpenClaw in a NemoClaw sandbox handling **button clicks and @-mentions**.
1109 Python tests, 42 plugin tests and `demo --stub` pass at HEAD. `contracts-v1` is still not tagged.

Commits since checkpoint 3 (newest first):

| Commit | What |
|---|---|
| 569d70b | `scripts/demo_up.sh` / `demo_down.sh` (long-running demo stack), `docs/demo-runbook.md` |
| af61fd5 | Agent instructions: report the run check exactly; only facts a check gives; no greeting |
| dc21a7a | OpenClaw `channels.slack.replyToMode=all` (bot replies in threads), set by the install script |
| 9a5fad0 | `scripts/refresh_card.sh` (restore a card whose buttons OpenClaw stripped) |
| 97cb4ad, 7bd6a77 | Thread lines show a Slack mention when OpenClaw gives no username |
| ea1b6c2 | OpenClaw installed in sandbox `rv-openclaw`; onboarding notes |
| 98ec3ba | `full_run.sh`: `SANDBOX_BACKEND=openshell` starts its gateway; `VERIFIER_HOST` |
| 2261c77 | `scripts/openclaw_onboard.sh`, `scripts/openclaw_install_plugin.sh`, NemoClaw preset `rv-verifier` |
| 75ba301 | Judge: reasoning on (missing weights/datasets → `could_not_attempt`) |
| 25ae0aa | Second candidate Aidan Hong (`aidanhong8`) with the 🔴 demo beat |

## Verified live today

- **Candidates.** Henry: 🟢 6 🟡 1 ⚪ 2. Aidan: 🟢 8 🔴 1 ⚪ 2. Aidan's portfolio claim is 🔴 on two hard checks
  ("Created Mar 2024; claim says 2021", "1 stars; claim says 500"), and his contribution to
  boomty/CameraDetectionforTraffic is 🟢.
- **OpenShell is the demo backend.** With `resume-verifier-runcheck:latest` and the repo policy it matched Docker
  (cubesolver `works_as_claimed`, `cpp_broken` `failed_to_run`). No network inside, the 256-PID limit holds,
  sandboxes are deleted after each run.
- **Card actions from Slack, through OpenClaw and the plugin:**
  - Re-check: a real click.
  - More context and Suggest questions: simulated plugin payloads posted to `/internal/actions`; both posted in the thread.
  - Edit submit: simulated. "edited quiz: language TypeScript → Python. 🟢 → 🔴", then reverted.
- **@-mention.** "why is Aidan Hong's portfolio claim red?" → `get_verdict` → correct answer in a thread in ~6–12 s.
- **Demo paths** (demo_up on 8101/8201, Slack stubbed): Simulate application runs Henry and Aidan with 0 stage errors.
  The filled card arrives ~30 s after the click. Open in Greenhouse → `/candidates/<id>?application_id=<id>` shows
  the annotated PDF and the note.

## Bot answers hardened (committed after checkpoint 5)

Live CLI tests had shown the bot breaking D5 ("fabrication", "recommend an interview") and inventing an answer for
"why is cubesolver yellow?". Biggest cause: NemoClaw's `tools.toolSearch.mode: "tools"` hid our tools behind
`tool_search`. Fixes, by impact:
1. The tools are registered `catalogMode: "direct-only"`.
2. Every tool result starts with a rules line and a `FACTS:` line computed from the JSON.
3. The installer writes `verifier/stack/openclaw/workspace-agents-block.md` into `/sandbox/.openclaw/workspace/AGENTS.md`.
4. Stricter `AGENT_INSTRUCTIONS.md` and a broader skill description.
`recheck`'s `by` is now the display name, else `<@senderId>`, else "someone".

Final round (r8): every question passed: fake/hire, the red portfolio claim, Aidan's verdict, Bob Smith, no candidate
named, off-topic, and cubesolver twice. Results are in the session scratchpad under `agent_tests/`.
Still model-dependent:
- The off-topic decline ("Paris." slipped through in 3 of 8 rounds before the scope rule moved first).
- Cubesolver sometimes makes one wasted `get_verdict("cubesolver")` call before asking.
- Unverified: a real @-mention re-check showing `<@you>` (the CLI has no Slack sender).
If the sandbox is recreated, rerun `scripts/openclaw_install_plugin.sh`, which restores the AGENTS block and tool settings.
Test the agent without Slack: one `nemoclaw rv-openclaw exec --stdin -- sh -s` running several
`openclaw agent --session-id <new id> --json -m "<question>"` in parallel (only one `exec` at a time; no `--deliver`,
so nothing posts).

## Final manual checklist (for the user, one sitting, before recording)

1. In `#resume-review-hub`, on a card, open **⋯ → Edit** on a claim. The modal should open (only a real click can
   test this). Cancel it.
2. Click **Re-check now**, and also post `@greenlight_helper re-check Aidan Hong`. Both thread lines should say "↻ Re-check started by @you".
3. Post `@greenlight_helper is Aidan Hong a fake candidate? Should we hire him?`. The thread reply should say the
   verifier doesn't judge honesty or make hiring calls, then the tally and the 🔴 claim, with no recommendation.
4. Post `@greenlight_helper why is cubesolver yellow?`. The bot should ask which candidate.
5. Run `SLACK=real bash scripts/demo_up.sh` once and click Simulate application for Henry (the real-Slack start is untested).

## The live test stack (still running at the pause; stop it before a demo run)

- **Mock ATS** on 127.0.0.1:8100.
- **Verifier** on 0.0.0.0:8200, with `RUNS_DIR=/tmp/rv-fullrun-POjK` (Aidan, application 1000001, card ts
  1792959822.242999), real modules and `SANDBOX_BACKEND=openshell`. Started by a scratchpad script; logs are in
  `/tmp/rv-fullrun-POjK/_logs/` (`service3.log`).
- **Run-check gateway** on 18080. Stop it with `bash verifier/stack/openshell/gateway.sh stop`.
- **Stop the servers:** `pkill -f "uvicorn verifier.core.service:app --host 0.0.0.0"` and
  `pkill -f "uvicorn verifier.ats.mock_app:app --port 8100"`.
- **NemoClaw's own gateway** on :8080 and the sandbox `rv-openclaw` keep running. Leave them.

## OpenClaw / NemoClaw (sandbox `rv-openclaw`)

- **Versions and setup:** NemoClaw v0.0.137, OpenClaw 2026.9.2, Local vLLM. Policies: Balanced plus the `slack` and
  `rv-verifier` presets. Slack Socket Mode with an allowlist of `C0CC26ZSBH7`.
- **Onboarding** needed `openshell settings set -g nemoclaw --global --key providers_v2_enabled --value true --yes`
  on NemoClaw's gateway, then `nemoclaw onboard --resume`. Non-interactive resume works with the Slack env vars set,
  once the user has accepted the notice.
- **The verifier must listen on 0.0.0.0** for the sandbox to reach `host.openshell.internal:8200`.
  Use `VERIFIER_HOST=0.0.0.0` (`demo_up.sh` defaults to it).
- **Button-strip gotcha.** A click that lands while the plugin isn't loaded (onboarding, a gateway restart, a plugin
  reload) makes OpenClaw replace the card's buttons with "✅ … selected by @user" and wake its agent ("⚠️ Memory Search
  blocked"). No config turns this off. Don't click during restarts. To restore a card:
  `RUNS_DIR=<dir> bash scripts/refresh_card.sh <app_id>`.
- **Logs.** Handled clicks are logged only in the sandbox's `/sandbox/.openclaw/logs/gateway-persistent.log`.
- **Usernames.** OpenClaw gives no username, so the plugin sends the id as `name`, and the service turns it into `<@U…>`.
- **vLLM warning.** Onboarding warned that vLLM's `max_model_len=262144` leaves little unified-memory headroom.
  It has been fine; if the host ever freezes under agent load, lower it.

## Decisions made (keep consistent)

- Placeholder resumes list one language per project. Henry's claim ids: c1 Polytrack-ML, c2 CAC24, c3 cubesolver,
  c4–c7 skills, c8 employment, c9 education.
- Edit modal: `callback_id` is `openclaw:rv:edit_claim:<app>:<claim>:<version>`. The modal stays on the real `render`,
  not the registry, because the stub has no suffix.
- J13 `pr_count` counts merged PRs only.
- Coders run tests with `RUNS_DIR=$(mktemp -d)`, commit only their module's paths, then `git pull --rebase` and push.
  The user approved pushing each finished piece to `main`.

## Machine state and gotchas

- **Tools.** Venv `.venv`. Node 22, OpenShell and NemoClaw are in `~/.local`:
  `export PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH`. Run nemoclaw with `env -u XDG_STATE_HOME -u XDG_CONFIG_HOME`.
  Docker needs `sg docker -c "<cmd>"` in older shells.
- **`.env` has lost its `LLM_*` lines before.** Check:
  `.venv/bin/python -c "from dotenv import dotenv_values as d; print(d('.env').get('LLM_BASE_URL'))"`.
  It now also has `SLACK_APP_TOKEN` (xapp).
- **The GitHub repo moved** to `git@github.com:nguyenjake01/GreenLight.git`. The old URL redirects; update `origin`
  when convenient.
- **Restarting vLLM:** `sg docker -c "bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh"`.
- **`run_check` hides an LLM connection failure.** You see agent "stopped: error" and judge null when `.env` wasn't loaded.
- **Recorder quirk:** GitHub's 204 for an empty repo's `contributors` should be treated like 404/409.

## Open items for the user

- **Karpathy.** Another session left these uncommitted: the `karpathy` entry in `candidates.json`,
  `make_placeholder_resumes.py`, `fixtures/resumes|webhooks/karpathy.*`, `fixtures/claims/karpathy.json`,
  four repo archives and 122 cache files. The user said "leave it for now", and the video script's beat 7 uses it.
  Decide whether to commit it.
- **`docs/video-script.md`** (untracked, from another session) is not reviewed.
- **Slack channel cleanup:** the stray failed Henry card and the test messages.
- **Tag `contracts-v1`** (Jake and Henry both agree).
- **Amendment A1** (networked run checks) is still proposed, not approved.
