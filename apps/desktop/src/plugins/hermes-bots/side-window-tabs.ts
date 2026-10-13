/**
 * A teammate's `message_agent` side window as a tab. When a bot's Bot Chat
 * is busy, a DM to it opens in its own titled session (the side window) and
 * the delivery runner stamps it; `profiles.list` reports the stamps as
 * `side_windows`. Each NEW stamp opens that window as an unread tab beside the
 * bot's Bot Chat, or re-badges the tab when it is already open. Nothing is
 * fronted, nothing navigates, and only stamped windows qualify: plain CLI
 * chats, crons and workers never open here.
 *
 * The newest stamp handled per window is remembered in plugin storage, so a
 * tab the user closed stays closed until another DM lands in that window, and
 * a window that arrived while the app was closed still opens on the next poll.
 */

import { host } from '@hermes/plugin-sdk'

import { $botMeta } from './data'
import { botConnectionRoute, botRosterMeta, botWorkspaceOwnerKey } from './routing'
import { getPluginCtx } from './shared'
import type { RosterRow } from './types'

const STORAGE_KEY = 'side-window-tabs-v1'
/** A window first met with a DM older than this is history, not news (a
 *  connection added today brings its old windows along). Unix seconds. */
const FRESH_SECONDS = 24 * 60 * 60
/** Handled stamps kept; the oldest go first. One entry per window ever opened. */
const HANDLED_CAP = 500

/** `<owner key>\n<session id>` → newest DM stamp handled (unix seconds). */
let handled: null | Record<string, number> = null
/** No record at all: this desktop never ran the tracker, so its first poll
 *  seeds instead of opening every recent window at once. */
let firstRun = false

function loadHandled(): Record<string, number> {
  const stored = getPluginCtx()?.storage?.get<unknown>(STORAGE_KEY, null)
  firstRun = !stored || typeof stored !== 'object'
  const entries = firstRun ? [] : Object.entries(stored as Record<string, unknown>)

  return Object.fromEntries(entries.filter((entry): entry is [string, number] => Number.isFinite(entry[1])))
}

function saveHandled(record: Record<string, number>): void {
  const kept = Object.entries(record)
    .sort((a, b) => b[1] - a[1])
    .slice(0, HANDLED_CAP)

  handled = Object.fromEntries(kept)

  try {
    getPluginCtx()?.storage?.set(STORAGE_KEY, handled)
  } catch {
    /* storage unavailable — this window still remembers until it closes */
  }
}

function openSideWindowTab(bot: RosterRow, workspaceOwnerKey: string, storedSessionId: string): void {
  let route: ReturnType<typeof botConnectionRoute>

  try {
    route = botConnectionRoute(bot)
  } catch {
    return // the bot's connection was removed: nowhere to open it
  }

  const canonical = bot.canonical_session

  host.openBackgroundSession(storedSessionId, {
    besideStoredIds: [canonical?.id, canonical?.resolved_id].filter(Boolean).map(String),
    profile: bot.name,
    ...(route ? { route } : {}),
    workspaceOwnerKey
  })
}

/** Feed every roster poll: open (or re-badge) the tab of each side window
 *  whose stamp is newer than the one already handled. */
export function trackSideWindows(roster: readonly RosterRow[], nowSeconds = Date.now() / 1000): void {
  if (typeof host.openBackgroundSession !== 'function') {
    return
  }

  const record = (handled ??= loadHandled())
  const seeding = firstRun
  let changed = seeding

  firstRun = false

  for (const bot of roster) {
    // A retained row repaints a source that did not answer: not evidence of a new DM.
    if (!bot || bot.ghost || bot.retained || !Array.isArray(bot.side_windows)) {
      continue
    }

    const ownerKey = botWorkspaceOwnerKey(bot)
    const quiet = Boolean(botRosterMeta(bot, $botMeta.get())?.hidden)

    for (const window of bot.side_windows) {
      const id = String(window?.id || '').trim()
      const at = Number(window?.at) || 0
      const key = `${ownerKey}\n${id}`
      const known = key in record

      if (!id || at <= 0 || (known && at <= record[key])) {
        continue
      }

      record[key] = at
      changed = true

      if (seeding || quiet || (!known && nowSeconds - at > FRESH_SECONDS)) {
        continue
      }

      openSideWindowTab(bot, ownerKey, id)
    }
  }

  if (changed) {
    saveHandled(record)
  }
}

/** Test seam: forget the in-memory record, as a relaunched window would. */
export function resetSideWindowTabsForTests(): void {
  handled = null
  firstRun = false
}
