"""Pure-Python tools for the Java coding agent.

All tools are standard LangChain @tool functions — no Node.js, no npx.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional

from langchain_core.tools import tool

# будет проставлен из JavaConfig при инициализации агента
_project_root: Path = Path(".")
_allowed_commands: List[str] = []
_command_timeout: int = 120
_max_output_chars: int = 8000


def configure_tools(
    project_root: str,
    allowed_commands: List[str],
    command_timeout: int,
    max_output_chars: int,
) -> None:
    """Set module-level defaults used by all tools."""
    global _project_root, _allowed_commands, _command_timeout, _max_output_chars
    _project_root = Path(project_root).resolve()
    _allowed_commands = [c.lower() for c in allowed_commands]
    _command_timeout = command_timeout
    _max_output_chars = max_output_chars


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _project_root / p
    return p.resolve()


def _truncate(text: str, limit: int | None = None) -> str:
    limit = limit or _max_output_chars
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [truncated {len(text) - limit} chars] ...\n" + text[-half:]


# ─────────────────────────── File tools ─────────────────────────────

@tool
def read_file(path: str) -> str:
    """Read the content of a file. Returns file content as text."""
    try:
        return _resolve(path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"ERROR: File not found: {path}"
    except Exception as e:
        return f"ERROR reading {path}: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Create or overwrite a file with the given content.

    Args:
        path: File path (relative to project root or absolute).
        content: Full file content to write.
    """
    try:
        target = _resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"OK: Written {len(content)} chars to {target}"
    except Exception as e:
        return f"ERROR writing {path}: {e}"


@tool
def create_directory(path: str) -> str:
    """Create a directory (and all missing parents)."""
    try:
        _resolve(path).mkdir(parents=True, exist_ok=True)
        return f"OK: Directory created: {path}"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def delete_file(path: str) -> str:
    """Delete a single file (not a directory)."""
    try:
        target = _resolve(path)
        if not target.exists():
            return f"ERROR: File not found: {path}"
        if target.is_dir():
            return "ERROR: Use a shell command to delete directories."
        target.unlink()
        return f"OK: Deleted {target}"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def move_file(source: str, destination: str) -> str:
    """Move or rename a file."""
    import shutil
    try:
        src = _resolve(source)
        dst = _resolve(destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"OK: Moved {src} → {dst}"
    except Exception as e:
        return f"ERROR: {e}"


# ─────────────────────────── Directory tools ────────────────────────

@tool
def list_directory(path: str = ".", recursive: bool = False) -> str:
    """List files and subdirectories.

    Args:
        path: Directory path (default: project root).
        recursive: If True, list recursively (up to 3 levels deep).
    """
    try:
        base = _resolve(path)
        if not base.is_dir():
            return f"ERROR: Not a directory: {path}"

        lines: List[str] = []

        def _walk(p: Path, indent: int, depth: int) -> None:
            if depth > 3:
                return
            try:
                entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
            except PermissionError:
                return
            for entry in entries:
                prefix = "  " * indent
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/")
                    if recursive:
                        _walk(entry, indent + 1, depth + 1)
                else:
                    size = entry.stat().st_size
                    lines.append(f"{prefix}{entry.name}  ({size} bytes)")

        _walk(base, 0, 0)
        return "\n".join(lines) or "(empty directory)"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def find_files(pattern: str, search_path: str = ".") -> str:
    """Find files matching a glob pattern inside the project.

    Args:
        pattern: Glob pattern, e.g. '**/*.java' or 'src/**/Test*.java'.
        search_path: Directory to search in (default: project root).
    """
    try:
        base = _resolve(search_path)
        matches = sorted(base.glob(pattern))
        if not matches:
            return f"No files found matching '{pattern}' in {base}"
        return "\n".join(str(m.relative_to(_project_root)) for m in matches[:200])
    except Exception as e:
        return f"ERROR: {e}"


# ─────────────────────────── Search tools ───────────────────────────

@tool
def search_in_files(
    text: str,
    file_pattern: str = "**/*.java",
    search_path: str = ".",
    case_sensitive: bool = True,
) -> str:
    """Search for a text string across files. Returns matching lines with context.

    Args:
        text: String to search for.
        file_pattern: Glob for files to search (default: all .java files).
        search_path: Root directory.
        case_sensitive: Case-sensitive match (default True).
    """
    try:
        base = _resolve(search_path)
        results: List[str] = []
        needle = text if case_sensitive else text.lower()

        for fpath in sorted(base.glob(file_pattern)):
            if not fpath.is_file():
                continue
            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            rel = fpath.relative_to(_project_root)
            for i, line in enumerate(lines, 1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    results.append(f"{rel}:{i}: {line.rstrip()}")
                    if len(results) >= 200:
                        results.append("... (truncated at 200 matches)")
                        return "\n".join(results)

        return "\n".join(results) if results else f"No matches for '{text}'"
    except Exception as e:
        return f"ERROR: {e}"


# ─────────────────────────── Shell / build tools ────────────────────

def _command_allowed(cmd: str) -> bool:
    """Check if the first token of a command is in the allowlist."""
    if not _allowed_commands:
        return True  # no allowlist — allow all
    first_token = cmd.strip().split()[0].lower().lstrip("./")
    return any(first_token == allowed.lstrip("./") for allowed in _allowed_commands)


@tool
def run_command(command: str, working_dir: str = "") -> str:
    """Execute a shell command and return its stdout + stderr.

    Allowed commands are configured in java_config.yaml (java.allowed_commands).
    Use this to run: mvn, gradle, java, javac, git, find, grep, etc.

    Args:
        command: Shell command string (e.g. 'mvn clean test').
        working_dir: Working directory; defaults to project root.
    """
    if not _command_allowed(command):
        first = command.strip().split()[0]
        return (
            f"ERROR: Command '{first}' is not in the allowed list.\n"
            f"Allowed: {_allowed_commands}"
        )

    cwd = _resolve(working_dir) if working_dir else _project_root
    if not cwd.is_dir():
        return f"ERROR: working_dir is not a directory: {cwd}"

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_command_timeout,
        )
        output = ""
        if proc.stdout:
            output += proc.stdout
        if proc.stderr:
            output += proc.stderr
        rc_line = f"\n[exit code: {proc.returncode}]"
        return _truncate(output) + rc_line
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {_command_timeout}s: {command}"
    except Exception as e:
        return f"ERROR running command: {e}"


@tool
def run_maven(goals: str, profiles: str = "", extra_args: str = "") -> str:
    """Run Maven with the given goals.

    Args:
        goals: Maven goals separated by spaces, e.g. 'clean compile test'.
        profiles: Comma-separated Maven profiles to activate, e.g. 'dev,ci'.
        extra_args: Additional Maven arguments, e.g. '-Dmaven.test.skip=true'.
    """
    cmd_parts = ["mvn", "-B", "--no-transfer-progress", goals]
    if profiles:
        cmd_parts.append(f"-P{profiles}")
    if extra_args:
        cmd_parts.append(extra_args)
    return run_command.invoke({"command": " ".join(cmd_parts)})  # type: ignore[attr-defined]


@tool
def run_gradle(tasks: str, extra_args: str = "") -> str:
    """Run Gradle (uses ./gradlew if present, otherwise gradle).

    Args:
        tasks: Gradle tasks separated by spaces, e.g. 'clean build test'.
        extra_args: Additional Gradle arguments, e.g. '--info'.
    """
    wrapper = _project_root / "gradlew"
    executable = "./gradlew" if wrapper.exists() else "gradle"
    cmd = f"{executable} {tasks}"
    if extra_args:
        cmd += f" {extra_args}"
    return run_command.invoke({"command": cmd})  # type: ignore[attr-defined]


# ─────────────────────────── Project info tools ─────────────────────

@tool
def get_project_structure() -> str:
    """Return the high-level project structure (src/, test/, pom.xml, etc.)."""
    lines: List[str] = [f"Project root: {_project_root}\n"]

    # Show top-level files and src/ tree
    for item in sorted(_project_root.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_file():
            lines.append(f"  {item.name}")
        else:
            lines.append(f"  {item.name}/")
            if item.name in ("src", "test", "tests"):
                for sub in sorted(item.rglob("*.java"))[:30]:
                    lines.append(f"    {sub.relative_to(_project_root)}")

    return "\n".join(lines)


@tool
def read_pom_or_build_file() -> str:
    """Read the project build descriptor: pom.xml or build.gradle(.kts)."""
    for name in ("pom.xml", "build.gradle.kts", "build.gradle"):
        candidate = _project_root / name
        if candidate.exists():
            content = candidate.read_text(encoding="utf-8")
            return f"=== {name} ===\n{_truncate(content, 6000)}"
    return "No pom.xml or build.gradle found in project root."


# ─────────────────────────── Tool factory ───────────────────────────

ALL_TOOLS = [
    read_file,
    write_file,
    create_directory,
    delete_file,
    move_file,
    list_directory,
    find_files,
    search_in_files,
    run_command,
    run_maven,
    run_gradle,
    get_project_structure,
    read_pom_or_build_file,
]


def get_tools(
    project_root: str = ".",
    allowed_commands: Optional[List[str]] = None,
    command_timeout: int = 120,
    max_output_chars: int = 8000,
):
    """Configure and return all Java agent tools."""
    configure_tools(
        project_root=project_root,
        allowed_commands=allowed_commands or [],
        command_timeout=command_timeout,
        max_output_chars=max_output_chars,
    )
    return ALL_TOOLS
