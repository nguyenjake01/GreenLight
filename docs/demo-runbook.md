# Demo runbook (recording day)

Pairs with `docs/video-script.md` (beats) and `docs/handoff.md` (machine state). Run everything from the repo root on the GB10.

## Before recording (in this order)

1. **vLLM answers.** `curl -s localhost:8000/v1/models | head -c 200`. If not, restart it:
   `sg docker -c "bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh"` and wait until `/v1/models` answers.
2. **OpenClaw is ready.** The sandbox is `rv-openclaw`. `demo_up.sh` checks it for you (step 3); to check by hand:
   `env -u XDG_STATE_HOME -u XDG_CONFIG_HOME PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH nemoclaw rv-openclaw channels status --channel slack --json`
   (run it under `sg docker -c` in an older shell). You want `"verdict": "healthy"`. Don't restart OpenClaw or reload the plugin from here on.
3. **Rehearse with Slack stubbed (warm-up, video-script item 6).** `bash scripts/demo_up.sh` (no `SLACK=real`), click
   *Simulate application* for henry, aidan, karpathy and note how long each takes. The filled card lands about 30 s after the click; the run lines
   then stream for 2 to 4 minutes (measured with the Docker backend while vLLM was shared: Henry 168 s, Aidan 240 s in all). Then Ctrl-C. This warms vLLM, Docker and the sandbox image
   without touching the Slack channel.
4. **Start the real stack.** `SLACK=real bash scripts/demo_up.sh`. Leave this terminal off screen. It prints the URLs and the
   runs dir (`/tmp/rv-demo-XXXX`); the real stack runs on ports 8101 (mock ATS) and 8201 (verifier); the verifier must stay on 8201 because the OpenClaw
   plugin points there. Stubbed stacks (rehearsals, other sessions' tests) use 8100/8200, so the two can run side by side.
   Add `DETACH=1` to get the terminal back.
5. **Browser tabs.** Firefox: `http://localhost:8101/` (mock ATS). Slack in light theme, demo channel only. Zoom about 125%.
   Check `.env` still has the `LLM_*` lines if the first run reports "couldn't read claims" (handoff, gotchas).
6. **Clean the channel** of old test cards (the stray "couldn't read claims" Henry card), so only the demo cards show.

If the browser is on another machine, start with `ATS_HOST=0.0.0.0 ATS_PUBLIC_URL=http://<GB10 address>:8101` so *Open in Greenhouse* works there.

## Click path per beat

- **3 Henry applies:** mock ATS index, click *Simulate application: henry*. The page just redirects back; switch to Slack. The placeholder card appears within a couple of seconds, and the filled card about 30 s later.
- **4 The card:** the tally (🟢 6 🟡 1 🔴 0 ⚪ 2), then scroll to the PROJECTS rows.
- **5 It runs the code:** open the card's thread. Run lines go from "running…" to the result as each repo finishes.
- **6 Aidan:** mock ATS, *Simulate application: aidan* (expect 🟢 8 🟡 0 🔴 1 ⚪ 2), then his card.
- **7 Karpathy (only if his fixtures are committed; the user hasn't decided yet):** *Simulate application: karpathy* (expect 🟢 7 ⚪ 5). Start it early so the card is ready when the beat comes. Otherwise cut the beat.
- **8 Candidate page:** on Aidan's card click *Open in Greenhouse*. It opens `http://localhost:8101/candidates/<id>?application_id=<id>`, with the annotated resume in the viewer, hover a mark, then scroll to the private note.
- **9 @-mention:** in Aidan's thread type `@<bot> why is portfolio-aidan-hong red?`. The answer takes about 12 s.

Each run adds its own card; the mock ATS index lists every application, newest first.

## Live upload

A candidate can apply on the mock ATS careers page, `http://localhost:8101/apply` (name, email, optional GitHub profile, resume PDF up to 5 MB). It fires the same signed webhook as the Simulate buttons.

- A new candidate needs `GITHUB_FETCH_MODE=live`. Replay mode only has the recorded candidates. Live mode calls GitHub with `GITHUB_TOKEN`, and the run checks git-clone the candidate's repos.
- To hide the Simulate buttons and show a link to the careers page instead, start with `MOCK_ATS_HIDE_SIMULATE=1 bash scripts/demo_up.sh`.
- The canned runs still work from a terminal when the buttons are hidden: `curl -s -X POST localhost:8101/_mock/apply -H 'content-type: application/json' -d '{"fixture":"henry"}'`

## If something goes wrong

- **Card lost its buttons** (a click landed while OpenClaw was restarting or reloading): `RUNS_DIR=<runs dir> bash scripts/refresh_card.sh <application_id>`. The application id is in the card's candidate URL and on the mock ATS index.
- **Don't click card buttons during an OpenClaw gateway restart or plugin reload.** Wait until `channels status` says healthy again.
- **Stack won't start, "port in use":** `bash scripts/demo_down.sh` (or `RUNS_DIR=<dir> bash scripts/demo_down.sh`). It only stops the mock ATS and the verifier it started.
- **Run lines say "couldn't test the behavior here" for everything:** check `<runs dir>/_logs/service.log` and that the OpenShell gateway is healthy: `PATH=$HOME/.local/bin:$PATH; source <(bash verifier/stack/openshell/gateway.sh env); openshell gateway info`.
- **vLLM died mid-run:** restart it (step 1), then click *Simulate application* again. A new application gets a new card.
- **Logs:** `<runs dir>/_logs/ats.log`, `service.log`, and `gateway.log` if demo_up started the gateway.

## Shutdown

- Ctrl-C in the `demo_up.sh` terminal, or `bash scripts/demo_down.sh` if you used `DETACH=1`. Runs stay in `/tmp/rv-demo-XXXX` (never commit them).
- `STOP_GATEWAY=1 bash scripts/demo_down.sh` also stops the OpenShell run-check gateway. Leave OpenClaw and vLLM running.
