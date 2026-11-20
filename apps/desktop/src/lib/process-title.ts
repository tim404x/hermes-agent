/**
 * The human title of a background-process completion row ("Asim short finished"), twin of
 * `tools/process_registry_notifications.py` (`process_job_label`, `relabel_legacy_process_title`).
 *
 * The title never carries the shell command, a path or a process id: it names the work by the
 * directory or script the job ran in, or says "Background job". Rows stored by an older backend
 * carry a title built from the command (`Background Process Finished: W=/home/...; for i in ...`);
 * they are re-titled here at render time, so history cleans without rewriting a database. Both
 * twins are pinned by `tests/fixtures/process_label_cases.json`.
 */

const VAR_PATH_RE = /(?:^|[\s;&|(])[A-Z][A-Z0-9_]*=(["']?)((?:\/|~)[^\s;&|"')]*)\1/g
const CD_RE = /(?:^|[\s;&|(])cd\s+(["']?)([^\s;&|"')]+)\1/g
const SCRIPT_RE = /(?:^|\s)(["']?)([^\s;&|"'()]+\.(?:py|sh|js|mjs|ts|rb))\1(?=$|[\s;&|)])/g

const GENERIC_DIRS = new Set([
  '~',
  'hermes',
  'home',
  'work',
  'cache',
  'scratch',
  'tmp',
  'src',
  'app',
  'apps',
  'bin',
  'scripts',
  'script',
  'element',
  'build',
  'dist',
  'out',
  'logs',
  'data',
  'tools'
])

const GENERIC_SCRIPTS = new Set(['run', 'main', 'build', 'index', 'script', 'test', 'start', 'job', 'task'])

const LEGACY_TITLE_RE = /^Background Process (Finished|Failed|Terminated|Lost)(?: \(exit (-?\d+|\?)\))?(?::\s*([\s\S]*))?$/
const LEGACY_BATCH_RE = /^(\d+) Background Processes Finished$/

function humanize(segment: string): null | string {
  const words = segment
    .replace(/[-_]\d{3,8}$/, '')
    .replace(/[-_.]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  if (!/\p{L}/u.test(words)) {
    return null
  }

  return words.charAt(0).toUpperCase() + words.slice(1)
}

function pathLabel(path: string): null | string {
  const segments = path.split('/').filter(Boolean)

  for (let index = segments.length - 1; index >= 0; index--) {
    const segment = segments[index].replace(/^\.+/, '')

    if (!segment || GENERIC_DIRS.has(segment.toLowerCase())) {
      continue
    }

    return humanize(segment)
  }

  return null
}

function scriptLabel(path: string): null | string {
  const base = (path.split('/').pop() ?? '').replace(/\.[^.]+$/, '')

  return !base || GENERIC_SCRIPTS.has(base.toLowerCase()) ? null : humanize(base)
}

/** The work a background command did, named for a person ("Asim short"), or null. */
export function processJobLabel(command: unknown): null | string {
  let text = typeof command === 'string' ? command : ''
  // A title stored by an older backend cut the command at 77 chars plus "...": a path running into
  // that cut may itself be cut, so it does not name anything.
  const truncated = text.endsWith('...')

  if (truncated) {
    text = text.slice(0, -3)
  }

  const kinds: [RegExp, (value: string) => null | string][] = [
    [VAR_PATH_RE, pathLabel],
    [CD_RE, pathLabel],
    [SCRIPT_RE, scriptLabel]
  ]

  for (const [pattern, toLabel] of kinds) {
    for (const match of text.matchAll(pattern)) {
      if (truncated && (match.index ?? 0) + match[0].length >= text.length) {
        continue
      }

      const label = toLabel(match[2])

      if (label) {
        return label
      }
    }
  }

  return null
}

const LEGACY_OUTCOMES: Record<string, string> = {
  Finished: 'finished',
  Lost: 'lost',
  Terminated: 'stopped'
}

/** A process row title as a person reads it; a title from an older backend is rebuilt from the
 *  command it quoted, anything else is returned unchanged. */
export function relabelProcessTitle(title: string): string {
  const batch = LEGACY_BATCH_RE.exec(title.trim())

  if (batch) {
    return `${batch[1]} background jobs finished`
  }

  const legacy = LEGACY_TITLE_RE.exec(title.trim())

  if (!legacy) {
    return title
  }

  const [, verb, exit, command] = legacy
  const outcome = LEGACY_OUTCOMES[verb] ?? (exit && exit !== '?' ? `failed, exit ${exit}` : 'failed')

  return `${processJobLabel(command ?? '') ?? 'Background job'} ${outcome}`
}

// `[IMPORTANT: Background process proc_x completed normally (exit code 0).\nCommand: ...` as an
// older backend persisted it with no display type.
const NOTIFICATION_HEAD_RE = /^Background process \S+ (completed normally|was killed|[^(\n]*?)\s*\(exit code (-?\d+)\)/
const NOTIFICATION_COMMAND_RE = /^Command: (.*)$/m

/** The title for an untyped `[IMPORTANT: Background process ...]` notification body. */
export function processNotificationTitle(headline: string, detail: string): string {
  const head = NOTIFICATION_HEAD_RE.exec(headline)
  const command = NOTIFICATION_COMMAND_RE.exec(detail)?.[1] ?? NOTIFICATION_COMMAND_RE.exec(headline)?.[1] ?? ''
  const label = processJobLabel(command) ?? 'Background job'

  if (/killed|terminated/i.test(headline)) {
    return `${label} stopped`
  }

  const exit = head?.[2]

  return exit === undefined || exit === '0' ? `${label} finished` : `${label} failed, exit ${exit}`
}
