"""Bounded OpenAI-compatible model adapter for the runtime intelligence layer.

No secret value is ever read from the environment except the single variable
named by ``api_key_env``. Credentials are never placed in request payloads,
error messages, usage records or logs. The configured provider/model is used
as-is: this adapter never falls back to another provider or a paid model.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from engine.intelligence import config as ai_config
from engine.intelligence import storage
from engine.intelligence.models import ModelHealth, ModelResult, ModelUsage
from engine.settings import now_ist

T = TypeVar("T", bound=BaseModel)

_MAX_ERROR_CHARS = 300
_MAX_RETRY_WAIT = 5.0
_MAX_BACKOFF = 5.0
_MAX_RESPONSE_BYTES = 1_000_000


def _now() -> str:
    """IST ISO timestamp with a safe fallback if the settings clock fails."""
    try:
        return now_ist().isoformat(timespec="seconds")
    except Exception:  # pragma: no cover - defensive only
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and result not in (float("inf"), float("-inf")) else default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_text(value: Any, secret: str | None = None) -> str:
    """Cap and redact any text that might be surfaced in a result or usage row."""
    text = str(value if value is not None else "")
    if secret:
        text = text.replace(secret, "[redacted]")
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + "..."
    return text


class _RateLimiter:
    """Small thread-safe sliding-window limiter for outbound model calls."""

    def __init__(self, per_minute: int) -> None:
        self._limit = max(0, _as_int(per_minute, 30))
        self._lock = threading.Lock()
        self._calls: list[float] = []

    def acquire(self) -> None:
        if self._limit <= 0:
            return  # a non-positive limit means "no artificial limit"
        while True:
            with self._lock:
                moment = time.monotonic()
                cutoff = moment - 60.0
                self._calls = [stamp for stamp in self._calls if stamp > cutoff]
                if len(self._calls) < self._limit:
                    self._calls.append(moment)
                    return
                wait = max(0.0, self._calls[0] + 60.0 - moment)
            if wait > 0:
                time.sleep(wait)


class ModelAdapter:
    """Call exactly one configured OpenAI-compatible endpoint or local endpoint."""

    def __init__(self, config: dict | None = None, store=storage) -> None:
        source = ai_config.model()
        if isinstance(config, dict):
            source.update(config)
        # Safety invariant, repeated here even though config.model() forces it.
        source["allow_paid"] = False
        self.config_error = "; ".join(ai_config.validate_model_settings(source))
        self.config: dict[str, Any] = source
        self.store = store

        self.provider = str(source.get("provider") or "openai_compatible")
        self.timeout_seconds = min(120.0, max(1.0, _as_float(source.get("timeout_seconds"), 20.0)))
        self.retries = max(0, min(4, _as_int(source.get("retries"), 2)))
        self.rate_limiter = _RateLimiter(source.get("rate_limit_per_minute"))
        self._key_env = str(source.get("api_key_env") or "")

        local = source.get("local_endpoint")
        if local:
            self.base_url = str(local).strip().rstrip("/")
            self.model_name = str(source.get("local_model") or source.get("model") or "local-model")
            self._uses_local = True
        else:
            self.base_url = str(source.get("base_url") or "").strip().rstrip("/")
            self.model_name = str(source.get("model") or "")
            self._uses_local = False
        self.model = self.model_name  # convenience alias for callers
        if self.config_error:
            # Never send a credential or a request to a rejected endpoint.
            self._key_env = ""
            self.base_url = ""

    # ------------------------------------------------------------------ helpers
    def _api_key(self) -> str | None:
        """Read only one allowlisted environment variable, never all of them."""
        if not self._key_env or self._key_env not in ai_config.ALLOWED_KEY_ENV:
            return None
        try:
            value = os.environ.get(self._key_env)
        except Exception:  # pragma: no cover - defensive only
            return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _headers(self, api_key: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        # A hosted credential is never forwarded to a user-configured local URL.
        if api_key and not self._uses_local:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _request_body(self, system: str, user_payload: dict) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": str(system)},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
            ],
            "temperature": min(2.0, max(0.0, _as_float(self.config.get("temperature"), 0.1))),
            "max_tokens": max(1, _as_int(self.config.get("max_tokens"), 1200)),
        }
        if bool(self.config.get("structured")):
            body["response_format"] = {"type": "json_object"}
        return body

    def _post(self, body: dict[str, Any], api_key: str | None, timeout: float) -> dict[str, Any]:
        if not self.base_url:
            raise ValueError("model endpoint is not configured")
        url = self.base_url + "/chat/completions"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, headers=self._headers(api_key), method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE_BYTES)
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise ValueError("model response is not a JSON object")
        return parsed

    @staticmethod
    def _content_text(response: dict[str, Any]) -> str:
        try:
            choices = response.get("choices") or []
            content = (choices[0] or {}).get("message", {}).get("content")
        except (AttributeError, IndexError, KeyError, TypeError):
            raise ValueError("malformed model response") from None
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            content = "".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty model response")
        return content

    @staticmethod
    def _parse_object(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("model response is not a JSON object")
        return parsed

    def _usage(
        self,
        response: dict[str, Any] | None,
        ok: bool,
        latency_ms: int,
        error: str | None,
        correlation_id: str | None,
    ) -> ModelUsage:
        raw = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(raw, dict):
            raw = {}
        prompt = _as_int(raw.get("prompt_tokens"), 0) or None
        completion = _as_int(raw.get("completion_tokens"), 0) or None
        total = _as_int(raw.get("total_tokens"), 0) or None
        if total is None and (prompt is not None or completion is not None):
            total = (prompt or 0) + (completion or 0)
        return ModelUsage(
            provider=self.provider,
            model=self.model_name,
            ts=_now(),
            ok=bool(ok),
            latency_ms=max(0, int(latency_ms)),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            error=error,
            correlation_id=correlation_id,
        )

    def _save_usage(self, usage: ModelUsage) -> None:
        try:
            self.store.save_model_usage(usage)
        except Exception:
            pass  # telemetry must never break an advisory model call

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def _backoff(self, attempt: int) -> float:
        return min(_MAX_BACKOFF, 0.5 * (2 ** max(0, attempt)))

    def _retry_after(self, exc: urllib.error.HTTPError) -> float:
        headers = getattr(exc, "headers", None)
        raw = headers.get("Retry-After") if headers is not None else None
        try:
            seconds = float(str(raw))
        except (TypeError, ValueError):
            return self._backoff(0)  # HTTP-date form is not supported
        if seconds <= 0:
            return 0.0
        return min(seconds, _MAX_RETRY_WAIT)

    def _failure(
        self,
        message: str,
        started: float,
        correlation_id: str | None = None,
        secret: str | None = None,
    ) -> ModelResult:
        safe = _safe_text(message, secret)
        usage = self._usage(None, False, self._elapsed_ms(started), safe, correlation_id)
        self._save_usage(usage)
        return ModelResult(ok=False, data=None, usage=usage, error=safe)

    # ------------------------------------------------------------------- public
    def complete(
        self,
        schema: type[T],
        system: str,
        user_payload: dict,
        agent_id: str,
        correlation_id: str | None = None,
    ) -> ModelResult:
        """Request one JSON object and validate it against ``schema``.

        Retries malformed output and transient HTTP errors; always records a
        ModelUsage row and never raises.
        """
        del agent_id  # accepted for interface symmetry; nothing agent-specific is sent
        started = time.monotonic()
        if self.config_error:
            return self._failure(self.config_error, started, correlation_id, None)
        key = self._api_key()
        if not self.base_url:
            return self._failure("model endpoint is not configured", started, correlation_id, key)
        if not self._uses_local and not key:
            name = self._key_env or "(unset)"
            return self._failure(
                f"API key environment variable {name} is not set", started, correlation_id, key
            )
        try:
            body = self._request_body(system, user_payload)
        except Exception as exc:
            return self._failure(
                f"invalid request payload: {_safe_text(exc, key)}", started, correlation_id, key
            )

        attempts = self.retries + 1
        last_error = "model call failed"
        for attempt in range(attempts):
            wait = 0.0
            try:
                self.rate_limiter.acquire()
                response = self._post(body, key, self.timeout_seconds)
                parsed = self._parse_object(self._content_text(response))
                validated = schema.model_validate(parsed)
                usage = self._usage(
                    response, True, self._elapsed_ms(started), None, correlation_id
                )
                self._save_usage(usage)
                return ModelResult(ok=True, data=validated.model_dump(), usage=usage)
            except urllib.error.HTTPError as exc:
                code = int(getattr(exc, "code", 0) or 0)
                if code in (401, 403):
                    # Authentication failure is not retryable and is never echoed verbatim.
                    return self._failure(
                        f"model endpoint rejected credentials (HTTP {code})",
                        started,
                        correlation_id,
                        key,
                    )
                if code == 429:
                    last_error = "rate limited by model endpoint (HTTP 429)"
                    wait = self._retry_after(exc)
                else:
                    last_error = f"model endpoint returned HTTP {code}"
                    wait = self._backoff(attempt)
            except urllib.error.URLError as exc:
                reason = type(getattr(exc, "reason", exc)).__name__
                last_error = f"network error: {reason}"
                wait = self._backoff(attempt)
            except ValidationError:
                last_error = "model output did not match the required schema"
                wait = self._backoff(attempt)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                last_error = f"malformed model output: {_safe_text(exc, key)}"
                wait = self._backoff(attempt)
            except Exception as exc:
                last_error = f"model call failed: {_safe_text(exc, key)}"
                wait = self._backoff(attempt)

            if attempt + 1 < attempts and wait > 0:
                time.sleep(wait)

        return self._failure(last_error, started, correlation_id, key)

    def health(self, record: bool = True) -> ModelHealth:
        """Bounded reachability check. Never raises and never saves a secret.

        ``record=False`` is used by dashboard health polling so repeated status
        refreshes do not fill the usage log with non-trading probe rows.
        """
        started = time.monotonic()
        key = self._api_key()

        def result(ok: bool, message: str, response: dict[str, Any] | None = None) -> ModelHealth:
            latency = self._elapsed_ms(started)
            safe = _safe_text(message, key)
            if record:
                usage = self._usage(
                    response, ok, latency, None if ok else safe, None
                )
                self._save_usage(usage)
            return ModelHealth(
                ok=bool(ok),
                provider=self.provider,
                model=self.model_name,
                ts=_now(),
                latency_ms=latency,
                message=safe,
                paid_allowed=False,
            )

        try:
            if self.config_error:
                return result(False, self.config_error)
            if not self.base_url:
                return result(False, "Model endpoint is not configured")
            if not self._uses_local and not key:
                name = self._key_env or "(unset)"
                return result(
                    False,
                    f"API key environment variable {name} is not set and no local_endpoint is configured",
                )
            self.rate_limiter.acquire()
            probe = {
                "model": self.model_name,
                "messages": [{"role": "user", "content": 'Reply with {"ok": true}.'}],
                "temperature": 0,
                "max_tokens": 8,
            }
            response = self._post(probe, key, min(self.timeout_seconds, 3.0))
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                return result(False, "model endpoint returned an empty response", response)
            return result(True, "model endpoint reachable", response)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            return result(False, f"model endpoint returned HTTP {code}")
        except Exception as exc:
            return result(False, f"model probe failed: {_safe_text(exc, key)}")
