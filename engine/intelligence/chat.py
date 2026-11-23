"""Live, read-only agent group chat for one symbol.

The chat runs the same advisory pipeline as the engine's intelligence gate
(snapshot -> six agents -> deterministic debate -> deterministic rules) but
streams each step so an operator can watch the debate happen.

Safety invariants:
* paper-only: it never calls a broker and never places an order;
* advisory: it never changes configuration and never promotes a strategy;
* fail-closed: agent errors stay errors, and the deterministic debate/rules
  still decide the outcome;
* one live run at a time, with a short cooldown, so a demo cannot hammer the
  model quota.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Callable

from engine.intelligence import config as ai_config

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.&_-]{1,60}$")
_SECRET_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*"
    r"[\"']?[^\s,\"']+"
)
_BEARER_TEXT = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]+")

_MAX_TEXT = 700
_MAX_EVENTS = 400
_MAX_SESSIONS = 12
_SESSION_TTL_SECONDS = 1800.0
_DEFAULT_COOLDOWN_SECONDS = 20.0
_HEARTBEAT_SECONDS = 15.0


class ChatUnavailable(RuntimeError):
    """Expected refusal or lookup failure; ``status_code`` maps to HTTP."""

    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def clean(value: Any, limit: int = _MAX_TEXT) -> str:
    """Redact secret-looking text and cap the size of any chat message."""
    text = str(value if value is not None else "")
    text = _BEARER_TEXT.sub("Bearer [REDACTED]", text)
    text = _SECRET_TEXT.sub(r"\1: [REDACTED]", text)
    return text[:limit]


def sanitize(value: Any) -> Any:
    """Recursively cap and redact every string before it reaches an SSE client."""
    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return clean(value)
    return value


def _now_iso() -> str:
    try:
        from engine.settings import now_ist
        return now_ist().isoformat(timespec="seconds")
    except Exception:
        return ""


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol.endswith(".NS"):
        symbol = symbol[:-3]
    if not _SYMBOL_RE.match(symbol):
        raise ChatUnavailable("invalid symbol", 400)
    return symbol


def _norm(value: Any) -> str:
    return str(value or "").strip().upper().replace(".NS", "")


class ChatSession:
    """One run's event log plus a condition used by the SSE reader."""

    def __init__(self, symbol: str) -> None:
        self.id = f"chat-{uuid.uuid4().hex[:12]}"
        self.symbol = symbol
        self.events: list[tuple[int, dict]] = []
        self.done = False
        self.ok = False
        self.error: str | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self._seq = 0
        self._condition = threading.Condition()

    def emit(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        with self._condition:
            if self.done:
                return
            self._seq += 1
            payload = sanitize(dict(event))
            payload.setdefault("type", "message")
            payload.setdefault("ts", _now_iso())
            payload["seq"] = self._seq
            self.events.append((self._seq, payload))
            if len(self.events) > _MAX_EVENTS:
                self.events = self.events[-_MAX_EVENTS:]
            self._condition.notify_all()

    def finish(self, ok: bool, error: str | None = None) -> None:
        with self._condition:
            if self.done:
                return
            if error:
                self._seq += 1
                self.events.append((self._seq, {
                    "type": "error", "seq": self._seq, "ts": _now_iso(),
                    "message": clean(error),
                }))
            self._seq += 1
            self.events.append((self._seq, {
                "type": "done", "seq": self._seq, "ts": _now_iso(),
                "ok": bool(ok),
            }))
            self.done = True
            self.ok = bool(ok)
            self.error = clean(error) if error else None
            self.finished_at = time.time()
            self._condition.notify_all()

    def read(self, after: int = 0,
             timeout: float = _HEARTBEAT_SECONDS) -> tuple[list[tuple[int, dict]], bool]:
        """Return events after ``after``; blocks up to ``timeout`` seconds."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                pending = [(seq, dict(event)) for seq, event in self.events
                           if seq > int(after or 0)]
                if pending or self.done:
                    return pending, self.done
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], False
                self._condition.wait(remaining)

    def summary(self) -> dict:
        return {
            "session_id": self.id,
            "symbol": self.symbol,
            "done": self.done,
            "ok": self.ok,
            "error": self.error,
            "events": len(self.events),
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


def _default_config() -> dict:
    return ai_config.config()


class AgentChatService:
    """Owns chat sessions and the single background advisory run."""

    def __init__(self, pipeline: Callable[[str, Callable[[dict], None]], Any] | None = None,
                 config_provider: Callable[[], dict] | None = None,
                 cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
                 session_ttl: float = _SESSION_TTL_SECONDS) -> None:
        self._pipeline = pipeline or run_pipeline
        self._config_provider = config_provider or _default_config
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._session_ttl = max(0.0, float(session_ttl))
        self._sessions: dict[str, ChatSession] = {}
        self._active_id: str | None = None
        self._last_started = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def start(self, symbol: Any) -> dict:
        sym = normalize_symbol(symbol)
        try:
            cfg = self._config_provider() or {}
        except Exception as exc:
            raise ChatUnavailable(
                f"intelligence config unavailable: {type(exc).__name__}", 503
            ) from exc
        if not bool(cfg.get("enabled")):
            raise ChatUnavailable(
                "AI mode is off. Switch the desk to AI mode, then start the chat.", 409
            )
        if not bool(cfg.get("paper_only", True)):
            raise ChatUnavailable("paper-only invariant missing; chat refused", 503)

        now = time.monotonic()
        with self._lock:
            self._prune()
            if self._active_id:
                active = self._sessions.get(self._active_id)
                if active is not None and not active.done:
                    raise ChatUnavailable(
                        "an agent chat is already running; wait for it to finish", 409
                    )
                self._active_id = None
            if self._last_started and now - self._last_started < self._cooldown:
                wait = int(self._cooldown - (now - self._last_started)) + 1
                raise ChatUnavailable(
                    f"please wait {wait}s before starting another chat", 429
                )
            self._last_started = now
            session = ChatSession(sym)
            self._sessions[session.id] = session
            self._active_id = session.id

        session.emit({
            "type": "session",
            "session_id": session.id,
            "symbol": sym,
            "paper_only": True,
            "message": (
                f"Live advisory review for {sym}. Paper-only: these agents can "
                "debate, but they cannot place an order."
            ),
        })
        thread = threading.Thread(
            target=self._run,
            args=(session,),
            name=f"agent-chat-{session.id}",
            daemon=True,
        )
        thread.start()
        return {
            "ok": True,
            "session_id": session.id,
            "symbol": sym,
            "paper_only": True,
            "stream": f"/api/intelligence/chat/{session.id}/stream",
            "started_at": _now_iso(),
        }

    def get(self, session_id: str) -> ChatSession:
        with self._lock:
            session = self._sessions.get(str(session_id or ""))
        if session is None:
            raise ChatUnavailable("chat session not found", 404)
        return session

    def status(self) -> dict:
        with self._lock:
            self._prune()
            rows = [session.summary() for session in self._sessions.values()]
            active = self._active_id
        return {"ok": True, "active": bool(active), "active_session": active,
                "sessions": rows[-_MAX_SESSIONS:]}

    # --------------------------------------------------------------- internals
    def _run(self, session: ChatSession) -> None:
        try:
            result = self._pipeline(session.symbol, session.emit)
            if isinstance(result, dict) and result.get("error"):
                session.finish(False, str(result["error"]))
            elif result is None:
                session.finish(False, "the advisory review did not complete")
            else:
                session.finish(True)
        except ChatUnavailable as exc:
            session.finish(False, str(exc))
        except Exception as exc:  # never let a chat crash a worker
            session.finish(False, f"{type(exc).__name__}: live chat failed")
        finally:
            with self._lock:
                if self._active_id == session.id:
                    self._active_id = None

    def _prune(self) -> None:
        now = time.time()
        stale = [
            session_id for session_id, session in self._sessions.items()
            if session.done and session.finished_at is not None
            and now - session.finished_at > self._session_ttl
        ]
        for session_id in stale:
            self._sessions.pop(session_id, None)
        if len(self._sessions) > _MAX_SESSIONS:
            done = sorted(
                (session for session in self._sessions.values() if session.done),
                key=lambda session: session.created_at,
            )
            for session in done[: max(0, len(self._sessions) - _MAX_SESSIONS)]:
                self._sessions.pop(session.id, None)


def sse_frames(session: ChatSession, after: int = 0,
               heartbeat_seconds: float = _HEARTBEAT_SECONDS):
    """Yield Server-Sent Events for one session until it finishes."""
    index = max(0, int(after or 0))
    while True:
        events, done = session.read(index, timeout=heartbeat_seconds)
        if events:
            for seq, event in events:
                index = seq
                payload = json.dumps(event, ensure_ascii=False, default=str)
                yield f"id: {seq}\ndata: {payload}\n\n"
            if done:
                return
            continue
        if done:
            return
        yield ": keep-alive\n\n"


# ---------------------------------------------------------------------------
# default pipeline
# ---------------------------------------------------------------------------
def _row_for(scan: dict, symbol: str) -> dict | None:
    for row in (scan.get("results") or []):
        if isinstance(row, dict) and _norm(row.get("symbol")) == symbol:
            return row
    return None


def _last_scan_row(symbol: str) -> tuple[dict | None, dict]:
    try:
        from engine.engine import ENGINE
        last = getattr(ENGINE, "last_scan", None)
        if isinstance(last, dict):
            return _row_for(last, symbol), last
    except Exception:
        pass
    return None, {}


def _feed_view(snapshot) -> dict:
    feed = getattr(snapshot, "feed", None)
    return {
        "active": getattr(feed, "active", None),
        "delayed": getattr(feed, "delayed", None),
        "groww_ok": bool(getattr(feed, "groww_ok", False)),
        "message": clean(getattr(feed, "message", "") or "", 220),
    }


def run_pipeline(symbol: str, emit: Callable[[dict], None]) -> Any:
    """Scan one symbol and stream the real advisory pipeline for it."""
    from engine import scanner
    from engine.intelligence.orchestrator import Orchestrator
    from engine.intelligence.snapshot import build_snapshot

    symbol = normalize_symbol(symbol)
    emit({"type": "status",
          "message": f"Scanning {symbol} on the current data feed..."})

    scan: dict = {}
    row: dict | None = None
    try:
        scan = scanner.scan(symbols=[symbol], with_fundamentals=False)
        row = _row_for(scan, symbol)
    except Exception as exc:
        emit({"type": "warning",
              "message": f"live scan failed ({type(exc).__name__}); trying the last scan"})

    if row is None:
        row, last = _last_scan_row(symbol)
        if row is not None:
            scan = last
            emit({"type": "warning",
                  "message": "Using the engine's most recent scan for this symbol."})

    if row is None:
        emit({"type": "error",
              "message": (f"No analyzable data for {symbol}. Check the symbol "
                          "and the data feed, then try again.")})
        return None

    emit({
        "type": "scan",
        "symbol": symbol,
        "price": row.get("price"),
        "score": row.get("score"),
        "status": row.get("status"),
        "confirmations": row.get("confirmations"),
        "source": scan.get("source"),
        "delayed": row.get("delayed"),
        "quote_age_sec": row.get("quote_age_sec"),
        "scanned": scan.get("scanned"),
        "ts": scan.get("ts"),
    })

    snapshot = build_snapshot(scan_result=scan)
    candidate = next(
        (item for item in snapshot.candidates if _norm(item.symbol) == symbol),
        None,
    )
    if candidate is None:
        emit({"type": "error",
              "message": f"{symbol} produced no candidate snapshot; chat stopped."})
        return None

    session = snapshot.market_session or {}
    emit({
        "type": "snapshot",
        "symbol": symbol,
        "price": candidate.price,
        "score": candidate.score,
        "status": candidate.status,
        "confirmations": candidate.confirmations,
        "action": candidate.action,
        "stop": candidate.stop,
        "target": candidate.target,
        "qty": candidate.qty,
        "risk_reward": candidate.risk_reward,
        "source": candidate.source,
        "delayed": candidate.delayed,
        "data_stale": candidate.data_stale,
        "quote_age_sec": candidate.quote_age_sec,
        "market_open": session.get("market_open"),
        "in_entry_window": session.get("in_entry_window"),
        "now_ist": session.get("now_ist"),
        "equity": snapshot.account.equity,
        "positions": len(snapshot.positions),
        "feed": _feed_view(snapshot),
        "rejections": list(candidate.rejection_reasons or [])[:8],
    })

    held = {position.symbol for position in snapshot.positions}
    orchestrator = Orchestrator()
    decision = orchestrator.evaluate(
        {"symbol": symbol}, snapshot=snapshot, held=held, on_event=emit
    )

    emit({
        "type": "decision",
        "symbol": symbol,
        "action": decision.action,
        "model_ok": bool(decision.model_ok),
        "rationale": clean(decision.rationale),
        "reject_reasons": [clean(item, 300) for item in
                           (decision.reject_reasons or [])][:12],
        "decision_id": decision.decision_id,
        "paper_only": True,
    })
    return decision


SERVICE = AgentChatService()


__all__ = [
    "AgentChatService",
    "ChatSession",
    "ChatUnavailable",
    "SERVICE",
    "clean",
    "sanitize",
    "normalize_symbol",
    "run_pipeline",
    "sse_frames",
]
