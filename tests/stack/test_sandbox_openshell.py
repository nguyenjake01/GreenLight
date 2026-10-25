"""OpenShell backend tests with a fake CLI runner (no gateway, no Docker needed)."""
from __future__ import annotations

import sys

import pytest
import yaml

from verifier.contracts.errors import SandboxError
from verifier.stack import sandbox_openshell as so
from verifier.stack.sandbox_openshell import ProcResult


def sub_of(argv):
    """[bin, "sandbox", <sub>, ...]; the post-upload hoist is an exec running so._HOIST."""
    return "hoist" if argv[2] == "exec" and so._HOIST in argv else argv[2]


def ok(out=b"", err=b"", **kw):
    return ProcResult(0, out, err, **kw)


class FakeRunner:
    """Records every argv; `handlers` maps the sandbox subcommand to a reply (or a callable).

    Mirrors the real 0.0.116 CLI (checked live): `create` blocks until Ready and exits 0;
    `upload` of a directory to DEST lands at DEST/<basename> (we then hoist it with an `exec`
    that runs _HOIST, reported here as "hoist"); `exec` exits with the remote exit code.
    """

    def __init__(self, **handlers):
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.handlers = handlers

    def __call__(self, argv, timeout_s):
        self.calls.append(argv)
        self.timeouts.append(timeout_s)
        sub = sub_of(argv)
        reply = self.handlers.get(sub, ok())
        reply = reply(argv) if callable(reply) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    def subs(self):
        return [sub_of(a) for a in self.calls]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(so, "openshell_bin", lambda: "openshell")


def test_lifecycle_commands(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNCHECK_IMAGE", raising=False)  # a .env loaded by another test sets it
    monkeypatch.delenv(so.POLICY_ENV, raising=False)
    r = FakeRunner()
    with so.open_sandbox(tmp_path, runner=r) as sb:
        assert sb.backend == "openshell"
        res = sb.exec("echo hi", 10)
    assert res["exit_code"] == 0 and res["timed_out"] is False
    # create blocks until Ready (no readiness polling), then upload + hoist, exec, delete
    assert r.subs() == ["create", "upload", "hoist", "exec", "delete"]
    create, upload, hoist, execc, delete = r.calls
    name = create[create.index("--name") + 1]
    assert name.startswith("rv-")
    assert create[create.index("--from") + 1] == "resume-verifier-runcheck:latest"
    assert create[create.index("--policy") + 1] == str(so.POLICY_PATH)
    assert "--memory" in create and "4Gi" in create and "--detach" in create
    assert create[create.index("--") + 1:] == ["sleep", "infinity"]
    # upload lands at <dest>/<basename>, so it goes to the staging dir and is then hoisted
    assert upload == ["openshell", "sandbox", "upload", name, str(tmp_path), so.STAGE_DIR,
                      "--no-git-ignore"]
    assert hoist[hoist.index("-n") + 1] == name
    assert hoist[-1] == so._HOIST and so.STAGE_DIR in so._HOIST and "/work" in so._HOIST
    # exec: bash -lc <cmd> in /work, targeted at THIS sandbox, with a hard in-sandbox kill
    assert execc[execc.index("-n") + 1] == name
    assert execc[execc.index("--workdir") + 1] == "/work"
    assert execc[execc.index("--env") + 1] == "HOME=/tmp"
    tail = execc[execc.index("--") + 1:]
    assert tail == ["bash", "-c", 'timeout -s KILL "$1" bash -lc "$2"; exit $?', "_",
                    "10", "echo hi"]
    assert delete == ["openshell", "sandbox", "delete", name]


def test_policy_env_override(tmp_path, monkeypatch):
    other = tmp_path / "p.yaml"
    monkeypatch.setenv(so.POLICY_ENV, str(other))
    r = FakeRunner()
    with so.open_sandbox(tmp_path, runner=r):
        pass
    create = r.calls[0]
    assert create[create.index("--policy") + 1] == str(other)


def test_exec_passes_command_verbatim_and_captures_output(tmp_path):
    r = FakeRunner(exec=lambda argv: ProcResult(3, b"out\n", b"err\n"))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("printf 'a b' | ./x; echo \"$HOME\"", 5)
    assert res["exit_code"] == 3 and res["stdout"] == b"out\n" and res["stderr"] == b"err\n"
    assert res["duration_s"] >= 0
    assert r.calls[-2][-1] == "printf 'a b' | ./x; echo \"$HOME\""  # verbatim, one arg


def test_timeout_from_host_backstop(tmp_path):
    r = FakeRunner(exec=ProcResult(None, b"partial", b"", timed_out=True))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("sleep 99", 2)
    assert res["timed_out"] is True and res["exit_code"] is None
    assert res["stdout"] == b"partial"


def test_timeout_from_in_sandbox_kill(tmp_path, monkeypatch):
    """Live: the in-sandbox `timeout -s KILL` surfaces as CLI exit 137 after ~the limit."""
    r = FakeRunner(exec=ProcResult(137, b"", b'_: line 1: 58 Killed  timeout -s KILL "$1" ...\n'))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        clock = iter([100.0, 102.1])
        monkeypatch.setattr(so.time, "monotonic", lambda: next(clock))
        res = sb.exec("sleep 99", 2)
    assert res["timed_out"] is True and res["exit_code"] is None


def test_timeout_from_cli_backstop_exit_124(tmp_path, monkeypatch):
    """Live: the CLI's own --timeout exits 124 (silently) after ~its limit (t + grace)."""
    r = FakeRunner(exec=ProcResult(124, b"partial", b""))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        clock = iter([100.0, 100.0 + 2 + so.EXEC_GRACE_S])
        monkeypatch.setattr(so.time, "monotonic", lambda: next(clock))
        res = sb.exec("sleep 99", 2)
    assert res["timed_out"] is True and res["exit_code"] is None and res["stdout"] == b"partial"


def test_cli_timeout_uses_grace_beyond_inner_kill(tmp_path):
    r = FakeRunner()
    with so.open_sandbox(tmp_path, runner=r) as sb:
        sb.exec("x", 7)
    execc = r.calls[-2]
    assert execc[execc.index("--timeout") + 1] == str(7 + int(so.EXEC_GRACE_S))
    assert r.timeouts[-2] > 7 + so.EXEC_GRACE_S      # host-side kill sits beyond the CLI's


def test_exit_137_without_waiting_is_not_a_timeout(tmp_path):
    """An OOM kill (137) well before the limit is a normal failure, not a timeout."""
    r = FakeRunner(exec=ProcResult(137, b"", b"Killed"))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("hog", 60)
    assert res["timed_out"] is False and res["exit_code"] == 137


def test_exit_124_without_waiting_is_a_plain_exit_code(tmp_path):
    r = FakeRunner(exec=ProcResult(124, b"", b""))
    with so.open_sandbox(tmp_path, runner=r) as sb:
        res = sb.exec("exit 124", 60)
    assert res["timed_out"] is False and res["exit_code"] == 124


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


def test_container_exited_adds_image_hint_and_still_deletes(tmp_path):
    """Live: a bad image (no `ip`, no /work, wrong uid) exits 1 with ContainerExited and leaves
    a sandbox in phase Error; the delete in the finally clears it."""
    err = (b"Error:   x sandbox entered error phase while provisioning: ContainerExited: "
           b"Container exited")
    r = FakeRunner(create=ProcResult(1, b"progress...", err))
    with pytest.raises(SandboxError, match=r"ContainerExited.*openshell logs rv-.*iproute2"):
        with so.open_sandbox(tmp_path, runner=r):
            pytest.fail("body must not run")
    assert r.subs() == ["create", "delete"]


def test_upload_failure_raises_and_tears_down(tmp_path):
    r = FakeRunner(upload=ProcResult(2, b"", b"disk full"))
    with pytest.raises(SandboxError, match="upload failed"):
        with so.open_sandbox(tmp_path, runner=r):
            pytest.fail("body must not run")
    assert r.subs() == ["create", "upload", "delete"]


def test_hoist_failure_raises_and_tears_down(tmp_path):
    r = FakeRunner(hoist=ProcResult(3, b"", b"unexpected upload layout"))
    with pytest.raises(SandboxError, match="hoist.*unexpected upload layout"):
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
    # live text: "code: 'Some requested entity was not found', message: "sandbox not found""
    r = FakeRunner(delete=ProcResult(1, b"", b"Error:   x code: 'Some requested entity was not "
                                              b"found', message: \"sandbox not found\""))
    with so.open_sandbox(tmp_path, runner=r):
        pass


def test_cli_missing_is_sandbox_error(tmp_path):
    r = FakeRunner(create=SandboxError("openshell CLI not found: openshell"))
    with pytest.raises(SandboxError, match="not found"):
        with so.open_sandbox(tmp_path, runner=r):
            pass


def test_two_sandboxes_open_at_once_use_distinct_names(tmp_path):
    r = FakeRunner()
    with so.open_sandbox(tmp_path, runner=r) as a, so.open_sandbox(tmp_path, runner=r) as b:
        assert a.name != b.name
        a.exec("echo a", 5)
        b.exec("echo b", 5)
    execs = [c for c in r.calls if sub_of(c) == "exec"]
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


def test_policy_user_is_numeric():
    pol = yaml.safe_load(so.POLICY_PATH.read_text())
    assert str(pol["process"]["run_as_user"]).isdigit()
