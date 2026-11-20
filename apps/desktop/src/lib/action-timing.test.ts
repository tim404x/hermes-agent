import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  __flushActionTimingForTests,
  __resetActionTimingForTests,
  ACTION_TIMING_LINE_PREFIX,
  ACTION_TIMING_MAX_SAMPLES,
  actionAbandon,
  actionEnd,
  actionPending,
  actionStart,
  markOnce,
  recordAction
} from './action-timing'

type Bridge = { hermesDesktop?: { logLine?: (line: string) => void } }

let now = 1000

function parse(line: null | string) {
  expect(line).toBeTruthy()
  expect(line!.startsWith(ACTION_TIMING_LINE_PREFIX)).toBe(true)

  return JSON.parse(line!.slice(ACTION_TIMING_LINE_PREFIX.length)) as {
    a: Record<string, number[]>
    drop?: Record<string, number>
    t0: number
    t1: number
    v: number
  }
}

describe('action-timing', () => {
  let logLine: ReturnType<typeof vi.fn<(line: string) => void>>

  beforeEach(() => {
    __resetActionTimingForTests()
    now = 1000
    vi.spyOn(performance, 'now').mockImplementation(() => now)
    logLine = vi.fn<(line: string) => void>()
    ;(window as unknown as Bridge).hermesDesktop = { logLine }
  })

  afterEach(() => {
    __resetActionTimingForTests()
    vi.restoreAllMocks()
    vi.useRealTimers()
    delete (window as unknown as Bridge).hermesDesktop
  })

  it('measures start -> end in integer ms and emits one line in the exact format', () => {
    actionStart('switch', 'bot-a')
    now += 812.4
    expect(actionEnd('switch', 'bot-a')).toBeCloseTo(812.4)
    actionStart('switch', 'bot-a')
    now += 640.6
    actionEnd('switch', 'bot-a')

    const line = __flushActionTimingForTests()
    expect(line).toMatch(/^\[perf-actions\] \{"v":1,"t0":\d+,"t1":\d+,"a":\{"switch":\[812,641\]\}\}$/)
    expect(logLine).toHaveBeenCalledTimes(1)
    expect(logLine).toHaveBeenCalledWith(line)

    const body = parse(line)
    expect(body.v).toBe(1)
    expect(body.t1).toBeGreaterThanOrEqual(body.t0)
    expect(body.drop).toBeUndefined()
  })

  it('emits nothing for an empty window', () => {
    expect(__flushActionTimingForTests()).toBeNull()
    expect(logLine).not.toHaveBeenCalled()
  })

  it('ignores ends without a start and keys starts per action+key', () => {
    expect(actionEnd('send_ack', 's1')).toBeNull()
    actionStart('send_ack', 's1')
    expect(actionEnd('send_ack', 's2')).toBeNull()
    expect(actionEnd('first_token', 's1')).toBeNull()
    now += 50
    expect(actionEnd('send_ack', 's1')).toBe(50)
    expect(actionEnd('send_ack', 's1')).toBeNull()
    expect(parse(__flushActionTimingForTests()).a).toEqual({ send_ack: [50] })
  })

  it('a new start for the same action+key replaces the old one and counts a drop', () => {
    actionStart('switch', 'k')
    now += 100
    actionStart('switch', 'k')
    now += 30
    actionEnd('switch', 'k')

    const body = parse(__flushActionTimingForTests())
    expect(body.a).toEqual({ switch: [30] })
    expect(body.drop).toEqual({ switch: 1 })
  })

  it('replace:false keeps the first unserved start without a drop', () => {
    actionStart('sidebar_update', '', { replace: false })
    now += 200
    actionStart('sidebar_update', '', { replace: false })
    now += 100
    actionEnd('sidebar_update')

    const body = parse(__flushActionTimingForTests())
    expect(body.a).toEqual({ sidebar_update: [300] })
    expect(body.drop).toBeUndefined()
  })

  it('startedAtOrBefore only closes a start that the work began after', () => {
    const refreshStartedEarly = now
    now += 10
    actionStart('sidebar_update')
    now += 40
    expect(actionEnd('sidebar_update', '', { startedAtOrBefore: refreshStartedEarly })).toBeNull()
    expect(actionPending('sidebar_update')).toBe(true)
    const refreshStartedLate = now
    now += 25
    expect(actionEnd('sidebar_update', '', { startedAtOrBefore: refreshStartedLate })).toBe(65)
  })

  it('abandon forgets a start without counting it', () => {
    actionStart('first_token', 's')
    actionAbandon('first_token', 's')
    now += 10
    expect(actionEnd('first_token', 's')).toBeNull()
    expect(__flushActionTimingForTests()).toBeNull()
  })

  it('pending starts expire after 60 s as drops', () => {
    actionStart('open_chat', 'x')
    now += 60_001
    expect(actionEnd('open_chat', 'x')).toBeNull()
    expect(parse(__flushActionTimingForTests()).drop).toEqual({ open_chat: 1 })

    actionStart('open_chat', 'y')
    now += 61_000
    const body = parse(__flushActionTimingForTests())
    expect(body.drop).toEqual({ open_chat: 1 })
    expect(body.a).toEqual({})
    expect(actionPending('open_chat', 'y')).toBe(false)
  })

  it('caps each action at 50 samples per window', () => {
    for (let i = 0; i < ACTION_TIMING_MAX_SAMPLES + 20; i++) {
      recordAction('send_ack', i)
    }

    recordAction('first_token', 7)
    const body = parse(__flushActionTimingForTests())
    expect(body.a.send_ack).toHaveLength(ACTION_TIMING_MAX_SAMPLES)
    expect(body.a.first_token).toEqual([7])
    // The window resets after a flush.
    expect(__flushActionTimingForTests()).toBeNull()
  })

  it('markOnce records ms since the time origin exactly once per page load', () => {
    now = 1234.5
    markOnce('open_roster')
    now = 5000
    markOnce('open_roster')
    markOnce('open_sidebar')
    expect(parse(__flushActionTimingForTests()).a).toEqual({ open_roster: [1235], open_sidebar: [5000] })
  })

  it('flushes on a 60 s window timer and on pagehide / hidden', () => {
    vi.useFakeTimers()
    recordAction('switch', 5)
    vi.advanceTimersByTime(60_000)
    expect(logLine).toHaveBeenCalledTimes(1)

    recordAction('switch', 6)
    window.dispatchEvent(new Event('pagehide'))
    expect(logLine).toHaveBeenCalledTimes(2)

    recordAction('switch', 7)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    document.dispatchEvent(new Event('visibilitychange'))
    visibility.mockRestore()
    expect(logLine).toHaveBeenCalledTimes(3)
    expect(parse(logLine.mock.calls[2][0] as string).a).toEqual({ switch: [7] })

    // Idle: the empty window stops the timer, nothing more is written.
    vi.advanceTimersByTime(180_000)
    expect(logLine).toHaveBeenCalledTimes(3)
  })

  it('never throws without a desktop bridge or when the bridge throws', () => {
    delete (window as unknown as Bridge).hermesDesktop
    recordAction('switch', 1)
    expect(() => __flushActionTimingForTests()).not.toThrow()

    ;(window as unknown as Bridge).hermesDesktop = {
      logLine: () => {
        throw new Error('ipc down')
      }
    }
    recordAction('switch', 2)
    expect(parse(__flushActionTimingForTests()).a).toEqual({ switch: [2] })

    vi.spyOn(performance, 'now').mockImplementation(() => {
      throw new Error('no clock')
    })
    expect(() => {
      actionStart('switch', 'z')
      actionEnd('switch', 'z')
      markOnce('open_sidebar')
    }).not.toThrow()
  })
})
