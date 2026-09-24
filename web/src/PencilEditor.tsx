import { useId } from 'react'
import { type PaletteColour } from './api'

export function PencilEditor({ palette, removedIndex = null, activeLabel, onPickColour, paintedCount, onApply, onDiscard, onUndo, canUndo, busy, error, disabled = false, disabledHint, lockedHint }: {
  palette: PaletteColour[]
  // The palette index of the colour the current cleanup removes: painting it erases.
  removedIndex?: number | null
  activeLabel: number | null
  onPickColour: (label: number) => void
  paintedCount: number
  onApply: () => void
  onDiscard: () => void
  onUndo: () => void
  canUndo: boolean
  busy: boolean
  error?: string
  disabled?: boolean
  disabledHint?: string
  // Set while editing is locked for a reason the user can undo (comparing on the canvas):
  // the controls stay focusable but inert, marked aria-disabled and described by it.
  lockedHint?: string
}) {
  const locked = disabled || busy
  // Why the controls are disabled, named on each of them.
  const hintId = useId()
  const hint = lockedHint ?? (disabled ? disabledHint : undefined)
  const describedBy = hint ? hintId : undefined
  const inert = lockedHint ? true : undefined

  return <div className="region-editor pencil-editor">
    <div className="region-editor-head">
      <h2>Pencil</h2>
      <button type="button" className="secondary" disabled={!canUndo || locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : onUndo}>Undo</button>
    </div>
    <p className="section-intro">Pick a colour, then drag on the artwork to paint individual pixels. Painting through an area splits it wherever it stops being connected. Apply your strokes to record a recoverable revision.</p>

    {hint && <p id={hintId} className="region-selection-status" role="status" aria-live="polite">{hint}</p>}
    {!hint && <p className="region-selection-status" role="status" aria-live="polite">{paintedCount === 0 ? 'No pixels painted yet.' : `${paintedCount} pixel${paintedCount === 1 ? '' : 's'} painted, not yet applied.`}</p>}

    {palette.length === 0 ? <p>Loading palette…</p> : <>
      <div className="region-swatch-grid" role="radiogroup" aria-label="Pencil colour">
        {palette.map(colour => {
          const active = activeLabel === colour.index
          const erases = colour.index === removedIndex
          return <button key={colour.index} type="button" role="radio" aria-checked={active}
            className={`region-swatch ${active ? 'is-active' : ''}`} disabled={locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : () => onPickColour(colour.index)}
            aria-label={`Paint with ${colour.name} ${colour.hex}${erases ? ', removed in Cleanup: erases' : ''}`} title={erases ? `Paint with ${colour.name}: Cleanup removes it, so painting it erases` : `Paint with ${colour.name}`}>
            <span className="region-swatch-colour" style={{ background: colour.hex }} aria-hidden="true" />
            <span className="region-swatch-text">
              <strong>{colour.name}</strong>
              <small>{erases ? (active ? 'erasing' : 'erase') : active ? 'painting' : 'paint'} · {colour.share_percent}%</small>
            </span>
          </button>
        })}
      </div>
      <div className="pencil-actions">
        <button type="button" disabled={paintedCount === 0 || locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : onApply}>Apply pencil edits</button>
        <button type="button" className="secondary" disabled={paintedCount === 0 || locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : onDiscard}>Discard</button>
      </div>
    </>}

    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
