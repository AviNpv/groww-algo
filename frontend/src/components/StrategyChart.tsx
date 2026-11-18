import { useEffect, useMemo, useState } from 'react'
import type { ChartResponse, ChartRule, ScanRow } from '../types'
import { api, money } from '../api'

interface Props {
  row: ScanRow
}

const W = 880
const PRICE_TOP = 12
const PRICE_BOTTOM = 306
const VOL_TOP = 320
const VOL_BOTTOM = 388
const LEFT = 8
const RIGHT = 96

const UP = '#0ecb81'
const DOWN = '#f6465d'

interface LevelLine {
  label: string
  value: number
  color: string
  dash?: string
}

export default function StrategyChart({ row }: Props) {
  const [interval, setInterval] = useState('5m')
  const [data, setData] = useState<ChartResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hover, setHover] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setHover(null)
    api.chart(row, interval, 5)
      .then((res) => {
        if (!cancelled) setData(res)
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
          setData(null)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
    // The row object is replaced on every live tick; only its symbol matters here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.symbol, interval])

  const candles = data?.candles ?? []
  const levels = data?.levels

  const scale = useMemo(() => {
    const values = candles.flatMap((c) => [c.h, c.l])
    if (levels) {
      for (const value of [levels.entry, levels.stop, levels.target, levels.support,
        levels.resistance, levels.vwap, levels.ema20, levels.ema50]) {
        if (typeof value === 'number' && Number.isFinite(value)) values.push(value)
      }
    }
    if (!values.length) return { min: 0, max: 1, step: 1, x: (i: number) => i, y: (v: number) => v }
    let min = Math.min(...values)
    let max = Math.max(...values)
    if (max === min) { max += max * 0.01 || 1; min -= min * 0.01 || 1 }
    const pad = (max - min) * 0.08
    min -= pad
    max += pad
    const step = (W - LEFT - RIGHT) / Math.max(1, candles.length)
    return {
      min, max, step,
      x: (i: number) => LEFT + i * step + step / 2,
      y: (v: number) => PRICE_TOP + ((max - v) / (max - min)) * (PRICE_BOTTOM - PRICE_TOP),
    }
  }, [candles, levels])

  const maxVolume = Math.max(1, ...candles.map((c) => c.v))
  const overlays = data?.overlays

  const levelLines: LevelLine[] = []
  if (levels) {
    levelLines.push(
      { label: `ENTRY ${money(levels.entry)}`, value: levels.entry, color: '#e6edf3' },
      { label: `STOP ${money(levels.stop)}`, value: levels.stop, color: DOWN, dash: '5 4' },
      { label: `TARGET ${money(levels.target)}`, value: levels.target, color: UP, dash: '5 4' },
    )
    if (levels.support) levelLines.push({ label: `SUPPORT ${money(levels.support)}`, value: levels.support, color: '#7d8994', dash: '2 4' })
    if (levels.resistance) levelLines.push({ label: `RESISTANCE ${money(levels.resistance)}`, value: levels.resistance, color: '#f0b90b', dash: '2 4' })
  }
  const overlayLines: LevelLine[] = [
    { label: 'VWAP', value: overlays?.vwap.at(-1) ?? 0, color: '#f0b90b', dash: '3 3' },
    { label: 'EMA20', value: overlays?.ema20.at(-1) ?? 0, color: '#4a9eff', dash: '3 3' },
    { label: 'EMA50', value: overlays?.ema50.at(-1) ?? 0, color: '#b07cff', dash: '3 3' },
  ].filter((line) => Number.isFinite(line.value) && line.value > 0)
  const allLines = [...levelLines, ...overlayLines]

  const polyline = (values: number[] | undefined) =>
    (values ?? []).map((value, i) => `${scale.x(i).toFixed(1)},${scale.y(value).toFixed(1)}`).join(' ')

  const hovered = hover !== null ? candles[hover] : null

  return (
    <div className="strategy-chart">
      <div className="chart-head">
        <div>
          <h3>Strategy on the candles</h3>
          <div className="tiny muted">
            Entry, stop, target, VWAP/EMA, support/resistance and detected patterns are drawn from
            the active <code>strategy.yaml</code> rules.
          </div>
        </div>
        <div className="chart-tools">
          {['5m', '15m', '1d'].map((value) => (
            <button
              key={value}
              className={`btn small ${interval === value ? 'primary' : 'ghost'}`}
              onClick={() => setInterval(value)}
            >
              {value}
            </button>
          ))}
        </div>
      </div>

      {loading && <div className="empty-state">Loading candles…</div>}
      {error && <div className="error-box">{error}</div>}
      {!loading && !error && data && data.count === 0 && (
        <div className="empty-state">
          No candles returned for {row.symbol}. {data.error ?? 'The market-data feed is unavailable.'}
        </div>
      )}

      {!loading && !error && data && candles.length > 0 && (
        <>
          <div className="chart-meta">
            <span className={`pill ${data.source === 'groww' ? 'live' : 'warn'}`}>
              <span className="dot" />{data.source === 'groww' ? 'GROWW DATA' : data.delayed ? 'YAHOO DELAYED' : 'NO FEED'}
            </span>
            <span className="tiny muted">{candles.length} candles · as of {data.as_of}</span>
          </div>

          <div className="chart-readout">
            {hovered ? (
              <span>
                {hovered.t} · O <b>{money(hovered.o)}</b> H <b>{money(hovered.h)}</b> L <b>{money(hovered.l)}</b>{' '}
                C <b className={hovered.c >= hovered.o ? 'pos' : 'neg'}>{money(hovered.c)}</b> · Vol{' '}
                {hovered.v.toLocaleString('en-IN')}
              </span>
            ) : (
              <span className="muted">Hover a candle for its OHLC · last close <b>{money(candles[candles.length - 1].c)}</b></span>
            )}
          </div>

          <svg className="candle-svg" viewBox={`0 0 ${W} 410`} role="img" aria-label={`${row.symbol} candlestick chart with strategy levels`}>
            <rect x={0} y={0} width={W} height={410} fill="#0b0e11" />
            {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
              const y = PRICE_TOP + fraction * (PRICE_BOTTOM - PRICE_TOP)
              const value = scale.max - fraction * (scale.max - scale.min)
              return (
                <g key={fraction}>
                  <line x1={LEFT} y1={y} x2={W - RIGHT} y2={y} stroke="#1e242b" strokeWidth={1} />
                  <text x={W - RIGHT + 6} y={y + 4} fill="#7d8994" fontSize={10}>{money(value)}</text>
                </g>
              )
            })}
            <line x1={LEFT} y1={VOL_TOP} x2={W - RIGHT} y2={VOL_TOP} stroke="#1e242b" />
            <line x1={LEFT} y1={VOL_BOTTOM} x2={W - RIGHT} y2={VOL_BOTTOM} stroke="#1e242b" />

            {candles.map((c, i) => {
              const x = scale.x(i)
              const up = c.c >= c.o
              const color = up ? UP : DOWN
              const bodyTop = scale.y(Math.max(c.o, c.c))
              const bodyHeight = Math.max(1, Math.abs(scale.y(c.o) - scale.y(c.c)))
              const width = Math.max(1, Math.min(9, scale.step * 0.62))
              const volHeight = (c.v / maxVolume) * (VOL_BOTTOM - VOL_TOP)
              return (
                <g key={i}>
                  <line x1={x} y1={scale.y(c.h)} x2={x} y2={scale.y(c.l)} stroke={color} strokeWidth={1} />
                  <rect x={x - width / 2} y={bodyTop} width={width} height={bodyHeight} fill={color} opacity={0.92} />
                  <rect x={x - width / 2} y={VOL_BOTTOM - volHeight} width={width} height={volHeight} fill={color} opacity={0.28} />
                </g>
              )
            })}

            <polyline points={polyline(overlays?.vwap)} fill="none" stroke="#f0b90b" strokeWidth={1.4} opacity={0.9} />
            <polyline points={polyline(overlays?.ema20)} fill="none" stroke="#4a9eff" strokeWidth={1.3} opacity={0.85} />
            <polyline points={polyline(overlays?.ema50)} fill="none" stroke="#b07cff" strokeWidth={1.3} opacity={0.8} />

            {allLines.map((line, i) => (
              <g key={`${line.label}-${i}`}>
                <line
                  x1={LEFT} y1={scale.y(line.value)} x2={W - RIGHT} y2={scale.y(line.value)}
                  stroke={line.color} strokeWidth={1.1} strokeDasharray={line.dash ?? '6 4'} opacity={0.85}
                />
                <text x={W - RIGHT + 5} y={scale.y(line.value) - 3} fill={line.color} fontSize={9.5}>
                  {line.label.length > 15 ? line.label.slice(0, 14) + '…' : line.label}
                </text>
              </g>
            ))}

            {data.markers.slice(0, 4).map((marker, i) => {
              const candle = candles[Math.min(marker.i, candles.length - 1)]
              if (!candle) return null
              const x = scale.x(Math.min(marker.i, candles.length - 1))
              const y = Math.max(18, scale.y(candle.h) - 10 - (i % 2) * 16)
              const color = marker.direction === 'bearish' ? DOWN : marker.direction === 'bullish' ? UP : '#f0b90b'
              return (
                <g key={`${marker.label}-${i}`}>
                  <polygon points={`${x},${y - 8} ${x - 5},${y + 2} ${x + 5},${y + 2}`} fill={color} />
                  <text x={x + 7} y={y + 2} fill={color} fontSize={10}>{marker.label}</text>
                </g>
              )
            })}

            {hover !== null && candles[hover] && (
              <line x1={scale.x(hover)} y1={PRICE_TOP} x2={scale.x(hover)} y2={VOL_BOTTOM} stroke="#8b949e" strokeWidth={1} strokeDasharray="2 3" />
            )}

            {candles.map((_, i) => (
              <rect
                key={`hit-${i}`}
                x={scale.x(i) - scale.step / 2}
                y={PRICE_TOP}
                width={scale.step}
                height={VOL_BOTTOM - PRICE_TOP}
                fill="transparent"
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
              />
            ))}

            <text x={LEFT} y={405} fill="#7d8994" fontSize={10}>VOLUME</text>
          </svg>

          <div className="chart-legend">
            <span><i style={{ background: UP }} />Bullish candle</span>
            <span><i style={{ background: DOWN }} />Bearish candle</span>
            <span><i style={{ background: '#f0b90b' }} />VWAP / resistance</span>
            <span><i style={{ background: '#4a9eff' }} />EMA20</span>
            <span><i style={{ background: '#b07cff' }} />EMA50</span>
            <span><i style={{ background: '#e6edf3' }} />Entry</span>
          </div>
        </>
      )}

      {data && data.rules.length > 0 && (
        <div className="drawer-section">
          <h3>Why this strategy would trade (or skip)</h3>
          <div className="rule-list">
            {data.rules.map((rule: ChartRule, i: number) => (
              <div className={`rule ${rule.ok === true ? 'ok' : rule.ok === false ? 'no' : 'na'}`} key={`${rule.label}-${i}`}>
                <span className="rule-mark">{rule.ok === true ? '✓' : rule.ok === false ? '✕' : '—'}</span>
                <span className="rule-label">{rule.label}</span>
                <span className="rule-detail">
                  {rule.required} <span className="muted">→</span> <b>{rule.actual}</b>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
