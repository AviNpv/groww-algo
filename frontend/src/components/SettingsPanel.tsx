import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ConfigSaveResponse, CredentialsStatus, ResetChallenge, Status } from '../types'
import { api } from '../api'

type ConfigMap = Record<string, unknown>
type SaveState = 'idle' | 'saving' | 'saved' | 'error'

interface QuickField {
  section: string
  key: string
  label: string
  kind: 'number' | 'boolean'
  unit?: string
  min?: number
  max?: number
  step?: number
}

interface SettingsPanelProps {
  status: Status | null
  onChanged?: () => void
}

/**
 * Quick controls and the raw YAML editor are two views of the same file:
 *
 *   quick control  -> POST /api/config/value -> server rewrites YAML
 *                  -> the editor text is replaced with the server's YAML
 *   YAML save      -> PUT  /api/config       -> server returns parsed config
 *                  -> every quick control is rebuilt from it
 *
 * The only rule is that the two views must never diverge silently, so quick
 * controls lock while the editor has unsaved text (and vice versa).
 */
const FIELDS: QuickField[] = [
  { section: 'account', key: 'starting_capital', label: 'Starting capital', kind: 'number', unit: '₹', min: 1000, step: 500 },
  { section: 'account', key: 'risk_per_trade', label: 'Risk per trade', kind: 'number', unit: '₹', min: 1, step: 10 },
  { section: 'account', key: 'max_positions', label: 'Max open positions', kind: 'number', unit: 'trades', min: 1, max: 20, step: 1 },
  { section: 'account', key: 'max_daily_loss', label: 'Daily loss cap', kind: 'number', unit: '₹', min: 0, step: 50 },
  { section: 'account', key: 'max_position_pct', label: 'Max exposure per name', kind: 'number', unit: '%', min: 1, max: 100, step: 1 },
  { section: 'entry', key: 'min_score', label: 'Minimum score', kind: 'number', unit: '/ 100', min: 0, max: 100, step: 1 },
  { section: 'entry', key: 'min_confirmations', label: 'Min factor confirmations', kind: 'number', unit: 'of 6', min: 0, max: 6, step: 1 },
  { section: 'entry', key: 'min_relative_volume', label: 'Min relative volume', kind: 'number', unit: '×', min: 0, max: 10, step: 0.1 },
  { section: 'entry', key: 'min_risk_reward', label: 'Min risk : reward', kind: 'number', unit: 'R', min: 0, max: 10, step: 0.1 },
  { section: 'entry', key: 'max_vwap_extension_pct', label: 'Max chase above VWAP', kind: 'number', unit: '%', min: 0, max: 20, step: 0.1 },
  { section: 'entry', key: 'min_atr_pct', label: 'Min ATR (volatility)', kind: 'number', unit: '%', min: 0, max: 20, step: 0.05 },
  { section: 'entry', key: 'max_atr_pct', label: 'Max ATR (volatility)', kind: 'number', unit: '%', min: 0, max: 30, step: 0.1 },
  { section: 'exit', key: 'target_r_mult', label: 'Target', kind: 'number', unit: 'R', min: 0.1, max: 10, step: 0.1 },
  { section: 'exit', key: 'trail_after_r', label: 'Start trailing after', kind: 'number', unit: 'R', min: 0, max: 10, step: 0.1 },
  { section: 'exit', key: 'time_stop_minutes', label: 'Time stop', kind: 'number', unit: 'min', min: 1, max: 600, step: 5 },
  { section: 'engine', key: 'scan_interval_sec', label: 'Scan every', kind: 'number', unit: 'sec', min: 15, max: 3600, step: 5 },
  { section: 'engine', key: 'autotrade', label: 'Auto paper-trade', kind: 'boolean' },
  { section: 'costs', key: 'brokerage_per_order', label: 'Brokerage cap', kind: 'number', unit: '₹/order', min: 0, step: 1 },
  { section: 'costs', key: 'slippage_pct', label: 'Slippage per side', kind: 'number', unit: '%', min: 0, max: 5, step: 0.01 },
]

const FIELD_BY_ID: Record<string, QuickField> = Object.fromEntries(
  FIELDS.map((field) => [`${field.section}.${field.key}`, field]),
)

function sectionOf(config: ConfigMap | null, section: string): Record<string, unknown> {
  const node = config?.[section]
  return node && typeof node === 'object' ? (node as Record<string, unknown>) : {}
}

function readField(config: ConfigMap | null, section: string, key: string): unknown {
  return sectionOf(config, section)[key]
}

function quickFromConfig(config: ConfigMap | null): Record<string, string> {
  const out: Record<string, string> = {}
  for (const field of FIELDS) {
    const value = readField(config, field.section, field.key)
    out[`${field.section}.${field.key}`] = value === undefined || value === null ? '' : String(value)
  }
  return out
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function summaryOf(config: ConfigMap | null): string {
  const score = readField(config, 'entry', 'min_score')
  const positions = readField(config, 'account', 'max_positions')
  const risk = readField(config, 'account', 'risk_per_trade')
  if (score === undefined && positions === undefined) return 'Valid strategy document'
  return `Valid · min score ${score ?? '—'} · ${positions ?? '—'} positions · ₹${risk ?? '—'} risk`
}

export default function SettingsPanel({ status, onChanged }: SettingsPanelProps) {
  const [yaml, setYaml] = useState('')
  const [draft, setDraft] = useState('')
  const [quick, setQuick] = useState<Record<string, string>>({})
  const [quickState, setQuickState] = useState<SaveState>('idle')
  const [quickError, setQuickError] = useState<string | null>(null)
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
  const quickRef = useRef<Record<string, string>>({})
  const dirtyRef = useRef(false)
  const pendingRef = useRef<Set<string>>(new Set())
  const busyRef = useRef(false)
  const flushTimerRef = useRef<number | null>(null)
  const parseTimerRef = useRef<number | null>(null)

  const dirty = draft !== yaml
  const controlsLocked = dirty || saving || resetting || loading

  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])

  const applyServer = useCallback(
    (next: { config: ConfigMap; yaml: string }, syncQuick: boolean) => {
      setYaml(next.yaml)
      yamlRef.current = next.yaml
      // Only replace the editor text when it holds no unsaved work.
      if (!dirtyRef.current) {
        setDraft(next.yaml)
        draftRef.current = next.yaml
      }
      if (syncQuick) {
        const nextQuick = quickFromConfig(next.config)
        quickRef.current = nextQuick
        setQuick(nextQuick)
      }
    },
    [],
  )

  const loadConfig = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.config()
      setYaml(res.yaml)
      yamlRef.current = res.yaml
      setDraft(res.yaml)
      draftRef.current = res.yaml
      const nextQuick = quickFromConfig(res.config)
      quickRef.current = nextQuick
      setQuick(nextQuick)
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

  const doFlush = useCallback(async () => {
    if (busyRef.current) return
    busyRef.current = true
    setQuickState('saving')
    setQuickError(null)
    try {
      let last: ConfigSaveResponse | null = null
      while (pendingRef.current.size) {
        const ids = Array.from(pendingRef.current)
        pendingRef.current.clear()
        for (const id of ids) {
          const field = FIELD_BY_ID[id]
          if (!field) continue
          const raw = (quickRef.current[id] ?? '').trim()
          if (field.kind === 'boolean') {
            last = await api.setConfigValue(field.section, field.key, raw === 'true')
            continue
          }
          if (!raw) continue
          const value = Number(raw)
          if (!Number.isFinite(value)) continue
          last = await api.setConfigValue(field.section, field.key, value)
        }
        if (last) applyServer(last, false)
      }
      setQuickState('saved')
      onChanged?.()
    } catch (err) {
      setQuickState('error')
      setQuickError(errText(err))
    } finally {
      busyRef.current = false
    }
  }, [applyServer, onChanged])

  const queueQuick = useCallback((id: string, raw: string) => {
    quickRef.current[id] = raw
    setQuick((prev) => ({ ...prev, [id]: raw }))
    pendingRef.current.add(id)
    setQuickState('saving')
    if (flushTimerRef.current !== null) window.clearTimeout(flushTimerRef.current)
    flushTimerRef.current = window.setTimeout(() => {
      flushTimerRef.current = null
      void doFlush()
    }, 700)
  }, [doFlush])

  // Never lose a debounced edit when the tab is switched.
  useEffect(() => () => {
    if (flushTimerRef.current !== null) window.clearTimeout(flushTimerRef.current)
    if (parseTimerRef.current !== null) window.clearTimeout(parseTimerRef.current)
    if (pendingRef.current.size) void doFlush()
  }, [doFlush])

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
        .then((res) => setParseState({ ok: true, message: summaryOf(res.config) }))
        .catch((err) => setParseState({ ok: false, message: errText(err) }))
    }, 450)
  }, [])

  const saveYaml = useCallback(async () => {
    if (parseState && !parseState.ok) return
    setSaving(true)
    setPageError(null)
    try {
      const res = await api.saveConfig(draftRef.current)
      applyServer(res, true)
      setParseState({ ok: true, message: summaryOf(res.config) })
      setQuickState('idle')
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
      applyServer(res, true)
      setQuickState('idle')
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

  const quickStatusText = useMemo(() => {
    if (quickState === 'saving') return 'Saving…'
    if (quickState === 'saved') return 'Saved to strategy.yaml'
    if (quickState === 'error') return quickError ?? 'Save failed'
    return dirty ? 'Quick controls paused until the YAML is saved or reverted' : 'Changes save automatically'
  }, [dirty, quickError, quickState])

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Settings &amp; strategy</h1>
          <p>
            Quick controls and the YAML editor both write <code>config/strategy.yaml</code>. The engine
            picks up a saved change on the next scan — no restart.
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

      <div className="settings-grid">
        <div className="card">
          <div className="card-head">
            <h2>Quick controls</h2>
            <span className={`sub ${quickState === 'error' ? 'neg' : ''}`}>{quickStatusText}</span>
          </div>
          <div className="card-body">
            {dirty && (
              <div className="inline-note">
                The editor has unsaved YAML. Save or revert it to unlock these controls — this keeps
                the two views from overwriting each other.
              </div>
            )}
            {FIELDS.map((field) => {
              const id = `${field.section}.${field.key}`
              const raw = quick[id] ?? ''
              return (
                <div className="field" key={id}>
                  <span className="l">{field.label}</span>
                  {field.kind === 'boolean' ? (
                    <label className="switch-row">
                      <input
                        type="checkbox"
                        checked={raw === 'true'}
                        disabled={controlsLocked}
                        onChange={(event) => queueQuick(id, event.target.checked ? 'true' : 'false')}
                      />
                      <span className="tiny muted">{raw === 'true' ? 'enabled' : 'disabled'}</span>
                    </label>
                  ) : (
                    <input
                      className="field-input"
                      type="number"
                      value={raw}
                      min={field.min}
                      max={field.max}
                      step={field.step}
                      disabled={controlsLocked}
                      onChange={(event) => queueQuick(id, event.target.value)}
                    />
                  )}
                  <span className="val">{field.kind === 'boolean' ? '' : field.unit ?? ''}</span>
                </div>
              )
            })}
            <div className="tiny muted" style={{ marginTop: 10 }}>
              Values are clamped by the server: weights must total 100 and the scan interval cannot go
              below 15 seconds.
            </div>
          </div>
        </div>

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
