import { useEffect, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import { ImageWorkspace, type WorkspaceLayer } from './ImageWorkspace'

export function ArtworkPreview({ layers, preferredLayerId }: { layers: WorkspaceLayer[]; preferredLayerId?: string }) {
  const [open, setOpen] = useState(false)
  const openButton = useRef<HTMLButtonElement>(null)
  const images = layers.filter(layer => layer.role === 'image')
  const preview = images.find(layer => layer.id === preferredLayerId) ?? images[0]

  function close() {
    setOpen(false)
    requestAnimationFrame(() => openButton.current?.focus())
  }

  useEffect(() => {
    if (!open) return
    const shell = document.querySelector<HTMLElement>('.inspector-shell')
    const isolated: Array<{ element: HTMLElement; inert: string | null }> = []
    let branch = shell
    while (branch?.parentElement) {
      const parent = branch.parentElement
      for (const sibling of [...parent.children]) {
        if (sibling instanceof HTMLElement && sibling !== branch) {
          isolated.push({ element: sibling, inert: sibling.getAttribute('inert') }); sibling.setAttribute('inert', '')
        }
      }
      if (parent === document.body) break
      branch = parent
    }
    shell?.querySelector<HTMLButtonElement>('.inspector-close')?.focus()
    const keyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return
      if (event.key === 'Escape' && shell?.querySelector('.bottom-sheet.open')) return
      if (event.key === 'Escape') return close()
      if (event.key !== 'Tab' || !shell) return
      const focusable = [...shell.querySelectorAll<HTMLElement>('button:not(:disabled), select:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')]
      const first = focusable[0], last = focusable.at(-1)
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', keyDown)
    return () => {
      document.removeEventListener('keydown', keyDown)
      isolated.forEach(({ element, inert }) => inert === null ? element.removeAttribute('inert') : element.setAttribute('inert', inert))
    }
  }, [open])

  if (!preview) return <div className="image-workspace empty-workspace"><span>◆</span><h2>Image workspace</h2><p>Import an image to inspect it here.</p></div>
  return <>
    <figure className="artwork-preview">
      <img src={preview.url} alt={preview.name} />
      <button ref={openButton} className="inspect-trigger" type="button" aria-label="Inspect image" title="Inspect image" onClick={() => setOpen(true)}><Search aria-hidden="true" /></button>
    </figure>
    {open && <div className="inspector-shell" role="dialog" aria-modal="true" aria-label="Artwork inspector">
      <ImageWorkspace layers={layers} preferredLayerId={preview.id} onClose={close} />
    </div>}
  </>
}
