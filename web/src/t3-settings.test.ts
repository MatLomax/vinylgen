// Card 4 of vinylgen-on-t3 (tracer card `cards/vinylgen-on-t3/card.md` section 5): the trace
// preview is retired with the old tracer. No client source calls the trace-preview route or
// keeps useTracePreview; the cleanup preview's shared parts live on without them.
import { describe, expect, it } from 'vitest'

const sources = import.meta.glob(['./**/*.ts', './**/*.tsx', '!./**/*.test.ts', '!./**/*.test.tsx'], { query: '?raw', import: 'default', eager: true }) as Record<string, string>

describe('the trace preview is retired', () => {
  // Passes on 3fe9f57: it guards the scan below against an empty glob.
  it('scans the client sources', () => {
    expect(Object.keys(sources)).toContain('./PipelineControls.tsx')
  })

  it('has no useTracePreview and no call to the trace-preview route', () => {
    const holding = Object.entries(sources).filter(([, text]) => text.includes('useTracePreview') || text.includes('/trace-preview')).map(([path]) => path)
    expect(holding).toEqual([])
  })
})
