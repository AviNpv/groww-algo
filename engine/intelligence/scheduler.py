"""Cancellable background scheduler for the optional research loop.

The scheduler only decides *eligibility* and runs strategy research. Scanning
and execution stay with the existing AlgoEngine; this class never places orders
and never starts the scanner.
"""
from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path
from typing import Any

from engine import settings
from engine.intelligence import config


class IntelligenceScheduler:
    """One daemon research thread, 24x7, with holiday-aware scan eligibility."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._holiday_cache: dict[str, set[str]] = {"path": "", "mtime": 0.0, "dates": set()}
        self._status: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "stopped_at": None,
            "last_research_at": None,
            "last_result": None,
            "last_error": None,
            "research_runs": 0,
            "next_research_at": None,
        }

    # ------------------------------------------------------------------
    # holidays / eligibility
    # ------------------------------------------------------------------
    def _holidays(self) -> set[str]:
        path_text = str(config.scheduler().get("holidays_file") or "").strip()
        if not path_text:
            return set()
        path = Path(path_text)
        if not path.is_absolute():
            path = settings.BASE_DIR / path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return set()
        cached = self._holiday_cache
        if cached["path"] == str(path) and cached["mtime"] == mtime:
            return set(cached["dates"])
        dates: set[str] = set()
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                token = line.split()[0].split("#")[0]
                try:
                    dates.add(dt.date.fromisoformat(token[:10]).isoformat())
                except ValueError:
                    continue
        except OSError:
            return set()
        self._holiday_cache = {"path": str(path), "mtime": mtime, "dates": dates}
        return set(dates)

    def should_research(self, now: dt.datetime | None = None) -> bool:
        """Research ignores market hours and holidays when 24x7 is enabled."""
        try:
            sched = config.scheduler()
            ev = config.evolution()
            return bool(sched.get("research_24x7")) and bool(ev.get("research_enabled"))
        except Exception:
            return False

    @staticmethod
    def _session_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
        if now.date() == settings.now_ist().date():
            return settings.market_session()
        start_text = str(settings.cfg("engine", "entry_window",
                                      default=["09:15", "15:30"])[0])
        end_text = str(settings.cfg("engine", "square_off", default="15:30"))

        def parse(value: str, fallback: dt.time) -> dt.time:
            try:
                hh, mm = value.split(":")[:2]
                return dt.time(int(hh), int(mm))
            except (ValueError, AttributeError):
                return fallback

        start = parse(start_text, dt.time(9, 15))
        end = parse(end_text, dt.time(15, 30))
        return (
            now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0),
            now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0),
        )

    def should_scan(self, now: dt.datetime | None = None) -> bool:
        """Eligibility only; the AlgoEngine owns the actual scan and orders."""
        try:
            now = now or settings.now_ist()
            if now.tzinfo is None:
                now = now.replace(tzinfo=settings.IST)
            if not settings.is_trading_day(now.date()):
                return False
            if now.date().isoformat() in self._holidays():
                return False
            if config.scheduler().get("market_sessions_only", True):
                start, end = self._session_bounds(now)
                if not (start <= now <= end):
                    return False
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            if not self.should_research():
                self._status["last_error"] = None
                self._status["stopped_at"] = _ts()
                return self.status()
            self._stop.clear()
            thread = threading.Thread(
                target=self._loop, name="intelligence-research", daemon=True
            )
            self._thread = thread
            self._status.update({
                "running": True,
                "started_at": _ts(),
                "stopped_at": None,
                "last_error": None,
            })
            thread.start()
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._status["running"] = False
            self._status["stopped_at"] = _ts()
            self._status["next_research_at"] = None
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=3)
            except Exception:
                pass
        return self.status()

    def status(self) -> dict:
        with self._lock:
            out = dict(self._status)
        try:
            sched = config.scheduler()
            ev = config.evolution()
            out["config"] = {
                "research_24x7": bool(sched.get("research_24x7")),
                "research_enabled": bool(ev.get("research_enabled")),
                "interval_hours": ev.get("interval_hours"),
                "market_sessions_only": bool(sched.get("market_sessions_only", True)),
                "holidays_file": str(sched.get("holidays_file") or "") or None,
            }
        except Exception as exc:
            out["config"] = {}
            out["last_error"] = f"config: {type(exc).__name__}: {exc}"[:300]
        out["should_research"] = self.should_research()
        out["should_scan"] = self.should_scan()
        return out

    # ------------------------------------------------------------------
    # loop
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                interval = 6 * 3600
                try:
                    interval = max(0.1, float(config.evolution().get("interval_hours", 6) or 6)) * 3600
                    if not self.should_research():
                        self._stop.wait(min(60.0, interval))
                        continue
                    with self._lock:
                        self._status["next_research_at"] = (
                            settings.now_ist() + dt.timedelta(seconds=interval)
                        ).isoformat(timespec="seconds")
                    try:
                        from engine.intelligence.evolution import StrategyEvolutionEngine

                        result = StrategyEvolutionEngine().run_once()
                        with self._lock:
                            self._status["last_research_at"] = _ts()
                            self._status["research_runs"] += 1
                            self._status["last_result"] = {
                                "status": result.get("status"),
                                "counts": result.get("counts"),
                                "promotion_status": (result.get("promotion") or {}).get("status"),
                            }
                            if result.get("ok") is False:
                                self._status["last_error"] = str(result.get("error"))[:500]
                    except Exception as exc:
                        with self._lock:
                            self._status["last_error"] = (
                                f"{type(exc).__name__}: {exc}"[:500]
                            )
                except Exception as exc:
                    with self._lock:
                        self._status["last_error"] = f"{type(exc).__name__}: {exc}"[:500]
                self._stop.wait(interval)
        finally:
            with self._lock:
                self._status["running"] = False
                self._status["next_research_at"] = None


# One process-wide instance shared by the startup hook, the mode switch and
# the read-only status endpoint. Without this, status can report a second,
# never-started scheduler while the real research thread is running.
_SCHEDULER: IntelligenceScheduler | None = None


def get_scheduler() -> IntelligenceScheduler:
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = IntelligenceScheduler()
    return _SCHEDULER


def stop_scheduler() -> None:
    global _SCHEDULER
    existing = _SCHEDULER
    _SCHEDULER = None
    if existing is not None:
        existing.stop()


def _ts() -> str:
    return settings.now_ist().isoformat(timespec="seconds")
