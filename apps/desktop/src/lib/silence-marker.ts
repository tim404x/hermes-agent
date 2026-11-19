/**
 * Intentional-silence markers: a bot that has nothing to say ends its turn with `NO_REPLY` or
 * `[SILENT]`, and the transport is meant to swallow it. The gateway already does for every
 * platform it delivers to; the stored row keeps the model's text, so a renderer that paints
 * stored rows has to apply the same rule or the token shows up as a message.
 *
 * Twin of `gateway/response_filters.py::is_intentional_silence_response` (marker set, edge
 * punctuation and sign-off emoji stripping, the 64-char cap). Both are pinned by the shared
 * cases in `tests/fixtures/silence_marker_cases.json`; change the two together.
 */

const SILENT_MARKERS = new Set(['[SILENT]', 'SILENT', 'NO_REPLY', 'NO REPLY', '[静默]', '静默', '[沉默]', '沉默'])

// Longer than any marker could plausibly be, even with stray punctuation.
const MARKER_LENGTH_CAP = 64

// Punctuation, whitespace and the pieces a sign-off emoji is built from (symbol, modifier,
// variation selector / ZWJ). Square brackets stay structural: `[SILENT` never becomes `SILENT`.
const EDGE_NOISE_RE = /^[\s\p{P}\p{So}\p{Sk}\p{Cf}\p{Mn}]$/u

const isEdgeNoise = (ch: string) => ch !== '[' && ch !== ']' && EDGE_NOISE_RE.test(ch)

function stripEdgeNoise(text: string): string {
  const chars = Array.from(text)
  let start = 0
  let end = chars.length

  while (start < end && isEdgeNoise(chars[start])) {
    start += 1
  }

  while (end > start && isEdgeNoise(chars[end - 1])) {
    end -= 1
  }

  return chars.slice(start, end).join('').trim()
}

const canonical = (text: string) => text.trim().toUpperCase().split(/\s+/).filter(Boolean).join(' ')

/** True only when `text` is exactly a silence marker; prose that mentions one is not. */
export function isSilenceMarkerText(text: unknown): boolean {
  const stripped = typeof text === 'string' ? text.trim() : ''

  if (!stripped || stripped.length > MARKER_LENGTH_CAP) {
    return false
  }

  return [stripped, stripEdgeNoise(stripped)].some(form => SILENT_MARKERS.has(canonical(form)))
}

/** A marker closing a real reply on its own last line is dropped (`"Paris.\n\nNO_REPLY"` paints
 *  `"Paris."`), as the gateway delivers it. A bare marker is returned as is: that is the silence
 *  verdict, decided by `isSilenceMarkerText`. */
export function stripTrailingSilenceMarker(text: string): string {
  const trimmed = text.trimEnd()
  const cut = trimmed.lastIndexOf('\n')

  if (cut < 0) {
    return text
  }

  const head = trimmed.slice(0, cut).trimEnd()

  return head.trim() && isSilenceMarkerText(trimmed.slice(cut + 1)) ? head : text
}
