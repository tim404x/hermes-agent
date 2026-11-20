/**
 * Bot Mode's hooks into the desktop's user-felt action timing (desktop.log
 * `[perf-actions]`): the first roster applied after launch (`open_roster`) and
 * the bot switch (`switch`: row click -> that bot's transcript painted).
 *
 * Namespace access inside try/catch on purpose: timing must never throw into
 * the open path, including under test doubles of the SDK that omit these
 * exports.
 */

import * as sdk from '@hermes/plugin-sdk'

/** A roster snapshot was fetched and handed to the query cache. */
export function noteRosterLoaded<T>(snapshot: T): T {
  try {
    sdk.markActionOnce('open_roster')
  } catch {
    /* timing is best-effort */
  }

  return snapshot
}

/** The user picked this bot (`key` = botRosterKey). */
export function noteSwitchStarted(key: string): void {
  try {
    sdk.viewActionStart('switch', key)
  } catch {
    /* timing is best-effort */
  }
}

/** The switch resolved the stored session it will show. `painted`: an open
 *  tab was fronted, so the transcript is already on screen. */
export function noteSwitchTarget(key: (() => string) | string, storedSessionId: string, painted = false): void {
  try {
    const switchKey = typeof key === 'function' ? key() : key
    sdk.viewActionBind('switch', switchKey, storedSessionId, painted ? { painted: true } : undefined)
  } catch {
    /* timing is best-effort */
  }
}

/** The switch failed or fell back to a fresh draft (nothing to paint). */
export function noteSwitchAbandoned(key: string): void {
  try {
    sdk.viewActionAbandon('switch', key)
  } catch {
    /* timing is best-effort */
  }
}
