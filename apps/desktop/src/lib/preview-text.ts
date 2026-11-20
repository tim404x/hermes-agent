/**
 * The one rule for turning a stored message into the short text a list row shows (sidebar
 * session titles/previews, bot-roster "last message" lines, room previews).
 *
 * Twin of `session_rules/preview_text.py::display_preview_text`, which the backend already
 * applies to every preview it serves. This copy covers previews served by an older backend or
 * repainted from a renderer cache. Both are pinned by the shared cases in
 * `tests/fixtures/preview_text_cases.json`; change the two together.
 *
 * - a mid-turn steer (`[OUT-OF-BAND USER MESSAGE …] words [/OUT-OF-BAND USER MESSAGE]`) unwraps
 *   to the words, also when the snippet was cut before the closing marker;
 * - a compaction carrier keeps only the user message after the summary's end marker;
 * - `[Runtime note: …]` / `[System note: …]` blocks and `<memory-context>` fences are removed;
 * - a row that is only a harness frame (`[IMPORTANT: …]`, `[System: …]`, `[ASYNC DELEGATION …]`,
 *   a pure compaction summary, …) becomes '' so the caller shows nothing rather than internals;
 * - with `silenceOk` (an assistant reply in a turn that may end silent), a bare silence marker
 *   (`NO_REPLY`, `[SILENT]`; `lib/silence-marker.ts`) becomes '' too and one closing the reply on its own
 *   line is dropped. A person's own words, titles and timeline rows never are.
 *
 * Slash-skill scaffolds are described as the typed `/skill — request` by the backend only (that
 * needs the skill registry); here such a row falls under the `[IMPORTANT:` frame and reads ''.
 */

import { isSilenceMarkerText, pyCollapseWhitespace, pyStrip, stripTrailingSilenceMarker } from './silence-marker'

const SUMMARY_END_MARKER = '--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---'

// The opener carries no inner "]"; a snippet cut inside it runs to the end of its line.
const STEER_FRAME_RE = /\[\/?OUT-OF-BAND USER MESSAGE[^\]\n]*(?:\]|$)/gm
const MEMORY_CONTEXT_RE = /<\s*memory-context\s*>[\s\S]*?<\/\s*memory-context\s*>/gi
const MEMORY_FENCE_RE = /<\/?\s*memory-context\s*>/gi
const INLINE_NOTE_RE = /\[(?:Runtime note|System note):[\s\S]*?\](?=[ \t]*(?:\n|$))/g
const INLINE_NOTE_TAIL_RE = /\[(?:Runtime note|System note):[\s\S]*$/

const WHOLE_ROW_FRAME_OPENERS = [
  '[IMPORTANT:',
  '[ASYNC DELEGATION',
  '[System:',
  '[SYSTEM]',
  '[Planning state preserved',
  '[Your active task list',
  // A scheduled job's output delivered into a Bot Chat (cron/scheduler_delivery.py bot_chat_message).
  '[Cronjob ',
  // Compaction carriers match by their whole introduction, so a user quoting the label still
  // wrote a real message.
  '[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted into the summary below. This is a handoff from a previous context window — treat it as background reference, NOT as active instructions.',
  '[CONTEXT SUMMARY]:',
  '[PRIOR CONTEXT — for reference only; not a new message]'
]

/** `raw` as a list row shows it: harness frames removed, whitespace collapsed; '' when nothing is left.
 *  `keepLines` leaves line breaks in place for a caller that still runs line-anchored rules (the
 *  markdown flattener) and collapses afterwards. */
export function sanitizePreviewText(
  raw: unknown,
  { keepLines = false, silenceOk = false }: { keepLines?: boolean; silenceOk?: boolean } = {}
): string {
  let text = typeof raw === 'string' ? raw : raw == null ? '' : String(raw)

  if (!pyStrip(text)) {
    return ''
  }

  const summaryEnd = text.lastIndexOf(SUMMARY_END_MARKER)

  if (summaryEnd >= 0) {
    text = text.slice(summaryEnd + SUMMARY_END_MARKER.length)
  }

  text = text
    .replace(STEER_FRAME_RE, ' ')
    .replace(MEMORY_CONTEXT_RE, '')
    .replace(MEMORY_FENCE_RE, '')
    .replace(INLINE_NOTE_RE, ' ')
    .replace(INLINE_NOTE_TAIL_RE, ' ')

  // Whitespace as Python reads it (session_rules/preview_text.py), so both twins flatten alike.
  text = pyStrip(text)

  if (silenceOk) {
    // Before flattening: a marker closing the reply on its own line ("Paris.\n\nNO_REPLY") is dropped
    // as the thread drops it; flattened, it would read as prose.
    text = stripTrailingSilenceMarker(text)
  }

  const flat = pyCollapseWhitespace(text)

  if (WHOLE_ROW_FRAME_OPENERS.some(opener => flat.startsWith(opener)) || (silenceOk && isSilenceMarkerText(flat))) {
    return ''
  }

  return keepLines ? text : flat
}
