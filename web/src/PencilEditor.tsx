import { type PaletteColour } from './api'

export function PencilEditor({ palette, activeLabel, onPickColour, paintedCount, onApply, onDiscard, onUndo, canUndo, busy, error, disabled = false, disabledHint }: {
  palette: PaletteColour[]
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
}) {
  const locked = disabled || busy

  return <div className="region-editor pencil-editor">
    <div className="region-editor-head">
      <h2>Pencil</h2>
      <button type="button" className="secondary" disabled={!canUndo || locked} onClick={onUndo}>Undo</button>
    </div>
    <p className="section-intro">Pick a colour, then drag on the artwork to paint individual pixels. Painting through an area splits it wherever it stops being connected. Apply your strokes to record a recoverable revision.</p>

    {disabled && disabledHint && <p className="region-selection-status" role="status" aria-live="polite">{disabledHint}</p>}
    {!disabled && <p className="region-selection-status" role="status" aria-live="polite">{paintedCount === 0 ? 'No pixels painted yet.' : `${paintedCount} pixel${paintedCount === 1 ? '' : 's'} painted, not yet applied.`}</p>}

    {palette.length === 0 ? <p>Loading palette…</p> : <>
      <div className="region-swatch-grid" role="radiogroup" aria-label="Pencil colour">
        {palette.map(colour => {
          const active = activeLabel === colour.index
          return <button key={colour.index} type="button" role="radio" aria-checked={active}
            className={`region-swatch ${active ? 'is-active' : ''}`} disabled={locked} onClick={() => onPickColour(colour.index)}
            aria-label={`Paint with ${colour.name} ${colour.hex}`} title={`Paint with ${colour.name}`}>
            <span className="region-swatch-colour" style={{ background: colour.hex }} aria-hidden="true" />
            <span className="region-swatch-text">
              <strong>{colour.name}</strong>
              <small>{active ? 'painting' : 'paint'} · {colour.share_percent}%</small>
            </span>
          </button>
        })}
      </div>
      <div className="pencil-actions">
        <button type="button" disabled={paintedCount === 0 || locked} onClick={onApply}>Apply pencil edits</button>
        <button type="button" className="secondary" disabled={paintedCount === 0 || locked} onClick={onDiscard}>Discard</button>
      </div>
    </>}

    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
