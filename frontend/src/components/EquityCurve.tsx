import { useMemo } from 'react'
import type { EquityPoint } from '../types'
import { money } from '../api'

interface Props {
  curve: EquityPoint[]
}

const W = 640
const H = 190
const PAD = 10

export default function EquityCurve({ curve }: Props) {
  const geometry = useMemo(() => {
    if (curve.length < 2) return null
    const values = curve.map((c) => c.equity)
    const min = Math.min(...values)
    const max = Math.max(...values)
    const span = max - min || Math.max(1, max * 0.01)
    const stepX = (W - PAD * 2) / (curve.length - 1)
    const y = (v: number) => PAD + (1 - (v - min) / span) * (H - PAD * 2)
    const points = values.map((v, i) => `${(PAD + i * stepX).toFixed(1)},${y(v).toFixed(1)}`)
    const line = `M ${points.join(' L ')}`
    const area = `${line} L ${(PAD + (curve.length - 1) * stepX).toFixed(1)},${H - PAD} L ${PAD},${H - PAD} Z`
    return { line, area, min, max, first: values[0], last: values[values.length - 1] }
  }, [curve])

  if (!geometry) {
    return <div className="empty-state">Not enough sessions yet — the equity curve appears after the second session.</div>
  }

  const gain = geometry.last - geometry.first
  const up = gain >= 0
  const stroke = up ? '#0ecb81' : '#f6465d'

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8, fontSize: 12 }}>
        <span className="muted">{curve[0].day}</span>
        <span className={up ? 'pos' : 'neg'} style={{ fontWeight: 700 }}>
          {up ? '+' : ''}₹{money(gain)}
        </span>
        <span className="muted">{curve[curve.length - 1].day}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="Equity curve">
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.28" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        <line x1={PAD} y1={PAD} x2={PAD} y2={H - PAD} stroke="#2b3139" strokeWidth="1" />
        <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="#2b3139" strokeWidth="1" />
        <path d={geometry.area} fill="url(#equityFill)" />
        <path d={geometry.line} fill="none" stroke={stroke} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
      </svg>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10.5 }} className="muted">
        <span>low ₹{money(geometry.min, 0)}</span>
        <span>high ₹{money(geometry.max, 0)}</span>
      </div>
    </div>
  )
}
