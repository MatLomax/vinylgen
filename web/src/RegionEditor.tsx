import { type PaletteColour } from './api'

export function RegionEditor({ palette, selectionLabels, neighbourLabels, onPick, onClear, onUndo, canUndo, busy, error }: {
  palette: PaletteColour[]
  selectionLabels: number[]
  neighbourLabels: number[]
  onPick: (label: number) => void
  onClear: () => void
  onUndo: () => void
  canUndo: boolean
  busy: boolean
  error?: string
}) {
  const count = selectionLabels.length
  const neighbours = new Set(neighbourLabels)

  return <div className="region-editor">
    <div className="region-editor-head">
      <h2>Edit selected areas</h2>
      <button type="button" className="secondary" disabled={!canUndo || busy} onClick={onUndo}>Undo</button>
    </div>
    <p className="section-intro">Click an area on the flattened artwork to select it; click it again to deselect. Choosing a swatch reassigns every selected area — a colour that already touches the selection dissolves it in, any other colour recolours it. Each change is a recoverable revision.</p>

    <p className="region-selection-status" role="status" aria-live="polite">{count === 0 ? 'No areas selected yet.' : `${count} area${count === 1 ? '' : 's'} selected.`}</p>

    {palette.length === 0 ? <p>Loading palette…</p> : <>
      <div className="region-swatch-grid" role="group" aria-label="Apply colour to selected areas">
        {palette.map(colour => {
          const dissolves = neighbours.has(colour.index)
          const noop = count > 0 && selectionLabels.every(label => label === colour.index)
          const disabled = busy || count === 0 || noop
          const verb = dissolves ? 'Dissolve selection into' : 'Recolour selection to'
          return <button key={colour.index} type="button" className={`region-swatch ${dissolves ? 'is-neighbour' : ''}`}
            disabled={disabled} onClick={() => onPick(colour.index)}
            aria-label={`${verb} ${colour.name} ${colour.hex}`} title={`${verb} ${colour.name}`}>
            <span className="region-swatch-colour" style={{ background: colour.hex }} aria-hidden="true" />
            <span className="region-swatch-text">
              <strong>{colour.name}</strong>
              <small>{dissolves ? 'dissolve' : 'recolour'} · {colour.share_percent}%</small>
            </span>
          </button>
        })}
      </div>
      <button type="button" className="secondary" disabled={count === 0 || busy} onClick={onClear}>Clear selection</button>
    </>}

    {error && <p className="error" role="alert">{error}</p>}
  </div>
}
