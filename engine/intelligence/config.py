"""Safe access to the optional runtime intelligence config block."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from engine import settings

# Only these non-broker variables may be read as a model credential. The name
# itself may be configured, so it MUST be constrained here and in the adapter.
ALLOWED_KEY_ENV = frozenset({"GROQ_API_KEY", "INTELLIGENCE_MODEL_API_KEY"})
# Hosted endpoints are restricted to known free-tier OpenAI-compatible APIs.
ALLOWED_HOSTS = frozenset({
    "api.groq.com",
    "generativelanguage.googleapis.com",
})
ALLOWED_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "require_for_entries": True,
    "fail_closed": True,
    "paper_only": True,
    "model": {
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "qwen/qwen3.8-27b",
        "api_key_env": "GROQ_API_KEY",
        "timeout_seconds": 20,
        "retries": 2,
        "rate_limit_per_minute": 30,
        "temperature": 0.1,
        "max_tokens": 1200,
        "structured": True,
        "allow_paid": False,
        "local_endpoint": None,
        "local_model": None,
    },
    "debate": {
        "rounds": 2,
        "critical_objection_blocks": True,
        "min_reports": 6,
    },
    "evolution": {
        "research_enabled": False,
        "interval_hours": 6,
        "auto_promote": False,
        "min_oos_return_pct": 0.5,
        "min_walk_forward_efficiency": 0.6,
        "max_drawdown_pct": 10.0,
        "shadow_sessions": 5,
        "candidate_count": 4,
    },
    "scheduler": {
        "research_24x7": True,
        "holidays_file": "config/nse_holidays.txt",
        "market_sessions_only": True,
    },
    "storage": {"db_path": "data/intelligence.sqlite"},
    "logging": {"usage": True, "decisions": True},
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def config() -> dict:
    raw = settings.cfg("intelligence", default={}) or {}
    merged = _merge(DEFAULTS, raw if isinstance(raw, dict) else {})
    # Hard safety invariant: intelligence can never opt into paid models or live trading.
    merged["paper_only"] = True
    model = dict(merged.get("model") or {})
    model["allow_paid"] = False
    merged["model"] = model
    return merged


def validate_model_settings(model: dict | None) -> list[str]:
    """Reject credential redirection: unknown env names or unapproved endpoints."""
    errors: list[str] = []
    values = model if isinstance(model, dict) else {}
    key_env = str(values.get("api_key_env") or "").strip()
    if key_env and key_env not in ALLOWED_KEY_ENV:
        errors.append(
            f"intelligence.model.api_key_env must be one of: {', '.join(sorted(ALLOWED_KEY_ENV))}"
        )
    for field in ("base_url", "local_endpoint"):
        raw = values.get(field)
        if raw is None or str(raw).strip() == "":
            continue
        parsed = urlparse(str(raw).strip())
        host = (parsed.hostname or "").lower()
        local = field == "local_endpoint" or host in ALLOWED_LOCAL_HOSTS
        if local:
            if parsed.scheme not in ("http", "https") or host not in ALLOWED_LOCAL_HOSTS:
                errors.append(f"intelligence.model.{field} must be a localhost endpoint")
        elif parsed.scheme != "https" or host not in ALLOWED_HOSTS:
            errors.append(
                f"intelligence.model.{field} host is not approved; allowed: "
                + ", ".join(sorted(ALLOWED_HOSTS))
            )
    return errors


def enabled() -> bool:
    return bool(config().get("enabled"))


def require_for_entries() -> bool:
    return bool(config().get("require_for_entries"))


def fail_closed() -> bool:
    return bool(config().get("fail_closed"))


def model() -> dict:
    return dict(config().get("model") or {})


def debate() -> dict:
    return dict(config().get("debate") or {})


def evolution() -> dict:
    return dict(config().get("evolution") or {})


def scheduler() -> dict:
    return dict(config().get("scheduler") or {})


def logging() -> dict:
    return dict(config().get("logging") or {})
