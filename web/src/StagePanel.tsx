import { useEffect, useId, useRef, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'
import { ChevronDown, ChevronUp, PanelRightClose, PanelRightOpen } from 'lucide-react'
import type { PanelMode } from './shellLayout'

// A drawer drag shorter than this is a tap.
const DRAG_THRESHOLD = 8
// A pointer click this soon after a drag ends is the drag's own click (a mouse sends one;
// a touch drag does not), not a tap. Keyboard and assistive-technology clicks (detail 0)
// always toggle.
const DRAG_CLICK_WINDOW_MS = 400

/**
 * The stage settings panel. Beside the canvas (`push`) and over it (`overlay`) it closes
 * to a thin edge holding its Show button; under a phone's canvas (`drawer`) it collapses
 * to its handle, beside the stage's run button when it has one (`peekAction`), and a
 * tap or a vertical drag on the handle opens and closes it; as a whole phone
 * surface (`page`) it is always open. A closed panel keeps its contents mounted, hidden,
 * so unsaved form input survives. Escape closes an overlay or drawer; focus follows the
 * toggle that replaced the one pressed.
 */
export function StagePanel({ mode, open, onOpenChange, stageLabel, peekAction, children }: {
  mode: PanelMode
  open: boolean
  onOpenChange: (open: boolean) => void
  stageLabel: string
  peekAction?: { label: string; disabled: boolean; run: () => void; describedBy?: string }
  children: ReactNode
}) {
  const bodyId = useId()
  const toggle = useRef<HTMLButtonElement>(null)
  const refocus = useRef(false)
  const drag = useRef<{ y: number; moved: boolean } | null>(null)
  const dragEnded = useRef(-Infinity)
  const shown = mode === 'page' || open

  useEffect(() => {
    if (!refocus.current) return
    refocus.current = false
    toggle.current?.focus()
  }, [open])

  function change(next: boolean) {
    if (next === open) return
    refocus.current = true
    onOpenChange(next)
  }

  function keyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== 'Escape' || event.defaultPrevented || !open || (mode !== 'overlay' && mode !== 'drawer')) return
    event.preventDefault()
    change(false)
  }

  function dragStart(event: PointerEvent<HTMLButtonElement>) {
    dragEnded.current = -Infinity
    drag.current = { y: event.clientY, moved: false }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }
  function dragMove(event: PointerEvent<HTMLButtonElement>) {
    if (drag.current && Math.abs(event.clientY - drag.current.y) > DRAG_THRESHOLD) drag.current.moved = true
  }
  function dragEnd(event: PointerEvent<HTMLButtonElement>) {
    const start = drag.current
    drag.current = null
    if (!start?.moved) return
    dragEnded.current = event.timeStamp
    change(event.clientY < start.y)
  }

  const expanded = { 'aria-expanded': shown, 'aria-controls': bodyId }
  let bar: ReactNode = null
  if (mode === 'drawer') {
    bar = <div className="drawer-bar">
      <button ref={toggle} type="button" className="drawer-handle" {...expanded}
        onPointerDown={dragStart} onPointerMove={dragMove} onPointerUp={dragEnd} onPointerCancel={() => { drag.current = null }}
        onClick={event => { if (event.detail === 0 || event.timeStamp - dragEnded.current > DRAG_CLICK_WINDOW_MS) change(!open) }}>
        <span className="drawer-grip" aria-hidden="true" />
        <strong>{stageLabel} settings</strong>
        {open ? <ChevronDown aria-hidden="true" /> : <ChevronUp aria-hidden="true" />}
      </button>
      {/* Open, the settings show the same button in place. */}
      {!open && peekAction && <button type="button" className="drawer-action" disabled={peekAction.disabled} aria-describedby={peekAction.describedBy} onClick={peekAction.run}>{peekAction.label}</button>}
    </div>
  } else if (mode !== 'page') {
    bar = open
      ? <div className="stage-panel-bar"><strong>{stageLabel}</strong><button ref={toggle} type="button" className="panel-toggle" aria-label="Hide stage settings" title="Hide stage settings" {...expanded} onClick={() => change(false)}><PanelRightClose aria-hidden="true" /></button></div>
      : <button ref={toggle} type="button" className="panel-toggle panel-edge-open" aria-label="Show stage settings" title={`Show ${stageLabel} settings`} {...expanded} onClick={() => change(true)}><PanelRightOpen aria-hidden="true" /></button>
  }

  return <aside className={`stage-panel panel-${mode} ${shown ? 'is-open' : 'is-closed'}`} aria-label="Stage settings" onKeyDown={keyDown}>
    {bar}
    <div id={bodyId} className="stage-panel-body" hidden={!shown}>{children}</div>
  </aside>
}
