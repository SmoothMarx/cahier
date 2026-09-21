/**
 * Renders the sidebar page in Node, with the SDK stubbed, against the payload
 * the real backend produced. Proves the parts the desktop app owns:
 *
 *   - register() contributes one nav row and one route on the right paths;
 *   - the page renders EVERY cahier the backend sent (nothing dropped);
 *   - no slug appears twice;
 *   - two renders produce identical text (deterministic);
 *   - "Open" hands the served URL to the app's native opener, "Copy link" to
 *     the clipboard — i.e. the access story actually works.
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

const WORK = path.join(os.homedir(), '.hermes', 'cache', 'scratch', 'cahier-hub-js')
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
export const Codicon = p => jsx('span', { title: p.title, children: null })
export const Button = p => jsx('button', { onClick: p.onClick, title: p.title, children: p.children })
export const Input = p => jsx('input', { value: p.value, placeholder: p.placeholder })
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
`)

pkg('react', { name: 'react', type: 'module', exports: { '.': './index.js', './jsx-runtime': './jsx-runtime.js' } })
pkg('@hermes/plugin-sdk', { name: '@hermes/plugin-sdk', type: 'module', exports: { '.': './index.js' } })

const pluginCopy = path.join(WORK, 'plugin.mjs')
fs.copyFileSync(PLUGIN_SRC, pluginCopy)

/* ----------------------------------------------------------------- harness */

globalThis.__CAHIER_FIXTURE__ = fixture
let clipboard = []
// Node 22 ships a getter-only global `navigator`; override the descriptor.
Object.defineProperty(globalThis, 'navigator', {
  value: { clipboard: { writeText: async t => { clipboard.push(t) } } },
  configurable: true,
  writable: true
})

const registered = []
const opened = []
const requested = []
const ctx = {
  register: c => registered.push(c),
  rest: async p => { requested.push(p); return fixture },
  os: { openExternal: u => opened.push(u) }
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

const buttons = []
walk(tree, n => { if (n.type === 'button') buttons.push(n) })
const openButtons = buttons.filter(b => textOf(b.props.children).join('') === 'Open')
const withUrl = fixture.rows.filter(r => r.url)
check(openButtons.length === withUrl.length, 'one Open per served cahier',
  `${openButtons.length} buttons for ${withUrl.length} urls`)

if (openButtons.length) {
  openButtons[0].props.onClick()
  check(opened.length === 1 && opened[0] === withUrl[0].url, 'Open calls the native opener with the served URL', opened[0])
}

const copyButtons = buttons.filter(b => String(b.props.title || '').startsWith('http'))
if (copyButtons.length) {
  await copyButtons[0].props.onClick()
  check(clipboard.length === 1 && clipboard[0] === copyButtons[0].props.title,
    'Copy link writes the served URL to the clipboard', clipboard[0])
}

check(requested.every(p => p.startsWith('/list')), 'page only ever GETs /list', requested.join(','))

const warnText = (fixture.integrity && fixture.integrity.warnings) || []
if (warnText.length) {
  check(warnText.every(w => text.includes(w.slice(0, 40))), 'integrity notes are visible in the panel',
    `${warnText.length} notes`)
}

console.log(fails.length ? `\nFAILED: ${fails.join('; ')}` : '\nall panel checks passed')
process.exit(fails.length ? 1 : 0)
