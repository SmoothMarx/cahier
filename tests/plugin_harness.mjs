/**
 * Renders the sidebar page in Node, with the SDK stubbed, against the payload
 * the real backend produced. Proves the parts the desktop app owns:
 *
 *   - register() contributes one nav row and one route on the right paths;
 *   - the page renders EVERY cahier the backend sent (nothing dropped);
 *   - no slug appears twice;
 *   - two renders produce identical text (deterministic);
 *   - grouping buckets the same rows by profile ▸ project (or project / flat)
 *     without dropping one, and switching the grouping restores the default;
 *   - "Open" frames the served URL in this window, "Browser" is the escape
 *     hatch to the real browser, "Copy link" hits the clipboard;
 *   - each row's lifecycle badge (serving / paused / closed) reads at its RIGHT
 *     edge, with one button per legal move directly underneath it;
 *   - those buttons POST /action with the slug and the verb (up | pause | close),
 *     and Close asks once before it ends the row;
 *   - a row's ✎ POSTs the human's profile/project to /filing;
 *   - /filing and /action are the ONLY non-read calls the page ever makes.
 *
 *   node tests/plugin_harness.mjs <fixture.json>
 *
 * The fixture comes from tests/dump_fixture.py (same FastAPI router the app
 * hits), so this is not a hand-written mock of the backend's answer.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_SRC = path.join(HERE, '..', 'desktop', 'plugin.js')
const fixturePath = process.argv[2]
if (!fixturePath) {
  console.error('usage: node plugin_harness.mjs <fixture.json>')
  process.exit(2)
}
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'))

/* ------------------------------------------------------------ stub packages */

const WORK = process.env.CAHIER_HUB_JS_WORK
  || path.join(os.tmpdir(), 'cahier-hub-js')   /* run.sh points this at its scratch dir */
fs.rmSync(WORK, { recursive: true, force: true })
const NM = path.join(WORK, 'node_modules')
fs.mkdirSync(path.join(NM, '@hermes', 'plugin-sdk'), { recursive: true })
fs.mkdirSync(path.join(NM, 'react'), { recursive: true })

const write = (rel, body) => fs.writeFileSync(path.join(NM, rel), body)
const pkg = (dir, json) => fs.writeFileSync(path.join(NM, dir, 'package.json'), JSON.stringify(json))

write('react/index.js', `
let states = []
let cursor = 0
export function useState(init) {
  const i = cursor++
  if (!(i in states)) states[i] = typeof init === 'function' ? init() : init
  const set = v => { states[i] = typeof v === 'function' ? v(states[i]) : v }
  return [states[i], set]
}
export function useMemo(fn) { cursor++; return fn() }
export function useCallback(fn) { cursor++; return fn }
export function useEffect() { cursor++ }
export function useRef(v) { const i = cursor++; if (!(i in states)) states[i] = { current: v }; return states[i] }
export function __reset() { cursor = 0 }
export default { useState, useMemo, useCallback, useEffect, useRef }
`)

write('react/jsx-runtime.js', `
export const Fragment = Symbol.for('react.fragment')
export const jsx = (type, props, key) => ({ type, props: props || {}, key })
export const jsxs = jsx
export const jsxDEV = jsx
`)

write('@hermes/plugin-sdk/index.js', `
import { jsx } from 'react/jsx-runtime'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar-nav'
export const cn = (...a) => a.flat().filter(Boolean).join(' ')
export const Badge = p => jsx('span', { children: p.children })
export const Codicon = p => jsx('span', { title: p.title, name: p.name, children: null })
export const Button = p => jsx('button', { ...p })
export const Input = p => jsx('input', { ...p })
export const ScrollArea = p => jsx('div', { children: p.children })
export const Separator = () => jsx('hr', {})
export const Skeleton = () => jsx('div', {})
export const EmptyState = p => jsx('div', { children: [p.title, p.description, p.children] })
export const ErrorState = p => jsx('div', { children: [p.title, p.description, p.children] })
export const useQuery = () => ({
  data: globalThis.__CAHIER_FIXTURE__,
  isLoading: false,
  isFetching: false,
  error: null,
  refetch: () => { globalThis.__REFETCHED__ = (globalThis.__REFETCHED__ || 0) + 1 }
})
// Outline glyph stub: renders as an <svg data-icon=...> so the walk can see
// which icon the toggle picked, and passes stroke/fill through untouched.
export const icons = new Proxy({}, {
  get: (_t, name) => (p => jsx('svg', {
    'data-icon': String(name),
    className: (p || {}).className,
    stroke: (p || {}).stroke,
    fill: (p || {}).fill,
    children: null
  }))
})
export const useTheme = () => {
  const mode = globalThis.__CAHIER_THEME__ || 'light'
  return {
    mode,
    resolvedMode: mode,
    renderedMode: mode,
    themes: [],
    theme: null,
    setMode: next => {
      globalThis.__CAHIER_THEME__ = next
      globalThis.__CAHIER_SETMODE__ = (globalThis.__CAHIER_SETMODE__ || []).concat(next)
    }
  }
}
`)

pkg('react', { name: 'react', type: 'module', exports: { '.': './index.js', './jsx-runtime': './jsx-runtime.js' } })
pkg('@hermes/plugin-sdk', { name: '@hermes/plugin-sdk', type: 'module', exports: { '.': './index.js' } })

const pluginCopy = path.join(WORK, 'plugin.mjs')
fs.copyFileSync(PLUGIN_SRC, pluginCopy)

/* ----------------------------------------------------------------- harness */

globalThis.__CAHIER_FIXTURE__ = fixture
// Start the harness in dark: the toggle must then offer light mode, which is
// the direction a human actually notices. __CAHIER_SETMODE__ records the ask.
globalThis.__CAHIER_THEME__ = 'dark'
let clipboard = []
// Node 22 ships a getter-only global `navigator`; override the descriptor.
Object.defineProperty(globalThis, 'navigator', {
  value: { clipboard: { writeText: async t => { clipboard.push(t) } } },
  configurable: true,
  writable: true
})

const registered = []
const opened = []
const calls = []
const store = new Map()
const ctx = {
  register: c => registered.push(c),
  rest: async (p, opts) => { calls.push({ path: p, opts: opts || {} }); return fixture },
  os: { openExternal: u => opened.push(u) },
  storage: {
    get: (k, d) => (store.has(k) ? store.get(k) : d),
    set: (k, v) => store.set(k, v),
    remove: k => store.delete(k)
  }
}

const plugin = (await import(pathToFileURL(pluginCopy).href)).default
plugin.register(ctx)

const react = await import(pathToFileURL(path.join(NM, 'react', 'index.js')).href)

const routes = registered.filter(c => c.area === 'routes')
const nav = registered.filter(c => c.area === 'sidebar-nav')

function walk(node, fn) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) { node.forEach(n => walk(n, fn)); return }
  fn(node)
  walk(node.props && node.props.children, fn)
}

function textOf(node, out = []) {
  if (node == null || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { node.forEach(n => textOf(n, out)); return out }
  if (typeof node === 'object' && node.props) textOf(node.props.children, out)
  return out
}

/** Real React calls function components; the walker has to as well. */
function expand(node) {
  if (node == null || typeof node === 'boolean') return node
  if (Array.isArray(node)) return node.map(expand)
  if (typeof node !== 'object') return node
  const el = typeof node.type === 'function' ? expand(node.type(node.props || {})) : node
  if (el && typeof el === 'object' && el.props && 'children' in el.props) {
    return { ...el, props: { ...el.props, children: expand(el.props.children) } }
  }
  return el
}

const fails = []
const ok = (label, extra = '') => console.log(`  PASS  ${label}${extra ? ` — ${extra}` : ''}`)
const check = (cond, label, extra = '') => {
  if (cond) ok(label, extra)
  else { fails.push(label); console.log(`  FAIL  ${label}${extra ? ` — ${extra}` : ''}`) }
}

console.log(`plugin: ${plugin.id} | fixture rows: ${fixture.rows.length} (scope ${fixture.scope})`)

check(routes.length === 1 && routes[0].data.path === '/cahiers', 'route registered at /cahiers')
check(nav.length === 1 && nav[0].data.label === 'Cahiers' && nav[0].data.path === '/cahiers',
  'sidebar nav row registered', JSON.stringify(nav[0] && nav[0].data))

react.__reset()
const tree = expand(routes[0].render())
const text = textOf(tree).join(' | ')

const missing = fixture.rows.filter(r => !text.includes(r.slug)).map(r => r.slug)
check(missing.length === 0, 'every backend cahier is listed', `${fixture.rows.length} rows`)

const present = fixture.rows.filter(r => text.includes(r.slug)).length
check(present === fixture.rows.length, 'no cahier dropped in render',
  `${present}/${fixture.rows.length} slugs rendered`)

// Row-level, not string-level: a cahier whose title matches its slug legitimately
// prints that text twice. One row node per cahier is the real invariant.
const slugNodes = []
walk(tree, n => { if (n.props && n.props['data-slug']) slugNodes.push(n.props['data-slug']) })
check(slugNodes.length === fixture.rows.length, 'one row node per cahier',
  `${slugNodes.length} nodes for ${fixture.rows.length} rows`)
const dupSlugs = slugNodes.filter((s, i) => slugNodes.indexOf(s) !== i)
check(new Set(slugNodes).size === slugNodes.length, 'no cahier rendered twice',
  dupSlugs.length ? dupSlugs.join(',') : `${new Set(slugNodes).size} unique`)

// Before any click: a click legitimately changes state, so determinism has to
// be measured on two untouched renders.
react.__reset()
const text2 = textOf(expand(routes[0].render())).join(' | ')
check(text2 === text, 'two renders are byte-identical (deterministic)')

check(String(nav[0].data.label) === 'Cahiers', 'nav label is the human door name')

const buttonsIn = (node, label) => {
  const out = []
  walk(node, n => { if (n.type === 'button' && textOf(n.props.children).join('') === label) out.push(n) })
  return out
}
const slugsIn = node => {
  const out = []
  walk(node, n => { if (n.props && n.props['data-slug']) out.push(n.props['data-slug']) })
  return out
}
const groupsIn = node => {
  const out = []
  walk(node, n => { if (n.props && n.props['data-group']) out.push(n.props['data-group']) })
  return out
}
const rerender = () => { react.__reset(); return expand(routes[0].render()) }

const buttons = []
walk(tree, n => { if (n.type === 'button') buttons.push(n) })
const withUrl = fixture.rows.filter(r => r.url)

/* ------------------------------------------- grouping: profile ▸ project (default) */

check(groupsIn(tree).length > 0, 'rows are bucketed into groups by default',
  `${groupsIn(tree).length} groups`)
const profiles = [...new Set(fixture.rows.map(r => r.profile || 'no profile'))]
const projects = [...new Set(fixture.rows.map(r => r.project || 'no project'))]
const missingProfiles = profiles.filter(p => !text.includes(p))
check(missingProfiles.length === 0, 'every profile in the payload has a group header',
  missingProfiles.length ? `missing ${missingProfiles.join(',')}` : `${profiles.length} profiles`)
const missingProjects = projects.filter(p => !text.includes(p))
check(missingProjects.length === 0, 'every project shows as a sub-group',
  missingProjects.length ? `missing ${missingProjects.join(',')}` : `${projects.length} projects`)

const flatPill = buttonsIn(tree, 'Flat')[0]
check(Boolean(flatPill), 'the grouping control offers Flat')
if (flatPill) {
  flatPill.props.onClick()
  const flat = rerender()
  check(groupsIn(flat).length === 0, 'Flat drops the group headers')
  check(slugsIn(flat).length === fixture.rows.length, 'Flat keeps every cahier',
    `${slugsIn(flat).length}/${fixture.rows.length}`)
  check(store.get('groupBy') === 'none', 'the chosen grouping is remembered')
}

const backPill = buttonsIn(tree, 'Profile ▸ Project')[0]
if (backPill) backPill.props.onClick()
const regrouped = rerender()
check(groupsIn(regrouped).length === groupsIn(tree).length,
  'switching back restores the profile ▸ project grouping', `${groupsIn(regrouped).length} groups`)
check(slugsIn(regrouped).length === fixture.rows.length,
  'grouping never drops a cahier', `${slugsIn(regrouped).length}/${fixture.rows.length}`)

/* ------------------------------------------------- reading a cahier in-window */

const openButtons = buttonsIn(regrouped, 'Open')
check(openButtons.length === withUrl.length, 'one Open per served cahier',
  `${openButtons.length} buttons for ${withUrl.length} urls`)

if (openButtons.length) {
  openButtons[0].props.onClick()
  const viewer = rerender()
  let frame = null
  walk(viewer, n => { if (n.type === 'iframe') frame = n })
  check(Boolean(frame), 'Open frames the cahier in this window (no browser tab)')
  check(Boolean(frame) && withUrl.some(r => r.url === frame.props.src),
    'the frame points at a served URL', frame && frame.props.src)
  check(slugsIn(viewer).length === 0, 'the viewer replaces the list')

  const browserBtn = buttonsIn(viewer, 'Browser')[0]
  check(Boolean(browserBtn), 'the browser stays one click away')
  if (browserBtn && frame) {
    browserBtn.props.onClick()
    check(opened.length === 1 && opened[0] === frame.props.src,
      'Browser hands the same URL to the native opener', opened[0])
  }

  const backBtn = buttonsIn(viewer, 'Cahiers')[0]
  check(Boolean(backBtn), 'the viewer keeps a way back to the list')
  if (backBtn) {
    backBtn.props.onClick()
    const listAgain = rerender()
    check(slugsIn(listAgain).length === fixture.rows.length, 'back returns to the full list',
      `${slugsIn(listAgain).length}/${fixture.rows.length}`)
  }
}

const copyButtons = buttonsIn(regrouped, 'Copy link')
if (copyButtons.length) {
  await copyButtons[0].props.onClick()
  check(clipboard.length === 1, 'Copy link writes the served URL to the clipboard', clipboard[0])
}

/* ------------------------------- actions: status on the right, buttons under it */

const rightCells = []
walk(regrouped, n => { if (n.props && n.props['data-row-status'] !== undefined) rightCells.push(n) })
check(rightCells.length === fixture.rows.length, 'every row carries a right-hand status cell',
  `${rightCells.length}/${fixture.rows.length}`)

const stateReadsFirst = rightCells.every(cell => {
  const kids = Array.isArray(cell.props.children) ? cell.props.children : [cell.props.children]
  return textOf(kids[0]).join('').trim().length > 0
})
check(stateReadsFirst, 'the state reads first in that cell, the buttons sit underneath it')

/* The badge reads the LIFECYCLE (what the buttons act on), not the phase. */
const badgeLabel = cell => textOf(cell.props.children[0]).join('').trim()
const lifeCells = rightCells.filter(c => c.props['data-row-lifecycle'])
check(lifeCells.every(c => badgeLabel(c) === c.props['data-row-lifecycle']),
  'the badge reads the lifecycle: serving / paused / closed',
  lifeCells.map(c => `${c.props['data-row-lifecycle']}→${badgeLabel(c)}`).join(', '))
const bareCells = rightCells.filter(c => c.props['data-row-lifecycle'] === '')
check(bareCells.every(c => badgeLabel(c).length > 0),
  'a page that was never published still shows its phase', `${bareCells.length} without a lifecycle`)

/* Every legal move is a button, and nothing else is. */
const legal = row => (Array.isArray(row.actions) && row.actions.length
  ? row.actions
  : (row.action === 'up' || row.action === 'down') ? [row.action] : [])
const wanted = fixture.rows.filter(r => r.controllable)
  .map(r => legal(r).length).reduce((a, b) => a + b, 0)
const actionBtns = []
walk(regrouped, n => { if (n.props && n.props['data-action-kind']) actionBtns.push(n) })
check(actionBtns.length === wanted, 'one button per legal move, none for the rest',
  `${actionBtns.length} buttons for ${wanted} legal moves`)

const wrongKind = actionBtns.filter(b => {
  const row = fixture.rows.find(r => r.slug === b.props['data-action-slug'])
  return !row || !legal(row).includes(b.props['data-action-kind'])
})
check(wrongKind.length === 0, 'the buttons follow the row: serving → Pause + Close, parked → Spin up',
  wrongKind.map(b => b.props['data-action-slug']).join(',') || `${actionBtns.length} match`)

const btnFor = (slug, kind) =>
  actionBtns.find(b => b.props['data-action-slug'] === slug && b.props['data-action-kind'] === kind)
const pausedRow = fixture.rows.find(r => r.controllable && r.lifecycle === 'paused')
const servingRow = fixture.rows.find(r => r.controllable && r.lifecycle === 'serving')
const parkedRow = fixture.rows.find(r => r.controllable && r.lifecycle !== 'serving')

if (pausedRow) {
  check(Boolean(btnFor(pausedRow.slug, 'up')) && Boolean(btnFor(pausedRow.slug, 'close'))
    && !btnFor(pausedRow.slug, 'pause'),
    'a paused cahier offers Spin up + Close, never Pause', pausedRow.slug)
} else {
  console.log('  note  no paused cahier in this fixture — the Paused row is not exercised')
}

if (servingRow) {
  check(Boolean(btnFor(servingRow.slug, 'pause')) && Boolean(btnFor(servingRow.slug, 'close')),
    'a serving cahier offers Pause + Close', servingRow.slug)
  const btn = btnFor(servingRow.slug, 'close')
  btn.props.onClick()
  const asked = rerender()
  check(buttonsIn(asked, 'Confirm close').length === 1,
    'Close asks once before it ends the row')
  await buttonsIn(asked, 'Confirm close')[0].props.onClick()
  const post = calls.filter(c => c.path === '/action').pop()
  check(Boolean(post) && post.opts.method === 'POST'
    && post.opts.body.slug === servingRow.slug && post.opts.body.action === 'close',
    'Close POSTs /action with the slug and the verb', JSON.stringify(post && post.opts.body))
} else {
  console.log('  note  no serving controllable cahier in this fixture — Close not clicked')
}

if (parkedRow) {
  const btn = btnFor(parkedRow.slug, 'up')
  check(Boolean(btn), 'a cahier that is off the bridge offers Spin up', parkedRow.slug)
  btn.props.onClick()
  const post = calls.filter(c => c.path === '/action').pop()
  check(Boolean(post) && post.opts.method === 'POST'
    && post.opts.body.slug === parkedRow.slug && post.opts.body.action === 'up',
    'Spin up POSTs /action with the slug and the verb', JSON.stringify(post && post.opts.body))
} else {
  console.log('  note  no parked controllable cahier in this fixture — Spin up not clicked')
}

/* ----------------------------------------- filing: a human's label on a row */

const rowSlug = fixture.rows[0].slug
let editBtn = null
walk(regrouped, n => { if (n.props && n.props['data-edit'] === rowSlug) editBtn = n })
check(Boolean(editBtn), 'every row offers the ✎ filing edit', rowSlug)

if (editBtn) {
  editBtn.props.onClick()
  const editor = rerender()
  // Target the editor's own fields: the header's filter box is also an <input>.
  const inputs = []
  walk(editor, n => { if (n.type === 'input' && String(n.props['aria-label'] || '').startsWith('Profile for')) inputs.push(n) })
  walk(editor, n => { if (n.type === 'input' && String(n.props['aria-label'] || '').startsWith('Project for')) inputs.push(n) })
  check(inputs.length === 2, 'the editor asks for profile + project', `${inputs.length} fields`)
  if (inputs.length >= 2) {
    inputs[0].props.onChange({ target: { value: 'dobbs' } })
    inputs[1].props.onChange({ target: { value: 'Demo' } })
    const saveBtn = buttonsIn(rerender(), 'Save')[0]
    check(Boolean(saveBtn), 'the editor has a Save button')
    if (saveBtn) {
      await saveBtn.props.onClick()
      const post = calls.find(c => c.path === '/filing')
      check(Boolean(post), 'Save POSTs to /filing')
      check(Boolean(post) && post.opts.method === 'POST', 'the write names its method',
        post && String(post.opts.method))
      check(Boolean(post) && post.opts.body && post.opts.body.slug === rowSlug
        && post.opts.body.profile === 'dobbs' && post.opts.body.project === 'Demo',
        'the write carries the slug and the human labels', JSON.stringify(post && post.opts.body))
    }
  }
}

/* ---------------------------------------- light/dark toggle (appearance door) */

const iconOf = node => {
  let hit = null
  walk(node, n => { if (!hit && n.props && n.props['data-icon']) hit = n })
  return hit
}

const themeToggles = []
walk(regrouped, n => { if (n.props && n.props['data-theme-toggle']) themeToggles.push(n) })
check(themeToggles.length === 1, 'exactly one light/dark toggle in the panel',
  `${themeToggles.length} toggles`)

const toggle = themeToggles[0]
if (toggle) {
  // "top right" = the toggle is the last control in the header's right-hand cluster.
  const clusters = []
  walk(regrouped, n => {
    if (n.props && typeof n.props.className === 'string' && n.props.className.includes('ml-auto')) clusters.push(n)
  })
  const rightCluster = clusters.find(c => Array.isArray(c.props.children) &&
    c.props.children[c.props.children.length - 1] === toggle)
  check(Boolean(rightCluster), 'the toggle is the rightmost control in the header (top right)')

  const labelDark = String(toggle.props['aria-label'] || '')
  check(labelDark.toLowerCase().includes('light') && toggle.props['aria-pressed'] === true,
    'in dark mode the toggle offers light mode and reads as pressed',
    `${labelDark} / aria-pressed=${toggle.props['aria-pressed']}`)

  const iconDark = iconOf(toggle)
  check(iconDark && String(iconDark.props['data-icon']) === 'Sun',
    'in dark mode the toggle shows the sun (the mode it switches to)',
    iconDark && String(iconDark.props['data-icon']))
  check(iconDark && iconDark.props.stroke !== undefined && iconDark.props.fill === undefined,
    'the icon is an outline glyph: stroke set, no fill (monochrome)',
    iconDark && `stroke=${iconDark.props.stroke} fill=${iconDark.props.fill}`)

  delete globalThis.__CAHIER_SETMODE__
  toggle.props.onClick()
  check(globalThis.__CAHIER_SETMODE__ && globalThis.__CAHIER_SETMODE__[0] === 'light',
    'clicking in dark mode asks the app for light mode',
    `${(globalThis.__CAHIER_SETMODE__ || []).join(',') || 'no setMode call'}`)
}

// The other direction: light mode must offer dark, with the moon.
globalThis.__CAHIER_THEME__ = 'light'
react.__reset()
const lightTree = expand(routes[0].render())
const lightToggles = []
walk(lightTree, n => { if (n.props && n.props['data-theme-toggle']) lightToggles.push(n) })
const lit = lightToggles[0]
check(lightToggles.length === 1 && String(lit.props['aria-label']).toLowerCase().includes('dark'),
  'in light mode the toggle offers dark mode', lit && String(lit.props['aria-label']))
const iconLight = lit && iconOf(lit)
check(iconLight && String(iconLight.props['data-icon']) === 'Moon',
  'in light mode the toggle shows the moon', iconLight && String(iconLight.props['data-icon']))
delete globalThis.__CAHIER_SETMODE__
if (lit) lit.props.onClick()
check(globalThis.__CAHIER_SETMODE__ && globalThis.__CAHIER_SETMODE__[0] === 'dark',
  'clicking in light mode asks the app for dark mode',
  `${(globalThis.__CAHIER_SETMODE__ || []).join(',') || 'no setMode call'}`)
globalThis.__CAHIER_THEME__ = 'dark'

const writes = calls.filter(c => c.path !== '/list')
check(writes.every(c => (c.path === '/filing' || c.path === '/action') && c.opts.method === 'POST'),
  'the panel writes nothing except the human\'s /filing and /action',
  writes.map(c => `${c.opts.method || 'GET'} ${c.path}`).join(',') || 'no writes')
check(calls.filter(c => c.path.startsWith('/list')).every(c => !c.opts.method || c.opts.method === 'GET'),
  'listing is always a plain GET')

const warnText = (fixture.integrity && fixture.integrity.warnings) || []
if (warnText.length) {
  check(warnText.every(w => text.includes(w.slice(0, 40))), 'integrity notes are visible in the panel',
    `${warnText.length} notes`)
}

console.log(fails.length ? `\nFAILED: ${fails.join('; ')}` : '\nall panel checks passed')
process.exit(fails.length ? 1 : 0)
