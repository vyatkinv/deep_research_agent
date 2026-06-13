"""Загрузчик скиллов из директории skills/.

Поддерживает два типа скиллов:
- YAML-скиллы (*.yaml) — промты + метаданные, без кода
- Python-скиллы (*.py) — подграфы LangGraph для сложной логики с циклами
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from config import SkillConfig

# Директория скиллов относительно этого файла
SKILLS_DIR = Path(__file__).parent / "skills"


# ─────────────────────────── YAML loader ────────────────────────────

def _load_yaml_skill(path: Path) -> SkillConfig:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return SkillConfig.model_validate(raw)


# ─────────────────────────── Python loader ──────────────────────────

def _load_python_skill(path: Path) -> Optional[SkillConfig]:
    """Загружает Python-скилл. Модуль должен содержать SKILL_CONFIG: SkillConfig."""
    spec = importlib.util.spec_from_file_location(f"skill_{path.stem}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    cfg: Optional[SkillConfig] = getattr(module, "SKILL_CONFIG", None)
    return cfg


# ─────────────────────────── public API ─────────────────────────────

def list_skills(skills_dir: str | Path = SKILLS_DIR) -> List[SkillConfig]:
    """Вернуть список всех доступных скиллов (YAML + Python), отсортированных по имени."""
    base = Path(skills_dir)
    if not base.is_dir():
        return []

    skills: List[SkillConfig] = []

    # Собираем по имени: Python-скилл имеет приоритет над YAML с тем же именем
    by_name: Dict[str, SkillConfig] = {}

    for path in sorted(base.iterdir()):
        if path.suffix == ".yaml":
            try:
                cfg = _load_yaml_skill(path)
                # Добавляем только если Python-версии ещё нет
                if cfg.name not in by_name:
                    by_name[cfg.name] = cfg
            except Exception as exc:
                print(f"WARN: не удалось загрузить скилл {path.name}: {exc}")
        elif path.suffix == ".py" and path.stem != "__init__":
            try:
                cfg = _load_python_skill(path)
                if cfg:
                    by_name[cfg.name] = cfg  # всегда перекрывает YAML
            except Exception as exc:
                print(f"WARN: не удалось загрузить Python-скилл {path.name}: {exc}")

    return sorted(by_name.values(), key=lambda s: s.name)


def load_skill(
    name: str,
    skills_dir: str | Path = SKILLS_DIR,
) -> SkillConfig:
    """Загрузить скилл по имени. Raises ValueError если не найден."""
    base = Path(skills_dir)

    # Сначала ищем YAML (приоритет), потом Python
    for suffix in (".yaml", ".py"):
        candidate = base / f"{name}{suffix}"
        if not candidate.exists():
            continue
        if suffix == ".yaml":
            return _load_yaml_skill(candidate)
        cfg = _load_python_skill(candidate)
        if cfg:
            return cfg

    available = [s.name for s in list_skills(base)]
    raise ValueError(
        f"Скилл '{name}' не найден в {base}.\n"
        f"Доступные скиллы: {', '.join(available) or '(нет)'}"
    )


def skills_registry(skills_dir: str | Path = SKILLS_DIR) -> Dict[str, SkillConfig]:
    """Вернуть словарь name → SkillConfig для всех скиллов."""
    return {s.name: s for s in list_skills(skills_dir)}


def get_subgraph(skill: SkillConfig):
    """
    Если у скилла has_subgraph=True — загрузить и вернуть его LangGraph-подграф.
    Python-скилл должен экспортировать функцию build_subgraph(cfg: AppConfig).
    """
    if not skill.has_subgraph:
        return None

    base = SKILLS_DIR
    py_path = base / f"{skill.name}.py"
    if not py_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(f"skill_{skill.name}", py_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    builder = getattr(module, "build_subgraph", None)
    return builder  # вызывается позже как builder(cfg) → compiled graph
