import { useCallback, useEffect, useMemo, useState } from 'react'
import type { PortfolioResponse } from '../types'
import { api, money, moneySigned, pct } from '../api'

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function qty(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

function when(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

export default function GrowwPortfolio() {
  const [data, setData] = useState<PortfolioResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (force: boolean) => {
    setLoading(true)
    setError(null)
    try {
      const next = await api.growwPortfolio(force)
      setData(next)
    } catch (err) {
      setError(errText(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(false)
  }, [load])

  const account = data?.account ?? null
  const auth = data?.auth ?? null
  const feed = data?.feed ?? null
  const summary = data?.summary ?? null
  const holdings = data?.holdings ?? []
  const positions = data?.positions ?? []
  const orders = data?.orders ?? []

  const priceLine = useMemo(() => {
    if (!summary?.priced_by?.length) return 'no price feed'
    return summary.priced_by.map((source) => (source === 'groww' ? 'Groww live' : 'Yahoo delayed')).join(' + ')
  }, [summary])

  const errors = Object.entries(data?.errors ?? {})

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Groww portfolio</h1>
          <p>
            Your real Groww demat holdings, intraday positions and order book, read live from the
            Groww Trade API. This build is read-only — it never places an order.
          </p>
        </div>
        <div className="toolbar">
          <button className="btn ghost" disabled={loading} onClick={() => void load(true)}>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && <div className="error-box">{error}</div>}
      {errors.length > 0 && (
        <div className="banner">
          <b>PARTIAL</b> — {errors.map(([key, value]) => `${key}: ${value}`).join(' · ')}
        </div>
      )}

      <div className="conn-strip">
        <div className={`conn-chip ${auth?.token_cached ? 'ok' : 'bad'}`}>
          <span className="dot" />
          <b>Token</b>
          <span>{auth?.token_cached ? `valid to ${when(auth.token_expiry)}` : auth?.state ?? 'missing'}</span>
        </div>
        <div className={`conn-chip ${feed?.portfolio_ok ? 'ok' : 'bad'}`}>
          <span className="dot" />
          <b>Portfolio API</b>
          <span>{feed?.portfolio_ok ? 'holdings reachable' : 'not authorised'}</span>
        </div>
        <div className={`conn-chip ${feed?.ok ? 'ok' : 'warn'}`}>
          <span className="dot" />
          <b>Market data</b>
          <span>{feed?.ok ? 'Groww live quotes' : 'blocked by Groww (403)'}</span>
        </div>
        <div className="conn-chip plain">
          <b>Preference</b>
          <span>{feed?.preference ?? 'auto'}</span>
        </div>
      </div>

      {!feed?.ok && (
        <div className="inline-note">
          Your Groww key is connected and the portfolio endpoints work. Live quotes and candles
          return <code>403 Access forbidden</code> because market data is not enabled on this API key
          (separate from trading permissions). Until it is, holdings are valued with Yahoo's ~15-minute
          delayed prices. The bot itself keeps running on the fallback feed.
        </div>
      )}

      {summary && (
        <div className="kpi-grid">
          <div className="kpi">
            <div className="kpi-label">Invested</div>
            <div className="kpi-value">₹{money(summary.invested, 0)}</div>
            <div className="kpi-sub">{summary.holdings_count} holdings</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Current value</div>
            <div className="kpi-value">₹{money(summary.current_value, 0)}</div>
            <div className="kpi-sub">priced by {priceLine}</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Total P&amp;L</div>
            <div className={`kpi-value ${summary.total_pnl >= 0 ? 'pos' : 'neg'}`}>
              {moneySigned(summary.total_pnl)}
            </div>
            <div className="kpi-sub">{pct(summary.total_pnl_pct)} on cost</div>
          </div>
          <div className="kpi">
            <div className="kpi-label">Today</div>
            <div className={`kpi-value ${summary.day_pnl >= 0 ? 'pos' : 'neg'}`}>
              {moneySigned(summary.day_pnl)}
            </div>
            <div className="kpi-sub">as of {when(data?.as_of)}</div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h2>Holdings</h2>
          <span className="sub">
            {account?.ucc ? `UCC ${account.ucc} · ` : ''}
            {summary?.holdings_count ?? 0} scrips · {account?.segments?.join(' · ') || '—'}
          </span>
        </div>
        {holdings.length === 0 ? (
          <div className="empty-state">
            {loading ? 'Reading holdings from Groww…' : 'No demat holdings on this Groww account.'}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th className="num">Qty</th>
                  <th className="num">Avg cost</th>
                  <th className="num">LTP</th>
                  <th className="num">Value</th>
                  <th className="num">P&amp;L</th>
                  <th className="num">Day</th>
                  <th className="num">Weight</th>
                </tr>
              </thead>
              <tbody>
                {holdings.map((row) => (
                  <tr key={row.symbol}>
                    <td className="symbol">
                      {row.symbol}
                      {row.free_quantity !== row.quantity && (
                        <span className="chip neutral" style={{ marginLeft: 6 }}>
                          {qty(row.free_quantity)} free
                        </span>
                      )}
                    </td>
                    <td className="num">{qty(row.quantity)}</td>
                    <td className="num">₹{money(row.average_price)}</td>
                    <td className={`num ${row.delayed ? 'muted' : ''}`}>
                      {row.price === null ? '—' : `₹${money(row.price)}`}
                    </td>
                    <td className="num">{row.value === null ? '—' : `₹${money(row.value)}`}</td>
                    <td className={`num ${(row.pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                      {row.pnl === null ? '—' : moneySigned(row.pnl)}
                      {row.pnl_pct !== null && (
                        <span className="tiny muted"> ({pct(row.pnl_pct)})</span>
                      )}
                    </td>
                    <td className={`num ${(row.day_pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                      {row.day_pnl === null ? '—' : moneySigned(row.day_pnl)}
                    </td>
                    <td className="num muted">
                      {row.weight_pct === null ? '—' : `${row.weight_pct.toFixed(1)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {summary && summary.missing_prices.length > 0 && (
          <div className="card-body tiny muted">
            No price for {summary.missing_prices.join(', ')} — excluded from the valuation.
          </div>
        )}
      </div>

      {positions.length > 0 && (
        <div className="card">
          <div className="card-head">
            <h2>Open positions</h2>
            <span className="sub">Intraday / carry-forward, from /v1/positions/user</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Product</th>
                  <th className="num">Qty</th>
                  <th className="num">Net price</th>
                  <th className="num">LTP</th>
                  <th className="num">Unrealised</th>
                  <th className="num">Realised</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((row, index) => (
                  <tr key={`${row.symbol}-${row.product}-${index}`}>
                    <td className="symbol">{row.symbol}</td>
                    <td>{row.product ?? '—'}</td>
                    <td className="num">{qty(row.quantity)}</td>
                    <td className="num">₹{money(row.net_price)}</td>
                    <td className="num">{row.price === null ? '—' : `₹${money(row.price)}`}</td>
                    <td className={`num ${(row.unrealised_pnl ?? 0) >= 0 ? 'pos' : 'neg'}`}>
                      {row.unrealised_pnl === null ? '—' : moneySigned(row.unrealised_pnl)}
                    </td>
                    <td className={`num ${row.realised_pnl >= 0 ? 'pos' : 'neg'}`}>
                      {moneySigned(row.realised_pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head">
          <h2>Order book</h2>
          <span className="sub">
            {summary?.orders_count ?? 0} order{(summary?.orders_count ?? 0) === 1 ? '' : 's'} today
          </span>
        </div>
        {orders.length === 0 ? (
          <div className="empty-state">
            No orders placed through the API today. The algo is paper-only, so this stays empty
            unless you trade manually or connect a real order path.
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th className="num">Qty</th>
                  <th className="num">Filled</th>
                  <th className="num">Avg fill</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((row, index) => (
                  <tr key={row.order_id ?? index}>
                    <td className="muted">{when(row.created_at)}</td>
                    <td className="symbol">{row.symbol ?? '—'}</td>
                    <td>{row.side ?? '—'}</td>
                    <td className="num">{qty(row.quantity)}</td>
                    <td className="num">{qty(row.filled)}</td>
                    <td className="num">₹{money(row.average_fill_price)}</td>
                    <td>{row.status ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="footer-note">
        Holdings, positions and orders come straight from Groww. Set{' '}
        <code>engine.data_source</code> in Settings to <code>groww</code> if you want the bot to
        refuse Yahoo entirely — it will then stay idle while Groww market data is blocked.
      </div>
    </section>
  )
}
