import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('read request deadline', () => {
  it.each(['fetch', 'body'] as const)('ends a hanging %s read after twenty seconds', async (phase) => {
    vi.useFakeTimers()
    const pending = new Promise<Response>(() => undefined)
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(() => phase === 'fetch'
      ? pending
      : new Promise<Response>((resolve) => setTimeout(() => resolve({
        ok: true, json: () => new Promise(() => undefined),
      } as Response), 15_000)))
    const outcome = api.getAuthStatus().catch((error: unknown) => error)
    const marker = Symbol('pending')
    await vi.advanceTimersByTimeAsync(19_999)
    expect(await Promise.race([outcome, Promise.resolve(marker)])).toBe(marker)
    await vi.advanceTimersByTimeAsync(1)
    const result = await Promise.race([outcome, Promise.resolve(marker)])
    expect(result).toMatchObject({ status: 408, code: 'READ_TIMEOUT', message: expect.stringContaining('重试') })
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('returns a fast response and clears its timer without aborting', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ authenticated: true })))
    expect(await api.getAuthStatus()).toEqual({ authenticated: true })
    expect(vi.getTimerCount()).toBe(0)
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false)
  })

  it('preserves HTTP problem details and clears the deadline', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: '会话已过期', code: 'SESSION_EXPIRED' }), { status: 401 }))
    await expect(api.getSession()).rejects.toMatchObject({ status: 401, code: 'SESSION_EXPIRED', message: '会话已过期' })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('does not time out or replay a POST while awaiting its response', async () => {
    vi.useFakeTimers()
    let complete!: (response: Response) => void
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(() => new Promise<Response>((resolve) => { complete = resolve }))
    let settled = false
    const result = api.recoverSession('synthetic-test-code').then((value) => { settled = true; return value })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(settled).toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', body: JSON.stringify({ recovery_code: 'synthetic-test-code' }) })
    expect(fetchMock.mock.calls[0][1]?.signal).toBeUndefined()
    expect(vi.getTimerCount()).toBe(0)
    complete(new Response(JSON.stringify({ authenticated: true })))
    expect(await result).toEqual({ authenticated: true })
  })
})
