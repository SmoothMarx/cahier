"""Cahier Hub — backend for the desktop sidebar page.

Every number the panel shows comes from ``cahier_ctl.iteration()``: the same
read-only call the CLI's ``list`` subcommand uses, so the page and the terminal
can never disagree about what a cahier IS.

GETs never write — not to the share directory, not to the registry, not to the
answers DB. The single exception is ``POST /filing``, where a human clicks ✎ and
says which profile/project a cahier belongs to; it goes through
``cahier_ctl.set_filing`` (validated, atomic, logged) so a panel edit and a
hand-edit of the same file are the same operation. When ``cahier_ctl.py`` cannot
be imported it degrades to the registry file alone and says so in
``integrity.warnings`` instead of pretending the list is complete.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

VERSION = "0.2.0"
BRIDGE_PORT = int(os.environ.get("CAHIER_HUB_BRIDGE_PORT") or 8766)
CACHE_TTL = float(os.environ.get("CAHIER_HUB_CACHE_TTL") or 5.0)
PROBE_TTL = 10.0

router = APIRouter()

_cache: dict[str, tuple[float, Any]] = {}
_ctl: Any = None
_ctl_error = ""


# --------------------------------------------------------------- control plane


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _ctl_path() -> Path:
    return _home() / "scripts" / "cahier_ctl.py"


def ctl() -> Any:
    """Import cahier_ctl once. None (plus the reason) when it is unusable."""
    global _ctl, _ctl_error
    if _ctl is not None or _ctl_error:
        return _ctl
    path = _ctl_path()
    if not path.is_file():
        _ctl_error = f"cahier_ctl.py not found at {path}"
        return None
    try:
        spec = importlib.util.spec_from_file_location("cahier_ctl", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("cahier_ctl", module)
        spec.loader.exec_module(module)
        _ctl = module
    except Exception as exc:  # a broken control plane degrades, never 500s
        _ctl_error = f"{type(exc).__name__}: {exc}"
        _ctl = None
    return _ctl


def _registry_path() -> Path:
    module = ctl()
    stored = getattr(module, "REGISTRY", None) if module else None
    return Path(stored) if stored else _home() / "state" / "cahier-registry.json"


def _groups_path() -> Path:
    """Where the human's filings live. Same precedence as the control plane, so the
    panel can never write a file the resolver does not read."""
    env = os.environ.get("CAHIER_HUB_GROUPS")
    if env:
        return Path(env)
    module = ctl()
    stored = getattr(module, "GROUPS", None) if module else None
    return Path(stored) if stored else _home() / "state" / "cahier-groups.json"


def _degraded_payload(scope: str) -> dict:
    """No control plane: the registry file is all we have, and we say so."""
    path = _registry_path()
    warnings = [f"cahier_ctl.py unavailable ({_ctl_error or 'not loaded'}) — "
                "showing the registry file only"]
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
        book = reg.get("cahiers") or {}
        warnings.append("served files were not scanned: the panel may be showing a stale list")
    except Exception as exc:
        book, reg = {}, {}
        warnings.append(f"registry unreadable at {path}: {exc}")

    rows = []
    for slug, ent in sorted(book.items()):
        pin = ent.get("pin") if isinstance(ent.get("pin"), dict) else {}
        served_path = ent.get("path") or ""
        rows.append({
            "slug": slug,
            "title": ent.get("title") or slug,
            "state": ent.get("state") or "unknown",
            "why": ent.get("why") or "",
            "served": bool(ent.get("served")),
            "two_way": bool(ent.get("two_way")),
            "armed": bool(ent.get("armed")),
            "deadline": ent.get("deadline") or "",
            "saves": ent.get("saves") or 0,
            "people": ent.get("people") or 0,
            "last_save": ent.get("last_save") or "",
            "file": Path(served_path).name if served_path else None,
            "file_age": None,
            "path": served_path,
            "controllable": bool(ent.get("controllable")),
            "action": ent.get("action") or "",
            "protected": bool(pin.get("protected")),
            "pin_users": pin.get("users", 0),
            "page_pin": bool(pin.get("page_pin")),
            "first_seen": ent.get("first_seen") or "",
            "last_change": ent.get("last_change") or "",
            "profile": ent.get("profile") or "",
            "project": ent.get("project") or "",
            "profile_source": ent.get("profile_source") or "",
            "project_source": ent.get("project_source") or "",
            "in_registry": True,
        })

    try:
        module = ctl()
        states = list(module.resolve_scope(scope)) if module else ["live", "pending"]
    except Exception:
        states = ["live", "pending"]
    picked = [r for r in rows if r["state"] in states]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scope": scope,
        "states": states,
        "shown": len(picked),
        "rows": picked,
        "vocab": {"profiles": [], "projects": []},
        "counts": {s: sum(1 for r in rows if r["state"] == s) for s in
                   ("live", "pending", "finished", "stopped", "unknown")} | {"total": len(rows)},
        "registry": {"path": str(path), "updated": reg.get("updated", ""), "entries": len(book)},
        "integrity": {"warnings": warnings, "duplicates": [], "unindexed_inbox": [],
                      "served_without_slug": [], "unregistered": [], "stale_registry_states": []},
        "degraded": True,
    }


def payload(scope: str) -> dict:
    """Cached iteration for a scope. The cache is what keeps a 30s panel cheap."""
    key = f"list:{scope}"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    module = ctl()
    if module is None:
        value = _degraded_payload(scope)
    else:
        try:
            value = module.iteration(scope=scope)
            value["degraded"] = False
        except Exception as exc:
            value = _degraded_payload(scope)
            value["integrity"]["warnings"].insert(0, f"iteration() failed: {type(exc).__name__}: {exc}")
    _cache[key] = (now, value)
    return value


# --------------------------------------------------------------------- bridge


def _bridge_base(request: Request) -> str:
    override = os.environ.get("CAHIER_HUB_BASE_URL")
    if override:
        return override.rstrip("/") + "/"
    host = request.url.hostname or "127.0.0.1"
    return f"http://{host}:{BRIDGE_PORT}/"


def _bridge_up() -> bool:
    """Is the static bridge answering? Probed on loopback, always.

    The URL we hand out uses the request's host (so the app's machine can reach
    it); the probe must use a host the backend itself can resolve.
    """
    probe = f"http://127.0.0.1:{BRIDGE_PORT}/"
    key = "probe:loopback"
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < PROBE_TTL:
        return bool(hit[1])
    ok = False
    try:
        with urllib.request.urlopen(probe, timeout=1.5) as resp:
            ok = 200 <= resp.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        ok = False
    _cache[key] = (now, ok)
    return ok


def _link_rows(rows: list, base: str) -> list:
    """Attach the served URL. The bridge serves GET without a token (the token
    gates the /save inbox only), so a plain link is the whole access story."""
    out = []
    for row in rows:
        item = dict(row)
        file_name = item.get("file")
        item["url"] = f"{base}{file_name}" if file_name and item.get("served") else ""
        out.append(item)
    return out


# --------------------------------------------------------------------- routes


@router.get("/health")
def health(request: Request) -> dict:
    module = ctl()
    reg = payload("all")
    base = _bridge_base(request)
    return {
        "ok": True,
        "version": VERSION,
        "degraded": reg.get("degraded", False),
        "control_plane": {
            "path": str(_ctl_path()),
            "loaded": module is not None,
            "error": _ctl_error,
        },
        "registry": reg["registry"],
        "bridge": {"base": base, "up": _bridge_up()},
        "counts": reg["counts"],
        "generated_at": reg["generated_at"],
    }


@router.get("/list")
def list_cahiers(
    request: Request,
    scope: str = Query("active", description="active | all | live,pending | finished…"),
) -> dict:
    data = payload(scope)
    base = _bridge_base(request)
    data = dict(data)
    data["rows"] = _link_rows(data["rows"], base)
    data["bridge"] = {"base": base, "up": _bridge_up()}
    return data


@router.get("/cahier")
def one_cahier(request: Request, slug: str = Query(..., min_length=1)) -> dict:
    data = payload("all")
    base = _bridge_base(request)
    for row in data["rows"]:
        if row["slug"] == slug:
            return {"generated_at": data["generated_at"], **_link_rows([row], base)[0]}
    raise HTTPException(status_code=404, detail=f"no cahier with slug {slug!r}")


@router.post("/filing")
def set_filing(request: Request, body: dict = Body(...)) -> dict:
    """The ONE write this backend performs, and only because a human asked.

    The panel's ✎ is the only caller. The write goes through
    ``cahier_ctl.set_filing`` — validated slug, atomic rename, logged to the
    control log — so a panel edit and a hand-edit of the same file are the same
    operation, and an unknown slug is refused instead of inventing an entry.
    Sending an empty string for a field FORGETS that override.
    """
    module = ctl()
    if module is None:
        raise HTTPException(status_code=503,
                           detail="cahier_ctl.py is unavailable — refusing to write a filing blind")
    slug = str((body or {}).get("slug") or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")
    listed = {r["slug"] for r in payload("all")["rows"]}
    if slug not in listed:
        raise HTTPException(status_code=404, detail=f"no cahier with slug {slug!r}")
    payload_body = body or {}
    for field in ("profile", "project"):
        value = payload_body.get(field)
        if value is not None and len(str(value)) > 64:
            raise HTTPException(status_code=400, detail=f"{field} is longer than 64 characters")
    result = module.set_filing(
        slug,
        profile=payload_body.get("profile"),
        project=payload_body.get("project"),
        clear=bool(payload_body.get("clear")),
        actor="panel",
        path=_groups_path(),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "filing rejected")
    _cache.clear()  # the very next GET must show the new grouping, not a 5s-old one
    return {**result, "row": one_cahier(request, slug=slug), "written_by": "panel"}

