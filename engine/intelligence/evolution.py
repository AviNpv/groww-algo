"""Deterministic, paper-only strategy evolution and promotion.

Safety model
------------
* Only four numeric leaves of ``config/strategy.yaml`` may be changed, and
  every change must survive both a strict "only safe paths changed" diff and
  the main API's strategy validator before a file is written.
* Backtests use the existing :func:`engine.backtest.run` engine. Costs,
  slippage, capital, leverage and execution/safety flags are never overridden.
* Temporal splits are enforced by giving the backtester a truncated view of
  historical daily bars (cutoff proxy). Every window sees only data available
  up to its own cutoff, so the out-of-sample window is never used for training.
  See ``limitations`` in ``run_once()`` output for the known approximation.
* Nothing is promoted automatically unless ``auto_promote`` is true *and* real
  shadow evaluations have been recorded via :meth:`record_shadow_evaluation`.
"""
from __future__ import annotations

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

from engine import settings
from engine.intelligence import config, storage
from engine.intelligence.models import ExecutionLog, StrategyVersion

# The only YAML leaves evolution is allowed to touch (raw V4 document paths).
SAFE_PATHS: tuple[tuple[str, ...], ...] = (
    ("exit", "initial_stop_loss", "atr_multiplier"),
    ("exit", "target", "target_r_mult"),
    ("exit", "trailing_stop", "activate_at_r"),
    ("risk", "minimum_risk_reward"),
)

# Deterministic multipliers. Targets and the top-level R:R floor may only move
# UP: lowering them below 2R is forbidden by the strategy safety contract.
_SAFE_FACTORS: dict[tuple[str, ...], tuple[float, ...]] = {
    SAFE_PATHS[0]: (0.9, 1.1, 0.8, 1.2),
    SAFE_PATHS[1]: (1.1, 1.25, 1.5),
    SAFE_PATHS[2]: (0.9, 1.1, 0.8, 1.2),
    SAFE_PATHS[3]: (1.1, 1.25, 1.5),
}
_MIN_RR = 2.0
_MIN_ENTRY_SCORE = 60
_TRAIN_DAYS = 10
_OOS_DAYS = 8
_WARMUP_SESSIONS = 60
_SYMBOL_LIMIT = 60
_WRITE_LOCK = threading.RLock()

_LIMITATIONS = (
    "engine.backtest.run() uses daily Yahoo bars and always anchors its window "
    "to the newest available session; evolution supplies per-window cutoffs by "
    "temporarily narrowing the backtester's yahoo data view, which is exact for "
    "daily bars but cannot model 5-minute intraday order flow.",
    "The existing backtester clamps days to 5..60 and uses the union of symbol "
    "trading dates; a symbol without a bar on a session is simply skipped.",
    "A concurrently started manual backtest could observe the temporary cutoff "
    "view for the few milliseconds of one window evaluation.",
    "risk.minimum_risk_reward is a declared safe key but the live strategy code "
    "reads entry.risk.minimum_risk_reward / entry.min_risk_reward instead, so "
    "changes to that key are behaviourally inert until the runtime changes.",
    "Shadow sessions are never invented: they must be recorded by a real "
    "shadow evaluator through record_shadow_evaluation().",
)


def _dotted(path: tuple[str, ...] | list[str]) -> str:
    return ".".join(str(part) for part in path)


def _now() -> str:
    return settings.now_ist().isoformat(timespec="seconds")


def _get(data: Any, path: tuple[str, ...], default=None):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _set(data: dict, path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def _leaves(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping; lists/scalars are treated as single values."""
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_leaves(value, path))
            else:
                out[path] = copy.deepcopy(value)
    else:
        out[prefix] = copy.deepcopy(data)
    return out


def load_active_yaml() -> str:
    """Return the exact raw strategy document currently on disk."""
    return settings.CONFIG_PATH.read_text(encoding="utf-8")


def _check_absolute_safety(data: Any) -> None:
    """Reject any document that is unsafe in absolute terms."""
    if not isinstance(data, dict):
        raise ValueError("strategy document is not a YAML mapping")
    mode = str(_get(data, ("app", "mode"), "paper")).lower()
    execution = str(_get(data, ("engine", "execution_mode"), "paper")).lower()
    if mode != "paper" or execution != "paper":
        raise ValueError("app.mode and engine.execution_mode must stay paper")
    score = _get(data, ("entry", "minimum_score"))
    if score is not None and float(score) != _MIN_ENTRY_SCORE:
        raise ValueError(f"entry.minimum_score must remain {_MIN_ENTRY_SCORE:g}")
    for path in (
        ("entry", "min_risk_reward"),
        ("entry", "risk", "minimum_risk_reward"),
        ("risk", "minimum_risk_reward"),
        ("exit", "target", "minimum_risk_reward"),
    ):
        value = _get(data, path)
        if value is not None and float(value) < _MIN_RR:
            raise ValueError(f"{_dotted(path)} must stay >= 2")
    if _get(data, ("execution", "live_trading", "allow_live_orders"), False):
        raise ValueError("execution.live_trading.allow_live_orders must stay false")
    for key in ("live_trading_disabled", "real_order_placement_disabled"):
        if _get(data, ("safety", key), True) is False:
            raise ValueError(f"safety.{key} must stay true")
    slippage = _get(data, ("costs", "slippage_pct"), 0)
    if slippage is None or float(slippage) <= 0:
        raise ValueError("costs.slippage_pct must stay positive")
    if _get(data, ("costs", "simulate_all_charges"), True) is False:
        raise ValueError("costs.simulate_all_charges must stay true")
    if _get(data, ("backtesting", "include_slippage"), True) is False:
        raise ValueError("backtesting.include_slippage must stay true")


def _assert_only_safe_changes(before: Any, after: Any) -> None:
    """Fail unless every changed leaf is one of SAFE_PATHS."""
    old, new = _leaves(before), _leaves(after)
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    allowed = {_dotted(path) for path in SAFE_PATHS}
    unexpected = sorted(changed - allowed)
    if unexpected:
        raise ValueError("unsafe config change: " + ", ".join(unexpected[:8]))
    _check_absolute_safety(after)


def _format_scalar(value: float) -> str:
    return f"{float(value):.6g}"


def _replace_yaml_scalar(text: str, path: tuple[str, ...], value: float) -> str:
    """Replace one numeric leaf in place, preserving comments and layout."""
    lines = text.splitlines()
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        key = stripped.split(":", 1)[0].strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = tuple(name for _, name in stack) + (key,)
        if current != tuple(path):
            stack.append((indent, key))
            continue
        head, colon, rest = line.partition(":")
        if not colon:
            return ""
        before_hash, hash_mark, comment = rest.partition("#")
        trailing = re.search(r"\s*$", before_hash).group(0)
        lines[index] = (
            f"{head}: {_format_scalar(value)}{trailing}"
            + (f"#{comment}" if hash_mark else "")
        )
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    raise ValueError(f"{_dotted(path)} not found in strategy.yaml")


def _validate_yaml_for_write(text: str) -> None:
    """Use the main API's validator when importable, else a conservative parse."""
    validator = None
    try:  # lazy: importing the API is optional and must not break startup
        from engine.api import _validate_strategy as validator  # type: ignore
    except Exception:
        validator = None
    if validator is not None:
        try:
            validator(text)
            return
        except Exception as exc:  # includes fastapi.HTTPException
            raise ValueError(f"strategy validation failed: {exc}") from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc
    _check_absolute_safety(parsed)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("strategy document is empty")


def _atomic_write_strategy(text: str, backup: bool = True) -> None:
    target = settings.CONFIG_PATH
    default = settings.CONFIG_DIR / "strategy.default.yaml"
    if target.resolve() == default.resolve() or target.name == "strategy.default.yaml":
        raise ValueError("refusing to write strategy.default.yaml")
    # Defence in depth: never rely on callers having checked absolute safety.
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error: {exc}") from exc
    _check_absolute_safety(parsed)
    _validate_yaml_for_write(text)
    with _WRITE_LOCK:
        backup_path = target.with_name(target.name + ".bak")
        tmp = target.with_name(target.name + ".tmp")
        if backup and target.exists():
            backup_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        settings.load_config(force=True)


class _CutoffYahoo:
    """Expose a backtester-local yahoo module whose daily() ends at ``cutoff``."""

    def __init__(self, real: Any, cutoff) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_cutoff", cutoff)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_real"), name)

    def daily(self, symbols: list[str], period: str = "1y") -> dict:
        frames = self._real.daily(symbols, period)
        cutoff = object.__getattribute__(self, "_cutoff")
        out: dict = {}
        for symbol, frame in (frames or {}).items():
            if frame is None or cutoff is None:
                out[symbol] = frame
                continue
            try:
                keep = 0
                for stamp in frame.index:
                    day = stamp.date() if hasattr(stamp, "date") else stamp
                    if day > cutoff:
                        break
                    keep += 1
                out[symbol] = frame.iloc[:keep] if keep else None
            except Exception:
                out[symbol] = frame
        return out


class StrategyEvolutionEngine:
    """Generate, validate, shadow-test and (rarely) promote strategy versions."""

    def __init__(self, store=storage) -> None:
        self.store = store
        self._lock = threading.RLock()

    @staticmethod
    def load_active_yaml() -> str:
        return load_active_yaml()

    # ------------------------------------------------------------------
    # candidate generation
    # ------------------------------------------------------------------
    def _generate_candidates(self, base_text: str, base_data: dict,
                             count: int) -> list[dict]:
        count = max(0, min(int(count), 24))
        out: list[dict] = []
        longest = max(len(factors) for factors in _SAFE_FACTORS.values())
        for round_index in range(longest):
            for path in SAFE_PATHS:
                if len(out) >= count:
                    return out
                factors = _SAFE_FACTORS[path]
                if round_index >= len(factors):
                    continue
                base_value = _get(base_data, path)
                if isinstance(base_value, bool) or not isinstance(base_value, (int, float)):
                    continue
                value = round(float(base_value) * factors[round_index], 4)
                if path == SAFE_PATHS[0]:
                    value = max(0.5, min(3.0, value))
                elif path == SAFE_PATHS[2]:
                    value = max(0.5, min(3.0, value))
                else:
                    value = max(_MIN_RR, value)
                if abs(value - float(base_value)) < 1e-9:
                    continue
                try:
                    text = _replace_yaml_scalar(base_text, path, value)
                    candidate_data = yaml.safe_load(text)
                    _assert_only_safe_changes(base_data, candidate_data)
                except Exception:
                    continue
                nested: dict = {}
                _set(nested, path, value)
                if path == SAFE_PATHS[0]:
                    nested.setdefault("exit", {})["stop_atr_mult"] = value
                elif path == SAFE_PATHS[1]:
                    nested.setdefault("exit", {})["target_r_mult"] = value
                elif path == SAFE_PATHS[2]:
                    nested.setdefault("exit", {})["trail_after_r"] = value
                changed = {_dotted(path): value}
                out.append({
                    "text": text,
                    "path": _dotted(path),
                    "value": value,
                    "changed": changed,
                    "overrides": nested,
                    "params": {
                        "changed": changed,
                        "base": {_dotted(p): _get(base_data, p) for p in SAFE_PATHS},
                        "deterministic": True,
                    },
                })
        return out

    # ------------------------------------------------------------------
    # walk-forward / out-of-sample validation
    # ------------------------------------------------------------------
    @staticmethod
    def _symbols() -> list[str] | None:
        try:
            from engine import universe
            return list(universe.load_symbols())[:_SYMBOL_LIMIT] or None
        except Exception:
            return None

    @staticmethod
    def _run_window(overrides: dict, days: int, cutoff, symbols: list[str] | None) -> dict:
        try:
            import engine.backtest as backtest
        except Exception as exc:  # pragma: no cover - optional dependency
            return {"ok": False, "error": f"backtester unavailable: {exc}",
                    "summary": None, "days": [], "trades": []}
        original = backtest.yahoo
        if cutoff is not None:
            backtest.yahoo = _CutoffYahoo(original, cutoff)
        try:
            return backtest.run(days=int(days), overrides=overrides, symbols=symbols)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "summary": None, "days": [], "trades": []}
        finally:
            backtest.yahoo = original

    @staticmethod
    def _metric_row(result: dict) -> dict:
        summary = result.get("summary") or {}
        try:
            return {
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "window": result.get("window"),
                "total_return_pct": round(float(summary.get("return_pct", 0) or 0), 3),
                "win_rate": round(float(summary.get("win_rate", 0) or 0), 2),
                "profit_factor": round(float(summary.get("profit_factor", 0) or 0), 3),
                "max_drawdown_pct": round(float(summary.get("max_drawdown_pct", 0) or 0), 3),
                "trades": int(summary.get("trades", 0) or 0),
            }
        except (TypeError, ValueError):
            return {"ok": False, "error": "malformed backtest summary",
                    "total_return_pct": 0.0, "win_rate": 0.0,
                    "profit_factor": 0.0, "max_drawdown_pct": 0.0, "trades": 0}

    def _validate_candidate(self, overrides: dict, limits: dict,
                            symbols: list[str] | None) -> dict:
        from engine import yahoo

        chosen = list(symbols or []) or (self._symbols() or [])
        frames = yahoo.daily(chosen, "2y") if chosen else {}
        dates = sorted({
            stamp.date() if hasattr(stamp, "date") else stamp
            for frame in (frames or {}).values() if frame is not None
            for stamp in frame.index
        })
        required = _WARMUP_SESSIONS + 2 * _TRAIN_DAYS + _OOS_DAYS
        if len(dates) < required:
            raise RuntimeError(
                f"insufficient history for walk-forward validation: "
                f"{len(dates)} sessions, need >= {required}"
            )
        oos_start = len(dates) - _OOS_DAYS
        recent_end = oos_start - 1
        recent_start = recent_end - _TRAIN_DAYS + 1
        old_end = recent_start - 1
        windows = (
            ("train_older", _TRAIN_DAYS, dates[old_end]),
            ("train_recent", _TRAIN_DAYS, dates[recent_end]),
            ("out_of_sample", _OOS_DAYS, dates[-1]),
        )
        results: dict[str, dict] = {}
        for name, days, cutoff in windows:
            result = self._run_window(overrides, days, cutoff, chosen or None)
            results[name] = self._metric_row(result)

        trains = [results["train_older"], results["train_recent"]]
        oos = results["out_of_sample"]
        train_returns = [float(row["total_return_pct"]) for row in trains]
        mean_train = sum(train_returns) / len(train_returns)
        efficiency = round(float(oos["total_return_pct"]) / mean_train, 3) if mean_train > 0 else 0.0
        max_dd = max(float(row["max_drawdown_pct"]) for row in results.values())
        min_oos = float(limits.get("min_oos_return_pct", 0.5))
        min_eff = float(limits.get("min_walk_forward_efficiency", 0.6))
        max_dd_limit = float(limits.get("max_drawdown_pct", 10.0))
        errors = [f"{name}: {row['error']}" for name, row in results.items()
                  if not row["ok"] or row["error"]]
        passed = (
            not errors
            and oos["trades"] > 0
            and float(oos["total_return_pct"]) >= min_oos
            and efficiency >= min_eff
            and max_dd <= max_dd_limit
            and all(row["trades"] > 0 for row in trains)
        )
        reasons: list[str] = list(errors)
        if oos["trades"] <= 0:
            reasons.append("no out-of-sample trades")
        if float(oos["total_return_pct"]) < min_oos:
            reasons.append("out-of-sample return below threshold")
        if efficiency < min_eff:
            reasons.append("walk-forward efficiency below threshold")
        if max_dd > max_dd_limit:
            reasons.append("drawdown above threshold")
        if any(row["trades"] <= 0 for row in trains):
            reasons.append("no trades in a training window")
        return {
            "total_return_pct": oos["total_return_pct"],
            "win_rate": oos["win_rate"],
            "profit_factor": oos["profit_factor"],
            "max_drawdown_pct": max_dd,
            "trades": oos["trades"],
            "train": trains,
            "out_of_sample": oos,
            "walk_forward_efficiency": efficiency,
            "mean_train_return_pct": round(mean_train, 3),
            "windows": [{"name": n, "days": d, "cutoff": c.date().isoformat()
                         if hasattr(c, "date") else str(c)} for n, d, c in windows],
            "thresholds": {
                "min_oos_return_pct": min_oos,
                "min_walk_forward_efficiency": min_eff,
                "max_drawdown_pct": max_dd_limit,
            },
            "costs_disabled": False,
            "validation_passed": bool(passed),
            "validation_reasons": reasons[:8],
        }

    # ------------------------------------------------------------------
    # versions / shadow / promotion
    # ------------------------------------------------------------------
    def _save(self, version: StrategyVersion) -> None:
        self.store.save_version(version)

    def ensure_baseline(self) -> dict | None:
        """Seed the current strategy as the rollback target before promotion.

        Without this, the first promotion has no parent and cannot be undone.
        The baseline is the exact on-disk YAML and is never rewritten here.
        """
        with self._lock:
            active = self.store.active_version()
            if active:
                return active
            try:
                for row in self.store.versions(limit=200):
                    metrics = row.get("metrics") or {}
                    if metrics.get("baseline") and row.get("status") in (
                            "active", "validated", "rolled_back"):
                        self.store.set_version_status(row["id"], "active")
                        return self.store.get_version(row["id"])
            except Exception:
                pass
            text = load_active_yaml()
            parsed = yaml.safe_load(text)
            _check_absolute_safety(parsed)
            baseline = StrategyVersion(
                ts=_now(),
                status="active",
                yaml=text,
                params={"baseline": True},
                metrics={
                    "baseline": True,
                    "validation_passed": True,
                    "source": "config/strategy.yaml",
                },
                rationale="baseline snapshot before intelligence promotion",
            )
            self.store.save_version(baseline)
            return self.store.get_version(baseline.version_id)

    @staticmethod
    def _from_row(row: dict, metrics: dict | None = None,
                  status: str | None = None) -> StrategyVersion:
        return StrategyVersion(
            version_id=row["id"],
            ts=row["ts"],
            status=status or row["status"],
            yaml=row["yaml"],
            params=row.get("params") if isinstance(row.get("params"), dict) else {},
            metrics=metrics if metrics is not None else (row.get("metrics") or {}),
            parent_version_id=row.get("parent_version_id"),
            rationale=row.get("rationale") or "",
        )

    def record_shadow_evaluation(self, version_id: str, ok: bool = True,
                                 detail: str = "") -> dict:
        """Record one real shadow evaluation. Nothing increments this by itself."""
        with self._lock:
            row = self.store.get_version(version_id)
            if not row or row.get("status") != "shadow":
                raise ValueError("shadow version not found")
            metrics = dict(row.get("metrics") or {})
            if not metrics.get("validation_passed"):
                raise ValueError("version has no successful validation")
            completed = int(metrics.get("shadow_sessions_completed", 0) or 0)
            required = int(metrics.get("shadow_sessions_required",
                                       config.evolution().get("shadow_sessions", 5)) or 5)
            evidence = list(metrics.get("shadow_evidence") or [])
            evidence.append({"ts": _now(), "ok": bool(ok), "detail": str(detail)[:300]})
            metrics["shadow_evidence"] = evidence[-20:]
            if ok:
                completed += 1
            else:
                metrics["shadow_failures"] = int(metrics.get("shadow_failures", 0) or 0) + 1
            metrics["shadow_sessions_completed"] = completed
            metrics["shadow_sessions_required"] = required
            self._save(self._from_row(row, metrics=metrics, status="shadow"))
            try:
                self.store.save_execution_log(ExecutionLog(
                    ts=_now(),
                    event=f"shadow_evaluation:{version_id}",
                    detail=f"ok={bool(ok)} {str(detail)[:250]}".strip(),
                    paper_only=True,
                ))
            except Exception:
                pass  # the version metrics are the source of truth
            return {
                "ok": True,
                "version_id": version_id,
                "completed": completed,
                "required": required,
                "ready": completed >= required,
            }

    def promote(self, version_id: str, require_shadow: bool = True,
                allow_rolled_back: bool = False) -> dict:
        """Atomically install a validated version's YAML as the live strategy."""
        with self._lock:
            self.ensure_baseline()
            row = self.store.get_version(version_id)
            if not row:
                raise ValueError("version not found")
            allowed = {"validated", "shadow", "active"}
            if allow_rolled_back:
                allowed.add("rolled_back")
            if row.get("status") not in allowed:
                raise ValueError(f"version status {row.get('status')} is not promotable")
            metrics = row.get("metrics") or {}
            if not metrics.get("validation_passed"):
                raise ValueError("version has no successful validation evidence")
            if require_shadow:
                done = int(metrics.get("shadow_sessions_completed", 0) or 0)
                need = int(metrics.get("shadow_sessions_required",
                                       config.evolution().get("shadow_sessions", 5)) or 5)
                if done < need:
                    raise ValueError(f"insufficient shadow evidence: {done}/{need}")
            text = row.get("yaml") or ""
            if not text.strip():
                raise ValueError("version has no YAML payload")
            _check_absolute_safety(yaml.safe_load(text))
            previous = self.store.active_version()
            if previous and previous.get("id") != version_id:
                base_text = str(previous.get("yaml") or "")
                if not base_text.strip():
                    raise ValueError("baseline version has no YAML payload")
                _assert_only_safe_changes(yaml.safe_load(base_text), yaml.safe_load(text))
            _atomic_write_strategy(text)
            if previous and previous.get("id") != version_id:
                self.store.set_version_status(previous["id"], "rolled_back")
            self.store.set_version_status(version_id, "active")
            return {
                "ok": True,
                "status": "promoted",
                "version_id": version_id,
                "previous_active": previous.get("id") if previous else None,
                "ts": _now(),
                "config_reloaded": True,
            }

    def _promote_ready(self, ev: dict) -> dict:
        default_required = int(ev.get("shadow_sessions", 5) or 5)
        auto_promote = bool(ev.get("auto_promote"))
        promoted: list[dict] = []
        awaiting: list[dict] = []
        try:
            rows = self.store.versions(limit=200)
        except Exception:
            rows = []
        for row in rows:
            if row.get("status") != "shadow":
                continue
            metrics = row.get("metrics") or {}
            if not metrics.get("validation_passed"):
                continue
            done = int(metrics.get("shadow_sessions_completed", 0) or 0)
            need = int(metrics.get("shadow_sessions_required", default_required) or default_required)
            if auto_promote and done >= need:
                try:
                    promoted.append(self.promote(row["id"], require_shadow=True))
                except Exception as exc:
                    awaiting.append({"version_id": row["id"],
                                     "error": f"{type(exc).__name__}: {exc}"[:300]})
            else:
                awaiting.append({"version_id": row["id"], "completed": done, "required": need,
                                 "reason": "auto_promote_disabled" if not auto_promote
                                 else "insufficient_shadow_evidence"})
        if promoted:
            status = "promoted"
        elif awaiting:
            status = "awaiting_shadow"
        else:
            status = "auto_promote_disabled" if not auto_promote else "no_candidates"
        return {
            "status": status,
            "auto_promote": auto_promote,
            "promoted": promoted,
            "awaiting": awaiting,
        }

    def restore_from_backup(self) -> dict:
        """Restore ``strategy.yaml`` from the atomic-write ``.bak`` file.

        This is the recovery path when the intelligence store is unavailable or
        was reset after a promotion. The backup is validated with the same
        absolute safety guard as any other write and is not overwritten while
        it is being restored.
        """
        with self._lock:
            target = settings.CONFIG_PATH
            backup_path = target.with_name(target.name + ".bak")
            if not backup_path.exists():
                return {
                    "ok": False,
                    "status": "no_backup",
                    "message": "no strategy.yaml.bak recovery file exists",
                    "ts": _now(),
                }
            try:
                text = backup_path.read_text(encoding="utf-8")
                if not text.strip():
                    raise ValueError("backup file is empty")
                _check_absolute_safety(yaml.safe_load(text))
                _atomic_write_strategy(text, backup=False)
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "restore_failed",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "ts": _now(),
                }
            return {
                "ok": True,
                "status": "restored_from_backup",
                "source": backup_path.name,
                "ts": _now(),
                "config_reloaded": True,
            }

    def rollback(self, version_id: str | None = None) -> dict:
        with self._lock:
            current = self.store.active_version()
            current_id = current.get("id") if current else None
            target = None
            if version_id:
                target = self.store.get_version(version_id)
                if not target:
                    return {"ok": False, "status": "not_found",
                            "message": "version not found"}
                if target["id"] == current_id:
                    return {"ok": True, "status": "already_active",
                            "version_id": current_id, "ts": _now()}
            elif current and current.get("parent_version_id"):
                parent = self.store.get_version(str(current["parent_version_id"]))
                if parent and parent["id"] != current_id and parent.get("status") != "active":
                    target = parent
            if target is None:
                try:
                    rows = self.store.versions(limit=200)
                except Exception:
                    rows = []
                for row in rows:
                    if row["id"] == current_id:
                        continue
                    if row.get("status") in ("validated", "active", "rolled_back"):
                        target = row
                        break
            if target is None:
                restored = self.restore_from_backup()
                if restored.get("ok"):
                    restored["status"] = "rolled_back"
                    restored["rolled_back_from"] = current_id
                return restored
            result = self.promote(target["id"], require_shadow=False, allow_rolled_back=True)
            result["status"] = "rolled_back"
            result["rolled_back_from"] = current_id
            return result

    # ------------------------------------------------------------------
    # main entry point
    # ------------------------------------------------------------------
    def run_once(self) -> dict:
        with self._lock:
            ev = config.evolution()
            limits = {key: ev.get(key) for key in (
                "min_oos_return_pct", "min_walk_forward_efficiency",
                "max_drawdown_pct", "shadow_sessions", "candidate_count")}
            zero_counts = {
                "generated": 0, "validated": 0, "rejected": 0,
                "shadow": 0, "promoted": 0, "errors": 0,
            }
            if not bool(ev.get("research_enabled")):
                return {
                    "ok": True,
                    "status": "skipped",
                    "reason": "evolution.research_enabled is false",
                    "ts": _now(),
                    "counts": dict(zero_counts),
                    "promotion": {"status": "skipped", "promoted": [], "awaiting": []},
                    "limitations": list(_LIMITATIONS),
                }
            try:
                self.store.init_db()
                self.ensure_baseline()
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "enabled",
                    "error": f"storage init failed: {type(exc).__name__}: {exc}"[:300],
                    "ts": _now(),
                    "counts": dict(zero_counts),
                    "promotion": {"status": "skipped", "promoted": [], "awaiting": []},
                    "limitations": list(_LIMITATIONS),
                }

            counts = dict(zero_counts)
            promotion = self._promote_ready(ev)
            try:
                symbols = self._symbols()
                base_text = load_active_yaml()
                base_data = yaml.safe_load(base_text)
                if not isinstance(base_data, dict):
                    raise ValueError("active strategy.yaml is not a mapping")
                active = self.store.active_version()
                parent_id = active.get("id") if active else None
                candidates = self._generate_candidates(
                    base_text, base_data, int(ev.get("candidate_count", 4) or 0)
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "enabled",
                    "error": f"candidate generation failed: {type(exc).__name__}: {exc}"[:300],
                    "ts": _now(),
                    "counts": counts,
                    "promotion": promotion,
                    "limitations": list(_LIMITATIONS),
                }
            counts["generated"] = len(candidates)
            for candidate in candidates:
                version = StrategyVersion(
                    ts=_now(),
                    status="candidate",
                    yaml=candidate["text"],
                    params=candidate["params"],
                    metrics={"validation_passed": False, "changed": candidate["changed"]},
                    parent_version_id=parent_id,
                    rationale=f"deterministic mutation of {candidate['path']} "
                              f"-> {_format_scalar(candidate['value'])}",
                )
                try:
                    self._save(version)
                except Exception:
                    counts["errors"] += 1
                    continue
                try:
                    metrics = self._validate_candidate(candidate["overrides"], ev, symbols)
                    metrics["changed"] = candidate["changed"]
                    metrics["shadow_sessions_completed"] = 0
                    metrics["shadow_sessions_required"] = int(ev.get("shadow_sessions", 5) or 5)
                    version.metrics = metrics
                    if not metrics.get("validation_passed"):
                        # Never let a failed backtest/walk-forward result enter
                        # the shadow queue. It stays visible as rejected evidence.
                        version.status = "rejected"
                        self._save(version)
                        counts["rejected"] += 1
                        continue
                    version.status = "validated"
                    self._save(version)
                    counts["validated"] += 1
                    version.status = "shadow"
                    self._save(version)
                    counts["shadow"] += 1
                except Exception as exc:
                    counts["rejected"] += 1
                    version.status = "rejected"
                    version.metrics = {
                        "validation_passed": False,
                        "changed": candidate["changed"],
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                    }
                    try:
                        self._save(version)
                    except Exception:
                        counts["errors"] += 1
            promotion = self._promote_ready(ev)
            counts["promoted"] = len(promotion["promoted"])
            return {
                "ok": True,
                "status": "enabled",
                "ts": _now(),
                "counts": counts,
                "candidate_count_requested": int(ev.get("candidate_count", 4) or 0),
                "symbols_evaluated": len(symbols or []),
                "limits": limits,
                "promotion": promotion,
                "limitations": list(_LIMITATIONS),
            }
