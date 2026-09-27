"""Стоп магазина на первом отказе.

Магазин, который ответил 401/403/418/429, капчей или стеной антибота, не получает
ни одного запроса до конца кулдауна. Состояние лежит в ``${CLAUDE_PLUGIN_DATA}``,
чтобы перезапуск сервера или новая сессия не обнуляли стоп.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from fastmcp.exceptions import ToolError

from mcp_core.errors import ConnectorError, ErrorCode
from mcp_core.plugin_data import plugin_data_dir
from mcp_core.redact import redact_error_text

__all__ = ["DEFAULT_COOLDOWN_S", "SourceStoppedError", "StopGate", "default_gate", "is_refusal"]

DEFAULT_COOLDOWN_S = 1800.0
_STATE_FILE = "stopped_sources.json"
_REFUSAL_CODES = frozenset({ErrorCode.RATE_LIMITED, ErrorCode.CHALLENGE_REQUIRED, ErrorCode.PERMISSION_DENIED})
_REFUSAL_STATUSES = frozenset({401, 403, 418, 429})


class SourceStoppedError(ConnectorError):
    """Магазин на стопе после отказа; запрос не отправлялся."""

    def __init__(self, source: str, *, reason: str, retry_after_s: int) -> None:
        super().__init__(
            ErrorCode.RATE_LIMITED,
            f"{source} stopped after a refusal ({reason}); no request sent, retry after {retry_after_s}s",
            provider=source,
            retry_after_s=retry_after_s,
        )

    def to_dict(self) -> dict:
        return {**super().to_dict(), "source_stopped": True}


def is_refusal(error: BaseException) -> bool:
    if isinstance(error, ConnectorError):
        payload = error.to_dict()
    elif isinstance(error, ToolError):
        try:
            payload = json.loads(str(error))
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
    else:
        return False
    if payload.get("source_stopped"):
        return False
    return payload.get("error") in _REFUSAL_CODES or payload.get("status_code") in _REFUSAL_STATUSES


class StopGate:
    def __init__(self, data_dir: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = data_dir / _STATE_FILE if data_dir is not None else None
        self._clock = clock
        self._memory: dict[str, dict[str, float | str]] = {}

    def check(self, source: str) -> None:
        entry = self._load().get(source)
        if entry is None:
            return
        left = float(entry["until"]) - self._clock()
        if left > 0:
            raise SourceStoppedError(source, reason=str(entry["reason"]), retry_after_s=round(left))

    def trip(self, source: str, reason: str, *, cooldown_s: float = DEFAULT_COOLDOWN_S) -> None:
        state = self._load()
        state[source] = {"until": self._clock() + cooldown_s, "reason": redact_error_text(reason, max_len=120)}
        self._save(state)

    def clear(self, source: str) -> None:
        state = self._load()
        if state.pop(source, None) is not None:
            self._save(state)

    def status(self) -> dict[str, dict[str, object]]:
        now = self._clock()
        return {
            source: {"reason": entry["reason"], "retry_after_s": round(float(entry["until"]) - now)}
            for source, entry in self._load().items()
            if float(entry["until"]) > now
        }

    def _load(self) -> dict[str, dict[str, float | str]]:
        if self._path is None:
            return dict(self._memory)
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            source: {"until": float(entry["until"]), "reason": str(entry.get("reason", ""))}
            for source, entry in raw.items()
            if isinstance(entry, dict) and isinstance(entry.get("until"), int | float)
        }

    def _save(self, state: dict[str, dict[str, float | str]]) -> None:
        now = self._clock()
        live = {source: entry for source, entry in state.items() if float(entry["until"]) > now}
        if self._path is None:
            self._memory = live
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)


_default: StopGate | None = None


def default_gate() -> StopGate:
    global _default
    if _default is None:
        _default = StopGate(plugin_data_dir())
    return _default
