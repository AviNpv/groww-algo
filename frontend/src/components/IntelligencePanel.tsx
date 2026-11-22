import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  IntelRecord,
  IntelligenceActionResult,
  IntelligenceDebate,
  IntelligenceDecisionRow,
  IntelligenceExecutionRow,
  IntelligenceHealth,
  IntelligenceList,
  IntelligenceObjection,
  IntelligenceStatus,
  IntelligenceUsageRow,
  IntelligenceVersionRow,
} from '../types'
import { api } from '../api'

const SNAPSHOT_REFRESH_MS = 5000

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function asRecord(value: unknown): IntelRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as IntelRecord)
    : {}
}

function pick(node: IntelRecord, ...keys: string[]): unknown {
  for (const key of keys) {
    if (node[key] !== undefined && node[key] !== null && node[key] !== '') return node[key]
  }
  return undefined
}

/** Look for a non-secret setting anywhere in a small config dict. */
function findDeep(node: unknown, keys: string[], depth = 0): unknown {
  if (!node || typeof node !== 'object' || depth > 3) return undefined
  const record = node as IntelRecord
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key]
  }
  for (const value of Object.values(record)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const found = findDeep(value, keys, depth + 1)
      if (found !== undefined) return found
    }
  }
  return undefined
}

function text(value: unknown, fallback = '—'): string {
  if (value === undefined || value === null || value === '') return fallback
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return fallback
}

function flag(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value
  if (value === 1) return true
  if (value === 0) return false
  if (typeof value === 'string') {
    const low = value.toLowerCase()
    if (['true', '1', 'yes', 'on', 'ok'].includes(low)) return true
    if (['false', '0', 'no', 'off'].includes(low)) return false
  }
  return null
}

function flagText(value: unknown): string {
  const result = flag(value)
  return result === null ? '—' : result ? 'true' : 'false'
}

function when(value: unknown): string {
  const raw = text(value, '')
  if (!raw) return '—'
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return raw
  return date.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
    second: '2-digit', hour12: false,
  })
}

function ratio(value: unknown): string {
  const num = Number(value)
  return Number.isFinite(num) ? `${(num * 100).toFixed(0)}%` : '—'
}

function latency(value: unknown): string {
  const num = Number(value)
  return Number.isFinite(num) ? `${num} ms` : '—'
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => text(item)).filter((item) => item !== '—') : []
}

/** Backend list endpoints use named arrays; keep `items` for older shapes. */
function rows<T>(value: IntelligenceList<T> | undefined, ...keys: string[]): T[] {
  if (!value) return []
  for (const key of ['items', ...keys]) {
    const found = (value as IntelRecord)[key]
    if (Array.isArray(found)) return found as T[]
  }
  return []
}

/** Decisions come back as storage rows; the full model lives in `payload` when present. */
function decisionView(row: IntelligenceDecisionRow): IntelligenceDecisionRow {
  const merged: IntelligenceDecisionRow = { ...row }
  for (const [key, value] of Object.entries(asRecord(row.payload))) {
    if (merged[key] === undefined || merged[key] === null || merged[key] === '') merged[key] = value
  }
  return merged
}

function actionMessage(result: IntelligenceActionResult, label: string): string {
  const message = text(pick(result, 'message', 'detail'), '')
  if (message) return `${label}: ${message}`
  const ok = flag(result.ok)
  return `${label}: ${ok === false ? 'failed' : 'done'}`
}

export default function IntelligencePanel() {
  const [status, setStatus] = useState<IntelligenceStatus | null>(null)
  const [health, setHealth] = useState<IntelligenceHealth | null>(null)
  const [snapshotError, setSnapshotError] = useState<string | null>(null)
  const [decisions, setDecisions] = useState<IntelligenceDecisionRow[]>([])
  const [usage, setUsage] = useState<IntelligenceUsageRow[]>([])
  const [executions, setExecutions] = useState<IntelligenceExecutionRow[]>([])
  const [versions, setVersions] = useState<IntelligenceVersionRow[]>([])
  const [adminToken, setAdminToken] = useState(() => sessionStorage.getItem('intel_admin_token') || '')
  const [debate, setDebate] = useState<IntelligenceDebate | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [symbol, setSymbol] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const cancelled = useRef(false)

  const loadSnapshot = useCallback(async () => {
    const [statusRes, healthRes] = await Promise.allSettled([
      api.intelligenceStatus(),
      api.intelligenceHealth(),
    ])
    if (cancelled.current) return
    setStatus(statusRes.status === 'fulfilled' ? statusRes.value : null)
    const healthValue = healthRes.status === 'fulfilled' ? healthRes.value : null
    setHealth(healthValue ? {
      ...healthValue,
      // Backend nests the adapter report under `health`.
      ...asRecord((healthValue as IntelRecord).health),
    } : null)
    const errors = [statusRes, healthRes]
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map((result) => errText(result.reason))
    setSnapshotError(errors.length ? errors.join(' · ') : null)
  }, [])

  const loadLists = useCallback(async () => {
    const [decisionsRes, usageRes, executionsRes, versionsRes] = await Promise.allSettled([
      api.intelligenceDecisions(20),
      api.intelligenceUsage(50),
      api.intelligenceExecutions(50),
      api.intelligenceVersions(30),
    ])
    if (cancelled.current) return
    const decisionRows = decisionsRes.status === 'fulfilled'
      ? rows(decisionsRes.value, 'decisions') : []
    setDecisions(decisionRows)
    setUsage(usageRes.status === 'fulfilled' ? rows(usageRes.value, 'usage') : [])
    setExecutions(executionsRes.status === 'fulfilled' ? rows(executionsRes.value, 'executions') : [])
    setVersions(versionsRes.status === 'fulfilled' ? rows(versionsRes.value, 'versions') : [])
    setSymbol((previous) => {
      if (previous && decisionRows.some((row) => text(decisionView(row).symbol) === previous)) {
        return previous
      }
      return text(decisionView(decisionRows[0] ?? {}).symbol, '')
    })
    const errors = [decisionsRes, usageRes, executionsRes, versionsRes]
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map((result) => errText(result.reason))
    setListError(errors.length ? errors.join(' · ') : null)
  }, [])

  const loadDebate = useCallback(async (selected: string) => {
    if (!selected) {
      setDebate(null)
      return
    }
    try {
      const result = asRecord(await api.intelligenceDebate(selected))
      const payload = result.found === false
        ? { symbol: selected, reports: [], detail: 'No debate recorded for this symbol yet.' }
        : asRecord(result.debate)
      if (!cancelled.current) setDebate(payload as IntelligenceDebate)
    } catch (err) {
      if (!cancelled.current) setDebate({ symbol: selected, reports: [], detail: errText(err) })
    }
  }, [])

  useEffect(() => {
    cancelled.current = false
    setLoading(true)
    void Promise.all([loadSnapshot(), loadLists()]).finally(() => {
      if (!cancelled.current) setLoading(false)
    })
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadSnapshot()
    }, SNAPSHOT_REFRESH_MS)
    return () => {
      cancelled.current = true
      window.clearInterval(timer)
    }
  }, [loadSnapshot, loadLists])

  useEffect(() => {
    void loadDebate(symbol)
  }, [symbol, loadDebate])

  const refresh = async () => {
    setNotice(null)
    setLoading(true)
    try {
      await Promise.all([loadSnapshot(), loadLists()])
      await loadDebate(symbol)
    } finally {
      if (!cancelled.current) setLoading(false)
    }
  }

  const runAction = async (label: string, action: () => Promise<IntelligenceActionResult>) => {
    setBusy(true)
    setNotice(null)
    try {
      const result = await action()
      if (cancelled.current) return
      setNotice({ ok: flag(result.ok) !== false, text: actionMessage(result, label) })
      await Promise.all([loadSnapshot(), loadLists()])
    } catch (err) {
      if (!cancelled.current) setNotice({ ok: false, text: `${label} failed: ${errText(err)}` })
    } finally {
      if (!cancelled.current) setBusy(false)
    }
  }

  const aiEnabled = useMemo(() => flag(status?.enabled), [status])
  const modeLabel = aiEnabled === null ? 'Unknown' : aiEnabled ? 'AI-assisted (paper)' : 'Normal (current rules)'
  const setMode = (enabled: boolean) => (
    runAction(enabled ? 'AI mode' : 'Normal mode', () => api.setIntelligenceMode(enabled, adminToken))
  )

  const failClosed = useMemo(
    () => findDeep(status?.config, ['fail_closed', 'failClosed', 'fail_closed_on_error']),
    [status],
  )
  const storageCounts = useMemo(
    () => Object.entries(status?.storage ?? {})
      .filter(([key, value]) => typeof value === 'number' && !/path|file|secret|token|key|url|dsn/i.test(key)),
    [status],
  )
  const schedulerRows = useMemo(() => {
    const scheduler = asRecord(status?.scheduler)
    return ['state', 'enabled', 'running', 'mode', 'interval_sec', 'interval_seconds', 'last_run_at', 'next_run_at']
      .filter((key) => scheduler[key] !== undefined)
      .map((key) => [key.replace(/_/g, ' '), text(scheduler[key])] as const)
  }, [status])

  const decisionRows = useMemo(() => decisions.map((row) => decisionView(row)), [decisions])
  const symbols = useMemo(
    () => Array.from(new Set(decisionRows.map((row) => text(row.symbol, '')).filter(Boolean))),
    [decisionRows],
  )
  const reports = debate?.reports ?? []
  const unresolved = debate?.unresolved_critical ?? []

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Intelligence</h1>
          <p>
            Runtime trade decisions, agent debates and strategy evolution. Evidence is kept in a
            separate store; nothing here places an order or changes cash, positions or P&amp;L.
          </p>
        </div>
        <div className="toolbar">
          <span className="pill">
            <span className="dot" />status + model refresh {SNAPSHOT_REFRESH_MS / 1000}s
          </span>
          <button className="btn ghost" disabled={loading} onClick={() => void refresh()}>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {snapshotError && <div className="error-box">Intelligence status unavailable: {snapshotError}</div>}
      {listError && <div className="error-box">Intelligence history unavailable: {listError}</div>}
      {notice && <div className={`banner ${notice.ok ? 'ok' : ''}`}>{notice.text}</div>}

      <div className="intel-status-grid">
        <div className="card intel-card">
          <div className="intel-label">Intelligence layer</div>
          <div className={`intel-value ${flag(status?.enabled) === true ? 'pos' : ''}`}>
            {flag(status?.enabled) === null ? '—' : flag(status?.enabled) ? 'ENABLED' : 'DISABLED'}
          </div>
          <div className="intel-sub">enabled flag from backend</div>
        </div>
        <div className="card intel-card">
          <div className="intel-label">Fail closed</div>
          <div className={`intel-value ${flag(failClosed) === true ? 'pos' : ''}`}>{flagText(failClosed)}</div>
          <div className="intel-sub">block trades when evidence is missing</div>
        </div>
        <div className="card intel-card">
          <div className="intel-label">Model health</div>
          <div className={`intel-value ${flag(health?.ok) === true ? 'pos' : 'neg'}`}>
            {flag(health?.ok) === null ? '—' : flag(health?.ok) ? 'OK' : 'DEGRADED'}
          </div>
          <div className="intel-sub intel-wrap">{text(health?.message, 'no health report')}</div>
        </div>
        <div className="card intel-card">
          <div className="intel-label">Paid model calls</div>
          <div className={`intel-value ${flag(health?.paid_allowed) === true ? 'neg' : 'pos'}`}>
            {flagText(health?.paid_allowed ?? false)}
          </div>
          <div className="intel-sub">paid_allowed from model health</div>
        </div>
        <div className="card intel-card">
          <div className="intel-label">Model</div>
          <div className="intel-value intel-wrap">{text(health?.model)}</div>
          <div className="intel-sub">provider {text(health?.provider)} · {latency(health?.latency_ms)}</div>
        </div>
        <div className="card intel-card">
          <div className="intel-label">Last model check</div>
          <div className="intel-value">{when(health?.ts)}</div>
          <div className="intel-sub">local time (en-IN)</div>
        </div>
      </div>

      <div className="card mode-card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div>
            <h2>Trading mode</h2>
            <span className="sub">switch between the current rules engine and the new AI review layer</span>
          </div>
          <span className={`pill ${aiEnabled === true ? 'live' : 'off'}`}>
            <span className="dot" />{modeLabel}
          </span>
        </div>
        <div className="card-body mode-switch">
          <button
            className={`btn mode-option ${aiEnabled === false ? 'primary' : ''}`}
            disabled={busy || loading || aiEnabled === null}
            onClick={() => void setMode(false)}
          >
            <strong>Normal</strong>
            <span>Current deterministic scanner, scoring, risk gates and paper broker.</span>
          </button>
          <button
            className={`btn mode-option ${aiEnabled === true ? 'primary' : ''}`}
            disabled={busy || loading || aiEnabled === null}
            onClick={() => void setMode(true)}
          >
            <strong>AI-assisted</strong>
            <span>Six-agent review before new paper entries. Model failure blocks entries.</span>
          </button>
        </div>
        <div className="card-body mode-note">
          Paper-only in both modes. Switching does not place orders or change capital, risk limits or
          the square-off rules.{' '}
          {aiEnabled === true && flag(health?.ok) === false && (
            <span style={{ color: 'var(--yellow)' }}>
              Model health is DEGRADED — fix the model key/endpoint before expecting AI entries.
            </span>
          )}
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head"><h2>Evidence store</h2><span className="sub">record counts</span></div>
          <div className="card-body">
            {storageCounts.length === 0 ? (
              <div className="empty-state">No storage counts reported.</div>
            ) : storageCounts.map(([key, value]) => (
              <div className="stat-line" key={key}>
                <span className="l">{key.replace(/_/g, ' ')}</span>
                <span className="v">{text(value)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Scheduler</h2><span className="sub">intelligence worker</span></div>
          <div className="card-body">
            {schedulerRows.length === 0 ? (
              <div className="empty-state">No scheduler state reported.</div>
            ) : schedulerRows.map(([key, value]) => (
              <div className="stat-line" key={key}>
                <span className="l">{key}</span>
                <span className="v intel-wrap">{value}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <h2>Latest trade decisions</h2>
          <span className="sub">{decisionRows.length} shown · click a symbol to inspect its debate</span>
        </div>
        {decisionRows.length === 0 ? (
          <div className="empty-state">No trade decisions recorded yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th><th>Action</th><th>Time</th><th>Rationale</th>
                  <th>Model OK</th><th>Reject reasons</th>
                </tr>
              </thead>
              <tbody>
                {decisionRows.map((row, index) => {
                  const action = text(row.action, '—').toUpperCase()
                  const reasons = list(row.reject_reasons)
                  return (
                    <tr key={text(row.id ?? row.decision_id, `${text(row.symbol)}-${index}`)}>
                      <td className="symbol">
                        <button className="intel-link" onClick={() => setSymbol(text(row.symbol))}>
                          {text(row.symbol)}
                        </button>
                      </td>
                      <td>
                        <span className={`status-pill ${action === 'ALLOW' ? 'qualified' : 'reject'}`}>
                          {action}
                        </span>
                      </td>
                      <td>{when(row.ts)}</td>
                      <td className="intel-wrap">{text(row.rationale)}</td>
                      <td className={flag(row.model_ok) ? 'pos' : 'neg'}>{flagText(row.model_ok)}</td>
                      <td className="intel-wrap">{reasons.length ? reasons.join(' · ') : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div>
            <h2>Agent debate</h2>
            <span className="sub">reports, confidence, uncertainty and objections</span>
          </div>
          <div className="toolbar">
            <select className="input select" value={symbol} onChange={(event) => setSymbol(event.target.value)}>
              <option value="">Select symbol</option>
              {symbols.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <button className="btn small ghost" disabled={!symbol} onClick={() => void loadDebate(symbol)}>
              Reload
            </button>
          </div>
        </div>
        <div className="card-body">
          {!symbol ? (
            <div className="empty-state">Choose a symbol from the latest decisions.</div>
          ) : !debate || (debate.detail && reports.length === 0) ? (
            <div className="empty-state">{text(debate?.detail, 'Loading debate…')}</div>
          ) : (
            <>
              <div className="detail-grid">
                <div className="detail-item"><div className="l">Decision</div><div className="v">{text(debate.decision)}</div></div>
                <div className="detail-item"><div className="l">Consensus</div><div className="v">{ratio(debate.consensus_score)}</div></div>
                <div className="detail-item"><div className="l">Rounds</div><div className="v">{text(debate.rounds)}</div></div>
                <div className="detail-item"><div className="l">Time</div><div className="v">{when(debate.ts)}</div></div>
              </div>
              <div className="intel-wrap" style={{ margin: '10px 0', fontSize: 12.5 }}>
                {text(debate.rationale)}
              </div>
              {unresolved.length > 0 && (
                <div className="banner">
                  <b>{unresolved.length} unresolved critical objection{unresolved.length === 1 ? '' : 's'}</b>
                  {' '}— {unresolved.map((item) => text(item.message)).join(' · ')}
                </div>
              )}
              {reports.length === 0 ? (
                <div className="empty-state">No agent reports stored for this snapshot.</div>
              ) : (
                <div className="intel-reports">
                  {reports.map((report, index) => (
                    <div className="intel-report" key={text(report.agent_id, `agent-${index}`)}>
                      <div className="intel-report-head">
                        <b>{text(report.agent_name ?? report.agent_id, 'agent')}</b>
                        <span className={`status-pill ${text(report.recommendation).toLowerCase() === 'support' ? 'qualified' : text(report.recommendation).toLowerCase() === 'oppose' ? 'reject' : 'watchlist'}`}>
                          {text(report.recommendation, 'abstain').toUpperCase()}
                        </span>
                      </div>
                      <div className="stat-line"><span className="l">Confidence</span><span className="v">{ratio(report.confidence)}</span></div>
                      <div className="stat-line"><span className="l">Uncertainty</span><span className="v">{ratio(report.uncertainty)}</span></div>
                      <div className="stat-line"><span className="l">Status</span><span className="v">{text(report.status)}</span></div>
                      <div className="intel-wrap intel-sub" style={{ marginTop: 7 }}>{text(report.summary)}</div>
                      {list(report.missing_information).length > 0 && (
                        <div className="intel-sub intel-wrap" style={{ marginTop: 5 }}>
                          Missing: {list(report.missing_information).join(' · ')}
                        </div>
                      )}
                      {(report.objections ?? []).length > 0 && (
                        <div className="reason-list" style={{ marginTop: 6 }}>
                          {(report.objections as IntelligenceObjection[]).map((objection, i) => (
                            <div className="intel-objection" key={text(objection.id, `obj-${i}`)}>
                              <span className="chip neutral">{text(objection.severity, 'minor')}</span>
                              <span className="intel-wrap">
                                {text(objection.category, 'objection')}: {text(objection.message)}
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head"><h2>Model usage</h2><span className="sub">{usage.length} calls</span></div>
          {usage.length === 0 ? (
            <div className="empty-state">No model calls recorded yet.</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Time</th><th>Provider</th><th>Model</th><th>OK</th><th>Latency</th><th>Tokens</th><th>Error</th></tr></thead>
                <tbody>
                  {usage.map((row, index) => (
                    <tr key={text(row.id ?? row.usage_id, `usage-${index}`)}>
                      <td>{when(row.ts)}</td>
                      <td>{text(row.provider)}</td>
                      <td>{text(row.model)}</td>
                      <td className={flag(row.ok) ? 'pos' : 'neg'}>{flagText(row.ok)}</td>
                      <td>{latency(row.latency_ms)}</td>
                      <td>{text(row.total_tokens)}</td>
                      <td className="intel-wrap">{text(row.error)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="card">
          <div className="card-head"><h2>Execution log</h2><span className="sub">{executions.length} events</span></div>
          {executions.length === 0 ? (
            <div className="empty-state">No intelligence execution events yet.</div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Time</th><th>Symbol</th><th>Event</th><th>Detail</th><th>Paper</th></tr></thead>
                <tbody>
                  {executions.map((row, index) => (
                    <tr key={text(row.id ?? row.event_id, `exec-${index}`)}>
                      <td>{when(row.ts)}</td>
                      <td className="symbol">{text(row.symbol)}</td>
                      <td>{text(row.event)}</td>
                      <td className="intel-wrap">{text(row.detail)}</td>
                      <td>{flagText(row.paper_only ?? true)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="card-head">
          <div>
            <h2>Strategy versions</h2>
            <span className="sub">promote a version or roll back to the previous one</span>
          </div>
          <div className="toolbar">
            <button
              className="btn small"
              disabled={busy}
              onClick={() => void runAction('Evolution run', () => api.runEvolution(adminToken))}
            >
              {busy ? 'Working…' : 'Run evolution'}
            </button>
            <button
              className="btn small danger"
              disabled={busy}
              onClick={() => void runAction('Rollback', () => api.rollbackStrategy(adminToken))}
            >
              Rollback
            </button>
          </div>
        </div>
        <div className="toolbar" style={{ marginBottom: 10 }}>
          <label className="field" style={{ maxWidth: 380 }}>
            <span>Operator token</span>
            <input
              className="input field-input"
              type="password"
              value={adminToken}
              placeholder="INTELLIGENCE_ADMIN_TOKEN"
              onChange={(event) => {
                const value = event.target.value
                setAdminToken(value)
                sessionStorage.setItem('intel_admin_token', value)
              }}
            />
          </label>
          <span className="tiny muted">
            Required for research, promote and rollback. If an admin token is configured on the
            server, the mode switch uses it too. Kept in this browser session only.
          </span>
        </div>
        {versions.length === 0 ? (
          <div className="empty-state">No strategy versions recorded yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Version</th><th>Time</th><th>Status</th><th>Parent</th><th>Rationale</th><th>Metrics</th><th /></tr></thead>
              <tbody>
                {versions.map((row, index) => {
                  const id = text(row.id ?? row.version_id)
                  const metrics = asRecord(row.metrics)
                  const metricPairs = Object.entries(metrics)
                    .filter(([, value]) => typeof value === 'number' || typeof value === 'boolean')
                    .slice(0, 3)
                  return (
                    <tr key={id || `version-${index}`}>
                      <td className="symbol">{id}</td>
                      <td>{when(row.ts)}</td>
                      <td><span className="chip">{text(row.status)}</span></td>
                      <td>{text(row.parent_version_id)}</td>
                      <td className="intel-wrap">{text(row.rationale)}</td>
                      <td className="intel-wrap">
                        {metricPairs.length
                          ? metricPairs.map(([key, value]) => `${key}: ${text(value)}`).join(' · ')
                          : '—'}
                      </td>
                      <td>
                        <button
                          className="btn small"
                          disabled={busy || !id || text(row.status) === 'active'}
                          onClick={() => void runAction(`Promote ${id}`, () => api.promoteStrategy(id, adminToken))}
                        >
                          Promote
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
