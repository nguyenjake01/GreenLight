"""J10 harness tests: baseline selection, classify_failure, RecordingSandbox, README filter,
run_check with a fake sandbox, and the Docker backend against a fake `docker` binary."""
from __future__ import annotations

import json
import stat
import sys
import tarfile
import textwrap
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from verifier.contracts.errors import LLMError, LLMSchemaError, SandboxError
from verifier.contracts.snapshot import Snapshot
from verifier.core import harness, store

REPOS = Path(__file__).resolve().parent / "fixtures_repos"
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
CLAIMS = json.loads((FIXTURES / "claims" / "henrychen4736.json").read_text())
CLAIM = next(c for c in CLAIMS["claims"] if c["claim_id"] == "c3")


# ── helpers ───────────────────────────────────────────────────────────────────

def res(out: str = "", err: str = "", code: int | None = 0, dur: float = 0.1, timed_out: bool = False):
    return {"exit_code": code, "stdout": out.encode(), "stderr": err.encode(),
            "duration_s": dur, "timed_out": timed_out}


class FakeSandbox:
    backend = "fake"

    def __init__(self, responder=None):
        self.calls: list[tuple[str, float]] = []
        self.responder = responder or (lambda cmd: res(f"ran {cmd}\n"))

    def exec(self, cmd, timeout_s):
        self.calls.append((cmd, timeout_s))
        return self.responder(cmd)


@pytest.fixture
def snap(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("RUN_CHECK", raising=False)
    path = tmp_path / "runs" / "app1" / "snapshots" / "snap_1"
    path.mkdir(parents=True)
    return Snapshot(path, {"snapshot_id": "snap_1"})  # type: ignore[arg-type]


@pytest.fixture
def wire(monkeypatch):
    """Patch registry.get inside the harness with a fake sandbox, agent and judge."""
    state = SimpleNamespace(sandbox=FakeSandbox(), closed=0, opened=[], agent_calls=[],
                            judge_calls=[], judge_result=None, judge_exc=None, agent_fn=None)

    @contextmanager
    def open_sandbox(repo_dir):
        state.opened.append(Path(repo_dir))
        assert Path(repo_dir).is_dir()
        try:
            yield state.sandbox
        finally:
            state.closed += 1

    def agent_probe(sb, claim, readme, *, max_commands=15, timeout_s=300.0):
        state.agent_calls.append({"sb": sb, "readme": readme, "max_commands": max_commands,
                                  "timeout_s": timeout_s})
        if state.agent_fn:
            state.agent_fn(sb)
        else:
            sb.exec("ls -la", 10.0)
            sb.exec("cat README.md", 10.0)
            sb.exec("./demo", 10.0)
        return {"model": "m", "prompt_version": "run-v1", "stopped": "done", "final_note": ""}

    def judge(claim, transcript):
        state.judge_calls.append(transcript)
        if state.judge_exc:
            raise state.judge_exc
        return state.judge_result or {"model": "j", "prompt_version": "judge-v1",
                                      "outcome": "works_as_claimed", "rationale": "ok"}

    mods = {"sandbox": SimpleNamespace(open_sandbox=open_sandbox),
            "agent": SimpleNamespace(agent_probe=agent_probe),
            "judge": SimpleNamespace(judge=judge)}
    monkeypatch.setattr(harness.registry, "get", lambda name: mods[name])
    return state


# ── classify_failure ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("output,kind", [
    ("curl: (6) Could not resolve host: example.com", "needs_network"),
    ("Temporary failure in name resolution", "needs_network"),
    ("Name or service not known", "needs_network"),
    ("Error: getaddrinfo ENOTFOUND registry.npmjs.org", "needs_network"),
    ("npm ERR! network request failed", "needs_network"),
    ("urllib3 NewConnectionError", "needs_network"),
    ("Could not fetch URL https://pypi.org/simple/x/", "needs_network"),
    ("ModuleNotFoundError: No module named 'numpy'", "deps_missing"),
    ("Error: Cannot find module 'express'", "deps_missing"),
    ("ERR_MODULE_NOT_FOUND", "deps_missing"),
    ("main.cpp:1:10: fatal error: foo.h: No such file or directory", "deps_missing"),
    ("bash: cmake: command not found", "deps_missing"),
    ("bash: line 1: cmake: not found", "deps_missing"),
    ("/usr/bin/ld: cannot find -lfoo", "deps_missing"),
    ("error: externally-managed-environment", "deps_missing"),
    ("main.cpp:2: error: 'x' was not declared", "error"),
    ("fatal error: something else", "error"),
    ("No such file or directory", "error"),
    ("", "error"),
])
def test_classify_failure(output, kind):
    assert harness.classify_failure(output) == kind


def test_classify_failure_network_wins_over_deps():
    assert harness.classify_failure("No module named x\nCould not resolve host") == "needs_network"


# ── excerpt ───────────────────────────────────────────────────────────────────

def test_excerpt_last_20_lines_and_char_cap():
    text = "\n".join(f"line{i}" for i in range(50))
    out = harness.excerpt(text)
    assert out.splitlines() == [f"line{i}" for i in range(30, 50)]
    assert len(harness.excerpt("x" * 5000)) == 1500
    assert harness.excerpt("a" * 5000 + "\nTAIL").endswith("TAIL")
    assert harness.excerpt("") == ""


# ── baseline selection ────────────────────────────────────────────────────────

@pytest.mark.parametrize("repo,step,command", [
    ("pytest_repo", "tests", "python3 -m pytest -q -x -p no:cacheprovider"),
    ("pytest_tests_dir", "tests", "python3 -m pytest -q -x -p no:cacheprovider"),
    ("npm_real", "tests", "npm test --silent"),
    ("makefile_repo", "build", "make -j2"),
    ("cmake_repo", "build", "cmake -S . -B /tmp/build && cmake --build /tmp/build -j2"),
    ("cpp_one_main", "build", "g++ -std=c++17 -O2 -o /tmp/a.out helper.cpp main.cpp"),
    ("cpp_two_mains", "build", "g++ -std=c++17 -O2 -fsyntax-only a.cpp b.cpp"),
    ("c_repo", "build", "gcc -O2 -o /tmp/a.out prog.c"),
    ("python_only", "syntax", "python3 -m py_compile app.py"),
    ("js_only", "syntax", 'for f in a.js b.mjs; do node --check "$f" || exit 1; done'),
    ("nothing", "skip", None),
])
def test_pick_baseline(repo, step, command):
    assert harness.pick_baseline(REPOS / repo) == (step, command)


def test_npm_default_test_script_falls_through_to_js_syntax():
    step, cmd = harness.pick_baseline(REPOS / "npm_default")
    assert (step, cmd) == ("syntax", 'for f in index.js; do node --check "$f" || exit 1; done')


def test_pytest_config_markers(tmp_path):
    for name, content in [("pyproject.toml", "[tool.pytest.ini_options]\nx=1\n"),
                          ("setup.cfg", "[tool:pytest]\nx=1\n"),
                          ("tox.ini", "[pytest]\nx=1\n"),
                          ("conftest.py", "")]:
        d = tmp_path / name.replace(".", "_")
        d.mkdir()
        (d / name).write_text(content)
        assert harness.pick_baseline(d)[0] == "tests", name
    other = tmp_path / "plain"
    other.mkdir()
    (other / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert harness.pick_baseline(other) == ("skip", None)


def test_source_lists_skip_hidden_and_vendored_dirs(tmp_path):
    for d in ("node_modules", "vendor", "build", "dist", ".git", ".venvx"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "bad.py").write_text("garbage(")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("x = 1\n")
    (tmp_path / "a.py").write_text("x = 1\n")
    assert harness.pick_baseline(tmp_path) == ("syntax", "python3 -m py_compile a.py pkg/b.py")


def test_source_list_capped_at_500(tmp_path):
    for i in range(520):
        (tmp_path / f"f{i:04d}.py").write_text("x = 1\n")
    _, cmd = harness.pick_baseline(tmp_path)
    assert cmd.count(".py") == 500
    assert "f0499.py" in cmd and "f0500.py" not in cmd


def test_paths_with_spaces_are_quoted(tmp_path):
    (tmp_path / "my prog.cpp").write_text("int main(){}\n")
    assert "'my prog.cpp'" in harness.pick_baseline(tmp_path)[1]


# ── baseline() ────────────────────────────────────────────────────────────────

def test_baseline_success():
    sb = FakeSandbox(lambda cmd: res("built\n", dur=3.14159))
    rec, out = harness.baseline(sb, REPOS / "cpp_one_main")
    assert rec["step"] == "build" and rec["exit_code"] == 0
    assert rec["failure_kind"] is None and rec["timed_out"] is False
    assert rec["duration_s"] == 3.142
    assert rec["output_path"] is None
    assert out == "built\n" and rec["output_excerpt"] == "built"
    assert sb.calls == [(rec["command"], 120.0)]


def test_baseline_failure_is_classified():
    sb = FakeSandbox(lambda cmd: res("", "main.cpp:2:5: error: 'x' undeclared\n", code=1))
    rec, out = harness.baseline(sb, REPOS / "cpp_broken")
    assert rec["exit_code"] == 1 and rec["failure_kind"] == "error"
    assert "undeclared" in rec["output_excerpt"] and "undeclared" in out

    sb = FakeSandbox(lambda cmd: res("", "ModuleNotFoundError: No module named 'x'\n", code=1))
    assert harness.baseline(sb, REPOS / "pytest_repo")[0]["failure_kind"] == "deps_missing"


def test_baseline_output_is_stdout_then_stderr():
    sb = FakeSandbox(lambda cmd: res("OUT\n", "ERR\n", code=1))
    _, out = harness.baseline(sb, REPOS / "python_only")
    assert out == "OUT\nERR\n"


def test_baseline_timeout_classifies_partial_output():
    sb = FakeSandbox(lambda cmd: res("Could not resolve host\n", code=None, dur=120.0, timed_out=True))
    rec, _ = harness.baseline(sb, REPOS / "makefile_repo")
    assert rec["timed_out"] is True and rec["exit_code"] is None
    assert rec["failure_kind"] == "needs_network"
    sb = FakeSandbox(lambda cmd: res("compiling...\n", code=None, dur=120.0, timed_out=True))
    assert harness.baseline(sb, REPOS / "makefile_repo")[0]["failure_kind"] == "error"


def test_baseline_skip_runs_nothing():
    sb = FakeSandbox()
    rec, out = harness.baseline(sb, REPOS / "nothing")
    assert sb.calls == []
    assert rec == {"step": "skip", "command": None, "exit_code": None, "duration_s": 0.0,
                   "timed_out": False, "failure_kind": None, "output_excerpt": "", "output_path": None}
    assert out == ""


# ── RecordingSandbox ──────────────────────────────────────────────────────────

def test_recording_sandbox_records_every_command(snap):
    inner = FakeSandbox(lambda cmd: res("\n".join(f"l{i}" for i in range(40)) + "\n", "warn\n", code=3, dur=0.5))
    rec = harness.RecordingSandbox(inner, snap.path)
    r = rec.exec("echo hi", 5.0)
    assert r["exit_code"] == 3 and r["stderr"] == b"warn\n"  # returned unchanged
    (c,) = rec.commands
    assert c["cmd"] == "echo hi" and c["exit_code"] == 3 and c["timed_out"] is False
    assert c["duration_s"] == 0.5
    assert c["output_excerpt"].splitlines()[-1] == "warn" and len(c["output_excerpt"].splitlines()) == 20
    raw = snap.path / c["output_path"]
    assert c["output_path"].startswith("raw/") and c["output_path"].endswith(".txt")
    assert raw.read_bytes() == ("\n".join(f"l{i}" for i in range(40)) + "\nwarn\n").encode()
    assert c["output_path"] == f"raw/{store.sha256_bytes(raw.read_bytes())}.txt"
    assert inner.calls == [("echo hi", 5.0)]


def test_recording_sandbox_same_output_same_raw_file(snap):
    rec = harness.RecordingSandbox(FakeSandbox(lambda cmd: res("same\n")), snap.path)
    rec.exec("a", 1)
    rec.exec("b", 1)
    a, b = rec.commands
    assert a["output_path"] == b["output_path"]
    assert len(list((snap.path / "raw").iterdir())) == 1


def test_recording_sandbox_records_timeouts(snap):
    inner = FakeSandbox(lambda cmd: res("partial", code=None, timed_out=True, dur=9.0))
    rec = harness.RecordingSandbox(inner, snap.path)
    rec.exec("sleep 99", 9)
    assert rec.commands[0]["timed_out"] is True and rec.commands[0]["exit_code"] is None


def test_recording_sandbox_command_budget(snap):
    inner = FakeSandbox()
    rec = harness.RecordingSandbox(inner, snap.path, max_commands=15)
    for i in range(15):
        assert rec.exec(f"c{i}", 1)["exit_code"] == 0
    r = rec.exec("one too many", 1)
    assert r["exit_code"] is None and b"budget used" in r["stderr"] and r["timed_out"] is False
    assert len(inner.calls) == 15 and len(rec.commands) == 15


def test_recording_sandbox_time_budget(snap):
    now = [1000.0]
    inner = FakeSandbox()
    rec = harness.RecordingSandbox(inner, snap.path, budget_s=300, clock=lambda: now[0])
    assert rec.exec("first", 1)["exit_code"] == 0
    now[0] += 301
    r = rec.exec("late", 1)
    assert r["exit_code"] is None and b"budget used" in r["stderr"]
    assert [c["cmd"] for c in rec.commands] == ["first"]


# ── README filter ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,expected", [
    ("cat README.md", True), ("cat readme", True), ("head -50 Readme.txt", True),
    ("less docs/guide.md", True), ("sed -n 1,20p NOTES.rst", True), ("tail CHANGELOG.md", True),
    ("grep -i usage README.rst", True), ("cat a.txt | head; cat b.md", True),
    ("ls -la", False), ("./demo", False), ("cat main.cpp", False), ("python3 app.py", False),
    ("cat out.txt > notes.md", True),  # conservative: prints/handles an .md with cat
    ("echo done", False),
])
def test_is_readme_read(cmd, expected):
    assert harness.is_readme_read(cmd) is expected


def test_build_transcript_starts_with_baseline_and_drops_readme_reads():
    base = {"step": "build", "command": "make -j2", "exit_code": 0, "duration_s": 1.0,
            "timed_out": False, "failure_kind": None, "output_excerpt": "ok", "output_path": "raw/x.txt"}
    cmds = [{"cmd": c, "exit_code": 0, "duration_s": 0.1, "timed_out": False,
             "output_excerpt": "", "output_path": "raw/y.txt"} for c in ("ls", "cat README.md", "./run")]
    tr = harness.build_transcript(base, cmds)
    assert [c["cmd"] for c in tr] == ["make -j2", "ls", "./run"]
    assert tr[0]["output_excerpt"] == "ok" and tr[0]["output_path"] == "raw/x.txt"
    skip = dict(base, step="skip", command=None, exit_code=None, output_path=None)
    assert harness.build_transcript(skip, [])[0]["cmd"] == "(no baseline)"


# ── README reading ────────────────────────────────────────────────────────────

def test_read_readme(tmp_path):
    assert harness.read_readme(REPOS / "with_readme").startswith("# Demo")
    assert harness.read_readme(REPOS / "nothing") is None
    (tmp_path / "README").write_text("x" * 9000)
    assert len(harness.read_readme(tmp_path)) == 8000
    (tmp_path / "README.md").write_text("md wins")
    assert harness.read_readme(tmp_path) == "md wins"


# ── run_check ─────────────────────────────────────────────────────────────────

def _make_archive(tmp_path, monkeypatch, files: dict[str, str], name="o__r"):
    fx = tmp_path / "fx"
    (fx / "repos").mkdir(parents=True)
    monkeypatch.setattr(harness, "FIXTURES_DIR", fx)
    src = tmp_path / "src"
    for rel, text in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    with tarfile.open(fx / "repos" / f"{name}.tar.gz", "w:gz") as tar:
        tar.add(src, arcname=".")
    (fx / "repos" / f"{name}.json").write_text(json.dumps({"repo": "O/R", "commit_sha": "a" * 40}))


def test_run_check_cubesolver_replay_full_trace(snap, wire):
    wire.sandbox = FakeSandbox(lambda cmd: res("Solved!\n"))
    trace = harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    sidecar = json.loads((FIXTURES / "repos" / "henrychen4736__cubesolver.json").read_text())
    assert trace["commit_sha"] == sidecar["commit_sha"]
    assert trace["repo"] == "henrychen4736/cubesolver" and trace["sandbox_backend"] == "fake"
    assert trace["baseline"]["step"] == "build"
    assert trace["baseline"]["command"] == "g++ -std=c++17 -O2 -o /tmp/a.out cubesolver.cpp"
    assert trace["baseline"]["failure_kind"] is None
    assert (snap.path / trace["baseline"]["output_path"]).read_text() == "Solved!\n"
    assert trace["agent"]["limits"] == {"max_commands": 15, "timeout_s": 300}
    assert trace["agent"]["stopped"] == "done" and trace["agent"]["timed_out"] is False
    assert [c["cmd"] for c in trace["agent"]["commands"]] == ["ls -la", "cat README.md", "./demo"]
    assert trace["judge"]["outcome"] == "works_as_claimed"
    # written through store, validated, and identical to what was returned
    written = store.read_json(snap.path / "run_traces" / "c3.json")
    assert written == trace
    store.validate(written, "run_trace")
    for c in trace["agent"]["commands"]:
        assert (snap.path / c["output_path"]).is_file()
    assert wire.closed == 1
    assert not wire.opened[0].exists()  # temp folder removed


def test_run_check_passes_readme_to_agent_but_not_judge(snap, wire, tmp_path, monkeypatch):
    _make_archive(tmp_path, monkeypatch, {"README.md": "SECRET-README-TEXT", "app.py": "print(1)\n"})
    trace = harness.run_check(snap, CLAIM, "O/R", mode="replay")  # archive names are lowercased
    assert wire.agent_calls[0]["readme"] == "SECRET-README-TEXT"
    assert wire.agent_calls[0]["max_commands"] == 15 and wire.agent_calls[0]["timeout_s"] == 300
    assert isinstance(wire.agent_calls[0]["sb"], harness.RecordingSandbox)
    (transcript,) = wire.judge_calls
    assert [c["cmd"] for c in transcript][0] == "python3 -m py_compile app.py"
    assert [c["cmd"] for c in transcript][1:] == ["ls -la", "./demo"]
    assert "SECRET-README-TEXT" not in json.dumps(transcript)
    # README reads stay in the trace
    assert "cat README.md" in [c["cmd"] for c in trace["agent"]["commands"]]


def test_run_check_baseline_only(snap, wire, monkeypatch):
    monkeypatch.setenv("RUN_CHECK", "baseline_only")
    trace = harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert trace["agent"] is None and trace["judge"] is None
    assert wire.agent_calls == [] and wire.judge_calls == []
    store.validate(store.read_json(snap.path / "run_traces" / "c3.json"), "run_trace")


@pytest.mark.parametrize("exc", [LLMError("down"), LLMSchemaError("bad", "{}")])
def test_run_check_judge_error_keeps_baseline(snap, wire, exc):
    wire.judge_exc = exc
    trace = harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert trace["judge"] is None and trace["agent"] is not None
    assert trace["baseline"]["step"] == "build"
    store.validate(trace, "run_trace")


def test_run_check_failed_baseline_in_trace(snap, wire):
    wire.sandbox = FakeSandbox(lambda cmd: res("", "cubesolver.cpp:3: error: boom\n", code=1))
    trace = harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert trace["baseline"]["exit_code"] == 1 and trace["baseline"]["failure_kind"] == "error"
    store.validate(trace, "run_trace")


def test_run_check_agent_exception_is_recorded_as_error(snap, wire):
    def boom(sb):
        sb.exec("ls", 5)
        raise RuntimeError("agent exploded")
    wire.agent_fn = boom
    trace = harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert trace["agent"]["stopped"] == "error" and len(trace["agent"]["commands"]) == 1
    store.validate(trace, "run_trace")


def test_run_check_agent_timeout_stop(snap, wire):
    wire.agent_fn = lambda sb: None
    # stopped value comes from agent_probe's result
    import verifier.core.harness as h
    h.registry.get("agent").agent_probe = lambda *a, **k: {
        "model": "m", "prompt_version": "run-v1", "stopped": "timeout", "final_note": ""}
    trace = harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert trace["agent"]["stopped"] == "timeout" and trace["agent"]["timed_out"] is True


def test_run_check_missing_archive_raises_sandbox_error(snap, wire):
    with pytest.raises(SandboxError, match="repo not recorded: nobody/nothing"):
        harness.run_check(snap, CLAIM, "nobody/nothing", mode="replay")
    assert wire.opened == []
    assert not (snap.path / "run_traces").exists()


def test_run_check_repo_too_large(snap, wire, tmp_path, monkeypatch):
    _make_archive(tmp_path, monkeypatch, {"big.bin": "x" * 5000})
    monkeypatch.setattr(harness, "MAX_REPO_BYTES", 1000)
    with pytest.raises(SandboxError, match="repo too large"):
        harness.run_check(snap, CLAIM, "o/r", mode="replay")


def test_run_check_cleans_up_on_sandbox_error(snap, wire, monkeypatch):
    wire.sandbox = FakeSandbox(lambda cmd: (_ for _ in ()).throw(SandboxError("docker died")))
    with pytest.raises(SandboxError, match="docker died"):
        harness.run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert wire.closed == 1 and not wire.opened[0].exists()
    assert not (snap.path / "run_traces" / "c3.json").exists()


def test_run_check_skip_baseline_when_nothing_to_build(snap, wire, tmp_path, monkeypatch):
    _make_archive(tmp_path, monkeypatch, {"notes.txt": "hi"})
    trace = harness.run_check(snap, CLAIM, "o/r", mode="replay")
    assert trace["baseline"]["step"] == "skip" and trace["baseline"]["output_path"] is None
    assert wire.judge_calls[0][0]["cmd"] == "(no baseline)"


def test_run_check_record_mode_clones(snap, wire, monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["git", "clone"]:
            dest = Path(argv[-1])
            dest.mkdir(parents=True)
            (dest / "app.py").write_text("print(1)\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    trace = harness.run_check(snap, CLAIM, "someone/repo", mode="record")
    assert calls[0][:4] == ["git", "clone", "--depth", "1"]
    assert "https://github.com/someone/repo.git" in calls[0]
    assert trace["commit_sha"] == "deadbeef"


# ── Docker backend (against a fake `docker` binary) ───────────────────────────

FAKE_DOCKER = textwrap.dedent("""\
    #!{python}
    import json, os, subprocess, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    if args[0] == "run":
        if os.environ.get("FAKE_DOCKER_RUN_FAIL"):
            sys.stderr.write(os.environ["FAKE_DOCKER_RUN_FAIL"])
            sys.exit(125)
        print("containerid")
    elif args[0] == "exec":
        i = args.index("timeout")
        os.execvp(args[i], args[i:])
""")


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "docker"
    exe.write_text(FAKE_DOCKER.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("RUNCHECK_IMAGE", "test-image:1")
    monkeypatch.delenv("FAKE_DOCKER_RUN_FAIL", raising=False)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return calls


def test_docker_run_flags_and_teardown(fake_docker):
    with harness.open_sandbox(REPOS / "python_only") as sb:
        assert sb.backend == "docker"
        run = fake_docker()[0]
        assert run[:3] == ["run", "-d", "--rm"]
        joined = " ".join(run)
        for needle in ["--network none", "--user 1000:1000", "--read-only", "--memory 4g",
                       "--pids-limit 256", "--cpus 2", "--cap-drop ALL",
                       "--security-opt no-new-privileges", "--tmpfs /tmp:rw,exec,nosuid,size=512m",
                       "-w /work", "--pull never"]:
            assert needle in joined, needle
        assert run[run.index("--name") + 1] == sb.name and sb.name.startswith("rv-")
        assert run[-2:] == ["sleep", "infinity"] and run[-3] == "test-image:1"
        mount = run[run.index("-v") + 1]
        host_copy, target = mount.rsplit(":", 1)
        assert target == "/work" and Path(host_copy, "app.py").is_file()
        assert Path(host_copy) != REPOS / "python_only"  # a copy, never the original
    calls = fake_docker()
    assert calls[-1] == ["rm", "-f", sb.name]
    assert not Path(host_copy).exists()


def test_docker_exec_runs_in_work_with_timeout(fake_docker):
    with harness.open_sandbox(REPOS / "python_only") as sb:
        r = sb.exec("echo out; echo err >&2; exit 3", 10)
    assert r["exit_code"] == 3 and r["stdout"] == b"out\n" and r["stderr"] == b"err\n"
    assert r["timed_out"] is False
    ex = [c for c in fake_docker() if c[0] == "exec"][0]
    assert ex[:3] == ["exec", "-w", "/work"]
    assert ex[4:8] == ["timeout", "-s", "KILL", "10"] and ex[8:10] == ["bash", "-lc"]


def test_docker_exec_timeout_kills(fake_docker):
    with harness.open_sandbox(REPOS / "python_only") as sb:
        r = sb.exec("echo started; sleep 20", 1)
    assert r["timed_out"] is True and r["exit_code"] is None
    assert r["duration_s"] < 10


def test_docker_exec_caps_output_at_1mb(fake_docker):
    with harness.open_sandbox(REPOS / "python_only") as sb:
        r = sb.exec("head -c 3000000 /dev/zero | tr '\\0' a; echo E >&2", 30)
    assert r["exit_code"] == 0
    assert len(r["stdout"]) == 1024 * 1024 + len(harness.TRUNCATED_MARKER)
    assert r["stdout"].endswith(b"[truncated]\n")
    assert r["stderr"] == b"E\n"


def test_docker_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SandboxError, match="docker not found"):
        with harness.open_sandbox(REPOS / "python_only"):
            pass


def test_docker_image_missing(fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_RUN_FAIL", "Unable to find image 'test-image:1' locally")
    with pytest.raises(SandboxError, match="image missing"):
        with harness.open_sandbox(REPOS / "python_only"):
            pass


def test_docker_failed_start(fake_docker, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_RUN_FAIL", "permission denied while trying to connect")
    with pytest.raises(SandboxError, match="docker run failed"):
        with harness.open_sandbox(REPOS / "python_only"):
            pass


def test_docker_removed_even_when_body_raises(fake_docker):
    with pytest.raises(ValueError):
        with harness.open_sandbox(REPOS / "python_only") as sb:
            raise ValueError("boom")
    assert fake_docker()[-1] == ["rm", "-f", sb.name]


def test_run_check_with_docker_backend_via_registry(snap, fake_docker, monkeypatch):
    """End to end through the registry with SANDBOX_BACKEND=docker and the fake docker binary."""
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("VERIFIER_REAL", "sandbox,harness")
    monkeypatch.setenv("RUN_CHECK", "baseline_only")
    from verifier.contracts import registry
    assert registry.get("sandbox") is harness
    trace = registry.get("harness").run_check(snap, CLAIM, "henrychen4736/cubesolver", mode="replay")
    assert trace["sandbox_backend"] == "docker" and trace["baseline"]["step"] == "build"
    store.validate(trace, "run_trace")
