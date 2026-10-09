#!/usr/bin/env python3
"""Cahier control plane — list / spin up (publish) / spin down (unpublish).

Bundled copy, shipped with the Cahier plugin so a fresh install has a
control plane before the user has one of their own. If a copy also exists at
$HERMES_HOME/scripts/cahier_ctl.py, THAT one is used (and is the one the CLI
and any cron job are running) — keep the real one there and treat this file as
a starting point. `GET /doctor` reports which copy is live and whether the two
have drifted.

Read-only by default. Every action needs --apply, so a mistake in the page or a
typo on the CLI shows you the plan instead of moving your files.

Why it works this way:
  * ONE shared bridge (cahier-share.service) serves the whole folder, and the
    inbox watcher + funnel are global too. So "spin down" must never stop those
    services — it takes the page out of the served folder instead, which is
    per-cahier, reversible, and leaves everyone else's page alone.
  * The one thing that IS per-cahier is the deadline timer. Spinning down a live
    cahier disarms it if (and only if) the armed slug matches.
  * A cahier is SAVED in its own project folder (rule, 2026-10-03): spinning down
    files the page into <project>/Cahiers, spinning up publishes it again from
    there in its ORIGINAL settings — same file, same gate, same deadline while it
    has not expired.
  * Pause and Close are the SAME file move with a different note: pause keeps the
    row active (the way back is one click), close marks it done. Only the registry
    can carry that note — the page is off the bridge either way.
  * Nothing here touches the index pages (INDEX_PAGES).

Usage:
  cahier_ctl.py list [--json]
  cahier_ctl.py up    --slug finance-filing [--apply]  # back in its original settings
  cahier_ctl.py pause --slug finance-filing [--apply]  # off the bridge, still active
  cahier_ctl.py close --slug finance-filing [--apply]  # off the bridge, marked done
  cahier_ctl.py down  --slug finance-filing [--apply]  # alias of close
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
H = os.path.join(HOME, ".hermes")
SERVE = os.path.join(HOME, "cahier-share")
RETIRED = os.path.join(H, "state", "cahier-retired")
STATE = os.path.join(H, "state")
REGISTRY = os.path.join(STATE, "cahier-registry.json")
DEADLINE = os.path.join(STATE, "cahier-deadline.json")
ANSWERS_DB = os.path.join(STATE, "cahier-answers.db")
CONTROL_LOG = os.path.join(STATE, "cahier-control.log")
PROJECTS = os.path.join(HOME, "Projects")
# Filing: which PROFILE built a cahier and which PROJECT it belongs to. Kept in one
# file a human can edit by hand (the panel writes the same file, through this
# module), so the answer survives every consumer and is never re-derived.
GROUPS = os.path.join(STATE, "cahier-groups.json")
SESSIONS_DB = os.path.join(H, "state.db")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Optional declarations a build may stamp into the page (see scripts/cahier.py):
# they are the strongest evidence of origin that travels WITH the artifact.
PROFILE_DECL = re.compile(r"""(?:const|let|var)\s+CAHIER_PROFILE\s*=\s*["']([^"']{0,64})["']""")
PROJECT_DECL = re.compile(r"""(?:const|let|var)\s+CAHIER_PROJECT\s*=\s*["']([^"']{0,64})["']""")
# The build session, wherever it sits. PURPOSE is a JSON string that may itself
# carry an escaped JSON payload, so the quotes around `session` can arrive with any
# number of backslashes in front — hence the permissive runs, tight id pattern.
SESSION_DECL = re.compile(r"""[\\"']{0,4}session[\\"']{0,4}\s*:\s*[\\"']{0,4}(\d{8}_[A-Za-z0-9_]{4,})""")
SLUG_DECL = re.compile(r"""SLUG\s*=\s*["']([^"']+)["']""")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# The generated page's real name is injected as `const TITLE = "..."`; the static
# <title>/<h1> keep the template's generic default, so they must never win over it.
JS_TITLE_RE = re.compile(r"""(?:const|let|var)\s+TITLE\s*=\s*["\']([^"\']+)["\']""")
GENERIC_TITLES = ("two-way cahier", "two way cahier", "cahier", "page title", "title",
                  "filterable table", "multipage viewer", "untitled")


def is_generic_title(s):
    """A template default, not a name: compare letters only, so 'Two-way cahier'
    and 'Two-Way  Cahier' are both rejected while a real title never is."""
    key = re.sub(r"[^a-z]", "", (s or "").lower())
    return not key or key in {re.sub(r"[^a-z]", "", g) for g in GENERIC_TITLES}

def _env_set(name, default):
    """A set from a comma-separated env var, or the shipped default."""
    raw = os.environ.get(name)
    return {s.strip() for s in raw.split(",") if s.strip()} if raw else set(default)


# Index / status pages live in the share directory but are not cahiers: kept out of
# the cahier list and never spin-down-able, whatever else happens. Override the
# names with CAHIER_INDEX_PAGES and CAHIER_NEVER (comma-separated).
INDEX_PAGES = _env_set("CAHIER_INDEX_PAGES", ("fleet-status.html", "fleet-status.json"))
NEVER = _env_set("CAHIER_NEVER", ("fleet-status", "fleet-status-json"))
STATUS_JSON = "fleet-status.json"

# Where a cahier is SAVED (rule, 2026-10-03): its own project folder, under a
# `Cahiers/` subfolder. A project this machine does not have falls back to
# ~/Projects/Cahiers. The share dir stays a SERVING copy: publishing puts the
# page there, spinning down files it back home.
CAHIERS_DIRNAME = "Cahiers"
CAHIER_DIR_NAMES = {"cahiers", "cahier"}

TWO_WAY_MARKERS = ("/save", "X-Save-Token")


# --------------------------------------------------------------------------- io
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    path = os.fspath(path)  # callers hand us Path objects as often as strings
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def read_text(path, cap=400_000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except OSError:
        return ""


def log_action(entry):
    entry = dict(entry)
    entry["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(CONTROL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ homes
def project_dir(project):
    """The real folder behind a project label, or '' — never creates anything."""
    label = re.sub(r"\s+", " ", str(project or "")).strip()
    if not label or not os.path.isdir(PROJECTS):
        return ""
    for name in sorted(os.listdir(PROJECTS)):
        path = os.path.join(PROJECTS, name)
        if name.startswith(".") or not os.path.isdir(path):
            continue
        if name.lower() == label.lower():
            return path
    return ""


def cahier_home(project="", create=False):
    """Where a cahier's page is SAVED: <project>/Cahiers, else ~/Projects/Cahiers.

    create=True makes the folder (a real save). Reads pass create=False: the
    panel's GETs must not touch this disk.
    """
    base = project_dir(project)
    dest = os.path.join(base, CAHIERS_DIRNAME) if base else os.path.join(PROJECTS, CAHIERS_DIRNAME)
    if create:
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError:
            return ""
        if not os.access(dest, os.W_OK):
            return ""
    return dest


# ------------------------------------------------------------------ discovery
def page_meta(path):
    """Only real two-way cahiers get a slug: the SLUG declaration is the marker."""
    txt = read_text(path)
    if not txt:
        return None
    m = SLUG_DECL.search(txt)
    if not m:
        return None
    slug = m.group(1).strip()
    if not SLUG_RE.match(slug):
        return None
    low = txt.lower()
    two_way = any(k.lower() in low for k in TWO_WAY_MARKERS)
    cands = []
    jt = JS_TITLE_RE.search(txt)
    if jt:
        cands.append(jt.group(1))
    t = TITLE_RE.search(txt)
    if t:
        cands.append(t.group(1))
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", txt, re.I | re.S)
    if h1:
        cands.append(re.sub(r"<[^>]+>", "", h1.group(1)))
    title = ""
    for c in cands:
        c = re.sub(r"\s+", " ", c or "").strip()
        if not is_generic_title(c):
            title = c
            break
    # Nowhere carries a real name: hand back nothing, and the caller falls back to the
    # file's own name — the one label every cahier is guaranteed to have.
    pd = PROFILE_DECL.search(txt)
    pj = PROJECT_DECL.search(txt)
    sd = SESSION_DECL.search(txt)
    return {"slug": slug, "title": title[:120], "two_way": two_way,
            "declared_profile": (pd.group(1).strip() if pd else ""),
            "declared_project": (pj.group(1).strip() if pj else ""),
            "origin_session": (sd.group(1).strip() if sd else "")}


def scan_served():
    out = {}
    if not os.path.isdir(SERVE):
        return out
    for name in sorted(os.listdir(SERVE)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(SERVE, name)
        meta = page_meta(path)
        if not meta:
            continue
        meta.update({"file": name, "served_path": path})
        out[meta["slug"]] = meta
    return out


def scan_retired():
    out = {}
    if not os.path.isdir(RETIRED):
        return out
    for name in sorted(os.listdir(RETIRED)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(RETIRED, name)
        meta = page_meta(path)
        if not meta:
            continue
        meta.update({"file": name, "retired_path": path})
        out[meta["slug"]] = meta
    return out


def scan_pending(max_files=400):
    """Two-way cahiers built outside the served folder = never published."""
    out = {}
    if not os.path.isdir(PROJECTS):
        return out
    seen = 0
    for root, dirs, files in os.walk(PROJECTS):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "venv", "__pycache__")]
        if root.count(os.sep) - PROJECTS.count(os.sep) > 2:
            dirs[:] = []
            continue
        # A `Cahiers/` folder IS the declaration: any .html inside it is a cahier
        # (rule 2026-10-03). Anywhere else the NAME has to say so, because the
        # rest of the tree is arbitrary and unslugged pages are not ours.
        explicit = os.path.basename(root).lower() in CAHIER_DIR_NAMES
        for name in files:
            if not name.endswith(".html"):
                continue
            low = name.lower()
            if not explicit and not any(k in low for k in ("cahier", "merges", "questions", "survey", "poll")):
                continue
            seen += 1
            if seen > max_files:
                return out
            path = os.path.join(root, name)
            meta = page_meta(path)
            if not meta or not meta["two_way"]:
                continue
            meta.update({"file": name, "source_path": path})
            out.setdefault(meta["slug"], meta)
    return out


def answer_stats():
    stats = {}
    if not os.path.exists(ANSWERS_DB):
        return stats
    try:
        con = sqlite3.connect(f"file:{ANSWERS_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT slug, COUNT(*) saves, COUNT(DISTINCT user) people,"
            " MIN(saved_at) first_save, MAX(saved_at) last_save FROM saves GROUP BY slug"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return stats
    for slug, saves, people, first, last in rows:
        stats[slug] = {"saves": saves, "people": people,
                       "first_save": (first or "")[:19], "last_save": (last or "")[:19]}
    return stats


TEST_SLUG_RE = re.compile(r"(^|[-_])(test|probe|e2e|demo|zz)([-_]|$)", re.I)


def served_extras():
    """Served files that are NOT controlled cahiers, so they are never hidden."""
    controlled = {m["served_path"] for m in scan_served().values()}
    out = []
    if not os.path.isdir(SERVE):
        return out
    for name in sorted(os.listdir(SERVE)):
        path = os.path.join(SERVE, name)
        if not os.path.isfile(path) or path in controlled:
            continue
        if name in INDEX_PAGES:
            continue
        out.append({"name": name, "bytes": os.path.getsize(path),
                    "age": human_age(time.time() - os.path.getmtime(path))})
    for sub in ("leaflet", "tickets"):
        d = os.path.join(SERVE, sub)
        if os.path.isdir(d):
            n = len([f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))])
            out.append({"name": sub + "/", "bytes": None,
                        "age": human_age(time.time() - os.path.getmtime(d)), "files": n})
    return out


# ------------------------------------------------------------------ states
def deadline_info():
    d = read_json(DEADLINE, {})
    if not d.get("slug"):
        return {}
    return d


def human_age(sec):
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{sec // 60}m"
    if sec < 172800:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def build_rows():
    """One row per cahier, with the evidence behind its state. No invented fields."""
    now = time.time()
    served, retired, pending = scan_served(), scan_retired(), scan_pending()
    stats, dl = answer_stats(), deadline_info()
    armed_slug = dl.get("slug")
    armed_until = None
    if armed_slug and dl.get("deadline"):
        try:
            armed_until = datetime.fromisoformat(dl["deadline"]).timestamp()
        except ValueError:
            armed_until = None

    # The registry is where a pause / close note lives: the page is off the bridge
    # either way, so the filesystem alone cannot tell the two apart.
    registry = load_registry()["cahiers"]
    slugs = sorted(set(served) | set(retired) | set(pending) | set(stats))
    rows = []
    for slug in slugs:
        if slug in NEVER:
            continue
        s, r, p = served.get(slug), retired.get(slug), pending.get(slug)
        meta = s or r or p or {}
        st = stats.get(slug, {})
        served_now = bool(s)
        is_armed = slug == armed_slug and (armed_until is None or armed_until > now)
        two_way = bool((meta.get("two_way")) or st)
        mark = str((registry.get(slug) or {}).get("lifecycle") or "").strip().lower()

        if is_armed:
            state, why = "live", f"deadline armed to {dl.get('deadline', '')[:16]}"
        elif served_now and st.get("saves"):
            state, why = "finished", (f"{st['saves']} save(s) from {st['people']} · "
                                      f"last {st['last_save'][:10] or '—'} · still served")
        elif served_now and not st.get("saves"):
            state, why = "pending", "served but no answers have ever come in"
        elif not served_now and st.get("saves"):
            state, why = "finished", f"{st['saves']} save(s) · page no longer served"
        elif p:
            state, why = "pending", "built, never published"
        elif r:
            state, why = "stopped", "unpublished"
        else:
            state, why = "unknown", "no page and no answers on record"

        # LIFECYCLE answers the question the buttons ask — on the bridge, parked on
        # purpose, or done — while state keeps answering "how is it doing". A page
        # that was never published has no lifecycle: its state says it all.
        if served_now:
            lifecycle = "serving"
        elif mark in ("paused", "closed"):
            lifecycle = mark
        elif r or st.get("saves"):
            lifecycle = "closed"
        else:
            lifecycle = ""
        if not two_way:
            actions = []
        elif lifecycle == "serving":
            actions = ["pause", "close"]
        elif lifecycle == "paused":
            actions = ["up", "close"]
        else:
            actions = ["up"]

        path = (meta.get("served_path") or meta.get("retired_path")
                or meta.get("source_path"))
        mtime = None
        if path and os.path.exists(path):
            mtime = os.path.getmtime(path)

        fil = filing(slug, meta)
        # The canonical home: the project's own Cahiers folder, else
        # ~/Projects/Cahiers. Computed here, never created here.
        home = cahier_home(fil["project"])
        file_name = meta.get("file") or ""

        rows.append({
            "slug": slug,
            "title": meta.get("title") or slug,
            "file": file_name or "—",
            "state": state,
            "lifecycle": lifecycle,
            "why": why,
            "served": served_now,
            "two_way": two_way,
            "armed": is_armed,
            "deadline": (dl.get("deadline") if is_armed else ""),
            "saves": st.get("saves", 0),
            "people": st.get("people", 0),
            "last_save": st.get("last_save", ""),
            "path": path or "—",
            "home": home,
            "home_path": os.path.join(home, file_name) if (home and file_name) else "",
            "file_age": human_age(now - mtime) if mtime else "—",
            # the button follows what the page IS, not what the deadline says:
            # served -> spin down (unpublish), not served -> spin up (publish)
            "action": ("down" if served_now else "up") if two_way else "none",
            # Every legal move for THIS row, in the order the panel offers them.
            "actions": actions,
            "controllable": two_way and slug not in NEVER,
            # where it lives in the fleet: profile groups, project sub-groups
            "profile": fil["profile"],
            "project": fil["project"],
            "profile_source": fil["profile_source"],
            "project_source": fil["project_source"],
        })
    # Serving first, then paused, then by state: the rows a human can act on at
    # the top of the flat list.
    life_order = {"serving": 0, "paused": 1}
    order = {"live": 0, "pending": 1, "finished": 2, "stopped": 3, "unknown": 4}
    rows.sort(key=lambda x: (life_order.get(x.get("lifecycle", ""), 2),
                             order.get(x["state"], 9), x["slug"]))
    return rows


def counts(rows):
    c = {"live": 0, "pending": 0, "finished": 0, "stopped": 0, "unknown": 0}
    life = {k: 0 for k in LIFECYCLES}
    for r in rows:
        c[r["state"]] = c.get(r["state"], 0) + 1
        k = r.get("lifecycle")
        if k in life:
            life[k] += 1
    c["total"] = len(rows)
    c["lifecycle"] = life
    return c


# ------------------------------------------------------------------ registry
# The registry is the ONE place that remembers a cahier across its whole life:
# built -> served -> finished -> retired. It is a cache of what the filesystem
# already says (build_rows is still the truth), so losing it costs nothing but
# history — never a page. Writes are merge-only: a slug is never deleted.
INBOX = os.path.join(STATE, "cahier-inbox")
PINS = os.path.join(STATE, "cahier-template-pins.json")
TEMPLATES = os.path.join(H, "skills", "productivity", "cahier", "templates")
BRIDGE_URL = "http://127.0.0.1:8766/"

# unit -> state(s) that mean healthy. The two one-shot services are meant to sit
# inactive: they fire on a path event / deadline and go back to sleep.
UNITS_OK = {
    "cahier-share.service": ("active",),
    "cahier-inbox.path": ("active",),
    "cahier-deadline.timer": ("active",),
    "cahier-deadline.service": ("inactive", "failed"),
    "cahier-inbox-trigger.service": ("inactive", "failed"),
}
ONE_SHOT_UNITS = {"cahier-deadline.service", "cahier-inbox-trigger.service"}


def pin_info(path):
    """PIN presence, read from the page itself — the USERS map is the marker."""
    txt = read_text(path, 300_000)
    m = re.search(r"(?:const|let|var)\s+USERS\s*=\s*\{([^}]*)\}", txt)
    users = len(re.findall(r"[\"'][^\"']+[\"']\s*:", m.group(1))) if m else 0
    gated = bool(re.search(r"(?:const|let|var)\s+PAGE_PIN\s*=\s*[\"'][^\"']+[\"']", txt))
    return {"protected": bool(users) or gated, "users": users, "page_pin": gated}


def load_registry():
    reg = read_json(REGISTRY, {})
    if not isinstance(reg, dict) or not isinstance(reg.get("cahiers"), dict):
        return {"version": 1, "cahiers": {}}
    return reg


def save_registry(reg):
    reg["version"] = 1
    reg["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(REGISTRY, reg)


def sync_registry(rows, note=""):
    """Merge the current filesystem view into the registry.

    Merge-only on purpose: a cahier that got retired (or whose page was deleted)
    keeps its entry, so 'we asked the family about X in August and they said Y'
    survives the teardown. State transitions are stamped, not overwritten blind.
    """
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    reg = load_registry()
    book = reg["cahiers"]
    added, changed = [], []
    for r in rows:
        slug = r["slug"]
        path = r.get("path")
        prior = book.get(slug) or {}
        fresh = {
            "slug": slug,
            "title": r["title"],
            "state": r["state"],
            "why": r["why"],
            "served": r["served"],
            "two_way": r["two_way"],
            "armed": r["armed"],
            "deadline": r["deadline"],
            "saves": r["saves"],
            "people": r["people"],
            "last_save": r["last_save"],
            "path": path,
            "controllable": r["controllable"],
            "profile": r.get("profile", ""),
            "project": r.get("project", ""),
            "profile_source": r.get("profile_source", ""),
            "project_source": r.get("project_source", ""),
            "home": r.get("home", ""),
            "settings": _settings_snapshot(r, prior),
            # derived lifecycle, plus the note only a spin action can write
            "lifecycle": r.get("lifecycle") or prior.get("lifecycle") or "",
            "lifecycle_at": prior.get("lifecycle_at", ""),
        }
        if path and os.path.isfile(path):
            fresh["pin"] = pin_info(path)
        ent = prior if prior else None
        if ent is None:
            fresh.update(first_seen=now, last_seen=now, last_change=now)
            book[slug] = fresh
            added.append(slug)
            continue
        if ent.get("state") != fresh["state"]:
            fresh["last_change"] = now
            changed.append(f"{slug}: {ent.get('state')} -> {fresh['state']}")
        else:
            fresh["last_change"] = ent.get("last_change") or now
        fresh["first_seen"] = ent.get("first_seen") or now
        fresh["last_seen"] = now
        book[slug] = fresh
    if note:
        reg["last_note"] = note
    save_registry(reg)
    return {"ok": True, "added": added, "changed": changed, "total": len(book)}


def _settings_snapshot(row, prior):
    """What a cahier needs to come back exactly as it was.

    The page carries its own gate (PINs, two-way, tabs), so this only has to
    remember the file name, the home folder, and a deadline that a spin down
    took away — "start it again in its original settings" (2026-10-03).
    """
    was = (prior or {}).get("settings")
    snap = dict(was) if isinstance(was, dict) else {}
    if row.get("file") not in (None, "", "—"):
        snap["file"] = row["file"]
    if row.get("home"):
        snap["home"] = row["home"]
    snap["two_way"] = bool(row.get("two_way"))
    deadline = row.get("deadline") or snap.get("deadline", "")
    if deadline:
        snap["deadline"] = deadline
    else:
        snap.pop("deadline", None)
    return {k: v for k, v in snap.items() if v not in (None, "")}


def saved_settings(slug):
    """The settings remembered for one cahier ({} when we never saw it served)."""
    ent = load_registry()["cahiers"].get(slug) or {}
    snap = ent.get("settings")
    return snap if isinstance(snap, dict) else {}


def remember_settings(slug, **fields):
    """Merge-only nudge to one registry entry's settings. merge-only because the
    registry is history: this must never erase what it already knew."""
    reg = load_registry()
    ent = reg["cahiers"].get(slug)
    if not isinstance(ent, dict):
        return {}
    snap = ent.get("settings") if isinstance(ent.get("settings"), dict) else {}
    snap.update({k: v for k, v in fields.items() if v not in (None, "")})
    ent["settings"] = snap
    save_registry(reg)
    return snap


def remember_lifecycle(slug, value="", actor="cli"):
    """The one note the filesystem cannot carry.

    A paused page and a closed page are byte-identical on disk: both are simply
    not in the serve folder. So the note that tells them apart (and the difference
    between either and "never published") lives in the registry, written by the
    spin action that caused it and cleared by the next spin up. '' forgets it.
    """
    reg = load_registry()
    ent = reg["cahiers"].get(slug)
    if not isinstance(ent, dict):
        ent = {"slug": slug,
               "first_seen": datetime.now().astimezone().isoformat(timespec="seconds")}
        reg["cahiers"][slug] = ent
    if value in LIFECYCLES:
        ent["lifecycle"] = value
        ent["lifecycle_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    else:
        ent.pop("lifecycle", None)
        ent.pop("lifecycle_at", None)
    save_registry(reg)
    log_action({"action": "lifecycle", "slug": slug, "lifecycle": ent.get("lifecycle", ""),
                "actor": actor})
    return ent.get("lifecycle", "")


# ---------------------------------------------------------------------- filing
# Two things a cahier knows about itself that nothing else records: which Hermes
# PROFILE built it, and which PROJECT it belongs to. Resolved per field, strongest
# evidence first, and every answer carries where it came from:
#
#   override  a human said so — the panel's ✎ or `cahier_ctl.py filing --set`
#   declared  the build stamped it into the page (CAHIER_PROFILE / CAHIER_PROJECT)
#   session   the session that built it, looked up in state.db (profile_name)
#   derived   a keyword match against the projects this machine actually has
#   none      nobody knows — said out loud instead of guessed
#
# Reading is always safe: a missing or garbled groups file is ignored, never fatal.
FILING_SOURCES = ("override", "declared", "session", "derived", "none")

_GROUPS_CACHE: dict = {}
_VOCAB_CACHE: dict = {}
_SESSION_PROFILE: dict = {}


def groups_path():
    """The one file both halves agree on; env-overridable so tests stay hermetic.

    The current name is checked first, the pre-2026-10-09 ``CAHIER_HUB_GROUPS``
    second — a deployment mid-rename must never read a different groups file than
    the panel writes.
    """
    return (os.environ.get("CAHIER_GROUPS") or os.environ.get("CAHIER_HUB_GROUPS")
            or GROUPS)


def load_groups(path=None):
    """The overrides, normalised. Cached on (mtime, size) so an edit is picked up
    on the next read — a panel write must never be served from a stale cache."""
    target = path or groups_path()
    try:
        st = os.stat(target)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    hit = _GROUPS_CACHE.get(target)
    if hit and hit[0] == stamp:
        return hit[1]
    data = read_json(target, {})
    clean = {}
    if isinstance(data, dict) and isinstance(data.get("cahiers"), dict):
        for slug, ent in data["cahiers"].items():
            if not isinstance(ent, dict):
                continue
            item = {k: str(ent[k]).strip() for k in ("profile", "project") if ent.get(k)}
            if item:
                clean[str(slug)] = item
    out = {"version": 1, "cahiers": clean,
           "updated": (data.get("updated") if isinstance(data, dict) else "") or "",
           "updated_by": (data.get("updated_by") if isinstance(data, dict) else "") or ""}
    _GROUPS_CACHE[target] = (stamp, out)
    return out


def save_groups(data, actor="cli", path=None):
    target = str(path or groups_path())
    out = {"version": 1,
           "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
           "updated_by": actor,
           "cahiers": {k: data["cahiers"][k] for k in sorted(data.get("cahiers") or {})}}
    write_json(target, out)
    _GROUPS_CACHE.pop(target, None)
    return out


def known_profiles():
    """Profile homes on this machine — the vocabulary the panel offers to type."""
    base = os.path.join(H, "profiles")
    extra = set()
    if os.path.isdir(base):
        extra = {d for d in os.listdir(base)
                 if not d.startswith(".") and os.path.isdir(os.path.join(base, d))}
    return ["default"] + sorted(extra - {"default"})


def project_vocab():
    """Labels a cahier's project could legitimately be: real dirs + fleet names."""
    hit = _VOCAB_CACHE.get("v")
    now = time.time()
    if hit and now - hit[0] < 60:
        return hit[1]
    names = {}
    if os.path.isdir(PROJECTS):
        for d in sorted(os.listdir(PROJECTS)):
            if not d.startswith(".") and os.path.isdir(os.path.join(PROJECTS, d)):
                names[d.lower()] = d
    fleet = read_json(os.path.join(SERVE, STATUS_JSON), {})
    for p in fleet.get("projects") or []:
        nm = p.get("name") if isinstance(p, dict) else None
        if nm:
            names.setdefault(str(nm).lower(), str(nm))
    _VOCAB_CACHE["v"] = (now, names)
    return names


def derive_project(slug):
    """Keyword match against the project vocabulary: the project's own NAME has to
    appear in the slug. The title is not consulted — it is prose, and 'Personal —
    Duplicates & Invoices' would otherwise file a Personal cahier under Invoices. No
    fuzzy guesses: a wrong project is worse than an empty one."""
    hay = re.sub(r"[^a-z0-9]+", " ", (slug or "").lower()).strip()
    flat = hay.replace(" ", "")
    vocab = project_vocab()
    best = ""
    for low in vocab:
        if (low in hay or low.replace(" ", "").replace(".", "") in flat) and len(low) > len(best):
            best = low
    return vocab.get(best, "")


def session_profile(sid):
    """The profile that owned a session id — state.db is the only place that knows."""
    if not sid:
        return ""
    if sid not in _SESSION_PROFILE:
        got = ""
        if os.path.exists(SESSIONS_DB):
            try:
                con = sqlite3.connect(f"file:{SESSIONS_DB}?mode=ro", uri=True)
                row = con.execute("SELECT profile_name FROM sessions WHERE id = ?",
                                  (sid,)).fetchone()
                con.close()
                got = (row[0] or "") if row else ""
            except sqlite3.Error:
                got = ""
        _SESSION_PROFILE[sid] = got
    return _SESSION_PROFILE[sid]


def filing(slug, meta=None, path=None):
    """profile + project for one cahier, each with the provenance of the answer."""
    meta = meta or {}
    over = (load_groups(path).get("cahiers") or {}).get(slug) or {}
    out = {}
    for field, declared in (("profile", meta.get("declared_profile")),
                            ("project", meta.get("declared_project"))):
        value = (over.get(field) or "").strip()
        source = "override"
        if not value:
            value = (declared or "").strip()
            source = "declared"
        if not value and field == "profile":
            value = session_profile(meta.get("origin_session") or "")
            source = "session"
        if not value and field == "project":
            value = derive_project(slug)
            source = "derived"
        if not value:
            source = "none"
        out[field] = value
        out[f"{field}_source"] = source
    return out


def set_filing(slug, profile=None, project=None, actor="cli", clear=False, path=None):
    """Record a human's answer for one cahier — the only write in the filing path.

    Atomic (write_json does tmp+rename) and logged, so a mistake is visible and
    reversible instead of silently half-applied. An empty string FORGETS that
    override and lets the resolver answer again.
    """
    slug = (slug or "").strip()
    if not SLUG_RE.match(slug):
        return {"ok": False, "error": f"not a cahier slug: {slug!r}"}
    target = str(path or groups_path())
    data = load_groups(target)
    book = data["cahiers"]
    if clear:
        book.pop(slug, None)
    else:
        ent = dict(book.get(slug) or {})
        for field, value in (("profile", profile), ("project", project)):
            if value is None:
                continue
            clean = re.sub(r"\s+", " ", str(value)).strip()[:64]
            if clean:
                ent[field] = clean
            else:
                ent.pop(field, None)
        if ent:
            book[slug] = ent
        else:
            book.pop(slug, None)
    save_groups(data, actor=actor, path=target)
    log_action({"actor": actor, "action": "filing", "slug": slug, "profile": profile,
                "project": project, "clear": bool(clear)})
    return {"ok": True, "slug": slug, "file": target,
            "after": dict(load_groups(target)["cahiers"].get(slug) or {})}


# ------------------------------------------------------------------ iteration
# ONE read-only entry point for every consumer: the CLI's `list` and the desktop
# panel both call this, so the two can never disagree about what a cahier IS.
# It only reads (build_rows + registry + two SQLite SELECTs) and never writes —
# recording a state change stays the caller's explicit decision (--sync).
# A scope is a view over a row's LIFECYCLE when it has one (serving / paused /
# closed) and over its state when it never left the bench (pending / unknown).
# "Active" used to mean live+pending; it now means "on the bridge right now" —
# the only reading of active that stays true once a pause exists.
SCOPES = {
    "active": ("serving",),
    "paused": ("paused",),
    "finished": ("finished", "stopped", "closed", "unknown"),
    "all": ("live", "pending", "finished", "stopped", "unknown",
            "serving", "paused", "closed"),
}
ALL_STATES = SCOPES["all"]
LIFECYCLES = ("serving", "paused", "closed")


def scope_key(row):
    """Which bucket a row answers to: its lifecycle when it has one, else its state."""
    return row.get("lifecycle") or row.get("state") or "unknown"


def resolve_scope(scope):
    """'active' | 'paused' | 'all' | 'serving,paused' -> tuple of buckets; junk falls back to all."""
    raw = (scope or "all").strip().lower()
    if raw in SCOPES:
        return SCOPES[raw]
    want = tuple(dict.fromkeys(s.strip() for s in raw.split(",")))
    keep = tuple(s for s in want if s in ALL_STATES)
    return keep or ALL_STATES


def _unindexed_inbox():
    """Inbox payloads the answers DB never recorded — the 'lost save' class."""
    inbox = sorted(f for f in os.listdir(INBOX)) if os.path.isdir(INBOX) else []
    if not inbox:
        return []
    filed = set()
    if os.path.exists(ANSWERS_DB):
        try:
            con = sqlite3.connect(f"file:{ANSWERS_DB}?mode=ro", uri=True)
            filed = {r[0] for r in con.execute("SELECT source_file FROM saves")}
            con.close()
        except sqlite3.Error:
            return []
    return [f for f in inbox if f not in filed]


def _served_without_slug():
    """Served .html that is not a controlled cahier: visible, not spin-down-able."""
    controlled = {m["served_path"] for m in scan_served().values()}
    if not os.path.isdir(SERVE):
        return []
    return [n for n in sorted(os.listdir(SERVE))
            if n.endswith(".html") and n not in INDEX_PAGES
            and os.path.join(SERVE, n) not in controlled]


def iteration(scope="all"):
    """The canonical, deterministic cahier list. Never writes, never invents."""
    rows = build_rows()
    reg = load_registry()
    book = reg["cahiers"]
    out, seen, dupes = [], set(), []
    for r in rows:
        slug = r["slug"]
        if slug in seen:  # build_rows already unions by slug; belt and braces
            dupes.append(slug)
            continue
        seen.add(slug)
        ent = book.get(slug) or {}
        pin = ent.get("pin")
        if not isinstance(pin, dict):
            pin = pin_info(r["path"]) if r.get("path") and os.path.isfile(r["path"]) else {}
        row = dict(r)
        row["protected"] = bool(pin.get("protected"))
        row["pin_users"] = pin.get("users", 0)
        row["page_pin"] = bool(pin.get("page_pin"))
        row["first_seen"] = ent.get("first_seen", "")
        row["last_change"] = ent.get("last_change", "")
        row["in_registry"] = bool(ent)
        out.append(row)

    want = resolve_scope(scope)
    picked = [r for r in out if scope_key(r) in want]
    on_disk = {r["slug"] for r in out}
    known = set(book)

    warnings = []
    unindexed = _unindexed_inbox()
    if unindexed:
        warnings.append(f"{len(unindexed)} inbox payload(s) never recorded in the answers DB: "
                        + ", ".join(unindexed[:3]))
    noslug = _served_without_slug()
    if noslug:
        warnings.append(f"{len(noslug)} served page(s) with no SLUG declaration: "
                        + ", ".join(noslug[:3]))
    unregistered = sorted(on_disk - known)
    if book:
        if unregistered:
            warnings.append(f"{len(unregistered)} cahier(s) missing from the registry: "
                            + ", ".join(unregistered[:3]))
    else:
        warnings.append("registry is empty — run: cahier_ctl.py list --sync")
    drift = sorted(s for s in known & on_disk
                   if book[s].get("state") != next(r["state"] for r in out if r["slug"] == s))
    if drift:
        warnings.append(f"{len(drift)} registry state(s) stale: " + ", ".join(drift[:3]))
    if dupes:
        warnings.append(f"duplicate slug(s) dropped: {', '.join(sorted(set(dupes)))}")

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": scope or "all",
        "states": list(want),
        "counts": counts(out),
        "shown": len(picked),
        "rows": picked,
        "vocab": {"profiles": known_profiles(),
                  "projects": sorted(set(project_vocab().values()), key=str.lower)},
        "registry": {"path": REGISTRY, "updated": reg.get("updated", ""), "entries": len(book)},
        "integrity": {"warnings": warnings, "duplicates": sorted(set(dupes)),
                      "unindexed_inbox": unindexed, "served_without_slug": noslug,
                      "unregistered": unregistered, "stale_registry_states": drift},
    }


# --------------------------------------------------------------------- doctor
def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def template_pins(create=False):
    """The templates are named DO NOT TOUCH because a stray edit silently changes
    every cahier built afterwards. Pin the hashes once, then drift is loud."""
    pins = read_json(PINS, {})
    if not isinstance(pins, dict):
        pins = {}
    found, drift, fresh = {}, [], {}
    if os.path.isdir(TEMPLATES):
        for name in sorted(os.listdir(TEMPLATES)):
            if not name.endswith(".html"):
                continue
            digest = _sha256(os.path.join(TEMPLATES, name))
            found[name] = digest
            want = pins.get(name)
            if want and want != digest:
                drift.append(name)
            elif not want:
                fresh[name] = digest
    missing = sorted(n for n in pins if n not in found)
    if fresh or create or missing:
        merged = dict(pins)
        merged.update(fresh)
        for n in missing:
            merged.pop(n, None)
        if merged != pins:
            write_json(PINS, merged)
    return {"ok": not drift, "files": len(found), "drift": drift,
            "newly_pinned": sorted(fresh), "missing": missing}


def _http(url, method="GET", data=None, timeout=5):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 — unreachable port etc.
        return f"{type(exc).__name__}"


def _unit_state(unit):
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def doctor():
    """Everything that silently breaks a cahier, checked in one pass.

    Ordered so the cheapest, most-load-bearing thing is first: if the bridge is
    down, nothing else on this list matters to anyone filling out a page.
    """
    checks = []

    def add(name, status, detail, hint=""):
        checks.append({"check": name, "status": status, "detail": detail, "hint": hint})

    code = _http(BRIDGE_URL)
    add("bridge_http", "ok" if code == 200 else "fail",
        f"GET {BRIDGE_URL} -> {code}",
        "" if code == 200 else "systemctl --user restart cahier-share.service")

    code = _http(BRIDGE_URL + "save", method="POST", data=b"{}")
    add("save_gate", "ok" if code in (400, 401, 403) else "fail",
        f"POST /save without token -> {code}",
        "" if code in (400, 401, 403) else "the /save write path is not gating as expected")

    bad_units = []
    for unit, want in UNITS_OK.items():
        got = _unit_state(unit)
        if got not in want:
            bad_units.append(f"{unit}={got} (want {'/'.join(want)})")
    add("units", "fail" if bad_units else "ok",
        "; ".join(bad_units) or f"{len(UNITS_OK)} units as expected",
        "" if not bad_units else "systemctl --user status <unit>")

    pins = template_pins()
    add("template_pins", "fail" if pins["drift"] else "ok",
        ("drift: " + ", ".join(pins["drift"])) if pins["drift"]
        else f"{pins['files']} templates pinned"
             + (f" · newly pinned: {', '.join(pins['newly_pinned'])}" if pins["newly_pinned"] else ""),
        "" if not pins["drift"] else
        "a template changed: confirm it was deliberate, then rm ~/.hermes/state/cahier-template-pins.json and re-pin")

    inbox = sorted(f for f in os.listdir(INBOX)) if os.path.isdir(INBOX) else []
    filed = set()
    if os.path.exists(ANSWERS_DB):
        try:
            con = sqlite3.connect(f"file:{ANSWERS_DB}?mode=ro", uri=True)
            filed = {r[0] for r in con.execute("SELECT source_file FROM saves")}
            con.close()
        except sqlite3.Error:
            pass
    stray = [f for f in inbox if f not in filed]
    add("inbox_parity", "warn" if stray else "ok",
        f"{len(stray)} inbox file(s) not in the answers DB" + (f": {', '.join(stray[:4])}" if stray else ""),
        "" if not stray else "the funnel did not record them — check cahier-inbox-trigger.service")

    dl = deadline_info()
    timer = _unit_state("cahier-deadline.timer")
    if dl.get("slug"):
        when = dl.get("deadline", "")
        overdue = False
        try:
            overdue = datetime.fromisoformat(when).timestamp() < time.time()
        except ValueError:
            pass
        if timer != "active":
            add("deadline", "fail", f"armed for {dl['slug']} but timer is {timer}",
                "the teardown will never fire — re-arm or disarm")
        elif overdue:
            add("deadline", "warn", f"{dl['slug']} past its deadline ({when[:16]}) and still armed",
                "cahier-deadline.service should have fired; run it by hand")
        else:
            add("deadline", "ok", f"{dl['slug']} armed to {when[:16]}")
    elif timer == "active":
        add("deadline", "warn", "timer active but no cahier-deadline.json",
            "stale timer — systemctl --user stop cahier-deadline.timer")
    else:
        add("deadline", "ok", "nothing armed")

    rows = build_rows()
    controlled = {m["served_path"] for m in scan_served().values()}
    orphans = []
    if os.path.isdir(SERVE):
        for name in sorted(os.listdir(SERVE)):
            if not name.endswith(".html") or name in INDEX_PAGES:
                continue
            if os.path.join(SERVE, name) not in controlled:
                orphans.append(name)
    add("served_orphans", "warn" if orphans else "ok",
        (f"{len(orphans)} served page(s) with no SLUG declaration: "
         + ", ".join(orphans[:4])) if orphans else "every served .html is a controlled cahier",
        "" if not orphans else "viewers/artifacts are fine here; just not spin-down-able")

    try:
        os.makedirs(RETIRED, exist_ok=True)
        retired_ok = os.access(RETIRED, os.W_OK)
    except OSError:
        retired_ok = False
    add("retired_dir", "ok" if retired_ok else "fail", RETIRED,
        "" if retired_ok else "spin down would fail — fix permissions")

    reg = load_registry()
    known = set(reg["cahiers"])
    on_disk = {r["slug"] for r in rows}
    new_slugs = sorted(on_disk - known) if known else []
    stale = sorted(s for s in known
                   if s in on_disk and reg["cahiers"][s].get("state")
                   != next(r["state"] for r in rows if r["slug"] == s))
    if not known:
        add("registry", "warn", f"{len(on_disk)} cahier(s) on disk, no registry yet",
            "cahier_ctl.py list --sync")
    else:
        add("registry", "ok" if not (new_slugs or stale) else "warn",
            f"{len(known)} entries"
            + (f" · new: {', '.join(new_slugs)}" if new_slugs else "")
            + (f" · stale state: {', '.join(stale)}" if stale else ""),
            "" if not (new_slugs or stale) else "cahier_ctl.py list --sync")

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    return {"ok": not fails, "checks": checks,
            "counts": {"ok": len(checks) - len(fails) - len(warns),
                       "warn": len(warns), "fail": len(fails)}}


# ------------------------------------------------------------------ actions
def _disarm(slug, apply, notes):
    dl = deadline_info()
    if dl.get("slug") != slug:
        return
    if not apply:
        notes.append(f"would disarm deadline timer (armed for {slug})")
        return
    try:
        subprocess.run(["systemctl", "--user", "stop", "cahier-deadline.timer"],
                       check=False, capture_output=True, timeout=15)
        os.remove(DEADLINE)
        notes.append("deadline timer disarmed")
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(f"deadline timer NOT disarmed ({exc}) — page still unpublished")


def spin_down(slug, apply=True, actor="cli", lifecycle="closed"):
    """Take the page off the bridge and file it back in its project folder.

    ``lifecycle`` is the note that outlives the move: 'closed' is the plain Stop,
    'paused' keeps the row Active with the way back one click away.
    """
    rows = {r["slug"]: r for r in build_rows()}
    row = rows.get(slug)
    if not row:
        return {"ok": False, "error": f"unknown cahier '{slug}'"}
    if slug in NEVER:
        return {"ok": False, "error": f"'{slug}' is not controllable"}
    src = row["path"]
    if not row["served"] or not src.endswith(".html") or not os.path.exists(src):
        return {"ok": False, "error": f"'{slug}' is not currently served"}
    if os.path.dirname(src) != SERVE:
        return {"ok": False, "error": f"refusing to move {src} (outside {SERVE})"}
    # Down files the page where it is SAVED (rule 2026-10-03): its own project
    # folder. RETIRED is the last resort — a home we cannot write to.
    dest_dir = cahier_home(row.get("project") or "", create=True) or RETIRED
    dest = os.path.join(dest_dir, os.path.basename(src))
    notes = []
    if os.path.exists(dest):
        dest = dest + f".prev-{int(time.time())}"
    if not apply:
        notes.append(f"would move {src} -> {dest}")
        notes.append(f"would mark it {lifecycle}")
        _disarm(slug, False, notes)
        return {"ok": True, "dry_run": True, "slug": slug, "lifecycle": lifecycle,
                "notes": notes}
    if row.get("armed") and row.get("deadline"):
        # captured BEFORE _disarm deletes the state file, so `up` can put it back
        remember_settings(slug, deadline=row["deadline"])
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.move(src, dest)
        notes.append(f"unpublished to its project folder: {dest}")
    except OSError as exc:
        return {"ok": False, "error": f"move failed: {exc}"}
    _disarm(slug, True, notes)
    remember_lifecycle(slug, lifecycle, actor=actor)
    notes.append(f"marked {lifecycle}")
    log_action({"action": "pause" if lifecycle == "paused" else "close", "slug": slug,
                "from": src, "to": dest, "home": dest_dir, "actor": actor, "result": "ok",
                "lifecycle": lifecycle, "notes": notes})
    return {"ok": True, "slug": slug, "state": "stopped", "lifecycle": lifecycle,
            "home": dest_dir, "notes": notes}


def _restore_settings(slug, snap, notes):
    """Put back what a spin down took away — the honest half of "in its original
    settings".

    The page carries its own gate (PINs live in the source, two-way is the
    template), so the file IS most of the answer. The one thing a spin down
    destroys is the deadline, and only a deadline still in the FUTURE is worth
    re-arming: restoring an expired one would tear the page down again minutes
    later, which is not what Start means.
    """
    out = {"page": "restored from its project folder",
           "gate": "from the page itself (PINs / two-way travel with the file)"}
    when = str(snap.get("deadline") or "").strip()
    if not when:
        return out
    if os.environ.get("CAHIER_CTL_NO_ARM"):
        out["deadline"] = f"not re-armed ({when[:16]}; arming disabled by CAHIER_CTL_NO_ARM)"
        return out
    try:
        due = datetime.fromisoformat(when.replace(" ", "T"))
    except ValueError:
        out["deadline"] = "not restorable: the recorded deadline is unreadable"
        return out
    if due <= datetime.now():
        out["deadline"] = f"not re-armed: {when[:16]} has already passed"
        return out
    script = os.path.join(H, "skills", "productivity", "cahier", "scripts", "arm-deadline.py")
    if not os.path.isfile(script):
        out["deadline"] = f"not re-armed: {script} is missing"
        return out
    try:
        proc = subprocess.run([sys.executable, script, "--slug", slug, "--at", when],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        out["deadline"] = f"not re-armed ({type(exc).__name__}: {exc})"
        return out
    if proc.returncode == 0:
        out["deadline"] = f"re-armed to {when[:16]}"
        notes.append(out["deadline"])
    else:
        out["deadline"] = ("not re-armed: "
                           + ((proc.stderr or proc.stdout or "arm-deadline.py failed").strip()[:120]))
    return out


def spin_up(slug, apply=True, actor="cli"):
    rows = {r["slug"]: r for r in build_rows()}
    row = rows.get(slug)
    if not row:
        return {"ok": False, "error": f"unknown cahier '{slug}'"}
    if slug in NEVER:
        return {"ok": False, "error": f"'{slug}' is not controllable"}
    if row["served"]:
        return {"ok": False, "error": f"'{slug}' is already served"}
    src = row["path"]
    if not src or not src.endswith(".html") or not os.path.exists(src):
        return {"ok": False, "error": f"no page on disk for '{slug}'"}
    snap = saved_settings(slug)
    # Its ORIGINAL file name: the page coming back must be the page that left.
    name = str(snap.get("file") or "").strip() or os.path.basename(src)
    dest = os.path.join(SERVE, name)
    notes = []
    if not apply:
        notes.append(f"would move {src} -> {dest}")
        if snap.get("deadline"):
            notes.append(f"would restore deadline {str(snap['deadline'])[:16]}")
        return {"ok": True, "dry_run": True, "slug": slug, "settings": snap, "notes": notes}
    try:
        os.makedirs(SERVE, exist_ok=True)
        shutil.move(src, dest)
        notes.append(f"published to {dest}")
    except OSError as exc:
        return {"ok": False, "error": f"move failed: {exc}"}
    restored = _restore_settings(slug, snap, notes)
    remember_lifecycle(slug, "", actor=actor)   # serving again: the pause/close note is spent
    log_action({"action": "up", "slug": slug, "from": src, "to": dest,
                "actor": actor, "result": "ok", "settings": restored, "notes": notes})
    return {"ok": True, "slug": slug, "state": "pending", "settings": restored, "notes": notes}


def spin_pause(slug, apply=True, actor="cli"):
    """Pause: off the bridge, still Active, startable again as it was."""
    return spin_down(slug, apply=apply, actor=actor, lifecycle="paused")


def spin_close(slug, apply=True, actor="cli"):
    """Close: off the bridge AND marked done.

    From a served page this is the same move as pause. From a paused one there is
    no file left to move — closing only rewrites the note, which IS the whole
    difference between the two states.
    """
    rows = {r["slug"]: r for r in build_rows()}
    row = rows.get(slug)
    if not row:
        return {"ok": False, "error": f"unknown cahier '{slug}'"}
    if slug in NEVER:
        return {"ok": False, "error": f"'{slug}' is not controllable"}
    if row["served"]:
        return spin_down(slug, apply=apply, actor=actor, lifecycle="closed")
    if (row.get("lifecycle") or "") == "paused":
        if not apply:
            return {"ok": True, "dry_run": True, "slug": slug,
                    "notes": ["would mark it done (it is already off the bridge)"]}
        remember_lifecycle(slug, "closed", actor=actor)
        notes = ["marked done (it was already off the bridge)"]
        log_action({"action": "close", "slug": slug, "from": row.get("path") or "", "to": "",
                    "actor": actor, "result": "ok", "lifecycle": "closed", "notes": notes})
        return {"ok": True, "slug": slug, "lifecycle": "closed", "notes": notes}
    return {"ok": False, "error": f"'{slug}' is not on the bridge — nothing to close"}


def spin(slug, action, apply=True, actor="cli"):
    """One door for the panel: 'up' | 'pause' | 'close' on one slug. 'down' is kept
    as an alias of close so an older caller keeps working. Never guesses."""
    what = str(action or "").strip().lower()
    if what == "up":
        return spin_up(slug, apply=apply, actor=actor)
    if what == "pause":
        return spin_pause(slug, apply=apply, actor=actor)
    if what in ("close", "down"):
        return spin_close(slug, apply=apply, actor=actor)
    return {"ok": False, "error": f"unknown action {action!r} (want 'up', 'pause' or 'close')"}


SERVED_CAHIER_PATHS = set()


def main():
    ap = argparse.ArgumentParser(description="Cahier control plane")
    ap.add_argument("cmd", choices=["list", "up", "pause", "close", "down", "extras", "doctor",
                                    "registry", "filing", "pins"])
    ap.add_argument("--slug")
    ap.add_argument("--repin", action="store_true",
                    help="pins: accept the current template hashes as the new truth (deliberate edit)")
    ap.add_argument("--apply", action="store_true", help="actually do it (default: dry run)")
    ap.add_argument("--profile", help="filing: the Hermes profile that owns this cahier ('' forgets it)")
    ap.add_argument("--project", help="filing: the project this cahier belongs to ('' forgets it)")
    ap.add_argument("--clear", action="store_true", help="filing: drop every override for this slug")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--scope",
                    help="list: only these buckets (comma separated, e.g. serving,paused,"
                         " or a state like live,pending)")
    ap.add_argument("--sync", action="store_true", help="list: merge the filesystem view into the registry")
    args = ap.parse_args()

    global SERVED_CAHIER_PATHS
    SERVED_CAHIER_PATHS = {m["served_path"] for m in scan_served().values()}

    if args.cmd == "list":
        res = iteration(scope=args.scope or "all")
        if args.sync:
            synced = sync_registry(build_rows(), note="list --sync")
            if not args.json:
                print(f"registry: +{len(synced['added'])} new, "
                      f"{len(synced['changed'])} state change(s), {synced['total']} tracked")
            res = iteration(scope=args.scope or "all")
        if args.json:
            print(json.dumps(res, indent=1))
        else:
            print(f"{'slug':<30} {'lifecycle':<10} {'state':<9} {'served':<7} saves  why")
            for r in res["rows"]:
                print(f"{r['slug']:<30} {(r.get('lifecycle') or '—'):<10} {r['state']:<9} "
                      f"{str(r['served']):<7} {r['saves']:<6} {r['why']}")
            c, life = res["counts"], res["counts"].get("lifecycle") or {}
            print(f"\n{res['shown']} shown ({res['scope']}) of {c['total']} · "
                  f"{life.get('serving', 0)} serving · {life.get('paused', 0)} paused · "
                  f"{life.get('closed', 0)} closed")
            print(f"  states: {c['live']} live · {c['pending']} pending · "
                  f"{c['finished']} finished · {c['stopped']} stopped")
            for w in res["integrity"]["warnings"]:
                print(f"WARN {w}")
        return 0

    if args.cmd == "filing":
        # Bare `filing` = what every cahier resolves to, and who answered. With a
        # slug it writes the human's override (the only write in this command).
        if not args.slug:
            res = {r["slug"]: {k: r[k] for k in
                               ("profile", "project", "profile_source", "project_source")}
                   for r in build_rows()}
            if args.json:
                print(json.dumps(res, indent=1))
            else:
                print(f"{'slug':<30} {'profile':<12} {'project':<20} source (profile/project)")
                for slug in sorted(res):
                    r = res[slug]
                    print(f"{slug:<30} {r['profile'] or '—':<12} {r['project'] or '—':<20} "
                          f"{r['profile_source']}/{r['project_source']}")
            return 0
        res = set_filing(args.slug, profile=args.profile, project=args.project,
                         clear=args.clear, actor="cli")
        if args.json:
            print(json.dumps(res, indent=1))
        elif res.get("ok"):
            print(f"filing {res['slug']}: {res['after'] or 'no override (resolves automatically)'}")
            print(f"  file: {res['file']}")
        else:
            print(res.get("error"), file=sys.stderr)
        return 0 if res.get("ok") else 2

    if args.cmd == "pins":
        # Templates are pinned by hash; a deliberate edit means a deliberate re-pin.
        res = template_pins()
        if args.repin and res["drift"]:
            pins = read_json(PINS, {})
            for name in res["drift"]:
                pins[name] = _sha256(os.path.join(TEMPLATES, name))
            write_json(PINS, pins)
            log_action({"actor": "cli", "action": "repin", "templates": res["drift"]})
            res = template_pins()
            res["repinned"] = True
        if args.json:
            print(json.dumps(res, indent=1))
        else:
            print(f"templates: {res['files']} pinned · drift: {', '.join(res['drift']) or 'none'}"
                  + (" (re-pinned)" if res.get("repinned") else ""))
        return 0 if res["ok"] else 1

    if args.cmd == "registry":
        reg = load_registry()
        book = reg["cahiers"]
        if args.json:
            print(json.dumps(reg, indent=1))
        elif not book:
            print("empty — run: cahier_ctl.py list --sync")
        else:
            print(f"registry · {len(book)} cahier(s) · updated {reg.get('updated', '?')}")
            print(f"{'slug':<30} {'state':<9} {'first seen':<11} {'last seen':<11} pin saves")
            for slug, e in sorted(book.items()):
                pin = "yes" if (e.get("pin") or {}).get("protected") else "no"
                print(f"{slug:<30} {str(e.get('state')):<9} "
                      f"{str(e.get('first_seen'))[:10]:<11} {str(e.get('last_seen'))[:10]:<11} "
                      f"{pin:<3} {e.get('saves', 0)}")
        return 0

    if args.cmd == "doctor":
        res = doctor()
        if args.json:
            print(json.dumps(res, indent=1))
        else:
            mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
            for c in res["checks"]:
                print(f"[{mark[c['status']]}] {c['check']:<16} {c['detail']}")
                if c["status"] != "ok" and c["hint"]:
                    print(f"{'':<7}-> {c['hint']}")
            n = res["counts"]
            print(f"\n{n['ok']} ok · {n['warn']} warn · {n['fail']} fail")
        return 0 if res["ok"] else 1

    if args.cmd == "extras":
        for n in served_extras():
            print(n)
        return 0

    if not args.slug:
        print("--slug is required for up/pause/close", file=sys.stderr)
        return 2
    verbs = {"up": spin_up, "pause": spin_pause, "close": spin_close, "down": spin_down}
    res = verbs[args.cmd](args.slug, args.apply)
    # Only a real transition is worth recording; a dry run touches nothing.
    if res.get("ok") and args.apply:
        try:
            sync_registry(build_rows(), note=f"{args.cmd} {args.slug}")
        except Exception as exc:  # noqa: BLE001 — the action already succeeded
            res["registry_warning"] = str(exc)
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
