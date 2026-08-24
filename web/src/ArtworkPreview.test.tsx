// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ArtworkPreview } from './ArtworkPreview'

const layers = [{ id: 'source', name: 'Source image', url: '/source.png', mediaType: 'image/png', role: 'image' as const }]
afterEach(cleanup)

describe('passive artwork preview and inspector lifecycle', () => {
  it('keeps comparison controls out of the passive page until Inspect is requested', () => {
    render(<ArtworkPreview layers={layers} />)
    expect(screen.getByRole('img', { name: 'Source image' })).toBeTruthy()
    expect(screen.queryByLabelText('Comparison mode')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Inspect image' }))
    expect(screen.getByRole('dialog', { name: 'Artwork inspector' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Close inspector' })).toBeTruthy()
    expect(screen.getAllByLabelText('Comparison mode')).toHaveLength(2)
  })

  it('closes with Escape and restores focus to the opener', async () => {
    render(<ArtworkPreview layers={layers} />)
    const opener = screen.getByRole('button', { name: 'Inspect image' })
    fireEvent.click(opener)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close inspector' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    await new Promise(resolve => requestAnimationFrame(resolve))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('Escape closes the controls sheet before the containing inspector', () => {
    render(<ArtworkPreview layers={layers} />)
    fireEvent.click(screen.getByRole('button', { name: 'Inspect image' }))
    fireEvent.click(screen.getByRole('button', { name: 'Controls' }))
    expect(document.querySelector('.bottom-sheet')?.classList.contains('open')).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.querySelector('.bottom-sheet')?.classList.contains('open')).toBe(false)
    expect(screen.getByRole('dialog', { name: 'Artwork inspector' })).toBeTruthy()
  })
})
