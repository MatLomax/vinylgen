// @vitest-environment jsdom
// `NibEditor`'s `onEditedChange`: whether the open map holds an edit not yet saved, read from nib's
// per-frame stats (`Editor.onStats`, `EditorStats.unsaved`: the undo position is not nib's save point). The host is told each change once,
// never every frame, and told `false` when the editor goes (unmount, or a remount on another
// map). `mountEditor` and `mountChrome` are stubs here: the test drives the stats nib would emit.
import { act, cleanup, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type StatsListener = (stats: { unsaved: boolean }) => void
const nib = vi.hoisted(() => ({ listeners: [] as StatsListener[][], unwatched: 0 }))
vi.mock('@nib/core', () => ({
  mountEditor: vi.fn(() => {
    const listeners: StatsListener[] = []
    nib.listeners.push(listeners)
    return {
      editor: { onStats: (fn: StatsListener) => { listeners.push(fn); return () => { nib.unwatched += 1; listeners.splice(listeners.indexOf(fn), 1) } } },
      loaded: Promise.resolve('label-map'),
      dispose: () => undefined,
    }
  }),
  mountChrome: vi.fn(() => ({ root: document.createElement('div'), notice: () => undefined, dispose: () => undefined })),
}))
vi.mock('@nib/core/chrome.css', () => ({}))

const emit = (unsaved: boolean, editor = nib.listeners.length - 1) => { for (const fn of [...nib.listeners[editor]]) fn({ unsaved }) }

beforeEach(() => {
  nib.listeners = []
  nib.unwatched = 0
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('NibEditor onEditedChange', () => {
  it('reports each change of nib\'s unsaved once, and false when the editor goes', async () => {
    const { NibEditor } = await import('./NibEditor')
    const onEditedChange = vi.fn()
    const view = render(createElement(NibEditor, { revisionId: 'trace-1', mapUrl: '/api/artifacts/map-1/content', onEditedChange }))
    await vi.waitFor(() => expect(nib.listeners[0]?.length).toBe(1))

    await act(async () => { emit(false); emit(false) })
    expect(onEditedChange).not.toHaveBeenCalled()
    await act(async () => { emit(true); emit(true); emit(true) })
    expect(onEditedChange.mock.calls).toEqual([[true]])
    await act(async () => { emit(false) })
    expect(onEditedChange.mock.calls).toEqual([[true], [false]])
    await act(async () => { emit(true) })

    // Another map: the old editor's watch ends, the host hears false, and the new one starts clean.
    view.rerender(createElement(NibEditor, { revisionId: 'trace-2', mapUrl: '/api/artifacts/map-2/content', onEditedChange }))
    await vi.waitFor(() => expect(nib.listeners[1]?.length).toBe(1))
    expect(nib.unwatched).toBe(1)
    expect(onEditedChange.mock.calls).toEqual([[true], [false], [true], [false]])

    await act(async () => { emit(true, 1) })
    view.unmount()
    expect(onEditedChange.mock.calls.at(-1)).toEqual([false])
    expect(nib.unwatched).toBe(2)
  })
})
