// Everything that talks to the GuardSQL API lives here, so the screens stay simple.

// Set VITE_API_BASE at build time to point at another deployment of the API.
export const API_BASE: string = (
  import.meta.env.VITE_API_BASE ?? 'https://guardsql.onrender.com'
).replace(/\/$/, '')

export interface Confidence {
  level: string
  score: number
  samples_agreeing: number
  samples_requested: number
  repairs: number
  judge: string | null
}

export interface Usage {
  llm_calls: number
  input_tokens: number
  output_tokens: number
  usage_complete: boolean
}

export interface AskResult {
  request_id: string
  sql: string
  columns: string[]
  rows: unknown[][]
  row_count: number
  row_limit_hit: boolean
  query_ms: number
  latency_ms: number
  confidence: Confidence
  usage: Usage
}

export type Schema = Record<string, string[]>

/** Any failure, whether the server answered with an error or never answered at all. */
export class ApiFailure extends Error {
  code: string
  status: number | null
  requestId: string | null
  reason: string | null
  retryAfter: number | null

  constructor(
    code: string,
    message: string,
    options: {
      status?: number | null
      requestId?: string | null
      reason?: string | null
      retryAfter?: number | null
    } = {},
  ) {
    super(message)
    this.name = 'ApiFailure'
    this.code = code
    this.status = options.status ?? null
    this.requestId = options.requestId ?? null
    this.reason = options.reason ?? null
    this.retryAfter = options.retryAfter ?? null
  }
}

export function parseRetryAfter(value: string | null): number | null {
  if (value === null) return null
  const seconds = Number.parseInt(value, 10)
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null
}

async function request<T>(
  path: string,
  options: { method?: string; apiKey?: string; body?: unknown; timeoutMs: number },
): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeoutMs)
  const headers: Record<string, string> = {}
  if (options.apiKey) headers['X-API-Key'] = options.apiKey
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    })
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === 'AbortError'
    throw new ApiFailure(
      timedOut ? 'TIMEOUT' : 'NETWORK',
      timedOut ? 'The request took too long.' : 'Could not reach the API.',
    )
  } finally {
    clearTimeout(timer)
  }

  if (response.ok) return (await response.json()) as T

  const retryAfter = parseRetryAfter(response.headers.get('retry-after'))
  let envelope: { error?: { code?: string; message?: string; request_id?: string; reason?: string } }
  try {
    envelope = (await response.json()) as typeof envelope
  } catch {
    envelope = {}
  }
  const detail = envelope.error
  throw new ApiFailure(detail?.code ?? 'HTTP_ERROR', detail?.message ?? 'The request failed.', {
    status: response.status,
    requestId: detail?.request_id ?? null,
    reason: detail?.reason ?? null,
    retryAfter,
  })
}

export function checkHealth(timeoutMs = 8000): Promise<unknown> {
  return request('/health', { timeoutMs })
}

export function askQuestion(apiKey: string, question: string): Promise<AskResult> {
  return request<AskResult>('/v1/ask', {
    method: 'POST',
    apiKey,
    body: { question },
    timeoutMs: 60_000,
  })
}

export async function fetchSchema(apiKey: string): Promise<Schema> {
  const body = await request<{ tables: Schema }>('/v1/schema', { apiKey, timeoutMs: 20_000 })
  return body.tables
}

export interface FailureText {
  title: string
  detail: string
}

/** Plain-language text for each error the API can return. The server never sends SQL or stack traces. */
export function describeFailure(failure: ApiFailure): FailureText {
  const wait = failure.retryAfter !== null ? ` Try again in about ${failure.retryAfter} s.` : ''
  switch (failure.code) {
    case 'UNAUTHORIZED':
      return {
        title: 'That API key was not accepted',
        detail: 'Check the key you pasted, or ask the owner for a new one.',
      }
    case 'RATE_LIMITED':
      return { title: 'Slow down a little', detail: `Too many requests from this key.${wait}` }
    case 'BUSY':
      return {
        title: 'The server is busy',
        detail: `It is answering other questions right now.${wait}`,
      }
    case 'DAILY_LIMIT':
      return {
        title: 'This key has used its requests for today',
        detail: 'The allowance resets at 00:00 UTC.',
      }
    case 'DAILY_CAPACITY':
      return {
        title: 'The demo is at capacity for today',
        detail: 'The free model quota is used up. It resets at 00:00 UTC.',
      }
    case 'CANNOT_ANSWER':
      return { title: 'This database cannot answer that', detail: failure.message }
    case 'QUERY_BLOCKED':
      return {
        title: 'Blocked by the safety policy',
        detail: failure.reason
          ? `The generated query broke a rule (${failure.reason}), so it was never run.`
          : 'The generated query broke a rule, so it was never run.',
      }
    case 'NETWORK':
      return {
        title: 'Could not reach the API',
        detail:
          'On the free tier the service sleeps when idle and takes about a minute to wake. Wait a moment and try again.',
      }
    case 'TIMEOUT':
      return {
        title: 'The API took too long',
        detail: 'It may be waking up from sleep. Try again in a moment.',
      }
    default:
      return { title: 'Something went wrong', detail: failure.message }
  }
}
