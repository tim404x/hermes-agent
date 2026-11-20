/**
 * Tool plumbing: calls the agent makes to find or poll its own tools, never work the user asked
 * for. "Reading tool details · 3 tools" or a process poll under a reply is the agent narrating its
 * own wiring, so the thread never paints these, live or reloaded, whatever
 * `display.tool_progress` says. A failed plumbing call is not something the user can act on either.
 */

const PLUMBING_TOOL_NAMES: ReadonlySet<string> = new Set([
  'session_search',
  'skill_view',
  'skills_list',
  'tool_describe',
  'tool_search'
])

// process_manage reads a running job's state; kill/write/submit change it and stay visible.
const PROCESS_READ_ACTIONS: ReadonlySet<string> = new Set(['list', 'log', 'poll', 'status', 'wait'])

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

function argsOf(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      return record(JSON.parse(value))
    } catch {
      return {}
    }
  }

  return record(value)
}

function isPlumbingCall(name: unknown, args: unknown): boolean {
  if (typeof name !== 'string') {
    return false
  }

  if (PLUMBING_TOOL_NAMES.has(name)) {
    return true
  }

  const action = argsOf(args).action

  return name === 'process_manage' && typeof action === 'string' && PROCESS_READ_ACTIONS.has(action)
}

/** True when a tool call (or every call a `tool_call` bridge batch runs) is plumbing. */
export function isPlumbingToolCall(toolName: string, args: unknown): boolean {
  if (toolName !== 'tool_call') {
    return isPlumbingCall(toolName, args)
  }

  const input = argsOf(args)
  const calls = Array.isArray(input.calls) ? input.calls : [input]

  return calls.length > 0 && calls.every(call => isPlumbingCall(record(call).name, record(call).arguments))
}
