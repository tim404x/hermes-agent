/**
 * "Pin in project" — the second pin.
 *
 * The global pin (`store/layout.ts` + `session-pin-sync.ts`) is a keep flag
 * that moves a chat OUT of its project into the sidebar's Pinned section. This
 * one keeps the chat IN its project and lists it first there, newest pin on
 * top, in both the overview preview and the entered project.
 *
 * There is deliberately no local copy of project pins. The durable record is
 * `sessions.project_pinned_at` (the pin TIME), the backend tree builder
 * orders on it, and every surface reads that order — so there is nothing to
 * reconcile, fence, or migrate. This module is the write path plus the
 * optimistic paint: flip the loaded row's stamp so `compareProjectRank`
 * re-sorts the preview on this frame, PATCH, roll back on failure, and refresh
 * the tree so the entered project's lanes (backend-ordered) catch up.
 */

import { setSessionProjectPinnedRemote } from '@/hermes'
import { refreshProjectTree } from '@/store/projects'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

/** True when the row is pinned inside its project. */
export const isProjectPinned = (session: Pick<SessionInfo, 'project_pinned_at'>): boolean =>
  session.project_pinned_at != null

function paintProjectPin(sessionId: string, stamp: null | number): void {
  const rows = $sessions.get()
  let changed = false

  const next = rows.map(row => {
    // Match on every identity the row can surface under, so a pin on a
    // compressed lineage's tip paints the root's row too (the backend stamps
    // the whole lineage in one write).
    if (!sessionMatchesStoredId(row, sessionId) || row.project_pinned_at === stamp) {
      return row
    }

    changed = true

    return { ...row, project_pinned_at: stamp }
  })

  // Preserve reference identity on a no-op so nothing downstream re-renders.
  if (changed) {
    $sessions.set(next)
  }
}

/**
 * Toggle "pin in project" on a row. Optimistic; rolls back and rethrows on a
 * failed PATCH so the caller can toast. Resolves after the tree refresh has
 * been kicked off (not awaited — the overlay already shows the new order).
 */
export async function toggleProjectPin(session: SessionInfo): Promise<void> {
  const previous = session.project_pinned_at ?? null
  const pinning = previous == null
  // Seconds, to match the backend's `time.time()` stamp so an optimistic pin
  // and the server's real one compare on the same scale until the row reloads.
  const optimistic = pinning ? Date.now() / 1000 : null

  paintProjectPin(session.id, optimistic)

  try {
    await setSessionProjectPinnedRemote(session.id, pinning, session.profile ?? null)
  } catch (err) {
    paintProjectPin(session.id, previous)
    throw err
  }

  void refreshProjectTree()
}
