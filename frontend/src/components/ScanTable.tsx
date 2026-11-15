import type { ScanRow } from '../types'
import { money, pct } from '../api'

interface Props {
  rows: ScanRow[]
  onSelect: (row: ScanRow) => void
}

const BUCKETS: Array<[string, string]> = [
  ['trend', 'Trend'],
  ['momentum', 'Mom'],
  ['pattern', 'Pat'],
  ['volume', 'Vol'],
  ['quality', 'Qual'],
]

function scoreClass(score: number): string {
  if (score >= 65) return ''
  if (score >= 55) return 'mid'
  return 'low'
}

export default function ScanTable({ rows, onSelect }: Props) {
  if (!rows.length) {
    return <div className="empty-state">No scan results yet. Run a scan from the Scanner tab.</div>
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Symbol</th>
            <th className="num">LTP</th>
            <th className="num">Chg</th>
            <th>Score</th>
            <th>Status</th>
            <th>Factors</th>
            <th>Patterns</th>
            <th className="num">Vol×</th>
            <th className="num">ATR%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={row.symbol} className="clickable" onClick={() => onSelect(row)}>
              <td className="num muted">{index + 1}</td>
              <td className="symbol">
                {row.symbol}
                <div className="tiny muted">{row.quality?.label ?? '—'}</div>
              </td>
              <td className="num">{money(row.price)}</td>
              <td className={`num ${row.change_pct >= 0 ? 'pos' : 'neg'}`}>{pct(row.change_pct)}</td>
              <td className="score-cell">
                <div className="score-top">
                  <b>{row.score.toFixed(0)}</b>
                  <span className="muted tiny">{row.confirmations}/5</span>
                </div>
                <div className="bar">
                  <div
                    className={`bar-fill ${scoreClass(row.score)}`}
                    style={{ width: `${Math.min(100, row.score)}%` }}
                  />
                </div>
              </td>
              <td>
                <span className={`status-pill ${row.status.toLowerCase()}`}>{row.status}</span>
              </td>
              <td>
                <div className="bucket-grid">
                  {BUCKETS.map(([key, label]) => {
                    const bucket = row.buckets?.[key]
                    return (
                      <div className="bucket" key={key} title={`${label}: ${bucket?.score ?? 0}/${bucket?.max ?? 0}`}>
                        <div className="bl">{label}</div>
                        <div className="bb">
                          <i style={{ width: `${Math.max(2, bucket?.pct ?? 0)}%` }} />
                        </div>
                      </div>
                    )
                  })}
                </div>
              </td>
              <td>
                {row.patterns.length === 0 && <span className="muted tiny">—</span>}
                {row.patterns.slice(0, 2).map((p) => (
                  <span className={`chip ${p.direction}`} key={p.name}>{p.name}</span>
                ))}
              </td>
              <td className="num">{row.indicators?.rel_vol?.toFixed(1) ?? '—'}</td>
              <td className="num">{row.indicators?.atr_pct?.toFixed(2) ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
