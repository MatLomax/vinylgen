import { useEffect, useState } from 'react'
import { api, type Artifact, type EditorState } from './api'

export function FlattenedEditor({ projectId, revisionId, flattened: _flattened, onCommit }: { projectId: string; revisionId: string; flattened: Artifact; onCommit: (id: string) => void }) {
  const [state, setState] = useState<EditorState | null>(null)
  const [names, setNames] = useState<Record<number, string>>({})
  const [source, setSource] = useState(0)
  const [target, setTarget] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    api.editor(revisionId, controller.signal).then(next => {
      setState(next); setNames(Object.fromEntries(next.palette.map(colour => [colour.index, colour.name])))
      setSource(0); setTarget(Math.min(1, next.palette.length - 1))
    }).catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    return () => controller.abort()
  }, [revisionId])

  async function merge() {
    if (!state || source === target) return
    setBusy(true); setError('')
    try {
      const next = await api.applyEdits(projectId, revisionId, [{ tool: 'merge_colour', source_label: source, target_label: target }])
      onCommit(next.id)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  async function rename(index: number, name: string) {
    const trimmed = name.trim()
    if (!trimmed) return
    setBusy(true); setError('')
    try {
      const next = await api.applyEdits(projectId, revisionId, [{ tool: 'rename_colour', label: index, name: trimmed }])
      onCommit(next.id)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  if (!state) return <section className="palette-editor"><h2>Palette</h2><p role={error ? 'alert' : 'status'}>{error || 'Loading palette…'}</p></section>
  const sourceColour = state.palette[source]
  const targetColour = state.palette[target]
  return <section className="palette-editor" aria-labelledby="palette-editor-title">
    <div className="section-heading"><h2 id="palette-editor-title">Palette</h2><span>{state.palette.length} colours</span></div>
    <p className="stage-guidance">The flattened image stays on the main canvas. Palette edits apply to complete colours—there are no painting tools.</p>
    <div className="palette-list">{state.palette.map(colour => <div key={colour.index}>
      <span className="swatch" style={{ background: colour.hex }} aria-hidden="true" />
      <label><span className="visually-hidden">Name for {colour.hex}</span><input value={names[colour.index] ?? colour.name} disabled={busy} onChange={event => setNames(current => ({ ...current, [colour.index]: event.target.value }))} onBlur={event => event.target.value.trim() !== colour.name && void rename(colour.index, event.target.value)} /></label>
      <span className="palette-value">{colour.hex}<small>{colour.share_percent.toFixed(2)}%</small></span>
    </div>)}</div>
    {state.palette.length > 1 && <fieldset className="merge-colours">
      <legend>Merge complete colours</legend>
      <label>From<select value={source} onChange={event => setSource(Number(event.target.value))}>{state.palette.map(colour => <option key={colour.index} value={colour.index}>{colour.name} · {colour.hex}</option>)}</select></label>
      <label>Into<select value={target} onChange={event => setTarget(Number(event.target.value))}>{state.palette.map(colour => <option key={colour.index} value={colour.index}>{colour.name} · {colour.hex}</option>)}</select></label>
      <p>{source === target ? 'Choose two different colours.' : <>Every <strong>{sourceColour?.name}</strong> pixel will become <strong>{targetColour?.name}</strong>. A new revision will be created.</>}</p>
      <button disabled={busy || source === target} onClick={() => void merge()}>{busy ? 'Saving…' : `Merge ${sourceColour?.name} into ${targetColour?.name}`}</button>
    </fieldset>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
