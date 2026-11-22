import { finalizeInterruptedMessages, sealOpenToolParts } from '@/lib/chat-messages'

import type { ClientSessionState } from '../../types'

/**
 * How long a submitted turn may sit unconfirmed (no message.start, no payload)
 * while the backend's live snapshot still says idle. The backend flags a
 * session running when it ACCEPTS the prompt, so this only has to cover the
 * submit round trip plus one poll that left before the accept. A local
 * `awaitingResponse` older than this, with the backend reporting idle, is a
 * turn whose events never reached this window: keeping it busy forever is the
 * "phantom spinner" (a frozen "55s · 1.2k tokens · Untangling…" row that
 * outlives the turn by hours and blocks every transcript refresh behind it).
 */
export const UNCONFIRMED_TURN_GRACE_MS = 30_000

/** True while this renderer still believes a turn is in flight for the state:
 *  any of the turn flags, or a pending (running) assistant bubble on screen. */
export function stateClaimsLiveTurn(state: ClientSessionState | undefined): boolean {
  if (!state) {
    return false
  }

  return Boolean(
    state.busy ||
    state.needsInput ||
    state.awaitingResponse ||
    state.turnLive ||
    state.turnStartedAt !== null ||
    state.messages.some(message => message.role === 'assistant' && message.pending)
  )
}

/** The submit-to-accept window: an optimistic submit the backend has not
 *  confirmed yet, still young enough that an idle snapshot may predate the
 *  accept. Only this case may outlive an authoritative idle report. */
export function withinUnconfirmedSubmitGrace(state: ClientSessionState, nowMs: number): boolean {
  if (!state.awaitingResponse || state.sawAssistantPayload || state.turnLive) {
    return false
  }

  const startedAt = state.turnStartedAt

  return typeof startedAt === 'number' && Number.isFinite(startedAt) && nowMs - startedAt < UNCONFIRMED_TURN_GRACE_MS
}

/**
 * Settle a turn the backend reports finished (idle, or reaped) but whose
 * terminal events never reached this renderer. Mirrors the message.complete
 * settle: every turn flag off, the clock cleared, open tool rows sealed and
 * pending assistant bubbles finalized, so neither the composer, the sidebar arc
 * nor the transcript's tail activity row can keep claiming a live turn.
 */
export function settleOrphanedTurn(state: ClientSessionState, nowMs = Date.now()): ClientSessionState {
  // Same primitive the running=false heartbeat settle uses: un-pend kept text,
  // drop empty "waiting for first token" shells. Un-pend first, because
  // sealOpenToolParts skips pending bubbles.
  const messages = sealOpenToolParts(finalizeInterruptedMessages(state.messages, state.streamId, nowMs / 1000))

  // A streamed bubble that survived is remembered so a late message.complete
  // settles onto it instead of appending a duplicate (#119569).
  const heartbeatSettledStreamId =
    state.streamId && messages.some(message => message.id === state.streamId)
      ? state.streamId
      : (state.heartbeatSettledStreamId ?? null)

  return {
    ...state,
    awaitingResponse: false,
    busy: false,
    heartbeatSettledStreamId,
    interimBoundaryPending: false,
    messages,
    needsInput: false,
    pendingBranchGroup: null,
    streamId: null,
    turnLive: false,
    turnStartedAt: null
  }
}
