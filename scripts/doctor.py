#!/usr/bin/env python3
"""Cahier Hub — doctor.

Prints the same checklist ``GET /doctor`` returns, so an install can be checked
without the desktop app. Nothing here writes.

    python3 scripts/doctor.py
    python3 scripts/doctor.py --json
    python3 scripts/doctor.py --base http://192.168.1.20:8766/

Exit code: 0 when nothing FAILs, 1 when something does (warnings do not fail the
run — a fresh install legitimately has no registry yet).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
API_FILE = PLUGIN / "dashboard" / "plugin_api.py"

GLYPH = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}


def load_api():
    """Import the plugin's backend module by path (it is not an installed package)."""
    if not API_FILE.is_file():
        sys.exit(f"dashboard/plugin_api.py is missing from {PLUGIN}")
    spec = importlib.util.spec_from_file_location("cahier_hub_api", API_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("cahier_hub_api", module)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="Is this Cahier Hub install wired up?")
    parser.add_argument("--base", default=None,
                        help="the URL answerers open, to check it (default: from settings)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    try:
        api = load_api()
    except ImportError as exc:  # fastapi lives in the host app's venv, not always here
        sys.exit(f"cannot import the backend ({exc}); run this with the interpreter that "
                 "runs Hermes, or use GET /doctor from the panel")

    report = api.doctor_report(args.base)
    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if report["fails"] else 0

    print(f"cahier-hub {report['version']} — doctor  ({report['generated_at']})")
    print()
    for check in report["checks"]:
        print(f"  [{GLYPH.get(check['status'], check['status'])}] {check['name']}: {check['detail']}")
        if check.get("fix") and check["status"] != "ok":
            print(f"          → {check['fix']}")
    layers = report["settings"]["layers"]
    non_default = {k: v for k, v in layers.items() if v != "default"}
    print()
    print(f"  settings from: {non_default or 'defaults only'}")
    print()
    print(f"{report['fails']} fail(s), {report['warns']} warning(s)")
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
