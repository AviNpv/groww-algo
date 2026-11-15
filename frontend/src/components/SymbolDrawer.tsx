import { useEffect } from 'react'
import type { ScanRow } from '../types'
import { money, pct } from '../api'

interface Props {
  row: ScanRow | null
  onClose: () => void
}

const INDICATOR_LABELS: Array<[string, string, string]> = [
  ['rsi', 'RSI', ''],
  ['adx', 'ADX', ''],
  ['rel_vol', 'Rel volume', 'x'],
  ['atr_pct', 'ATR', '%'],
  ['vwap', 'VWAP', '₹'],
  ['ema20', 'EMA20', '₹'],
  ['ema50', 'EMA50', '₹'],
  ['ret5', '5-day return', '%'],
  ['pos_52w', '52w range', '%'],
]

export default function SymbolDrawer({ row, onClose }: Props) {
  useEffect(() => {
    if (!row) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [row, onClose])

  if (!row) return null

  const atr = row.atr ?? (row.price * (row.indicators?.atr_pct ?? 1)) / 100
  const stopDist = Math.max(row.price * 0.004, Math.min(row.price * 0.02, atr * 0.6))
  const stop = row.price - stopDist
  const target = row.price + stopDist * 2

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h2>{row.symbol}</h2>
            <div className="sub">
              ₹{money(row.price)} ·{' '}
              <span className={row.change_pct >= 0 ? 'pos' : 'neg'}>{pct(row.change_pct)}</span> ·{' '}
              <span className={`status-pill ${row.status.toLowerCase()}`}>{row.status}</span>
            </div>
          </div>
          <button className="btn small ghost" onClick={onClose}>Close</button>
        </div>

        <div className="detail-grid">
          <div className="detail-item">
            <div className="l">Composite score</div>
            <div className="v">{row.score.toFixed(1)}/100</div>
          </div>
          <div className="detail-item">
            <div className="l">Confirmations</div>
            <div className="v">{row.confirmations}/5</div>
          </div>
          <div className="detail-item">
            <div className="l">Quality</div>
            <div className="v">{row.quality?.score?.toFixed(0) ?? '—'}</div>
          </div>
          <div className="detail-item">
            <div className="l">ATR</div>
            <div className="v">₹{atr.toFixed(2)}</div>
          </div>
        </div>

        <div className="drawer-section">
          <h3>Factor breakdown</h3>
          {Object.entries(row.buckets ?? {}).map(([key, bucket]) => (
            <div key={key} style={{ marginBottom: 9 }}>
              <div className="score-top">
                <span style={{ textTransform: 'capitalize' }}>{key}</span>
                <span className="muted">{bucket.score.toFixed(1)} / {bucket.max}</span>
              </div>
              <div className="bar">
                <div className="bar-fill mid" style={{ width: `${Math.min(100, bucket.pct)}%` }} />
              </div>
            </div>
          ))}
        </div>

        <div className="drawer-section">
          <h3>Patterns detected</h3>
          {row.patterns.length === 0 && <div className="muted tiny">No pattern fired on the latest bars.</div>}
          {row.patterns.map((p) => (
            <div key={p.name} style={{ marginBottom: 9 }}>
              <span className={`chip ${p.direction}`}>{p.name}</span>
              <span className="tiny muted">{Math.round(p.confidence * 100)}% · {p.note}</span>
            </div>
          ))}
        </div>

        <div className="drawer-section">
          <h3>Levels</h3>
          <div className="stat-line"><span className="l">Support</span><span className="v">{row.support ? `₹${money(row.support)}` : '—'}</span></div>
          <div className="stat-line"><span className="l">Resistance</span><span className="v">{row.resistance ? `₹${money(row.resistance)}` : '—'}</span></div>
          <div className="stat-line"><span className="l">VWAP</span><span className="v">₹{money(row.indicators?.vwap)}</span></div>
        </div>

        <div className="drawer-section">
          <h3>Indicators</h3>
          <div className="detail-grid">
            {INDICATOR_LABELS.map(([key, label, unit]) => (
              <div className="detail-item" key={key}>
                <div className="l">{label}</div>
                <div className="v">
                  {unit === '₹' ? '₹' : ''}
                  {row.indicators?.[key]?.toFixed(2) ?? '—'}
                  {unit !== '₹' ? unit : ''}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="drawer-section">
          <h3>Quality components</h3>
          {Object.entries(row.quality?.components ?? {}).map(([key, value]) => (
            <div className="stat-line" key={key}>
              <span className="l" style={{ textTransform: 'capitalize' }}>{key.replace(/_/g, ' ')}</span>
              <span className="v">{Number(value).toFixed(1)}</span>
            </div>
          ))}
          {!row.quality?.fundamentals_used && (
            <div className="tiny muted" style={{ marginTop: 8 }}>
              Fundamentals unavailable — quality is derived from price, volume and trend persistence.
            </div>
          )}
        </div>

        <div className="drawer-section">
          <h3>Why it scored</h3>
          <ul className="reason-list">
            {row.reasons.map((reason, i) => <li key={i}>{reason}</li>)}
          </ul>
        </div>

        <div className="drawer-section">
          <h3>Indicative plan (bot sizes automatically)</h3>
          <div className="stat-line"><span className="l">Entry</span><span className="v">₹{money(row.price)}</span></div>
          <div className="stat-line"><span className="l">Stop</span><span className="v neg">₹{money(stop)}</span></div>
          <div className="stat-line"><span className="l">Target (2R)</span><span className="v pos">₹{money(target)}</span></div>
          <div className="tiny muted" style={{ marginTop: 8 }}>
            Heuristic output, not investment advice. Patterns have false positives.
          </div>
        </div>
      </div>
    </div>
  )
}
