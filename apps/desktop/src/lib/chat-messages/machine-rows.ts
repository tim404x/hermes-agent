/**
 * Machine rows typed at read time, so history stored by an older backend renders by the same rules
 * as rows the current backend types at write time (no database rewrite).
 *
 * A `user` row is the person only when the person wrote it. Rows the runtime wrote into the user
 * role (a scheduled job's output, a bot's task brief, a background-process completion, a runtime
 * notification) become typed notices here and paint as one quiet event row each, never as the
 * user's own bubble. An interrupt sentinel stored as assistant prose paints nothing. An UNTYPED
 * `[IMPORTANT: Background process ...]` row stays a user row (a person may quote the marker,
 * #112144); the thread renders it as a process note titled by `processNotificationTitle`.
 */
import { relabelProcessTitle } from '@/lib/process-title'
import type { SessionMessage } from '@/types/hermes'

/** Desktop display kinds for runtime-written user rows (the backend serves the first two too). */
export const CRON_DELIVERY_KIND = 'cron_delivery'
export const BOT_BRIEF_KIND = 'bot_brief'
export const RUNTIME_NOTICE_KIND = 'runtime_notice'

export type MachineEventKind = 'brief' | 'cron' | 'notice' | 'process'

export const MACHINE_EVENT_KINDS: Readonly<Record<string, MachineEventKind>> = {
  [BOT_BRIEF_KIND]: 'brief',
  [CRON_DELIVERY_KIND]: 'cron',
  [RUNTIME_NOTICE_KIND]: 'notice'
}

// cron/scheduler_delivery.py::bot_chat_message: `[Cronjob "<name>" output — ...]\n\n<output>`.
const CRON_FRAME_RE = /^\[Cronjob "(.*?)" output\b[^\]]*\]\s*/
// agent/conversation_loop.py INTERRUPT_WAITING_FOR_MODEL_PREFIX: cancellation metadata a backend
// before the fix stored as the turn's assistant prose.
const INTERRUPT_SENTINEL_RE = /^Operation interrupted: waiting for model response \([^)\n]*\)\.?$/
// Inter-agent deliveries keep their own attributed notice (and the reply fold that keys on them).
const AGENT_DELIVERY_RE = /^(?:Message from |\[Message from agent ')/

type Metadata = Record<string, unknown>

function metadataOf(message: SessionMessage): Metadata {
  let parsed: unknown = message.display_metadata

  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed)
    } catch {
      return {}
    }
  }

  return parsed && typeof parsed === 'object' ? (parsed as Metadata) : {}
}

// The resume projection serves a row's words as `text`, the REST page as `content`.
const storedText = (message: SessionMessage) =>
  typeof message.content === 'string' && message.content
    ? message.content
    : typeof message.text === 'string'
      ? message.text
      : ''

function cronRow(message: SessionMessage, metadata: Metadata): null | SessionMessage {
  const text = storedText(message)
  const frame = CRON_FRAME_RE.exec(text)
  const typed = metadata.cron_delivery && typeof metadata.cron_delivery === 'object' ? metadata.cron_delivery : null

  if (!frame && !typed) {
    return null
  }

  const named = typed ? (typed as Metadata).job_name : undefined
  const jobName = (typeof named === 'string' && named.trim()) || frame?.[1]?.trim() || 'Scheduled job'
  const output = message.display_content ?? (frame ? text.slice(frame[0].length) : text)

  return {
    ...message,
    display_content: output,
    display_kind: CRON_DELIVERY_KIND,
    display_metadata: { ...metadata, display_text: jobName }
  }
}

function userMachineRow(message: SessionMessage): SessionMessage {
  const kind = message.display_kind
  const metadata = metadataOf(message)

  if (!kind || kind === 'internal_notification' || kind === CRON_DELIVERY_KIND) {
    const cron = cronRow(message, metadata)

    if (cron) {
      return cron
    }
  }

  if (!kind) {
    return message
  }

  if (kind === 'process_complete' && typeof metadata.display_text === 'string') {
    return { ...message, display_metadata: { ...metadata, display_text: relabelProcessTitle(metadata.display_text) } }
  }

  // Any other runtime notification is the runtime speaking, never the user.
  if (kind === 'internal_notification' && !AGENT_DELIVERY_RE.test(storedText(message).trimStart())) {
    return { ...message, display_kind: RUNTIME_NOTICE_KIND }
  }

  return message
}

/** The row as the thread should read it. Returns the same object when nothing changes. */
export function typeMachineRow(message: SessionMessage): SessionMessage {
  if (message.role === 'user') {
    return userMachineRow(message)
  }

  if (message.role === 'assistant' && INTERRUPT_SENTINEL_RE.test(storedText(message).trim())) {
    return { ...message, content: '', display_content: '' }
  }

  return message
}

/** The one-line label of a typed machine event row. */
export function machineEventLabel(kind: MachineEventKind, metadata: Metadata): string {
  if (kind === 'cron') {
    return typeof metadata.display_text === 'string' && metadata.display_text ? metadata.display_text : 'Scheduled job'
  }

  if (kind === 'brief') {
    const author = metadata.author && typeof metadata.author === 'object' ? (metadata.author as Metadata) : {}
    const raw = typeof author.name === 'string' && author.name.trim() ? author.name : author.id
    const name = typeof raw === 'string' ? raw.replace(/^bot:/, '').split(/\s+[|\u00b7\u2022\u2014\u2013]\s+/)[0] : ''

    return name ? `Brief from ${name}` : 'Task brief'
  }

  return 'Notification'
}

export { metadataOf as machineRowMetadata }
