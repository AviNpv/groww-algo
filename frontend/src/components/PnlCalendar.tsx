import { useMemo } from 'react'
import type { CalendarDay } from '../types'
import { moneySigned, pct } from '../api'

interface Props {
  year: number
  month: number
  days: CalendarDay[]
  monthPnl: number
  sessions: number
  green: number
  red: number
  selected: string | null
  onSelect: (day: string | null) => void
  onMonthChange: (year: number, month: number) => void
}

const DOW = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December']

export default function PnlCalendar({
  year, month, days, monthPnl, sessions, green, red, selected, onSelect, onMonthChange,
}: Props) {
  const byDay = useMemo(() => {
    const map = new Map<string, CalendarDay>()
    days.forEach((d) => map.set(d.day, d))
    return map
  }, [days])

  const maxAbs = useMemo(
    () => Math.max(1, ...days.map((d) => Math.abs(d.pnl))),
    [days],
  )

  const daysInMonth = new Date(year, month, 0).getDate()
  const offset = (new Date(year, month - 1, 1).getDay() + 6) % 7
  const today = new Date()
  const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`

  const cells: Array<{ key: string; date: string | null }> = []
  for (let i = 0; i < offset; i += 1) cells.push({ key: `pad-${i}`, date: null })
  for (let d = 1; d <= daysInMonth; d += 1) {
    cells.push({
      key: `d-${d}`,
      date: `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`,
    })
  }
  while (cells.length % 7 !== 0) cells.push({ key: `tail-${cells.length}`, date: null })

  const move = (delta: number) => {
    const next = new Date(year, month - 1 + delta, 1)
    onMonthChange(next.getFullYear(), next.getMonth() + 1)
  }

  return (
    <div>
      <div className="card-head">
        <div>
          <h2>P&amp;L Calendar</h2>
          <div className="sub">
            {sessions} sessions · {green} green · {red} red ·{' '}
            <span className={monthPnl >= 0 ? 'pos' : 'neg'}>
              {moneySigned(monthPnl)}
            </span>{' '}
            this month
          </div>
        </div>
        <div className="cal-toolbar">
          <button className="btn small icon" onClick={() => move(-1)} aria-label="Previous month">‹</button>
          <span className="month">{MONTHS[month - 1]} {year}</span>
          <button className="btn small icon" onClick={() => move(1)} aria-label="Next month">›</button>
        </div>
      </div>

      <div className="card-body">
        <div className="cal-grid">
          {DOW.map((d) => (
            <div className="cal-dow" key={d}>{d}</div>
          ))}

          {cells.map(({ key, date }) => {
            if (!date) return <div className="cal-cell empty" key={key} />
            const data = byDay.get(date)
            const dayNum = Number(date.slice(-2))
            if (!data) {
              return (
                <div
                  className={`cal-cell${date === todayKey ? ' today' : ''}`}
                  key={key}
                  title="No session recorded"
                >
                  <span className="cal-day">{dayNum}</span>
                </div>
              )
            }
            const win = data.pnl > 0
            const loss = data.pnl < 0
            const alpha = 0.10 + Math.min(0.42, (Math.abs(data.pnl) / maxAbs) * 0.42)
            const background = win
              ? `rgba(14, 203, 129, ${alpha})`
              : loss
                ? `rgba(246, 70, 93, ${alpha})`
                : 'var(--bg-soft)'
            return (
              <div
                className={`cal-cell session${date === todayKey ? ' today' : ''}${selected === date ? ' selected' : ''}`}
                key={key}
                style={{ background }}
                onClick={() => onSelect(selected === date ? null : date)}
                title={`${date} · P&L ${moneySigned(data.pnl)} · ${data.trades} trades`}
              >
                <span className="cal-day">{dayNum}</span>
                <span className="cal-trades">{data.trades} trade{data.trades === 1 ? '' : 's'}</span>
                <span className={`cal-pnl ${win ? 'pos' : loss ? 'neg' : 'muted'}`}>
                  {moneySigned(data.pnl, 0)}
                </span>
                <span className="cal-pct">{pct(data.pnl_pct)}</span>
              </div>
            )
          })}
        </div>

        <div className="cal-legend">
          <span><i className="legend-swatch" style={{ background: 'var(--green)' }} />Profit day</span>
          <span><i className="legend-swatch" style={{ background: 'var(--red)' }} />Loss day</span>
          <span><i className="legend-swatch" style={{ background: 'var(--panel-3)' }} />No session</span>
          <span className="muted">Darker = larger move · click a day for detail</span>
        </div>
      </div>
    </div>
  )
}
