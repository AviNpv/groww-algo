import { useEffect, useMemo, useState } from 'react'

type ConfigMap = Record<string, unknown>
type Tone = 'blue' | 'yellow' | 'green' | 'red' | 'purple'
type RowTone = 'good' | 'bad' | 'muted' | 'accent'

interface Row {
  label: string
  value: string
  tone?: RowTone
}

interface Stage {
  id: string
  title: string
  kicker: string
  tone: Tone
  rows: Row[]
  weights?: { label: string; value: number }[]
}

interface Props {
  config: ConfigMap | null
  dirty?: boolean
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

const first = (node: Record<string, unknown>, ...keys: unknown[]): unknown => {
  for (const key of keys) {
    if (key === undefined || key === null) continue
    const value = node[String(key)]
    if (value !== undefined && value !== null) return value
  }
  return undefined
}

const str = (value: unknown, fallback = '—'): string => {
  if (value === undefined || value === null || value === '') return fallback
  if (Array.isArray(value)) return value.length ? value.join(' → ') : fallback
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

const num = (value: unknown, fallback = 0): number => {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

const enabled = (value: unknown): boolean => value === true || value === 'true' || value === 1 || value === '1'

const rupee = (value: unknown): string => `₹${num(value).toLocaleString('en-IN')}`

const entryWindow = (engine: Record<string, unknown>): string => {
  const start = first(engine, 'entry_start_time')
  const end = first(engine, 'entry_end_time')
  if (start !== undefined && end !== undefined) return `${str(start)} → ${str(end)}`
  const legacy = first(engine, 'entry_window')
  if (Array.isArray(legacy) && legacy.length) return `${str(legacy[0])} → ${str(legacy[legacy.length - 1])}`
  return '—'
}

function buildStages(config: ConfigMap | null): Stage[] {
  const app = asRecord(config?.app)
  const engine = asRecord(config?.engine)
  const data = asRecord(config?.data)
  const groww = asRecord(data.groww)
  const yahoo = asRecord(data.yahoo_finance)
  const validation = asRecord(data.validation)
  const account = asRecord(config?.account)
  const costs = asRecord(config?.costs)
  const universe = asRecord(config?.universe)
  const filters = asRecord(universe.filters)
  const scanner = asRecord(universe.scanner)
  const indicators = asRecord(config?.indicators)
  const market = asRecord(config?.market_filter)
  const entry = asRecord(config?.entry)
  const entryTrend = asRecord(entry.trend)
  const entryMomentum = asRecord(entry.momentum)
  const entryVolume = asRecord(entry.volume)
  const entryAction = asRecord(entry.price_action)
  const short = asRecord(config?.short_entry)
  const shortTrend = asRecord(short.trend)
  const exit = asRecord(config?.exit)
  const initialStop = asRecord(exit.initial_stop_loss)
  const target = asRecord(exit.target)
  const timeStop = asRecord(exit.time_stop)
  const squareOff = asRecord(exit.square_off)
  const scoring = asRecord(config?.scoring)
  const thresholds = asRecord(scoring.thresholds)
  const weights = asRecord(scoring.weights)
  const risk = asRecord(config?.risk)
  const safety = asRecord(config?.safety)

  const weightRows = Object.entries(weights)
    .map(([label, value]) => ({ label: label.replace(/_/g, ' '), value: num(value) }))
    .sort((a, b) => b.value - a.value)

  return [
    {
      id: 'data',
      title: 'Data feed',
      kicker: `${str(first(data, 'primary_source'))} → ${str(first(data, 'secondary_source'))}`,
      tone: 'blue',
      rows: [
        { label: 'Mode', value: str(first(data, 'source_mode')) },
        { label: 'Groww', value: enabled(groww.use_for_entry_confirmation) ? 'entry confirmation' : 'display only', tone: enabled(groww.use_for_entry_confirmation) ? 'good' : 'muted' },
        { label: 'Yahoo', value: enabled(yahoo.use_for_scanning) ? 'scanning only' : 'disabled', tone: enabled(yahoo.use_for_scanning) ? 'accent' : 'muted' },
        { label: 'Quote TTL', value: `${num(first(engine, 'quote_refresh_seconds', first(engine, 'quote_refresh_sec')))}s` },
        { label: 'Max quote age', value: `${num(validation.max_quote_age_seconds)}s`, tone: 'good' },
        { label: 'Reject bad data', value: enabled(validation.reject_invalid_prices) ? 'stale / OHLC / volume' : 'off', tone: enabled(validation.reject_invalid_prices) ? 'good' : 'bad' },
      ],
    },
    {
      id: 'universe',
      title: 'Universe',
      kicker: str(first(universe, 'symbols_file', universe.file)),
      tone: 'blue',
      rows: [
        { label: 'Source', value: str(universe.source) },
        { label: 'Max symbols', value: str(num(universe.max_symbols)) },
        { label: 'Min price', value: rupee(filters.minimum_price) },
        { label: 'Min daily value', value: `₹${(num(filters.minimum_average_daily_value) / 1e7).toFixed(1)} Cr` },
        { label: 'Candidates', value: `max ${num(scanner.max_candidates)}`, tone: 'accent' },
        { label: 'Filters', value: `SME ${enabled(filters.exclude_sme) ? 'out' : 'in'} · ETF ${enabled(filters.exclude_etf) ? 'out' : 'in'}` },
      ],
    },
    {
      id: 'engine',
      title: 'Engine',
      kicker: entryWindow(engine),
      tone: 'yellow',
      rows: [
        { label: 'Version', value: `V${str(app.version)} · ${str(app.mode)}` },
        { label: 'Auto-trade', value: enabled(engine.auto_trade) ? 'ON' : 'OFF', tone: enabled(engine.auto_trade) ? 'good' : 'muted' },
        { label: 'Scan interval', value: `${num(first(engine, 'scan_interval_seconds', first(engine, 'scan_interval_sec')))}s` },
        { label: 'Square-off', value: str(first(engine, 'square_off_time', first(engine, 'square_off'))) },
        { label: 'Closed candles', value: enabled(engine.use_closed_candles_only) ? 'only' : 'forming allowed', tone: enabled(engine.use_closed_candles_only) ? 'good' : 'bad' },
        { label: 'Long / short', value: `${enabled(engine.allow_long) ? 'L' : '—'} / ${enabled(engine.allow_short) ? 'S' : '—'}` },
      ],
    },
    {
      id: 'timeframes',
      title: 'Timeframes',
      kicker: `${str(indicators.execution_timeframe)} · ${str(indicators.trend_timeframe)}`,
      tone: 'blue',
      rows: [
        { label: 'Execution', value: str(indicators.execution_timeframe) },
        { label: 'Trend', value: str(indicators.trend_timeframe) },
        { label: 'Context', value: str(indicators.context_timeframe) },
        { label: 'EMA periods', value: str(indicators.ema && asRecord(indicators.ema).periods) },
        { label: 'Breakout', value: `${num(asRecord(indicators.breakout).lookback)} bars` },
        { label: 'Volume lookback', value: `${num(asRecord(indicators.volume).lookback)} bars` },
      ],
    },
    {
      id: 'market',
      title: 'Market filter',
      kicker: `${str(market.benchmark)} · regime gate`,
      tone: 'yellow',
      rows: [
        { label: 'Status', value: enabled(market.enabled) ? 'ENABLED' : 'DISABLED', tone: enabled(market.enabled) ? 'good' : 'muted' },
        { label: 'Market direction', value: enabled(market.use_market_direction) ? 'required' : 'off' },
        { label: 'Sector strength', value: enabled(market.use_sector_strength) ? 'required' : 'off' },
        { label: 'Long gate', value: enabled(asRecord(market.long).require_bullish_market) ? 'bullish market' : 'off' },
        { label: 'Short gate', value: enabled(asRecord(market.short).require_bearish_market) ? 'bearish market' : 'off' },
        { label: 'Missing data', value: enabled(market.reject_if_market_data_missing) ? 'reject entries' : 'allow', tone: enabled(market.reject_if_market_data_missing) ? 'good' : 'bad' },
      ],
    },
    {
      id: 'score',
      title: 'Composite score',
      kicker: `${num(first(scoring, 'maximum_score', scoring.max_score))}-point ranking`,
      tone: 'purple',
      weights: weightRows,
      rows: [
        { label: 'Ignore below', value: str(thresholds.ignore) },
        { label: 'Watchlist', value: str(thresholds.watchlist) },
        { label: 'Qualified', value: str(thresholds.qualified) },
        { label: 'Entry', value: str(thresholds.entry), tone: 'accent' },
        { label: 'Excellent', value: str(thresholds.excellent) },
        { label: 'Long / short split', value: enabled(scoring.separate_long_short_scoring) ? 'yes' : 'no' },
      ],
    },
    {
      id: 'entry',
      title: 'Long entry',
      kicker: `Min ${str(first(entry, 'minimum_score', entry.min_score))} · ${str(first(entry, 'required_confirmations', entry.min_confirmations))} confirms`,
      tone: 'green',
      rows: [
        { label: 'Direction', value: str(entry.direction) },
        { label: 'Score', value: `≥ ${str(first(entry, 'minimum_score', entry.min_score))}`, tone: 'accent' },
        { label: 'Confirmations', value: `≥ ${str(first(entry, 'required_confirmations', entry.min_confirmations))}` },
        { label: 'EMA stack', value: enabled(entryTrend.ema_alignment) ? str(entryTrend.condition) : 'off' },
        { label: 'Price / VWAP', value: enabled(entryTrend.price_above_vwap) ? 'above required' : 'off' },
        { label: 'RSI band', value: `${num(entryMomentum.rsi_min)} – ${num(entryMomentum.rsi_max)} · ADX ≥ ${num(entryMomentum.adx_min)}` },
        { label: 'Volume', value: `≥ ${num(entryVolume.relative_volume_min)}×` },
        { label: 'Pattern', value: enabled(entryAction.require_breakout_or_retest) ? 'breakout / retest + candle' : 'any' },
        { label: 'ATR band', value: `${num(asRecord(entry.volatility).minimum_atr_pct)}% – ${num(asRecord(entry.volatility).maximum_atr_pct)}%` },
        { label: 'Min R:R', value: `${num(asRecord(entry.risk).minimum_risk_reward)}R`, tone: 'good' },
      ],
    },
    {
      id: 'short',
      title: 'Short entry',
      kicker: `Min ${str(first(short, 'minimum_score', short.min_score))} · ${str(first(short, 'required_confirmations', short.min_confirmations))} confirms`,
      tone: 'green',
      rows: [
        { label: 'Direction', value: str(short.direction) },
        { label: 'Score', value: `≥ ${str(first(short, 'minimum_score', short.min_score))}`, tone: 'accent' },
        { label: 'EMA stack', value: enabled(shortTrend.ema_alignment) ? str(shortTrend.condition) : 'off' },
        { label: 'Price / VWAP', value: enabled(shortTrend.price_below_vwap) ? 'below required' : 'off' },
        { label: 'RSI band', value: `${num(asRecord(short.momentum).rsi_min)} – ${num(asRecord(short.momentum).rsi_max)}` },
        { label: 'Volume', value: `≥ ${num(asRecord(short.volume).relative_volume_min)}×` },
        { label: 'Pattern', value: enabled(asRecord(short.price_action).require_breakdown_or_retest) ? 'breakdown / retest + candle' : 'any' },
        { label: 'Min R:R', value: `${num(asRecord(short.risk).minimum_risk_reward)}R` },
      ],
    },
    {
      id: 'risk',
      title: 'Risk & capital',
      kicker: `${num(first(risk, 'maximum_trades_per_day', account.max_trades_per_day))} trades/day`,
      tone: 'green',
      rows: [
        { label: 'Capital', value: rupee(first(account, 'initial_capital', account.starting_capital)) },
        { label: 'Intraday leverage', value: `${num(first(account, 'intraday_leverage', account.leverage) ?? 5)}× · margin 1/${num(first(account, 'intraday_leverage', account.leverage) ?? 5)}`, tone: 'accent' },
        { label: 'Positions', value: `max ${num(first(risk, 'maximum_open_positions', account.max_positions))}` },
        { label: 'Risk / trade', value: rupee(first(risk, 'risk_per_trade', account.risk_per_trade)) },
        { label: 'Daily loss cap', value: rupee(first(risk, 'maximum_daily_loss', account.max_daily_loss)), tone: 'bad' },
        { label: 'Max position', value: `${num(account.max_position_pct)}%` },
        { label: 'Order minimum', value: rupee(account.minimum_order_value) },
        { label: 'Cooldown', value: `${num(asRecord(risk.cooldown).after_loss_minutes)} min after loss`, tone: 'accent' },
        { label: 'Brokerage', value: `${num(asRecord(costs.brokerage).percentage)}% · cap ₹${num(asRecord(costs.brokerage).per_order)}` },
      ],
    },
    {
      id: 'exit',
      title: 'Exit rules',
      kicker: str(first(squareOff, 'time', exit.square_off)),
      tone: 'red',
      rows: [
        { label: 'Initial stop', value: `${num(initialStop.atr_multiplier)}× ATR · ${num(initialStop.minimum_stop_pct)}%–${num(initialStop.maximum_stop_pct)}%`, tone: 'bad' },
        { label: 'Target', value: `${num(target.target_r_mult)}R`, tone: 'good' },
        { label: 'Break-even', value: `${num(asRecord(exit.breakeven).activate_at_r)}R` },
        { label: 'Trailing', value: `${num(asRecord(exit.trailing_stop).activate_at_r)}R · ATR + profit lock` },
        { label: 'Time stop', value: `${num(timeStop.maximum_holding_minutes)} min` },
        { label: 'Square-off', value: `${str(squareOff.time)} · all positions`, tone: 'accent' },
        { label: 'Overnight', value: enabled(asRecord(exit.overnight).enabled) ? 'allowed' : 'never', tone: enabled(asRecord(exit.overnight).enabled) ? 'bad' : 'good' },
        { label: 'Safety', value: enabled(safety.paper_trading_only) ? 'paper only · live blocked' : 'live allowed', tone: enabled(safety.paper_trading_only) ? 'good' : 'bad' },
      ],
    },
  ]
}

export default function StrategyFlowChart({ config, dirty }: Props) {
  const stages = useMemo(() => buildStages(config), [config])
  const [active, setActive] = useState(0)
  const [pinned, setPinned] = useState<number | null>(null)
  const [playing, setPlaying] = useState(
    () => typeof window === 'undefined' || !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  )

  useEffect(() => {
    if (!playing || pinned !== null || stages.length === 0) return
    const timer = window.setInterval(() => setActive((value) => (value + 1) % stages.length), 1700)
    return () => window.clearInterval(timer)
  }, [playing, pinned, stages.length])

  const selected = pinned ?? active
  const stage = stages[selected] ?? stages[0]
  const flowing = playing && pinned === null

  return (
    <div className={`card sf-card ${flowing ? 'sf-flowing' : ''}`}>
      <div className="card-head">
        <h2>Strategy flow</h2>
        <div className="sf-head-actions">
          {dirty && <span className="sf-badge">draft preview</span>}
          <span className="sub">Live from strategy.yaml</span>
          <button
            type="button"
            className="sf-btn"
            onClick={() => setPlaying((value) => !value)}
            aria-label={playing ? 'Pause animation' : 'Play animation'}
          >
            {playing ? '❚❚ Pause' : '▶ Play'}
          </button>
        </div>
      </div>
      <div className="card-body">
        {!config ? (
          <div className="empty-state">Loading strategy…</div>
        ) : (
          <>
            <div className="sf-flow">
              {stages.map((item, index) => (
                <div className="sf-unit" key={item.id}>
                  <button
                    type="button"
                    className={`sf-stage tone-${item.tone} ${index === selected ? 'active' : ''}`}
                    onClick={() => setPinned((value) => (value === index ? null : index))}
                    title={`${item.title} — click to pin details`}
                  >
                    <span className="sf-step">STEP {index + 1}</span>
                    <span className="sf-title">{item.title}</span>
                    <span className="sf-kicker">{item.kicker}</span>
                    {item.rows.slice(0, 2).map((row) => (
                      <span className="sf-kv" key={row.label}>
                        <span>{row.label}</span>
                        <b className={row.tone ? `t-${row.tone}` : ''}>{row.value}</b>
                      </span>
                    ))}
                    {item.weights && (
                      <span className="sf-weights">
                        {item.weights.map((weight) => (
                          <span className="sf-weight" key={weight.label}>
                            <span>{weight.label}</span>
                            <span className="sf-weight-bar"><i style={{ width: `${weight.value}%` }} /></span>
                            <b>{weight.value}</b>
                          </span>
                        ))}
                      </span>
                    )}
                  </button>
                  {index < stages.length - 1 && (
                    <span className="sf-link" aria-hidden="true">
                      <i style={{ animationDelay: `${index * 0.18}s` }} />
                    </span>
                  )}
                </div>
              ))}
            </div>

            <div className={`sf-detail tone-${stage.tone}`}>
              <div className="sf-detail-head">
                <b>{stage.title}</b>
                <span className="sf-hint">
                  {pinned !== null ? 'Pinned — click the stage again to resume' : 'Following the live signal · click any stage to pin it'}
                </span>
              </div>
              <div className="sf-detail-grid">
                {stage.rows.map((row) => (
                  <span className="sf-kv" key={row.label}>
                    <span>{row.label}</span>
                    <b className={row.tone ? `t-${row.tone}` : ''}>{row.value}</b>
                  </span>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
