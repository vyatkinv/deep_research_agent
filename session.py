"""LangGraph agent session with persistent conversation memory.

AgentSession оборачивает LangGraph ReAct-агент с MemorySaver:
- Вся история диалога сохраняется внутри сессии (в памяти процесса).
- Каждый новый session.reset() начинает новую ветку разговора.
- Subgraph-скиллы (fix_build, fix_tests) запускаются изолированно,
  но результат добавляется в основной тред как сообщение.
"""

from __future__ import annotations

import os
import sys
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

from config import AppConfig, GigaChatConfig, JavaConfig, JavaPromptsConfig, SkillConfig

DEFAULT_SYSTEM_PROMPT = """\
Ты — опытный Java-разработчик (Senior/Principal уровень).
Отвечай на русском языке если задача поставлена по-русски.
Пиши только чистый, production-ready код.
Используй современный Java (17+).

Перед любым изменением кода — сначала прочитай существующие файлы.
После написания кода — компилируй и запускай тесты.
"""


def _apply_api_env(cfg: AppConfig) -> None:
    key = cfg.api.resolved_api_key()
    url = cfg.api.resolved_base_url()
    if key:
        os.environ["OPENAI_API_KEY"] = key
    if url:
        os.environ["OPENAI_BASE_URL"] = url


def _build_llm(model_name: str, cfg: AppConfig) -> Any:
    """Создать LLM по имени модели.

    Поддерживаемые форматы:
      gigachat:GigaChat-Pro   → langchain_gigachat.GigaChat
      openai:gpt-4.1          → init_chat_model (LangChain universal)
      gpt-4.1                 → init_chat_model (LangChain universal)
    """
    if model_name.startswith("gigachat:"):
        model_id = model_name[len("gigachat:"):]
        try:
            from langchain_gigachat import GigaChat  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "Установи пакет: pip install langchain-gigachat"
            ) from exc

        gc_cfg: GigaChatConfig = cfg.gigachat or GigaChatConfig()
        creds = gc_cfg.resolved_credentials()
        if not creds:
            raise ValueError(
                "GigaChat credentials не заданы. "
                "Укажи gigachat.credentials в конфиге или GIGACHAT_CREDENTIALS в .env"
            )

        kwargs: Dict[str, Any] = {
            "credentials": creds,
            "scope": gc_cfg.scope,
            "model": model_id,
            "verify_ssl_certs": gc_cfg.verify_ssl_certs,
        }
        if gc_cfg.base_url:
            kwargs["base_url"] = gc_cfg.base_url
        return GigaChat(**kwargs)

    # Универсальный LangChain провайдер (openai, anthropic, google и т.д.)
    from langchain.chat_models import init_chat_model  # type: ignore
    return init_chat_model(model_name)


async def _load_mcp_tools(cfg: AppConfig) -> List[Any]:
    if not cfg.mcp.servers:
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore
    except ImportError:
        print("WARN: langchain-mcp-adapters не установлен — MCP пропущен.", file=sys.stderr)
        return []

    connections: Dict[str, Any] = {}
    for srv in cfg.mcp.servers:
        if srv.transport == "stdio":
            connections[srv.name] = {
                "transport": "stdio",
                "command": srv.command,
                "args": srv.args,
                "env": {**os.environ, **srv.env} if srv.env else None,
            }
        elif srv.transport == "sse":
            connections[srv.name] = {
                "transport": "sse",
                "url": srv.url,
                "headers": srv.headers or {},
            }
    client = MultiServerMCPClient(connections)
    return await client.get_tools()


class AgentSession:
    """Persistent LangGraph conversation session.

    Создаётся один раз при старте CLI. Хранит историю разговора
    и кэширует построенные графы.
    """

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.thread_id: str = str(uuid4())

        # Ленивая инициализация
        self._graph: Any = None
        self._tools: List[Any] = []
        self._subgraphs: Dict[str, Any] = {}   # skill_name → compiled subgraph

        # AGENT.md прочитан при старте, но вставляется в граф при первом stream()
        # (граф нельзя создать без API-ключа, поэтому инъекция ленивая)
        self._pending_context: Optional[str] = None

    # ──────────────────── graph building ────────────────────────────

    async def _ensure_tools(self) -> List[Any]:
        if not self._tools:
            from java_tools import get_tools
            java_cfg = self.cfg.java or JavaConfig()
            builtin = get_tools(
                project_root=java_cfg.project_root,
                allowed_commands=java_cfg.allowed_commands,
                command_timeout=java_cfg.command_timeout,
                max_output_chars=java_cfg.max_output_chars,
            )
            mcp = await _load_mcp_tools(self.cfg)
            self._tools = builtin + mcp
        return self._tools

    async def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph

        from langgraph.prebuilt import create_react_agent      # type: ignore
        from langgraph.checkpoint.memory import MemorySaver    # type: ignore
        from langchain_core.messages import SystemMessage       # type: ignore

        _apply_api_env(self.cfg)

        java_prompts = self.cfg.java_prompts or JavaPromptsConfig()
        system = java_prompts.build_system_prompt() or DEFAULT_SYSTEM_PROMPT

        llm = _build_llm(self.cfg.models.research, self.cfg)
        tools = await self._ensure_tools()

        self._graph = create_react_agent(
            llm,
            tools,
            prompt=SystemMessage(content=system),
            checkpointer=MemorySaver(),
        )
        return self._graph

    async def _ensure_subgraph(self, skill: SkillConfig) -> Optional[Any]:
        if skill.name in self._subgraphs:
            return self._subgraphs[skill.name]

        from skill_loader import get_subgraph
        builder = get_subgraph(skill)
        if builder is None:
            return None
        graph = builder(self.cfg)
        self._subgraphs[skill.name] = graph
        return graph

    # ──────────────────── streaming ─────────────────────────────────

    async def stream(
        self,
        message: str,
        skill: Optional[SkillConfig] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream agent updates. Yields raw LangGraph update chunks."""

        # ── Subgraph-скилл: запускаем изолированно ─────────────────
        if skill and skill.has_subgraph:
            subgraph = await self._ensure_subgraph(skill)
            if subgraph:
                initial = {"task": message, "messages": [], "iteration": 0}
                final_content: List[str] = []

                async for chunk in subgraph.astream(initial, stream_mode="updates"):
                    yield chunk
                    # Собираем итоговые сообщения для добавления в основной тред
                    for data in chunk.values():
                        for msg in data.get("messages", []):
                            c = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
                            if c:
                                final_content.append(c)

                # Добавляем итог в основной тред чтобы агент помнил контекст
                if final_content:
                    summary = final_content[-1]
                    graph = await self._ensure_graph()
                    config = {"configurable": {"thread_id": self.thread_id}}
                    await graph.ainvoke(
                        {"messages": [
                            {"role": "user", "content": f"[Результат скилла {skill.name}]: {message}"},
                            {"role": "assistant", "content": summary},
                        ]},
                        config=config,
                    )
                return

        # ── Обычный ReAct агент с памятью ──────────────────────────
        graph = await self._ensure_graph()

        # Ленивая инъекция AGENT.md при первом вызове stream()
        if self._pending_context:
            from langchain_core.messages import HumanMessage, AIMessage  # type: ignore
            config_ctx = {"configurable": {"thread_id": self.thread_id}}
            graph.update_state(config_ctx, {"messages": [
                HumanMessage(
                    content=f"[Контекст проекта из AGENT.md]\n\n{self._pending_context}"
                ),
                AIMessage(
                    content="Контекст проекта принят. Буду учитывать его в работе."
                ),
            ]})
            self._pending_context = None

        # Инъекция промта скилла в тело сообщения (не в system prompt)
        content = message
        if skill:
            addon = skill.build_addon_prompt()
            if addon:
                content = f"{addon}\n\n---\n\n{message}"

        config = {"configurable": {"thread_id": self.thread_id}}
        async for chunk in graph.astream(
            {"messages": [{"role": "user", "content": content}]},
            config=config,
            stream_mode="updates",
        ):
            yield chunk

    # ──────────────────── compact ───────────────────────────────────

    async def compact(self, keep_last: int = 10) -> str:
        """Сжать историю диалога: суммаризировать старые сообщения через LLM.

        Алгоритм:
        1. Получить все сообщения текущего треда из MemorySaver.
        2. LLM суммаризирует сообщения[:-keep_last] в один AbridgedMessage.
        3. Создать новый тред: [резюме] + сообщения[-keep_last:].
        4. Переключить self.thread_id на новый тред.

        Args:
            keep_last: Сколько последних сообщений оставить без изменений.

        Returns:
            Строка с описанием результата.
        """
        from langchain_core.messages import (                      # type: ignore
            AIMessage, HumanMessage, SystemMessage, ToolMessage,
        )

        graph = await self._ensure_graph()
        config = {"configurable": {"thread_id": self.thread_id}}

        state = graph.get_state(config)
        messages: List[Any] = list(state.values.get("messages", []))
        total = len(messages)

        if total <= keep_last + 2:
            return f"Нечего компактировать: {total} сообщений (порог {keep_last + 2})"

        to_summarize = messages[:-keep_last]
        to_keep      = messages[-keep_last:]

        # ── Строим текст для суммаризации ────────────────────────────
        lines: List[str] = []
        for m in to_summarize:
            if isinstance(m, HumanMessage):
                lines.append(f"User: {str(m.content)[:400]}")
            elif isinstance(m, AIMessage):
                if m.content:
                    lines.append(f"Agent: {str(m.content)[:400]}")
                elif getattr(m, "tool_calls", None):
                    tools = ", ".join(tc["name"] for tc in m.tool_calls)
                    lines.append(f"Agent [tools]: {tools}")
            elif isinstance(m, ToolMessage):
                preview = str(m.content)[:200].replace("\n", " ")
                lines.append(f"Tool result: {preview}")

        llm = _build_llm(self.cfg.models.summarization, self.cfg)
        summary_response = await llm.ainvoke([
            SystemMessage(content=(
                "Сожми историю диалога Java-ассистента в структурированное резюме. "
                "Сохрани: что было сделано, какие файлы созданы/изменены, "
                "текущий контекст и нерешённые вопросы. "
                "Пиши кратко — не более 15 предложений."
            )),
            HumanMessage(content="\n".join(lines)),
        ])

        summary_msg = AIMessage(
            content=(
                "[Резюме предыдущего разговора]\n"
                + summary_response.content
            )
        )

        # ── Новый тред с компактной историей ─────────────────────────
        new_thread_id = str(uuid4())
        new_config = {"configurable": {"thread_id": new_thread_id}}

        # update_state на пустом треде = установить начальное состояние
        graph.update_state(new_config, {"messages": [summary_msg] + list(to_keep)})

        self.thread_id = new_thread_id
        new_count = 1 + len(to_keep)
        return (
            f"Компакт выполнен: {total} → {new_count} сообщений "
            f"({len(to_summarize)} сжато в резюме, {len(to_keep)} оставлено)"
        )

    def message_count(self) -> int:
        """Вернуть количество сообщений в текущем треде (синхронно)."""
        if self._graph is None:
            return 0
        config = {"configurable": {"thread_id": self.thread_id}}
        try:
            state = self._graph.get_state(config)
            return len(state.values.get("messages", []))
        except Exception:
            return 0

    # ──────────────────── project context (AGENT.md) ────────────────

    async def load_project_context(self, path: "Path") -> bool:  # type: ignore[name-defined]
        """Загрузить AGENT.md в историю диалога как контекстное сообщение.

        Если граф ещё не инициализирован (нет API-ключа) — сохраняет контент
        в _pending_context для ленивой инъекции при первом stream().

        Returns:
            True если файл найден и принят (сразу или отложенно).
        """
        from pathlib import Path as _Path

        fpath = _Path(path)
        if not fpath.exists():
            return False

        content = fpath.read_text(encoding="utf-8", errors="replace")

        # Если граф уже построен — вставляем немедленно
        if self._graph is not None:
            from langchain_core.messages import HumanMessage, AIMessage  # type: ignore
            config = {"configurable": {"thread_id": self.thread_id}}
            self._graph.update_state(config, {"messages": [
                HumanMessage(
                    content=f"[Контекст проекта из {fpath.name}]\n\n{content}"
                ),
                AIMessage(
                    content=(
                        f"Принял контекст проекта из {fpath.name}. "
                        "Буду учитывать эту информацию во всей дальнейшей работе."
                    )
                ),
            ]})
        else:
            # Граф ещё не создан — отложить до первого stream()
            self._pending_context = content

        return True

    async def auto_load_context(self) -> bool:
        """Проверить наличие AGENT.md в корне проекта.

        Читает файл в память без инициализации графа (безопасно при старте
        без API-ключа). Контекст будет влит в граф при первом stream().
        """
        from pathlib import Path as _Path
        java_cfg = self.cfg.java
        root = _Path(java_cfg.project_root if java_cfg else ".")
        agent_md = root / "AGENT.md"
        if not agent_md.exists():
            return False
        content = agent_md.read_text(encoding="utf-8", errors="replace")
        self._pending_context = content
        return True

    # ──────────────────── control ────────────────────────────────────

    def reset(self) -> None:
        """Начать новую ветку диалога (новый thread_id, чистая история)."""
        self.thread_id = str(uuid4())
        self._graph = None   # пересоздадим с новым MemorySaver

    def update_project(self, project_root: str) -> None:
        """Сменить рабочую директорию проекта и сбросить кэш инструментов."""
        if self.cfg.java is None:
            from config import JavaConfig
            self.cfg.java = JavaConfig(project_root=project_root)
        else:
            self.cfg.java = self.cfg.java.model_copy(
                update={"project_root": project_root}
            )
        self._tools = []           # пересоздадим с новым project_root
        self._subgraphs = {}
