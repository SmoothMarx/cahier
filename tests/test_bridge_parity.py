"""Cross-implementation parity: control plane ↔ bridge ↔ panel.

Three code paths answer "what cahiers exist?":

  * `cahier_ctl.build_rows()`       — the control plane (source of truth)
  * `GET /fleet/cahiers` on :8766   — the bridge's read-only view of the above
  * `GET /list` on the panel router — what the sidebar actually renders

They must agree exactly: same slug set, no duplicates, no drops. A cahier that
shows in one and not the others is either invisible in the panel (dropped) or a
phantom the user can click but never open.

The bridge half SKIPS when :8766 is not listening (laptop off, service down) —
a stopped optional service must never turn the suite red. The control-plane half
runs unconditionally.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_cahier import PREFIX, _load, api, client, ctl  # noqa: E402,F401

BRIDGE = os.environ.get("CAHIER_BRIDGE", "http://127.0.0.1:8766/fleet/cahiers")


def _dupes(seq):
    return sorted(s for s, n in Counter(seq).items() if n > 1)


@pytest.fixture(scope="module")
def bridge_rows():
    try:
        with urllib.request.urlopen(BRIDGE, timeout=10) as resp:
            import json

            return json.loads(resp.read())["rows"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        pytest.skip(f"bridge not reachable at {BRIDGE}: {exc}")


def test_bridge_and_panel_agree_on_the_slug_set(client, bridge_rows):
    panel = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]
    b = [r["slug"] for r in bridge_rows]
    p = [r["slug"] for r in panel]
    assert set(b) == set(p), (
        f"panel and bridge disagree — dropped {sorted(set(b) - set(p))}, "
        f"phantom {sorted(set(p) - set(b))}"
    )
    assert not _dupes(b), f"bridge returned duplicate slugs: {_dupes(b)}"
    assert not _dupes(p), f"panel returned duplicate slugs: {_dupes(p)}"


def test_bridge_and_panel_agree_on_served_state(client, bridge_rows):
    panel = {r["slug"]: r for r in client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]}
    mismatch = [
        (r["slug"], r.get("served"), panel[r["slug"]]["served"])
        for r in bridge_rows
        if r["slug"] in panel and bool(r.get("served")) != bool(panel[r["slug"]]["served"])
    ]
    assert not mismatch, f"served flag drift (slug, bridge, panel): {mismatch}"


def test_active_scope_never_exceeds_all_scope(client):
    allr = client.get(f"{PREFIX}/list", params={"scope": "all"}).json()["rows"]
    act = client.get(f"{PREFIX}/list", params={"scope": "active"}).json()["rows"]
    assert {r["slug"] for r in act} <= {r["slug"] for r in allr}
    assert all(r["lifecycle"] == "serving" for r in act), \
        "active scope must mean exactly 'on the bridge'"
    paused = client.get(f"{PREFIX}/list", params={"scope": "paused"}).json()["rows"]
    assert all(r["lifecycle"] == "paused" for r in paused), "paused scope leaked a row"
    assert not ({r["slug"] for r in act} & {r["slug"] for r in paused})
