import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo, SidebarSessionsResponse } from '@/hermes'
import {
  __flushActionTimingForTests,
  __resetActionTimingForTests,
  ACTION_TIMING_LINE_PREFIX,
  actionStart
} from '@/lib/action-timing'
import { setSessions, setSessionsLoadError, setSessionsLoading } from '@/store/session'

import { deferred } from '../../../test/deferred'

import { useSessionListActions } from './use-session-list-actions'

// Action timing at the sidebar list apply: `open_sidebar` (first non-empty list
// after load) and `sidebar_update` (sessions.changed -> the next list refresh
// that STARTED after the event is applied).

const row = (id: string): SessionInfo =>
  ({
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1000,
    message_count: 3,
    model: 'm',
    output_tokens: 0,
    preview: 'hey',
    profile: 'default',
    source: 'desktop',
    started_at: 900,
    title: `Chat ${id}`
  }) as SessionInfo

const sidebar = (sessions: SessionInfo[]): SidebarSessionsResponse => ({
  recents: { sessions },
  cron: { sessions: [] },
  messaging: { sessions: [] }
})

const listSidebarSessions = vi.fn()
const clock = vi.hoisted(() => ({ now: 0 }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  listSidebarSessions: (...args: unknown[]) => listSidebarSessions(...args)
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  gatewayActivationEpoch: () => 0
}))

vi.mock('@/store/session-removal', async importActual => ({
  ...(await importActual<Record<string, unknown>>()),
  $removedSessionIds: { get: () => new Set<string>() }
}))

function flushed(): { a: Record<string, number[]> } | null {
  const line = __flushActionTimingForTests()

  return line ? JSON.parse(line.slice(ACTION_TIMING_LINE_PREFIX.length)) : null
}

beforeEach(() => {
  __resetActionTimingForTests()
  clock.now = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => clock.now)
  listSidebarSessions.mockReset()
  setSessions([])
  setSessionsLoading(false)
  setSessionsLoadError(false)
})

afterEach(() => {
  __resetActionTimingForTests()
  vi.restoreAllMocks()
  setSessions([])
})

describe('sidebar action timing', () => {
  it('marks open_sidebar once, at the first NON-empty list applied', async () => {
    listSidebarSessions.mockResolvedValueOnce(sidebar([]))
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    await act(async () => {
      await result.current.refreshSessions()
    })
    expect(flushed()).toBeNull()

    clock.now = 2500
    listSidebarSessions.mockResolvedValueOnce(sidebar([row('a')]))
    await act(async () => {
      await result.current.refreshSessions()
    })
    clock.now = 4000
    listSidebarSessions.mockResolvedValueOnce(sidebar([row('a'), row('b')]))
    await act(async () => {
      await result.current.refreshSessions()
    })

    expect(flushed()?.a).toEqual({ open_sidebar: [2500] })
  })

  it('sidebar_update is served only by a refresh that started after the event', async () => {
    setSessions([row('a')])
    const early = deferred<SidebarSessionsResponse>()
    listSidebarSessions.mockReturnValueOnce(early.promise)
    const { result } = renderHook(() => useSessionListActions({ profileScope: 'default' }))

    // A refresh already in flight when sessions.changed lands...
    let earlyRun: Promise<void> = Promise.resolve()
    act(() => {
      earlyRun = result.current.refreshSessions()
    })
    clock.now = 1100
    actionStart('sidebar_update', '', { replace: false })
    clock.now = 1150
    // ...and a second event before any refresh serves the first.
    actionStart('sidebar_update', '', { replace: false })

    // The newer refresh (started after the event) supersedes and serves it.
    const late = deferred<SidebarSessionsResponse>()
    listSidebarSessions.mockReturnValueOnce(late.promise)
    clock.now = 1200
    let lateRun: Promise<void> = Promise.resolve()
    act(() => {
      lateRun = result.current.refreshSessions()
    })

    clock.now = 1300
    await act(async () => {
      early.resolve(sidebar([row('a'), row('b')]))
      await earlyRun
    })
    clock.now = 1450
    await act(async () => {
      late.resolve(sidebar([row('a'), row('b')]))
      await lateRun
    })

    // From the FIRST unserved event (1100) to the apply (1450); the earlier
    // in-flight refresh could not serve it.
    expect(flushed()?.a.sidebar_update).toEqual([350])
  })
})
