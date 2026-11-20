/**
 * Continuous timing of the user-felt actions (desktop speed, add25 item 6a).
 *
 * Pure bookkeeping, no React: a start/end pair per action+key measured with
 * performance.now(), a bounded per-window buffer (50 samples per action), and
 * ONE `[perf-actions] {json}` line per 60 s window through the preload's
 * `logLine` (desktop.log). That handler writes to disk synchronously per
 * call, which is why samples are batched instead of logged one by one.
 *
 * Every export swallows its own errors: instrumentation must never throw into
 * the code it measures, never await, and cost nothing while idle (the window
 * timer starts on the first activity and stops once a window is empty).
 */

export type TimedAction =
  | 'first_token'
  | 'open_chat'
  | 'open_roster'
  | 'open_sidebar'
  | 'send_ack'
  | 'sidebar_update'
  | 'switch'

export const ACTION_TIMING_WINDOW_MS = 60_000
export const ACTION_TIMING_PENDING_TTL_MS = 60_000
export const ACTION_TIMING_MAX_SAMPLES = 50
export const ACTION_TIMING_LINE_PREFIX = '[perf-actions] '

interface PendingStart {
  action: TimedAction
  startedAt: number
}

let pending = new Map<string, PendingStart>()
let samples: Partial<Record<TimedAction, number[]>> = {}
let drops: Partial<Record<TimedAction, number>> = {}
let marked = new Set<TimedAction>()
let windowStartedAt = 0
let timer: null | ReturnType<typeof setInterval> = null
let lifecycleHooked = false

const pendingKey = (action: TimedAction, key: string) => `${action}\u0000${key}`

/** performance.now(), or 0 where it is missing (never throws). */
export function actionNow(): number {
  try {
    return typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : 0
  } catch {
    return 0
  }
}

function epochNow(): number {
  return Date.now()
}

function noteDrop(action: TimedAction) {
  drops[action] = (drops[action] ?? 0) + 1
  ensureWindow()
}

function measureForDevTools(action: TimedAction, start: number, end: number) {
  try {
    if (typeof performance === 'undefined' || typeof performance.measure !== 'function') {
      return
    }

    const name = `hermes:${action}`
    performance.measure(name, { start, end })
    // DevTools has already recorded the entry; keep the timeline buffer flat.
    performance.clearMeasures?.(name)
  } catch {
    /* older engines without the options form: DevTools mark is optional */
  }
}

function expirePending(now: number) {
  for (const [key, entry] of pending) {
    if (now - entry.startedAt >= ACTION_TIMING_PENDING_TTL_MS) {
      pending.delete(key)
      noteDrop(entry.action)
    }
  }
}

function hookLifecycle() {
  if (lifecycleHooked || typeof window === 'undefined' || typeof window.addEventListener !== 'function') {
    return
  }

  lifecycleHooked = true
  window.addEventListener('pagehide', () => flushActionTiming())

  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') {
        flushActionTiming()
      }
    })
  }
}

function ensureWindow() {
  if (!windowStartedAt) {
    windowStartedAt = epochNow()
  }

  hookLifecycle()

  if (timer === null && typeof setInterval === 'function') {
    timer = setInterval(() => flushActionTiming(), ACTION_TIMING_WINDOW_MS)
    // Node test hosts: never hold the process open.
    ;(timer as unknown as { unref?: () => void }).unref?.()
  }
}

function stopTimer() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

/** Store one sample (integer ms) for the current window. */
export function recordAction(action: TimedAction, ms: number): void {
  try {
    if (!Number.isFinite(ms) || ms < 0) {
      return
    }

    const bucket = samples[action] ?? (samples[action] = [])

    if (bucket.length < ACTION_TIMING_MAX_SAMPLES) {
      bucket.push(Math.round(ms))
    }

    ensureWindow()
  } catch {
    /* never surface */
  }
}

export interface ActionStartOptions {
  /** false: keep an existing pending start (time from the FIRST unserved
   *  trigger). Default true: a new start replaces the old one (a drop). */
  replace?: boolean
}

export function actionStart(action: TimedAction, key = '', options?: ActionStartOptions): void {
  try {
    const now = actionNow()
    const id = pendingKey(action, key)
    const existing = pending.get(id)

    if (existing) {
      if (options?.replace === false && now - existing.startedAt < ACTION_TIMING_PENDING_TTL_MS) {
        return
      }

      noteDrop(action)
    }

    pending.set(id, { action, startedAt: now })
    ensureWindow()
  } catch {
    /* never surface */
  }
}

export function actionPending(action: TimedAction, key = ''): boolean {
  try {
    return pending.has(pendingKey(action, key))
  } catch {
    return false
  }
}

export interface ActionEndOptions {
  /** Only close a start that happened at or before this performance.now()
   *  stamp (e.g. a refresh must have STARTED after the event it serves). */
  startedAtOrBefore?: number
}

/** Close a pending start; returns the measured ms (null when nothing was
 *  pending). Ends without a start are ignored. */
export function actionEnd(action: TimedAction, key = '', options?: ActionEndOptions): null | number {
  try {
    if (pending.size === 0) {
      return null
    }

    const id = pendingKey(action, key)
    const entry = pending.get(id)

    if (!entry) {
      return null
    }

    if (options?.startedAtOrBefore !== undefined && entry.startedAt > options.startedAtOrBefore) {
      return null
    }

    pending.delete(id)
    const now = actionNow()

    if (now - entry.startedAt >= ACTION_TIMING_PENDING_TTL_MS) {
      noteDrop(action)

      return null
    }

    const ms = now - entry.startedAt
    measureForDevTools(action, entry.startedAt, now)
    recordAction(action, ms)

    return ms
  } catch {
    return null
  }
}

/** Forget a pending start without counting it (explicit cancel / failure). */
export function actionAbandon(action: TimedAction, key = ''): void {
  try {
    pending.delete(pendingKey(action, key))
  } catch {
    /* never surface */
  }
}

/** Once per page load: ms from the renderer time origin (performance.now()). */
export function markOnce(action: TimedAction): void {
  try {
    if (marked.has(action)) {
      return
    }

    marked.add(action)
    const now = actionNow()
    measureForDevTools(action, 0, now)
    recordAction(action, now)
  } catch {
    /* never surface */
  }
}

/** Build the window line, or null when the window holds nothing. */
function buildLine(t1: number): null | string {
  const a: Record<string, number[]> = {}

  for (const [action, bucket] of Object.entries(samples)) {
    if (bucket && bucket.length > 0) {
      a[action] = bucket
    }
  }

  const drop: Record<string, number> = {}

  for (const [action, count] of Object.entries(drops)) {
    if (count && count > 0) {
      drop[action] = count
    }
  }

  const hasDrops = Object.keys(drop).length > 0

  if (Object.keys(a).length === 0 && !hasDrops) {
    return null
  }

  const body: Record<string, unknown> = { v: 1, t0: windowStartedAt || t1, t1, a }

  if (hasDrops) {
    body.drop = drop
  }

  return `${ACTION_TIMING_LINE_PREFIX}${JSON.stringify(body)}`
}

/** Emit the current window (if it holds anything) and start a new one. */
export function flushActionTiming(): null | string {
  try {
    expirePending(actionNow())
    const t1 = epochNow()
    const line = buildLine(t1)

    samples = {}
    drops = {}
    windowStartedAt = pending.size > 0 ? t1 : 0

    if (pending.size === 0) {
      stopTimer()
    }

    if (line) {
      try {
        window.hermesDesktop?.logLine?.(line)
      } catch {
        /* no bridge (browser / tests): the line is still returned */
      }
    }

    return line
  } catch {
    return null
  }
}

/** Test hook: flush the window and return the line it produced. */
export function __flushActionTimingForTests(): null | string {
  return flushActionTiming()
}

/** Test hook: drop every pending start, sample, once-mark and the timer. */
export function __resetActionTimingForTests(): void {
  stopTimer()
  pending = new Map()
  samples = {}
  drops = {}
  marked = new Set()
  windowStartedAt = 0
}
