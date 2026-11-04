import { normalizeWs as normalizedText } from './parts'
import type { ChatMessage, ChatMessagePart } from './types'

function sameOccurrencePart(stored: ChatMessagePart, local: ChatMessagePart): boolean {
  if (stored.type === 'tool-call' && local.type === 'tool-call') {
    return Boolean(stored.toolCallId) && stored.toolCallId === local.toolCallId
  }

  if ((stored.type === 'text' || stored.type === 'reasoning') && local.type === stored.type) {
    return normalizedText(stored.text) === normalizedText(local.text)
  }

  return false
}

interface AssistantPrefixCoverage {
  anchored: boolean
  /** Each covered local part, by the index of the stored part covering it in
   *  the flattened assistant parts of `stored`. */
  covering: Map<number, ChatMessagePart>
  remaining: ChatMessage[]
}

function coverAssistantPrefix(stored: ChatMessage[], local: ChatMessage[]): AssistantPrefixCoverage {
  const parts = stored.flatMap(message => (message.role === 'assistant' ? message.parts : []))
  let cursor = 0
  let anchored = false
  let stopped = false
  const remaining: ChatMessage[] = []
  const covering = new Map<number, ChatMessagePart>()

  for (const message of local) {
    if (stopped || message.role !== 'assistant' || message.error) {
      stopped = true
      remaining.push(message)

      continue
    }

    let consumed = 0

    for (const part of message.parts) {
      if (!parts[cursor] || !sameOccurrencePart(parts[cursor], part)) {
        break
      }

      anchored ||= part.type === 'tool-call'
      covering.set(cursor, part)
      cursor += 1
      consumed += 1
    }

    if (consumed < message.parts.length) {
      stopped = true
      remaining.push(consumed ? { ...message, parts: message.parts.slice(consumed) } : message)
    }
  }

  return { anchored, covering, remaining }
}

/** Subtract an ordered, tool-anchored prefix within an already matched user
 * interval. Hydration can fold several live bubbles into one durable row;
 * bubble ordinals and equal text alone cannot establish that coverage. */
export function withoutCoveredAssistantPrefix(stored: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const { anchored, remaining } = coverAssistantPrefix(stored, local)

  // A coincidentally equal paragraph, without the same tool occurrence after
  // it, is insufficient evidence to remove anything.
  return anchored ? remaining : local
}

/** The stored rows of a matched interval, each tool call the stored page has
 * no result for yet taking the result its covered live occurrence already got.
 * A tool batch persists each result just before that result's `tool.complete`
 * fires, so a page read mid-batch can predate rows whose completions the
 * cache already applied. Coverage then dropped the live copy and the stored
 * one stood in without a result its event will never repeat: it read
 * "Result unavailable" once the turn ended. A stored result always wins. */
export function withCoveredLiveToolResults(stored: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
  const { anchored, covering } = coverAssistantPrefix(stored, local)

  if (!anchored) {
    return stored
  }

  let flatIndex = 0
  let changed = false

  const next = stored.map(message => {
    if (message.role !== 'assistant') {
      return message
    }

    let partChanged = false

    const parts = message.parts.map(part => {
      const live = covering.get(flatIndex)
      flatIndex += 1

      if (
        part.type !== 'tool-call' ||
        live?.type !== 'tool-call' ||
        Object.hasOwn(part, 'result') ||
        !Object.hasOwn(live, 'result')
      ) {
        return part
      }

      partChanged = true

      return {
        ...part,
        result: live.result,
        ...(live.completedAt !== undefined && { completedAt: live.completedAt }),
        ...(live.isError !== undefined && { isError: live.isError }),
        ...(live.toolResultMetadata !== undefined && { toolResultMetadata: live.toolResultMetadata })
      } as ChatMessagePart
    })

    if (!partChanged) {
      return message
    }

    changed = true

    return { ...message, parts }
  })

  return changed ? next : stored
}
