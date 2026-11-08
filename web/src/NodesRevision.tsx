import type { Revision, TraceSettings } from './api'
import { CURVE_KINDS, DEFAULT_TRACE_SETTINGS, normalizeTraceSettings } from './PipelineControls'

// The half-widths a trace revision ran at, one entry per curve kind in the Curve kinds order:
// its recorded `settings.trace` over the defaults, so a revision traced before the per-kind
// table (all at w 3.5) reads as every kind at 1.75. `null` is a kind that was off.
export function revisionHalfWidths(revision: Revision): { label: string; half: number | null }[] {
  const trace = revisionTrace(revision)
  return CURVE_KINDS.map(({ key, label }) => ({ label, half: trace[key] }))
}

// A trace revision's recorded `settings.trace` over the defaults.
function revisionTrace(revision: Revision): TraceSettings {
  const stored = revision.settings.trace
  return { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings(stored && typeof stored === 'object' ? stored as Record<string, unknown> : {}) }
}

// One line naming a trace revision's half-widths: "Lines 1 px · Circular arcs off · …",
// followed by its arc minimum sweeps, "min sweep arc A, ellipse E" in degrees, only when either
// is above 0 (at the default the line names the half-widths alone).
export function halfWidthsLine(revision: Revision): string {
  const halves = revisionHalfWidths(revision).map(({ label, half }) => `${label} ${half === null ? 'off' : `${half} px`}`).join(' · ')
  const { min_sweep_arc: arc, min_sweep_ellipse: ellipse } = revisionTrace(revision)
  return arc > 0 || ellipse > 0 ? `${halves} · min sweep arc ${arc}, ellipse ${ellipse}` : halves
}

// The Nodes stage's revision bar: which trace run nib's editor has open, stepped like the Trace
// stage's runs (`runs` newest first, `index` the open one's place; a step makes that run the
// selected revision), and what that revision is: its message, when it was made, and the
// half-widths it was traced at. Stepping is held while `locked` says why (an unsaved edit).
export function NodesRevision({ runs, index, onStep, locked = null }: {
  runs: Revision[]; index: number; onStep: (delta: number) => void; locked?: string | null
}) {
  const shown = index >= 0 ? runs[index] : undefined
  if (!shown) return null
  const count = runs.length
  return <div className="nodes-revision">
    <div className="iteration-stepper" role="group" aria-label="Step through trace runs">
      <button type="button" className="secondary" aria-label="Open the previous trace run" disabled={Boolean(locked) || index >= count - 1} title={locked ?? undefined} onClick={() => onStep(1)}>◀</button>
      <span className="iteration-indicator">{`Trace ${count - index} of ${count}`}</span>
      <button type="button" className="secondary" aria-label="Open the next trace run" disabled={Boolean(locked) || index <= 0} title={locked ?? undefined} onClick={() => onStep(-1)}>▶</button>
    </div>
    <p className="nodes-revision-detail">
      <strong>{shown.message || 'Trace'}</strong>{' · '}<time dateTime={shown.created_at}>{new Date(shown.created_at).toLocaleString()}</time>
      <br /><span className="nodes-revision-halves">{halfWidthsLine(shown)}</span>
    </p>
    {locked && <p className="stage-guidance">{locked}</p>}
  </div>
}
