/**
 * Switching between two bots is a warm switch, not a reload.
 *
 * The real click path end to end: roster row click (`openRosterBot`) → the
 * canonical-chat registry open → the SDK's `host.openSession` → the core
 * in-place open → the route resume → `resumeSession` with the REAL session
 * state cache. Only the transport is fake: every gateway RPC and REST read
 * goes through one virtual backend with fixed latencies (measured from the
 * Mac against the live VPS, see the bot-switch-speed-1119 measurements), so
 * timings are deterministic virtual milliseconds, never wall clock.
 *
 * What a user must see on A → B → A once both chats were opened in this
 * window: no "Waking up …" overlay and no blank thread, the target's last
 * thread painted from cache within the paint budget, the background refresh
 * still landing new rows, no duplicate rows, and a live turn on A still
 * streaming after the round trip.
 */

import { useStore } from '@nanostores/react'
import { act, cleanup, render } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatSwapOverlay } from '@/app/chat/chat-swap-overlay'
import { PRIMARY_SESSION_VIEW } from '@/app/chat/session-view'
import type * as OpenSessionModule from '@/app/open-session'
import { getLatestSessionMessages, getSession } from '@/hermes'
import type { RosterRow } from '@/plugins/hermes-bots/types'
import {
  openGatewayForAgent,
  openGatewayForProfile,
  requestGatewayForAgent,
  requestGatewayForProfile
} from '@/store/gateway'
import { $gatewaySwapTarget, ensureGatewayAgent, ensureGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $freshDraftReady,
  $gatewayState,
  $messages,
  $resumeExhaustedSessionId,
  $resumeFailedSessionId,
  $selectedStoredSessionId,
  $sessionResumeRequest,
  setActiveSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages,
  setSelectedStoredSessionId,
  setSessions,
  setUnlistedSessionOwnerRows
} from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'

import { useRouteResume } from './use-route-resume'
import { useSessionActions } from './use-session-actions'
import { useSessionStateCache } from './use-session-state-cache'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getSession: vi.fn(),
  getLatestSessionMessages: vi.fn(),
  getAllSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(),
  ensureGatewayProfile: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGatewayConnectionId: vi.fn(() => CONNECTION),
  openGatewayForAgent: vi.fn(),
  openGatewayForProfile: vi.fn(),
  requestGatewayForAgent: vi.fn(),
  requestGatewayForProfile: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  noteActiveTreeGroup: vi.fn(),
  revealTreePane: vi.fn()
}))

// The core in-place open navigates with the router; this harness has no
// router, so the navigate the SDK hands it becomes the harness route.
vi.mock('@/app/open-session', async importOriginal => {
  const original = await importOriginal<typeof OpenSessionModule>()

  return {
    ...original,
    openSession: vi.fn((id: string, _navigate: unknown, intent: never, scope: never) =>
      original.openSession(id, to => harnessRoute.navigate(to), intent, scope)
    )
  }
})

const CONNECTION = 'vps-7777'

/** Virtual latencies (ms) as the Mac sees them through the SSH tunnel to the
 *  VPS: ~75 ms socket RTT, ~165 ms per HTTP request, plus server time
 *  (measurements.md, 19 Nov 2026, load 40). P95 is what a loaded VPS gives. */
const P50 = {
  ipc: 2,
  'session.list': 125,
  'session.activate': 110,
  'session.resume': 260,
  'session.info': 110,
  rpc: 110,
  restSession: 280,
  restPage: 545
}

const P95 = {
  ipc: 2,
  'session.list': 375,
  'session.activate': 300,
  'session.resume': 500,
  'session.info': 300,
  rpc: 300,
  restSession: 500,
  restPage: 1_500
}

let LATENCY = P95

/** A frame between a route change and the route-resume effect. */
const FRAME_MS = 16
/** The paint budget a warm switch must meet (task target: under 200 ms). */
const PAINT_BUDGET_MS = 200

interface BotFixture {
  name: string
  running?: boolean
  storedId: string
  runtimeId: string
  rows: Array<{ content: string; id: number; role: string; timestamp: number }>
}

const bots: Record<string, BotFixture> = {}

function makeBot(name: string): BotFixture {
  const rows = Array.from({ length: 6 }, (_, index) => ({
    content: `${name} row ${index}`,
    id: index + 1,
    role: index % 2 === 0 ? 'user' : 'assistant',
    timestamp: 1_000 + index
  }))

  return { name, storedId: `${name}-bot-chat`, runtimeId: `rt-${name}`, rows }
}

const harnessRoute = {
  path: '/',
  navigate(to: string) {
    harnessRoute.path = to.startsWith('#') ? to.slice(1) : to
    harnessRoute.listener?.()
  },
  listener: null as null | (() => void)
}

interface Trace {
  calls: Array<{ at: number; method: string; profile?: string }>
  overlay: Array<{ at: number; value: null | string }>
  published: Array<{ at: number; ids: string[]; text: string }>
}

const trace: Trace = { calls: [], overlay: [], published: [] }

function later<T>(ms: number, value: () => T): Promise<T> {
  return new Promise((resolve, reject) => {
    setTimeout(() => {
      try {
        resolve(value())
      } catch (error) {
        reject(error)
      }
    }, ms)
  })
}

function botFor(profile: unknown): BotFixture {
  const bot = bots[String(profile || '')]

  if (!bot) {
    throw new Error(`no fake bot for profile ${String(profile)}`)
  }

  return bot
}

function botForSession(id: unknown): BotFixture {
  const bot = Object.values(bots).find(candidate => candidate.storedId === id || candidate.runtimeId === id)

  if (!bot) {
    throw new Error(`no fake bot for session ${String(id)}`)
  }

  return bot
}

function sessionRow(bot: BotFixture) {
  return {
    id: bot.storedId,
    title: 'Bot Chat',
    hidden: true,
    profile: bot.name,
    connection_id: CONNECTION,
    message_count: bot.rows.length,
    live_message_count: bot.rows.length,
    source: 'desktop',
    started_at: 1,
    last_active: bot.rows.at(-1)!.timestamp,
    ended_at: null,
    input_tokens: 0,
    output_tokens: 0,
    is_active: false,
    model: null,
    preview: null,
    tool_call_count: 0
  }
}

/** The one virtual backend every fake transport call goes through. */
function backend(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
  trace.calls.push({ at: Date.now(), method, profile: String(params.profile ?? '') || undefined })

  switch (method) {
    case 'session.list': {
      const bot = botFor(params.profile)

      return later(LATENCY['session.list'], () => ({ sessions: [sessionRow(bot)] }))
    }

    case 'session.info':
      return later(LATENCY['session.info'], () => sessionRow(botForSession(params.session_id)))
    case 'session.activate': {
      const bot = botForSession(params.session_id)

      return later(LATENCY['session.activate'], () => ({
        info: {},
        message_count: bot.rows.length,
        messages: [],
        messages_omitted: true,
        resumed: bot.storedId,
        running: Boolean(bot.running),
        session_id: bot.runtimeId,
        session_key: bot.storedId
      }))
    }

    case 'session.resume': {
      const bot = botForSession(params.session_id)

      return later(LATENCY['session.resume'], () => ({
        info: {},
        message_count: bot.rows.length,
        messages: [],
        messages_omitted: true,
        resumed: bot.storedId,
        running: false,
        session_id: bot.runtimeId,
        session_key: bot.storedId
      }))
    }

    default:
      return later(LATENCY.rpc, () => ({}))
  }
}

function installTransport() {
  vi.mocked(requestGatewayForAgent).mockImplementation(((
    _connectionId: null | string,
    profile: string,
    method: string,
    params?: Record<string, unknown>
  ) => backend(method, { profile, ...(params ?? {}) })) as never)
  vi.mocked(requestGatewayForProfile).mockImplementation(((
    profile: string,
    method: string,
    params?: Record<string, unknown>
  ) => backend(method, { profile, ...(params ?? {}) })) as never)
  vi.mocked(openGatewayForAgent).mockImplementation(() => later(LATENCY.ipc, () => undefined))
  vi.mocked(openGatewayForProfile).mockImplementation(() => later(LATENCY.ipc, () => undefined))
  vi.mocked(ensureGatewayAgent).mockImplementation(() => later(LATENCY.ipc, () => true) as never)
  vi.mocked(ensureGatewayProfile).mockImplementation(() => later(LATENCY.ipc, () => undefined))
  vi.mocked(getSession).mockImplementation(((id: string) => {
    trace.calls.push({ at: Date.now(), method: 'GET /api/sessions/:id' })

    return later(LATENCY.restSession, () => sessionRow(botForSession(id)))
  }) as never)
  vi.mocked(getLatestSessionMessages).mockImplementation(((id: string) => {
    trace.calls.push({ at: Date.now(), method: 'GET /api/sessions/:id/messages' })
    const bot = botForSession(id)

    return later(LATENCY.restPage, () => ({ messages: bot.rows.map(row => ({ ...row })), session_id: bot.storedId }))
  }) as never)
}

/** The app shell slice a bot open drives: real session cache, real session
 *  actions, real route resume. */
function AppHarness({ onReady }: { onReady: (cache: ReturnType<typeof useSessionStateCache>) => void }) {
  const activeSessionId = useStore($activeSessionId)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const freshDraftReady = useStore($freshDraftReady)
  const gatewayState = useStore($gatewayState)
  const sessionResumeRequest = useStore($sessionResumeRequest)
  const resumeFailedSessionId = useStore($resumeFailedSessionId)
  const resumeExhaustedSessionId = useStore($resumeExhaustedSessionId)
  const busyRef = useRef(false)
  const creatingSessionRef = useRef(false)
  const path = useRoutePath()
  const pathRef = useRef(path)

  pathRef.current = path

  const cache = useSessionStateCache({
    activeSessionId,
    busyRef,
    selectedStoredSessionId,
    setAwaitingResponse,
    setBusy,
    setMessages
  })

  const routedSessionId = path.startsWith('/') && path.length > 1 ? decodeURIComponent(path.slice(1)) : null

  const actions = useSessionActions({
    activeSessionId,
    activeSessionIdRef: cache.activeSessionIdRef,
    busyRef,
    creatingSessionRef,
    ensureSessionState: cache.ensureSessionState,
    getRouteToken: () => pathRef.current,
    getRoutedStoredSessionId: () => routedSessionId,
    holdSessionTranscriptView: cache.holdSessionTranscriptView,
    navigate: ((to: string) => harnessRoute.navigate(to)) as never,
    requestGateway: ((method: string, params?: Record<string, unknown>) => backend(method, params)) as never,
    routedSessionId,
    resetViewSync: cache.resetViewSync,
    runtimeIdByStoredSessionIdRef: cache.runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef: cache.selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef: cache.sessionStateByRuntimeIdRef,
    syncSessionStateToView: cache.syncSessionStateToView,
    updateSessionState: cache.updateSessionState
  })

  useRouteResume({
    activeSessionId,
    activeSessionIdRef: cache.activeSessionIdRef,
    creatingSessionRef,
    currentView: 'chat',
    freshDraftReady,
    gatewayState,
    locationPathname: path,
    resumeExhaustedSessionId,
    resumeFailedSessionId,
    resumeSession: actions.resumeSession,
    routedSessionId,
    runtimeIdByStoredSessionIdRef: cache.runtimeIdByStoredSessionIdRef,
    selectedStoredSessionId,
    selectedStoredSessionIdRef: cache.selectedStoredSessionIdRef,
    sessionResumeRequest,
    startFreshSessionDraft: actions.startFreshSessionDraft
  } as never)

  useEffect(() => {
    onReady(cache)
  }, [cache, onReady])

  return null
}

function useRoutePath(): string {
  const [path, setPath] = useState(harnessRoute.path)

  useEffect(() => {
    // The router commits the route a frame after the hash changes.
    harnessRoute.listener = () => {
      setTimeout(() => setPath(harnessRoute.path), FRAME_MS)
    }

    return () => {
      harnessRoute.listener = null
    }
  }, [])

  return path
}

const { openRosterBot } = await import('@/plugins/hermes-bots/roster-actions')
const { $openBotChat, $pendingBotOpen } = await import('@/plugins/hermes-bots/bot-state')

function roster(bot: BotFixture): RosterRow {
  return {
    canonical_session: { id: bot.storedId, resolved_id: bot.storedId, title: 'Bot Chat' },
    connectionId: CONNECTION,
    connectionKind: 'remote',
    name: bot.name,
    route: { connectionId: CONNECTION, mode: 'remote', profile: bot.name, targetProfile: bot.name },
    sourceScoped: true,
    targetProfile: bot.name
  } as RosterRow
}

const primaryIds = () => PRIMARY_SESSION_VIEW.$messages.get().map(message => String(message.id))
const primaryText = () => JSON.stringify(PRIMARY_SESSION_VIEW.$messages.get())

let cache!: ReturnType<typeof useSessionStateCache>
let container: HTMLElement | null = null
/** Virtual ms the rendered "Waking up" overlay was visible (opacity-100). */
let overlayMs = 0

function SwapOverlayProbe() {
  return <ChatSwapOverlay profile={useStore($gatewaySwapTarget)} />
}

let unlisten: Array<() => void> = []

async function mount() {
  ;({ container } = render(
    <>
      <AppHarness onReady={ready => (cache = ready)} />
      <SwapOverlayProbe />
    </>
  ))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  unlisten = [
    $gatewaySwapTarget.listen(value => trace.overlay.push({ at: Date.now(), value })),
    PRIMARY_SESSION_VIEW.$messages.listen(messages =>
      trace.published.push({
        at: Date.now(),
        ids: messages.map(message => String(message.id)),
        text: JSON.stringify(messages)
      })
    )
  ]
}

async function writeTrace(kind: string, data: unknown) {
  const file = process.env.BOT_SWITCH_TRACE

  if (file) {
    const fs = await import('node:fs')
    fs.appendFileSync(file, `${JSON.stringify({ kind, test: expect.getState().currentTestName, data })}\n`)
  }
}

/** Advance the virtual clock in small steps, each in its own act scope, so
 *  React commits (and runs effects) between timers the way a real frame
 *  would. One act around the whole span would defer every render to its end. */
async function advance(ms: number, step = 4) {
  for (let elapsed = 0; elapsed < ms; elapsed += step) {
    const slice = Math.min(step, ms - elapsed)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(slice)
    })

    if (container?.querySelector('.opacity-100')) {
      overlayMs += slice
    }
  }
}

/** Click a bot row and let the whole open (and its background refresh) run
 *  to quiescence. Returns the click instant on the virtual clock. */
async function clickBot(bot: BotFixture, settleMs = 3_000): Promise<number> {
  const clickedAt = Date.now()
  let done = false

  act(() => {
    void openRosterBot(roster(bot)).finally(() => {
      done = true
    })
  })
  await advance(settleMs)

  if (!done) {
    await writeTrace('stuck', {
      calls: trace.calls.map(call => `${call.at - clickedAt} ${call.method}`),
      overlay: trace.overlay,
      path: harnessRoute.path,
      selected: $selectedStoredSessionId.get(),
      active: $activeSessionId.get(),
      messages: $messages.get().length,
      primary: primaryIds().length,
      request: $sessionResumeRequest.get()
    })
  }

  expect(done).toBe(true)

  return clickedAt
}

/** Virtual ms from `since` until the primary view first showed all of
 *  `bot`'s rows, or null when it never did. */
function paintedAt(bot: BotFixture, since: number, lastRow = bot.rows.length - 1): null | number {
  const hit = trace.published.find(entry => entry.at >= since && entry.text.includes(`${bot.name} row ${lastRow}`))

  return hit ? hit.at - since : null
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  vi.setSystemTime(1_800_000_000_000)
  trace.calls = []
  trace.overlay = []
  trace.published = []
  overlayMs = 0
  LATENCY = P95
  window.localStorage.clear()
  harnessRoute.path = '/'
  Object.assign(bots, { atlas: makeBot('atlas'), apex: makeBot('apex') })
  installTransport()
  setSessions([])
  setUnlistedSessionOwnerRows([])
  $gatewayState.set('open')
  $openBotChat.set(null)
  $pendingBotOpen.set(null)
})

afterEach(() => {
  for (const off of unlisten) {
    off()
  }

  unlisten = []
  cleanup()
  clearAllSessionStates()
  setActiveSessionId(null)
  setSelectedStoredSessionId(null)
  setMessages([])
  setSessions([])
  setUnlistedSessionOwnerRows([])
  $gatewaySwapTarget.set(null)
  vi.useRealTimers()
  vi.clearAllMocks()
})

function sinceClick(at: number) {
  return {
    calls: trace.calls
      .filter(call => call.at >= at)
      .map(call => `${call.at - at}ms ${call.method}${call.profile ? ` [${call.profile}]` : ''}`),
    overlay: trace.overlay
      .filter(entry => entry.at >= at)
      .map(entry => `${entry.at - at}ms ${entry.value ?? 'cleared'}`),
    published: trace.published
      .filter(entry => entry.at >= at)
      .map(entry => `${entry.at - at}ms ${entry.ids.length} rows`)
  }
}

/** Virtual ms the primary thread sat EMPTY after the click: an empty
 *  publication counts only while it stays on screen past its own instant. */
function blankMs(since: number, until: number): number {
  const entries = trace.published.filter(entry => entry.at >= since && entry.at <= until)
  let blank = 0

  entries.forEach((entry, index) => {
    if (entry.ids.length === 0) {
      blank += (entries[index + 1]?.at ?? until) - entry.at
    }
  })

  return blank
}

function duplicateIds(): string[] {
  const ids = primaryIds()

  return ids.filter((id, index) => ids.indexOf(id) !== index)
}

describe('bot switch A → B → A', () => {
  for (const [label, profile] of [
    ['p50', P50],
    ['p95', P95]
  ] as const) {
    it(`records the call-by-call trace of a cold and a warm switch (${label} latencies)`, async () => {
      LATENCY = profile
      await mount()
      const atlas = bots.atlas!
      const apex = bots.apex!

      const coldA = await clickBot(atlas, 9_000)
      const cold = { paint: paintedAt(atlas, coldA), blank: blankMs(coldA, coldA + 9_000), ...sinceClick(coldA) }
      await clickBot(apex, 9_000)
      const warmA = await clickBot(atlas)
      const warm = { paint: paintedAt(atlas, warmA), blank: blankMs(warmA, warmA + 3_000), ...sinceClick(warmA) }

      await writeTrace(`trace-${label}`, { cold, warm, overlaySeen: overlayMs })

      expect(primaryText()).toContain('atlas row 5')
      expect(duplicateIds()).toEqual([])
    })
  }

  it('paints a warm target from cache inside the budget, with no loading state', async () => {
    await mount()
    const atlas = bots.atlas!
    const apex = bots.apex!

    await clickBot(atlas, 9_000)
    await clickBot(apex, 9_000)
    overlayMs = 0
    const clickedAt = await clickBot(atlas)

    // (b) painted from cache before any network round trip could land.
    const painted = paintedAt(atlas, clickedAt)
    expect(painted).not.toBeNull()
    expect(painted!).toBeLessThanOrEqual(PAINT_BUDGET_MS)
    // (a) no blank thread and no "Waking up" overlay on screen.
    expect(blankMs(clickedAt, clickedAt + 3_000)).toBe(0)
    expect(overlayMs).toBe(0)
    // (d) no duplicate rows.
    expect(primaryText()).toContain('atlas row 5')
    expect(duplicateIds()).toEqual([])
    expect($openBotChat.get()).toMatchObject({ openedSessionId: atlas.storedId, openedRegistryId: atlas.storedId })
  })

  it('shows a message that arrived for B while A was on screen', async () => {
    await mount()
    const atlas = bots.atlas!
    const apex = bots.apex!

    await clickBot(apex, 9_000)
    await clickBot(atlas, 9_000)
    // A cron delivery / teammate DM lands in B's chat while A is on screen.
    apex.rows.push(
      { content: 'apex row 6', id: 7, role: 'user', timestamp: 2_000 },
      { content: 'apex row 7', id: 8, role: 'assistant', timestamp: 2_001 }
    )
    vi.mocked(getLatestSessionMessages).mockClear()
    const clickedAt = await clickBot(apex)

    // (c) the cached thread paints first, inside the budget, and the
    // background refresh (forceResume: rebind + latest persisted page) lands
    // what arrived while away behind it.
    expect(paintedAt(apex, clickedAt, 5)).not.toBeNull()
    expect(paintedAt(apex, clickedAt, 5)!).toBeLessThanOrEqual(PAINT_BUDGET_MS)
    expect(vi.mocked(getLatestSessionMessages).mock.calls.map(call => call[0])).toEqual([apex.storedId])
    expect(primaryText()).toContain('apex row 7')
    expect(primaryIds()).toHaveLength(apex.rows.length)
    expect(duplicateIds()).toEqual([])
    // The quiet refresh appended the new rows and kept every unchanged row.
    const refresh = trace.published.filter(entry => entry.at > clickedAt + paintedAt(apex, clickedAt, 5)!)
    expect(refresh.every(entry => entry.ids.length >= 6)).toBe(true)
  })

  it('keeps a live turn on A streaming across A → B → A', async () => {
    await mount()
    const atlas = bots.atlas!
    const apex = bots.apex!

    await clickBot(atlas, 9_000)
    atlas.running = true

    const stream = (text: string) =>
      act(() => {
        cache.updateSessionState(
          atlas.runtimeId,
          state => ({
            ...state,
            awaitingResponse: true,
            busy: true,
            messages: [
              ...state.messages.filter(message => message.id !== 'assistant-live'),
              { id: 'assistant-live', role: 'assistant', pending: true, parts: [{ type: 'text', text }] }
            ]
          }),
          atlas.storedId
        )
      })

    stream('A streaming reply')
    await clickBot(apex, 9_000)
    // Deltas keep landing in A's own slice while B is on screen.
    stream('A streaming reply, continued off screen')
    expect(primaryText()).not.toContain('A streaming reply')

    const clickedAt = await clickBot(atlas)

    // (e) the live tail is on screen at once and survives the refresh.
    expect(paintedAt(atlas, clickedAt)!).toBeLessThanOrEqual(PAINT_BUDGET_MS)
    expect(primaryText()).toContain('A streaming reply, continued off screen')
    expect($activeSessionId.get()).toBe(atlas.runtimeId)
    stream('A streaming reply, continued on screen again')
    expect(primaryText()).toContain('A streaming reply, continued on screen again')
    expect(duplicateIds()).toEqual([])
  })

  it('paints the saved tail on the first switch after a relaunch', async () => {
    await mount()
    const apex = bots.apex!

    await clickBot(apex, 9_000)
    // Relaunch: the in-memory session cache and the resolved rows are gone,
    // the persisted transcript tail is not.
    cleanup()
    clearAllSessionStates()
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setUnlistedSessionOwnerRows([])
    harnessRoute.path = '/'
    trace.published = []
    overlayMs = 0
    await mount()

    const clickedAt = await clickBot(apex, 9_000)

    expect(paintedAt(apex, clickedAt)).not.toBeNull()
    expect(paintedAt(apex, clickedAt)!).toBeLessThanOrEqual(PAINT_BUDGET_MS)
    expect(overlayMs).toBe(0)
    expect(duplicateIds()).toEqual([])
  })

  it('opens the registry row when the roster still names an older one', async () => {
    await mount()
    const atlas = bots.atlas!
    const stale = { ...roster(atlas), canonical_session: { id: 'atlas-old-tip', resolved_id: 'atlas-old-tip' } }

    await act(async () => {
      void openRosterBot(stale as RosterRow)
    })
    await advance(9_000)

    // The registry (session.list by title) is the authority: its row wins.
    expect($selectedStoredSessionId.get()).toBe(atlas.storedId)
    expect(primaryText()).toContain('atlas row 5')
    expect(trace.calls.filter(call => call.method === 'session.list')).toHaveLength(1)
  })
})
