/**
 * Sends into a session whose identity is still being confirmed.
 *
 * Bot Mode's paint-first click (bot-switch-speed) opens the roster's last
 * known Bot Chat at once and asks the registry beside it. The paint is safe;
 * a send is not: the roster row can be stale (another window or a messaging
 * lane displaced the Bot Chat, or the window was hidden and the roster poll
 * paused), and a message accepted by the old runtime lands in a conversation
 * the registry answer then navigates away from. So the opener registers a
 * hold on the painted stored id, and the submit pipeline awaits its verdict
 * before it binds a target:
 *
 *  - confirmed, same id: the send proceeds as typed;
 *  - confirmed, another id: the send is replayed into that chat once it is the
 *    one on screen (the draft moves with it);
 *  - unconfirmed (the lookup failed, or the open was superseded): the send is
 *    refused, so the composer gives the words back, and the opener's
 *    `onUnconfirmed` explains and offers a retry. A FAILED hold stays in place,
 *    so later sends are refused too until a newer open replaces it; an
 *    unconfirmed chat is never treated as confirmed by default.
 */

export type SubmitConfirmation =
  | { kind: 'confirmed'; storedSessionId: string }
  | { kind: 'none' }
  | { kind: 'unconfirmed'; reason: 'failed' | 'superseded' }

export interface SubmitHoldOptions {
  /** Called when a held send is refused because confirmation FAILED (not
   *  when it was merely superseded by a newer open). */
  onUnconfirmed?: () => void
}

interface SubmitHold extends SubmitHoldOptions {
  confirmation: Promise<null | string>
  failed: boolean
}

const holds = new Map<string, SubmitHold>()

/** Hold sends into `storedSessionId` until `confirmation` settles: a string is
 *  the confirmed stored id (the same one or the chat that replaces it), `null`
 *  means the open was superseded, a rejection means it could not be confirmed.
 *  Register it BEFORE the session is opened, so no send can slip in between.
 *  Returns a release for an opener that gives up without a verdict. */
export function holdSubmitsUntilConfirmed(
  storedSessionId: string,
  confirmation: Promise<null | string>,
  options: SubmitHoldOptions = {}
): () => void {
  const id = String(storedSessionId || '').trim()

  if (!id) {
    return () => undefined
  }

  const hold: SubmitHold = { ...options, confirmation, failed: false }
  holds.set(id, hold)

  const release = () => {
    if (holds.get(id) === hold) {
      holds.delete(id)
    }
  }

  confirmation.then(release, () => {
    hold.failed = true
  })

  return release
}

export function hasSubmitHold(storedSessionId: null | string | undefined): boolean {
  return Boolean(storedSessionId && holds.has(storedSessionId))
}

/** The verdict a send into `storedSessionId` must wait for. A hold replaced
 *  while waiting (the user clicked the same bot again) is followed to the
 *  newest one, so a double click never refuses a send it could deliver. */
export async function awaitSubmitConfirmation(storedSessionId: null | string | undefined): Promise<SubmitConfirmation> {
  let hold = storedSessionId ? holds.get(storedSessionId) : undefined

  if (!storedSessionId || !hold) {
    return { kind: 'none' }
  }

  for (;;) {
    let confirmed: null | string = null
    let failed = false

    try {
      confirmed = await hold.confirmation
    } catch {
      failed = true
    }

    const newer = holds.get(storedSessionId)

    if (newer && newer !== hold) {
      hold = newer

      continue
    }

    if (failed) {
      hold.onUnconfirmed?.()

      return { kind: 'unconfirmed', reason: 'failed' }
    }

    return confirmed ? { kind: 'confirmed', storedSessionId: confirmed } : { kind: 'unconfirmed', reason: 'superseded' }
  }
}

/** Forget every hold (a whole session-store reset). */
export function clearSubmitHolds(): void {
  holds.clear()
}
