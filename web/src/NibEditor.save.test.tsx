// @vitest-environment jsdom
// NibEditor's save outcomes beyond the frozen NibEditor.test.tsx's "unchanged" notice: a save the
// server accepts hands the new revision to `onSaved` and says so; any other refusal is an error
// listing the server's problems. The browser stubs are the frozen test's (nib's pack.slowtest.ts).
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Revision } from './api'
import { NibEditor } from './NibEditor'

const NODE_FS = 'node:fs'
let MAP = ''
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ NODE_FS) as { readFileSync(path: string, encoding: 'utf8'): string }
  const here = (import.meta as unknown as { dirname: string }).dirname
  MAP = fs.readFileSync(`${here}/../../tests/fixtures/t3-test-array/map.json`, 'utf8')
})
const MAP_URL = '/api/artifacts/map-1/content'
const noop = () => {}
const inert = () => new Proxy({} as Record<PropertyKey, unknown>, {
  get: (target, key) => (key in target ? target[key] : noop),
  set: (target, key, value) => { target[key] = value; return true },
})
const json = (body: unknown, status: number) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

function stubBrowser(answer: Response) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url === MAP_URL && (init?.method ?? 'GET') === 'GET') return new Response(MAP, { status: 200 })
    if (url === '/api/revisions/trace-1/map' && init?.method === 'POST') return answer
    return json({ detail: 'unexpected' }, 404)
  }))
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', noop)
  vi.stubGlobal('Path2D', class { constructor() { return inert() } })
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => inert() as unknown as CanvasRenderingContext2D)
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

async function saveWith(answer: Response, onSaved = vi.fn()) {
  stubBrowser(answer)
  render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: MAP_URL, onSaved }))
  const save = await screen.findByRole('button', { name: /^save\b/i })
  await vi.waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false))
  await act(async () => { fireEvent.click(save) })
  return onSaved
}

describe('NibEditor saves', () => {
  it('hands the new revision to onSaved and says it was saved', async () => {
    const child: Revision = { id: 'child-1', project_id: 'p', parent_revision_id: 'trace-1', kind: 'trace', message: 'edited in nib', settings: {}, created_at: '2026-01-01' }
    const onSaved = await saveWith(json(child, 201))
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith(child))
    expect(screen.getByRole('status').textContent).toMatch(/saved/i)
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('shows any other refusal as an error with its problems', async () => {
    const onSaved = await saveWith(json({ detail: 'The map fails the planar-map check with 2 violation(s).', reason: 'invalid', violations: 2, problems: ['ring 0 of face 0 does not close', 'edge 3 sides'] }, 422))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/2 violation/)
    expect(alert.querySelectorAll('li')).toHaveLength(2)
    expect(onSaved).not.toHaveBeenCalled()
  })
})
