// The overlay used to run its own 80ms setInterval + setState braille ticker —
// the same mechanism class (per-tick DOM mutation scheduling a style recalc)
// that GlyphSpinner was rewritten to remove. It now renders GlyphSpinner, so
// what needs pinning is that no timer comes back, that the label still survives
// the fade-out, and that the spinner stops animating once the swap is done.
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatSwapOverlay, SWAP_OVERLAY_GRACE_MS } from './chat-swap-overlay'

afterEach(() => {
  cleanup()
})

describe('ChatSwapOverlay', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('never shows for a swap that settles inside the grace period', () => {
    // A warm bot switch paints its cached thread within a frame or two; the
    // overlay flashing over it read as a reload on every switch.
    const { container, rerender } = render(<ChatSwapOverlay profile="turqoise" />)

    act(() => {
      vi.advanceTimersByTime(SWAP_OVERLAY_GRACE_MS - 1)
    })
    rerender(<ChatSwapOverlay profile={null} />)
    act(() => {
      vi.advanceTimersByTime(SWAP_OVERLAY_GRACE_MS)
    })

    expect(container.querySelector('.opacity-100')).toBeNull()
    expect(screen.queryByText(/turqoise/)).toBeNull()
  })

  it('keeps the last profile name through the fade-out, with the glyph frozen', () => {
    const { container, rerender } = render(<ChatSwapOverlay profile="turqoise" />)

    // A real wake outlasts the grace period and shows the overlay.
    act(() => {
      vi.advanceTimersByTime(SWAP_OVERLAY_GRACE_MS)
    })

    expect(container.querySelector('.opacity-100')).not.toBeNull()
    expect(container.querySelector('.glyph-spinner')?.hasAttribute('data-paused')).toBe(false)

    rerender(<ChatSwapOverlay profile={null} />)

    // Label held so the overlay doesn't blank while it fades.
    expect(screen.getByText(/turqoise/)).toBeTruthy()
    // ...and the spinner stops, the way clearing the interval used to stop it.
    expect(container.querySelector('.glyph-spinner')?.getAttribute('data-paused')).toBe('true')
  })
})
