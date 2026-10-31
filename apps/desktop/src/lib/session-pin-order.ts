import type { SessionInfo } from '@/types/hermes'

/** New server-originated pins enter ahead of the user's existing pin order.
 * Creation time also keeps several briefs discovered after an offline period
 * newest-first. Never apply this comparator to already-adopted manual pins. */
export function compareIncomingPins(a: SessionInfo, b: SessionInfo): number {
  return b.started_at - a.started_at || a.id.localeCompare(b.id)
}
