"""Python-скилл fix_build — итеративное исправление ошибок компиляции.

Граф:
  compile → (success?) → END
                ↓ no
             read_errors → fix_files → compile (повтор)
"""

from __future__ import annotations

from typing import Annotated, List, TypedDict

from config import AppConfig, SkillConfig

SKILL_CONFIG = SkillConfig(
    name="fix_build",
    description="Исправляет ошибки компиляции итеративно до успешного билда",
    output_format="files",
    has_subgraph=True,
    workflow_hint=(
        "Цикл: compile → читай ошибки → правь файлы → повтори. "
        "Максимум 10 итераций."
    ),
)

MAX_ITERATIONS = 10


# ─────────────────────────── State ──────────────────────────────────

class FixBuildState(TypedDict):
    task: str                          # исходная задача пользователя
    messages: Annotated[list, "append"]  # история сообщений агента
    build_output: str                  # вывод последней компиляции
    iteration: int                     # текущая итерация
    success: bool                      # True если скомпилировалось


# ─────────────────────────── nodes ──────────────────────────────────

def _run_compile(build_tool: str, project_root: str) -> str:
    import subprocess
    if build_tool == "gradle":
        cmd = "./gradlew compileJava --no-daemon -q"
    else:
        cmd = "mvn compile -q"
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=project_root,
            capture_output=True, text=True, timeout=120
        )
        out = (result.stdout + result.stderr).strip()
        return out, result.returncode == 0
    except subprocess.TimeoutExpired:
        return "TIMEOUT: компиляция не завершилась за 120 секунд", False


def make_compile_node(build_tool: str, project_root: str):
    def compile_node(state: FixBuildState) -> dict:
        it = state.get("iteration", 0) + 1
        output, ok = _run_compile(build_tool, project_root)
        status = "✅ Компиляция успешна" if ok else f"❌ Ошибки (итерация {it})"
        return {
            "build_output": output,
            "success": ok,
            "iteration": it,
            "messages": [{"role": "tool", "content": f"{status}\n{output}"}],
        }
    return compile_node


def make_fix_node(llm, tools):
    """Узел: отправляет ошибки компилятора в LLM, та исправляет файлы."""
    from langgraph.prebuilt import create_react_agent  # type: ignore
    from langchain_core.messages import HumanMessage, SystemMessage  # type: ignore

    fix_prompt = SystemMessage(content=(
        "Ты Java-разработчик. Тебе дан вывод компилятора с ошибками. "
        "Используй инструменты read_file и write_file чтобы исправить каждую ошибку. "
        "После исправления ОСТАНОВИСЬ — не запускай компиляцию сам, это сделает граф."
    ))
    inner_graph = create_react_agent(llm, tools, state_modifier=fix_prompt)

    def fix_node(state: FixBuildState) -> dict:
        build_out = state["build_output"]
        result = inner_graph.invoke({
            "messages": [HumanMessage(content=(
                f"Исправь следующие ошибки компиляции:\n\n{build_out}"
            ))]
        })
        last = result["messages"][-1]
        content = getattr(last, "content", str(last))
        return {"messages": [{"role": "assistant", "content": content}]}

    return fix_node


def should_continue(state: FixBuildState) -> str:
    if state.get("success"):
        return "done"
    if state.get("iteration", 0) >= MAX_ITERATIONS:
        return "give_up"
    return "fix"


# ─────────────────────────── builder ────────────────────────────────

def build_subgraph(cfg: AppConfig):
    """Собрать и скомпилировать LangGraph-граф для fix_build."""
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

    compile_node = make_compile_node(build_tool, project_root)
    fix_node = make_fix_node(llm, tools)

    def give_up_node(state: FixBuildState) -> dict:
        it = state.get("iteration", 0)
        return {"messages": [{
            "role": "assistant",
            "content": (
                f"⚠️ Не удалось исправить ошибки компиляции за {it} итераций.\n"
                f"Последний вывод компилятора:\n{state['build_output']}"
            )
        }]}

    def done_node(state: FixBuildState) -> dict:
        it = state.get("iteration", 0)
        return {"messages": [{
            "role": "assistant",
            "content": f"✅ Компиляция успешна после {it} итераций."
        }]}

    builder = StateGraph(FixBuildState)
    builder.add_node("compile", compile_node)
    builder.add_node("fix", fix_node)
    builder.add_node("done", done_node)
    builder.add_node("give_up", give_up_node)

    builder.set_entry_point("compile")
    builder.add_conditional_edges("compile", should_continue, {
        "fix": "fix",
        "done": "done",
        "give_up": "give_up",
    })
    builder.add_edge("fix", "compile")
    builder.add_edge("done", END)
    builder.add_edge("give_up", END)

    return builder.compile()
