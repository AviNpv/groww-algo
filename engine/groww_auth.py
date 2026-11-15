"""Groww TOTP token minting (shared with the working nse-ai-paper-trade client).

Flow:
    POST https://api.groww.in/v1/token/api/access
    Authorization: Bearer <GROWW_TOTP_TOKEN>
    {"key_type": "totp", "totp": "<6 digits from GROWW_TOTP_SECRET>"}
    -> {"token": "<ACCESS_TOKEN>", "expiry": "..."}

Only minting lives here. No order endpoint is ever touched.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import threading
import time

import requests

from engine.settings import BASE_DIR, CACHE_DIR, env

BASE = "https://api.groww.in"
TOKEN_PATH = "/v1/token/api/access"
ENV_PATH = BASE_DIR / ".env"
TOKEN_CACHE = CACHE_DIR / "groww_access_token.json"

_lock = threading.RLock()
_fail_lock = threading.Lock()
_last_fail: dict[str, object] = {"ts": 0.0, "error": None}
_FAIL_COOLDOWN = 600.0


class GrowwAuthError(RuntimeError):
    def __init__(self, stage: str, message: str, status: int | None = None):
        self.stage = stage
        self.status = status
        self.message = message
        super().__init__(message)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip().strip('"').strip("'")
    for key in ("GROWW_TOTP_TOKEN", "GROWW_TOTP_SECRET", "GROWW_API_SECRET", "GROWW_ACCESS_TOKEN"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def api_key() -> str | None:
    return (read_env().get("GROWW_TOTP_TOKEN") or "").strip() or None


def totp_secret() -> str | None:
    return (read_env().get("GROWW_TOTP_SECRET") or "").strip() or None


def api_secret() -> str | None:
    """The API secret of an approval-type key (no authenticator app needed)."""
    return (read_env().get("GROWW_API_SECRET") or "").strip() or None


def auth_mode() -> str:
    if api_secret():
        return "approval"
    if totp_secret():
        return "totp"
    return "none"


def _approval_payload() -> dict:
    secret = api_secret()
    if not secret:
        raise GrowwAuthError("config", "GROWW_API_SECRET is not set in .env")
    timestamp = int(time.time())
    checksum = hashlib.sha256((secret + str(timestamp)).encode("utf-8")).hexdigest()
    return {"key_type": "approval", "checksum": checksum, "timestamp": timestamp}


def current_code(secret: str | None = None) -> str:
    secret = secret or totp_secret()
    if not secret:
        raise GrowwAuthError("config", "GROWW_TOTP_SECRET is not set in .env")
    try:
        import pyotp
    except ImportError as exc:
        raise GrowwAuthError("config", "pyotp is not installed") from exc
    cleaned = secret.replace(" ", "").replace("-", "").upper()
    try:
        return pyotp.TOTP(cleaned).now()
    except Exception as exc:
        raise GrowwAuthError("config", "GROWW_TOTP_SECRET is not a valid base32 secret") from exc


def _read_cache() -> dict | None:
    try:
        if not TOKEN_CACHE.exists():
            return None
        data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_cache(token: str, expiry: str | None, ref: str | None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps({
        "token": token,
        "expiry": expiry,
        "token_ref_id": ref,
        "obtained_at": dt.datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")


def _valid(cache: dict | None) -> bool:
    if not cache or not cache.get("token"):
        return False
    expiry = cache.get("expiry")
    if not expiry:
        return True
    try:
        exp = dt.datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc) < exp - dt.timedelta(minutes=2)


def generate_access_token() -> str:
    key = api_key()
    if not key:
        raise GrowwAuthError("config", "GROWW_TOTP_TOKEN is not set in .env")
    mode = auth_mode()
    if mode == "approval":
        payload = _approval_payload()
    elif mode == "totp":
        payload = {"key_type": "totp", "totp": current_code()}
    else:
        raise GrowwAuthError("config", "Add either GROWW_API_SECRET or GROWW_TOTP_SECRET to .env")
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-VERSION": "1.0",
    }
    try:
        resp = requests.post(BASE + TOKEN_PATH, headers=headers,
                             json=payload, timeout=20)
    except requests.RequestException as exc:
        raise GrowwAuthError("network", f"could not reach Groww: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        data = {}

    if resp.status_code == 200:
        token = data.get("token") or (data.get("payload") or {}).get("token")
        if not token:
            raise GrowwAuthError("unknown", "Groww returned 200 without a token", 200)
        payload_data = data.get("payload") or data
        _write_cache(token, payload_data.get("expiry"), payload_data.get("tokenRefId"))
        return token

    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    detail = f"{err.get('errorCode')}: {err.get('errorMessage')}" if err else ""
    mapping = {
        400: ("secret", f"Groww rejected the credentials. Check the API key/secret and the PC clock. {detail}".strip()),
        401: ("api_key", "Groww rejected the API key or session ('Please Login and Try Again'). "
                        "Approve the key for today at groww.in/trade-api/api-keys — approval expires daily at 6 AM IST."),
        403: ("approval", "Approve the API key for today at groww.in/trade-api/api-keys."),
        429: ("rate_limit", "Token endpoint limit (150/day). Reusing the cached token."),
    }
    stage, msg = mapping.get(resp.status_code, ("unknown", f"Groww returned HTTP {resp.status_code}"))
    raise GrowwAuthError(stage, msg, resp.status_code)


def get_token(force: bool = False) -> str:
    with _lock:
        cache = _read_cache()
        if not force and _valid(cache):
            return cache["token"]  # type: ignore[index]

        with _fail_lock:
            last = _last_fail
            if not force and last["error"] is not None and time.time() - float(last["ts"]) < _FAIL_COOLDOWN:
                raise last["error"]  # type: ignore[misc]

        try:
            token = generate_access_token()
        except GrowwAuthError as exc:
            with _fail_lock:
                _last_fail["ts"] = time.time()
                _last_fail["error"] = exc
            raise
        with _fail_lock:
            _last_fail["ts"] = 0.0
            _last_fail["error"] = None
        return token


def invalidate() -> None:
    with _lock:
        try:
            TOKEN_CACHE.unlink(missing_ok=True)
        except OSError:
            pass


def status() -> dict:
    key = api_key()
    secret = totp_secret()
    approval = api_secret()
    cached = _read_cache()
    info = {
        "api_key_present": bool(key),
        "api_key_hint": mask(key),
        "totp_secret_present": bool(secret),
        "api_secret_present": bool(approval),
        "auth_mode": auth_mode(),
        "needs_daily_approval": auth_mode() == "approval",
        "token_cached": bool(cached and cached.get("token")),
        "token_expiry": (cached or {}).get("expiry"),
        "obtained_at": (cached or {}).get("obtained_at"),
        "state": "unknown",
        "message": "",
    }
    if not key:
        info.update(state="no_api_key", message="Add GROWW_TOTP_TOKEN to .env")
    elif not secret and not approval and not info["token_cached"]:
        info.update(state="no_secret", message="Add GROWW_API_SECRET (or GROWW_TOTP_SECRET) to .env")
    elif _valid(cached):
        info.update(state="ready", message="Access token cached and valid")
    else:
        info.update(state="mintable", message="Credentials present; a fresh token can be minted")
    return info


def save_credentials(api_key: str | None = None, api_secret: str | None = None,
                     totp_secret: str | None = None) -> dict:
    """Persist credentials to .env without touching other lines.

    Only non-empty arguments are written; pass "" to clear a value. The
    access-token cache and failure cooldown are reset so the next call mints
    with the new credentials immediately.
    """
    updates: dict[str, str] = {}
    if api_key is not None:
        updates["GROWW_TOTP_TOKEN"] = api_key.strip()
    if api_secret is not None:
        updates["GROWW_API_SECRET"] = api_secret.strip()
    if totp_secret is not None:
        updates["GROWW_TOTP_SECRET"] = totp_secret.strip()
    if not updates:
        return status()

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)

    with _fail_lock:
        _last_fail["ts"] = 0.0
        _last_fail["error"] = None
    invalidate()
    return status()
