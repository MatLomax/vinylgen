// @vitest-environment jsdom
// The Nodes stage's trace-run bar (`NodesRevision`) and its half-width line.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Revision } from './api'
import { NodesRevision, halfWidthsLine } from './NodesRevision'

const run = (id: string, trace: Record<string, unknown> | undefined, message: string | null = 'Deterministic trace pipeline'): Revision =>
  ({ id, project_id: 'p', parent_revision_id: null, kind: 'trace', message, settings: trace === undefined ? {} : { trace }, created_at: `2026-11-0${id.length}T18:15:05Z` })

afterEach(cleanup)

describe('halfWidthsLine', () => {
  it('names each kind in the Curve kinds order, off for null, the default for a kind not recorded', () => {
    expect(halfWidthsLine(run('a', { half_line: 1, half_arc: null, half_ellipse: null, half_cubic: 2 })))
      .toBe('Lines 1 px · Circular arcs off · Elliptical arcs off · Cubic curves 2 px')
    expect(halfWidthsLine(run('a', { soft_path_nodes: 5000 }))).toBe('Lines 1.75 px · Circular arcs 1.75 px · Elliptical arcs 1.75 px · Cubic curves 1.75 px')
    expect(halfWidthsLine(run('a', undefined))).toBe('Lines 1.75 px · Circular arcs 1.75 px · Elliptical arcs 1.75 px · Cubic curves 1.75 px')
  })
})

describe('NodesRevision', () => {
  const runs = [run('ccc', { half_line: 1 }), run('bb', {}, 'edited in nib'), run('a', {})]

  it('shows the open run as "Trace N of M" with its message and time, and steps older with ◀ and newer with ▶', () => {
    const onStep = vi.fn()
    render(createElement(NodesRevision, { runs, index: 1, onStep }))
    expect(screen.getByText('Trace 2 of 3')).toBeTruthy()
    expect(screen.getByText('edited in nib')).toBeTruthy()
    expect(document.querySelector('time')?.getAttribute('dateTime')).toBe(runs[1].created_at)
    fireEvent.click(screen.getByRole('button', { name: 'Open the previous trace run' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open the next trace run' }))
    expect(onStep.mock.calls).toEqual([[1], [-1]])
  })

  it('disables ◀ on the oldest run and ▶ on the newest', () => {
    render(createElement(NodesRevision, { runs, index: 0, onStep: vi.fn() }))
    expect((screen.getByRole('button', { name: 'Open the next trace run' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Open the previous trace run' }) as HTMLButtonElement).disabled).toBe(false)
    cleanup()
    render(createElement(NodesRevision, { runs, index: 2, onStep: vi.fn() }))
    expect((screen.getByRole('button', { name: 'Open the previous trace run' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('holds both arrows while locked, and says why', () => {
    render(createElement(NodesRevision, { runs, index: 1, onStep: vi.fn(), locked: 'Save or undo your node edits to open another trace run.' }))
    for (const name of ['Open the previous trace run', 'Open the next trace run']) expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Save or undo your node edits to open another trace run.')).toBeTruthy()
  })

  it('renders nothing when the open revision is not in a trace chain', () => {
    const { container } = render(createElement(NodesRevision, { runs: [], index: -1, onStep: vi.fn() }))
    expect(container.innerHTML).toBe('')
  })
})
