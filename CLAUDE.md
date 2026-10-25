# NvidiaHackathon: Resume Verifier

`docs/design-doc.md` is the source of truth, and `docs/coding-prompts.md` holds each module's prompt. Read the sections a task cites before starting it.

## Lead and coders

- The main session is the lead: Opus 5.5 on high effort. It reads, plans, splits work, reviews and integrates. It doesn't write large amounts of code itself.
- Code writing goes to Sonnet 5.5 subagents: the `coder` agent (`.claude/agents/coder.md`), or the Agent tool with `model: "sonnet"`.
- Give each coder a full brief, since it starts without this conversation: the files it owns, the spec or interfaces to follow (cite the design doc sections), how to test, and the done line.
- Small edits (a line or two, a config value, a doc fix) are fine for the lead to make directly.
- The lead reviews every coder's result (reads the diff, reruns the tests) before calling the work done or committing it.

## Parallelize

- Whenever tasks don't depend on each other, run them at the same time: several coders in one message, and independent reads, searches and commands batched together.
- Give each parallel coder its own files. Two coders never edit the same file at once; if work overlaps, run it in order or split the files differently.
- Run work in sequence only when one step needs another's output.
- The lead never sits waiting. Anything slow that doesn't block the next step (a test suite, a live run, a build,
  a long check) goes to a background subagent (or a background command), and the lead keeps working meanwhile.

## Project rules (from the design doc, §13)

- `verifier/contracts/` is frozen after the `contracts-v1` tag. Change it only when Jake and Henry both agree.
- Each lane works only in its own clone (lane H on the GB10, `rv-a/` and `rv-b/` on Henry's laptop).
- Push to `main` only when the module's own tests and `python -m verifier.demo --stub` both pass; `git pull --rebase` first. Never push a half-finished module.
- Never commit `runs/` or `.env`.
