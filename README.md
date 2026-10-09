# Cahier Hub

Every cahier in one place, reachable from the desktop sidebar.

A cahier is a question the agent asks a human — a served HTML page (or a chat
question) whose answers land in an inbox and a SQLite table. `Cahier Hub` is the
index: one sidebar row → `/cahiers` → every live and pending cahier with its
saves, deadline, PIN state and served link, grouped by **profile ▸ project**.
Read one **here** (framed in the workspace pane, browser one click away), copy
its link, filter by scope, or say which profile/project a cahier belongs to.

**Reads are read-only by construction.** The page renders whatever
`cahier_ctl.iteration()` returns — the same call the CLI's `list` uses — so the
page and the terminal can never disagree about what a cahier *is*. Grouping only
buckets that order; it never re-sorts or re-counts it. Zero API keys, zero model
tokens.

**Three writes exist, and a human makes each.** The ✎ on a row POSTs `/filing` to
record the profile and project behind a cahier; the buttons under each row's
status POST `/action` to **spin it up**, **pause it** or **close it** — all go
through the control plane (`cahier_ctl.set_filing`, `cahier_ctl.spin`),
slug-validated, atomic, and audit-lined in `cahier-control.log`. Nothing else on
the page writes anywhere.

**One row, three moves.** A row's badge is its lifecycle — `serving` (on the
bridge), `paused` (off the bridge on purpose, still active and startable) or
`closed` (done) — and the buttons under it are exactly the moves that state
allows: serving → *Pause* + *Close*, paused → *Spin up* + *Close*, closed →
*Spin up*. Pause and close are the same file move; only the registry note tells
them apart, which is why a paused row is one click from being served again while
a closed one is finished. *Close* asks once (it ends the row's life); *Spin up*
and *Pause* are one click, and every refusal is a 4xx with a reason and nothing
moved. The phase (`live` / `pending` / `finished`) reads on the left of the row,
so a badge never has to mean two things.

## What's in here

| Path | Role |
|---|---|
| `plugin.yaml` | Plugin manifest: `config_schema` (the Desktop settings form), version, tags |
| `__init__.py` | Agent-side stub — registers no tools/hooks by design |
| `dashboard/manifest.json` | Dashboard half: label, icon, API file, hidden tab |
| `dashboard/plugin_api.py` | JSON backend (reads + the two human writes), mounted at `/api/plugins/cahier-hub/` |
| `desktop/plugin.js` | Sidebar row + `/cahiers` page: grouping, in-window viewer, ✎ filing (runs inside the Electron app) |
| `lib/cahier_ctl.py` | Bundled control plane, so a fresh install has one before it has its own |
| `scripts/doctor.py` | Prints the install checklist (`GET /doctor` from a terminal) |
| `tests/` | Backend contract suites + panel render harness |

## Endpoints

| Route | Returns |
|---|---|
| `GET /health` | Control-plane load status and which copy, settings + the layer each came from, bridge port, counts |
| `GET /doctor` | The install checklist: `ok`/`warn`/`fail` per check, each fail carrying its own fix |
| `GET /list?scope=active\|paused\|finished\|all` | Rows + counts + integrity warnings + the folder vocabulary (what the page fetches) |
| `GET /cahier?slug=<slug>` | One cahier, plus its latest saves |
| `POST /filing` | Records `{slug, profile, project}` (empty string = forget). 404 on an unknown slug, 400 on bad input, 503 when the control plane is missing |
| `POST /action` | `{slug, action: "up"\|"pause"\|"close"}` — spin a cahier up, pause it (off the bridge, still active), or close it (off the bridge and done; `down` is kept as an alias of close). Same refusals, same 503 |

Every route sits behind the dashboard's own auth, so a bare `curl` from the host
gets `401`; the app's `ctx.rest` carries the session token.

`active` = on the bridge right now (`serving`). `paused` = off the bridge on
purpose (`paused`). `finished` = done or never published (`finished`, `stopped`,
`closed`, `unknown`). `all` = everything. Unknown scopes fall back to `all`
rather than erroring — a stale bookmark should never show a broken page.

The two vocabularies are deliberately separate: **lifecycle** is what the
buttons act on and what the badge says, **state** is the phase the control plane
derives from the deadline and the answer table (`live` = deadline armed,
`pending` = nobody has answered, `finished` = answered, `stopped` = parked). A
served page that already has answers is `finished` by state and `serving` by
lifecycle — both true, and no longer one word asked to mean both.

## Configuration

Settings are read from four layers, most specific first. `GET /doctor` names the
layer every value came from.

| Layer | How | Use it for |
|---|---|---|
| `CAHIER_HUB_<KEY>` environment variables | `CAHIER_HUB_BRIDGE_PORT=9000` | ops, containers, systemd |
| `plugins.entries.cahier-hub.settings` in `$HERMES_HOME/config.yaml` | the **Desktop → Capabilities → Plugins** settings form, driven by `config_schema` | anyone using the app |
| `$HERMES_HOME/cahier-hub.json` | written by the agent when it asks the onboarding questions in a session chat | the first run, conversationally |
| built-in defaults | this repo | everything else |

The keys (also in `plugin.yaml`): `bridge_port`, `base_url`, `cache_ttl`,
`control_plane`, `groups_file`, `share_dir`, `projects_root`, `hosting_mode`,
`timezone`, `notify_channel`, `notify_target`, `notify_on`, `quiet_hours`,
`pin_enabled`, `users`.

### The onboarding questions

Asked **in the session chat**, by the agent, at the moment it decides to create a
cahier — once, and skipped entirely for a plain local viewer. One at a time, with
the default named.

| # | Question | Setting | Default |
|---|---|---|---|
| 1 | Who's answering, by name? | `users` | just you |
| 2 | Where will they open it — this machine, the same network, or anywhere? | `hosting_mode` | `lan` |
| 3 | What link will you send them? | `base_url` | derived from the request host |
| 4 | Where should submitted answers land? | `notify_channel`, `notify_target` | `none` |
| 5 | What should that tell you about? | `notify_on` | `new_answer` |
| 6 | Any quiet hours? | `quiet_hours` | none |
| 7 | Which timezone are the deadlines in? | `timezone` | the host's |

The PIN is **derived from #2**, never asked as its own question: `public` means a
PIN is mandatory, `lan` means ask once, `local` means no PIN, no bridge, no
notifications.

Question 3 exists because a link built from `127.0.0.1` works for the operator and
is dead for everybody else. `GET /doctor` fails that combination outright when
`hosting_mode` is not `local`.

## Where a cahier's profile/project comes from

Grouping needs two labels per cahier and refuses to invent them. Each answer
carries its provenance (`profile_source` / `project_source`), resolved strongest
first — the same ladder lives in `cahier_ctl.filing()`:

| Source | Meaning |
|---|---|
| `override` | a human said so: the panel's ✎ or `cahier_ctl.py filing --slug … --profile … --project …` |
| `declared` | stamped into the page by `cahier.py build` (`CAHIER_PROFILE` / `CAHIER_PROJECT`) |
| `session` | the session that built it → `profile_name` in `$HERMES_HOME/state.db` |
| `derived` | the project's own name appears in the slug (`acme-restructure` → ACME). Slug only — titles are prose |
| `none` | nobody knows; the panel shows an empty chip |

Overrides live in one hand-editable file,
`$HERMES_HOME/state/cahier-groups.json` (`CAHIER_HUB_GROUPS` re-points it, which
is how the tests stay hermetic):

```json
{"version": 1, "updated_by": "panel", "cahiers": {"rolodex-merges": {"profile": "default", "project": "rolodex"}}}
```

## The control plane

`dashboard/plugin_api.py` does not read the registry itself: it iterates through
`cahier_ctl.iteration()`, the same call the CLI uses, so the panel and the
terminal cannot drift apart. It looks for that module in this order:

1. `control_plane` (a configured path),
2. `$HERMES_HOME/scripts/cahier_ctl.py` — a real installation's own copy, and the
   one its CLI and cron jobs are already running,
3. `lib/cahier_ctl.py` — the copy bundled here, so a fresh clone works with
   nothing else set up.

`GET /doctor` reports which one is live and whether the local and bundled copies
have drifted (expected after a local edit — the local one wins). When
`HERMES_HOME` is not `~/.hermes`, the control plane's own paths are re-pointed at
it, because it hardcodes `~/.hermes` otherwise.

## Install

Backend half:

```bash
hermes plugins install cahier-hub          # or: clone into $HERMES_HOME/plugins/
hermes plugins enable cahier-hub           # adds it to plugins.enabled
hermes plugins doctor cahier-hub           # runtime discovery + import contract
# restart hermes-serve so the route mounts
```

Desktop half — the app runs its own `plugin.js` from its own home, so the file
has to land there:

```
$HERMES_HOME/desktop-plugins/cahier-hub/plugin.js
```

then **Rescan** in the app's plugin settings. (On a local app this is what
`materializeDesktopHalf` copies for you; across a remote backend it is a one-file
drop.)

Check it:

```bash
python3 scripts/doctor.py            # or: GET /api/plugins/cahier-hub/doctor
```

## Verify

```bash
./tests/run.sh
```

Runs the control-plane compile, the doctor, both backend suites (pytest, real
FastAPI app + real router) and the panel harness (Node, stubbed SDK, real
payload):

1. **Contract suite** — health, scope equality with `cahier_ctl.iteration()`, no
   dropped/double slugs, stable order across calls, and a read-only proof that
   the hashes of `cahier-registry.json` and `cahier-answers.db` are unchanged by
   a request, plus **cross-implementation parity**: the panel's slug set must
   equal `GET /fleet/cahiers` on the bridge exactly — a slug visible in one and
   not the other is a cahier the user either can't see or can't open. That half
   skips cleanly when `:8766` isn't listening (override with
   `CAHIER_HUB_BRIDGE=`), so a stopped optional service never turns the suite
   red. The filing tests write to a `tmp_path` override file: the write lands
   there and nowhere else, an unknown slug / traversal / oversized label is
   refused with nothing left behind, a GET never touches the file, and an
   override always beats every automatic answer.
2. **Configuration and doctor suite** — a throwaway `HERMES_HOME` with nothing
   in it, proving the layer precedence (env > `config.yaml` > chat answers >
   defaults), that another plugin's settings block cannot bleed in, that the
   bundled control plane loads when no local one exists and the local one wins
   when it does, that `HERMES_HOME` re-points the state paths, and that the
   doctor fails a loopback `base_url` on a `lan` deployment.
3. **Panel harness** — `register()` wires one sidebar row and one route, the page
   renders every cahier the backend sent exactly once, grouping buckets those
   rows by profile ▸ project (and Flat drops the headers) without losing any,
   "Open" frames the served URL in-window, "Browser" hands the same URL to the OS,
   "Copy link" writes it to the clipboard, and the ✎ POSTs `/filing` — the only
   non-read call the page makes.

The harness runs against `scope=active` (what the page opens with) and
`scope=all` (empty-scope edge), so an empty or degenerate payload fails loudly.

## Disclosure

What a user should know before installing (admission-policy terms):

- **Network.** No outbound calls. The dashboard half answers on hermes-serve's
  own origin (`/api/plugins/cahier-hub/*`) and reads local files. A bridge, if
  you run one, listens on your LAN so answerers can open a cahier — that is the
  only listener. No telemetry, no analytics, no update pings, no third party.
- **Reads outside its own data.** The cahier files and the profile/project
  filings in the folders you configure (`share_dir`, `projects_root`,
  `groups_file`), plus the control plane at `$HERMES_HOME/scripts/cahier_ctl.py`
  or the copy bundled here. It reads no credentials and no other application's
  files or token stores.
- **Writes.** Inside `$HERMES_HOME` (its own state) and the folders you
  configure. It never writes into Hermes core files or another plugin's
  directory.
- **Shell.** It spawns one local Python process — the control plane
  (`cahier_ctl.py`), the same call the CLI's `list` makes. Nothing else, and no
  `--yolo`/non-interactive flags are propagated to children.
- **Background processes.** None. The bridge is a service you run yourself; the
  plugin neither bundles nor starts it.
- **Credentials.** None required: zero API keys, zero model tokens, empty
  `requires_env`. A cahier PIN, when you enable one, lives inside the page you
  serve.
- **What it needs first.** The `cahier` skill to build and read cahiers. The
  plugin bundles a copy of the control plane so it can read without the skill,
  but reading a cahier you cannot build is not much use — install the skill too.

## Failure behaviour

`cahier_ctl.py` missing or broken → the API degrades to the registry file alone
and says so in `integrity.warnings`; the page shows a warning strip instead of an
empty list. A half-read is never silently presented as a complete one.

Integrity warnings are the panel telling on itself: inbox files that were never
ingested, served pages with no `SLUG` declaration (viewers and artifacts are
fine here — they are just not spin-down-able), registry rows whose state no
longer matches disk, and duplicate slugs. They appear in the page's warning
strip, never hidden.
