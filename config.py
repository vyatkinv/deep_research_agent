"""Configuration loader — reads config.yaml and validates via Pydantic."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

# ──────────────────────────── sub-models ────────────────────────────


class ApiConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""

    def resolved_api_key(self) -> str:
        """Env var takes priority over config file value."""
        return os.getenv("OPENAI_API_KEY") or self.api_key or ""

    def resolved_base_url(self) -> str:
        return os.getenv("OPENAI_BASE_URL") or self.base_url


class ModelsConfig(BaseModel):
    research: str = "openai:gpt-4.1"
    summarization: str = "openai:gpt-4.1-mini"
    compression: str = "openai:gpt-4.1"
    final_report: str = "openai:gpt-4.1"


class ResearchConfig(BaseModel):
    max_concurrent_units: int = Field(5, ge=1, le=20)
    max_researcher_iterations: int = Field(6, ge=1)
    max_react_tool_calls: int = Field(10, ge=1)
    search_api: str = "tavily"  # tavily | openai | anthropic | none
    allow_clarification: bool = False

    @field_validator("search_api")
    @classmethod
    def validate_search_api(cls, v: str) -> str:
        allowed = {"tavily", "openai", "anthropic", "none"}
        if v not in allowed:
            raise ValueError(f"search_api must be one of {allowed}")
        return v


class MCPServerConfig(BaseModel):
    name: str
    transport: str = "stdio"  # stdio | sse
    # stdio транспорт
    command: Optional[str] = None
    args: List[str] = []
    env: Dict[str, str] = {}
    # sse транспорт
    url: Optional[str] = None
    headers: Dict[str, str] = {}

    def to_mcp_dict(self) -> Dict[str, Any]:
        """Convert to the format expected by open_deep_research MCPConfig."""
        if self.transport == "stdio":
            return {
                "command": self.command,
                "args": self.args,
                "env": self.env or None,
                "transport": "stdio",
            }
        # sse
        return {
            "url": self.url,
            "headers": self.headers or None,
            "transport": "sse",
        }


class MCPConfig(BaseModel):
    servers: List[MCPServerConfig] = []
    prompt: Optional[str] = None

    def to_open_deep_research_format(self) -> Optional[Dict[str, Any]]:
        """Build the MCPConfig dict for open_deep_research Configuration."""
        if not self.servers:
            return None
        return {srv.name: srv.to_mcp_dict() for srv in self.servers}


class PromptsConfig(BaseModel):
    system: str = ""
    query_writer: str = ""
    summarizer: str = ""
    reflection: str = ""
    report_writer: str = ""

    def as_system_prompts_dict(self) -> Dict[str, str]:
        """Map to the system_prompts keys expected by open_deep_research."""
        mapping: Dict[str, str] = {}
        if self.query_writer:
            mapping["query_writer"] = self.query_writer
        if self.summarizer:
            mapping["summarizer"] = self.summarizer
        if self.reflection:
            mapping["reflection"] = self.reflection
        if self.report_writer:
            mapping["report_writer"] = self.report_writer
        return mapping


# ──────────────────────────── Java agent config ─────────────────────


class JavaConfig(BaseModel):
    project_root: str = "."
    build_tool: str = "maven"  # maven | gradle | none
    java_version: str = "21"
    # Команды, которые агент может выполнять (allowlist)
    allowed_commands: List[str] = [
        "mvn", "gradle", "gradlew", "./gradlew",
        "java", "javac", "jar",
        "git", "find", "grep", "cat", "ls", "tree",
    ]
    # Таймаут на каждую команду в секундах
    command_timeout: int = 120
    # Максимальный размер вывода команды (символов)
    max_output_chars: int = 8000
    # Рабочая директория для команд
    working_dir: str = "."

    @field_validator("build_tool")
    @classmethod
    def validate_build_tool(cls, v: str) -> str:
        allowed = {"maven", "gradle", "none"}
        if v not in allowed:
            raise ValueError(f"build_tool must be one of {allowed}")
        return v


class JavaPromptsConfig(BaseModel):
    """Промты для каждого этапа цепочки Java-агента."""
    system: str = ""           # общий системный промт
    task_analyzer: str = ""    # анализ задачи
    architect: str = ""        # проектирование решения
    code_writer: str = ""      # написание кода
    test_writer: str = ""      # написание тестов
    reviewer: str = ""         # ревью кода

    def build_system_prompt(self) -> str:
        """Собрать единый системный промт из всех секций."""
        parts = [self.system.strip()]
        sections = [
            ("## Анализ задачи", self.task_analyzer),
            ("## Проектирование", self.architect),
            ("## Написание кода", self.code_writer),
            ("## Написание тестов", self.test_writer),
            ("## Ревью кода", self.reviewer),
        ]
        for header, content in sections:
            if content.strip():
                parts.append(f"{header}\n{content.strip()}")
        return "\n\n".join(p for p in parts if p)


# ──────────────────────────── Skill config ──────────────────────────


class SkillConfig(BaseModel):
    """Описание одного скилла — именованного сценария для агента."""

    name: str
    description: str = ""

    # Промт, который ДОБАВЛЯЕТСЯ к системному промту агента
    skill_prompt: str = ""

    # Подсказка агенту о порядке вызова инструментов
    workflow_hint: str = ""

    # Формат финального ответа
    output_format: str = "text"  # text | files | report | diff | explanation

    # Дополнительные инструменты только для этого скилла (имена)
    extra_tools: List[str] = []

    # Если True — скилл реализован как Python sub-граф (не только промты)
    has_subgraph: bool = False

    def build_addon_prompt(self) -> str:
        """Сформировать блок, который добавляется к системному промту."""
        parts: List[str] = []
        if self.skill_prompt.strip():
            parts.append(f"## Скилл: {self.name}\n{self.skill_prompt.strip()}")
        if self.workflow_hint.strip():
            parts.append(f"## Порядок работы\n{self.workflow_hint.strip()}")
        if self.output_format and self.output_format != "text":
            parts.append(f"## Формат вывода\n{self.output_format}")
        return "\n\n".join(parts)


# ──────────────────────────── GigaChat config ───────────────────────


class GigaChatConfig(BaseModel):
    """Настройки для GigaChat (langchain-gigachat).

    Используется когда models.research / summarization содержит префикс gigachat:.
    Пример: models.research = "gigachat:GigaChat-Pro"

    credentials — авторизационный ключ из личного кабинета Сбера.
    Env-переменная GIGACHAT_CREDENTIALS имеет приоритет.
    """

    credentials: str = ""
    scope: str = "GIGACHAT_API_PERS"  # GIGACHAT_API_PERS | GIGACHAT_API_CORP | GIGACHAT_API_B2B
    verify_ssl_certs: bool = False     # False для работы с российскими CA
    base_url: Optional[str] = None    # кастомный эндпоинт (обычно не нужен)

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        allowed = {"GIGACHAT_API_PERS", "GIGACHAT_API_CORP", "GIGACHAT_API_B2B"}
        if v not in allowed:
            raise ValueError(f"scope must be one of {allowed}")
        return v

    def resolved_credentials(self) -> str:
        return os.getenv("GIGACHAT_CREDENTIALS") or self.credentials or ""


# ──────────────────────────── root config ───────────────────────────


class AppConfig(BaseModel):
    api: ApiConfig = Field(default_factory=ApiConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    # Java-агент (опционально)
    java: Optional[JavaConfig] = None
    java_prompts: Optional[JavaPromptsConfig] = None
    # GigaChat (опционально)
    gigachat: Optional[GigaChatConfig] = None


# ──────────────────────────── loader ────────────────────────────────


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate config from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return AppConfig.model_validate(raw)
