import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  awaitSubmitConfirmation,
  clearSubmitHolds,
  hasSubmitHold,
  holdSubmitsUntilConfirmed
} from './session-open-confirmation'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  promise.catch(() => undefined)

  return { promise, reject, resolve }
}

afterEach(() => clearSubmitHolds())

describe('sends into a session opened before its identity is confirmed', () => {
  it('waits for the verdict and names the confirmed chat', async () => {
    const confirmation = deferred<null | string>()
    holdSubmitsUntilConfirmed('painted', confirmation.promise)

    const verdict = awaitSubmitConfirmation('painted')
    confirmation.resolve('registry-row')

    await expect(verdict).resolves.toEqual({ kind: 'confirmed', storedSessionId: 'registry-row' })
    expect(hasSubmitHold('painted')).toBe(false)
  })

  it('keeps refusing after a failed confirmation until a newer open replaces the hold', async () => {
    const onUnconfirmed = vi.fn()
    const failed = deferred<null | string>()
    holdSubmitsUntilConfirmed('painted', failed.promise, { onUnconfirmed })
    failed.reject(new Error('registry down'))

    await expect(awaitSubmitConfirmation('painted')).resolves.toEqual({ kind: 'unconfirmed', reason: 'failed' })
    await expect(awaitSubmitConfirmation('painted')).resolves.toEqual({ kind: 'unconfirmed', reason: 'failed' })
    expect(onUnconfirmed).toHaveBeenCalledTimes(2)

    holdSubmitsUntilConfirmed('painted', Promise.resolve('painted'))
    await Promise.resolve()
    await expect(awaitSubmitConfirmation('painted')).resolves.toEqual({ kind: 'none' })
  })

  it('follows a newer hold on the same chat instead of refusing (double click)', async () => {
    const first = deferred<null | string>()
    const second = deferred<null | string>()
    holdSubmitsUntilConfirmed('painted', first.promise)

    const verdict = awaitSubmitConfirmation('painted')
    holdSubmitsUntilConfirmed('painted', second.promise)
    first.resolve(null)
    second.resolve('painted')

    await expect(verdict).resolves.toEqual({ kind: 'confirmed', storedSessionId: 'painted' })
  })

  it('refuses without blaming the backend when the open was superseded', async () => {
    const onUnconfirmed = vi.fn()
    const confirmation = deferred<null | string>()
    holdSubmitsUntilConfirmed('painted', confirmation.promise, { onUnconfirmed })

    const verdict = awaitSubmitConfirmation('painted')
    confirmation.resolve(null)

    await expect(verdict).resolves.toEqual({ kind: 'unconfirmed', reason: 'superseded' })
    expect(onUnconfirmed).not.toHaveBeenCalled()
  })
})
