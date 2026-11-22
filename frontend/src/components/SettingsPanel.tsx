import { useCallback, useEffect, useRef, useState } from 'react'
import type { CredentialsStatus, ResetChallenge, Status } from '../types'
import { api } from '../api'
import StrategyFlowChart from './StrategyFlowChart'

type ConfigMap = Record<string, unknown>

interface SettingsPanelProps {
  status: Status | null
  onChanged?: () => void
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

const first = (node: Record<string, unknown>, ...keys: string[]): unknown => {
  for (const key of keys) {
    if (node[key] !== undefined && node[key] !== null) return node[key]
  }
  return undefined
}

function summaryOf(config: ConfigMap | null): string {
  if (!config) return 'Valid strategy document'
  const app = asRecord(config.app)
  const entry = asRecord(config.entry)
  const account = asRecord(config.account)
  const score = first(entry, 'minimum_score', 'min_score')
  const confirmations = first(entry, 'required_confirmations', 'min_confirmations')
  const positions = first(account, 'max_positions')
  const risk = first(account, 'risk_per_trade')
  const leverage = first(account, 'intraday_leverage', 'leverage')
  const version = first(app, 'version')
  if (score === undefined && positions === undefined) return 'Valid strategy document'
  return `Valid · V${version ?? '—'} · min score ${score ?? '—'} · ${confirmations ?? '—'} confirmations · ${positions ?? '—'} positions · ₹${risk ?? '—'} risk${leverage === undefined ? '' : ` · ${leverage}× leverage`}`
}

export default function SettingsPanel({ status, onChanged }: SettingsPanelProps) {
  const [yaml, setYaml] = useState('')
  const [draft, setDraft] = useState('')
  const [config, setConfig] = useState<ConfigMap | null>(null)
  const [parseState, setParseState] = useState<{ ok: boolean; message: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [pageError, setPageError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [creds, setCreds] = useState<CredentialsStatus | null>(null)
  const [credsBusy, setCredsBusy] = useState(false)

  const [challenge, setChallenge] = useState<ResetChallenge | null>(null)
  const [answer, setAnswer] = useState('')
  const [scope, setScope] = useState<'history' | 'factory'>('history')
  const [resetError, setResetError] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)
  const [secondsLeft, setSecondsLeft] = useState(0)

  const yamlRef = useRef('')
  const draftRef = useRef('')
  const dirtyRef = useRef(false)
  const parseTimerRef = useRef<number | null>(null)

  const dirty = draft !== yaml

  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])

  const applyServer = useCallback((next: { config: ConfigMap; yaml: string }) => {
    setYaml(next.yaml)
    setConfig(next.config)
    yamlRef.current = next.yaml
    if (!dirtyRef.current) {
      setDraft(next.yaml)
      draftRef.current = next.yaml
    }
  }, [])

  const loadConfig = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.config()
      setYaml(res.yaml)
      setConfig(res.config)
      yamlRef.current = res.yaml
      setDraft(res.yaml)
      draftRef.current = res.yaml
      setParseState(null)
      setPageError(null)
    } catch (err) {
      setPageError(errText(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadConfig()
    api.credentials()
      .then((res) => setCreds(res.status))
      .catch(() => setCreds(null))
  }, [loadConfig])

  useEffect(() => () => {
    if (parseTimerRef.current !== null) window.clearTimeout(parseTimerRef.current)
  }, [])

  const onDraftChange = useCallback((value: string) => {
    setDraft(value)
    draftRef.current = value
    setNotice(null)
    if (parseTimerRef.current !== null) window.clearTimeout(parseTimerRef.current)
    if (value === yamlRef.current) {
      setParseState(null)
      return
    }
    setParseState({ ok: true, message: 'Checking…' })
    parseTimerRef.current = window.setTimeout(() => {
      parseTimerRef.current = null
      api.parseConfig(value)
        .then((res) => {
          setConfig(res.config)
          setParseState({ ok: true, message: summaryOf(res.config) })
        })
        .catch((err) => setParseState({ ok: false, message: errText(err) }))
    }, 450)
  }, [])

  const saveYaml = useCallback(async () => {
    if (parseState && !parseState.ok) return
    setSaving(true)
    setPageError(null)
    try {
      const res = await api.saveConfig(draftRef.current)
      applyServer(res)
      setParseState({ ok: true, message: summaryOf(res.config) })
      setNotice(res.message)
      onChanged?.()
    } catch (err) {
      setPageError(errText(err))
    } finally {
      setSaving(false)
    }
  }, [applyServer, onChanged, parseState])

  const revertYaml = useCallback(() => {
    setDraft(yamlRef.current)
    draftRef.current = yamlRef.current
    setParseState(null)
    setNotice(null)
  }, [])

  const restoreDefaults = useCallback(async () => {
    if (!window.confirm('Replace strategy.yaml with the shipped default strategy? Your current rules are backed up to strategy.yaml.bak.')) return
    setSaving(true)
    setPageError(null)
    try {
      const res = await api.restoreConfig()
      applyServer(res)
      setNotice(res.message)
      onChanged?.()
    } catch (err) {
      setPageError(errText(err))
    } finally {
      setSaving(false)
    }
  }, [applyServer, onChanged])

  const testCredentials = useCallback(async () => {
    setCredsBusy(true)
    try {
      const res = await api.testCredentials()
      setCreds(res.status)
    } catch (err) {
      setPageError(errText(err))
    } finally {
      setCredsBusy(false)
    }
  }, [])

  const openReset = useCallback(async () => {
    setResetError(null)
    setAnswer('')
    try {
      const next = await api.resetChallenge()
      setChallenge(next)
      setSecondsLeft(next.expires_in)
    } catch (err) {
      setPageError(errText(err))
    }
  }, [])

  useEffect(() => {
    if (!challenge) return
    const id = window.setInterval(() => {
      setSecondsLeft((value) => (value > 0 ? value - 1 : 0))
    }, 1000)
    return () => window.clearInterval(id)
  }, [challenge])

  const submitReset = useCallback(async () => {
    if (!challenge) return
    const code = answer.trim().toUpperCase()
    if (!code) {
      setResetError('Type the code shown above, exactly as displayed.')
      return
    }
    setResetting(true)
    setResetError(null)
    try {
      const res = await api.resetAll({ challenge_id: challenge.challenge_id, answer: code, scope })
      setChallenge(null)
      setAnswer('')
      setNotice(res.message)
      await loadConfig()
      onChanged?.()
    } catch (err) {
      setResetError(errText(err))
    } finally {
      setResetting(false)
    }
  }, [answer, challenge, loadConfig, onChanged, scope])

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Settings &amp; strategy</h1>
          <p>
            The YAML editor writes <code>config/strategy.yaml</code>. The engine picks up a saved
            change on the next scan — no restart.
          </p>
        </div>
        <div className="toolbar">
          <button className="btn ghost" disabled={loading || saving} onClick={() => void loadConfig()}>
            Reload from file
          </button>
        </div>
      </div>

      {pageError && <div className="error-box">{pageError}</div>}
      {notice && (
        <div className="banner ok">
          <b>DONE</b> — {notice}
        </div>
      )}

      <StrategyFlowChart config={config} dirty={dirty && !loading} />

      <div className="card">
        <div className="card-head">
          <h2>Strategy YAML</h2>
          <span className={`sub ${parseState && !parseState.ok ? 'neg' : ''}`}>
            {parseState ? parseState.message : dirty ? 'Unsaved changes' : 'In sync with the file'}
          </span>
        </div>
        <div className="card-body">
          <textarea
            className="yaml-editor"
            spellCheck={false}
            value={draft}
            disabled={loading || saving}
            onChange={(event) => onDraftChange(event.target.value)}
          />
          <div className="toolbar" style={{ marginTop: 10 }}>
            <button
              className="btn primary"
              disabled={!dirty || saving || (parseState !== null && !parseState.ok)}
              onClick={() => void saveYaml()}
            >
              {saving ? 'Saving…' : 'Save YAML'}
            </button>
            <button className="btn ghost" disabled={!dirty || saving} onClick={revertYaml}>
              Revert
            </button>
            <button className="btn ghost" disabled={saving} onClick={() => void restoreDefaults()}>
              Restore defaults
            </button>
          </div>
          <div className="tiny muted" style={{ marginTop: 8 }}>
            Saving rewrites the file atomically and keeps a <code>strategy.yaml.bak</code> copy.
            Invalid YAML is rejected before anything is written.
          </div>
        </div>
      </div>

      <div className="settings-wide">
        <div className="card">
          <div className="card-head">
            <h2>Groww credentials</h2>
            <span className="sub">Market data only — this build never places a real order</span>
          </div>
          <div className="card-body">
            {creds ? (
              <>
                <div className="stat-line">
                  <span className="l">Auth mode</span>
                  <span className="v">
                    {creds.auth_mode === 'approval' ? 'Approval key' : creds.auth_mode === 'totp' ? 'TOTP' : 'Not configured'}
                  </span>
                </div>
                <div className="stat-line">
                  <span className="l">API key</span>
                  <span className="v">{creds.api_key_present ? creds.api_key_hint : 'missing'}</span>
                </div>
                <div className="stat-line">
                  <span className="l">API secret</span>
                  <span className="v">{creds.api_secret_present ? 'stored in .env' : 'missing'}</span>
                </div>
                <div className="stat-line">
                  <span className="l">Access token</span>
                  <span className="v">
                    {creds.token_cached ? `cached until ${creds.token_expiry ?? '—'}` : 'not cached'}
                  </span>
                </div>
                <div className="stat-line">
                  <span className="l">State</span>
                  <span className={`v ${creds.state === 'ready' ? 'pos' : 'neg'}`}>{creds.state}</span>
                </div>
                <div className="tiny muted" style={{ marginTop: 8 }}>{creds.message}</div>
                {creds.needs_daily_approval && (
                  <div className="inline-note">
                    Approval keys expire every day at 06:00 IST. Approve the key at
                    groww.in/trade-api/api-keys each morning, then press Test.
                  </div>
                )}
                <div className="toolbar" style={{ marginTop: 10 }}>
                  <button className="btn" disabled={credsBusy} onClick={() => void testCredentials()}>
                    {credsBusy ? 'Testing…' : 'Test connection'}
                  </button>
                </div>
              </>
            ) : (
              <div className="empty-state">Loading credential status…</div>
            )}
          </div>
        </div>

        <div className="card danger-card">
          <div className="card-head">
            <h2>Reset</h2>
            <span className="sub">Clears the ledger and market caches</span>
          </div>
          <div className="card-body">
            <p className="tiny muted" style={{ marginTop: 0 }}>
              Resetting wipes every paper trade, position, signal and daily snapshot, so the Overview
              starts again from today with your configured capital. A one-time code is required.
            </p>
            <div className="toolbar">
              <button className="btn danger" onClick={() => void openReset()}>
                Reset app…
              </button>
            </div>
            {status && (
              <div className="tiny muted" style={{ marginTop: 10 }}>
                Engine {status.state.toUpperCase()} · equity ₹{status.equity.toLocaleString('en-IN')} ·{' '}
                {status.position_count} open position{status.position_count === 1 ? '' : 's'}
              </div>
            )}
          </div>
        </div>
      </div>

      {challenge && (
        <div className="modal-backdrop" role="dialog" aria-modal="true">
          <div className="modal">
            <h2>Confirm reset</h2>
            <p>
              Type the code below to continue. It expires in{' '}
              <b>{Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, '0')}</b>.
            </p>
            <div className="captcha-display">{challenge.prompt}</div>
            <input
              className="captcha-code"
              type="text"
              autoFocus
              spellCheck={false}
              placeholder="TYPE THE CODE"
              value={answer}
              disabled={resetting}
              onChange={(event) => setAnswer(event.target.value.toUpperCase())}
              onKeyDown={(event) => {
                if (event.key === 'Enter') void submitReset()
              }}
            />

            <div className="scope-row">
              <label className={scope === 'history' ? 'scope-option active' : 'scope-option'}>
                <input
                  type="radio"
                  name="reset-scope"
                  checked={scope === 'history'}
                  onChange={() => setScope('history')}
                />
                <span>
                  <b>History only</b>
                  <small>Keep my strategy.yaml, clear trades and caches.</small>
                </span>
              </label>
              <label className={scope === 'factory' ? 'scope-option active' : 'scope-option'}>
                <input
                  type="radio"
                  name="reset-scope"
                  checked={scope === 'factory'}
                  onChange={() => setScope('factory')}
                />
                <span>
                  <b>Factory reset</b>
                  <small>Also restore the default strategy.yaml.</small>
                </span>
              </label>
            </div>

            {resetError && <div className="modal-error">{resetError}</div>}

            <div className="toolbar" style={{ marginTop: 14, justifyContent: 'flex-end' }}>
              <button
                className="btn ghost"
                disabled={resetting}
                onClick={() => {
                  setChallenge(null)
                  setResetError(null)
                }}
              >
                Cancel
              </button>
              <button className="btn danger" disabled={resetting} onClick={() => void submitReset()}>
                {resetting ? 'Resetting…' : scope === 'factory' ? 'Reset everything' : 'Clear history'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}
