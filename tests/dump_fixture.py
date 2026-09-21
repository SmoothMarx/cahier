"""Dump the panel's real payload for the JS harness.

    <venv>/python3 tests/dump_fixture.py > fixture.json

Uses the same FastAPI app + router the desktop hits, so the harness renders
against bytes the backend actually produced.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_FILE = HERE.parent / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/cahier-hub"


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location("cahier_hub_api", API_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cahier_hub_api"] = module
    spec.loader.exec_module(module)

    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    client = TestClient(app)

    scope = sys.argv[1] if len(sys.argv) > 1 else "active"
    payload = client.get(f"{PREFIX}/list", params={"scope": scope}).json()
    json.dump(payload, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
