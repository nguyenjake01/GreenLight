"""OpenShell sandbox backend for the run check (design doc §8.3 `sandbox`, §10 H6).

``open_sandbox(repo_dir)`` creates a fresh OpenShell sandbox from ``RUNCHECK_IMAGE`` with the
no-network policy ``openshell/runcheck-policy.yaml``, copies ``repo_dir`` to ``/work``, yields a
``Sandbox`` whose ``exec`` runs ``bash -lc "<cmd>"`` in ``/work``, and always deletes the
sandbox afterwards.

Every OpenShell command line lives in the "Command lines" section below, so they are easy to
fix. Each one is tagged LIVE (confirmed against a real 0.0.116 gateway with the Docker compute
driver on the GB10, 2026-10-25) or UNCONFIRMED (with the reason). Everything talks to the CLI
through ``subprocess``; a ``runner`` can be injected for tests.

Requirements the live check turned up (see also the notes on each command):

* The CLI talks to the *active* gateway (``openshell gateway select``, or ``OPENSHELL_GATEWAY`` /
  ``--gateway-endpoint`` in the environment); this module passes no gateway flag.
* The supervisor builds a network namespace for the no-network policy, so ``RUNCHECK_IMAGE``
  needs ``ip`` (apt package ``iproute2``) at /usr/sbin/ip, and /work must exist in the image.
* ``process.run_as_user`` in the policy must be the image's user (uid 1000 in
  ``docker/runcheck.Dockerfile``); set ``OPENSHELL_POLICY`` to use another policy file.
* Add ``/home`` to the policy's ``read_only`` list: the exec endpoint starts a login shell as the
  image user and without it every command's stderr begins with
  ``/bin/bash: /home/<user>/.profile: Permission denied``.
* The 256-PID limit is a gateway setting (Docker driver default is 2048). LIVE: start the gateway
  with ``OPENSHELL_GATEWAY_CONFIG=<toml>`` containing ``[openshell.drivers.docker]`` /
  ``sandbox_pids_limit = 256`` (HostConfig.PidsLimit 256; a fork bomb stopped at ~241).
* ``/dev/zero`` is unreadable under the policy's Landlock rules (only /dev/null and
  /dev/urandom are listed); add it to ``read_only`` if run-check commands need it.
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
POLICY_ENV = "OPENSHELL_POLICY"             # optional override (e.g. image runs as another uid)

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
EXEC_GRACE_S = 5.0                          # CLI/host backstops sit this far beyond the in-sandbox kill
STAGE_DIR = "/work/.rv-upload"              # `sandbox upload` nests the dir here; we hoist it


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


def policy_path() -> Path:
    return Path(os.environ.get(POLICY_ENV) or POLICY_PATH)


def cmd_create(name: str, image: str, policy: Path) -> list[str]:
    """LIVE. `sandbox create --name N --from IMAGE --policy P --memory 4Gi --cpu 2 --detach
    --no-tty -- sleep infinity`:
    * blocks until the sandbox is Ready (exec works the moment it returns, ~0.2 s with a local
      image) and exits 0; if the container dies while provisioning it exits 1 with the reason
      (e.g. "ContainerExited") and *leaves a sandbox in phase Error* that `delete` must remove;
    * a local image (`functional-run-sandbox:arm64`, no registry) works with --from: the
      Docker driver reports "Image already present" and never pulls;
    * --memory/--cpu are applied (HostConfig.Memory = 4294967296, NanoCpus = 2e9).
    Output (progress lines) goes to stdout and is ignored."""
    return [openshell_bin(), "sandbox", "create", "--name", name, "--from", image,
            "--policy", str(policy), "--memory", MEMORY, "--cpu", CPUS,
            "--detach", "--no-tty", "--", *MAIN_CMD]


def cmd_upload(name: str, local: Path, dest: str) -> list[str]:
    """LIVE. `sandbox upload NAME LOCAL DEST --no-git-ignore`. A local *directory* always lands
    at DEST/<basename of LOCAL> (an existing DEST, `/work`, is not replaced and a trailing
    `/` or `/.` on LOCAL changes nothing; a missing DEST is created). So `_upload` uploads to
    STAGE_DIR and hoists the contents. File modes, symlinks, dotfiles and empty files are
    preserved. --no-git-ignore: .gitignore filtering applies only inside git repos, so the
    flag keeps the copy verbatim there. Prints "Uploading ..." and a check mark to
    stdout/stderr; exit 1 on a missing local path or sandbox ("sandbox not found")."""
    return [openshell_bin(), "sandbox", "upload", name, str(local), dest, "--no-git-ignore"]


def cmd_exec(name: str, argv: list[str], timeout_s: int) -> list[str]:
    """LIVE. `sandbox exec -n N --workdir /work --no-tty --timeout S --env HOME=/tmp -- argv...`:
    * the CLI exits with the remote command's exit code, stdout and stderr stay separate;
    * HOME=/tmp reaches the command (so `bash -lc` reads /tmp/.profile, not /home/...);
    * the CLI's own --timeout S kills the wait and exits 124 with nothing on stderr (a plain
      `sleep 10` with --timeout 2 returned 124 after 2.0 s);
    * if the *top-level* remote process dies of a signal the CLI exits 1, not 137 (so
      `inner_argv` runs the killer inside a bash that survives and reports 137);
    * stdin is /dev/null in `subprocess_runner`, so readers get EOF."""
    return [openshell_bin(), "sandbox", "exec", "-n", name, "--workdir", WORKDIR,
            "--no-tty", "--timeout", str(timeout_s), "--env", f"HOME={EXEC_HOME}",
            "--", *argv]


def cmd_delete(name: str) -> list[str]:
    """LIVE. `sandbox delete NAME`: exit 0 and "Deleted sandbox NAME" (also removes a sandbox
    stuck in phase Error, and the container, in ~0.2 s); an unknown name exits 1 with
    "sandbox not found"."""
    return [openshell_bin(), "sandbox", "delete", name]


# Hoists what `sandbox upload` nested under STAGE_DIR/<basename> into /work.
_HOIST = ('set -e; shopt -s dotglob nullglob; cd ' + STAGE_DIR + '; set -- *; '
          '[ "$#" -eq 1 ] || { echo "unexpected upload layout" >&2; exit 3; }; '
          'for f in "$1"/*; do mv "$f" /work/; done; cd /work; rm -rf ' + STAGE_DIR)


def inner_argv(cmd: str, timeout_s: int) -> list[str]:
    """What runs inside the sandbox: `bash -lc "<cmd>"` under `timeout -s KILL`, run from an
    outer bash that outlives the kill. LIVE: `timeout -s KILL` re-raises the signal on itself,
    so run as the top-level process it makes the CLI exit 1; under the outer bash a timeout
    gives exit 137, and the whole process group (background children too) is gone. The outer
    bash adds a `_: line 1: N Killed  timeout ...` line to stderr when that happens."""
    return ["bash", "-c", 'timeout -s KILL "$1" bash -lc "$2"; exit $?', "_",
            str(timeout_s), cmd]


def looks_like_timeout(returncode: int | None, duration_s: float, timeout_s: int,
                       cli_timeout_s: int) -> bool:
    """True when the sandboxed command was killed by its time limit.

    LIVE: the in-sandbox `timeout -s KILL` gives 137 (128+9) after ~timeout_s; the CLI's own
    --timeout (cli_timeout_s, the backstop) gives 124 after ~cli_timeout_s. A 137 long before
    the limit is an OOM kill or a self-kill, i.e. an ordinary failure.
    """
    if returncode == 137 and duration_s >= timeout_s - 1.0:
        return True
    return returncode == 124 and duration_s >= cli_timeout_s - 1.0


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
        cli_timeout = t + int(EXEC_GRACE_S)
        argv = cmd_exec(self.name, inner_argv(cmd, t), cli_timeout)
        started = time.monotonic()
        res = self._run(argv, cli_timeout + 5.0)
        duration = time.monotonic() - started
        timed_out = res.timed_out or looks_like_timeout(res.returncode, duration, t, cli_timeout)
        return {
            "exit_code": None if timed_out else res.returncode,
            "stdout": _cap(res.stdout, res.stdout_truncated),
            "stderr": _cap(res.stderr, res.stderr_truncated),
            "duration_s": round(duration, 3),
            "timed_out": timed_out,
        }


def _fail(what: str, res: ProcResult) -> SandboxError:
    why = "timed out" if res.timed_out else f"exit {res.returncode}"
    return SandboxError(f"openshell sandbox {what} failed ({why}): "
                        f"{_tail(res.stderr) or _tail(res.stdout)}")


def _upload(name: str, repo_dir: Path, runner: Runner) -> None:
    """Copy repo_dir's contents to /work (the CLI would put it at /work/<basename>)."""
    res = runner(cmd_upload(name, repo_dir, STAGE_DIR), UPLOAD_TIMEOUT_S)
    if res.timed_out or res.returncode != 0:
        raise _fail("upload", res)
    res = runner(cmd_exec(name, ["bash", "-c", _HOIST], int(UPLOAD_TIMEOUT_S)), UPLOAD_TIMEOUT_S)
    if res.timed_out or res.returncode != 0:
        raise _fail("upload (hoist)", res)


def _create(name: str, repo_dir: Path, runner: Runner) -> None:
    res = runner(cmd_create(name, env("RUNCHECK_IMAGE"), policy_path()), CREATE_TIMEOUT_S)
    if res.timed_out or res.returncode != 0:
        err = _fail("create", res)
        if b"ContainerExited" in res.stderr + res.stdout:
            # LIVE: the supervisor's reason is only in `openshell logs`, not in the CLI error.
            err = SandboxError(f"{err} [hint: `openshell logs {name}`; RUNCHECK_IMAGE needs "
                               "iproute2 (ip) and a /work directory, and the policy's "
                               "run_as_user must match the image user]")
        raise err
    _upload(name, repo_dir, runner)


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
