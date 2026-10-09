/**
 * Cahier Hub — the sidebar door to every cahier.
 *
 * Sidebar half of the `cahier-hub` plugin. Its Python half lives beside it at
 * `dashboard/plugin_api.py` and hermes-serve mounts it at
 * /api/plugins/cahier-hub/* — exactly the prefix `ctx.rest` is scoped to, so
 * every path below is RELATIVE to that ('/list', not the full URL).
 *
 *   ~/.hermes/plugins/cahier-hub/
 *   ├── dashboard/{manifest.json,plugin_api.py}   ← backend routes
 *   └── desktop/plugin.js                         ← this file
 *
 * The desktop half is LOCAL to the machine running the Electron app: when the
 * app points at a remote backend, drop this file into
 *   Windows : %USERPROFILE%\.hermes\desktop-plugins\cahier-hub\plugin.js
 *   Linux   : ~/.hermes/desktop-plugins/cahier-hub/plugin.js
 * then Rescan (a folder holding plugin.js with no package marker is treated as
 * hand-installed and never overwritten). Hot-loads in seconds — no rebuild.
 *
 * Three writes exist, and every one of them is the human's click: `Open` reads a
 * cahier HERE, in an iframe filling the workspace pane (the browser stays one
 * click away); the ✎ on a row records which profile/project that cahier belongs
 * to; and the buttons under a row's status move it. Spin up serves the page again
 * from its project folder, in its ORIGINAL settings (2026-10-03): the same file,
 * the same gate, the same deadline while that deadline has not passed. Pause and
 * Close are the same file move — off the bridge — with a different registry note:
 * pause stays Active and startable, close is done. Which of the three a row offers
 * is the backend's call (row.actions); the badge above them says which state the
 * row is in (serving / paused / closed), and the phase reads on the left.
 * Everything else is a GET. The list order is the backend's — it is sorted
 * deterministically in cahier_ctl.iteration() and is NOT re-sorted here, so the
 * panel and `cahier_ctl.py list` read identically; grouping only buckets that
 * order.
 */

import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  ErrorState,
  Input,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  ScrollArea,
  Separator,
  Skeleton,
  cn,
  icons,
  useQuery,
  useTheme
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useMemo, useState } from 'react'

const ROUTE = '/cahiers'

/** The desktop app hands us its own REST + native bridge at register() time. */
let rest = null
let osApi = null
let storage = null

/* ------------------------------------------------------------------ labels */

const STATE_LABEL = {
  live: 'live',
  pending: 'pending',
  finished: 'finished',
  stopped: 'stopped',
  unknown: 'unknown'
}

/** live reads as "armed/answering"; pending as "nobody has answered yet". */
const STATE_VARIANT = {
  live: 'default',
  pending: 'muted',
  finished: 'muted',
  stopped: 'destructive',
  unknown: 'muted'
}

/** STATE is the phase ("how is it doing"); LIFECYCLE is what the buttons act on
 *  ("on the bridge / parked on purpose / done"). A page that was never published
 *  has no lifecycle, so it keeps showing its phase. */
const LIFECYCLE_LABEL = {
  serving: 'serving',
  paused: 'paused',
  closed: 'closed'
}

const LIFECYCLE_VARIANT = {
  serving: 'default',
  paused: 'muted',
  closed: 'muted'
}

/** The badge a row shows: its lifecycle when it has one, else its phase. */
function statusLabel(row) {
  return (row.lifecycle ? LIFECYCLE_LABEL[row.lifecycle] : STATE_LABEL[row.state]) || row.state
}

function statusVariant(row) {
  return (row.lifecycle ? LIFECYCLE_VARIANT[row.lifecycle] : STATE_VARIANT[row.state]) || 'muted'
}

const SCOPES = [
  { id: 'active', label: 'Active' },
  { id: 'paused', label: 'Paused' },
  { id: 'finished', label: 'Finished' },
  { id: 'all', label: 'All' }
]

/** How the rows are bucketed. Profile ▸ project is the default: it answers
 *  "what did THIS agent work on" before "which cahier was that". */
const GROUPINGS = [
  { id: 'profile', label: 'Profile ▸ Project' },
  { id: 'project', label: 'Project' },
  { id: 'none', label: 'Flat' }
]

const NO_PROFILE = '(no profile)'
const NO_PROJECT = '(no project)'

/** Where a filing answer came from — shown on hover, never invented. */
const SOURCE_HINT = {
  override: 'you set this',
  declared: 'stamped into the page at build time',
  session: 'the session that built it (state.db)',
  derived: 'matched from the slug',
  none: 'nobody has said yet'
}

/* ------------------------------------------------------------------ helpers */

function clock(value) {
  const s = String(value || '')
  if (!s) return ''
  return s.length >= 16 ? `${s.slice(0, 10)} ${s.slice(11, 16)}` : s
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`
}

/** Bucket rows into profile ▸ project, or project alone. Rows keep the backend's
 *  order inside each bucket, so a group is a window onto the same list — never a
 *  second opinion about it. */
function groupRows(rows, mode) {
  if (mode === 'none') return []
  const groups = new Map()
  for (const row of rows) {
    const main = mode === 'profile' ? row.profile || NO_PROFILE : row.project || NO_PROJECT
    const sub = mode === 'profile' ? row.project || NO_PROJECT : ''
    if (!groups.has(main)) groups.set(main, new Map())
    const subs = groups.get(main)
    if (!subs.has(sub)) subs.set(sub, [])
    subs.get(sub).push(row)
  }
  return [...groups].map(([label, subs]) => ({
    id: `g:${label}`,
    label,
    total: [...subs.values()].reduce((n, list) => n + list.length, 0),
    live: [...subs.values()].reduce(
      (n, list) => n + list.filter(r => r.state === 'live').length, 0),
    subs: [...subs].map(([slabel, list]) => ({
      id: `s:${label}:${slabel}`,
      label: slabel,
      rows: list
    }))
  }))
}

/** The served URL, copied without a dialog. Copy is silent+local on purpose:
 *  the bridge serves GET without a token, so the link IS the access story. */
function CopyLink({ url }) {
  const [done, setDone] = useState(false)
  if (!url) return null
  return jsx(Button, {
    variant: 'ghost',
    size: 'xs',
    title: url,
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(url)
        setDone(true)
        setTimeout(() => setDone(false), 1500)
      } catch {
        setDone(false)
      }
    },
    children: done ? 'Copied' : 'Copy link'
  })
}

function ScopePill({ active, onClick, children }) {
  return jsx('button', {
    type: 'button',
    onClick,
    className: cn(
      'rounded-full border px-2.5 py-0.5 text-[0.6875rem] leading-5 transition-colors',
      active
        ? 'border-(--ui-accent) text-(--ui-accent)'
        : 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
    ),
    children
  })
}

/** profile › project, with the provenance of each answer on hover. Unknown reads
 *  as unknown — an empty chip is more honest than a plausible guess. */
function FilingTag({ row }) {
  const chip = (value, source, fallback) =>
    jsx('span', {
      title: `${value || fallback} — ${SOURCE_HINT[source] || 'unknown source'}`,
      className: cn(
        'rounded-full border px-1.5 py-px font-mono text-[0.625rem] leading-4',
        value
          ? 'border-(--ui-stroke-secondary) text-(--ui-text-secondary)'
          : 'border-(--ui-stroke-secondary) text-(--ui-text-tertiary) italic'
      ),
      children: value || fallback
    })

  return jsxs('div', {
    className: 'flex items-center gap-1',
    'data-filing': `${row.profile || ''}|${row.project || ''}`,
    children: [
      chip(row.profile, row.profile_source, 'no profile'),
      jsx(Codicon, { name: 'chevron-right', size: '0.7rem', className: 'text-(--ui-text-tertiary)' }),
      chip(row.project, row.project_source, 'no project')
    ]
  })
}

/** Inline editor for one row's filing. The panel's only write: it POSTs the
 *  human's answer to /filing, which validates + logs it in cahier_ctl. */
function FilingEditor({ row, vocab, onSaved, onCancel }) {
  const [profile, setProfile] = useState(row.profile || '')
  const [project, setProject] = useState(row.project || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const save = async (clear) => {
    setBusy(true)
    setError('')
    try {
      await rest('/filing', {
        method: 'POST',
        body: { slug: row.slug, profile, project, clear: Boolean(clear) }
      })
      setBusy(false)
      onSaved()
    } catch (e) {
      setBusy(false)
      setError(String((e && e.message) || e))
    }
  }

  const hint = vocab
    ? `e.g. ${(vocab.profiles || []).slice(0, 4).join(', ') || 'default'}`
    : ''

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 rounded-(--ui-radius-md) border border-(--ui-accent) px-2 py-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Input, {
            value: profile,
            onChange: e => setProfile(e.target.value),
            placeholder: 'profile',
            'aria-label': `Profile for ${row.slug}`
          }),
          jsx(Input, {
            value: project,
            onChange: e => setProject(e.target.value),
            placeholder: 'project',
            'aria-label': `Project for ${row.slug}`
          })
        ]
      }),
      hint
        ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: hint })
        : null,
      error
        ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-tertiary)', children: error })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Button, {
            variant: 'secondary',
            size: 'xs',
            disabled: busy,
            onClick: () => save(false),
            children: busy ? 'Saving…' : 'Save'
          }),
          jsx(Button, {
            variant: 'ghost',
            size: 'xs',
            disabled: busy,
            title: 'Drop the override and let Cahier Hub resolve it again',
            onClick: () => save(true),
            children: 'Clear'
          }),
          jsx(Button, { variant: 'ghost', size: 'xs', disabled: busy, onClick: onCancel, children: 'Cancel' })
        ]
      })
    ]
  })
}

/* Every move a row allows, right where its status is. The backend decides which
 * ones those are (row.actions): a served cahier can be paused or closed, a paused
 * one spun up or closed, anything else spun up. Up = serve it again from its
 * project folder in its original settings — the same file, the same PIN gate (it
 * travels inside the page) and the same deadline while that deadline is still
 * ahead. Pause = off the bridge but still active, one click from coming back.
 * Close = off the bridge and done, so it drops to Finished. Close is the one move
 * that both changes what OTHERS can reach and ends the row's life, so it asks
 * once; pause is reversible and does not. */

const ACTION_LABEL = { up: 'Spin up', pause: 'Pause', close: 'Close' }
const ACTION_BUSY = { up: 'Spinning up…', pause: 'Pausing…', close: 'Closing…' }
const ACTION_HINT = {
  up: 'Serve it again, in its original settings',
  pause: 'Off the bridge, still active — spin it up again any time',
  close: 'Off the bridge and done — the row moves to Finished'
}

/** The legal moves for one row: the backend's list, with the older single
 *  `action` kept as the fallback. `down` is the old name for close. */
function rowActions(row) {
  if (!row.controllable) return []
  const list = Array.isArray(row.actions) && row.actions.length
    ? row.actions
    : (row.action === 'up' || row.action === 'down') ? [row.action] : []
  return list.map(a => (a === 'down' ? 'close' : a)).filter(a => ACTION_LABEL[a])
}

function ActionButtons({ row, onDone }) {
  const [busy, setBusy] = useState('')
  const [confirming, setConfirming] = useState('')
  const [error, setError] = useState('')
  const actions = rowActions(row)
  if (!actions.length) return null

  const send = async action => {
    setBusy(action)
    setError('')
    try {
      await rest('/action', { method: 'POST', body: { slug: row.slug, action } })
      setBusy('')
      setConfirming('')
      if (onDone) onDone()
    } catch (e) {
      setBusy('')
      setConfirming('')
      setError(String((e && e.message) || e))
    }
  }

  return jsxs('div', {
    className: 'flex flex-col items-end gap-0.5',
    children: [
      jsx('div', {
        className: 'flex items-center gap-1',
        children: actions.map(action =>
          jsx(Button, {
            key: action,
            variant: action === actions[0] ? 'secondary' : 'ghost',
            size: 'xs',
            disabled: Boolean(busy),
            title: ACTION_HINT[action],
            'aria-label': `${ACTION_LABEL[action]} ${row.slug}`,
            'data-action-slug': row.slug,
            'data-action-kind': action,
            onClick: () =>
              action === 'close' && confirming !== action ? setConfirming(action) : send(action),
            children: busy === action
              ? ACTION_BUSY[action]
              : confirming === action
                ? 'Confirm close'
                : ACTION_LABEL[action]
          }, action)
        )
      }),
      confirming && !busy
        ? jsx(Button, {
            variant: 'ghost',
            size: 'xs',
            onClick: () => setConfirming(''),
            children: 'Cancel'
          })
        : null,
      error
        ? jsx('span', {
            className: 'max-w-[9rem] text-right text-[0.625rem] text-(--ui-text-tertiary)',
            children: error
          })
        : null
    ]
  })
}

function CahierRow({ row, vocab, onOpen, onSaved }) {
  const [editing, setEditing] = useState(false)
  const meta = [
    // A lifecycle badge says serving/paused/closed, which is the wrong place for
    // "how is it doing" — so the phase reads here whenever the badge is not it.
    row.lifecycle ? STATE_LABEL[row.state] || row.state : null,
    plural(row.saves, 'save', 'saves'),
    row.people ? plural(row.people, 'contributor', 'contributors') : null,
    row.last_save ? `last ${clock(row.last_save)}` : null,
    row.armed && row.deadline ? `deadline ${clock(row.deadline)}` : null
  ].filter(Boolean)

  return jsxs('div', {
    'data-slug': row.slug,
    className: cn(
      'flex items-start gap-3 rounded-(--ui-radius-md) border px-3 py-2',
      row.state === 'live'
        ? 'border-(--ui-accent)'
        : 'border-(--ui-stroke-secondary)'
    ),
    children: [
      jsxs('div', {
        className: 'flex min-w-0 flex-1 flex-col gap-1.5',
        children: [
          jsxs('div', {
            className: 'flex min-w-0 items-center gap-2',
            children: [
              jsx('span', {
                className: 'truncate text-[0.8125rem] font-medium text-(--ui-text-primary)',
                children: row.title || row.slug
              }),
              row.protected
                ? jsx(Codicon, {
                    name: 'lock',
                    size: '0.8rem',
                    className: 'text-(--ui-text-tertiary)',
                    title: row.pin_users
                      ? `PIN-gated · ${plural(row.pin_users, 'user', 'users')}`
                      : 'PIN-gated'
                  })
                : null,
              jsx('span', {
                className: 'ml-auto truncate font-mono text-[0.6875rem] text-(--ui-text-tertiary)',
                children: row.slug
              })
            ]
          }),
          jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-secondary)',
            children: meta.join(' · ') || 'no activity yet'
          }),
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(FilingTag, { row }),
              jsx(Button, {
                variant: 'ghost',
                size: 'xs',
                title: 'Say which profile/project this cahier belongs to',
                'aria-label': `File ${row.slug}`,
                'data-edit': row.slug,
                onClick: () => setEditing(v => !v),
                children: jsx(Codicon, { name: 'edit', size: '0.75rem' })
              })
            ]
          }),
          row.why
            ? jsx('div', {
                className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                children: row.why
              })
            : null,
          editing
            ? jsx(FilingEditor, {
                row,
                vocab,
                onSaved: () => {
                  setEditing(false)
                  if (onSaved) onSaved()
                },
                onCancel: () => setEditing(false)
              })
            : null,
          jsxs('div', {
            className: 'flex items-center gap-2 pt-0.5',
            children: [
              row.url
                ? jsx(Button, {
                    variant: 'secondary',
                    size: 'xs',
                    title: 'Read it here, in this window',
                    onClick: () => onOpen(row),
                    children: [
                      jsx(Codicon, { name: 'open-preview', size: '0.8rem' }),
                      jsx('span', { children: 'Open' })
                    ]
                  })
                : null,
              row.url
                ? jsx(Button, {
                    variant: 'ghost',
                    size: 'xs',
                    title: 'Open in your browser instead',
                    onClick: () => osApi && osApi.openExternal(row.url),
                    children: [
                      jsx(Codicon, { name: 'link-external', size: '0.8rem' }),
                      jsx('span', { children: 'Browser' })
                    ]
                  })
                : null,
              jsx(CopyLink, { url: row.url }),
              row.file
                ? jsx('span', {
                    className: 'truncate font-mono text-[0.625rem] text-(--ui-text-tertiary)',
                    title: row.home_path
                      ? `saved in ${row.home_path}`
                      : row.path,
                    children: row.file
                  })
                : null
            ]
          })
        ]
      }),
      /* Right-hand side of the row: the state reads first, the button that
         changes it sits directly underneath — the two things a human came for. */
      jsxs('div', {
        'data-row-status': row.state,
        'data-row-lifecycle': row.lifecycle || '',
        className: 'flex shrink-0 flex-col items-end gap-1.5',
        children: [
          jsx(Badge, {
            variant: statusVariant(row),
            size: 'xs',
            title: row.lifecycle
              ? `${row.lifecycle} · ${STATE_LABEL[row.state] || row.state}`
              : undefined,
            children: statusLabel(row)
          }),
          jsx(ActionButtons, { row, onDone: onSaved })
        ]
      })
    ]
  })
}

/** One collapsible group: a profile (with project sub-groups) or a project. */
function Group({ group, mode, collapsed, onToggle, renderRow }) {
  const open = !collapsed[group.id]
  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('button', {
        type: 'button',
        onClick: () => onToggle(group.id),
        'data-group': group.id,
        className: 'flex items-center gap-1.5 rounded-(--ui-radius-md) px-1 py-0.5 text-left hover:bg-(--ui-stroke-secondary)/40',
        children: [
          jsx(Codicon, { name: open ? 'chevron-down' : 'chevron-right', size: '0.8rem', className: 'text-(--ui-text-tertiary)' }),
          jsx('span', {
            className: 'text-[0.75rem] font-medium text-(--ui-text-primary)',
            children: group.label
          }),
          jsx('span', {
            className: 'text-[0.625rem] text-(--ui-text-tertiary)',
            children: group.live
              ? `${plural(group.total, 'cahier', 'cahiers')} · ${group.live} live`
              : plural(group.total, 'cahier', 'cahiers')
          })
        ]
      }),
      open
        ? jsx('div', {
            className: 'flex flex-col gap-2 pl-3.5',
            children: group.subs.map(sub =>
              mode === 'profile' && sub.label
                ? jsxs('div', {
                    key: sub.id,
                    className: 'flex flex-col gap-1.5',
                    children: [
                      jsxs('button', {
                        type: 'button',
                        onClick: () => onToggle(sub.id),
                        className: 'flex items-center gap-1.5 text-left',
                        children: [
                          jsx(Codicon, {
                            name: collapsed[sub.id] ? 'chevron-right' : 'chevron-down',
                            size: '0.7rem',
                            className: 'text-(--ui-text-tertiary)'
                          }),
                          jsx('span', {
                            className: 'font-mono text-[0.6875rem] text-(--ui-text-secondary)',
                            children: sub.label
                          }),
                          jsx('span', {
                            className: 'text-[0.625rem] text-(--ui-text-tertiary)',
                            children: String(sub.rows.length)
                          })
                        ]
                      }),
                      collapsed[sub.id]
                        ? null
                        : jsx('div', {
                            className: 'flex flex-col gap-2 pl-3',
                            children: sub.rows.map(renderRow)
                          })
                    ]
                  }, sub.id)
                : jsx('div', {
                    key: sub.id,
                    className: 'flex flex-col gap-2',
                    children: sub.rows.map(renderRow)
                  }, sub.id)
            )
          })
        : null
    ]
  })
}

/** The cahier itself, framed in the workspace pane. A plain iframe to the bridge
 *  is the whole mechanism: the page is same-origin with its own /save, so answers
 *  work exactly as they do in a tab. `Browser` is the escape hatch, kept one
 *  click away because some cahiers are big enough to want a real tab. */
function Viewer({ row, onBack }) {
  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-hidden p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, {
            variant: 'ghost',
            size: 'xs',
            onClick: onBack,
            children: [
              jsx(Codicon, { name: 'arrow-left', size: '0.8rem' }),
              jsx('span', { children: 'Cahiers' })
            ]
          }),
          jsx(Badge, {
            variant: statusVariant(row),
            size: 'xs',
            children: statusLabel(row)
          }),
          jsx('span', {
            className: 'truncate text-[0.8125rem] font-medium text-(--ui-text-primary)',
            children: row.title || row.slug
          }),
          jsxs('span', {
            className: 'ml-auto flex items-center gap-1.5',
            children: [
              jsx(Button, {
                variant: 'ghost',
                size: 'xs',
                onClick: () => osApi && osApi.openExternal(row.url),
                children: [
                  jsx(Codicon, { name: 'link-external', size: '0.8rem' }),
                  jsx('span', { children: 'Browser' })
                ]
              }),
              jsx(CopyLink, { url: row.url })
            ]
          })
        ]
      }),
      jsx('iframe', {
        src: row.url,
        title: row.title || row.slug,
        allow: 'fullscreen; clipboard-write',
        referrerPolicy: 'strict-origin-when-cross-origin',
        'data-cahier-frame': row.slug,
        className: 'min-h-0 w-full flex-1 rounded-(--ui-radius-md) border border-(--ui-stroke-secondary) bg-transparent'
      })
    ]
  })
}

function Warnings({ list }) {
  if (!list || !list.length) return null
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-(--ui-radius-md) border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5 text-[0.6875rem] font-medium text-(--ui-text-secondary)',
        children: [
          jsx(Codicon, { name: 'warning', size: '0.8rem' }),
          jsx('span', { children: `${plural(list.length, 'integrity note', 'integrity notes')} from cahier_ctl` })
        ]
      }),
      ...list.map((w, i) =>
        jsx('div', {
          key: `w${i}`,
          className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
          children: w
        })
      )
    ]
  })
}

/* -------------------------------------------------------------------- panel */

function Panel() {
  const [scope, setScope] = useState('active')
  const [needle, setNeedle] = useState('')
  const [groupBy, setGroupBy] = useState(() => {
    try {
      return (storage && storage.get('groupBy', 'profile')) || 'profile'
    } catch {
      return 'profile'
    }
  })
  const [collapsed, setCollapsed] = useState({})
  const [viewing, setViewing] = useState(null)

  // Light/dark is the app's, not ours: `useTheme` is the app's own theme door,
  // so this button flips the real appearance (and persists it) rather than
  // skinning only this pane. `renderedMode` is what is actually painted — under
  // `system` that is the honest answer about which way the toggle goes.
  const theme = useTheme() || {}
  const dark = theme.renderedMode === 'dark'
  const flipTheme = () => {
    if (typeof theme.setMode === 'function') theme.setMode(dark ? 'light' : 'dark')
  }

  const query = useQuery({
    queryKey: ['cahier-hub', scope],
    queryFn: () => rest(`/list?scope=${encodeURIComponent(scope)}`),
    staleTime: 5000,
    refetchInterval: 30000,
    retry: 1
  })

  const data = query.data
  const rows = useMemo(() => {
    const list = (data && data.rows) || []
    const q = needle.trim().toLowerCase()
    if (!q) return list
    return list.filter(r =>
      [r.slug, r.title, r.file, r.why, r.profile, r.project].some(v =>
        String(v || '').toLowerCase().includes(q)
      )
    )
  }, [data, needle])

  const groups = useMemo(() => groupRows(rows, groupBy), [rows, groupBy])
  const counts = (data && data.counts) || {}
  const bridge = data && data.bridge
  const vocab = (data && data.vocab) || null

  const toggle = id => setCollapsed(prev => ({ ...prev, [id]: !prev[id] }))
  const pickGrouping = id => {
    setGroupBy(id)
    try {
      if (storage) storage.set('groupBy', id)
    } catch {
      /* storage is a nicety — never a reason to fail the click */
    }
  }

  const header = jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Codicon, { name: 'notebook', size: '0.9rem' }),
          jsx('span', {
            className: 'text-[0.8125rem] font-medium text-(--ui-text-primary)',
            children: 'Cahiers'
          }),
          jsxs('span', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: [
              plural((counts.lifecycle || {}).serving || 0, 'serving', 'serving'),
              ' · ',
              plural((counts.lifecycle || {}).paused || 0, 'paused', 'paused'),
              ' · ',
              `${counts.total || 0} tracked`
            ]
          }),
          jsxs('span', {
            className: 'ml-auto flex items-center gap-1',
            children: [
              jsx(Button, {
                variant: 'ghost',
                size: 'xs',
                title: bridge ? `bridge ${bridge.base}` : 'bridge unknown',
                onClick: () => query.refetch(),
                children: [
                  jsx(Codicon, { name: 'refresh', size: '0.8rem' }),
                  jsx('span', {
                    children: query.isFetching
                      ? 'Refreshing…'
                      : bridge && bridge.up
                        ? 'Bridge up'
                        : 'Bridge down'
                  })
                ]
              }),
              // Rightmost control in the header = top right of the pane. Icons
              // are Tabler outline glyphs (stroke, no fill): monochrome, inherits
              // currentColor, so it reads the same in either mode.
              jsx(Button, {
                variant: 'ghost',
                size: 'icon-xs',
                title: dark ? 'Switch to light mode' : 'Switch to dark mode',
                'aria-label': dark ? 'Switch to light mode' : 'Switch to dark mode',
                'aria-pressed': dark,
                'data-theme-toggle': dark ? 'dark' : 'light',
                onClick: flipTheme,
                children: dark
                  ? jsx(icons.Sun, { className: 'size-3.5', stroke: 1.75, 'aria-hidden': true })
                  : jsx(icons.Moon, { className: 'size-3.5', stroke: 1.75, 'aria-hidden': true })
              })
            ]
          })
        ]
      }),
      jsx(Input, {
        value: needle,
        onChange: e => setNeedle(e.target.value),
        placeholder: 'Filter by slug, title, file, profile or project…',
        'aria-label': 'Filter cahiers'
      }),
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: SCOPES.map(s =>
          jsx(ScopePill, {
            active: scope === s.id,
            onClick: () => setScope(s.id),
            children: s.label
          }, s.id)
        )
      }),
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('span', {
            className: 'text-[0.625rem] uppercase tracking-wide text-(--ui-text-tertiary)',
            children: 'Group'
          }),
          ...GROUPINGS.map(g =>
            jsx(ScopePill, {
              active: groupBy === g.id,
              onClick: () => pickGrouping(g.id),
              children: g.label
            }, g.id)
          )
        ]
      })
    ]
  })

  const renderRow = row =>
    jsx(
      CahierRow,
      { row, vocab, onOpen: setViewing, onSaved: () => query.refetch() },
      row.slug
    )

  let body
  if (query.isLoading) {
    body = jsxs('div', {
      className: 'flex flex-col gap-2',
      children: [
        jsx(Skeleton, { className: 'h-16 w-full' }),
        jsx(Skeleton, { className: 'h-16 w-full' }),
        jsx(Skeleton, { className: 'h-16 w-full' })
      ]
    })
  } else if (query.error) {
    body = jsx(ErrorState, {
      title: 'Cahier Hub could not read the cahier list',
      description: String((query.error && query.error.message) || query.error),
      children: jsx(Button, { variant: 'secondary', onClick: () => query.refetch(), children: 'Retry' })
    })
  } else if (!rows.length) {
    body = jsx(EmptyState, {
      title: needle ? 'No cahier matches that filter' : 'No cahiers in this scope',
      description: needle
        ? 'Clear the filter, or switch to All to see finished cahiers.'
        : 'Serve a cahier and it shows up here within 30 seconds.'
    })
  } else if (groupBy === 'none') {
    body = jsx('div', { className: 'flex flex-col gap-2', children: rows.map(renderRow) })
  } else {
    body = jsx('div', {
      className: 'flex flex-col gap-3',
      children: groups.map(g =>
        jsx(
          Group,
          { group: g, mode: groupBy, collapsed, onToggle: toggle, renderRow },
          g.id
        )
      )
    })
  }

  if (viewing && viewing.url) {
    return jsx(Viewer, { row: viewing, onBack: () => setViewing(null) })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-hidden p-3',
    children: [
      header,
      data ? jsx(Warnings, { list: data.integrity && data.integrity.warnings }) : null,
      data && data.degraded
        ? jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: 'Degraded: the registry file only, served files not scanned.'
          })
        : null,
      jsx(Separator, {}),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: body
      })
    ]
  })
}

/* -------------------------------------------------------------------- plugin */

export default {
  id: 'cahier-hub',
  register(ctx) {
    rest = ctx.rest
    osApi = ctx.os
    storage = ctx.storage

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: ROUTE },
      render: () => jsx(Panel, {})
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: ROUTE, label: 'Cahiers', codicon: 'notebook' }
    })
  }
}
