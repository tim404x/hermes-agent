/**
 * "Painted" end for the view actions (`switch`, `open_chat`): the user picked a
 * chat and waits for its transcript. Start at the click, bind the stored
 * session id once the open path knows it, end on the animation frame after a
 * non-empty transcript for that id lands in a store:
 *
 * - main pane: `$selectedStoredSessionId === id` and `$messages` non-empty and
 *   no longer the array that was on screen at the click;
 * - a workspace tile / tab (every Bot Chat): that tile is the one on screen
 *   (`$focusedStoredSessionId`, which in Bots mode is the workspace's fronted
 *   tile) and its `$sessionStates` entry holds a non-empty transcript. Gating on
 *   the fronted tile matters: the state cache ingests every session's events, so
 *   a busy bot's array changes on each delta whether or not it is on screen.
 *
 * One view is pending at a time (the window shows one chat): a newer view
 * start replaces the older one. Store subscriptions exist ONLY while a view is
 * pending, so idle cost is zero; nothing here renders or awaits.
 */

import type { ChatMessage } from '@/lib/chat-messages'
import { $messages, $selectedStoredSessionId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionStates } from '@/store/session-states'

import { ACTION_TIMING_PENDING_TTL_MS, actionAbandon, actionEnd, actionNow, actionStart } from './action-timing'

export type ViewAction = 'open_chat' | 'switch'

interface PendingView {
  action: ViewAction
  key: string
  startedAt: number
  mainAtStart: ChatMessage[]
  focusedAtStart: null | string
  storedSessionId: null | string
}

let view: null | PendingView = null
let unsubscribers: (() => void)[] = []

function unsubscribe() {
  for (const off of unsubscribers) {
    try {
      off()
    } catch {
      /* already gone */
    }
  }

  unsubscribers = []
}

function stateMessagesFor(storedSessionId: string): ChatMessage[] | undefined {
  const states = $sessionStates.get()

  for (const runtimeId in states) {
    const state = states[runtimeId]

    if (state?.storedSessionId === storedSessionId && state.messages?.length) {
      return state.messages
    }
  }

  return undefined
}

function nextFrame(run: () => void) {
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => run())
  } else {
    setTimeout(run, 0)
  }
}

function finish(current: PendingView) {
  view = null
  unsubscribe()
  nextFrame(() => {
    actionEnd(current.action, current.key)
  })
}

function check() {
  try {
    const current = view

    if (!current) {
      unsubscribe()

      return
    }

    if (actionNow() - current.startedAt >= ACTION_TIMING_PENDING_TTL_MS) {
      // The timing module counts the expiry as a drop on its own flush.
      view = null
      unsubscribe()

      return
    }

    const id = current.storedSessionId

    if (!id) {
      return
    }

    const main = $messages.get()

    if ($selectedStoredSessionId.get() === id && main.length > 0 && main !== current.mainAtStart) {
      finish(current)

      return
    }

    if (stateMessagesFor(id) && $focusedStoredSessionId.get() === id && $selectedStoredSessionId.get() !== id) {
      finish(current)
    }
  } catch {
    /* never surface */
  }
}

function subscribe() {
  if (unsubscribers.length > 0) {
    return
  }

  unsubscribers = [
    $messages.listen(check),
    $selectedStoredSessionId.listen(check),
    $sessionStates.listen(check),
    $focusedStoredSessionId.listen(check)
  ]
}

/** The user picked a chat (click / row). `storedSessionId` when already known. */
export function viewActionStart(action: ViewAction, key: string, storedSessionId?: null | string): void {
  try {
    if (view && (view.action !== action || view.key !== key)) {
      actionAbandon(view.action, view.key)
    }

    actionStart(action, key)
    view = {
      action,
      key,
      startedAt: actionNow(),
      mainAtStart: $messages.get(),
      focusedAtStart: $focusedStoredSessionId.get() ?? null,
      storedSessionId: null
    }

    if (storedSessionId) {
      viewActionBind(action, key, storedSessionId)
    }
  } catch {
    /* never surface */
  }
}

export interface ViewBindOptions {
  /** The transcript is already on screen (an open tab was fronted). */
  painted?: boolean
}

/** The open path resolved which stored session the pending view will show. */
export function viewActionBind(
  action: ViewAction,
  key: string,
  storedSessionId: string,
  options?: ViewBindOptions
): void {
  try {
    const current = view

    if (!current || current.action !== action || current.key !== key || !storedSessionId) {
      return
    }

    if (options?.painted) {
      finish(current)

      return
    }

    const main = $messages.get()

    // Re-picking the chat that is already on screen is not an open: nothing loads, and a
    // one-frame sample would drag the p50 of real opens down. Not counted at all.
    const mainAlreadyShown =
      $selectedStoredSessionId.get() === storedSessionId && main.length > 0 && main === current.mainAtStart

    const tileAlreadyShown = current.focusedAtStart === storedSessionId && Boolean(stateMessagesFor(storedSessionId))

    if (mainAlreadyShown || tileAlreadyShown) {
      view = null
      unsubscribe()
      actionAbandon(action, key)

      return
    }

    current.storedSessionId = storedSessionId
    subscribe()
    check()
  } catch {
    /* never surface */
  }
}

/** The open failed or was superseded by something that is not a view open. */
export function viewActionAbandon(action: ViewAction, key: string): void {
  try {
    if (view?.action === action && view.key === key) {
      view = null
      unsubscribe()
    }

    actionAbandon(action, key)
  } catch {
    /* never surface */
  }
}

/** Test hook. */
export function __resetViewTimingForTests(): void {
  view = null
  unsubscribe()
}
