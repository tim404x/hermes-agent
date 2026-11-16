import { textWithoutReferenceLines } from '@/components/assistant-ui/reference-kinds'
import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'

// The model-facing frame a mid-turn steer row is stored in (agent/prompt_builder.py
// STEER_MARKER_OPEN / _CLOSE). The gateway's display projection already strips it; an
// older backend or a raw `content` read does not, so matching strips it too.
const STEER_OPEN = '[OUT-OF-BAND USER MESSAGE'
const STEER_CLOSE = '[/OUT-OF-BAND USER MESSAGE'

// appendSessionTextMessage's id: `user-<Date.now()>-<random>`. Prompts carry
// `user-<submission uuid>` and bind through their submit receipt instead.
const ECHO_ID = /^user-(\d{13})-[a-z0-9]+$/

// A merged row is written when the agent drains its steers, minutes after the
// first was typed, never before. The slack absorbs Mac vs gateway clock skew.
const ECHO_CLOCK_SLACK_MS = 60_000

/** The user's own words of a stored steer row: the out-of-band frame removed. */
export function steerBodyText(text: string): string {
  const open = text.indexOf(STEER_OPEN)

  if (open < 0) {
    return text
  }

  const lineEnd = text.indexOf('\n', open)
  const start = lineEnd < 0 ? open + STEER_OPEN.length : lineEnd + 1
  const close = text.indexOf(STEER_CLOSE, start)

  return text.slice(start, close < 0 ? undefined : close)
}

const normalized = (text: string) =>
  textWithoutReferenceLines(steerBodyText(text))
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map(line => line.trimEnd())
    .join('\n')
    .trim()

/** When the composer painted this optimistic echo, if its id says so. */
export function echoCreatedAtMs(message: ChatMessage): number | undefined {
  const match = ECHO_ID.exec(message.id)

  return match ? Number(match[1]) : undefined
}

/** Whether a committed user row was written no earlier than an echo painted at `createdAtMs`. */
export function writtenSinceEcho(row: ChatMessage, createdAtMs: number): boolean {
  return typeof row.timestamp === 'number' && row.timestamp * 1000 >= createdAtMs - ECHO_CLOCK_SLACK_MS
}

/**
 * The items whose words a committed user row holds as whole lines, consumed in
 * order. Steers typed inside one tool batch reach the agent as ONE drained
 * steer (interrupt_control joins them with newlines), so one stored row stands
 * for several sends; a steer requeued as the next turn's prompt is joined the
 * same way. Each row line is consumed once: a repeat the row holds once is a
 * different send. `eligible` pairs an item with the rows that may hold it.
 */
export function itemsHeldByCommittedRows<T>(
  items: T[],
  textOf: (item: T) => string,
  committed: ChatMessage[],
  eligible: (item: T, row: ChatMessage) => boolean
): Set<T> {
  const rows = committed
    .filter(row => row.role === 'user' && row.rowId !== undefined)
    .map(row => ({ row, body: `\n${normalized(chatMessageText(row))}\n`, cursor: 0 }))

  const held = new Set<T>()

  if (!rows.length) {
    return held
  }

  for (const item of items) {
    const text = normalized(textOf(item))

    if (!text) {
      continue
    }

    for (const entry of rows) {
      if (!eligible(item, entry.row)) {
        continue
      }

      const at = entry.body.indexOf(`\n${text}\n`, entry.cursor)

      if (at >= 0) {
        // The closing newline stays available as the next line's opening one.
        entry.cursor = at + text.length + 1
        held.add(item)

        break
      }
    }
  }

  return held
}

const CORRECTION_ID = 'user-inflight-correction-'

/**
 * Optimistic user rows of `previous` that `next` already stores inside a merged
 * row: composer echoes (stamped ids, matched against rows written since) and
 * resume-projected corrections (no stamp, matched against `newlyCommitted`).
 */
export function optimisticEchoesHeldByCommittedRows(
  previous: ChatMessage[],
  next: ChatMessage[],
  newlyCommitted: ChatMessage[]
): Set<ChatMessage> {
  const fresh = new Set(newlyCommitted)

  const echoes = previous.filter(
    message => message.role === 'user' && (ECHO_ID.test(message.id) || message.id.startsWith(CORRECTION_ID))
  )

  return itemsHeldByCommittedRows(echoes, chatMessageText, next, (echo, row) => {
    const createdAtMs = echoCreatedAtMs(echo)

    return createdAtMs === undefined ? fresh.has(row) : writtenSinceEcho(row, createdAtMs)
  })
}

/** Indexes of a live turn's corrections that a user row stored since the turn began already holds. */
export function correctionsHeldByCommittedRows(
  corrections: string[],
  messages: ChatMessage[],
  turnStartedAt: unknown,
  slackS: number
): Set<number> {
  if (typeof turnStartedAt !== 'number' || turnStartedAt <= 0) {
    return new Set()
  }

  return itemsHeldByCommittedRows(
    corrections.map((_, index) => index),
    index => corrections[index],
    messages,
    (_, row) => typeof row.timestamp === 'number' && row.timestamp >= turnStartedAt - slackS
  )
}
