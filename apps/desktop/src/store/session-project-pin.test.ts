import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { makeCwdSession } from '@/test/session-info'
import type { SessionInfo } from '@/types/hermes'

const patch = vi.fn<(id: string, pinned: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(() =>
  Promise.resolve({ ok: true })
)

const refreshProjectTree = vi.fn(() => Promise.resolve())

vi.mock('@/hermes', () => ({
  setSessionProjectPinnedRemote: (id: string, pinned: boolean, profile?: null | string) => patch(id, pinned, profile)
}))
vi.mock('@/store/projects', () => ({ refreshProjectTree: () => refreshProjectTree() }))
vi.mock('@/store/session', () => {
  const $sessions = atom<SessionInfo[]>([])

  return {
    $sessions,
    sessionMatchesStoredId: (
      session: Pick<SessionInfo, '_lineage_ids' | '_lineage_root_id' | 'id'>,
      storedId: string
    ) =>
      session.id === storedId ||
      session._lineage_root_id === storedId ||
      Boolean(session._lineage_ids?.includes(storedId))
  }
})

import { $sessions } from '@/store/session'

import { isProjectPinned, toggleProjectPin } from './session-project-pin'

const rowStamp = (id: string) => $sessions.get().find(r => r.id === id)?.project_pinned_at

describe('toggleProjectPin', () => {
  beforeEach(() => {
    patch.mockClear()
    refreshProjectTree.mockClear()
    $sessions.set([
      makeCwdSession('/p', { id: 'a', profile: 'k9' }),
      makeCwdSession('/p', { id: 'b', project_pinned_at: 100 }),
      makeCwdSession('/p', { id: 'c' })
    ])
  })

  afterEach(() => {
    $sessions.set([])
  })

  it('paints the pin optimistically before the PATCH resolves, then refreshes the tree', async () => {
    let release!: () => void
    patch.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          release = () => resolve({ ok: true })
        })
    )

    const pending = toggleProjectPin($sessions.get()[0])

    // Same frame: the row already carries a stamp so the preview re-sorts.
    expect(typeof rowStamp('a')).toBe('number')
    expect(refreshProjectTree).not.toHaveBeenCalled()

    release()
    await pending

    expect(patch).toHaveBeenCalledWith('a', true, 'k9')
    expect(refreshProjectTree).toHaveBeenCalledTimes(1)
  })

  it('stamps in SECONDS so an optimistic pin compares on the backend scale', async () => {
    await toggleProjectPin($sessions.get()[0])

    const stamp = rowStamp('a') as number
    const nowSeconds = Date.now() / 1000

    expect(stamp).toBeGreaterThan(nowSeconds - 5)
    expect(stamp).toBeLessThanOrEqual(nowSeconds + 1)
  })

  it('unpins a pinned row: clears the stamp and PATCHes false', async () => {
    await toggleProjectPin($sessions.get()[1])

    expect(rowStamp('b')).toBeNull()
    expect(patch).toHaveBeenCalledWith('b', false, null)
  })

  it('rolls the row back and rethrows when the PATCH fails', async () => {
    patch.mockImplementationOnce(() => Promise.reject(new Error('offline')))

    await expect(toggleProjectPin($sessions.get()[0])).rejects.toThrow('offline')

    expect(rowStamp('a')).toBeNull()
    expect(refreshProjectTree).not.toHaveBeenCalled()
  })

  it('leaves unrelated rows untouched (reference identity preserved)', async () => {
    const before = $sessions.get()

    await toggleProjectPin(before[0])

    const after = $sessions.get()
    expect(after[1]).toBe(before[1])
    expect(after[2]).toBe(before[2])
  })

  it('paints every row of a compression lineage, matching the backend write', async () => {
    const root = makeCwdSession('/p', { id: 'root' })
    const tip = makeCwdSession('/p', { id: 'tip', _lineage_root_id: 'root' })
    $sessions.set([root, tip])

    await toggleProjectPin(tip)

    // The store keys the paint on the id it PATCHed (the tip); the root row
    // matches through its lineage and is painted too.
    expect(typeof rowStamp('tip')).toBe('number')
    expect(patch).toHaveBeenCalledWith('tip', true, null)
  })
})

describe('isProjectPinned', () => {
  it('is true only for a present stamp', () => {
    expect(isProjectPinned({ project_pinned_at: 1 })).toBe(true)
    expect(isProjectPinned({ project_pinned_at: 0 })).toBe(true)
    expect(isProjectPinned({ project_pinned_at: null })).toBe(false)
    expect(isProjectPinned({})).toBe(false)
  })
})
