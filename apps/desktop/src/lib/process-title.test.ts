import { describe, expect, it } from 'vitest'

import cases from '../../../../tests/fixtures/process_label_cases.json'

import { processJobLabel, processNotificationTitle, relabelProcessTitle } from './process-title'

// Shared with tools/process_registry_notifications.py: both twins must name a job the same way.
describe('process titles (shared cases)', () => {
  it.each(cases.labels)('labels $command', ({ command, label }) => {
    expect(processJobLabel(command)).toBe(label)
  })

  it.each(cases.legacy)('re-titles "$display_text"', ({ display_text, title }) => {
    expect(relabelProcessTitle(display_text)).toBe(title)
  })
})

describe('untyped process notifications', () => {
  it('names a finished job by its work dir and never quotes the command', () => {
    expect(
      processNotificationTitle(
        'Background process proc_151f1eb6add3 completed normally (exit code 0).',
        'Command: W=/home/hermes/.hermes/work/asim-short-1119; for i in $(seq 1 90); do sleep 1; done\nOutput:\nok'
      )
    ).toBe('Asim short finished')
  })

  it('says which job failed and how', () => {
    expect(
      processNotificationTitle(
        'Background process proc_x exited with an error (exit code 2).',
        'Command: cd ~/.hermes/work/deal-heatmap/element; python3 build.py'
      )
    ).toBe('Deal heatmap failed, exit 2')
  })
})
