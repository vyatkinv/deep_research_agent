"""Session logger — пишет JSONL-лог общения агента с моделью.

Формат каждой строки:
  {"ts": "ISO8601", "event": "<тип>", ...поля...}

Типы событий:
  session_start  — начало сессии
  user           — сообщение пользователя
  tool_call      — вызов инструмента агентом
  tool_result    — результат инструмента
  ai             — ответ модели
  tokens         — статистика токенов за один вызов
  compact        — сжатие истории
  skill          — активирован скилл
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class SessionLogger:
    """Пишет JSONL-лог в logs/<дата>_<session_id>.jsonl."""

    def __init__(self, logs_dir: str, session_id: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.log_path: Optional[Path] = None

        if not enabled:
            return

        log_dir = Path(logs_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_id = session_id[:8]
        self.log_path = log_dir / f"session_{ts}_{short_id}.jsonl"

        self._write("session_start", session_id=session_id)

    # ─────────────────────── public API ────────────────────────────────

    def log_user(self, content: str, skill: Optional[str] = None) -> None:
        self._write("user", content=content, skill=skill)

    def log_ai(
        self,
        content: str,
        tokens: Optional[Dict[str, int]] = None,
        skill: Optional[str] = None,
    ) -> None:
        self._write("ai", content=content, tokens=tokens, skill=skill)

    def log_tool_call(self, name: str, args: Dict[str, Any]) -> None:
        safe_args = {k: str(v)[:300] for k, v in args.items()}
        self._write("tool_call", name=name, args=safe_args)

    def log_tool_result(self, name: str, content: str, tool_call_id: str = "") -> None:
        preview = content[:800]
        truncated = len(content) > 800
        self._write("tool_result", name=name, content=preview,
                    truncated=truncated, tool_call_id=tool_call_id)

    def log_tokens(self, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        self._write("tokens", input=input_tokens, output=output_tokens, total=total_tokens)

    def log_compact(self, from_count: int, to_count: int, summarized: int) -> None:
        self._write("compact", from_count=from_count, to_count=to_count, summarized=summarized)

    def log_skill(self, skill_name: str, auto: bool = True) -> None:
        self._write("skill", name=skill_name, auto=auto)

    def new_session(self, session_id: str) -> None:
        self._write("session_start", session_id=session_id, note="reset")

    # ─────────────────────── internal ──────────────────────────────────

    def _write(self, event: str, **kwargs: Any) -> None:
        if not self.enabled or self.log_path is None:
            return
        entry = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event}
        entry.update({k: v for k, v in kwargs.items() if v is not None})
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # лог не критичен
