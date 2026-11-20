/**
 * Intentional-silence markers: a bot that has nothing to say ends its turn with `NO_REPLY` or
 * `[SILENT]`, and the transport is meant to swallow it. The gateway already does for every
 * platform it delivers to; the stored row keeps the model's text, so a renderer that paints
 * stored rows has to apply the same rule or the token shows up as a message.
 *
 * Twin of `gateway/response_filters.py::is_intentional_silence_response` and
 * `strip_trailing_silence_marker`: same marker sets, same edge rules, the 64-code-point cap,
 * Python's `str.isspace` whitespace and the same explicit emoji ranges (so a newer Unicode in V8
 * cannot change a verdict). Both are pinned by the shared cases in
 * `tests/fixtures/silence_marker_cases.json`; change the two together.
 */

const SILENT_MARKERS: ReadonlySet<string> = new Set([
  '[SILENT]',
  'SILENT',
  'NO_REPLY',
  'NO REPLY',
  '[静默]',
  '静默',
  '[沉默]',
  '沉默'
])

// The machine tokens a model emits on purpose. Only these may carry a sign-off wrapping
// (`NO_REPLY ⚡`, `✅ NO_REPLY`, `NO_REPLY 1️⃣`); the natural words keep the punctuation-only rule,
// so `No reply 😕` stays a person's answer.
const MACHINE_MARKERS: ReadonlySet<string> = new Set(['NO_REPLY', '[SILENT]', '[静默]', '[沉默]'])

// Longer than any marker could plausibly be, even with stray punctuation. Counted in code points.
const MARKER_LENGTH_CAP = 64

// Python `str.isspace()`: JS `\s` lacks \x1c-\x1f and \x85, and adds U+FEFF.
const WS = '\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'
const WS_CHAR_RE = new RegExp(`^[${WS}]$`, 'u')
const WS_RUN_RE = new RegExp(`[${WS}]+`, 'u')
const LEADING_WS_RE = new RegExp(`^[${WS}]+`, 'u')
const TRAILING_WS_RE = new RegExp(`[${WS}]+$`, 'u')

const pyRstrip = (text: string) => text.replace(TRAILING_WS_RE, '')
/** Python `str.strip()`. */
export const pyStrip = (text: string) => pyRstrip(text.replace(LEADING_WS_RE, ''))
/** Python `" ".join(text.split())`. */
export const pyCollapseWhitespace = (text: string) => text.split(WS_RUN_RE).filter(Boolean).join(' ')

const PUNCTUATION_RE = /^\p{P}$/u
// Never Sk: a backtick quotes a value (a code span is content); skin tones sit in the emoji ranges.
const SIGNOFF_CATEGORY_RE = /^[\p{So}\p{Cf}\p{Mn}]$/u

// A keycap emoji (`1️⃣`) is its base character, an optional variation selector and U+20E3.
const KEYCAP_BASES = new Set('0123456789#*')
const KEYCAP = '\u20e3'

// Emoji blocks taken by range, not by Unicode category (same ranges as the Python twin).
const SIGNOFF_EMOJI_RANGES: readonly (readonly [number, number])[] = [
  [0x1f000, 0x1faff],
  [0x2600, 0x27bf]
]

// Square brackets stay structural: `[SILENT` never becomes `SILENT`.
const isEdgePunctuation = (ch: string) => ch !== '[' && ch !== ']' && PUNCTUATION_RE.test(ch)

function isEdgeSignoff(ch: string): boolean {
  if (ch === '[' || ch === ']') {
    return false
  }

  const code = ch.codePointAt(0) ?? 0

  return (
    SIGNOFF_EMOJI_RANGES.some(([lo, hi]) => code >= lo && code <= hi) ||
    WS_CHAR_RE.test(ch) ||
    PUNCTUATION_RE.test(ch) ||
    SIGNOFF_CATEGORY_RE.test(ch)
  )
}

function stripEdges(chars: readonly string[], isNoise: (ch: string) => boolean): string {
  let start = 0
  let end = chars.length

  while (start < end && isNoise(chars[start])) {
    start += 1
  }

  while (end > start && isNoise(chars[end - 1])) {
    end -= 1
  }

  return pyStrip(chars.slice(start, end).join(''))
}

/** Length of the keycap sequence starting at `i` (`1`, optional U+FE0F, U+20E3), else 0. */
function keycapFrom(chars: readonly string[], i: number, end: number): number {
  if (i < end && KEYCAP_BASES.has(chars[i])) {
    const j = i + 1 + (i + 1 < end && chars[i + 1] === '\ufe0f' ? 1 : 0)

    if (j < end && chars[j] === KEYCAP) {
      return j + 1 - i
    }
  }

  return 0
}

/** Length of the keycap sequence ending at `end`, else 0. */
function keycapUntil(chars: readonly string[], start: number, end: number): number {
  if (end > start && chars[end - 1] === KEYCAP) {
    const k = end - 1 - (end - 2 >= start && chars[end - 2] === '\ufe0f' ? 1 : 0)

    if (k - 1 >= start && KEYCAP_BASES.has(chars[k - 1])) {
      return end - (k - 1)
    }
  }

  return 0
}

/** Strip a sign-off emoji wrapping, keycaps included, from both ends; a bare digit stays text. */
function stripSignoffEdges(chars: readonly string[]): string {
  let start = 0
  let end = chars.length

  while (start < end) {
    const step = isEdgeSignoff(chars[start]) ? 1 : keycapFrom(chars, start, end)

    if (!step) {
      break
    }

    start += step
  }

  while (end > start) {
    const step = isEdgeSignoff(chars[end - 1]) ? 1 : keycapUntil(chars, start, end)

    if (!step) {
      break
    }

    end -= step
  }

  return pyStrip(chars.slice(start, end).join(''))
}

const canonical = (text: string) => pyCollapseWhitespace(text.toUpperCase())

/** True only when `text` is exactly a silence marker; prose that mentions one is not. */
export function isSilenceMarkerText(text: unknown): boolean {
  const stripped = typeof text === 'string' ? pyStrip(text) : ''
  const chars = Array.from(stripped)

  if (!chars.length || chars.length > MARKER_LENGTH_CAP) {
    return false
  }

  return (
    SILENT_MARKERS.has(canonical(stripped)) ||
    SILENT_MARKERS.has(canonical(stripEdges(chars, isEdgePunctuation))) ||
    MACHINE_MARKERS.has(canonical(stripSignoffEdges(chars)))
  )
}

/** A marker closing a real reply on its own last line is dropped (`"Paris.\n\nNO_REPLY"` paints
 *  `"Paris."`), as the gateway delivers it. A bare marker is returned as is: that is the silence
 *  verdict, decided by `isSilenceMarkerText`. */
export function stripTrailingSilenceMarker(text: string): string {
  const trimmed = pyRstrip(text)
  const cut = trimmed.lastIndexOf('\n')

  if (cut < 0) {
    return text
  }

  const head = pyRstrip(trimmed.slice(0, cut))

  return pyStrip(head) && isSilenceMarkerText(trimmed.slice(cut + 1)) ? head : text
}
