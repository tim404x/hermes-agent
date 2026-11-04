// Stored ids created in THIS renderer run. A brand-new session lives only in the
// gateway's in-memory map until its first turn persists a state.db row — so if a
// respawning/flapping backend drops it, both resume RPC and the REST transcript
// 404 even though the user just made it. We must NOT treat that as "gone" (which
// yanks them to a fresh draft — the "new sessions clear themselves" bug); the
// bounded retry rebinds it when the backend returns. Boot-into-a-stale-last-id
// (NOT in this set) still legitimately drops to a draft.
//
// Split into its own module (rather than living inside use-session-actions/index.ts)
// so a lean transcript-reading hook can check it without pulling in the whole
// session-actions import graph.
const createdThisRun = new Set<string>()

export function markSessionCreatedThisRun(storedSessionId: string): void {
  createdThisRun.add(storedSessionId)
}

/** True while `storedSessionId` was minted by this window this run and may still have
 *  no state.db row (drafts persist lazily on the first prompt, #123622) — callers about
 *  to read its transcript before anything was ever sent should skip the request instead
 *  of 404ing against a row that cannot exist yet. */
export function sessionCreatedThisRun(storedSessionId: string): boolean {
  return createdThisRun.has(storedSessionId)
}

/** A warm cache of a chat this window created, still under its first stored id: every row
 *  came from this window's own stream, never from a runtime's (possibly compressed) context
 *  replay, so it is the chat's display transcript and paints like a persisted-display one
 *  while the REST refresh catches up. A compression rotation mints a new stored id, which
 *  is not in this set, so a rotated chat stays on the loader as before (#73646). */
export function isOwnStreamedTranscript(
  storedSessionId: string,
  state: { messages: readonly unknown[]; storedSessionId?: null | string }
): boolean {
  return (
    sessionCreatedThisRun(storedSessionId) && state.storedSessionId === storedSessionId && state.messages.length > 0
  )
}
