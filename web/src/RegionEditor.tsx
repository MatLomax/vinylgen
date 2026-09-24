import { useId } from 'react'
import { type PaletteColour } from './api'

export function RegionEditor({ palette, removedIndex = null, selectionLabels, neighbourLabels, onPick, onClear, onUndo, canUndo, busy, error, disabled = false, disabledHint, lockedHint }: {
  palette: PaletteColour[]
  // The palette index of the colour the current cleanup removes: recolouring to it erases.
  removedIndex?: number | null
  selectionLabels: number[]
  neighbourLabels: number[]
  onPick: (label: number) => void
  onClear: () => void
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
  const count = selectionLabels.length
  const neighbours = new Set(neighbourLabels)
  const locked = disabled || busy
  // Why the controls are disabled, named on each of them.
  const hintId = useId()
  const hint = lockedHint ?? (disabled ? disabledHint : undefined)
  const describedBy = hint ? hintId : undefined
  const inert = lockedHint ? true : undefined

  return <div className="region-editor">
    <div className="region-editor-head">
      <h2>Edit selected areas</h2>
      <button type="button" className="secondary" disabled={!canUndo || locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : onUndo}>Undo</button>
    </div>
    <p className="section-intro">Click an area on the flattened artwork to select it; click it again to deselect. Choosing a swatch reassigns every selected area — a colour that already touches the selection dissolves it in, any other colour recolours it. Each change is a recoverable revision.</p>

    {busy && <p className="region-busy" role="status" aria-live="polite"><span className="spinner spinner-inline" aria-hidden="true" />Replacing colour… you can keep selecting areas.</p>}

    {hint && <p id={hintId} className="region-selection-status" role="status" aria-live="polite">{hint}</p>}
    {!hint && <p className="region-selection-status" role="status" aria-live="polite">{count === 0 ? 'No areas selected yet.' : `${count} area${count === 1 ? '' : 's'} selected.`}</p>}

    {palette.length === 0 ? <p>Loading palette…</p> : <>
      <div className="region-swatch-grid" role="group" aria-label="Apply colour to selected areas">
        {palette.map(colour => {
          const dissolves = neighbours.has(colour.index)
          const noop = count > 0 && selectionLabels.every(label => label === colour.index)
          const swatchDisabled = locked || count === 0 || noop
          const verb = dissolves ? 'Dissolve selection into' : 'Recolour selection to'
          const erases = colour.index === removedIndex
          return <button key={colour.index} type="button" className={`region-swatch ${dissolves ? 'is-neighbour' : ''}`}
            disabled={swatchDisabled} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : () => onPick(colour.index)}
            aria-label={`${verb} ${colour.name} ${colour.hex}${erases ? ', removed in Cleanup: erases' : ''}`} title={erases ? `${verb} ${colour.name}: Cleanup removes it, so this erases` : `${verb} ${colour.name}`}>
            <span className="region-swatch-colour" style={{ background: colour.hex }} aria-hidden="true" />
            <span className="region-swatch-text">
              <strong>{colour.name}</strong>
              <small>{erases ? 'erase' : dissolves ? 'dissolve' : 'recolour'} · {colour.share_percent}%</small>
            </span>
          </button>
        })}
      </div>
      <button type="button" className="secondary" disabled={count === 0 || locked} aria-disabled={inert} aria-describedby={describedBy} onClick={inert ? undefined : onClear}>Clear selection</button>
    </>}

    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
