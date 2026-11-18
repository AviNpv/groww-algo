import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  CalendarResponse,
  EquityPoint,
  LiveResponse,
  Overview,
  PositionsResponse,
  ScanResponse,
  ScanRow,
  Status,
  Trade,
} from './types'
import { api, exactPct, exactSigned, money, timeOnly } from './api'
import PnlCalendar from './components/PnlCalendar'
import EquityCurve from './components/EquityCurve'
import ScanTable from './components/ScanTable'
import StrategyChart from './components/StrategyChart'
import TradesTable from './components/TradesTable'
import SymbolDrawer from './components/SymbolDrawer'
import SettingsPanel from './components/SettingsPanel'
import GrowwPortfolio from './components/GrowwPortfolio'

type Tab = 'overview' | 'scanner' | 'chart' | 'positions' | 'trades' | 'portfolio' | 'settings'

/** Dashboard polling interval - the whole screen refreshes once per second. */
const LIVE_REFRESH_MS = 1000

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'overview', label: 'Overview' },
  { id: 'scanner', label: 'Scanner' },
  { id: 'chart', label: 'Strategy Chart' },
  { id: 'positions', label: 'Positions' },
  { id: 'trades', label: 'Trades' },
  { id: 'portfolio', label: 'Groww Portfolio' },
  { id: 'settings', label: 'Settings' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<Status | null>(null)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [calendar, setCalendar] = useState<CalendarResponse | null>(null)
  const [curve, setCurve] = useState<EquityPoint[]>([])
  const [scan, setScan] = useState<ScanResponse | null>(null)
  const [positions, setPositions] = useState<PositionsResponse | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [lastTick, setLastTick] = useState<string | null>(null)
  const [calTick, setCalTick] = useState(0)
  const [autotrade, setAutotrade] = useState(false)
  const [selectedDay, setSelectedDay] = useState<string | null>(null)
  const [selectedRow, setSelectedRow] = useState<ScanRow | null>(null)
  const [chartSymbol, setChartSymbol] = useState('')
  const [scanFilter, setScanFilter] = useState('')
  const [scanStatus, setScanStatus] = useState('ALL')
  const [tradeFilter, setTradeFilter] = useState('')
  const [month, setMonth] = useState(() => {
    const now = new Date()
    return { year: now.getFullYear(), month: now.getMonth() + 1 }
  })

  const loadCore = useCallback(async () => {
    const results = await Promise.allSettled([
      api.status(),
      api.overview(),
      api.scan(),
      api.positions(),
      api.equity(400),
    ])
    const [s, o, sc, p, eq] = results
    if (s.status === 'fulfilled') setStatus(s.value)
    if (o.status === 'fulfilled') setOverview(o.value)
    if (sc.status === 'fulfilled') setScan(sc.value)
    if (p.status === 'fulfilled') setPositions(p.value)
    if (eq.status === 'fulfilled') setCurve(eq.value.curve)
    if (results.every((r) => r.status === 'rejected')) {
      setError('Backend not reachable. Start it with: .venv\\Scripts\\python.exe -m uvicorn engine.api:app --port 8000')
    } else {
      setError(null)
    }
  }, [])

  const loadTrades = useCallback(async () => {
    try {
      const result = await api.trades(300)
      setTrades(result.trades)
    } catch {
      /* the core loader already surfaces backend errors */
    }
  }, [])

  /**
   * One endpoint returns status, overview, calendar, equity curve, positions,
   * trades and the latest scan together, after re-marking open positions.
   * Every number on screen - including the calendar cell for today - is
   * therefore from the same instant and stays in sync on every tick.
   */
  const applySnapshot = useCallback((data: LiveResponse) => {
    setStatus(data.status)
    setOverview(data.overview)
    setScan(data.scan)
    setPositions(data.positions)
    setCurve(data.curve)
    setTrades(data.trades)
    setCalendar(data.calendar)
    setLastTick(data.ts)
    // Clear only connectivity errors; action errors stay visible until dismissed.
    setError((prev) => (prev?.startsWith('Backend not reachable') ? null : prev))
  }, [])

  const ticking = useRef(false)
  const tick = useCallback(async () => {
    if (ticking.current) return
    ticking.current = true
    try {
      applySnapshot(await api.live(month.year, month.month))
    } catch {
      setError('Backend not reachable. Start it with: .venv\\Scripts\\python.exe -m uvicorn engine.api:app --port 8000')
    } finally {
      ticking.current = false
    }
  }, [applySnapshot, month.year, month.month])

  useEffect(() => {
    void tick()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void tick()
    }, LIVE_REFRESH_MS)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void tick()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [tick])

  const runScan = async () => {
    setBusy(true)
    try {
      const result = await api.runScan(autotrade)
      setScan(result)
      await loadCore()
      await loadTrades()
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const toggleEngine = async () => {
    setBusy(true)
    try {
      const next = status?.state === 'running' ? await api.stopEngine() : await api.startEngine()
      setStatus(next)
      await loadCore()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const closePosition = async (tradeId: number, symbol: string) => {
    if (!window.confirm(`Close the ${symbol} paper position at market?`)) return
    try {
      await api.closePosition(tradeId)
      await loadCore()
      await loadTrades()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  const selectedDayData = useMemo(
    () => calendar?.days.find((d) => d.day === selectedDay) ?? null,
    [calendar, selectedDay],
  )

  const filteredScan = useMemo(() => {
    const rows = scan?.results ?? []
    const needle = scanFilter.trim().toUpperCase()
    return rows.filter((row) => {
      if (needle && !row.symbol.includes(needle)) return false
      if (scanStatus !== 'ALL' && row.status !== scanStatus) return false
      return true
    })
  }, [scan, scanFilter, scanStatus])

  const scanSymbols = useMemo(() => (scan?.results ?? []).map((r) => r.symbol), [scan])

  const chartRow = useMemo<ScanRow>(() => {
    const found = (scan?.results ?? []).find((r) => r.symbol === chartSymbol)
    if (found) return found
    return {
      symbol: chartSymbol || scanSymbols[0] || 'RELIANCE',
      price: 0, change_pct: 0, volume: null, score: 0, status: '', confirmations: 0,
      buckets: {}, patterns: [], support: null, resistance: null, breakout: false,
      quality: { score: 0, label: '—', components: {}, fundamentals_used: false },
      indicators: {}, reasons: [], atr: null, fundamentals: null,
    }
  }, [chartSymbol, scan, scanSymbols])

  const sourceLabel = status?.data_source.active === 'groww' ? 'GROWW LIVE' : 'YAHOO DELAYED'
  const sourceClass = status?.data_source.active === 'groww' ? 'live' : 'warn'

  return (
    <div>
      <header className="header">
        <div className="brand">
          <span className="brand-mark">₹</span>
          GROWW ALGO <small>intraday desk</small>
        </div>
        <nav className="nav">
          {TABS.map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? 'active' : ''}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="header-right">
          <span className={`pill ${sourceClass}`} title={status?.data_source.message ?? ''}>
            <span className="dot" />{sourceLabel}
          </span>
          <span className={`pill ${status?.market_open ? 'live' : 'off'}`}>
            <span className="dot" />{status?.market_open ? 'MARKET OPEN' : 'MARKET CLOSED'}
          </span>
          <span className="pill live" title={`Auto-refresh every second · last update ${lastTick ?? '—'}`}>
            <span className="dot" />LIVE {lastTick ? new Date(lastTick).toLocaleTimeString('en-IN', { hour12: false }) : '—'}
          </span>
          <span className="equity-chip">₹{money(overview?.equity ?? 0)}</span>
        </div>
      </header>

      <main className="container">
        {error && <div className="error-box">{error}</div>}

        {tab === 'overview' && (
          <section>
            <div className="page-head">
              <div>
                <h1>Overview</h1>
                <p>
                  Paper intraday desk · {status?.config?.universe_size ?? 0} symbols scanned ·{' '}
                  {status?.mode?.toUpperCase() ?? 'PAPER'} mode
                </p>
              </div>
              <div className="toolbar">
                <span className="pill">
                  <span className="dot" />last scan {timeOnly(status?.last_scan_at ?? null)}
                </span>
                <button className="btn ghost" onClick={() => void loadCore()}>Refresh</button>
              </div>
            </div>

            {overview?.demo && (
              <div className="banner">
                <b>DEMO DATA</b> — the calendar below is seeded sample history so you can see the layout.
                Reset it before the first live paper session.
              </div>
            )}

            <div className="kpi-grid">
              <div className="kpi">
                <div className="kpi-label">Equity</div>
                <div className="kpi-value">₹{money(overview?.equity)}</div>
                <div className="kpi-sub">from ₹{money(overview?.starting_capital, 0)}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Today P&amp;L</div>
                <div className={`kpi-value ${(overview?.today_pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                  {exactSigned(overview?.today_pnl)}
                </div>
                <div className="kpi-sub">{exactPct(overview?.today_pnl_pct)}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Total P&amp;L</div>
                <div className={`kpi-value ${(overview?.total_pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                  {exactSigned(overview?.total_pnl)}
                </div>
                <div className="kpi-sub">{exactPct(overview?.total_return_pct)} all time</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Win rate</div>
                <div className="kpi-value">{(overview?.win_rate ?? 0).toFixed(1)}%</div>
                <div className="kpi-sub">{overview?.closed_trades ?? 0} closed trades</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Profit factor</div>
                <div className={`kpi-value ${(overview?.profit_factor ?? 0) >= 1 ? 'pos' : 'neg'}`}>
                  {(overview?.profit_factor ?? 0).toFixed(2)}
                </div>
                <div className="kpi-sub">
                  avg win ₹{money(overview?.avg_win, 0)} / loss ₹{money(overview?.avg_loss, 0)}
                </div>
              </div>
              <div className="kpi">
                <div className="kpi-label">Max drawdown</div>
                <div className="kpi-value neg">{(overview?.max_drawdown_pct ?? 0).toFixed(2)}%</div>
                <div className="kpi-sub">
                  {overview?.green_days ?? 0} green / {overview?.red_days ?? 0} red days
                </div>
              </div>
            </div>

            <div className="grid-2">
              <div className="card">
                {calendar ? (
                  <PnlCalendar
                    key={calTick}
                    year={calendar.year}
                    month={calendar.month}
                    days={calendar.days}
                    monthPnl={calendar.month_pnl}
                    sessions={calendar.sessions}
                    green={calendar.green}
                    red={calendar.red}
                    selected={selectedDay}
                    onSelect={setSelectedDay}
                    onMonthChange={(year, nextMonth) => {
                      setSelectedDay(null)
                      setMonth({ year, month: nextMonth })
                    }}
                  />
                ) : (
                  <div className="empty-state">Loading calendar…</div>
                )}
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <div className="card">
                  <div className="card-head">
                    <h2>Equity curve</h2>
                    <span className="sub">{curve.length} sessions</span>
                  </div>
                  <div className="card-body">
                    <EquityCurve curve={curve} />
                  </div>
                </div>

                <div className="card">
                  <div className="card-head">
                    <h2>Bot status</h2>
                    <button className="btn small" disabled={busy} onClick={() => void toggleEngine()}>
                      {status?.state === 'running' ? 'Stop bot' : 'Start bot'}
                    </button>
                  </div>
                  <div className="card-body">
                    <div className="stat-line">
                      <span className="l">Engine</span>
                      <span className={status?.state === 'running' ? 'pos' : 'neg'} style={{ fontWeight: 600 }}>
                        {status?.state?.toUpperCase() ?? '—'}
                      </span>
                    </div>
                    <div className="stat-line">
                      <span className="l">Data feed</span>
                      <span className="v">
                        {status?.data_source.active?.toUpperCase() ?? '—'}
                        {status?.data_source.groww_ok ? '' : ' (fallback)'}
                      </span>
                    </div>
                    <div className="stat-line">
                      <span className="l">Entry window</span>
                      <span className="v">{status?.entry_window ? 'OPEN' : 'CLOSED'}</span>
                    </div>
                    <div className="stat-line">
                      <span className="l">Open positions</span>
                      <span className="v">
                        {positions?.positions.length ?? 0} / {status?.config.max_positions ?? 0}
                      </span>
                    </div>
                    <div className="stat-line">
                      <span className="l">Next scan</span>
                      <span className="v">{timeOnly(status?.next_scan_at ?? null)}</span>
                    </div>
                    <div className="stat-line">
                      <span className="l">Risk / trade</span>
                      <span className="v">₹{money(status?.config.risk_per_trade, 0)}</span>
                    </div>
                    <div className="stat-line">
                      <span className="l">Daily loss cap</span>
                      <span className="v">₹{money(status?.config.max_daily_loss, 0)}</span>
                    </div>
                    {status?.last_error && (
                      <div className="tiny neg" style={{ marginTop: 8 }}>{status.last_error}</div>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {selectedDayData && (
              <div className="card" style={{ marginTop: 14 }}>
                <div className="card-head">
                  <h2>Session detail · {selectedDayData.day}</h2>
                  <button className="btn small ghost" onClick={() => setSelectedDay(null)}>Clear</button>
                </div>
                <div className="card-body">
                  <div className="detail-grid">
                    <div className="detail-item">
                      <div className="l">P&amp;L</div>
                      <div className={`v ${selectedDayData.pnl >= 0 ? 'pos' : 'neg'}`}>
                        {exactSigned(selectedDayData.pnl)}
                      </div>
                    </div>
                    <div className="detail-item">
                      <div className="l">Return</div>
                      <div className={`v ${selectedDayData.pnl >= 0 ? 'pos' : 'neg'}`}>
                        {exactPct(selectedDayData.pnl_pct)}
                      </div>
                    </div>
                    <div className="detail-item">
                      <div className="l">Closing equity</div>
                      <div className="v">₹{money(selectedDayData.equity)}</div>
                    </div>
                    <div className="detail-item">
                      <div className="l">Trades</div>
                      <div className="v">
                        {selectedDayData.trades} ({selectedDayData.wins}W / {selectedDayData.losses}L)
                      </div>
                    </div>
                    <div className="detail-item">
                      <div className="l">Realized</div>
                      <div className={`v ${selectedDayData.realized_pnl >= 0 ? 'pos' : 'neg'}`}>
                        {exactSigned(selectedDayData.realized_pnl)}
                      </div>
                    </div>
                    <div className="detail-item">
                      <div className="l">Unrealized</div>
                      <div className={`v ${selectedDayData.unrealized_pnl >= 0 ? 'pos' : 'neg'}`}>
                        {exactSigned(selectedDayData.unrealized_pnl)}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-head">
                <h2>Recent trades</h2>
                <button className="btn small ghost" onClick={() => setTab('trades')}>View all</button>
              </div>
              <TradesTable trades={trades.slice(0, 6)} filter="" />
            </div>
          </section>
        )}

        {tab === 'scanner' && (
          <section>
            <div className="page-head">
              <div>
                <h1>Market scanner</h1>
                <p>
                  {scan?.scanned ?? 0} symbols scanned · {scan?.qualified ?? 0} qualified ·{' '}
                  {scan?.watchlist ?? 0} on watchlist · source {scan?.source ?? '—'}
                </p>
              </div>
              <div className="toolbar">
                <label className="tiny muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={autotrade}
                    onChange={(e) => setAutotrade(e.target.checked)}
                  />
                  auto-trade new signals
                </label>
                <button className="btn primary" disabled={busy} onClick={() => void runScan()}>
                  {busy ? 'Scanning…' : 'Scan now'}
                </button>
              </div>
            </div>

            {autotrade && (
              <div className="banner">
                Auto-trade is ON for this manual scan. Orders are still paper-only and obey the
                position, risk and daily-loss limits in <code>config/strategy.yaml</code>.
              </div>
            )}

            <div className="card">
              <div className="card-head">
                <div className="toolbar">
                  <input
                    className="input"
                    placeholder="Search symbol"
                    value={scanFilter}
                    onChange={(e) => setScanFilter(e.target.value)}
                  />
                  <select
                    className="input select"
                    value={scanStatus}
                    onChange={(e) => setScanStatus(e.target.value)}
                  >
                    <option value="ALL">All statuses</option>
                    <option value="QUALIFIED">Qualified</option>
                    <option value="WATCHLIST">Watchlist</option>
                    <option value="REJECT">Reject</option>
                  </select>
                </div>
                <div className="meta-row">
                  <span>last scan <b>{timeOnly(scan?.ts ?? null)}</b></span>
                  <span>duration <b>{scan?.duration_ms ? `${(scan.duration_ms / 1000).toFixed(1)}s` : '—'}</b></span>
                  <span>showing <b>{filteredScan.length}</b></span>
                </div>
              </div>
              <ScanTable rows={filteredScan} onSelect={setSelectedRow} />
            </div>

            {(scan?.trade_errors?.length ?? 0) > 0 && (
              <div className="card" style={{ marginTop: 14 }}>
                <div className="card-head"><h2>Bot orders this scan</h2></div>
                <div className="card-body">
                  {scan?.trade_errors.map((line, i) => (
                    <div className="stat-line" key={i}>
                      <span className="l">{line}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {!autotrade && (scan?.signals?.filter((s) => s.action === 'BUY').length ?? 0) > 0 && (
              <div className="card" style={{ marginTop: 14 }}>
                <div className="card-head"><h2>Signals the bot would take</h2></div>
                <div className="card-body">
                  {scan?.signals.filter((s) => s.action === 'BUY').slice(0, 5).map((signal) => (
                    <div className="stat-line" key={signal.symbol}>
                      <span className="l">
                        {signal.symbol} · qty {signal.qty} · stop ₹{money(signal.stop)} · target ₹{money(signal.target)}
                      </span>
                      <span className="v">{signal.score?.toFixed(0)}/100</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </section>
        )}

        {tab === 'chart' && (
          <section>
            <div className="page-head">
              <div>
                <h1>Strategy chart</h1>
                <p>
                  Candles with the active strategy drawn on them — entry, stop, target, VWAP, EMAs,
                  support/resistance and pattern markers. Pick any scanned symbol or type your own.
                </p>
              </div>
              <div className="toolbar">
                <input
                  className="input"
                  list="chart-symbols"
                  placeholder="Symbol (e.g. RELIANCE)"
                  value={chartSymbol}
                  onChange={(e) => setChartSymbol(e.target.value.trim().toUpperCase())}
                />
                <datalist id="chart-symbols">
                  {scanSymbols.map((symbol) => <option key={symbol} value={symbol} />)}
                </datalist>
                <button className="btn ghost" onClick={() => setChartSymbol('')}>Clear</button>
              </div>
            </div>
            <div className="card">
              <div className="card-body">
                <StrategyChart row={chartRow} />
              </div>
            </div>
            <div className="tiny muted" style={{ marginTop: 10 }}>
              Levels come from <code>config/strategy.yaml</code>; candle data comes from Groww when
              approved, otherwise the delayed Yahoo feed. Paper trading only — no real orders.
            </div>
          </section>
        )}

        {tab === 'positions' && (
          <section>
            <div className="page-head">
              <div>
                <h1>Positions</h1>
                <p>
                  Cash ₹{money(positions?.cash)} · equity ₹{money(positions?.equity)} ·{' '}
                  {positions?.positions.length ?? 0} open
                </p>
              </div>
              <button className="btn ghost" onClick={() => void loadCore()}>Refresh</button>
            </div>

            {!positions?.positions.length && (
              <div className="card"><div className="empty-state">No open paper positions. The bot opens them automatically during the entry window.</div></div>
            )}

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 14 }}>
              {positions?.positions.map((p) => (
                <div className="card" key={p.trade_id}>
                  <div className="card-head">
                    <div>
                      <h2>{p.symbol}</h2>
                      <span className="sub">
                        {p.qty} @ ₹{money(p.avg_price)} · opened {timeOnly(p.opened_at)}
                      </span>
                    </div>
                    <span className={`status-pill ${p.unrealized >= 0 ? 'buy' : 'sell'}`}>
                      {p.unrealized >= 0 ? 'IN PROFIT' : 'IN LOSS'}
                    </span>
                  </div>
                  <div className="card-body">
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                      <span className="muted tiny">Unrealized P&amp;L</span>
                      <span className={`kpi-value ${p.unrealized >= 0 ? 'pos' : 'neg'}`} style={{ fontSize: 22 }}>
                        {exactSigned(p.unrealized)}
                      </span>
                    </div>
                    <div className="stat-line"><span className="l">Last price</span><span className="v">₹{money(p.last_price)}</span></div>
                    <div className="stat-line"><span className="l">Return</span><span className={`v ${p.unrealized_pct >= 0 ? 'pos' : 'neg'}`}>{exactPct(p.unrealized_pct)}</span></div>
                    <div className="stat-line"><span className="l">R multiple</span><span className="v">{p.r_multiple?.toFixed(2) ?? '—'}</span></div>
                    <div className="stat-line"><span className="l">Stop</span><span className="v neg">₹{money(p.stop)}</span></div>
                    <div className="stat-line"><span className="l">Target</span><span className="v pos">₹{money(p.target)}</span></div>
                    <div className="stat-line"><span className="l">Value</span><span className="v">₹{money(p.value)}</span></div>
                    <button
                      className="btn danger"
                      style={{ width: '100%', marginTop: 10 }}
                      onClick={() => void closePosition(p.trade_id, p.symbol)}
                    >
                      Close position
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {tab === 'trades' && (
          <section>
            <div className="page-head">
              <div>
                <h1>Trades</h1>
                <p>Every simulated fill, newest first. Charges include brokerage, STT and slippage.</p>
              </div>
              <input
                className="input"
                placeholder="Filter symbol or exit reason"
                value={tradeFilter}
                onChange={(e) => setTradeFilter(e.target.value)}
              />
            </div>
            <div className="card">
              <TradesTable trades={trades} filter={tradeFilter} />
            </div>
          </section>
        )}

        {tab === 'portfolio' && <GrowwPortfolio />}

        {tab === 'settings' && (
          <SettingsPanel
            status={status}
            onChanged={() => {
              setCalTick((prev) => prev + 1)
              void loadCore()
              void loadTrades()
            }}
          />
        )}
      </main>

      <SymbolDrawer row={selectedRow} onClose={() => setSelectedRow(null)} />
    </div>
  )
}
