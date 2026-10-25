"""Environment config with the §6.2 defaults. The only place defaults live.

Entry points (service.py, demo.py, CLIs) call ``dotenv.load_dotenv()`` first.
"""
from __future__ import annotations

import os

# Variables with no default ("—" in §6.2) map to "".
DEFAULTS: dict[str, str] = {
    "VERIFIER_REAL": "",
    "RUNS_DIR": "runs",
    "GITHUB_TOKEN": "",
    "GITHUB_FETCH_MODE": "replay",
    "GITHUB_CACHE_DIR": "fixtures/cache",
    "LLM_BASE_URL": "https://inference.local/v1",
    "LLM_MODEL": "Qwen3.6-35B-A3B-NVFP4",
    "LLM_NO_THINK_EXTRA": '{"chat_template_kwargs": {"enable_thinking": false}}',
    "SLACK_BOT_TOKEN": "",
    "SLACK_CHANNEL_ID": "",
    "GREENHOUSE_WEBHOOK_SECRET": "",
    "HARVEST_AUTH_URL": "http://localhost:8100/token",
    "HARVEST_BASE_URL": "http://localhost:8100/v3",
    "HARVEST_CLIENT_ID": "mock",
    "HARVEST_CLIENT_SECRET": "mock",
    "NOTE_VISIBILITY": "private",
    "VERIFIER_WEBHOOK_URL": "http://localhost:8200/webhooks/greenhouse",
    "ATS_PUBLIC_URL": "http://localhost:8100",
    "INTERNAL_TOKEN": "",
    "SANDBOX_BACKEND": "openshell",
    "RUNCHECK_IMAGE": "resume-verifier-runcheck:latest",
    "RUN_CHECK": "on",
    "SLACK_ACTIONS": "plugin",
    "CUT": "",
    "WRITEBACK_CAP_S": "600",
}


def env(name: str) -> str:
    """The environment value if it is non-empty, else the §6.2 default (KeyError if unknown)."""
    value = os.environ.get(name)
    if value:
        return value
    return DEFAULTS[name]
