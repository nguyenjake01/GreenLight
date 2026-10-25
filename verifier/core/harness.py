"""J10: run harness + Docker sandbox backend (design doc §7.5, §8.3 `harness`/`sandbox`, §11 J10).

``run_check`` checks a repo out, opens a sandbox (via the registry), runs a deterministic
baseline, optionally lets the agent probe the repo, asks the judge for an outcome and writes
``run_traces/{claim_id}.json`` plus ``raw/<sha>.txt`` files through ``store``.

Rules: repo code only ever runs inside the sandbox (the harness reads files on the host to pick
the baseline step, nothing more); the judge never sees the README; the sandbox is always closed
and the temp folder always removed.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from verifier.contracts import registry
from verifier.contracts.config import env
from verifier.contracts.errors import LLMError, SandboxError
from verifier.contracts.snapshot import Snapshot
from verifier.contracts.types import (
    AgentRecord, BaselineRecord, Claim, CommandRecord, ExecResult, FetchMode, JudgeResult,
    RunTrace,
)
from verifier.core import store

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

README_NAMES = ("README.md", "README.rst", "README.txt", "README")
README_MAX_CHARS = 8000
MAX_REPO_BYTES = 50 * 1024 * 1024
BASELINE_TIMEOUT_S = 120.0
AGENT_MAX_COMMANDS = 15
AGENT_TIMEOUT_S = 300
STREAM_CAP = 1024 * 1024
EXCERPT_LINES = 20
EXCERPT_CHARS = 1500
MAX_SOURCE_FILES = 500
SKIP_DIRS = {"node_modules", "vendor", "build", "dist"}
TRUNCATED_MARKER = b"\n[truncated]\n"

# ── failure classification ────────────────────────────────────────────────────

_NETWORK_PATTERNS = (
    "Could not resolve host", "Temporary failure in name resolution", "Name or service not known",
    "getaddrinfo", "ENOTFOUND", "EAI_AGAIN", "Network is unreachable", "NewConnectionError",
    "Failed to establish a new connection", "Could not fetch URL", "npm ERR! network",
)
_DEPS_PATTERNS = (
    "ModuleNotFoundError", "No module named", "Cannot find module", "ERR_MODULE_NOT_FOUND",
    "command not found", ": not found", "cannot find -l", "externally-managed-environment",
    # A data file the repo doesn't ship (e.g. Polytrack-ML's unpublished capture): could not
    # run here, not a build failure by the candidate. Lead's addition to the J10 table.
    "ENOENT: no such file or directory",
)


def classify_failure(output: str) -> str:
    """`needs_network`, `deps_missing` or `error` by substring match; first group wins."""
    if any(p in output for p in _NETWORK_PATTERNS):
        return "needs_network"
    if any(p in output for p in _DEPS_PATTERNS):
        return "deps_missing"
    for line in output.splitlines():
        if "fatal error:" in line and "No such file or directory" in line:
            return "deps_missing"
    return "error"


# ── output helpers ────────────────────────────────────────────────────────────

def excerpt(output: str) -> str:
    """Last 20 lines, at most 1,500 characters (the tail is kept)."""
    lines = output.splitlines()[-EXCERPT_LINES:]
    text = "\n".join(lines)
    return text[-EXCERPT_CHARS:]


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _combined_output(res: ExecResult) -> str:
    """stdout, then stderr."""
    return _decode(res["stdout"]) + _decode(res["stderr"])


def _write_raw(snapshot_path: Path, output: str) -> str:
    """Content-addressed raw output; returns the path relative to the snapshot dir."""
    data = output.encode("utf-8")
    sha = store.sha256_bytes(data)
    rel = f"raw/{sha}.txt"
    store.write_bytes(Path(snapshot_path) / rel, data)
    return rel


# ── baseline ──────────────────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _source_files(repo_dir: Path, exts: tuple[str, ...]) -> list[str]:
    """Sorted relative paths, skipping hidden folders and node_modules/vendor/build/dist."""
    found: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
        for name in files:
            if name.lower().endswith(exts) and not name.startswith("."):
                found.append((Path(root) / name).relative_to(repo_dir).as_posix())
    found.sort()
    return found[:MAX_SOURCE_FILES]


def _has_pytest(repo_dir: Path) -> bool:
    if (repo_dir / "pytest.ini").is_file() or (repo_dir / "conftest.py").is_file():
        return True
    if "[tool.pytest" in _read_text(repo_dir / "pyproject.toml"):
        return True
    if "[tool:pytest]" in _read_text(repo_dir / "setup.cfg"):
        return True
    if "[pytest]" in _read_text(repo_dir / "tox.ini"):
        return True
    for folder in ("tests", "test"):
        d = repo_dir / folder
        if d.is_dir() and any(p.is_file() for p in d.rglob("test_*.py")):
            return True
    return False


def _has_npm_test(repo_dir: Path) -> bool:
    import json
    pkg = repo_dir / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(_read_text(pkg))
    except ValueError:
        return False
    script = (data.get("scripts") or {}).get("test") if isinstance(data, dict) else None
    return isinstance(script, str) and bool(script.strip()) and "no test specified" not in script


def _count_mains(repo_dir: Path, files: list[str]) -> int:
    pat = re.compile(r"\bmain\s*\(")
    return sum(1 for f in files if pat.search(_read_text(repo_dir / f)))


def _compile_command(compiler: str, flags: str, files: list[str], repo_dir: Path) -> str:
    paths = " ".join(shlex.quote(f) for f in files)
    if _count_mains(repo_dir, files) > 1:  # several programs: can't link them together
        return f"{compiler} {flags} -fsyntax-only {paths}"
    return f"{compiler} {flags} -o /tmp/a.out {paths}"


def pick_baseline(repo_dir: Path) -> tuple[str, str | None]:
    """(step, command) chosen by reading files on the host. First match wins."""
    repo_dir = Path(repo_dir)
    if _has_pytest(repo_dir):
        return "tests", "python3 -m pytest -q -x -p no:cacheprovider"
    if _has_npm_test(repo_dir):
        return "tests", "npm test --silent"
    if (repo_dir / "Makefile").is_file() or (repo_dir / "makefile").is_file():
        return "build", "make -j2"
    if (repo_dir / "CMakeLists.txt").is_file():
        return "build", "cmake -S . -B /tmp/build && cmake --build /tmp/build -j2"
    cpp = _source_files(repo_dir, (".cpp", ".cc", ".cxx"))
    if cpp:
        return "build", _compile_command("g++", "-std=c++17 -O2", cpp, repo_dir)
    c = _source_files(repo_dir, (".c",))
    if c:
        return "build", _compile_command("gcc", "-O2", c, repo_dir)
    py = _source_files(repo_dir, (".py",))
    if py:
        return "syntax", "python3 -m py_compile " + " ".join(shlex.quote(f) for f in py)
    js = _source_files(repo_dir, (".js", ".mjs", ".cjs"))
    if js:
        loop = " ".join(shlex.quote(f) for f in js)
        return "syntax", f'for f in {loop}; do node --check "$f" || exit 1; done'
    return "skip", None


def baseline(sb: Any, repo_dir: Path) -> tuple[BaselineRecord, str]:
    """Run the baseline step inside `sb`. Returns (record, full output text).

    `output_path` is left None; `run_check` writes the raw file and fills it in.
    """
    step, command = pick_baseline(repo_dir)
    if command is None:
        return ({"step": "skip", "command": None, "exit_code": None, "duration_s": 0.0,
                 "timed_out": False, "failure_kind": None, "output_excerpt": "",
                 "output_path": None}, "")
    res = sb.exec(command, BASELINE_TIMEOUT_S)
    output = _combined_output(res)
    failed = res["timed_out"] or res["exit_code"] != 0
    record: BaselineRecord = {
        "step": step,  # type: ignore[typeddict-item]
        "command": command,
        "exit_code": res["exit_code"],
        "duration_s": round(float(res["duration_s"]), 3),
        "timed_out": bool(res["timed_out"]),
        "failure_kind": classify_failure(output) if failed else None,  # type: ignore[typeddict-item]
        "output_excerpt": excerpt(output),
        "output_path": None,
    }
    return record, output


# ── RecordingSandbox ──────────────────────────────────────────────────────────

class RecordingSandbox:
    """Wraps the real sandbox; the agent's only way to run commands. Records every command."""

    def __init__(self, inner: Any, snapshot_path: Path, *,
                 max_commands: int = AGENT_MAX_COMMANDS, budget_s: float = AGENT_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic):
        self._inner = inner
        self._snapshot_path = Path(snapshot_path)
        self.max_commands = max_commands
        self.budget_s = budget_s
        self._clock = clock
        self._started = clock()
        self._records: list[CommandRecord] = []
        self.backend = getattr(inner, "backend", "unknown")

    @property
    def commands(self) -> list[CommandRecord]:
        return list(self._records)

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        if len(self._records) >= self.max_commands or self._clock() - self._started >= self.budget_s:
            return {"exit_code": None, "stdout": b"", "stderr": b"budget used",
                    "duration_s": 0.0, "timed_out": False}
        res = self._inner.exec(cmd, timeout_s)
        output = _combined_output(res)
        self._records.append({
            "cmd": cmd,
            "exit_code": res["exit_code"],
            "duration_s": round(float(res["duration_s"]), 3),
            "timed_out": bool(res["timed_out"]),
            "output_excerpt": excerpt(output),
            "output_path": _write_raw(self._snapshot_path, output),
        })
        return res


# ── judge transcript ──────────────────────────────────────────────────────────

_README_CAT = re.compile(r"\b(cat|head|tail|less|more|sed)\b[^|;&\n]*\.(md|rst)\b", re.IGNORECASE)


def is_readme_read(cmd: str) -> bool:
    """A command mentioning 'readme', or printing a .md/.rst file with cat/head/tail/less/more/sed."""
    return "readme" in cmd.lower() or bool(_README_CAT.search(cmd))


def build_transcript(base: BaselineRecord, commands: list[CommandRecord]) -> list[CommandRecord]:
    """Baseline first (harness output, no README), then the agent's commands minus README reads."""
    head: CommandRecord = {
        "cmd": base["command"] or "(no baseline)",
        "exit_code": base["exit_code"],
        "duration_s": base["duration_s"],
        "timed_out": base["timed_out"],
        "output_excerpt": base["output_excerpt"],
        "output_path": base["output_path"] or "",
    }
    return [head] + [c for c in commands if not is_readme_read(c["cmd"])]


# ── repo checkout ─────────────────────────────────────────────────────────────

def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _archive_stem(repo_full_name: str) -> str:
    owner, _, name = repo_full_name.partition("/")
    return f"{owner}__{name}".lower()


def _checkout(repo_full_name: str, dest: Path, mode: FetchMode) -> str:
    """Put the repo's working tree in `dest`; return its commit sha."""
    if mode == "replay":
        stem = _archive_stem(repo_full_name)
        archive = FIXTURES_DIR / "repos" / f"{stem}.tar.gz"
        sidecar = FIXTURES_DIR / "repos" / f"{stem}.json"
        if not archive.is_file() or not sidecar.is_file():
            raise SandboxError(f"repo not recorded: {repo_full_name}")
        try:
            commit_sha = store.read_json(sidecar)["commit_sha"]
            with tarfile.open(archive, "r:gz") as tar:
                members = tar.getmembers()
                if sum(m.size for m in members if m.isfile()) > MAX_REPO_BYTES:
                    raise SandboxError("repo too large")
                tar.extractall(dest, members=members, filter="data")
        except SandboxError:
            raise
        except (tarfile.TarError, OSError, KeyError, ValueError) as e:
            raise SandboxError(f"could not extract {repo_full_name}: {e}") from e
        return str(commit_sha)

    url = f"https://github.com/{repo_full_name}.git"
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
                       check=True, capture_output=True, timeout=300,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        rev = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as e:
        detail = getattr(e, "stderr", b"") or b""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise SandboxError(f"git clone failed for {repo_full_name}: {detail.strip() or e}") from e
    if _tree_size(dest) > MAX_REPO_BYTES:
        raise SandboxError("repo too large")
    return rev.stdout.strip()


def read_readme(repo_dir: Path) -> str | None:
    """The root README (README.md, README.rst, README.txt or README), cut to 8,000 characters."""
    for name in README_NAMES:
        path = Path(repo_dir) / name
        if path.is_file():
            return _read_text(path)[:README_MAX_CHARS]
    return None


# ── run_check ─────────────────────────────────────────────────────────────────

def run_check(snapshot: Snapshot, claim: Claim, repo_full_name: str, *,
              mode: FetchMode) -> RunTrace:
    """Check out the repo, open a sandbox, baseline, agent, judge; write
    run_traces/{claim_id}.json; return the trace. Raises SandboxError."""
    sandbox_mod = registry.get("sandbox")
    tmp = Path(tempfile.mkdtemp(prefix="rv-repo-"))
    try:
        repo_dir = tmp / "repo"
        commit_sha = _checkout(repo_full_name, repo_dir, mode)
        readme = read_readme(repo_dir)

        with sandbox_mod.open_sandbox(repo_dir) as sb:
            base, base_out = baseline(sb, repo_dir)
            if base_out:
                base["output_path"] = _write_raw(snapshot.path, base_out)

            agent_rec: AgentRecord | None = None
            judge_res: JudgeResult | None = None
            if env("RUN_CHECK") != "baseline_only":
                agent_rec = _run_agent(sb, snapshot, claim, readme)
                transcript = build_transcript(base, agent_rec["commands"])
                try:
                    judge_res = registry.get("judge").judge(claim, transcript)
                except LLMError:
                    judge_res = None
            backend = getattr(sb, "backend", "unknown")

        trace: RunTrace = {
            "schema_version": "1",
            "claim_id": claim["claim_id"],
            "repo": repo_full_name,
            "commit_sha": commit_sha,
            "sandbox_backend": backend,
            "baseline": base,
            "agent": agent_rec,
            "judge": judge_res,
        }
        traces = snapshot.traces_dir()
        traces.mkdir(parents=True, exist_ok=True)
        store.write_json(traces / f"{claim['claim_id']}.json", trace)
        return trace
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_agent(sb: Any, snapshot: Snapshot, claim: Claim, readme: str | None) -> AgentRecord:
    rec = RecordingSandbox(sb, snapshot.path, max_commands=AGENT_MAX_COMMANDS,
                           budget_s=AGENT_TIMEOUT_S)
    try:
        result = registry.get("agent").agent_probe(
            rec, claim, readme, max_commands=AGENT_MAX_COMMANDS, timeout_s=float(AGENT_TIMEOUT_S))
        model, version, stopped = result["model"], result["prompt_version"], result["stopped"]
    except Exception:  # agent_probe promises no errors escape; keep the baseline if one does
        model, version, stopped = "unknown", "run-v1", "error"
    return {
        "model": model,
        "prompt_version": version,
        "limits": {"max_commands": AGENT_MAX_COMMANDS, "timeout_s": AGENT_TIMEOUT_S},
        "stopped": stopped,
        "timed_out": stopped == "timeout",
        "commands": rec.commands,
    }


# ── Docker backend ────────────────────────────────────────────────────────────

class _CappedReader(threading.Thread):
    """Reads a pipe to EOF, keeps the first 1 MB, discards the rest, marks truncation."""

    def __init__(self, pipe: Any, cap: int = STREAM_CAP):
        super().__init__(daemon=True)
        self._pipe, self._cap = pipe, cap
        self.data = bytearray()
        self.truncated = False

    def run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(65536)
                if not chunk:
                    break
                room = self._cap - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (OSError, ValueError):
            pass

    def result(self) -> bytes:
        return bytes(self.data) + (TRUNCATED_MARKER if self.truncated else b"")


class DockerSandbox:
    backend = "docker"

    def __init__(self, name: str):
        self.name = name

    def exec(self, cmd: str, timeout_s: float) -> ExecResult:
        argv = ["docker", "exec", "-w", "/work", self.name,
                "timeout", "-s", "KILL", str(int(max(1, timeout_s))), "bash", "-lc", cmd]
        start = time.monotonic()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as e:
            raise SandboxError(f"docker exec failed: {e}") from e
        out_r, err_r = _CappedReader(proc.stdout), _CappedReader(proc.stderr)
        out_r.start()
        err_r.start()
        host_timed_out = False
        try:
            proc.wait(timeout=timeout_s + 10)  # host-side backstop
        except subprocess.TimeoutExpired:
            host_timed_out = True
            proc.kill()
            proc.wait()
            subprocess.run(["docker", "exec", self.name, "pkill", "-KILL", "-u", "1000", "-f", "bash -lc"],
                           capture_output=True, timeout=15)
        out_r.join(5)
        err_r.join(5)
        duration = time.monotonic() - start
        rc = proc.returncode
        # `timeout -s KILL` ends with 137 (docker exec relays it), 124, or -9 when the host sees the kill
        killed = host_timed_out or (rc in (137, 124, -9) and duration >= timeout_s * 0.9)
        return {"exit_code": None if killed else rc, "stdout": out_r.result(),
                "stderr": err_r.result(), "duration_s": round(duration, 3), "timed_out": killed}


def _make_writable(root: Path) -> None:
    """Make the copy writable by uid 1000 (and anyone), without following symlinks."""
    for dirpath, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                os.chmod(p, os.stat(p).st_mode | 0o666 | (0o111 if os.path.isdir(p) else 0))
    os.chmod(root, 0o777)


def _rmtree_force(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(os.path.dirname(p), 0o777)
            os.chmod(p, 0o777)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


@contextmanager
def open_sandbox(repo_dir: Path) -> Iterator[DockerSandbox]:
    """Docker backend: a fresh container per run; repo_dir copied in as /work. No network,
    non-root. Always torn down. Raises SandboxError."""
    if shutil.which("docker") is None:
        raise SandboxError("docker not found on PATH")
    image = env("RUNCHECK_IMAGE")
    name = f"rv-{uuid.uuid4().hex[:12]}"
    tmp = Path(tempfile.mkdtemp(prefix="rv-work-"))
    started = False
    try:
        work = tmp / "work"
        shutil.copytree(repo_dir, work, symlinks=True)
        _make_writable(work)
        argv = ["docker", "run", "-d", "--rm", "--name", name, "--pull", "never",
                "--network", "none", "--user", "1000:1000", "--read-only",
                "--tmpfs", "/tmp:rw,exec,nosuid,size=512m", "-e", "HOME=/tmp",
                "--memory", "4g", "--pids-limit", "256", "--cpus", "2",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-v", f"{work}:/work", "-w", "/work", image, "sleep", "infinity"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise SandboxError(f"docker run failed: {e}") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            if "No such image" in err or "Unable to find image" in err or "pull access denied" in err:
                raise SandboxError(f"runcheck image missing: {image} ({err})")
            raise SandboxError(f"docker run failed: {err}")
        started = True
        yield DockerSandbox(name)
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        _rmtree_force(tmp)
