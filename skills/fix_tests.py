"""Python-скилл fix_tests — итеративное исправление падающих тестов.

Граф:
  run_tests → (all pass?) → END
                  ↓ no
              analyze_failures → fix_code → run_tests (повтор)
"""

from __future__ import annotations

import re
from typing import Annotated, List, TypedDict

from config import AppConfig, SkillConfig

SKILL_CONFIG = SkillConfig(
    name="fix_tests",
    description="Исправляет падающие тесты итеративно до полного прохождения",
    output_format="files",
    has_subgraph=True,
    workflow_hint=(
        "Цикл: run tests → читай failures → правь production/test → повтори. "
        "Максимум 10 итераций."
    ),
)

MAX_ITERATIONS = 10
# Паттерны для парсинга имён упавших тестов из вывода mvn/gradle
_MVN_FAIL_RE = re.compile(r"(?:FAIL|ERROR).*?(\w[\w.]+Test(?:\w+)?)", re.MULTILINE)
_GRADLE_FAIL_RE = re.compile(r"(\w[\w.]+Test(?:\w+)?)\s*>.*?FAILED", re.MULTILINE)


# ─────────────────────────── State ──────────────────────────────────

class FixTestsState(TypedDict):
    task: str
    messages: Annotated[list, "append"]
    test_output: str
    failed_tests: List[str]
    iteration: int
    success: bool


# ─────────────────────────── helpers ────────────────────────────────

def _run_tests(build_tool: str, project_root: str) -> tuple[str, bool]:
    import subprocess
    cmd = "./gradlew test --no-daemon" if build_tool == "gradle" else "mvn test"
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=project_root,
            capture_output=True, text=True, timeout=300
        )
        out = (result.stdout + result.stderr).strip()
        ok = result.returncode == 0
        return out, ok
    except subprocess.TimeoutExpired:
        return "TIMEOUT: тесты не завершились за 5 минут", False


def _extract_failed_tests(output: str, build_tool: str) -> List[str]:
    pattern = _GRADLE_FAIL_RE if build_tool == "gradle" else _MVN_FAIL_RE
    return list(dict.fromkeys(pattern.findall(output)))  # unique, preserve order


# ─────────────────────────── nodes ──────────────────────────────────

def make_run_tests_node(build_tool: str, project_root: str):
    def run_tests_node(state: FixTestsState) -> dict:
        it = state.get("iteration", 0) + 1
        output, ok = _run_tests(build_tool, project_root)
        failed = [] if ok else _extract_failed_tests(output, build_tool)
        status = "✅ Все тесты прошли" if ok else f"❌ Падений: {len(failed)} (итерация {it})"
        return {
            "test_output": output,
            "success": ok,
            "failed_tests": failed,
            "iteration": it,
            "messages": [{"role": "tool", "content": f"{status}\n{output[:4000]}"}],
        }
    return run_tests_node


def make_analyze_and_fix_node(llm, tools):
    from langgraph.prebuilt import create_react_agent  # type: ignore
    from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

    fix_prompt = SystemMessage(content=(
        "Ты Java-разработчик. Тебе дан вывод упавших тестов. "
        "Для каждого упавшего теста: прочитай тест (read_file), "
        "прочитай тестируемый класс (read_file), определи причину падения, "
        "исправь production-код или тест (write_file). "
        "После исправления ОСТАНОВИСЬ — не запускай тесты сам."
    ))
    inner_graph = create_react_agent(llm, tools, state_modifier=fix_prompt)

    def analyze_fix_node(state: FixTestsState) -> dict:
        failed = state.get("failed_tests", [])
        test_out = state["test_output"]
        prompt = (
            f"Упавшие тесты: {', '.join(failed) if failed else 'см. вывод'}\n\n"
            f"Вывод тестового запуска:\n{test_out[:6000]}"
        )
        result = inner_graph.invoke({
            "messages": [HumanMessage(content=prompt)]
        })
        last = result["messages"][-1]
        content = getattr(last, "content", str(last))
        return {"messages": [{"role": "assistant", "content": content}]}

    return analyze_fix_node


def should_continue(state: FixTestsState) -> str:
    if state.get("success"):
        return "done"
    if state.get("iteration", 0) >= MAX_ITERATIONS:
        return "give_up"
    return "fix"


# ─────────────────────────── builder ────────────────────────────────

def build_subgraph(cfg: AppConfig):
    from langgraph.graph import StateGraph, END  # type: ignore
    from langchain.chat_models import init_chat_model  # type: ignore
    from java_tools import get_tools

    java_cfg = cfg.java
    project_root = java_cfg.project_root if java_cfg else "."
    build_tool = java_cfg.build_tool if java_cfg else "maven"

    llm = init_chat_model(cfg.models.research)
    tools = get_tools(
        project_root=project_root,
        allowed_commands=(java_cfg.allowed_commands if java_cfg else []),
        command_timeout=(java_cfg.command_timeout if java_cfg else 120),
        max_output_chars=(java_cfg.max_output_chars if java_cfg else 8000),
    )

    run_tests_node = make_run_tests_node(build_tool, project_root)
    fix_node = make_analyze_and_fix_node(llm, tools)

    def done_node(state: FixTestsState) -> dict:
        it = state.get("iteration", 0)
        return {"messages": [{"role": "assistant",
            "content": f"✅ Все тесты прошли после {it} итераций."}]}

    def give_up_node(state: FixTestsState) -> dict:
        it = state.get("iteration", 0)
        failed = state.get("failed_tests", [])
        return {"messages": [{"role": "assistant",
            "content": (
                f"⚠️ Не удалось исправить тесты за {it} итераций.\n"
                f"Оставшиеся падения: {', '.join(failed)}\n\n"
                f"Последний вывод:\n{state['test_output'][:2000]}"
            )}]}

    builder = StateGraph(FixTestsState)
    builder.add_node("run_tests", run_tests_node)
    builder.add_node("fix", fix_node)
    builder.add_node("done", done_node)
    builder.add_node("give_up", give_up_node)

    builder.set_entry_point("run_tests")
    builder.add_conditional_edges("run_tests", should_continue, {
        "fix": "fix",
        "done": "done",
        "give_up": "give_up",
    })
    builder.add_edge("fix", "run_tests")
    builder.add_edge("done", END)
    builder.add_edge("give_up", END)

    return builder.compile()
