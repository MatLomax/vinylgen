// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'

const layers: WorkspaceLayer[] = [
  { id: 'source', name: 'Source', url: '/source.png', mediaType: 'image/png', role: 'image' },
  { id: 'flat', name: 'Flattened', url: '/flat.png', mediaType: 'image/png', role: 'image' },
  { id: 'report', name: 'Report', url: '/report.png', mediaType: 'image/png', role: 'image' },
  { id: 'mask', name: 'Edit mask', url: '/mask.png', mediaType: 'image/png', role: 'mask' },
]

beforeEach(() => {
  vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function load(container: HTMLElement, name: string, width: number, height: number) {
  const image = [...container.querySelectorAll('img')].find(node => node.src.endsWith(`/${name}.png`)) as HTMLImageElement
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
  fireEvent.load(image)
}

function prepare() {
  const result = render(<ImageWorkspace layers={layers} />)
  const viewport = within(result.container).getByLabelText('Image comparison viewport')
  Object.defineProperties(viewport, {
    clientWidth: { configurable: true, value: 800 },
    clientHeight: { configurable: true, value: 600 },
  })
  viewport.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) })
  HTMLElement.prototype.setPointerCapture = () => undefined
  load(result.container, 'source', 1000, 700)
  load(result.container, 'flat', 1000, 700)
  load(result.container, 'report', 1000, 850)
  load(result.container, 'mask', 1000, 700)
  return { ...result, viewport }
}

describe('ImageWorkspace interactions', () => {
  it('renders an enabled mask on the same shared image stage', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByLabelText('Edit mask'))
    const mask = screen.getByAltText('Edit mask')
    expect(mask.classList.contains('mask')).toBe(true)
    expect(mask.parentElement?.classList.contains('image-stage')).toBe(true)
  })

  it('blocks a misleading comparison when intrinsic dimensions differ', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    const after = within(desktop).getByLabelText('After') as HTMLSelectElement
    fireEvent.change(after, { target: { value: 'report' } })
    fireEvent.click(within(desktop).getByText('split'))
    expect(within(desktop).getByRole('status').textContent).toContain('different pixel dimensions')
    expect(screen.queryByAltText('Report')).toBeNull()
  })

  it('pans with one pointer and pinches around a shared transform', () => {
    const { container, viewport } = prepare()
    const stage = container.querySelector('.image-stage') as HTMLElement
    const initial = stage.style.transform
    fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 100, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 1, clientX: 140, clientY: 125 })
    expect(stage.style.transform).not.toBe(initial)
    const panned = stage.style.transform
    fireEvent.pointerDown(viewport, { pointerId: 2, clientX: 200, clientY: 100 })
    fireEvent.pointerMove(viewport, { pointerId: 2, clientX: 260, clientY: 100 })
    expect(stage.style.transform).not.toBe(panned)
    fireEvent.pointerUp(viewport, { pointerId: 2 })
    fireEvent.pointerUp(viewport, { pointerId: 1 })
  })

  it('exposes phone controls with touch-sized primary actions', () => {
    const { container } = prepare()
    const toolbarButton = screen.getByRole('button', { name: 'Controls' })
    fireEvent.click(toolbarButton)
    const sheet = container.querySelector('.bottom-sheet') as HTMLElement
    expect(sheet.classList.contains('open')).toBe(true)
    expect(within(sheet).getByRole('button', { name: 'Done' })).toBeTruthy()
    fireEvent.click(within(sheet).getByRole('button', { name: 'split' }))
    expect(within(sheet).getByLabelText('Split position', { exact: true })).toBeTruthy()
  })

  it('exposes comparison selection state and manual blink controls', () => {
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    const blink = within(desktop).getByRole('button', { name: 'blink' })
    expect(blink.getAttribute('aria-pressed')).toBe('false')
    fireEvent.change(within(desktop).getByLabelText('After'), { target: { value: 'flat' } })
    fireEvent.click(blink)
    expect(blink.getAttribute('aria-pressed')).toBe('true')
    expect(within(desktop).getByRole('button', { name: 'Pause blink' })).toBeTruthy()
    fireEvent.click(within(desktop).getByRole('button', { name: 'Show after image' }))
    expect(screen.getByAltText('Flattened')).toBeTruthy()
    expect(within(desktop).getByRole('button', { name: 'Show before image' })).toBeTruthy()
  })

  it('starts blink paused when reduced motion is preferred', () => {
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
    const { container } = prepare()
    const desktop = container.querySelector('.desktop-view-controls') as HTMLElement
    fireEvent.click(within(desktop).getByRole('button', { name: 'blink' }))
    expect(within(desktop).getByRole('button', { name: 'Resume blink' })).toBeTruthy()
    expect(within(desktop).getByText(/reduced-motion/).textContent).toContain('reduced-motion')
    fireEvent.click(within(desktop).getByRole('button', { name: 'Resume blink' }))
    expect(within(desktop).getByRole('button', { name: 'Pause blink' })).toBeTruthy()
    expect(within(desktop).queryByText(/reduced-motion/)).toBeNull()
  })

  it('treats phone controls as a modal, traps focus, closes on Escape, and restores focus', () => {
    const { container } = prepare()
    const trigger = screen.getByRole('button', { name: 'Controls' })
    trigger.focus()
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Workspace controls' })
    const done = within(dialog).getByRole('button', { name: 'Done' })
    expect(document.activeElement).toBe(done)
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(container.querySelector('.viewport')?.hasAttribute('inert')).toBe(true)

    const focusable = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')]
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(focusable[focusable.length - 1])
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('isolates the full application background and redirects programmatic outside focus', () => {
    const result = render(<main><button aria-hidden="false">Outside action</button><section><ImageWorkspace layers={layers} /></section></main>)
    const outside = screen.getByRole('button', { name: 'Outside action' })
    const trigger = screen.getByRole('button', { name: 'Controls' })
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Workspace controls' })
    const done = within(dialog).getByRole('button', { name: 'Done' })

    expect(outside.hasAttribute('inert')).toBe(true)
    expect(outside.getAttribute('aria-hidden')).toBe('true')
    outside.focus()
    fireEvent.focusIn(outside)
    expect(document.activeElement).toBe(done)

    fireEvent.click(done)
    expect(outside.hasAttribute('inert')).toBe(false)
    expect(outside.getAttribute('aria-hidden')).toBe('false')
    expect(document.activeElement).toBe(trigger)
    result.unmount()
  })
})
