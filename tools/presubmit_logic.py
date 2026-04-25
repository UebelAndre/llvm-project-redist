"""Pure helpers for BCR-style ``presubmit.yml`` (no YAML I/O).

Used by ``run_presubmit.py`` and unit tests. YAML loading is done in the CLI.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# Platforms commonly used in BCR presubmit matrices (extend as needed).
KNOWN_PLATFORMS: frozenset[str] = frozenset(
    {
        "centos7",
        "debian10",
        "debian11",
        "rockylinux8",
        "ubuntu2004",
        "ubuntu2204",
        "ubuntu2404",
        "macos",
        "macos_arm64",
        "windows",
    }
)

MATRIX_VAR_RE = re.compile(r"\$\{\{\s*(\w+)\s*\}\}")


@dataclasses.dataclass(frozen=True)
class ExpandedTask:
    """One concrete presubmit task after matrix expansion."""

    task_id: str
    platform: str
    bazel: str
    name: str
    build_targets: tuple[str, ...]
    test_targets: tuple[str, ...]
    build_flags: tuple[str, ...]
    test_flags: tuple[str, ...]


def platform_family(platform: str) -> str:
    """Return coarse OS family: linux, macos, or windows."""
    p = platform.lower()
    if p.startswith("macos"):
        return "macos"
    if p.startswith("windows"):
        return "windows"
    return "linux"


def host_family() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def agent_queue(platform: str) -> str:
    """BCR-style Buildkite agent queue for a presubmit platform string."""
    p = platform.lower()
    if p == "macos":
        return "macos"
    if p == "macos_arm64":
        return "macos_arm64"
    if p.startswith("windows"):
        return "windows"
    return "default"


def substitute_matrix_vars(value: Any, matrix_row: dict[str, Any]) -> Any:
    """Replace ``${{ var }}`` placeholders in strings using *matrix_row*."""
    if isinstance(value, str):
        m = MATRIX_VAR_RE.fullmatch(value.strip())
        if m:
            key = m.group(1)
            if key not in matrix_row:
                raise ValueError(f"Unknown matrix variable: {key}")
            return matrix_row[key]
        return MATRIX_VAR_RE.sub(
            lambda mm: str(matrix_row[mm.group(1)]) if mm.group(1) in matrix_row else mm.group(0),
            value,
        )
    if isinstance(value, list):
        return [substitute_matrix_vars(v, matrix_row) for v in value]
    if isinstance(value, dict):
        return {k: substitute_matrix_vars(v, matrix_row) for k, v in value.items()}
    return value


def _matrix_cartesian(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand top-level matrix keys into list of row dicts."""
    if not matrix:
        return [{}]
    keys = [k for k, v in matrix.items() if isinstance(v, list) and v]
    if not keys:
        return [{}]
    rows: list[dict[str, Any]] = [{}]
    for key in keys:
        vals = matrix[key]
        new_rows: list[dict[str, Any]] = []
        for row in rows:
            for v in vals:
                nr = dict(row)
                nr[key] = v
                new_rows.append(nr)
        rows = new_rows
    return rows


def _task_needs_matrix(task: dict[str, Any], matrix_keys: Iterable[str]) -> bool:
    """True if any task field value references a matrix key."""
    keys = set(matrix_keys)

    def walk(x: Any) -> bool:
        if isinstance(x, str):
            for m in MATRIX_VAR_RE.finditer(x):
                if m.group(1) in keys:
                    return True
            return False
        if isinstance(x, list):
            return any(walk(i) for i in x)
        if isinstance(x, dict):
            return any(walk(i) for i in x.values())
        return False

    return walk(task)


def _expand_tasks_for_matrix(
    tasks: dict[str, Any],
    matrix: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return list of (task_id, concrete_task_dict) with matrix applied."""
    matrix_keys = list(matrix.keys()) if matrix else []
    rows = _matrix_cartesian(matrix)
    out: list[tuple[str, dict[str, Any]]] = []
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        if matrix and _task_needs_matrix(task, matrix_keys):
            for row in rows:
                concrete = substitute_matrix_vars(task, row)
                if not isinstance(concrete, dict):
                    continue
                out.append((task_id, concrete))
        else:
            concrete = substitute_matrix_vars(task, rows[0] if rows else {})
            if isinstance(concrete, dict):
                out.append((task_id, concrete))
    return out


def collect_tasks(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (matrix, tasks) merging top-level ``tasks`` and ``bcr_test_module``."""
    matrix: dict[str, Any] = {}
    tasks: dict[str, Any] = {}

    top_matrix = doc.get("matrix") or {}
    if isinstance(top_matrix, dict):
        matrix.update(top_matrix)

    top_tasks = doc.get("tasks") or {}
    if isinstance(top_tasks, dict):
        tasks.update(top_tasks)

    btm = doc.get("bcr_test_module")
    if isinstance(btm, dict):
        m = btm.get("matrix") or {}
        if isinstance(m, dict):
            for k, v in m.items():
                matrix.setdefault(k, v)
        t = btm.get("tasks") or {}
        if isinstance(t, dict):
            tasks.update(t)

    return matrix, tasks


def validate_presubmit(doc: dict[str, Any]) -> list[str]:
    """Return list of human-readable errors (empty if valid)."""
    errors: list[str] = []
    matrix, tasks = collect_tasks(doc)
    if not tasks:
        errors.append("presubmit.yml must define at least one task in `tasks` or `bcr_test_module.tasks`")
        return errors

    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            errors.append(f"Task {task_id!r}: expected a mapping, got {type(task).__name__}")
            continue
        if "bazel" not in task:
            errors.append(f"Task {task_id!r}: missing required field `bazel`")

    # Validate matrix-referenced platforms / bazel after expansion
    try:
        expanded = expand_presubmit(doc)
    except ValueError as e:
        errors.append(str(e))
        return errors

    for et in expanded:
        if et.platform not in KNOWN_PLATFORMS:
            errors.append(
                f"Task {et.task_id!r}: unknown platform {et.platform!r} "
                f"(known: {', '.join(sorted(KNOWN_PLATFORMS))})"
            )
        if not str(et.bazel).strip():
            errors.append(f"Task {et.task_id!r}: empty `bazel` after matrix expansion")

    return errors


def expand_presubmit(doc: dict[str, Any]) -> list[ExpandedTask]:
    """Expand matrix and return concrete :class:`ExpandedTask` rows."""
    matrix, tasks = collect_tasks(doc)
    expanded_raw = _expand_tasks_for_matrix(tasks, matrix)
    out: list[ExpandedTask] = []
    for task_id, t in expanded_raw:
        platform = str(t.get("platform", "")).strip()
        bazel = str(t.get("bazel", "")).strip()
        name = str(t.get("name", task_id))
        bt = t.get("build_targets") or []
        tt = t.get("test_targets") or []
        bf = t.get("build_flags") or []
        tf = t.get("test_flags") or []
        if not isinstance(bt, list):
            raise ValueError(f"Task {task_id!r}: build_targets must be a list")
        if not isinstance(tt, list):
            raise ValueError(f"Task {task_id!r}: test_targets must be a list")
        if not isinstance(bf, list):
            raise ValueError(f"Task {task_id!r}: build_flags must be a list")
        if not isinstance(tf, list):
            raise ValueError(f"Task {task_id!r}: test_flags must be a list")
        out.append(
            ExpandedTask(
                task_id=task_id,
                platform=platform,
                bazel=bazel,
                name=name,
                build_targets=tuple(str(x) for x in bt),
                test_targets=tuple(str(x) for x in tt),
                build_flags=tuple(str(x) for x in bf),
                test_flags=tuple(str(x) for x in tf),
            )
        )
    return out


def rewrite_llvm_project_label(label: str) -> str:
    """Turn ``@llvm-project//foo`` into ``//foo`` for in-tree module runs."""
    if label.startswith("@llvm-project//"):
        return "//" + label[len("@llvm-project//") :]
    return label


def filter_by_platform_family(
    expanded: list[ExpandedTask], family: str
) -> list[ExpandedTask]:
    return [et for et in expanded if platform_family(et.platform) == family]


def find_expanded_task(
    expanded: list[ExpandedTask],
    task_id: str,
    platform: str | None,
    bazel: str | None,
) -> ExpandedTask:
    """Pick the single matching expanded task or raise ``ValueError``."""
    candidates = [et for et in expanded if et.task_id == task_id]
    if not candidates:
        raise ValueError(f"No expanded task with id {task_id!r}")
    if platform is not None:
        candidates = [et for et in candidates if et.platform == platform]
    if bazel is not None:
        candidates = [et for et in candidates if et.bazel == bazel]
    if len(candidates) != 1:
        raise ValueError(
            f"Ambiguous or missing task {task_id!r} for platform={platform!r} bazel={bazel!r} "
            f"({len(candidates)} matches)"
        )
    return candidates[0]


def versions_from_git_diff_lines(lines: str) -> list[str]:
    """Parse ``git diff --name-only`` output for ``versions/<name>/`` paths."""
    versions: set[str] = set()
    for line in lines.splitlines():
        line = line.strip()
        m = re.match(r"versions/([^/]+)/", line)
        if m:
            versions.add(m.group(1))
    return sorted(versions)


def changed_version_dirs(git_base_ref: str, repo_root: str) -> list[str]:
    """Return sorted unique ``versions/X`` directory names changed vs *git_base_ref*."""
    import subprocess

    out = subprocess.run(
        ["git", "diff", f"{git_base_ref}...HEAD", "--name-only", "--pretty=format:"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {out.returncode}): {out.stderr.strip() or out.stdout.strip()}"
        )
    return versions_from_git_diff_lines(out.stdout)


def read_version_string(llvm_version: str, versions_dir: str) -> str:
    p = Path(versions_dir) / llvm_version / "version.txt"
    if p.is_file():
        return p.read_text().strip()
    return llvm_version
