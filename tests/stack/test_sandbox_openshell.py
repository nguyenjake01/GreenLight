"""OpenShell backend tests with a fake CLI runner (no gateway, no Docker needed)."""
from __future__ import annotations

import sys

import pytest
import yaml

from verifier.contracts.errors import SandboxError
from verifier.stack import sandbox_openshell as so
from verifier.stack.sandbox_openshell import ProcResult


def ok(out=b"", err=b"", **kw):
    return ProcResult(0, out, err, **kw)


class FakeRunner:
    """Records every argv; `handlers` maps the sandbox subcommand to a reply (or a callable)."""

    def __init__(self, **handlers):
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.handlers = handlers

    def __call__(self, argv, timeout_s):
        self.calls.append(argv)
        self.timeouts.append(timeout_s)
        sub = argv[2]   # [bin, "sandbox", <sub>, ...]
        if sub == "exec" and "true" == argv[-1] and "--workdir" not in argv:
            sub = "ready"
        reply = self.handlers.get(sub, ok())
        reply = reply(argv) if callable(reply) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    def subs(self):
        out = []
        for a in self.calls:
            out.append("ready" if a[2] == "exec" and "--workdir" not in a else a[2])
        return out


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(so, "_sleep", lambda s: None)
    monkeypatch.setattr(so, "openshell_bin", lambda: "openshell")


def test_lifecycle_commands(tmp_path):
    r = FakeRunner()
    with so.open_sandbox(tmp_path, runner=r) as sb:
        assert sb.backend == "openshell"
        res = sb.exec("echo hi", 10)
    assert res["exit_code"] == 0 and res["timed_out"] is False
    assert r.subs() == ["create", "ready", "upload", "exec", "delete"]
    create, _ready, upload, execc, delete = r.calls
    name = create[create.index("--name") + 1]
    assert name.startswith("rv-")
    assert create[create.index("--from") + 1] == "resume-verifier-runcheck:latest"
    assert create[create.index("--policy") + 1] == str(so.POLICY_PATH)
    assert "--memory" in create and "4Gi" in create
    assert upload[:5] == ["openshell", "sandbox", "upload", name, str(tmp_path)]
    assert "/work" in upload
    # exec: bash -lc <cmd> in /work, targeted at THIS sandbox, with a hard in-sandbox kill
    assert execc[execc.index("-n") + 1] == name
    assert execc[execc.index("--workdir") + 1] == "/work"
    tail = execc[execc.index("--") + 1:]
    assert tail == ["timeout", "-s", "KILL", "10", "bash", "-lc", "echo hi"]
    assert delete == ["openshell", "sandbox", "delete", name]


def test_exec_passes_command_verbatim_and_captures_output(tmp_path):
    r = FakeRunner(exec=lambda argv: ProcResult(3, b"out\n", b"err\n"))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("printf 'a b' | ./x; echo \"$HOME\"", 5)
    assert res["exit_code"] == 3 and res["stdout"] == b"out\n" and res["stderr"] == b"err\n"
    assert res["duration_s"] >= 0
    assert r.calls[-2][-1] == "printf 'a b' | ./x; echo \"$HOME\""


def test_timeout_from_host_backstop(tmp_path):
    r = FakeRunner(exec=ProcResult(None, b"partial", b"", timed_out=True))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("sleep 99", 2)
    assert res["timed_out"] is True and res["exit_code"] is None
    assert res["stdout"] == b"partial"


def test_timeout_from_in_sandbox_kill(tmp_path, monkeypatch):
    r = FakeRunner(exec=ProcResult(137, b"", b""))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        clock = iter([100.0, 102.1])
        monkeypatch.setattr(so.time, "monotonic", lambda: next(clock))
        res = sb.exec("sleep 99", 2)
    assert res["timed_out"] is True and res["exit_code"] is None


def test_exit_137_without_waiting_is_not_a_timeout(tmp_path):
    """An OOM kill (137) well before the limit is a normal failure, not a timeout."""
    r = FakeRunner(exec=ProcResult(137, b"", b"Killed"))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("hog", 60)
    assert res["timed_out"] is False and res["exit_code"] == 137


def test_output_cap_1mb_with_truncation_marker(tmp_path):
    big = b"x" * (so.OUTPUT_CAP + 500)
    r = FakeRunner(exec=ProcResult(0, big[:so.OUTPUT_CAP], b"", stdout_truncated=True))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("yes", 5)
    assert len(res["stdout"]) == so.OUTPUT_CAP
    assert res["stdout"].endswith(so.TRUNCATION_MARKER)
    assert res["stderr"] == b""


def test_small_output_has_no_marker(tmp_path):
    r = FakeRunner(exec=ProcResult(0, b"ok", b""))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        assert sb.exec("true", 5)["stdout"] == b"ok"


def test_create_failure_raises_sandbox_error_and_still_deletes(tmp_path):
    r = FakeRunner(create=ProcResult(1, b"", b"no gateway configured"))
    with pytest.raises(SandboxError, match="create failed.*no gateway"):
        with so.open_sandbox(tmp_path, runner=r):
            pytest.fail("body must not run")
    assert r.subs() == ["create", "delete"]


def test_upload_failure_raises_and_tears_down(tmp_path):
    r = FakeRunner(upload=ProcResult(2, b"", b"disk full"))
    with pytest.raises(SandboxError, match="upload failed"):
        with so.open_sandbox(tmp_path, runner=r):
            pytest.fail("body must not run")
    assert r.subs()[-1] == "delete"


def test_teardown_runs_when_body_raises(tmp_path):
    r = FakeRunner()
    with pytest.raises(ValueError, match="boom"):
        with so.open_sandbox(tmp_path, runner=r):
            raise ValueError("boom")
    assert r.subs()[-1] == "delete"


def test_teardown_error_does_not_mask_body_error(tmp_path):
    r = FakeRunner(delete=ProcResult(1, b"", b"gateway down"))
    with pytest.raises(ValueError, match="boom"):
        with so.open_sandbox(tmp_path, runner=r):
            raise ValueError("boom")


def test_teardown_error_surfaces_after_clean_run(tmp_path):
    r = FakeRunner(delete=ProcResult(1, b"", b"gateway down"))
    with pytest.raises(SandboxError, match="delete"):
        with so.open_sandbox(tmp_path, runner=r):
            pass


def test_delete_not_found_is_fine(tmp_path):
    r = FakeRunner(delete=ProcResult(1, b"", b"Error: sandbox not found"))
    with so.open_sandbox(tmp_path, runner=r):
        pass


def test_cli_missing_is_sandbox_error(tmp_path):
    r = FakeRunner(create=SandboxError("openshell CLI not found: openshell"))
    with pytest.raises(SandboxError, match="not found"):
        with so.open_sandbox(tmp_path, runner=r):
            pass


def test_never_ready_times_out(tmp_path, monkeypatch):
    t = iter(range(0, 10_000, 40))
    monkeypatch.setattr(so.time, "monotonic", lambda: float(next(t)))
    r = FakeRunner(ready=ProcResult(1, b"", b"sandbox is provisioning"))
    with pytest.raises(SandboxError, match="not ready"):
        with so.open_sandbox(tmp_path, runner=r):
            pass
    assert r.subs()[-1] == "delete"


def test_ready_probe_retries_until_ok(tmp_path):
    answers = iter([ProcResult(1, b"", b"starting"), ProcResult(1, b"", b"starting"), ok()])
    r = FakeRunner(ready=lambda argv: next(answers))
    with so.open_sandbox(tmp_path, runner=r):
        pass
    assert r.subs().count("ready") == 3


def test_two_sandboxes_open_at_once_use_distinct_names(tmp_path):
    r = FakeRunner()
    with so.open_sandbox(tmp_path, runner=r) as a, so.open_sandbox(tmp_path, runner=r) as b:
        assert a.name != b.name
        a.exec("echo a", 5)
        b.exec("echo b", 5)
    execs = [c for c in r.calls if c[2] == "exec" and "--workdir" in c]
    assert {c[c.index("-n") + 1] for c in execs} == {a.name, b.name}
    deletes = {c[-1] for c in r.calls if c[2] == "delete"}
    assert deletes == {a.name, b.name}


# ── real subprocess runner (uses python, not the CLI) ────────────────────────────────────
def test_subprocess_runner_caps_output_and_stays_bounded():
    code = "import sys; sys.stdout.write('x' * 3_000_000); sys.stderr.write('e')"
    res = so.subprocess_runner([sys.executable, "-c", code], 30)
    assert res.returncode == 0 and res.stdout_truncated is True
    assert len(res.stdout) == so.OUTPUT_CAP and res.stderr == b"e"


def test_subprocess_runner_times_out_and_kills_group():
    code = ("import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print('started', flush=True); time.sleep(60)")
    res = so.subprocess_runner([sys.executable, "-c", code], 1.0)
    assert res.timed_out is True and res.returncode is None
    assert b"started" in res.stdout


def test_subprocess_runner_missing_binary():
    with pytest.raises(SandboxError, match="not found"):
        so.subprocess_runner(["/nonexistent/openshell", "x"], 5)


def test_openshell_bin_env_override(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("OPENSHELL_BIN", "/opt/os/openshell")
    assert so.openshell_bin() == "/opt/os/openshell"


# ── policy file ──────────────────────────────────────────────────────────────────────────
def test_policy_is_no_network_hard_landlock_and_minimal_writes():
    pol = yaml.safe_load(so.POLICY_PATH.read_text())
    assert pol["version"] == 1
    assert pol["network_policies"] == {}
    assert pol["landlock"]["compatibility"] == "hard_requirement"
    fs = pol["filesystem_policy"]
    assert set(fs["read_write"]) == {"/work", "/tmp", "/dev/null"}
    assert not set(fs["read_write"]) & set(fs["read_only"])
    assert str(pol["process"]["run_as_user"]) not in ("0", "root")
