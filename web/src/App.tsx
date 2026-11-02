import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  API_BASE,
  ApiFailure,
  askQuestion,
  checkHealth,
  describeFailure,
  fetchSchema,
} from './api'
import type { AskResult, Schema } from './api'
import { formatCell, formatMs, isNumeric } from './format'

type ApiState = 'checking' | 'waking' | 'online' | 'offline'

const KEY_STORAGE = 'guardsql.apiKey'
const MAX_QUESTION = 2000

const EXAMPLES: { label: string; question: string; tone?: 'safe' | 'attack' }[] = [
  { label: 'Count customers', question: 'How many customers are there?' },
  {
    label: 'Revenue by country',
    question: 'What is the total order amount per customer country? Show country and total.',
  },
  {
    label: 'Units by category',
    question: 'What is the total quantity sold per product category? Show category and quantity.',
  },
  {
    label: 'Most stock',
    question: 'What is the name of the product with the most units in stock?',
  },
  {
    label: 'Ask for salaries',
    question: 'What is the average salary of employees?',
    tone: 'attack',
  },
  { label: 'Try a delete', question: 'Delete all cancelled orders.', tone: 'attack' },
]

function readStoredKey(): string {
  try {
    return sessionStorage.getItem(KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}

function storeKey(key: string | null): void {
  try {
    if (key === null) sessionStorage.removeItem(KEY_STORAGE)
    else sessionStorage.setItem(KEY_STORAGE, key)
  } catch {
    // Storage can be blocked (private windows); the page works without it.
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function useApiState(): ApiState {
  const [state, setState] = useState<ApiState>('checking')
  useEffect(() => {
    let cancelled = false
    async function wake(): Promise<void> {
      // A sleeping free-tier service needs about a minute, so retry for about 90 s.
      for (let attempt = 0; attempt < 18 && !cancelled; attempt++) {
        try {
          await checkHealth()
          if (!cancelled) setState('online')
          return
        } catch {
          if (!cancelled) setState('waking')
          await sleep(5000)
        }
      }
      if (!cancelled) setState('offline')
    }
    void wake()
    return () => {
      cancelled = true
    }
  }, [])
  return state
}

const STATE_TEXT: Record<ApiState, string> = {
  checking: 'Checking the API…',
  waking: 'Waking the API (the free tier sleeps when idle, about a minute)…',
  online: 'API online',
  offline: 'API not reachable',
}

function ResultView({ result }: { result: AskResult }) {
  const [copied, setCopied] = useState(false)
  const { confidence: c, usage: u } = result
  const level = c.level.toLowerCase()

  async function copySql(): Promise<void> {
    try {
      await navigator.clipboard.writeText(result.sql)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard access can be denied; the SQL is still selectable.
    }
  }

  return (
    <section className="card" aria-label="Answer">
      <div className="row between">
        <h2>Answer</h2>
        <span className={`badge badge-${level}`} title={`score ${c.score.toFixed(2)}`}>
          {c.level} confidence
        </span>
      </div>

      <div className="row between">
        <h3>SQL that ran</h3>
        <button type="button" className="ghost" onClick={() => void copySql()}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="sql">{result.sql}</pre>

      <h3>
        Result <span className="muted">({result.row_count} {result.row_count === 1 ? 'row' : 'rows'})</span>
      </h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {result.columns.map((name) => (
                <th key={name}>{name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, index) => (
              <tr key={index}>
                {row.map((cell, position) => (
                  <td
                    key={position}
                    className={cell === null ? 'null' : isNumeric(cell) ? 'num' : undefined}
                  >
                    {formatCell(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.row_limit_hit && (
        <p className="notice">
          The result was cut off at the service's row limit, so more rows may exist.
        </p>
      )}

      <dl className="meta">
        <div>
          <dt>Total time</dt>
          <dd>{formatMs(result.latency_ms)}</dd>
        </div>
        <div>
          <dt>Database time</dt>
          <dd>{formatMs(result.query_ms)}</dd>
        </div>
        <div>
          <dt>Model calls</dt>
          <dd>{u.llm_calls === 0 ? '0 (cached)' : u.llm_calls}</dd>
        </div>
        <div>
          <dt>Tokens in / out</dt>
          <dd>
            {u.input_tokens} / {u.output_tokens}
          </dd>
        </div>
        <div>
          <dt>Agreement</dt>
          <dd>
            {c.samples_agreeing} of {c.samples_requested}
          </dd>
        </div>
        <div>
          <dt>Repairs</dt>
          <dd>{c.repairs}</dd>
        </div>
      </dl>
      <p className="muted small">Request {result.request_id}</p>
    </section>
  )
}

export default function App() {
  const apiState = useApiState()
  const [apiKey, setApiKey] = useState(readStoredKey)
  const [remember, setRemember] = useState(() => readStoredKey() !== '')
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<AskResult | null>(null)
  const [failure, setFailure] = useState<ApiFailure | null>(null)
  const [schema, setSchema] = useState<Schema | null>(null)
  const [schemaError, setSchemaError] = useState<string | null>(null)

  function changeKey(value: string): void {
    setApiKey(value)
    setSchema(null)
    if (remember) storeKey(value.trim() === '' ? null : value)
  }

  function changeRemember(checked: boolean): void {
    setRemember(checked)
    storeKey(checked && apiKey.trim() !== '' ? apiKey : null)
  }

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault()
    const text = question.trim()
    if (text === '' || loading) return
    setLoading(true)
    setFailure(null)
    try {
      setResult(await askQuestion(apiKey.trim(), text))
    } catch (error) {
      setResult(null)
      setFailure(
        error instanceof ApiFailure ? error : new ApiFailure('UNKNOWN', 'Unexpected error.'),
      )
    } finally {
      setLoading(false)
    }
  }

  async function loadSchema(): Promise<void> {
    setSchemaError(null)
    try {
      setSchema(await fetchSchema(apiKey.trim()))
    } catch (error) {
      setSchemaError(error instanceof ApiFailure ? describeFailure(error).title : 'Failed')
    }
  }

  const hasKey = apiKey.trim() !== ''
  const problem = failure ? describeFailure(failure) : null

  return (
    <main>
      <header>
        <h1>GuardSQL</h1>
        <p className="lead">
          Ask a database questions in English. The language model writes the SQL, but it is treated
          as untrusted: every query is checked by deterministic rules and run with a read-only
          database role.
        </p>
        <p className={`status status-${apiState}`} role="status">
          <span className="dot" aria-hidden="true" /> {STATE_TEXT[apiState]}
        </p>
      </header>

      <section className="card" aria-label="Access">
        <label htmlFor="key">API key</label>
        <input
          id="key"
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder="gsql_…"
          value={apiKey}
          onChange={(event) => changeKey(event.target.value)}
        />
        <label className="check">
          <input
            type="checkbox"
            checked={remember}
            onChange={(event) => changeRemember(event.target.checked)}
          />
          Remember in this tab only
        </label>
        <p className="muted small">
          Keys are issued individually by the owner. Your key is sent only to {API_BASE} and is not
          stored on any server by this page.
        </p>
        <div className="row">
          <button type="button" className="ghost" disabled={!hasKey} onClick={() => void loadSchema()}>
            Show what I can ask about
          </button>
          {schemaError && <span className="error-text">{schemaError}</span>}
        </div>
        {schema && (
          <ul className="schema">
            {Object.entries(schema).map(([table, columns]) => (
              <li key={table}>
                <strong>{table}</strong>
                <span className="muted"> {columns.join(', ')}</span>
              </li>
            ))}
          </ul>
        )}
        {schema && (
          <p className="muted small">Sensitive columns (emails, phones, birth dates, salaries) are not listed and cannot be queried.</p>
        )}
      </section>

      <form className="card" onSubmit={(event) => void submit(event)} aria-label="Ask a question">
        <label htmlFor="question">Your question</label>
        <textarea
          id="question"
          rows={3}
          maxLength={MAX_QUESTION}
          placeholder="How many customers are there?"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <div className="chips" aria-label="Example questions">
          {EXAMPLES.map((example) => (
            <button
              key={example.label}
              type="button"
              className={`chip ${example.tone === 'attack' ? 'chip-attack' : ''}`}
              onClick={() => setQuestion(example.question)}
            >
              {example.label}
            </button>
          ))}
        </div>
        <div className="row">
          <button type="submit" className="primary" disabled={loading || !hasKey || question.trim() === ''}>
            {loading ? 'Thinking…' : 'Ask'}
          </button>
          {!hasKey && <span className="muted small">Enter an API key first.</span>}
        </div>
      </form>

      {problem && (
        <section className="card problem" role="alert">
          <h2>{problem.title}</h2>
          <p>{problem.detail}</p>
          {failure?.requestId && <p className="muted small">Request {failure.requestId}</p>}
        </section>
      )}

      {result && <ResultView result={result} />}

      <footer className="muted small">
        Synthetic demo data. Free tiers (Render, Neon, Gemini) so the first request after a quiet
        spell can be slow. Source and design notes on GitHub.
      </footer>
    </main>
  )
}
