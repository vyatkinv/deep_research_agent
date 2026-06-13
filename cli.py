#!/usr/bin/env python3
"""Java Agent CLI — интерактивный AI-ассистент для Java разработки.

Запуск:
    python3 cli.py
    python3 cli.py --config java_config.yaml
    python3 cli.py --project /path/to/my-java-project
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from rich import box

from config import AppConfig, JavaConfig, SkillConfig, load_config
from session import AgentSession
from router import route as auto_route, score_all
from skill_loader import list_skills, load_skill, skills_registry

# ────────────────────────── Theme ───────────────────────────────────

THEME = Theme({
    "tool.call":    "dim cyan",
    "tool.result":  "dim",
    "skill.active": "bold yellow",
    "skill.auto":   "dim yellow",
    "cmd":          "bold white",
    "prompt.base":  "bold green",
    "prompt.skill": "bold yellow",
    "error":        "bold red",
    "ok":           "bold green",
    "info":         "dim white",
    "header":       "bold blue",
    "muted":        "dim",
})

# ────────────────────────── INIT prompt ─────────────────────────────

INIT_TASK = """\
Твоя задача — проанализировать этот Java-проект и создать файл AGENT.md \
в корне проекта. AGENT.md — это контекстный документ для AI-ассистента.

Шаги:
1. get_project_structure() — изучи общую структуру
2. read_pom_or_build_file() — прочитай зависимости и настройки сборки
3. find_files("**/*.java") — получи список Java-файлов
4. Прочитай 4-6 ключевых файлов: главный класс (@SpringBootApplication или main), \
   1-2 контроллера, 1-2 сервиса, конфигурационные классы
5. write_file("AGENT.md", <контент>) — сохрани документацию

Структура AGENT.md (строго соблюдай):

# Project: <artifactId или имя директории>

## Обзор
<1-3 предложения: что делает проект, для кого, какую проблему решает>

## Стек
- Java <версия>
- <Maven|Gradle> <версия если есть>
- Spring Boot <версия> (если используется)
- БД: <тип если есть>

## Ключевые зависимости
<список основных зависимостей из pom.xml/build.gradle, не транзитивных>

## Структура пакетов
```
<корневой пакет>/
  controller/   — <описание>
  service/      — <описание>
  repository/   — <описание>
  entity/       — <описание>
  dto/          — <описание>
  config/       — <описание>
```

## Ключевые классы
| Класс | Роль |
|-------|------|
| <ClassName> | <краткое описание> |

## Соглашения о коде
<паттерны которые используются в проекте: стиль DI, транзакции, тесты, DTO и т.д.>

## Важные замечания для AI-ассистента
<нестандартные решения, особенности проекта, на что обращать внимание>

---
*Сгенерировано Java Agent /init*
"""

COMPACT_THRESHOLD = 30  # авто-компакт при превышении

# ────────────────────────── Helpers ─────────────────────────────────

def _detect_project_info(root: str) -> Dict[str, str]:
    info: Dict[str, str] = {"name": Path(root).name, "build": "unknown", "java": "?"}
    pom = Path(root) / "pom.xml"
    gradle_kts = Path(root) / "build.gradle.kts"
    gradle = Path(root) / "build.gradle"

    if pom.exists():
        info["build"] = "Maven"
        try:
            text = pom.read_text(encoding="utf-8", errors="replace")
            if m := re.search(r"<artifactId>([^<]+)</artifactId>", text):
                info["name"] = m.group(1).strip()
            if m := re.search(r"<java\.version>([^<]+)</java\.version>", text):
                info["java"] = m.group(1).strip()
            elif m := re.search(r"<source>(\d+)</source>", text):
                info["java"] = m.group(1).strip()
            if m := re.search(r"spring-boot[^<]*<version>([^<]+)</version>", text):
                info["spring"] = m.group(1).strip()
        except Exception:
            pass
    elif gradle_kts.exists() or gradle.exists():
        info["build"] = "Gradle"
        try:
            gfile = gradle_kts if gradle_kts.exists() else gradle
            text = gfile.read_text(encoding="utf-8", errors="replace")
            if m := re.search(r"(sourceCompatibility|javaVersion)\s*[=:]\s*[\"']?(\d+)", text):
                info["java"] = m.group(2)
            if m := re.search(r"org\.springframework\.boot[\"' ]+version[\"' ]+([0-9.]+)", text):
                info["spring"] = m.group(1)
        except Exception:
            pass

    return info


def _fmt_tool_args(args: Dict[str, Any]) -> str:
    parts = []
    for k, v in list(args.items())[:3]:
        sv = str(v)
        if len(sv) > 55:
            sv = sv[:52] + "..."
        parts.append(sv if k in ("path", "command", "text", "goals", "tasks") else f"{k}={sv!r}")
    return ", ".join(parts)


# ────────────────────────── Renderer ────────────────────────────────

class Renderer:
    def __init__(self, console: Console, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose
        self._answer_started = False

    def reset(self) -> None:
        self._answer_started = False

    def render_chunk(self, chunk: Dict[str, Any]) -> None:
        for node, data in chunk.items():
            if not isinstance(data, dict):
                continue
            for msg in data.get("messages", []):
                self._render_msg(msg)

    def _render_msg(self, msg: Any) -> None:
        # Словарь (синтетические сообщения subgraph / ToolMessage dict)
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                return
            if role == "assistant":
                self._print_ai(content)
            elif role == "tool" and self.verbose:
                self.console.print(f"[tool.result]  ↳ {content[:200]}[/tool.result]")
            return

        # AIMessage с tool_calls
        tool_calls = getattr(msg, "tool_calls", [])
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name", "?")
                args_str = _fmt_tool_args(tc.get("args", {}))
                self.console.print(f"[tool.call]⚙  {name}({args_str})[/tool.call]")
            return

        # ToolMessage
        try:
            from langchain_core.messages import ToolMessage  # type: ignore
            if isinstance(msg, ToolMessage):
                if self.verbose:
                    preview = (str(msg.content) or "")[:200].replace("\n", " ")
                    self.console.print(f"[tool.result]  ↳ {preview}[/tool.result]")
                return
        except ImportError:
            pass

        # AIMessage с текстом
        content = getattr(msg, "content", "")
        if content:
            self._print_ai(content)

    def _print_ai(self, text: str) -> None:
        if not text.strip():
            return
        if not self._answer_started:
            self.console.print()
            self._answer_started = True
        has_md = any(tok in text for tok in ("```", "**", "##", "- ", "1. ", "| "))
        if has_md:
            self.console.print(Markdown(text))
        else:
            self.console.print(text)

    def skill_auto(self, skill: SkillConfig) -> None:
        self.console.print(
            f"[skill.auto]⚡ авто-скилл: [bold]{skill.name}[/bold] — {skill.description}[/skill.auto]"
        )

    def error(self, msg: str) -> None:
        self.console.print(f"[error]✗ {msg}[/error]")

    def ok(self, msg: str) -> None:
        self.console.print(f"[ok]✓ {msg}[/ok]")

    def info(self, msg: str) -> None:
        self.console.print(f"[info]{msg}[/info]")

    def rule(self, title: str = "") -> None:
        self.console.print(Rule(title, style="muted"))


# ────────────────────────── Slash commands ───────────────────────────

COMMANDS: Dict[str, str] = {
    "/help":             "Показать эту справку",
    "/init":             "Сканировать проект и создать AGENT.md с контекстом",
    "/compact [N]":      "Сжать историю диалога (оставить последние N сообщений, по умолч. 10)",
    "/skills":           "Список всех доступных скиллов",
    "/skill <name|off>": "Активировать скилл или отключить (/skill off)",
    "/tools":            "Список инструментов агента",
    "/config":           "Показать текущую конфигурацию",
    "/project <path>":   "Сменить Java-проект без перезапуска",
    "/auto":             "Вкл/выкл авто-определение скилла",
    "/verbose":          "Вкл/выкл подробный вывод результатов инструментов",
    "/reset":            "Начать новую сессию (очистить историю диалога)",
    "/clear":            "Очистить экран",
    "/exit":             "Выйти",
}


# ────────────────────────── CLI ─────────────────────────────────────

class JavaAgentCLI:
    def __init__(self, config_path: str, project: Optional[str] = None) -> None:
        self.cfg = load_config(config_path)
        self.config_path = config_path

        if project:
            if self.cfg.java is None:
                self.cfg.java = JavaConfig(project_root=project)
            else:
                self.cfg.java = self.cfg.java.model_copy(update={"project_root": project})

        self.console = Console(theme=THEME)
        self.R = Renderer(self.console)
        self.session = AgentSession(self.cfg)
        self.registry: Dict[str, SkillConfig] = skills_registry()

        self.active_skill: Optional[SkillConfig] = None
        self.auto_skill: bool = True
        self.verbose: bool = False
        self.project_info: Dict[str, str] = {}

    # ──────────────────── async main loop ────────────────────────────

    async def run(self) -> None:
        await self._welcome()

        loop = asyncio.get_event_loop()

        while True:
            try:
                raw: str = await loop.run_in_executor(None, self._prompt)
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n[info]До свидания![/info]")
                break

            text = raw.strip()
            if not text:
                continue

            if text.startswith("/"):
                await self._dispatch_slash(text)
            else:
                await self._handle_message(text)

    # ──────────────────── prompt ─────────────────────────────────────

    def _prompt(self) -> str:
        skill_part = f"[{self.active_skill.name}] " if self.active_skill else ""
        auto_part  = "~" if self.auto_skill and not self.active_skill else ""
        count = self.session.message_count()
        count_part = f"({count}) " if count > 0 else ""
        prompt_str = f"{count_part}{skill_part}{auto_part}> "
        try:
            # Явный вывод промта и чтение из буфера — обход проблем с кодировкой stdin
            import sys
            sys.stdout.write(prompt_str)
            sys.stdout.flush()
            line = sys.stdin.buffer.readline()
            if not line:
                raise EOFError
            return line.decode("utf-8", errors="replace").rstrip("\n")
        except (EOFError, KeyboardInterrupt):
            raise

    # ──────────────────── message handling ───────────────────────────

    async def _handle_message(self, message: str) -> None:
        skill = self.active_skill

        if skill is None and self.auto_skill:
            skill = auto_route(message, self.registry)
            if skill:
                self.R.skill_auto(skill)

        self.R.reset()

        try:
            async for chunk in self.session.stream(message, skill):
                self.R.render_chunk(chunk)
        except KeyboardInterrupt:
            self.R.info("Прервано.")
        except Exception as exc:
            self.R.error(f"Ошибка агента: {exc}")
        finally:
            self.console.print()

        # Авто-компакт при большой истории
        count = self.session.message_count()
        if count > COMPACT_THRESHOLD:
            self.R.info(
                f"История: {count} сообщений. "
                "Введи /compact чтобы сжать и освободить контекст."
            )

    # ──────────────────── slash dispatcher ───────────────────────────

    async def _dispatch_slash(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        handlers = {
            "/help":    self._cmd_help,
            "/init":    self._cmd_init,
            "/compact": lambda: self._cmd_compact(arg),
            "/skills":  self._cmd_skills,
            "/skill":   lambda: self._cmd_skill(arg),
            "/tools":   self._cmd_tools,
            "/config":  self._cmd_config,
            "/project": lambda: self._cmd_project(arg),
            "/auto":    self._cmd_auto,
            "/verbose": self._cmd_verbose,
            "/reset":   self._cmd_reset,
            "/clear":   self._cmd_clear,
            "/exit":    self._cmd_exit,
            "/quit":    self._cmd_exit,
        }

        handler = handlers.get(cmd)
        if handler:
            result = handler()
            if asyncio.iscoroutine(result):
                await result
        else:
            self.R.error(f"Неизвестная команда: {cmd}  →  /help")

    # ──────────────────── /help ──────────────────────────────────────

    def _cmd_help(self) -> None:
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("cmd",  style="cmd",  no_wrap=True)
        t.add_column("desc", style="info")
        for cmd, desc in COMMANDS.items():
            t.add_row(cmd, desc)
        self.console.print(Panel(
            t,
            title="[header]Команды[/header]",
            border_style="blue",
        ))

    # ──────────────────── /init ──────────────────────────────────────

    async def _cmd_init(self) -> None:
        java_cfg = self.cfg.java
        project_root = Path(java_cfg.project_root if java_cfg else ".").resolve()
        agent_md = project_root / "AGENT.md"

        if agent_md.exists():
            self.R.info(
                f"AGENT.md уже существует: {agent_md}\n"
                "  Перезаписать? (y/N) ",
            )
            loop = asyncio.get_event_loop()
            answer = (await loop.run_in_executor(None, input, "")).strip().lower()
            if answer not in ("y", "yes", "да"):
                self.R.info("Отменено.")
                return

        self.R.rule("init — сканирование проекта")
        self.R.info(f"Проект: {project_root}")
        self.console.print()

        self.R.reset()
        try:
            async for chunk in self.session.stream(INIT_TASK):
                self.R.render_chunk(chunk)
        except KeyboardInterrupt:
            self.R.info("Прервано.")
            return
        except Exception as exc:
            self.R.error(f"Ошибка при init: {exc}")
            return
        finally:
            self.console.print()

        if agent_md.exists():
            loaded = await self.session.load_project_context(agent_md)
            if loaded:
                size = agent_md.stat().st_size
                self.R.ok(
                    f"AGENT.md создан ({size} байт) и загружен в контекст сессии.\n"
                    f"  {agent_md}"
                )
            else:
                self.R.error("AGENT.md создан, но не удалось загрузить в контекст.")
        else:
            self.R.error(
                "Агент не создал AGENT.md. Попробуй:\n"
                "  python3 cli.py --project <путь> и затем /init"
            )

    # ──────────────────── /compact ───────────────────────────────────

    async def _cmd_compact(self, arg: str) -> None:
        keep = 10
        if arg.isdigit():
            keep = max(2, int(arg))

        before = self.session.message_count()
        if before == 0:
            self.R.info("История пуста — нечего компактировать.")
            return

        self.R.info(f"Компактирую {before} сообщений (оставляю {keep} последних)...")

        with Live(
            Spinner("dots", text="[info]Суммаризирую историю...[/info]"),
            console=self.console,
            refresh_per_second=10,
        ):
            try:
                result = await self.session.compact(keep_last=keep)
            except Exception as exc:
                self.R.error(f"Ошибка компакта: {exc}")
                return

        self.R.ok(result)

    # ──────────────────── /skills ────────────────────────────────────

    def _cmd_skills(self) -> None:
        skills = list_skills()
        t = Table(box=box.SIMPLE, show_header=True, padding=(0, 2))
        t.add_column("Скилл",    style="bold",  no_wrap=True)
        t.add_column("Тип",      style="muted", no_wrap=True)
        t.add_column("Вывод",    style="muted", no_wrap=True)
        t.add_column("Описание", style="info")

        for sk in skills:
            active = " ◀" if self.active_skill and self.active_skill.name == sk.name else ""
            typ = "subgraph" if sk.has_subgraph else "prompt"
            t.add_row(sk.name + active, typ, sk.output_format, sk.description)

        self.console.print(Panel(
            t,
            title=f"[header]Скиллы ({len(skills)})[/header]",
            border_style="blue",
        ))
        self.R.info("/skill <name>  активировать   /skill off  отключить")

    # ──────────────────── /skill ─────────────────────────────────────

    def _cmd_skill(self, name: str) -> None:
        if not name:
            current = self.active_skill.name if self.active_skill else "нет"
            self.R.info(f"Активный скилл: {current}")
            return

        if name.lower() in ("off", "none", "нет", "выкл"):
            self.active_skill = None
            self.auto_skill = True
            self.R.ok("Скилл отключён — авто-определение включено")
            return

        try:
            skill = load_skill(name)
            self.active_skill = skill
            self.auto_skill = False
            note = " (subgraph-режим)" if skill.has_subgraph else ""
            self.R.ok(f"Скилл: [bold]{skill.name}[/bold]{note}\n  {skill.description}")
        except ValueError as e:
            self.R.error(str(e))

    # ──────────────────── /tools ─────────────────────────────────────

    def _cmd_tools(self) -> None:
        from java_tools import ALL_TOOLS
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("Инструмент", style="bold cyan", no_wrap=True)
        t.add_column("Описание",   style="info")
        for tool in ALL_TOOLS:
            first = (tool.description or "").split("\n")[0]
            t.add_row(tool.name, first)

        self.console.print(Panel(
            t,
            title=f"[header]Инструменты ({len(ALL_TOOLS)})[/header]",
            border_style="blue",
        ))
        if self.cfg.mcp.servers:
            names = ", ".join(s.name for s in self.cfg.mcp.servers)
            self.R.info(f"MCP-серверы: {names}")

    # ──────────────────── /config ────────────────────────────────────

    def _cmd_config(self) -> None:
        java = self.cfg.java
        pinfo = self.project_info

        # Определяем провайдера и отображаем соответствующий ключ
        gc_creds = self.cfg.gigachat and self.cfg.gigachat.resolved_credentials()
        openai_key = self.cfg.api.resolved_api_key()
        if gc_creds:
            provider_disp = f"GigaChat ({self.cfg.gigachat.scope})"
            key_disp = f"***{gc_creds[-4:]}" if len(gc_creds) > 4 else "[red](не задан!)[/red]"
        elif openai_key:
            provider_disp = self.cfg.api.resolved_base_url()
            key_disp = f"***{openai_key[-4:]}" if len(openai_key) > 4 else "[red](не задан!)[/red]"
        else:
            provider_disp = self.cfg.api.resolved_base_url()
            key_disp = "[red](не задан!)[/red]"

        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column("", style="bold",  no_wrap=True)
        t.add_column("", style="info")

        rows = [
            ("Конфиг",          self.config_path),
            ("Провайдер",       provider_disp),
            ("API key",         key_disp),
            ("Модель",          self.cfg.models.research),
            ("Суммар. модель",  self.cfg.models.summarization),
            ("Проект",          java.project_root if java else "."),
            ("Build tool",      pinfo.get("build", java.build_tool if java else "?")),
            ("Java version",    pinfo.get("java", java.java_version if java else "?")),
            ("Сообщений",       str(self.session.message_count())),
            ("Авто-скилл",      "вкл" if self.auto_skill else "выкл"),
            ("Verbose",         "вкл" if self.verbose else "выкл"),
            ("MCP серверы",     ", ".join(s.name for s in self.cfg.mcp.servers) or "нет"),
            ("Активный скилл",  self.active_skill.name if self.active_skill else "нет"),
        ]
        for k, v in rows:
            t.add_row(k, str(v))

        self.console.print(Panel(t, title="[header]Конфигурация[/header]", border_style="blue"))

    # ──────────────────── /project ───────────────────────────────────

    def _cmd_project(self, path: str) -> None:
        if not path:
            java = self.cfg.java
            self.R.info(f"Текущий проект: {java.project_root if java else '.'}")
            return

        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            self.R.error(f"Директория не найдена: {resolved}")
            return

        self.session.update_project(str(resolved))
        self.project_info = _detect_project_info(str(resolved))
        spring = f" • Spring Boot {self.project_info['spring']}" if "spring" in self.project_info else ""
        self.R.ok(
            f"Проект: {resolved}\n"
            f"  {self.project_info.get('build','?')} • "
            f"Java {self.project_info.get('java','?')}{spring}"
        )

    # ──────────────────── /auto ──────────────────────────────────────

    def _cmd_auto(self) -> None:
        self.auto_skill = not self.auto_skill
        state = "включён" if self.auto_skill else "выключен"
        self.R.ok(f"Авто-скилл {state}")

    # ──────────────────── /verbose ───────────────────────────────────

    def _cmd_verbose(self) -> None:
        self.verbose = not self.verbose
        self.R.verbose = self.verbose
        state = "включён" if self.verbose else "выключен"
        self.R.ok(f"Verbose {state}")

    # ──────────────────── /reset ─────────────────────────────────────

    async def _cmd_reset(self) -> None:
        self.session.reset()
        self.active_skill = None
        self.auto_skill = True
        self.R.ok("Новая сессия — история очищена")

        # Автозагрузка AGENT.md в новую сессию
        loaded = await self.session.auto_load_context()
        if loaded:
            self.R.info("AGENT.md загружен в контекст новой сессии")

    # ──────────────────── /clear ─────────────────────────────────────

    def _cmd_clear(self) -> None:
        self.console.clear()
        self._print_status()

    # ──────────────────── /exit ──────────────────────────────────────

    def _cmd_exit(self) -> None:
        self.console.print("[info]До свидания![/info]")
        sys.exit(0)

    # ──────────────────── welcome ────────────────────────────────────

    async def _welcome(self) -> None:
        java = self.cfg.java
        root = java.project_root if java else "."
        self.project_info = _detect_project_info(root)

        skills_count = len(list_skills())
        from java_tools import ALL_TOOLS

        spring_line = ""
        if "spring" in self.project_info:
            spring_line = f"\n[bold]Spring Boot:[/bold] {self.project_info['spring']}"

        info = (
            f"[bold]Модель:[/bold]      {self.cfg.models.research}\n"
            f"[bold]Проект:[/bold]      {Path(root).resolve()}\n"
            f"[bold]Build:[/bold]       {self.project_info.get('build','?')} "
            f"• Java {self.project_info.get('java','?')}"
            f"{spring_line}\n"
            f"[bold]Скиллы:[/bold]      {skills_count} "
            f"({'авто' if self.auto_skill else 'ручной'} режим)\n"
            f"[bold]Инструменты:[/bold] {len(ALL_TOOLS)} встроенных"
            + (f" + {len(self.cfg.mcp.servers)} MCP" if self.cfg.mcp.servers else "")
        )

        self.console.print(Panel(
            info,
            title="[bold blue]☕ Java Agent[/bold blue]",
            subtitle="[muted]/help — команды  •  /init — инициализировать проект[/muted]",
            border_style="blue",
            padding=(1, 2),
        ))

        has_key = (
            self.cfg.api.resolved_api_key()
            or (self.cfg.gigachat and self.cfg.gigachat.resolved_credentials())
        )
        if not has_key:
            self.R.error(
                "API ключ не задан.\n"
                "  GigaChat: добавь GIGACHAT_CREDENTIALS в .env\n"
                "  OpenAI:   добавь OPENAI_API_KEY в .env"
            )
            self.console.print()

        # Автозагрузка AGENT.md если существует
        loaded = await self.session.auto_load_context()
        if loaded:
            java_cfg = self.cfg.java
            md_path = Path(java_cfg.project_root if java_cfg else ".") / "AGENT.md"
            self.R.info(f"Контекст проекта загружен: {md_path}")
            self.console.print()

    def _print_status(self) -> None:
        java = self.cfg.java
        root = Path(java.project_root if java else ".").name
        skill = f"[{self.active_skill.name}] " if self.active_skill else ""
        auto  = "~auto " if self.auto_skill and not self.active_skill else ""
        count = self.session.message_count()
        self.console.print(
            Rule(f"[muted]{skill}{auto}{root} • {count} сообщений • {self.cfg.models.research}[/muted]")
        )


# ────────────────────────── Entry point ─────────────────────────────

def _load_dotenv(config_path: str) -> None:
    env_path = Path(config_path).parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(env_path)
    except ImportError:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="java-agent",
        description="Интерактивный Java Agent CLI",
    )
    p.add_argument("--config", "-c", default="java_config.yaml",
                   help="Путь к конфигу (по умолч: java_config.yaml)")
    p.add_argument("--project", "-p",
                   help="Путь к Java-проекту")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    _load_dotenv(args.config)
    cli = JavaAgentCLI(config_path=args.config, project=args.project)
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
