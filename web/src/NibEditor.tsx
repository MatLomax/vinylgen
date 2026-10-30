import { useEffect, useRef, useState } from 'react'
import { mountEditor, type Host, type MountedEditor, type SaveResult } from '@nib/core'
import { api, type Revision } from './api'

// nib's editor on a trace revision's planar map (`map.json`), the Trace canvas's Nodes overlay.
// The component owns the canvas and mounts nib's `Editor` on it with `mountEditor`
// (`@nib/core`, vendored as `web/vendor/nib-core-0.1.0.tgz`) through a server host: `open` is a
// GET of the map artefact at `mapUrl`, `save` a POST of the exact text nib wrote to
// `/api/revisions/{revisionId}/map`, which makes a new trace revision (`onSaved` receives it).
// The server's "unchanged" (nothing was edited) is a notice; any other refusal is an error that
// lists its problems. With no `mapUrl` (a revision traced before t3, or none at all) it renders
// nothing.
export type NibEditorProps = { revisionId: string; mapUrl?: string | null; onSaved?: (revision: Revision) => void }

type Notice = { role: 'status' | 'alert'; text: string; problems?: string[] }
const MAP_NAME = 'map.json'

async function fetchMap(url: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(url, signal ? { signal } : undefined)
  if (!response.ok) throw new Error(`the map could not be loaded (${response.status})`)
  return response.text()
}

export function NibEditor({ revisionId, mapUrl, onSaved }: NibEditorProps) {
  const root = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const mounted = useRef<MountedEditor | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<Notice | null>(null)
  // The latest props the host reads when nib calls it, without remounting the editor.
  const latest = useRef({ revisionId, mapUrl, onSaved })
  latest.current = { revisionId, mapUrl, onSaved }

  useEffect(() => {
    const element = canvas.current
    if (!mapUrl || !element) return
    const controller = new AbortController()
    let current: MountedEditor | null = null
    const host: Host = {
      open: async () => {
        const url = latest.current.mapUrl
        return url ? { name: MAP_NAME, text: await fetchMap(url), kind: 'label-map' } : null
      },
      save: async (_name, text, kind): Promise<SaveResult> => {
        // Only the planar map goes back to the server; nib's SVG exports have no place there.
        if (kind !== 'label-map') return 'cancelled'
        try {
          const saved = await api.saveMap(latest.current.revisionId, text)
          if ('refusal' in saved) {
            const { refusal } = saved
            if (refusal.reason === 'unchanged') { setNotice({ role: 'status', text: refusal.detail }); return 'empty' }
            setNotice({ role: 'alert', text: refusal.detail, problems: refusal.problems })
            return 'cancelled'
          }
          setNotice({ role: 'status', text: 'Saved as a new trace revision.' })
          latest.current.onSaved?.(saved.revision)
          return 'saved'
        } catch (cause) {
          setNotice({ role: 'alert', text: `Could not save the map: ${cause instanceof Error ? cause.message : String(cause)}` })
          return 'cancelled'
        }
      },
    }
    setLoaded(false)
    fetchMap(mapUrl, controller.signal).then(text => {
      if (controller.signal.aborted) return
      current = mountEditor(element, { host, text, kind: 'label-map' })
      mounted.current = current
      return current.loaded.then(() => { if (!controller.signal.aborted) setLoaded(true) })
    }).catch(cause => {
      if (!controller.signal.aborted) setNotice({ role: 'alert', text: `Could not open the map: ${cause instanceof Error ? cause.message : String(cause)}` })
    })
    return () => {
      controller.abort()
      current?.dispose()
      if (mounted.current === current) mounted.current = null
    }
  }, [mapUrl])

  // The canvas is nib's: a wheel over it zooms nib's view, never the canvas behind it.
  useEffect(() => {
    const element = root.current
    if (!element) return
    const wheel = (event: WheelEvent) => event.stopPropagation()
    element.addEventListener('wheel', wheel)
    return () => element.removeEventListener('wheel', wheel)
  }, [mapUrl])

  async function save() {
    const editor = mounted.current?.editor
    if (!editor || saving) return
    setSaving(true)
    setNotice(null)
    try { await editor.saveDocument() } finally { setSaving(false) }
  }

  if (!mapUrl) return null
  return <div ref={root} className="nib-editor" onPointerDown={event => event.stopPropagation()}>
    <canvas ref={canvas} className="nib-editor-canvas" aria-label="Planar map editor" />
    <div className="nib-editor-bar">
      <button type="button" disabled={!loaded || saving} onClick={save}>{saving ? 'Saving…' : 'Save as new revision'}</button>
      {!loaded && !notice && <span className="nib-editor-note">Loading the map…</span>}
      {notice && <div className={`nib-editor-note${notice.role === 'alert' ? ' error' : ''}`} role={notice.role}>
        {notice.text}
        {notice.problems && notice.problems.length > 0 && <ul>{notice.problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
      </div>}
    </div>
  </div>
}

export default NibEditor
