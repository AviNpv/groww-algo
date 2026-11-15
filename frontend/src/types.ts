export interface Bucket {
  score: number
  max: number
  pct: number
}

export interface PatternHit {
  name: string
  direction: 'bullish' | 'bearish' | 'neutral' | string
  confidence: number
  note: string
}

export interface Quality {
  score: number
  label: string
  components: Record<string, number>
  fundamentals_used: boolean
  fundamentals?: Record<string, unknown>
}

export interface ScanRow {
  symbol: string
  price: number
  change_pct: number
  volume: number | null
  score: number
  status: string
  confirmations: number
  buckets: Record<string, Bucket>
  patterns: PatternHit[]
  support: number | null
  resistance: number | null
  breakout: boolean
  quality: Quality
  indicators: Record<string, number>
  reasons: string[]
  atr?: number | null
  fundamentals?: Record<string, unknown> | null
}

export interface Signal {
  symbol: string
  action: string
  price?: number
  stop?: number
  target?: number
  qty?: number
  score?: number
  status?: string
  reason?: string
  rejections?: string[]
  buckets?: Record<string, Bucket>
  patterns?: PatternHit[]
  executed?: { price: number; qty: number; trade_id: number }
}

export interface ScanResponse {
  ts: string | null
  source: string | null
  scanned: number | null
  eligible?: number | null
  qualified: number | null
  watchlist: number | null
  duration_ms: number | null
  results: ScanRow[]
  signals: Signal[]
  trade_errors: string[]
  errors: string[]
}

export interface Overview {
  equity: number
  cash: number
  market_value: number
  starting_capital: number
  total_pnl: number
  total_return_pct: number
  today_pnl: number
  today_pnl_pct: number
  realized_pnl: number
  unrealized_pnl: number
  open_positions: number
  closed_trades: number
  win_rate: number
  profit_factor: number
  avg_win: number
  avg_loss: number
  best_trade: number
  worst_trade: number
  max_drawdown_pct: number
  best_day: number
  worst_day: number
  green_days: number
  red_days: number
  sessions: number
  day_date: string
  day_start_equity: number
  demo: boolean
  mode: string
}

export interface CalendarDay {
  day: string
  equity: number
  cash: number
  market_value: number
  realized_pnl: number
  unrealized_pnl: number
  trades: number
  wins: number
  losses: number
  notes?: string | null
  demo: number
  pnl: number
  pnl_pct: number
}

export interface CalendarResponse {
  year: number
  month: number
  days: CalendarDay[]
  month_pnl: number
  sessions: number
  green: number
  red: number
  demo: boolean
}

export interface EquityPoint {
  day: string
  equity: number
  realized_pnl: number
  trades: number
}

export interface Position {
  trade_id: number
  symbol: string
  qty: number
  avg_price: number
  stop: number | null
  target: number | null
  initial_risk: number | null
  highest_price: number | null
  last_price: number
  opened_at: string
  score: number | null
  reason: string | null
  value: number
  unrealized: number
  unrealized_pct: number
  r_multiple: number | null
}

export interface PositionsResponse {
  positions: Position[]
  cash: number
  equity: number
}

export interface Trade {
  id: number
  symbol: string
  qty: number
  entry_price: number
  exit_price: number | null
  stop: number | null
  target: number | null
  opened_at: string
  closed_at: string | null
  pnl: number | null
  charges: number
  score: number | null
  exit_reason: string | null
  reason: string | null
  mode: string
  demo: number
}

export interface Status {
  state: string
  mode: string
  demo: boolean
  market_open: boolean
  entry_window: boolean
  now_ist: string
  last_scan_at: string | null
  next_scan_at: string | null
  last_cycle_at: string | null
  position_count: number
  equity: number
  data_source: {
    active: string
    groww_ok: boolean
    state: string
    message: string
    fallback_reason: string | null
  }
  last_error: string | null
  config: {
    mode: string
    capital: number
    risk_per_trade: number
    max_positions: number
    max_daily_loss: number
    min_score: number
    scan_interval_sec: number
    universe_size: number
  }
}

export interface ConfigResponse {
  config: Record<string, unknown>
  yaml: string
  path?: string
}

export interface ConfigSaveResponse {
  ok: boolean
  config: Record<string, unknown>
  yaml: string
  message: string
}

export interface CredentialsStatus {
  api_key_present: boolean
  api_key_hint: string
  totp_secret_present: boolean
  api_secret_present: boolean
  auth_mode: 'approval' | 'totp' | 'none'
  needs_daily_approval: boolean
  token_cached: boolean
  token_expiry: string | null
  obtained_at: string | null
  state: string
  message: string
}

export interface FeedHealth {
  ok: boolean
  active?: string
  stage?: string | null
  state?: string
  message?: string
  fallback_reason?: string | null
  auth?: CredentialsStatus
  user?: { ucc?: string; active_segments?: string[] }
}

export interface CredentialsResponse {
  status: CredentialsStatus
  health: FeedHealth
  env_path: string
  ok?: boolean
  message?: string
}

export interface ResetChallenge {
  challenge_id: string
  prompt: string
  expires_in: number
}

export interface ResetResponse {
  ok: boolean
  scope: string
  starting_capital: number
  cache_files_cleared: number
  message: string
  status: Status
  overview: Overview
}

export interface ConfigParseResponse {
  ok: boolean
  config: Record<string, unknown>
}

export interface BacktestSummary {
  starting_capital: number
  ending_equity: number
  total_pnl: number
  return_pct: number
  trades: number
  wins: number
  losses: number
  win_rate: number
  profit_factor: number
  avg_win: number
  avg_loss: number
  best_trade: number
  worst_trade: number
  expectancy: number
  max_drawdown_pct: number
  green_days: number
  red_days: number
  flat_days: number
  best_day: number
  worst_day: number
}

export interface BacktestDay {
  day: string
  equity: number
  pnl: number
  trades: number
  wins: number
  losses: number
  qualified: number
}

export interface BacktestTrade {
  day: string
  symbol: string
  qty: number
  entry: number
  exit: number
  stop: number
  target: number
  pnl: number
  reason: string
  score: number | null
  r_multiple: number
}

export interface PortfolioHolding {
  symbol: string
  isin: string | null
  quantity: number
  average_price: number
  free_quantity: number
  t1_quantity: number
  pledged_quantity: number
  locked_quantity: number
  exchanges: string[]
  price: number | null
  price_source: string | null
  delayed: boolean
  value: number | null
  pnl: number | null
  pnl_pct: number | null
  day_change: number | null
  day_pnl: number | null
  weight_pct: number | null
}

export interface PortfolioPosition {
  symbol: string
  exchange: string | null
  product: string | null
  quantity: number
  net_price: number
  realised_pnl: number
  isin: string | null
  price: number | null
  price_source: string | null
  delayed: boolean
  value: number
  unrealised_pnl: number | null
  unrealised_pct: number | null
}

export interface PortfolioOrder {
  order_id: string | null
  symbol: string | null
  status: string | null
  side: string | null
  quantity: number
  filled: number
  price: number
  average_fill_price: number
  order_type: string | null
  product: string | null
  exchange: string | null
  created_at: string | null
}

export interface PortfolioSummary {
  holdings_count: number
  positions_count: number
  orders_count: number
  invested: number
  current_value: number
  total_pnl: number
  total_pnl_pct: number
  day_pnl: number
  unrealised_pnl: number
  missing_prices: string[]
  priced_by: string[]
}

export interface PortfolioFeed {
  preference: string
  active: string
  ok: boolean
  portfolio_ok: boolean
  live_data: boolean
  state: string
  message: string
  fallback_reason: string | null
  checked_at: string
}

export interface PortfolioResponse {
  ok: boolean
  errors: Record<string, string>
  account: {
    ucc: string | null
    segments: string[]
    nse_enabled: boolean
    bse_enabled: boolean
  } | null
  holdings: PortfolioHolding[]
  positions: PortfolioPosition[]
  orders: PortfolioOrder[]
  summary: PortfolioSummary
  auth: {
    state: string
    message: string
    auth_mode: string
    token_cached: boolean
    token_expiry: string | null
    needs_daily_approval: boolean
    api_key_hint: string
  }
  feed: PortfolioFeed
  as_of: string
}
