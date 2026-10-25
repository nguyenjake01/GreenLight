"""OpenShell sandbox backend for the run check (design doc §8.3 `sandbox`, §10 H6).

``open_sandbox(repo_dir)`` creates a fresh OpenShell sandbox from ``RUNCHECK_IMAGE`` with the
no-network policy ``openshell/runcheck-policy.yaml``, copies ``repo_dir`` to ``/work``, yields a
``Sandbox`` whose ``exec`` runs ``bash -lc "<cmd>"`` in ``/work``, and always deletes the
sandbox afterwards.

Every OpenShell command line lives in the "Command lines" section below, so they are easy to
fix at bring-up. Each one is tagged DOCS (flag documented in the 0.0.116 docs / ``--help``)
or GUESS (behavior not confirmed without a live gateway). Everything talks to the CLI through
``subprocess``; a ``runner`` can be injected for tests.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from verifier.contracts.config import env
from verifier.contracts.errors import SandboxError
from verifier.contracts.types import ExecResult

POLICY_PATH = Path(__file__).resolve().parent / "openshell" / "runcheck-policy.yaml"

OUTPUT_CAP = 1_000_000                      # 1 MB per stream (§8.3)
TRUNCATION_MARKER = b"\n[truncated: output exceeded 1 MB]\n"
MEMORY = "4Gi"                              # §8.3 (proposed): 4 GB
CPUS = "2"                                  # matches the Docker backend's --cpus 2
WORKDIR = "/work"
EXEC_HOME = "/tmp"                          # HOME for bash -l; /home may be read-only
MAIN_CMD = ["sleep", "infinity"]            # the sandbox's canonical main process
CREATE_TIMEOUT_S = 300.0                    # image pull / sandbox provisioning
UPLOAD_TIMEOUT_S = 300.0
DELETE_TIMEOUT_S = 120.0
READY_TIMEOUT_S = 90.0
EXEC_GRACE_S = 5.0                          # CLI/host backstops sit this far beyond the in-sandbox kill


# ══ Command lines (the only place OpenShell flags appear) ════════════════════════════════
def openshell_bin() -> str:
    """OPENSHELL_BIN, else `openshell` on PATH, else ~/.local/bin/openshell."""
    explicit = os.environ.get("OPENSHELL_BIN")
    if explicit:
        return explicit
    found = shutil.which("openshell")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "openshell")


def cmd_create(name: str, image: str, policy: Path) -> list[str]:
    """DOCS (`sandbox create --help`): --name, --from <image ref>, --policy, --memory, --cpu,
    --detach, --no-tty, trailing `-- <command>`. GUESS: that `create` returns once the sandbox
    is Ready (we also poll with `cmd_exec` below). GUESS: a locally built image works with
    --from (the Docker driver's default pull policy is IfNotPresent)."""
    return [openshell_bin(), "sandbox", "create", "--name", name, "--from", image,
            "--policy", str(policy), "--memory", MEMORY, "--cpu", CPUS,
            "--detach", "--no-tty", "--", *MAIN_CMD]


def cmd_upload(name: str, local: Path, dest: str) -> list[str]:
    """DOCS (`sandbox upload <NAME> <LOCAL_PATH> [DEST]`, --no-git-ignore). GUESS: with an
    explicit DEST of /work, a local *directory* lands as /work's contents rather than
    /work/<basename> (docs only describe the basename rule for the default destination).
    --no-git-ignore so the repo is copied as-is."""
    return [openshell_bin(), "sandbox", "upload", name, str(local), dest, "--no-git-ignore"]


def cmd_exec(name: str, argv: list[str], timeout_s: int) -> list[str]:
    """DOCS (`sandbox exec`): -n/--name, --workdir, --timeout (seconds, 0 = none), --no-tty,
    --env KEY=VALUE, `-- <command...>`; the CLI exits with the remote command's exit code.
    GUESS: the exit code / message the CLI uses when its own --timeout fires (see
    `looks_like_timeout`). The real kill is `timeout -s KILL` inside the sandbox."""
    return [openshell_bin(), "sandbox", "exec", "-n", name, "--workdir", WORKDIR,
            "--no-tty", "--timeout", str(timeout_s), "--env", f"HOME={EXEC_HOME}",
            "--", *argv]


def cmd_ready_probe(name: str) -> list[str]:
    """GUESS: a trivial exec succeeds only once the sandbox is Ready."""
    return [openshell_bin(), "sandbox", "exec", "-n", name, "--no-tty", "--timeout", "30",
            "--", "true"]


def cmd_delete(name: str) -> list[str]:
    """DOCS (`sandbox delete [NAME]...`)."""
    return [openshell_bin(), "sandbox", "delete", name]


def inner_argv(cmd: str, timeout_s: int) -> list[str]:
    """What runs inside the sandbox: `bash -lc "<cmd>"` under a hard kill (kills the group)."""
    return ["timeout", "-s", "KILL", str(timeout_s), "bash", "-lc", cmd]


def looks_like_timeout(returncode: int | None, stderr: bytes, duration_s: float,
                       timeout_s: int) -> bool:
    """True when the sandboxed command was killed by its time limit.

    `timeout -s KILL` exits 137 (128+9) when it kills the command. GUESS: the CLI's own
    --timeout reports a message containing "timed out" / "deadline".
    """
    if returncode == 137 and duration_s >= timeout_s - 1.0:
        return True
    if returncode not in (0, None) and duration_s >= timeout_s - 1.0:
        low = stderr.lower()
        return b"timed out" in low or b"timeout" in low or b"deadline" in low
    return False


# ══ Process runner ═══════════════════════════════════════════════════════════════════════
@dataclass
class ProcResult:
    returncode: int | None          # None when the host-side timeout killed it
    stdout: bytes                   # first OUTPUT_CAP bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False


Runner = Callable[[list[str], float], ProcResult]


class _Capped:
    """Reads a pipe to EOF, keeping only the first `cap` bytes (memory stays bounded)."""

    def __init__(self, fd: int, cap: int):
        self.fd, self.cap = fd, cap
        self.data = bytearray()
        self.truncated = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            room = self.cap - len(self.data)
            if room > 0:
                self.data += chunk[:room]
            if len(chunk) > max(room, 0):
                self.truncated = True


def subprocess_runner(argv: list[str], timeout_s: float) -> ProcResult:
    """Run `argv`, kill its process group after `timeout_s`, cap each stream at OUTPUT_CAP."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except FileNotFoundError as exc:
        raise SandboxError(f"openshell CLI not found: {argv[0]}") from exc
    except OSError as exc:
        raise SandboxError(f"could not start openshell CLI: {exc}") from exc
    out = _Capped(proc.stdout.fileno(), OUTPUT_CAP)
    err = _Capped(proc.stderr.fileno(), OUTPUT_CAP)
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
        proc.wait()
    out.thread.join(5.0)
    err.thread.join(5.0)
    proc.stdout.close()
    proc.stderr.close()
    return ProcResult(None if timed_out else proc.returncode, bytes(out.data), bytes(err.data),
                      out.truncated, err.truncated, timed_out)


def _cap(data: bytes, already_truncated: bool) -> bytes:
    """At most OUTPUT_CAP bytes in total; a truncation marker replaces the tail."""
    if already_truncated or len(data) > OUTPUT_CAP:
        return data[:OUTPUT_CAP - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return data


def _tail(data: bytes, n: int = 400) -> str:
    return data[-n:].decode("utf-8", errors="replace").strip()


# ══ Sandbox ══════════════════════════════════════════════════════════════════════════════
class OpenShellSandbox:
    backend = "openshell"

    def __init__(self, name: str, runner: Runner):
        self.name = name
        self._run = runner

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        t = max(1, math.ceil(timeout_s))
        argv = cmd_exec(self.name, inner_argv(cmd, t), t + int(EXEC_GRACE_S))
        started = time.monotonic()
        res = self._run(argv, t + EXEC_GRACE_S + 5.0)
        duration = time.monotonic() - started
        timed_out = res.timed_out or looks_like_timeout(res.returncode, res.stderr, duration, t)
        return {
            "exit_code": None if timed_out else res.returncode,
            "stdout": _cap(res.stdout, res.stdout_truncated),
            "stderr": _cap(res.stderr, res.stderr_truncated),
            "duration_s": round(duration, 3),
            "timed_out": timed_out,
        }


_sleep = time.sleep


def _wait_ready(name: str, runner: Runner) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last = ""
    while True:
        res = runner(cmd_ready_probe(name), 45.0)
        if res.returncode == 0 and not res.timed_out:
            return
        last = _tail(res.stderr) or _tail(res.stdout)
        if time.monotonic() >= deadline:
            raise SandboxError(f"sandbox {name} not ready after {READY_TIMEOUT_S:.0f}s: {last}")
        _sleep(1.0)


def _create(name: str, repo_dir: Path, runner: Runner) -> None:
    res = runner(cmd_create(name, env("RUNCHECK_IMAGE"), POLICY_PATH), CREATE_TIMEOUT_S)
    if res.timed_out or res.returncode != 0:
        why = "timed out" if res.timed_out else f"exit {res.returncode}"
        raise SandboxError(f"openshell sandbox create failed ({why}): "
                           f"{_tail(res.stderr) or _tail(res.stdout)}")
    _wait_ready(name, runner)
    res = runner(cmd_upload(name, repo_dir, WORKDIR), UPLOAD_TIMEOUT_S)
    if res.timed_out or res.returncode != 0:
        why = "timed out" if res.timed_out else f"exit {res.returncode}"
        raise SandboxError(f"openshell sandbox upload failed ({why}): "
                           f"{_tail(res.stderr) or _tail(res.stdout)}")


def _delete(name: str, runner: Runner) -> None:
    res = runner(cmd_delete(name), DELETE_TIMEOUT_S)
    if res.timed_out:
        raise SandboxError(f"openshell sandbox delete {name} timed out")
    if res.returncode != 0:
        text = (res.stderr + res.stdout).lower()
        if b"not found" in text or b"notfound" in text:
            return
        raise SandboxError(f"openshell sandbox delete {name} failed (exit {res.returncode}): "
                           f"{_tail(res.stderr) or _tail(res.stdout)}")


@contextmanager
def open_sandbox(repo_dir: Path, *, runner: Runner | None = None) -> Iterator[OpenShellSandbox]:
    """A fresh sandbox per call; `repo_dir` is copied in as /work. Always torn down.

    Raises SandboxError if the sandbox can't be created or the repo can't be copied, or if
    teardown fails after an otherwise clean run (teardown errors never mask a body error).
    """
    run = runner or subprocess_runner
    name = f"rv-{uuid.uuid4().hex[:12]}"
    body_failed = False
    try:
        _create(name, Path(repo_dir), run)
        yield OpenShellSandbox(name, run)
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            _delete(name, run)
        except SandboxError:
            if not body_failed:
                raise
