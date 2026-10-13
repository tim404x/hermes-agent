/**
 * A teammate's message_agent side window opens as a tab beside that bot's
 * Bot Chat (profiles.list `side_windows`, stamped by the delivery runner).
 *
 * Contract: a NEW stamp opens (or re-badges) the tab exactly once; the same
 * stamp never reopens a tab the user closed; only a later DM does. The first
 * poll on a desktop that never ran this treats existing windows as history.
 */

import { host } from '@hermes/plugin-sdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $botMeta } from './data'
import * as shared from './shared'
import { resetSideWindowTabsForTests, trackSideWindows } from './side-window-tabs'
import type { RosterRow } from './types'

const store = new Map<string, unknown>()

function useStorage() {
  shared.setPluginCtx({
    storage: {
      get: (key: string, fallback: unknown) => (store.has(key) ? store.get(key) : fallback),
      remove: (key: string) => store.delete(key),
      set: (key: string, value: unknown) => store.set(key, JSON.parse(JSON.stringify(value)))
    }
  } as unknown as Parameters<typeof shared.setPluginCtx>[0])
}

/** A (re)launched window: the handled stamps come back from plugin storage. */
function freshTracker() {
  resetSideWindowTabsForTests()

  return trackSideWindows
}

const NOW = 1_800_000_000

function atlas(windows: { at: number; id: string }[], extra: Partial<RosterRow> = {}): RosterRow {
  return {
    canonical_session: { id: 'atlas-chat', resolved_id: 'atlas-chat-tip' },
    connectionId: 'local',
    name: 'atlas',
    side_windows: windows.map(w => ({ ...w, by: 'bot:neo', title: `window ${w.id}` })),
    ...extra
  } as RosterRow
}

const open = vi.fn()

beforeEach(() => {
  store.clear()
  useStorage()
  open.mockReset()
  $botMeta.set({})
  host.openBackgroundSession = open as never
})

afterEach(() => {
  // @ts-expect-error — restore the harness default (an older desktop without the verb).
  delete host.openBackgroundSession
})

describe('trackSideWindows', () => {
  it('treats the windows of a first run as history', () => {
    const track = freshTracker()

    track([atlas([{ at: NOW - 60, id: 'sw-old' }])], NOW)

    expect(open).not.toHaveBeenCalled()
  })

  it('opens a new window beside the bot chat, once per DM', () => {
    let track = freshTracker()
    track([atlas([])], NOW)

    track([atlas([{ at: NOW, id: 'sw-1' }])], NOW)
    track([atlas([{ at: NOW, id: 'sw-1' }])], NOW + 5)

    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith('sw-1', {
      besideStoredIds: ['atlas-chat', 'atlas-chat-tip'],
      profile: 'atlas',
      // A row on this connection has no cross-connection route: the profile alone owns it.
      workspaceOwnerKey: 'bot:atlas'
    })

    // A relaunch remembers what it handled: a tab the user closed stays closed...
    track = freshTracker()
    track([atlas([{ at: NOW, id: 'sw-1' }])], NOW + 60)
    expect(open).toHaveBeenCalledTimes(1)

    // ...until a new DM lands in that window.
    track([atlas([{ at: NOW + 90, id: 'sw-1' }])], NOW + 120)
    expect(open).toHaveBeenCalledTimes(2)
  })

  it('opens a window that arrived while the app was closed', () => {
    let track = freshTracker()
    track([atlas([])], NOW)

    track = freshTracker()
    track([atlas([{ at: NOW + 3600, id: 'sw-night' }])], NOW + 7200)

    expect(open).toHaveBeenCalledWith('sw-night', expect.anything())
  })

  it('never opens a stale window it first meets, nor windows of hidden or offline rows', () => {
    $botMeta.set({ 'hidden-bot': { hidden: true } })
    const track = freshTracker()
    track([atlas([])], NOW)

    track(
      [
        atlas([{ at: NOW - 3 * 24 * 3600, id: 'sw-stale' }]),
        atlas([{ at: NOW, id: 'sw-hidden' }], { name: 'hidden-bot' }),
        atlas([{ at: NOW, id: 'sw-retained' }], { name: 'offline-bot', retained: true })
      ],
      NOW
    )

    expect(open).not.toHaveBeenCalled()
  })

  it('is inert on a desktop without the background-tab verb', () => {
    // @ts-expect-error — an older desktop.
    delete host.openBackgroundSession
    const track = freshTracker()

    expect(() => track([atlas([{ at: NOW, id: 'sw-1' }])], NOW)).not.toThrow()
  })
})
