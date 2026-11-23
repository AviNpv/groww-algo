import { useCallback, useEffect, useRef, useState } from 'react'
import type { AgentChatEvent, AgentChatSession } from '../types'
import { api, money } from '../api'

type ChatStatus = 'idle' | 'starting' | 'live' | 'done' | 'error'

interface ChatMessage {
  id: number
  kind: 'system' | 'warning' | 'error' | 'decision' | 'agent'
  agentId?: string
  text: string
  meta?: string
  time: string
}

interface AgentLook {
  name: string
  icon: string
  color: string
  persona: string
}

const AGENTS: Record<string, AgentLook> = {
  market_regime: {
    name: 'Market Regime',
    icon: '🌦️',
    color: '#38bdf8',
    persona: 'the desk weather presenter',
  },
  stock_scanner: {
    name: 'Stock Scanner',
    icon: '🔍',
    color: '#34d399',
    persona: 'caffeine-powered screener',
  },
  technical_analyst: {
    name: 'Technical Analyst',
    icon: '📈',
    color: '#fbbf24',
    persona: 'sees candles in his sleep',
  },
  strategy_analyst: {
    name: 'Strategy Analyst',
    icon: '🧠',
    color: '#a78bfa',
    persona: 'the checklist lawyer',
  },
  adversarial_critic: {
    name: 'Adversarial Critic',
    icon: '😈',
    color: '#fb7185',
    persona: 'professional sceptic',
  },
  risk_analyst: {
    name: 'Risk Analyst',
    icon: '🛡️',
    color: '#2dd4bf',
    persona: 'spreadsheet bodyguard',
  },
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function clock(iso?: string): string {
  const date = iso ? new Date(iso) : new Date()
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleTimeString('en-IN', { hour12: false })
}

function pct(value: number | undefined): string {
  if (value === undefined || value === null || Number.isNaN(value)) return '—'
  return `${Math.round(value * 100)}%`
}

function label(value: string | undefined): string {
  if (!value) return '—'
  return value.replaceAll('_', ' ')
}

export default function AgentChat() {
  const [symbol, setSymbol] = useState('UNITDSPR')
  const [picks, setPicks] = useState<string[]>([])
  const [status, setStatus] = useState<ChatStatus>('idle')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [typing, setTyping] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [session, setSession] = useState<AgentChatSession | null>(null)
  const [elapsed, setElapsed] = useState(0)

  const sourceRef = useRef<EventSource | null>(null)
  const statusRef = useRef<ChatStatus>('idle')
  const seqRef = useRef(0)
  const startedRef = useRef<number | null>(null)
  const endRef = useRef<HTMLDivElement | null>(null)

  const setStatusSafe = useCallback((next: ChatStatus) => {
    statusRef.current = next
    setStatus(next)
  }, [])

  const push = useCallback(
    (kind: ChatMessage['kind'], text: string, extra?: Partial<ChatMessage>) => {
      seqRef.current += 1
      const { time, ...rest } = extra ?? {}
      setMessages((prev) => [
        ...prev,
        {
          id: seqRef.current,
          kind,
          text,
          time: clock(time),
          ...rest,
        },
      ])
    },
    [],
  )

  useEffect(() => {
    let cancelled = false
    api
      .scan()
      .then((data) => {
        if (cancelled) return
        const symbols = [
          ...data.signals.filter((row) => row.action === 'BUY').map((row) => row.symbol),
          ...data.results.map((row) => row.symbol),
        ]
        const unique = Array.from(new Set(symbols)).slice(0, 10)
        setPicks(unique)
        if (unique.length > 0) setSymbol((current) => (current === 'UNITDSPR' ? unique[0] : current))
      })
      .catch(() => {
        /* the start button will surface a real backend error */
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (status !== 'live' && status !== 'starting') return
    const timer = window.setInterval(() => {
      if (startedRef.current) {
        setElapsed(Math.floor((Date.now() - startedRef.current) / 1000))
      }
    }, 1000)
    return () => window.clearInterval(timer)
  }, [status])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [messages, typing])

  useEffect(
    () => () => {
      sourceRef.current?.close()
    },
    [],
  )

  const handleEvent = useCallback(
    (event: AgentChatEvent) => {
      const when = event.ts

      switch (event.type) {
        case 'session':
          push('system', event.message ?? 'Live group chat connected.', { time: when })
          break

        case 'status':
          push('system', event.message ?? 'Working…', { time: when })
          break

        case 'warning':
          push('warning', event.message ?? 'Warning', { time: when })
          break

        case 'scan':
          push(
            'system',
            [
              `Feed: ${event.source ?? 'unknown'}${event.delayed ? ' (delayed)' : ''} · ${String(
                event.symbol ?? '',
              )} ₹${money(event.price as number | undefined)} · score ${money(
                event.score as number | undefined,
                1,
              )} · ${event.status ?? '—'}`,
              event.quote_age_sec !== undefined && event.quote_age_sec !== null
                ? `Quote age ${money(event.quote_age_sec, 1)}s`
                : '',
            ]
              .filter(Boolean)
              .join('\n'),
            { time: when },
          )
          break

        case 'snapshot':
          push(
            'system',
            [
              `Market ${event.market_open ? 'open' : 'closed'} · entry window ${
                event.in_entry_window ? 'open' : 'closed'
              } · equity ₹${money(event.equity as number | undefined)} · ${
                event.positions ?? 0
              } open position(s)`,
              `Plan: ${label(event.action as string | undefined)} · qty ${event.qty ?? '—'} · stop ₹${money(
                event.stop as number | undefined,
              )} · target ₹${money(event.target as number | undefined)} · R:R ${money(
                event.risk_reward as number | undefined,
                2,
              )}`,
              event.data_stale
                ? 'The feed marks this quote as stale — the debate still runs, but freshness counts against it.'
                : '',
            ]
              .filter(Boolean)
              .join('\n'),
            { time: when },
          )
          break

        case 'agent_start':
          setTyping(event.agent_id ?? null)
          break

        case 'agent_report': {
          const id = event.agent_id ?? 'unknown'
          setTyping((prev) => (prev === id ? null : prev))
          const metaParts = [
            label(event.recommendation),
            `confidence ${pct(event.confidence)}`,
            `status ${event.status ?? '—'}`,
            `${event.evidence_count ?? 0} evidence`,
          ]
          if (event.critical_objections?.length) {
            metaParts.push(`${event.critical_objections.length} critical objection(s)`)
          }
          push('agent', event.summary ?? 'No summary returned.', {
            agentId: id,
            meta: metaParts.join(' · '),
            time: when,
          })
          if (event.status === 'error') {
            push('error', `${AGENTS[id]?.name ?? id} could not complete model analysis.`, {
              time: when,
            })
          }
          break
        }

        case 'debate':
          push(
            'system',
            [
              `Debate verdict: ${label(event.decision as string | undefined)} · consensus ${pct(
                event.consensus_score as number | undefined,
              )} · ${event.reports ?? 0} report(s) (${event.support ?? 0} support / ${
                event.oppose ?? 0
              } oppose / ${event.abstain ?? 0} abstain)`,
              event.rationale ? String(event.rationale) : '',
            ]
              .filter(Boolean)
              .join('\n'),
            { time: when },
          )
          break

        case 'rules': {
          const failed = (event.checks ?? []).filter((check) => check.ok === false)
          push(
            'system',
            [
              `Deterministic rules: ${event.allowed ? 'ALLOWED' : 'BLOCKED'} · ${
                (event.checks ?? []).length
              } check(s)`,
              failed.length
                ? `Failed: ${failed.map((check) => check.name ?? 'check').join(', ')}`
                : '',
              ...(event.reasons ?? []).slice(0, 4),
            ]
              .filter(Boolean)
              .join('\n'),
            { time: when },
          )
          break
        }

        case 'decision':
          push(
            'decision',
            [
              `DECISION: ${event.action ?? 'REJECT'} · paper-only · ${
                event.model_ok ? 'model reports ok' : 'model did not complete'
              }`,
              String(event.rationale ?? ''),
              ...(event.reject_reasons ?? []).slice(0, 5),
            ]
              .filter(Boolean)
              .join('\n'),
            { time: when },
          )
          break

        case 'error':
          setStatusSafe('error')
          push('error', event.message ?? 'The live chat failed.', { time: when })
          break

        case 'done':
          setTyping(null)
          sourceRef.current?.close()
          sourceRef.current = null
          setStatusSafe(event.ok ? 'done' : 'error')
          push(
            'system',
            event.ok
              ? 'Debate complete. The decision is saved in the intelligence ledger. No order was placed.'
              : 'Debate ended early. See the errors above.',
            { time: when },
          )
          break

        default:
          break
      }
    },
    [push, setStatusSafe],
  )

  const start = useCallback(async () => {
    if (statusRef.current === 'starting' || statusRef.current === 'live') return
    sourceRef.current?.close()
    sourceRef.current = null
    seqRef.current = 0
    setMessages([])
    setTyping(null)
    setError(null)
    setSession(null)
    setElapsed(0)
    startedRef.current = Date.now()
    setStatusSafe('starting')

    try {
      const created = await api.startAgentChat(symbol.trim().toUpperCase())
      setSession(created)
      setStatusSafe('live')
      const source = new EventSource(api.agentChatStreamUrl(created.session_id))
      sourceRef.current = source
      source.onmessage = (message) => {
        try {
          handleEvent(JSON.parse(message.data) as AgentChatEvent)
        } catch {
          /* ignore one malformed frame and keep listening */
        }
      }
      source.onerror = () => {
        if (statusRef.current === 'done' || statusRef.current === 'error') {
          source.close()
          return
        }
        setError('Live stream interrupted; reconnecting…')
      }
    } catch (err) {
      setStatusSafe('error')
      setError(errText(err))
    }
  }, [handleEvent, setStatusSafe, symbol])

  const disconnect = useCallback(() => {
    sourceRef.current?.close()
    sourceRef.current = null
    setTyping(null)
    if (statusRef.current === 'live' || statusRef.current === 'starting') {
      setStatusSafe('done')
      push('system', 'Stream disconnected. The run continues server-side and its decision is saved.')
    }
  }, [push, setStatusSafe])

  const busy = status === 'starting' || status === 'live'
  const statusPill =
    status === 'live' ? 'LIVE' : status === 'starting' ? 'STARTING' : status.toUpperCase()

  return (
    <section className="agentchat-page">
      <div className="page-head">
        <div>
          <h1>Agent Group Chat</h1>
          <p>
            Watch the six advisory agents debate one NSE symbol in real time. Read-only, paper-only,
            and no order capability.
          </p>
        </div>
        <div className="toolbar">
          <span className={`pill ${status === 'live' ? 'live' : status === 'error' ? 'warn' : ''}`}>
            <span className="dot" />
            {statusPill}
          </span>
          {busy && <span className="pill">⏱ {elapsed}s</span>}
          <a className="btn ghost" href="/agent-demo.html" target="_blank" rel="noreferrer">
            Scripted demo ↗
          </a>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}

      <div className="card agentchat-controls">
        <div className="agentchat-row">
          <label className="agentchat-symbol">
            <span>Symbol</span>
            <input
              value={symbol}
              disabled={busy}
              onChange={(event) => setSymbol(event.target.value.toUpperCase())}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void start()
              }}
              placeholder="e.g. UNITDSPR"
              spellCheck={false}
            />
          </label>
          <button className="btn primary" onClick={() => void start()} disabled={busy || !symbol.trim()}>
            {busy ? 'Debate running…' : 'Start live debate'}
          </button>
          <button className="btn ghost" onClick={disconnect} disabled={!busy}>
            Disconnect
          </button>
          {session && <span className="agentchat-session">session {session.session_id}</span>}
        </div>
        {picks.length > 0 && (
          <div className="agentchat-picks">
            <span>From the latest scan:</span>
            {picks.map((pick) => (
              <button
                key={pick}
                type="button"
                className={`agentchat-chip ${pick === symbol ? 'active' : ''}`}
                disabled={busy}
                onClick={() => setSymbol(pick)}
              >
                {pick}
              </button>
            ))}
          </div>
        )}
        <p className="agentchat-hint">
          Best results with the engine stopped, so the six agents get the model quota to themselves.
          The run can take several minutes on a free model tier; messages appear as each agent
          answers.
        </p>
      </div>

      <div className="agentchat-phone">
        <header className="agentchat-top">
          <div className="agentchat-avatars">
            {Object.values(AGENTS).map((agent) => (
              <span key={agent.name} style={{ background: agent.color }} title={agent.name}>
                {agent.icon}
              </span>
            ))}
          </div>
          <div className="agentchat-title">
            <b>NSE AI Trading Desk</b>
            <span>
              6 agents · paper-only ·{' '}
              {status === 'live' ? `live for ${elapsed}s` : status === 'done' ? 'last debate' : 'waiting'}
            </span>
          </div>
          <span className={`agentchat-badge ${status === 'live' ? 'live' : ''}`}>
            {status === 'live' ? '● LIVE' : 'READ-ONLY'}
          </span>
        </header>

        <div className="agentchat-scroll">
          {messages.length === 0 && (
            <div className="agentchat-empty">
              <div className="agentchat-empty-icon">🤖</div>
              <b>No debate yet</b>
              <p>
                Pick a symbol and start the live debate. Every bubble below is a real agent report or
                a real deterministic check — nothing is pre-scripted.
              </p>
            </div>
          )}

          {messages.map((message) =>
            message.kind === 'agent' ? (
              <div className="agentchat-msg" key={message.id}>
                <div
                  className="agentchat-avatar"
                  style={{ background: AGENTS[message.agentId ?? '']?.color ?? '#64748b' }}
                >
                  {AGENTS[message.agentId ?? '']?.icon ?? '🤖'}
                </div>
                <div className="agentchat-bubble">
                  <div className="agentchat-name">
                    {AGENTS[message.agentId ?? '']?.name ?? message.agentId ?? 'Agent'}
                    <span className="agentchat-persona">
                      {AGENTS[message.agentId ?? '']?.persona ?? 'advisory agent'}
                    </span>
                  </div>
                  <div className="agentchat-text">{message.text}</div>
                  <div className="agentchat-meta">
                    <span>{message.meta}</span>
                    <span>{message.time}</span>
                  </div>
                </div>
              </div>
            ) : (
              <div
                key={message.id}
                className={`agentchat-system ${message.kind === 'decision' ? 'decision' : ''} ${
                  message.kind === 'error' ? 'error' : ''
                } ${message.kind === 'warning' ? 'warning' : ''}`}
              >
                <div>{message.text}</div>
                <span>{message.time}</span>
              </div>
            ),
          )}

          {typing && (
            <div className="agentchat-msg">
              <div
                className="agentchat-avatar"
                style={{ background: AGENTS[typing]?.color ?? '#64748b' }}
              >
                {AGENTS[typing]?.icon ?? '🤖'}
              </div>
              <div className="agentchat-typing">
                <span>{AGENTS[typing]?.name ?? typing} is thinking</span>
                <i />
                <i />
                <i />
              </div>
            </div>
          )}
          <div ref={endRef} />
        </div>

        <footer className="agentchat-foot">
          <input disabled placeholder="Agents only — the desk is read-only" />
          <div>
            <b>Paper-only</b> · advisory only · no order endpoint is reachable from this screen
          </div>
        </footer>
      </div>

      <div className="agentchat-summary card">
        <div>
          <div className="intel-label">Pipeline</div>
          <p>
            One-symbol scan → six independent agent reports → deterministic debate → deterministic
            rules → fail-closed decision. The same gate used before a paper entry.
          </p>
        </div>
        <div>
          <div className="intel-label">What you are seeing</div>
          <p>
            Real model output, not a script. If the provider rate-limits a call, that agent appears
            with an error and the debate still fails closed. {session ? `Session ${session.symbol} · ` : ''}
            {session ? `started ${clock(session.started_at)}` : 'Nothing has been purchased or sold.'}
          </p>
        </div>
        <div>
          <div className="intel-label">Equity</div>
          <p>
            Paper account stays untouched by this screen. A decision can be saved to the intelligence
            ledger, but no execution is triggered.
          </p>
        </div>
      </div>
    </section>
  )
}
