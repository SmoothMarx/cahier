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
 * Read-only by contract: this pane only ever GETs. The list order is the
 * backend's — it is sorted deterministically in cahier_ctl.iteration() and is
 * NOT re-sorted here, so the panel and `cahier_ctl.py list` read identically.
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
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useMemo, useState } from 'react'

const ROUTE = '/cahiers'

/** The desktop app hands us its own REST + native bridge at register() time. */
let rest = null
let osApi = null

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

const SCOPES = [
  { id: 'active', label: 'Active' },
  { id: 'all', label: 'All' },
  { id: 'finished', label: 'Finished' }
]

/* ------------------------------------------------------------------ helpers */

function clock(value) {
  const s = String(value || '')
  if (!s) return ''
  return s.length >= 16 ? `${s.slice(0, 10)} ${s.slice(11, 16)}` : s
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`
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

function CahierRow({ row }) {
  const meta = [
    plural(row.saves, 'save', 'saves'),
    row.people ? plural(row.people, 'contributor', 'contributors') : null,
    row.last_save ? `last ${clock(row.last_save)}` : null,
    row.armed && row.deadline ? `deadline ${clock(row.deadline)}` : null
  ].filter(Boolean)

  return jsxs('div', {
    'data-slug': row.slug,
    className: cn(
      'flex flex-col gap-1.5 rounded-(--ui-radius-md) border px-3 py-2',
      row.state === 'live'
        ? 'border-(--ui-accent)'
        : 'border-(--ui-stroke-secondary)'
    ),
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Badge, {
            variant: STATE_VARIANT[row.state] || 'muted',
            size: 'xs',
            children: STATE_LABEL[row.state] || row.state
          }),
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
            className: 'ml-auto font-mono text-[0.6875rem] text-(--ui-text-tertiary)',
            children: row.slug
          })
        ]
      }),
      jsx('div', {
        className: 'text-[0.6875rem] text-(--ui-text-secondary)',
        children: meta.join(' · ') || 'no activity yet'
      }),
      row.why
        ? jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: row.why
          })
        : null,
      jsxs('div', {
        className: 'flex items-center gap-2 pt-0.5',
        children: [
          row.url
            ? jsx(Button, {
                variant: 'secondary',
                size: 'xs',
                onClick: () => osApi && osApi.openExternal(row.url),
                children: [
                  jsx(Codicon, { name: 'link-external', size: '0.8rem' }),
                  jsx('span', { children: 'Open' })
                ]
              })
            : null,
          jsx(CopyLink, { url: row.url }),
          row.file
            ? jsx('span', {
                className: 'truncate font-mono text-[0.625rem] text-(--ui-text-tertiary)',
                children: row.file
              })
            : null
        ]
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
      [r.slug, r.title, r.file, r.why].some(v => String(v || '').toLowerCase().includes(q))
    )
  }, [data, needle])

  const counts = (data && data.counts) || {}
  const bridge = data && data.bridge

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
              plural(counts.live || 0, 'live', 'live'),
              ' · ',
              plural(counts.pending || 0, 'pending', 'pending'),
              ' · ',
              `${counts.total || 0} tracked`
            ]
          }),
          jsx('span', {
            className: 'ml-auto',
            children: jsx(Button, {
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
            })
          })
        ]
      }),
      jsx(Input, {
        value: needle,
        onChange: e => setNeedle(e.target.value),
        placeholder: 'Filter by slug, title or file…',
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
      })
    ]
  })

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
  } else {
    body = jsx('div', {
      className: 'flex flex-col gap-2',
      children: rows.map(r => jsx(CahierRow, { row: r }, r.slug))
    })
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
