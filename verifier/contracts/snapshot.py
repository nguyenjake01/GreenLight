"""Snapshot reader + cache_key, shared by fetch, rubric and harness (design doc §8.2)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

from verifier.contracts.types import EvidenceDoc


def cache_key(method: str, url: str) -> str:
    """'GET https://api.github.com/repos/a/b?per_page=100': method uppercased,
    scheme and host lowercased, query parameters sorted.

    Query values are re-encoded the same way every time (parse, sort, re-encode), so
    ``q=a+b`` and ``q=a%20b`` share a key. The fragment is dropped.
    """
    parts = urlsplit(url)
    pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
    query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in pairs)
    netloc = parts.netloc
    if "@" in netloc:  # lowercase only the host part
        userinfo, _, hostport = netloc.rpartition("@")
        netloc = f"{userinfo}@{hostport.lower()}"
    else:
        netloc = netloc.lower()
    out = f"{parts.scheme.lower()}://{netloc}{parts.path}"
    if query:
        out += "?" + query
    return f"{method.upper()} {out}"


class Snapshot:
    snapshot_id: str
    path: Path                       # runs/{app}/snapshots/{snapshot_id}
    evidence: EvidenceDoc

    def __init__(self, path: Path, evidence: EvidenceDoc):
        self.path = Path(path)
        self.evidence = evidence
        self.snapshot_id = evidence["snapshot_id"]

    @classmethod
    def load(cls, path: Path) -> Snapshot:
        path = Path(path)
        evidence = json.loads((path / "evidence.json").read_text(encoding="utf-8"))
        return cls(path, evidence)

    def get(self, method: str, url: str) -> tuple[int, Any] | None:
        """(status, parsed JSON body) for a recorded key; None if not recorded or in misses."""
        key = cache_key(method, url)
        if key in self.evidence.get("misses", []):
            return None
        for rec in self.evidence["responses"]:
            if rec["key"] == key:
                body = self.get_raw(rec["body_path"])
                return rec["status"], json.loads(body.decode("utf-8"))
        return None

    def get_raw(self, rel_path: str) -> bytes:
        return (self.path / rel_path).read_bytes()

    def traces_dir(self) -> Path:
        return self.path / "run_traces"
