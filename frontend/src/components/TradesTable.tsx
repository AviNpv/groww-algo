import type { Trade } from '../types'
import { money, moneySigned } from '../api'

interface Props {
  trades: Trade[]
  filter: string
}

function fmt(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

export default function TradesTable({ trades, filter }: Props) {
  const needle = filter.trim().toUpperCase()
  const rows = needle
    ? trades.filter((t) => t.symbol.includes(needle) || (t.exit_reason ?? '').toUpperCase().includes(needle))
    : trades

  if (!rows.length) {
    return <div className="empty-state">No trades match this filter.</div>
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th className="num">Qty</th>
            <th className="num">Entry</th>
            <th className="num">Exit</th>
            <th className="num">P&amp;L</th>
            <th className="num">Charges</th>
            <th className="num">Score</th>
            <th>Opened</th>
            <th>Closed</th>
            <th>Exit reason</th>
            <th>Tag</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t) => {
            const pnl = t.pnl
            const open = t.closed_at === null
            return (
              <tr key={t.id}>
                <td className="symbol">{t.symbol}</td>
                <td className="num">{t.qty}</td>
                <td className="num">₹{money(t.entry_price)}</td>
                <td className="num">{t.exit_price ? `₹${money(t.exit_price)}` : '—'}</td>
                <td className={`num ${pnl === null ? 'muted' : pnl >= 0 ? 'pos' : 'neg'}`}>
                  {open ? 'OPEN' : moneySigned(pnl)}
                </td>
                <td className="num muted">₹{money(t.charges, 0)}</td>
                <td className="num">{t.score ? t.score.toFixed(0) : '—'}</td>
                <td className="muted">{fmt(t.opened_at)}</td>
                <td className="muted">{fmt(t.closed_at)}</td>
                <td>
                  {t.exit_reason
                    ? <span className={`chip ${t.exit_reason === 'TARGET' ? 'bull' : t.exit_reason.includes('STOP') ? 'bear' : 'neutral'}`}>{t.exit_reason}</span>
                    : <span className="muted tiny">—</span>}
                </td>
                <td className="tiny muted">{t.demo ? 'DEMO' : t.mode.toUpperCase()}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
