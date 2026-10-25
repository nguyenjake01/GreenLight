"""Keep tests independent of the developer's `.env`.

Modules such as `verifier.core.service` call `load_dotenv()` at import time, which would leak
local settings (SANDBOX_BACKEND, RUNCHECK_IMAGE, LLM_*) into every later test. Tests set the
environment they need with monkeypatch instead.
"""
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
