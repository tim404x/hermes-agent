/**
 * Grok Bot faces: the appearance string contract, the first-frame render, and
 * the mood animation driven by the shared face clock.
 *
 * `grok:<shape>[:<expression>]` is stored per bot (profile ui_meta) and must
 * round-trip forever; unknown shapes are rejected (the bot falls back to its
 * classic face) while an unknown expression degrades to neutral, because the
 * body is the identity and the expression only a mood on top.
 *
 * The moods are the point of the feature: idle keeps the slit eyes, think turns
 * the body into the three pulsing dots, work spins the orbit rings. Each is
 * asserted on the painted DOM, not on engine internals.
 */

import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return {
    atom,
    blobatarSvg: undefined,
    createBudgetedLoop: undefined,
    host: { state: { connectionId: { get: () => 'local' } } },
    profileColor: () => '#8b5cf6',
    PROFILE_SWATCHES: ['#8b5cf6'],
    queryClient: undefined,
    useQuery: vi.fn(),
    useValue: vi.fn()
  }
})

vi.mock('./shared', () => ({ getPluginCtx: () => null, ID: 'hermes-bots' }))

const { BotFace } = await import('./avatar')
const { formatGrokShape, paintGrokFace, parseGrokShape } = await import('./grok-face')

function face(shape: string, color = '#ff9800', mood: 'idle' | 'think' | 'work' = 'idle') {
  const { container } = render(<BotFace color={color} mood={mood} name="delta" shape={shape} size={36} />)

  return container.querySelector('svg') as SVGSVGElement
}

/** Eye holes are the black paths punched into the body mask. */
function eyeHoles(svg: SVGSVGElement) {
  return [...svg.querySelectorAll('mask path[fill="#000"]')].filter(p => Number(p.getAttribute('opacity') ?? 1) > 0.05)
}

describe('grok appearance strings', () => {
  it('round-trips shape and expression', () => {
    expect(parseGrokShape('grok:triangle:proud')).toEqual({ expression: 'proud', shape: 'triangle' })
    expect(formatGrokShape('triangle', 'proud')).toBe('grok:triangle:proud')
    expect(formatGrokShape('circle')).toBe('grok:circle')
    expect(parseGrokShape(formatGrokShape('circle'))).toEqual({ expression: 'neutral', shape: 'circle' })
  })

  it('rejects unknown bodies but forgives unknown expressions', () => {
    expect(parseGrokShape('grok:dodecahedron')).toBeNull()
    expect(parseGrokShape('squircle')).toBeNull()
    expect(parseGrokShape(null)).toBeNull()
    expect(parseGrokShape('grok:pebble:ecstatic')).toEqual({ expression: 'neutral', shape: 'pebble' })
  })
})

describe('grok face render', () => {
  it('draws a complete first frame in the bot colour, tagged for the face clock and the PNG backfill', () => {
    const svg = face('grok:hexagon', '#ff6700')

    expect(svg.getAttribute('data-hb-math')).toBe('1')
    expect(svg.getAttribute('data-hb-grok')).toBe('grok:hexagon')
    expect(svg.getAttribute('data-bot-face')).toBe('delta')
    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe('#ff6700')
    expect(eyeHoles(svg)).toHaveLength(2)
  })

  it('cuts the eyes out as holes: no light under-layer anywhere outside the mask', () => {
    // A white copy of the body under the colour used to leak a pale anti-aliased rim
    // around every face on a dark sidebar. Eyes are holes now (xAI's look): the only
    // paint is the colour, clipped by the body mask the eyes are punched into.
    for (const shape of [
      'grok:squircle',
      'grok:pebble',
      'grok:hexagon',
      'grok:circle',
      'grok:triangle',
      'grok:cloud'
    ]) {
      const svg = face(shape, '#9159fe')
      const painted = [...svg.querySelectorAll('path, rect, circle')].filter(el => !el.closest('mask, defs'))

      expect(eyeHoles(svg), shape).toHaveLength(2)
      expect(painted.length, shape).toBeGreaterThan(0)
      expect(
        painted.filter(el => el.getAttribute('fill') !== '#9159fe').map(el => el.outerHTML.slice(0, 80)),
        shape
      ).toEqual([])
    }
  })

  it('crops the slot so the body fills about 88% of it, like the reference', () => {
    // The circle body has radius 100 engine units; bloub's own still crop (125) left it at
    // 80% of the slot and xAI's render fills 88%. Centred, so the slot stays symmetric.
    const svg = face('grok:circle')
    const [x, y, w, h] = (svg.getAttribute('viewBox') ?? '').split(' ').map(Number)

    expect(x).toBe(-w! / 2)
    expect(y).toBe(-h! / 2)
    expect(200 / w!).toBeGreaterThan(0.86)
    expect(200 / w!).toBeLessThan(0.9)
  })

  it('recolours from the clock when the colour changes, without a remount', () => {
    const svg = face('grok:circle', '#1084fe')
    svg.setAttribute('data-hb-ink', '#e152b0')
    paintGrokFace(svg, 1)

    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe('#e152b0')
  })
})

describe('grok face moods', () => {
  it('idle keeps the slit eyes open on the body', () => {
    const svg = face('grok:squircle')
    paintGrokFace(svg, 10)
    paintGrokFace(svg, 11)

    expect(eyeHoles(svg)).toHaveLength(2)
    expect(svg.querySelectorAll('linearGradient')).toHaveLength(0)
  })

  it('think turns the body into the three pulsing dots, eyes gone', () => {
    const svg = face('grok:squircle')
    paintGrokFace(svg, 10)
    svg.setAttribute('data-hb-mood', 'think')
    paintGrokFace(svg, 10.1)
    paintGrokFace(svg, 12)

    // The body is the middle dot; the two side dots are separate circles.
    expect(svg.querySelectorAll(':scope > circle')).toHaveLength(2)
    expect(eyeHoles(svg)).toHaveLength(0)
  })

  it('work spins the orbit rings, and re-arms them after the rest beat', () => {
    const svg = face('grok:squircle')
    paintGrokFace(svg, 10)
    svg.setAttribute('data-hb-mood', 'work')
    paintGrokFace(svg, 10.1)
    paintGrokFace(svg, 11.5)

    expect(svg.querySelectorAll('linearGradient').length).toBeGreaterThan(0)

    // One orbit (3.4s) + rest (0.8s) later the rings are back.
    for (let t = 11.6; t < 16.4; t += 0.2) {
      paintGrokFace(svg, t)
    }

    expect(svg.querySelectorAll('linearGradient').length).toBeGreaterThan(0)
  })

  it('falls back to the classic face for a non-grok shape', () => {
    const svg = face('squircle')

    expect(svg.hasAttribute('data-hb-grok')).toBe(false)
    expect(svg.getAttribute('data-hb-shape')).toBe('squircle')
  })
})
