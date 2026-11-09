import { useEffect, useRef, useState } from 'react'
import { mountChrome, mountEditor, type Host, type MountedChrome, type MountedEditor, type SaveResult } from '@nib/core'
import '@nib/core/chrome.css'
import { api, type Revision } from './api'

// nib's editor with nib's chrome on a trace revision's planar map (`map.json`): the Nodes stage's
// canvas area. The component owns a positioned container holding a canvas. It mounts nib's
// `Editor` on the canvas with `mountEditor` (`@nib/core`, vendored as
// `web/vendor/nib-core-0.9.5.tgz`) through a server host, and nib's chrome over it in the
// container with `mountChrome(container, editor, { actions: ['save'] })`, styled by
// `@nib/core/chrome.css`. The host's `open` is a GET of the map artefact at `mapUrl`; its `save`
// (the chrome's Save) is a POST of the exact text nib wrote to `/api/revisions/{revisionId}/map`,
// which makes a new trace revision (`onSaved` receives it). Every outcome is a line on the
// chrome's notice: the saved revision, the server's "unchanged" detail, or a refusal's detail
// followed by its problems, one per sentence. The server stores the posted text byte for byte as
// the new revision's map.json, so a save keeps the editor as it is: nib sets its save point and
// keeps its view, selection, layers panel and undo history, the next save posts to the saved
// revision, and props moving onto that revision's own map do not remount it. Any other `mapUrl`
// (another revision, trace run or project) remounts the editor on that map. `notice`, when given,
// is put on the chrome's notice line, at mount and whenever it changes, such as `SAVED` on the
// revision a save made. The component has no controls of its own. With no `mapUrl` (a revision
// traced before t3, or none at all) it renders nothing.
export type NibEditorProps = { revisionId: string; mapUrl?: string | null; onSaved?: (revision: Revision) => void; notice?: string | null; onEditedChange?: (edited: boolean) => void }

const MAP_NAME = 'map.json'
export const SAVED = 'Saved as a new trace revision.'

async function fetchMap(url: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(url, signal ? { signal } : undefined)
  if (!response.ok) throw new Error(`the map could not be loaded (${response.status})`)
  return response.text()
}

// A problem as a sentence: its first letter capitalised, and a closing period unless it
// already ends one.
function sentence(text: string): string {
  const trimmed = text.trim()
  if (!trimmed) return ''
  const capitalised = trimmed[0].toUpperCase() + trimmed.slice(1)
  return /[.!?]$/.test(capitalised) ? capitalised : `${capitalised}.`
}

// A refusal on one notice line: the server's detail, then each problem as its own sentence.
export function refusalNotice(detail: string, problems: readonly string[] = []): string {
  return [detail.trim(), ...problems.map(sentence)].filter(Boolean).join(' ')
}

const reason = (cause: unknown) => cause instanceof Error ? cause.message : String(cause)

export function NibEditor({ revisionId, mapUrl, onSaved, notice, onEditedChange }: NibEditorProps) {
  const container = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  // The latest props the host reads when nib calls it, without remounting the editor.
  const latest = useRef({ revisionId, mapUrl, onSaved, notice, onEditedChange })
  latest.current = { revisionId, mapUrl, onSaved, notice, onEditedChange }
  const mountedChrome = useRef<MountedChrome | null>(null)
  // The editor's mount: the map it was opened on and the revision a save from it last made
  // (`saved`). Props moving onto the saved revision's own map keep the mount; any other map
  // starts a new one (`key`), which the effect below remounts the editor on.
  const [mount, setMount] = useState({ key: 0, mapUrl, saved: null as string | null })
  if (mapUrl !== mount.mapUrl) {
    const own = mount.saved !== null && revisionId === mount.saved && Boolean(mapUrl)
    setMount({ key: own ? mount.key : mount.key + 1, mapUrl, saved: own ? mount.saved : null })
  }
  const mountKey = mapUrl === mount.mapUrl ? mount.key : null

  useEffect(() => {
    const element = canvas.current, root = container.current
    if (mountKey === null || !mapUrl || !element || !root) return
    const controller = new AbortController()
    let editor: MountedEditor | null = null
    let chrome: MountedChrome | null = null
    let saving = false
    // The revision the last save from this mount made: the next save posts there.
    let savedTo: string | null = null
    // Whether the open map holds an edit not yet saved: nib's `unsaved`, true while its undo
    // position is not the save point its last save set (the opened map before any save).
    let edited = false
    const setEdited = (next: boolean) => { if (next !== edited) { edited = next; latest.current.onEditedChange?.(next) } }
    let unwatch: (() => void) | null = null
    // nib's chrome clears its notice line once its Save has resolved, so a save's outcome is
    // put on the line on the next task, after that clear; then a saved revision goes to
    // `onSaved`, which selects it while this editor stays mounted. A chrome disposed by then
    // ignores the line.
    const afterSave = (text: string, saved?: Revision) => {
      window.setTimeout(() => {
        chrome?.notice(text)
        if (saved) latest.current.onSaved?.(saved)
      }, 0)
    }
    const host: Host = {
      open: async () => {
        const url = latest.current.mapUrl
        return url ? { name: MAP_NAME, text: await fetchMap(url), kind: 'label-map' } : null
      },
      save: async (_name, text, kind): Promise<SaveResult> => {
        // Only the planar map goes back to the server; nib's SVG exports have no place there.
        // A second Save while one is on its way posts nothing.
        if (kind !== 'label-map' || saving) return 'cancelled'
        saving = true
        try {
          const saved = await api.saveMap(savedTo ?? latest.current.revisionId, text)
          if ('refusal' in saved) {
            const { refusal } = saved
            if (refusal.reason === 'unchanged') { afterSave(refusal.detail); return 'empty' }
            afterSave(refusalNotice(refusal.detail, refusal.problems))
            return 'cancelled'
          }
          savedTo = saved.revision.id
          setMount(current => current.key === mountKey ? { ...current, saved: saved.revision.id } : current)
          afterSave(SAVED, saved.revision)
          return 'saved'
        } catch (cause) {
          afterSave(`Could not save the map: ${reason(cause)}`)
          return 'cancelled'
        } finally {
          saving = false
        }
      },
    }
    // The editor and its chrome, on the map's text; with none (the map failed to load) the
    // canvas stays empty and the chrome says why.
    const mount = (text?: string) => {
      editor = mountEditor(element, text === undefined ? { host } : { host, text, kind: 'label-map' })
      chrome = mountChrome(root, editor.editor, { actions: ['save'] })
      unwatch = editor.editor.onStats(stats => setEdited(stats.unsaved))
      mountedChrome.current = chrome
      if (latest.current.notice) chrome.notice(latest.current.notice)
      return editor
    }
    fetchMap(mapUrl, controller.signal).then(text => {
      if (controller.signal.aborted) return
      return mount(text).loaded.then(() => undefined, cause => {
        if (!controller.signal.aborted) chrome?.notice(`Could not open the map: ${reason(cause)}`)
      })
    }, cause => {
      if (controller.signal.aborted) return
      mount()
      chrome?.notice(`Could not open the map: ${reason(cause)}`)
    })
    return () => {
      controller.abort()
      unwatch?.()
      setEdited(false)
      if (mountedChrome.current === chrome) mountedChrome.current = null
      chrome?.dispose()
      editor?.dispose()
    }
    // Keyed on the mount, not the URL: the saved revision's own map keeps this editor, which
    // reopens nothing (`latest` carries the new props to the host).
  }, [mountKey])

  // A notice given once the chrome is mounted replaces the line's text.
  useEffect(() => { if (notice) mountedChrome.current?.notice(notice) }, [notice])

  if (!mapUrl) return null
  return <div ref={container} className="nib-editor">
    <canvas ref={canvas} className="nib-editor-canvas" aria-label="Planar map editor" />
  </div>
}

export default NibEditor
