import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiFailure, askQuestion, describeFailure, parseRetryAfter } from './api'

afterEach(() => vi.unstubAllGlobals())

function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

describe('parseRetryAfter', () => {
  it('reads whole seconds and ignores junk', () => {
    expect(parseRetryAfter('12')).toBe(12)
    expect(parseRetryAfter(null)).toBeNull()
    expect(parseRetryAfter('soon')).toBeNull()
    expect(parseRetryAfter('0')).toBeNull()
  })
})

describe('askQuestion', () => {
  it('sends the key in a header and the question as JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(respond(200, { sql: 'SELECT 1' }))
    vi.stubGlobal('fetch', fetchMock)
    await askQuestion('gsql_key', 'how many?')
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url.endsWith('/v1/ask')).toBe(true)
    expect((init.headers as Record<string, string>)['X-API-Key']).toBe('gsql_key')
    expect(init.body).toBe(JSON.stringify({ question: 'how many?' }))
  })

  it('turns the error envelope into an ApiFailure with Retry-After', async () => {
    const envelope = { error: { code: 'RATE_LIMITED', message: 'slow', request_id: 'r1' } }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond(429, envelope, { 'Retry-After': '7' })))
    const failure = await askQuestion('k', 'q').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiFailure)
    expect(failure).toMatchObject({ code: 'RATE_LIMITED', status: 429, requestId: 'r1', retryAfter: 7 })
  })

  it('reports an unreachable server as NETWORK', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await expect(askQuestion('k', 'q')).rejects.toMatchObject({ code: 'NETWORK' })
  })

  it('survives a non-JSON error body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('bad gateway', { status: 502 })))
    await expect(askQuestion('k', 'q')).rejects.toMatchObject({ code: 'HTTP_ERROR', status: 502 })
  })
})

describe('describeFailure', () => {
  it('mentions the wait for rate limits and tomorrow for daily caps', () => {
    const limited = new ApiFailure('RATE_LIMITED', 'x', { retryAfter: 5 })
    expect(describeFailure(limited).detail).toContain('5 s')
    const daily = describeFailure(new ApiFailure('DAILY_LIMIT', 'x'))
    expect(daily.detail).toContain('00:00 UTC')
  })

  it('shows the blocked rule but never SQL', () => {
    const blocked = new ApiFailure('QUERY_BLOCKED', 'blocked', { reason: 'RESTRICTED_COLUMN' })
    expect(describeFailure(blocked).detail).toContain('RESTRICTED_COLUMN')
  })

  it('falls back to the server message for unknown codes', () => {
    expect(describeFailure(new ApiFailure('WEIRD', 'odd thing')).detail).toBe('odd thing')
  })
})
