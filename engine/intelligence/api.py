"""Read-only runtime intelligence API with explicit strategy lifecycle controls.

Every intelligence import is lazy so the main FastAPI app can start even when
optional modules are still incomplete. No credentials, environment variables,
broker clients or order-placement functions are exposed here.
"""
from __future__ import annotations

import importlib
import os
import re
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])
api_router = router  # convenience alias for the main app

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.&_-]{1,60}$")
_SECRET_HINTS = (
    "key", "secret", "token", "password", "passwd", "credential",
    "authorization", "auth_", "cookie", "base_url", "endpoint", "env",
)
_SECRET_TEXT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|authorization)\b\s*[:=]\s*"
    r"[\"']?[^\s,\"']+"
)
_BEARER_TEXT = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]+")
_SAFE_KEYS = frozenset({"total_tokens", "prompt_tokens", "completion_tokens"})

_EVOLUTION_KEYS = (
    "research_enabled", "interval_hours", "auto_promote",
    "min_oos_return_pct", "min_walk_forward_efficiency",
    "max_drawdown_pct", "shadow_sessions", "candidate_count",
)
_SCHEDULER_KEYS = ("research_24x7", "holidays_file", "market_sessions_only")


class RollbackRequest(BaseModel):
    version_id: str | None = None


def _config():
    from engine.intelligence import config as ai_config
    return ai_config


def _store():
    from engine.intelligence import storage
    return storage


def _get_scheduler():
    """Return the same process-wide scheduler used by startup and mode switch."""
    from engine.intelligence.scheduler import get_scheduler
    return get_scheduler()


def _get_evolution_engine():
    from engine.intelligence.evolution import StrategyEvolutionEngine
    return StrategyEvolutionEngine()


def _limit(value: int | None, default: int, cap: int) -> int:
    try:
        return max(1, min(int(value if value is not None else default), cap))
    except (TypeError, ValueError):
        return default


def _ts() -> str:
    from engine.settings import now_ist
    return now_ist().isoformat(timespec="seconds")


def _require_admin(request: Request) -> None:
    """Gate strategy-changing routes with an explicit operator token.

    If the token is unset, mutation is disabled entirely. The token is never
    accepted from a query string and is compared in constant time.
    """
    token = os.environ.get("INTELLIGENCE_ADMIN_TOKEN") or ""
    if not token:
        raise HTTPException(status_code=503, detail="intelligence admin actions disabled")
    provided = request.headers.get("x-intelligence-token") or ""
    if not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="invalid intelligence admin token")


def _scrub(value: Any) -> Any:
    """Recursively remove anything that looks like a secret or raw endpoint."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key).lower()
            if text not in _SAFE_KEYS and any(hint in text for hint in _SECRET_HINTS):
                continue
            out[str(key)] = _scrub(item)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_scrub(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _scrub(value.model_dump())
        except Exception:
            pass
    if isinstance(value, str):
        text = _SECRET_TEXT.sub(r"\1: [REDACTED]", value)
        return _BEARER_TEXT.sub("Bearer [REDACTED]", text)[:500]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:300]


# ----------------------------------------------------------------------
# status / health
# ----------------------------------------------------------------------
@router.get("/status")
def intelligence_status() -> dict:
    cfg = _config()
    whole = cfg.config()
    evolution = whole.get("evolution") or {}
    sched = whole.get("scheduler") or {}
    model = whole.get("model") or {}
    store = _store()
    try:
        storage_summary = store.summary()
    except Exception as exc:
        storage_summary = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        scheduler_status = _get_scheduler().status()
    except Exception as exc:
        scheduler_status = {"available": False,
                            "error": f"{type(exc).__name__}: {exc}"[:200]}
    return {
        "ok": True,
        "ts": _ts(),
        "enabled": bool(whole.get("enabled")),
        "config": {
            "enabled": bool(whole.get("enabled")),
            "require_for_entries": bool(whole.get("require_for_entries")),
            "fail_closed": bool(whole.get("fail_closed")),
            "paper_only": bool(whole.get("paper_only")),
            "evolution": {key: evolution.get(key) for key in _EVOLUTION_KEYS},
            "scheduler": {
                **{key: sched.get(key) for key in _SCHEDULER_KEYS},
                "holidays_file": str(sched.get("holidays_file") or "") or None,
            },
            "model": {
                "provider": model.get("provider"),
                "model": model.get("model"),
                "structured": bool(model.get("structured")),
                "allow_paid": False,
            },
        },
        "storage": storage_summary,
        "scheduler": scheduler_status,
    }


@router.get("/health")
def intelligence_health() -> dict:
    """Model-adapter health, probed lazily; never returns key material."""
    model_cfg = _config().model()
    info: dict[str, Any] = {
        "ok": False,
        "ts": _ts(),
        "provider": model_cfg.get("provider"),
        "model": model_cfg.get("model"),
        "paid_allowed": False,
    }
    try:
        module = importlib.import_module("engine.intelligence.model_adapter")
    except Exception as exc:
        info.update({
            "status": "unavailable",
            "message": f"model adapter not available: {type(exc).__name__}",
        })
        return info

    attempts: list[tuple[str, Any]] = []
    for name in ("get_health", "health", "check_health", "model_health"):
        if callable(getattr(module, name, None)):
            attempts.append((name, getattr(module, name)))
    for name in ("get_adapter", "create_adapter", "get_model_adapter",
                 "ModelAdapter", "ModelGateway", "Adapter"):
        if callable(getattr(module, name, None)):
            attempts.append((name, getattr(module, name)))

    last_error = "no health probe found in model adapter"
    for name, factory in attempts:
        try:
            try:
                probe = factory()
            except TypeError:
                probe = factory(model_cfg)
            if isinstance(probe, dict) or hasattr(probe, "model_dump"):
                health = probe
            else:
                method = None
                for attr in ("health", "get_health", "check_health", "healthy"):
                    candidate = getattr(probe, attr, None)
                    if callable(candidate):
                        method = candidate
                        break
                if method is None:
                    continue
                try:
                    # Dashboard polling probes health every few seconds. Keep
                    # those probes out of the model-usage evidence log; real
                    # agent calls still record usage in complete().
                    health = method(record=False)
                except TypeError:
                    try:
                        health = method()
                    except TypeError:
                        health = method(model_cfg)
            if hasattr(health, "model_dump"):
                health = health.model_dump()
            if not isinstance(health, dict):
                health = {"message": str(health)[:300]}
            clean = _scrub(health)
            info.update({
                "ok": bool(clean.get("ok", clean.get("healthy", False))),
                "status": "ok" if clean.get("ok", clean.get("healthy")) else "degraded",
                "health": clean,
                "probe": name,
            })
            return info
        except Exception as exc:
            last_error = f"{name}: {type(exc).__name__}: {exc}"[:300]
    info.update({"status": "error", "message": _scrub(last_error)})
    return info


# ----------------------------------------------------------------------
# evidence views
# ----------------------------------------------------------------------
@router.get("/decisions")
def intelligence_decisions(
    limit: int = Query(default=20, ge=1, le=200),
    symbol: str | None = Query(default=None),
) -> dict:
    if symbol is not None:
        symbol = symbol.strip().upper()
        if not symbol:
            symbol = None
        elif not _SYMBOL_RE.match(symbol):
            raise HTTPException(status_code=400, detail="invalid symbol")
    rows = _scrub(_store().latest_decisions(_limit(limit, 20, 200), symbol))
    return {"ts": _ts(), "symbol": symbol, "count": len(rows), "decisions": rows}


@router.get("/debate/{symbol}")
def intelligence_debate(symbol: str) -> dict:
    symbol = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(status_code=400, detail="invalid symbol")
    debate = _scrub(_store().latest_debate(symbol))
    return {"ts": _ts(), "symbol": symbol, "debate": debate,
            "found": debate is not None}


@router.get("/usage")
def intelligence_usage(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    rows = _scrub(_store().usage(_limit(limit, 50, 200)))
    return {"ts": _ts(), "count": len(rows), "usage": rows}


@router.get("/executions")
def intelligence_executions(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    rows = _scrub(_store().executions(_limit(limit, 50, 200)))
    return {"ts": _ts(), "count": len(rows), "executions": rows}


@router.get("/versions")
def intelligence_versions(limit: int = Query(default=30, ge=1, le=200)) -> dict:
    store = _store()
    rows = _scrub(store.versions(_limit(limit, 30, 200)))
    active = store.active_version()
    active_summary = None
    if active:
        active_summary = _scrub({key: value for key, value in active.items()
                                if key != "yaml"})
    return {"ts": _ts(), "count": len(rows), "versions": rows,
            "active": active_summary}


# ----------------------------------------------------------------------
# explicit strategy lifecycle controls (no order endpoints, ever)
# ----------------------------------------------------------------------
@router.post("/evolution/run")
def intelligence_evolution_run(request: Request) -> dict:
    _require_admin(request)
    return _scrub(_get_evolution_engine().run_once())


@router.post("/strategy/{version_id}/promote")
def intelligence_strategy_promote(version_id: str, request: Request) -> dict:
    _require_admin(request)
    if not _ID_RE.match(version_id or ""):
        raise HTTPException(status_code=400, detail="invalid version_id")
    store = _store()
    row = store.get_version(version_id)
    if not row:
        raise HTTPException(status_code=404, detail="version not found")
    if row.get("status") not in ("validated", "shadow"):
        raise HTTPException(status_code=400, detail="only validated/shadow versions can be promoted")
    try:
        result = _get_evolution_engine().promote(
            version_id, require_shadow=(row.get("status") == "shadow")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/strategy/rollback")
def intelligence_strategy_rollback(request: Request, req: RollbackRequest | None = None) -> dict:
    _require_admin(request)
    version_id = (req.version_id if req else None)
    if version_id is not None:
        version_id = version_id.strip()
        if not _ID_RE.match(version_id):
            raise HTTPException(status_code=400, detail="invalid version_id")
    result = _get_evolution_engine().rollback(version_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("message") or result))
    return result
