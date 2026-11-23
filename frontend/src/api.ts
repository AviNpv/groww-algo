// API base URL configuration
// Support both VITE_API_BASE_URL (standard Vite) and API_BASE_URL (custom)
// The value is embedded in the frontend bundle - this is unavoidable since
// the frontend must know the backend URL to make API calls.
// Set this to: https://groww-algo.onrender.com (your Render backend)
// In development (local), if not set, defaults to 127.0.0.1:8000
// 
// To keep the value "private" from casual browser inspection,
// use VITE_API_BASE_URL (standard Vite prefix) - values with VITE_ prefix
// are injected into the browser bundle but are not surfaced automatically
// in the UI. Set this in your dashboard: Netlify → Site Settings → Env Vars
const API_BASE = import.meta.env.VITE_API_BASE_URL || import.meta.env.API_BASE_URL || 'https://groww-algo.onrender.com'

import type {
  AgentChatSession,
  CalendarResponse,
  ChartResponse,
  ConfigParseResponse,
  ConfigResponse,
  ConfigSaveResponse,
  CredentialsResponse,
  EquityPoint,
  IntelligenceActionResult,
  IntelligenceDebate,
  IntelligenceDecisionRow,
  IntelligenceExecutionRow,
  IntelligenceHealth,
  IntelligenceList,
  IntelligenceStatus,
  IntelligenceUsageRow,
  IntelligenceVersionRow,
  LiveResponse,
  PortfolioResponse,
  Overview,
  PositionsResponse,
  ResetChallenge,
  ResetResponse,
  ScanResponse,
  ScanRow,
  Status,
  Trade,
} from './types'

// API prefix for all backend routes
const BACKEND_API_PREFIX = '/api'

// Request function that prepends API_BASE and BACKEND_API_PREFIX
// Each route should NOT include /api prefix (the frontend adds it consistently)
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${BACKEND_API_PREFIX}${path}`
  const response = await fetch(url, {
    ...init,
    // Never let the browser serve a cached snapshot on a live tick.
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    const text = await response.text()
    let detail = text
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') detail = parsed.detail
      else if (parsed.detail) detail = JSON.stringify(parsed.detail)
    } catch {
      /* keep the raw body */
    }
    throw new Error(detail.slice(0, 300) || `${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

/** Strategy-changing intelligence endpoints use the operator token when one is configured. */
function adminPost(path: string, token: string, body?: unknown): Promise<IntelligenceActionResult> {
  return request<IntelligenceActionResult>(path, {
    method: 'POST',
    headers: token ? { 'X-Intelligence-Token': token } : {},
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
}

export const api = {
  /** Single request used by the 1-second dashboard refresh. */
  live: (year: number, month: number) =>
    request<LiveResponse>(`/live?year=${year}&month=${month}`),
  status: () => request<Status>('/status'),
  overview: () => request<Overview>('/overview'),
  calendar: (year: number, month: number) =>
    request<CalendarResponse>(`/calendar?year=${year}&month=${month}`),
  equity: (limit = 400) =>
    request<{ curve: EquityPoint[]; demo: boolean }>(`/equity?limit=${limit}`),
  scan: () => request<ScanResponse>('/scan'),
  /** Candles + strategy levels/rules for one scanner candidate. */
  chart: (row: ScanRow, interval = '5m', days = 5) =>
    request<ChartResponse>('/chart', {
      method: 'POST',
      body: JSON.stringify({ symbol: row.symbol, interval, days, row }),
    }),
  runScan: (autotrade: boolean) =>
    request<ScanResponse>('/scan', {
      method: 'POST',
      body: JSON.stringify({ autotrade }),
    }),
  positions: () => request<PositionsResponse>('/positions'),
  trades: (limit = 200) => request<{ trades: Trade[] }>(`/trades?limit=${limit}`),
  closePosition: (tradeId: number) =>
    request<{ ok: boolean; pnl?: number }>(`/positions/${tradeId}/close`, { method: 'POST' }),
  startEngine: () => request<Status>('/engine/start', { method: 'POST' }),
  stopEngine: () => request<Status>('/engine/stop', { method: 'POST' }),
  config: () => request<ConfigResponse>('/config'),
  saveConfig: (yaml: string) =>
    request<ConfigSaveResponse>('/config', {
      method: 'PUT',
      body: JSON.stringify({ yaml }),
    }),
  setConfigValue: (section: string, key: string, value: number | boolean | string) =>
    request<ConfigSaveResponse>('/config/value', {
      method: 'POST',
      body: JSON.stringify({ section, key, value }),
    }),
  parseConfig: (yaml: string) =>
    request<ConfigParseResponse>('/config/parse', {
      method: 'POST',
      body: JSON.stringify({ yaml }),
    }),
  restoreConfig: () =>
    request<ConfigSaveResponse>('/config/default', { method: 'POST' }),
  growwPortfolio: (force = false) =>
    request<PortfolioResponse>(`/groww/portfolio${force ? '?force=true' : ''}`),
  credentials: () => request<CredentialsResponse>('/credentials'),
  saveCredentials: (body: { api_key?: string; api_secret?: string; totp_secret?: string }) =>
    request<CredentialsResponse>('/credentials', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  testCredentials: () =>
    request<CredentialsResponse>('/credentials/test', { method: 'POST' }),
  resetChallenge: () =>
    request<ResetChallenge>('/reset/challenge', { method: 'POST' }),
  resetAll: (body: { challenge_id: string; answer: string; scope: string }) =>
    request<ResetResponse>('/reset', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /* Runtime intelligence (separate read-only evidence store + explicit actions). */
  intelligenceStatus: () => request<IntelligenceStatus>('/intelligence/status'),
  intelligenceHealth: () => request<IntelligenceHealth>('/intelligence/health'),
  intelligenceDecisions: (limit = 20, symbol = '') =>
    request<IntelligenceList<IntelligenceDecisionRow>>(
      `/intelligence/decisions?limit=${limit}&symbol=${encodeURIComponent(symbol)}`,
    ),
  intelligenceDebate: (symbol: string) =>
    request<IntelligenceDebate>(`/intelligence/debate/${encodeURIComponent(symbol)}`),
  intelligenceUsage: (limit = 50) =>
    request<IntelligenceList<IntelligenceUsageRow>>(`/intelligence/usage?limit=${limit}`),
  intelligenceExecutions: (limit = 50) =>
    request<IntelligenceList<IntelligenceExecutionRow>>(`/intelligence/executions?limit=${limit}`),
  intelligenceVersions: (limit = 30) =>
    request<IntelligenceList<IntelligenceVersionRow>>(`/intelligence/versions?limit=${limit}`),
  /** Start one live advisory run; the six agents stream over SSE. */
  startAgentChat: (symbol: string) =>
    request<AgentChatSession>('/intelligence/chat/start', {
      method: 'POST',
      body: JSON.stringify({ symbol }),
    }),
  agentChatStreamUrl: (sessionId: string) =>
    `${API_BASE}${BACKEND_API_PREFIX}/intelligence/chat/${encodeURIComponent(sessionId)}/stream`,
  setIntelligenceMode: (enabled: boolean, token = '') =>
    adminPost('/intelligence/mode', token, { enabled }),
  runEvolution: (token = '') => adminPost('/intelligence/evolution/run', token),
  promoteStrategy: (versionId: string, token = '') =>
    adminPost(`/intelligence/strategy/${encodeURIComponent(versionId)}/promote`, token),
  rollbackStrategy: (token = '') => adminPost('/intelligence/strategy/rollback', token),
}

// Helper functions
export function money(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString('en-IN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function signed(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${money(value, digits)}`
}

/** Rupee amount with the sign in front of the symbol: +₹1,234.00 / -₹40.50 */
export function moneySigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : '-'}₹${money(Math.abs(value), digits)}`
}

/** Exact number with no display rounding; the backend already removed float noise. */
export function exact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString('en-IN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 8,
  })
}

/** Exact rupee P&L, e.g. -₹3.13 instead of a rounded -₹3. */
export function exactSigned(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : '-'}₹${exact(Math.abs(value))}`
}

export function exactPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${exact(value)}%`
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`
}

export function timeOnly(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })
}