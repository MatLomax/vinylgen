// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ICON_RAIL_QUERY, OVERLAY_PANEL_QUERY, PHONE_QUERY, SETTINGS_PANEL_STORAGE_KEY, STAGE_RAIL_STORAGE_KEY } from './shellLayout'

const mocks = vi.hoisted(() => ({
  projects: vi.fn(), project: vi.fn(), effectiveArtifacts: vi.fn(), artifacts: vi.fn(), editor: vi.fn(),
  inkscapeHandoff: vi.fn(), health: vi.fn(), followJob: vi.fn(), generate: vi.fn(),
}))
vi.mock('./api', async importOriginal => {
  const original = await importOriginal<typeof import('./api')>()
  return { ...original, api: { ...original.api, ...mocks }, followJob: mocks.followJob }
})
vi.mock('./ImageWorkspace', () => ({ ImageWorkspace: () => null }))

// A window matching exactly the given media queries; `resizeWindow` changes them live,
// notifying each query's listeners as the browser does.
let matching: string[] = []
let listeners: Map<string, Set<(event: { matches: boolean }) => void>>
function windowMatching(...queries: string[]) {
  matching = queries
  listeners = new Map()
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    get matches() { return matching.includes(query) },
    addEventListener: (_type: string, listener: (event: { matches: boolean }) => void) => { if (!listeners.has(query)) listeners.set(query, new Set()); listeners.get(query)!.add(listener) },
    removeEventListener: (_type: string, listener: (event: { matches: boolean }) => void) => listeners.get(query)?.delete(listener),
  })))
}
function resizeWindow(...queries: string[]) {
  matching = queries
  act(() => { for (const [query, set] of listeners) for (const listener of set) listener({ matches: queries.includes(query) }) })
}
const PHONE = [PHONE_QUERY, OVERLAY_PANEL_QUERY, ICON_RAIL_QUERY]
const TABLET = [OVERLAY_PANEL_QUERY, ICON_RAIL_QUERY]
const LAPTOP = [ICON_RAIL_QUERY]

async function renderApp(stage = '') {
  window.history.replaceState(null, '', stage ? `/#stage=${stage}` : '/')
  render(createElement((await import('./App')).default))
  return document.querySelector('main')!
}
const workspace = () => document.querySelector('.workspace')!
const railToggle = () => screen.getByRole('button', { name: /stage rail$/ })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  window.scrollTo = vi.fn()
  mocks.projects.mockResolvedValue([])
  mocks.health.mockResolvedValue({ status: 'ok', max_upload_bytes: 100 * 1024 * 1024 })
  mocks.followJob.mockReturnValue(() => undefined)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('the stage rail', () => {
  it('shows stage names from 1280px, with no tooltips needed', async () => {
    windowMatching()
    const main = await renderApp()
    expect(main.className).toBe('rail-expanded')
    expect(railToggle().getAttribute('aria-label')).toBe('Collapse stage rail')
    expect(railToggle().getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('tab', { name: 'Trace' }).getAttribute('title')).toBeNull()
  })

  it('shows icons only below 1280px, each stage name kept as its tooltip and accessible name', async () => {
    windowMatching(...LAPTOP)
    const main = await renderApp()
    expect(main.className).toBe('rail-collapsed')
    expect(railToggle().getAttribute('aria-label')).toBe('Expand stage rail')
    expect(railToggle().getAttribute('aria-expanded')).toBe('false')
    expect(railToggle().getAttribute('aria-controls')).toBe(screen.getByRole('tablist').id)
    expect(screen.getByRole('tab', { name: 'Trace' }).getAttribute('title')).toBe('Trace')
    expect(screen.getByRole('tablist').getAttribute('aria-orientation')).toBe('vertical')
  })

  it('remembers the user\'s choice over the window\'s default', async () => {
    windowMatching()
    const main = await renderApp()
    fireEvent.click(railToggle())
    expect(main.className).toBe('rail-collapsed')
    expect(localStorage.getItem(STAGE_RAIL_STORAGE_KEY)).toBe('collapsed')
    cleanup()
    windowMatching(...LAPTOP)
    localStorage.setItem(STAGE_RAIL_STORAGE_KEY, 'expanded')
    expect((await renderApp()).className).toBe('rail-expanded')
  })

  it('is a horizontal step strip on a phone', async () => {
    windowMatching(...PHONE)
    const main = await renderApp()
    // Neither rail width applies: the strip keeps each stage's icon and name.
    expect(main.className).toBe('rail-strip')
    expect(screen.getByRole('tablist').getAttribute('aria-orientation')).toBe('horizontal')
    expect(screen.getByRole('tab', { name: 'Trace' }).getAttribute('title')).toBeNull()
  })
})

describe('the stage settings panel', () => {
  it('sits beside the canvas above 1024px, open until the user closes it, and remembers that', async () => {
    windowMatching(...LAPTOP)
    await renderApp('flatten')
    expect(workspace().className).toContain('panel-push panel-open')
    fireEvent.click(screen.getByRole('button', { name: 'Hide stage settings' }))
    expect(workspace().className).toContain('panel-push panel-closed')
    expect(localStorage.getItem(SETTINGS_PANEL_STORAGE_KEY)).toBe('closed')
    cleanup()
    await renderApp('flatten')
    expect(workspace().className).toContain('panel-push panel-closed')
    expect(screen.getByRole('button', { name: 'Show stage settings' })).toBeTruthy()
  })

  it('opens over the canvas from 701px to 1024px, closed at first and not remembered', async () => {
    windowMatching(...TABLET)
    localStorage.setItem(SETTINGS_PANEL_STORAGE_KEY, 'open')
    await renderApp('flatten')
    expect(workspace().className).toContain('panel-overlay panel-closed')
    fireEvent.click(screen.getByRole('button', { name: 'Show stage settings' }))
    expect(workspace().className).toContain('panel-overlay panel-open')
    expect(screen.getByRole('button', { name: 'Flatten image' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Hide stage settings' }))
    expect(localStorage.getItem(SETTINGS_PANEL_STORAGE_KEY)).toBe('open')
  })

  it('opens over the canvas on arrival at Create or Upload, whose settings are the only way forward', async () => {
    windowMatching(...TABLET)
    await renderApp('create')
    expect(workspace().className).toContain('panel-overlay panel-open')
    fireEvent.click(screen.getByRole('button', { name: 'Hide stage settings' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Flatten' }))
    expect(workspace().className).toContain('panel-overlay panel-closed')
    fireEvent.click(screen.getByRole('tab', { name: 'Upload' }))
    expect(workspace().className).toContain('panel-overlay panel-open')
  })

  it('follows the window between modes, keeping each mode\'s own open state', async () => {
    windowMatching()
    await renderApp('flatten')
    expect(workspace().className).toContain('panel-push panel-open')
    expect(document.querySelector('main')!.className).toBe('rail-expanded')
    resizeWindow(...TABLET)
    expect(workspace().className).toContain('panel-overlay panel-closed')
    expect(document.querySelector('main')!.className).toBe('rail-collapsed')
    fireEvent.click(screen.getByRole('button', { name: 'Show stage settings' }))
    resizeWindow(...PHONE)
    expect(workspace().className).toContain('panel-drawer panel-closed')
    expect(document.querySelector('main')!.className).toBe('rail-strip')
    expect(screen.getByRole('tablist').getAttribute('aria-orientation')).toBe('horizontal')
    resizeWindow(...TABLET)
    expect(workspace().className).toContain('panel-overlay panel-open')
    resizeWindow()
    expect(workspace().className).toContain('panel-push panel-open')
  })

  it('turns a phone\'s Create page into a drawer under the canvas while its generation runs', async () => {
    windowMatching(...PHONE)
    const project = { id: 'project-1', slug: 'whale', name: 'Whale', created_at: '2026-01-01', updated_at: '2026-01-01' }
    mocks.generate.mockResolvedValue({ project, job: { id: 'job-1', project_id: project.id, revision_id: null, result_revision_id: null, kind: 'generate', status: 'running', progress: 0.1, message: 'Generating', error: null } })
    mocks.project.mockResolvedValue({ ...project, revisions: [] })
    mocks.effectiveArtifacts.mockResolvedValue([])
    await renderApp('create')
    expect(workspace().className).toContain('panel-page')
    fireEvent.change(await screen.findByLabelText('Short subject'), { target: { value: 'sky whale' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate source' }))
    await vi.waitFor(() => expect(workspace().className).toContain('panel-drawer panel-closed'))
    expect(document.querySelector('.artwork-surface')!.className).toContain('mobile-stage-active')
    expect(screen.getByRole('button', { name: 'Create settings' })).toBeTruthy()
  })

  it('is a bottom drawer under a phone\'s canvas, opening collapsed to its handle', async () => {
    windowMatching(...PHONE)
    await renderApp('flatten')
    expect(workspace().className).toContain('panel-drawer panel-closed')
    const handle = screen.getByRole('button', { name: 'Flatten settings' })
    expect(handle.getAttribute('aria-expanded')).toBe('false')
    // Collapsed, the stage's run button sits beside the handle, locked as it is in the
    // settings (no project is chosen).
    const peek = screen.getByRole('button', { name: 'Flatten image' }) as HTMLButtonElement
    expect(peek.closest('.drawer-bar')).toBeTruthy()
    expect(peek.disabled).toBe(true)
    fireEvent.click(handle)
    expect(workspace().className).toContain('panel-drawer panel-open')
    expect(screen.getByRole('button', { name: 'Flatten image' }).closest('.drawer-bar')).toBeNull()
  })

  it('is the whole phone surface on a stage without a canvas', async () => {
    windowMatching(...PHONE)
    await renderApp('create')
    expect(workspace().className).toContain('panel-page panel-open')
    expect(await screen.findByLabelText('Short subject')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^(Show stage settings|Hide stage settings|Create settings)$/ })).toBeNull()
  })
})
