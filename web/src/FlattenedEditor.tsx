import { useEffect, useId, useState } from 'react'
import { HexColorPicker, HexColorInput } from 'react-colorful'
import { api, type Artifact, type EditorState } from './api'
import { hexToRgb } from './regionSelection'

export function FlattenedEditor({ projectId, revisionId, flattened: _flattened, onCommit, onPreview, onPreviewEnd, disabled = false, disabledHint, lockedHint }: { projectId: string; revisionId: string; flattened: Artifact; onCommit: (id: string) => void; onPreview?: (index: number, hex: string) => void; onPreviewEnd?: () => void; disabled?: boolean; disabledHint?: string; lockedHint?: string }) {
  const [state, setState] = useState<EditorState | null>(null)
  const [names, setNames] = useState<Record<number, string>>({})
  const [source, setSource] = useState(0)
  const [target, setTarget] = useState(1)
  const [editing, setEditing] = useState<number | null>(null)
  const [draftHex, setDraftHex] = useState('#000000')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Why the controls are disabled, named on each of them.
  const hintId = useId()

  useEffect(() => {
    const controller = new AbortController()
    api.editor(revisionId, controller.signal).then(next => {
      setState(next); setNames(Object.fromEntries(next.palette.map(colour => [colour.index, colour.name])))
      setSource(0); setTarget(Math.min(1, next.palette.length - 1)); setEditing(null)
    }).catch(cause => { if (!controller.signal.aborted) setError(String(cause)) })
    return () => controller.abort()
  }, [revisionId])

  // Dropping the live preview when the picker closes keeps the main canvas in sync
  // with what is actually committed, so a cancelled edit leaves no ghost recolour.
  useEffect(() => { if (editing === null) onPreviewEnd?.() }, [editing, onPreviewEnd])

  function openPicker(colour: { index: number; hex: string }) {
    onPreviewEnd?.()
    setEditing(colour.index); setDraftHex(colour.hex)
  }

  function previewHex(index: number, hex: string) {
    setDraftHex(hex); onPreview?.(index, hex)
  }

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

  async function recolour(index: number) {
    setBusy(true); setError('')
    try {
      const [r, g, b] = hexToRgb(draftHex)
      const next = await api.applyEdits(projectId, revisionId, [{ tool: 'recolour_colour', label: index, rgb: [r, g, b] }])
      setEditing(null)
      onCommit(next.id)
    } catch (cause) { setError(String(cause)) }
    finally { setBusy(false) }
  }

  if (!state) return <section className="palette-editor"><h2>Palette</h2><p role={error ? 'alert' : 'status'}>{error || 'Loading palette…'}</p></section>
  const sourceColour = state.palette[source]
  const targetColour = state.palette[target]
  const locked = busy || disabled
  // `lockedHint`: editing is locked for a reason the user can undo (comparing on the
  // canvas); the controls stay focusable but inert, marked aria-disabled and described by it.
  const hint = lockedHint ?? (disabled ? disabledHint : undefined)
  const describedBy = hint ? hintId : undefined
  const inert = lockedHint ? true : undefined
  return <section className="palette-editor" aria-labelledby="palette-editor-title">
    <div className="section-heading"><h2 id="palette-editor-title">Palette</h2><span>{state.palette.length} colours</span></div>
    {state.requested_colour_count !== null && state.palette.length < state.requested_colour_count && <p className="stage-guidance" role="status">Produced {state.palette.length} of {state.requested_colour_count} requested — the image couldn't sustain {state.requested_colour_count - state.palette.length === 1 ? 'the last distinct colour' : `${state.requested_colour_count - state.palette.length} more distinct colours`}.</p>}
    <p id={hintId} className="stage-guidance" role="status">{hint ?? 'The flattened image stays on the main canvas. Pick a swatch to recolour a whole colour—changes preview live and apply as a new revision.'}</p>
    <div className="palette-list">{state.palette.map(colour => <div key={colour.index}>
      <button type="button" className="swatch swatch-button" style={{ background: colour.hex }} aria-label={`Recolour ${names[colour.index] ?? colour.name}`} aria-expanded={editing === colour.index} disabled={locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : () => editing === colour.index ? setEditing(null) : openPicker(colour)} />
      <label><span className="visually-hidden">Name for {colour.hex}</span><input value={names[colour.index] ?? colour.name} disabled={locked} readOnly={inert} aria-disabled={inert} aria-describedby={describedBy} onChange={event => setNames(current => ({ ...current, [colour.index]: event.target.value }))} onBlur={event => !inert && event.target.value.trim() !== colour.name && void rename(colour.index, event.target.value)} /></label>
      <span className="palette-value">{colour.hex}<small>{colour.share_percent.toFixed(2)}%</small></span>
      {editing === colour.index && <div className="colour-picker" role="group" aria-label={`Recolour ${colour.name}`}>
        <HexColorPicker color={draftHex} onChange={hex => previewHex(colour.index, hex)} />
        <div className="colour-picker-row"><HexColorInput prefixed color={draftHex} onChange={hex => previewHex(colour.index, hex)} aria-label="Hex colour value" /></div>
        <div className="colour-picker-actions">
          <button type="button" className="secondary" disabled={busy} onClick={() => setEditing(null)}>Cancel</button>
          <button type="button" disabled={locked || draftHex.toLowerCase() === colour.hex.toLowerCase()} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : () => void recolour(colour.index)}>{busy ? 'Saving…' : 'Apply colour'}</button>
        </div>
      </div>}
    </div>)}</div>
    {state.palette.length > 1 && <fieldset className="merge-colours" disabled={locked}>
      <legend>Merge complete colours</legend>
      <label>From<select value={source} onChange={event => setSource(Number(event.target.value))}>{state.palette.map(colour => <option key={colour.index} value={colour.index}>{colour.name} · {colour.hex}</option>)}</select></label>
      <label>Into<select value={target} onChange={event => setTarget(Number(event.target.value))}>{state.palette.map(colour => <option key={colour.index} value={colour.index}>{colour.name} · {colour.hex}</option>)}</select></label>
      <p>{source === target ? 'Choose two different colours.' : <>Every <strong>{sourceColour?.name}</strong> pixel will become <strong>{targetColour?.name}</strong>. A new revision will be created.</>}</p>
      <button disabled={locked || source === target} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : () => void merge()}>{busy ? 'Saving…' : `Merge ${sourceColour?.name} into ${targetColour?.name}`}</button>
    </fieldset>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>
}
