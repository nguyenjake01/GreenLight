# Handoff: Resume Verifier build (checkpoint, Oct 25 2026, ~13:36 CDT)

Read this first in the next session, then `CLAUDE.md`, then the design-doc sections a task cites.
Setup: one machine (the GB10 itself, arm64, Python 3.12). The lead (Opus) plans and reviews;
Sonnet `coder` agents write the code, several at once on separate files.

## Where things stand

**On `main` (pushed, reviewed, tests and `demo --stub` passing at each commit):**

| Commit | Module |
|---|---|
| c5fef2f | J1 contracts, stubs, registry, store, `read_resume`, fixtures, thin runner, `demo.py`; demo repo archives in `fixtures/repos/` |
| 0d206dc | H3 Slack transport, J4 `link_github` |
| a96a4bc | H2 `llm_chat` + claim extraction |
| 201e910 | J6 Slack card / thread renderers (golden card `fixtures/render/henry_card.json`) |
| 50e4eae | J8 annotated resume PDF |
| a33e5cc | J10 run harness + Docker backend; H6 OpenShell sandbox, run agent, judge |
| 0d1d08b | J2 GitHub fetcher, matching, recorder |
| c10307d | Henry's real GitHub evidence in `fixtures/cache/` (22 responses, unauthenticated) |
| 62c4dd8 | J7 orchestrator (threads, per-app lock, meta.json, stage errors, write-back cap) |
| 26f0624 | J3 mock ATS, Greenhouse adapter, Harvest v3, webhook route; J11 candidate page + vendored pdf.js |
| 19299ba | J5 rubric (project, skill) incl. J10's `run` check, golden tests |

`contracts-v1` is **not tagged** yet (Jake's job in the plan; tag when convenient).

**Uncommitted work from coders stopped mid-task (review before keeping):**
- `verifier/core/service.py` (+295 lines) and `tests/core/test_service_internal.py`: **J9 internal API**,
  nearly done. The coder was fixing one test assumption ("more claims than runs"). Rerun
  `pytest tests/core/test_service_internal.py tests/core/test_service_webhook.py`, fix, commit.
- `verifier/core/pipeline.py` (+95/-) and `verifier/core/render.py` (+106): **J12 Edit claim**, in progress
  (real `render_edit_modal`, `validate_edit`, `edit_claim`). **J14** (`core/generate.py`, prompts) not started.
  Check `pytest tests/core/test_pipeline.py tests/core/test_render.py` still pass; either finish J12 or
  `git checkout` these two files and restart J12 → J14 with one coder.
- `verifier/stack/openclaw/resume-verifier/` and `tests/stack/fake_actions_server.py`: **H4+H5 OpenClaw plugin**,
  src/tests/dist exist; the coder was reading a NemoClaw doc on plugin install. Unverified: does it compile,
  do node tests pass. `dist/` and `node_modules/` probably shouldn't be committed.
- **J13 `oss_contribution`**: stopped early; no files changed (rubric.py is clean). Restart it.

## Stack / machine state
- Venv: `.venv` (all deps from `~/Downloads/hackathon/d-bundle/wheelhouse` + pip). Always use `.venv/bin/python`.
- USB contents are copied at `~/Downloads/hackathon/` (a–f bundles). Model weights copied to
  `~/.cache/huggingface/hub/` and sha256-verified.
- Installed user-locally: Node 22.23.4 (`~/.local/node/bin`), OpenShell 0.0.116 (`~/.local/bin`),
  NemoClaw v0.0.137 (`~/.nemoclaw/source`, `npm link`ed). **Not on PATH by default**: run
  `export PATH=$HOME/.local/node/bin:$HOME/.local/bin:$PATH` (editing `.bashrc` was blocked).
- Docker: user `dell` is now in the `docker` group, but older shells need `sg docker -c "<cmd>"`.
- Images `nvcr.io/nvidia/vllm:nemoclaw-qwen36-9204569b` and `functional-run-sandbox:arm64` are loaded,
  and container `vllm-qwen` was started at the checkpoint (startup takes several minutes; not yet
  answering then). Check: `sg docker -c "docker logs --tail 20 vllm-qwen"`, `curl -s localhost:8000/v1/models`.
  If the container is gone, restart with `bash ~/Downloads/hackathon/a-bundle/start-vllm-offline.sh` (via `sg docker`).
  Full reload from scratch, if ever needed:
  `cd ~/Downloads/hackathon && sg docker -c "docker load -i a-bundle/nvcr.io_nvidia_vllm_nemoclaw-qwen36-9204569b.arm64.tar && docker load -i c-bundle/functional-run-sandbox.arm64.tar && bash a-bundle/start-vllm-offline.sh"`
- vLLM serves the model as `nvidia/Qwen3.6-35B-A3B-NVFP4` → set `LLM_MODEL` to that and
  `LLM_BASE_URL=http://localhost:8000/v1` in `.env` (verifier on the host, design doc §5.2).
- `.env` exists (user-created). `GREENHOUSE_WEBHOOK_SECRET` and `INTERNAL_TOKEN` set;
  **`GITHUB_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID` still blank**; fake candidate's GitHub login not provided.

## Decisions made (keep consistent)
- Resumes are **placeholders** (user's choice): `fixtures/resumes/make_placeholder_resumes.py` makes
  `henry.pdf` / `fake.pdf`. Fake login placeholder everywhere: `rv-fake-candidate`. Henry's claims are
  c1 Polytrack-ML, c2 CAC24, c3 cubesolver, c4–c7 skills, c8 employment, c9 education.
- `fixtures/repos/henrychen4736__{cubesolver,polytrack-ml,cac24}.tar.gz` are packed from the USB e-bundle
  (working trees with offline deps), not from `git archive`.
- Lead edit in `core/harness.py`: `classify_failure` treats `ENOENT: no such file or directory` as
  `deps_missing`, so Polytrack-ML's unshipped data file reads "could not run here", not a failure.
- Push rule: user approved pushing each module to `main` once its tests and `demo --stub` pass.
  Commit only the module's own files (`git add <paths>`), then `git pull --rebase --autostash` and push.
- Coders run tests with `RUNS_DIR=$(mktemp -d)` so parallel runs don't collide.

## Known issues / next steps (in order)
1. Finish and commit J9 (uncommitted, nearly done).
2. Finish J12, then J14 (one coder: they share `pipeline.py` and `render.py`).
3. Restart J13 (rubric.py contribution checks; hand-made oss responses in `fixtures/golden/fake/`).
4. **Fixture refresh** (after J12 is in, since it touches the render golden):
   - Real rubric on the recorded evidence gives Polytrack-ML 🟡 because the placeholder resume claims
     "Python, JavaScript" and Python is ~6% of that repo (HTML 57%, JS 35%). Change the placeholder line to
     JavaScript only, regenerate `henry.pdf`, `henry.resume.json`, `fixtures/claims/henrychen4736.json`,
     `fixtures/llm/extraction_output.json`.
   - Replace `fixtures/snapshots/sample/` with a copy of a real replay snapshot (§14), regenerate
     `fixtures/verdicts/henrychen4736.json` from the real rubric, `UPDATE_GOLDEN=1` for the henry golden case
     and `fixtures/render/henry_card.json`. CAC24 is 🟢 on real data (Python 45%).
5. Stack bring-up (H1): confirm vLLM serves; run H2 live extraction on both resumes (diff against fixtures);
   build/verify run-check image (`docker/runcheck.Dockerfile`, or use `functional-run-sandbox:arm64`
   via `RUNCHECK_IMAGE`) and run J10's live Docker check on cubesolver + `tests/core/fixtures_repos/cpp_broken`;
   `nemoclaw onboard` (needs a short internet window) for OpenClaw + Slack; OpenShell commands in
   `stack/sandbox_openshell.py` are partly guessed (tagged GUESS) and need a live check.
6. With Slack tokens: `.venv/bin/python tests/stack/live_slack_check.py`; install the OpenClaw plugin, probe
   button routing (H4 Step 0), pick `SLACK_ACTIONS`.
7. Full-system run (§6.4): mock ATS on 8100, service on 8200, `python -m verifier.ats.apply henry`,
   with `VERIFIER_REAL` set to the real modules.
8. Record the fake candidate's account once its login is known (needs `GITHUB_TOKEN` or the 60/h quota).

## Open items for the user
- Slack bot token + channel ID, GitHub token, fake candidate login → `.env`.
- Tag `contracts-v1`.
- Real resume PDFs, if they should replace the placeholders.
