/**
 * Grok Bot faces: the xAI bot avatar family (8 clay shapes, 16 rest
 * expressions, solid white slanted slit eyes painted on the body) drawn LIVE in
 * any colour, and animated by the roster mood instead of frozen in a PNG.
 *
 * The geometry and motion come from `./grokbot`, a vendored copy of bloub
 * (github.com/jeremy-prt/bloub, MIT), whose constants are measured frame by
 * frame off xAI's own reference video. Nothing here re-tunes them: this file
 * only maps Hermes concepts (appearance string, colour, mood) onto that engine
 * and serialises its frames into the avatar SVG.
 *
 * Appearance string: `grok:<shape>[:<expression>]`, e.g. `grok:triangle:proud`.
 * English ids on our side so stored metadata reads naturally; bloub's ids are
 * French and stay private to the vendored engine.
 *
 * Painting: the React render draws the first frame (so a face is complete with
 * no clock, in tests, and in the PNG backfill), then the shared face clock
 * (`startFaceClock`, 15fps budgeted, visibility-gated) calls `paintGrokFace`
 * for every visible `svg[data-hb-grok]`. One engine per <svg> lives in a
 * WeakMap, so unmounted faces are collected with their node.
 */

import { BotEngine, type BotFrame } from './grokbot/engine'
import { EXPRESSION_BY_ID } from './grokbot/expressions'
import { RAYON } from './grokbot/repere'
import { SHAPE_BY_ID } from './grokbot/skins'
import { STATE_BY_ID, type StateId } from './grokbot/states'
import type { FaceMood } from './types'

/** Our id -> bloub id. Order is the picker order. */
export const GROK_SHAPES = {
  circle: 'cercle',
  pebble: 'galet',
  squircle: 'squircle',
  capsule: 'capsule',
  triangle: 'triangle',
  hexagon: 'hexagone',
  cloud: 'nuage',
  droplet: 'goutte'
} as const

/** Our id -> bloub id. Order is the picker order. */
export const GROK_EXPRESSIONS = {
  neutral: 'neutre',
  happy: 'heureux',
  laughing: 'hilare',
  excited: 'excite',
  proud: 'fier',
  curious: 'curieux',
  attentive: 'attentif',
  surprised: 'surpris',
  shy: 'timide',
  confused: 'confus',
  suspicious: 'mefiant',
  bored: 'blase',
  sleepy: 'somnolent',
  sad: 'triste',
  scared: 'effraye',
  angry: 'colere'
} as const

export type GrokShapeId = keyof typeof GROK_SHAPES
export type GrokExpressionId = keyof typeof GROK_EXPRESSIONS

/** xAI's Grok Bot palette, offered as swatches next to the profile colours. Red, orange,
 *  amber, green, blue and violet are xAI's official values (design post + client colour table,
 *  measured 15 Nov 2026); the other six are bloub's picker samples. */
export const GROK_PALETTE = [
  '#0a0a0c',
  '#8b5e3c',
  '#ff263c',
  '#ff6700',
  '#ff9800',
  '#00c972',
  '#2fbfa0',
  '#1084fe',
  '#9159fe',
  '#e152b0',
  '#a3a3a3',
  '#f1efe9'
] as const

export interface GrokSpec {
  expression: GrokExpressionId
  shape: GrokShapeId
}

const PREFIX = 'grok:'

export function isGrokShape(shape: null | string | undefined): boolean {
  return parseGrokShape(shape) !== null
}

/** `grok:<shape>[:<expression>]` -> spec; anything else (or an unknown id) -> null. */
export function parseGrokShape(shape: null | string | undefined): GrokSpec | null {
  if (typeof shape !== 'string' || !shape.startsWith(PREFIX)) {
    return null
  }

  const [rawShape = '', rawExpression = 'neutral'] = shape.slice(PREFIX.length).split(':')

  if (!Object.prototype.hasOwnProperty.call(GROK_SHAPES, rawShape)) {
    return null
  }

  // An unknown expression degrades to neutral instead of dropping the whole
  // face: the shape is the identity, the expression is a mood on top.
  const expression = Object.prototype.hasOwnProperty.call(GROK_EXPRESSIONS, rawExpression)
    ? (rawExpression as GrokExpressionId)
    : 'neutral'

  return { shape: rawShape as GrokShapeId, expression }
}

export function formatGrokShape(shape: GrokShapeId, expression: GrokExpressionId = 'neutral'): string {
  return expression === 'neutral' ? `${PREFIX}${shape}` : `${PREFIX}${shape}:${expression}`
}

/** Half-side of the avatar viewBox, in engine units (body radius = RAYON = 100).
 *  Tighter than bloub's still export (125) so the body fills ~88% of the slot, like
 *  xAI's own render; the work-mood orbit rings clip a little more at this crop. */
const HALF = 113
/** Work mood: one orbit (its measured length), a short rest beat, again. Orbit is a
 *  one-shot in the engine (rings fade at its end), so the loop re-arms it. */
const ORBIT_HOLD = STATE_BY_ID.get('orbit')?.duration ?? 3.4
const WORK_REST = 0.8

interface GrokRecord {
  engine: BotEngine
  expression: GrokExpressionId
  mood: string
  phase: StateId
  phaseSince: number
  shape: GrokShapeId
  uid: string
}

const records = new WeakMap<SVGSVGElement, GrokRecord>()

function radiiFor(shape: GrokShapeId) {
  return SHAPE_BY_ID.get(GROK_SHAPES[shape] as never)?.radii ?? null
}

function expressionFor(expression: GrokExpressionId) {
  return EXPRESSION_BY_ID.get(GROK_EXPRESSIONS[expression]) ?? null
}

function moodState(mood: string): StateId {
  if (mood === 'think') {
    return 'thinking'
  }

  return mood === 'work' ? 'orbit' : 'idle'
}

function newUid() {
  return Math.random().toString(36).slice(2, 8)
}

function attr(value: number | string) {
  return String(value).replace(/[<>&"]/g, '')
}

/** One engine frame -> the avatar's inner SVG markup (bloub's BloubBot.vue template, flattened). */
export function grokFrameMarkup(frame: BotFrame, ink: string, uid: string): string {
  const color = attr(ink)
  const maskId = `hb-grok-${uid}`

  // Eyes are solid WHITE paint on the opaque body (Tim, 17 Nov 2026: he prefers the white
  // eyes the picker shows on a white card). `alpha` drives blinks and mood fades, so it
  // stays the eye's opacity: a fading eye blends into the body colour, never into a hole.
  const whiteEyes = frame.eyes
    .map(eye => `<path d="${eye.d}" transform="${eye.matrix}" opacity="${eye.alpha}" fill="#fff"/>`)
    .join('')

  const notch = frame.notch
    ? `<circle cx="${frame.notch.x}" cy="${frame.notch.y}" r="${frame.notch.r}" fill="#000"/>`
    : ''

  const gradients = frame.arcs
    .map(
      arc =>
        `<linearGradient id="${uid}-${arc.id}" gradientUnits="userSpaceOnUse" x1="${arc.grad.x1}" y1="${arc.grad.y1}" x2="${arc.grad.x2}" y2="${arc.grad.y2}">` +
        arc.grad.stops
          .map((stop, i) => `<stop offset="${i / Math.max(1, arc.grad.stops.length - 1)}" stop-color="${attr(stop)}"/>`)
          .join('') +
        '</linearGradient>'
    )
    .join('')

  const arcs = (half: 'back' | 'front') =>
    frame.arcs.length
      ? `<g fill="none" stroke-linecap="round">${frame.arcs
          .map(
            arc =>
              `<path d="${arc[half]}" stroke="url(#${uid}-${arc.id})" stroke-width="${arc.width}" opacity="${arc.opacity}"/>`
          )
          .join('')}</g>`
      : ''

  const dots = frame.dots
    .map(dot => {
      const fill = attr(dot.color ?? color)
      // `depth` (burst particles) fades toward the paper in bloub; opacity is the
      // colour-format-agnostic equivalent, so any CSS colour still works.
      const opacity = dot.opacity * (dot.depth === undefined ? 1 : dot.depth)

      return dot.d
        ? `<path d="${dot.d}" transform="translate(${dot.x} ${dot.y}) rotate(${dot.rot ?? 0}) scale(${RAYON})" fill="${fill}" opacity="${opacity}"/>`
        : `<circle cx="${dot.x}" cy="${dot.y}" r="${dot.r}" fill="${fill}" opacity="${opacity}"/>`
    })
    .join('')

  // The body is never cut where the eyes are: the eyes are painted ON TOP of the opaque
  // colour, inside the same body mask, so their anti-aliased edge blends white into the
  // body colour only. Eye holes let a dark sidebar show through as black eyes, and a white
  // under-layer left a pale rim around the body; this has neither. The notch stays a real
  // hole: it is the gap that separates the notification dot from the body, not an eye.
  const body =
    `<g opacity="${frame.bodyAlpha}">` +
    `<g mask="url(#${maskId})"><rect x="${-HALF}" y="${-HALF}" width="${HALF * 2}" height="${HALF * 2}" fill="${color}"/>${whiteEyes}</g></g>`

  return (
    `<defs><mask id="${maskId}" maskUnits="userSpaceOnUse" x="${-HALF}" y="${-HALF}" width="${HALF * 2}" height="${HALF * 2}">` +
    `<path d="${frame.bodyPath}" fill="#fff"/>${notch}</mask>${gradients}</defs>` +
    arcs('back') +
    (frame.dotsBehind ? dots : '') +
    body +
    (frame.dotsBehind ? '' : dots) +
    arcs('front')
  )
}

/** First frame for the React render: the mood's state, already settled. */
export function grokInitialMarkup(spec: GrokSpec, ink: string, mood: string, uid: string): string {
  const engine = new BotEngine(RAYON, moodState(mood), radiiFor(spec.shape), expressionFor(spec.expression))
  const state = moodState(mood)
  engine.reset(state, 0)

  // Sample past the entry morph so a still render is the pose, not a blend.
  return grokFrameMarkup(engine.sample(STATE_BY_ID.get(state)?.morph ?? 0.5), ink, uid)
}

/** Face-clock hook: advance this svg's engine to `t` (seconds) and repaint it. */
export function paintGrokFace(svg: SVGSVGElement, t: number) {
  const spec = parseGrokShape(svg.getAttribute('data-hb-grok'))

  if (!spec) {
    return
  }

  const mood = svg.getAttribute('data-hb-mood') || 'idle'
  const ink = svg.getAttribute('data-hb-ink') || '#0a0a0c'
  let rec = records.get(svg)

  if (!rec) {
    const state = moodState(mood)
    const engine = new BotEngine(RAYON, state, radiiFor(spec.shape), expressionFor(spec.expression))
    engine.reset(state, t - (STATE_BY_ID.get(state)?.morph ?? 0.5))
    rec = { engine, expression: spec.expression, mood, phase: state, phaseSince: t, shape: spec.shape, uid: newUid() }
    records.set(svg, rec)
  }

  // Shape / expression edits morph in place (the engine blends them) instead of
  // snapping, exactly like the reference customiser.
  if (rec.shape !== spec.shape) {
    rec.engine.setShape(radiiFor(spec.shape), t)
    rec.shape = spec.shape
  }

  if (rec.expression !== spec.expression) {
    rec.engine.setExpression(expressionFor(spec.expression), t)
    rec.expression = spec.expression
  }

  if (rec.mood !== mood) {
    rec.mood = mood
    rec.phase = moodState(mood)
    rec.phaseSince = t
  } else if (mood === 'work') {
    const held = t - rec.phaseSince

    if (rec.phase === 'orbit' && held >= ORBIT_HOLD) {
      rec.phase = 'idle'
      rec.phaseSince = t
    } else if (rec.phase === 'idle' && held >= WORK_REST) {
      rec.phase = 'orbit'
      rec.phaseSince = t
    }
  }

  rec.engine.setState(rec.phase, t)
  svg.innerHTML = grokFrameMarkup(rec.engine.sample(t), ink, rec.uid)
}

interface GrokFaceProps {
  color: string
  mood: FaceMood
  name: string
  size: number
  spec: GrokSpec
}

/** The avatar <svg>. React owns the attributes; the face clock owns the inside. */
export function GrokFace({ color, mood, name, size, spec }: GrokFaceProps) {
  const appearance = formatGrokShape(spec.shape, spec.expression)
  // Re-derived only when the look changes; a mood flip must NOT reset innerHTML
  // (the clock is mid-animation), so mood stays out of the memo key.
  const markup = grokMarkupFor(spec, color)

  return (
    <svg
      aria-hidden
      // Clipped (no overflow-visible): the work-mood orbit rings reach past the
      // body, and in a 22-36px roster row they would paint over the bot's name.
      className="block overflow-hidden"
      dangerouslySetInnerHTML={{ __html: markup }}
      data-bot-face={name}
      data-hb-grok={appearance}
      data-hb-ink={color}
      data-hb-math="1"
      data-hb-mood={mood}
      height={size}
      viewBox={`${-HALF} ${-HALF} ${HALF * 2} ${HALF * 2}`}
      width={size}
    />
  )
}

const markupCache = new Map<string, string>()

function grokMarkupFor(spec: GrokSpec, color: string): string {
  const key = `${spec.shape}:${spec.expression}:${color}`
  const cached = markupCache.get(key)

  if (cached) {
    return cached
  }

  const markup = grokInitialMarkup(spec, color, 'idle', newUid())

  if (markupCache.size > 256) {
    markupCache.clear()
  }

  markupCache.set(key, markup)

  return markup
}
