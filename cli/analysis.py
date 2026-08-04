from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable


@dataclass(frozen=True)
class Diagnostic:
    """Static analysis diagnostic produced during task validation."""

    level: str
    code: str
    message: str
    file: str
    line: int
    column: int
    task: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "task": self.task,
        }


@dataclass(frozen=True)
class TaskAnalysis:
    task_name: str
    diagnostics: tuple[Diagnostic, ...]

    @property
    def has_errors(self) -> bool:
        return any(diagnostic.level == "error" for diagnostic in self.diagnostics)


@dataclass(frozen=True)
class AnalysisReport:
    task_analyses: tuple[TaskAnalysis, ...]

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        flattened: list[Diagnostic] = []
        for analysis in self.task_analyses:
            flattened.extend(analysis.diagnostics)
        return tuple(flattened)

    @property
    def has_errors(self) -> bool:
        return any(analysis.has_errors for analysis in self.task_analyses)

    @property
    def error_count(self) -> int:
        return sum(1 for diagnostic in self.diagnostics if diagnostic.level == "error")


@dataclass(frozen=True)
class AssignmentInfo:
    name: str
    line: int
    kind: str


@dataclass(frozen=True)
class ModuleIndex:
    path: Path
    tree: ast.Module
    source: str
    function_nodes: dict[str, ast.AST]
    module_assignments: dict[str, AssignmentInfo]
    tandem_module_aliases: set[str]
    tandem_symbol_aliases: dict[str, str]


_DISALLOWED_CALLS = {
    ("open",),
    ("requests", "get"),
    ("requests", "post"),
    ("requests", "put"),
    ("requests", "delete"),
    ("requests", "patch"),
    ("requests", "request"),
    ("httpx", "get"),
    ("httpx", "post"),
    ("httpx", "put"),
    ("httpx", "delete"),
    ("httpx", "patch"),
    ("urllib", "request", "urlopen"),
    ("socket", "socket"),
}

_DISALLOWED_ATTRIBUTE_NAMES = {
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
}


def _iter_target_names(target: ast.expr) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _iter_target_names(element)


def _matches_tandem_symbol(
    expr: ast.expr,
    symbol_name: str,
    *,
    module_aliases: set[str],
    symbol_aliases: dict[str, str],
) -> bool:
    if isinstance(expr, ast.Attribute):
        return (
            isinstance(expr.value, ast.Name)
            and expr.value.id in module_aliases
            and expr.attr == symbol_name
        )

    if isinstance(expr, ast.Name):
        return symbol_aliases.get(expr.id) == symbol_name

    return False


def _assignment_kind(
    expr: ast.expr,
    *,
    module_aliases: set[str],
    symbol_aliases: dict[str, str],
) -> str:
    if isinstance(expr, ast.Call):
        if _matches_tandem_symbol(
            expr.func,
            "immutable",
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        ):
            return "immutable"
        if _matches_tandem_symbol(
            expr.func,
            "constant",
            module_aliases=module_aliases,
            symbol_aliases=symbol_aliases,
        ):
            return "constant"

    return "plain"


def _build_module_index(module: ModuleType) -> ModuleIndex:
    module_path_value = getattr(module, "__file__", None)
    if not module_path_value:
        raise RuntimeError(
            f"Loaded module {module.__name__!r} does not have a source file."
        )

    module_path = Path(module_path_value).resolve()
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    tandem_module_aliases: set[str] = set()
    tandem_symbol_aliases: dict[str, str] = {}

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "tandem":
                    tandem_module_aliases.add(bound_name)
        elif isinstance(statement, ast.ImportFrom) and statement.module == "tandem":
            for alias in statement.names:
                tandem_symbol_aliases[alias.asname or alias.name] = alias.name

    function_nodes: dict[str, ast.AST] = {}
    module_assignments: dict[str, AssignmentInfo] = {}

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_nodes[statement.name] = statement
        elif isinstance(statement, ast.Assign):
            kind = _assignment_kind(
                statement.value,
                module_aliases=tandem_module_aliases,
                symbol_aliases=tandem_symbol_aliases,
            )
            for target in statement.targets:
                for name in _iter_target_names(target):
                    module_assignments[name] = AssignmentInfo(
                        name=name,
                        line=statement.lineno,
                        kind=kind,
                    )
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            kind = _assignment_kind(
                statement.value,
                module_aliases=tandem_module_aliases,
                symbol_aliases=tandem_symbol_aliases,
            )
            for name in _iter_target_names(statement.target):
                module_assignments[name] = AssignmentInfo(
                    name=name,
                    line=statement.lineno,
                    kind=kind,
                )

    return ModuleIndex(
        path=module_path,
        tree=tree,
        source=source,
        function_nodes=function_nodes,
        module_assignments=module_assignments,
        tandem_module_aliases=tandem_module_aliases,
        tandem_symbol_aliases=tandem_symbol_aliases,
    )


def _find_function_node(index: ModuleIndex, func: Any) -> ast.AST | None:
    matches = [
        node
        for node in ast.walk(index.tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == func.__name__
    ]
    if not matches:
        return None

    source_line = getattr(func, "__code__", None)
    if source_line is None:
        return matches[0]

    return min(
        matches, key=lambda node: abs(node.lineno - func.__code__.co_firstlineno)
    )


def _find_name_location(node: ast.AST, name: str) -> tuple[int, int]:
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Name)
            and isinstance(child.ctx, ast.Load)
            and child.id == name
        ):
            return child.lineno, child.col_offset

    return getattr(node, "lineno", 1), getattr(node, "col_offset", 0)


def _qualified_name(expr: ast.expr) -> tuple[str, ...]:
    parts: list[str] = []
    cursor: ast.expr | None = expr

    while isinstance(cursor, ast.Attribute):
        parts.insert(0, cursor.attr)
        cursor = cursor.value

    if isinstance(cursor, ast.Name):
        parts.insert(0, cursor.id)
        return tuple(parts)

    return ()


def _iter_calls(root: ast.AST) -> list[ast.Call]:
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            calls.append(node)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is root:
                for decorator in node.decorator_list:
                    self.visit(decorator)
                for statement in node.body:
                    self.visit(statement)
                if node.returns is not None:
                    self.visit(node.returns)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is root:
                for decorator in node.decorator_list:
                    self.visit(decorator)
                for statement in node.body:
                    self.visit(statement)
                if node.returns is not None:
                    self.visit(node.returns)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return None

    Visitor().visit(root)
    return calls


def _find_disallowed_calls(node: ast.AST) -> list[tuple[ast.Call, str]]:
    violations: list[tuple[ast.Call, str]] = []

    for call in _iter_calls(node):
        qualified = _qualified_name(call.func)
        if qualified in _DISALLOWED_CALLS:
            violations.append((call, ".".join(qualified)))
            continue

        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in _DISALLOWED_ATTRIBUTE_NAMES
        ):
            violations.append((call, call.func.attr))

    return violations


def _immutable_names_for_module(module: ModuleType) -> frozenset[str]:
    """Names the SDK's immutable registry recorded for this module.

    ``tandem.Immutable(...)`` registers each wrapped module-level constant at
    import time, keyed by module name, and the SDK exposes them through
    ``all_immutable_names`` precisely so this build-time scanner can tell which
    globals a task is allowed to read. We consult that registry instead of
    re-deriving it from the AST, so any ``tandem.Immutable(...)`` binding is
    honoured with one source of truth.

    The module has already been imported by discovery, so the registry is
    populated by the time we get here. If the SDK can't be imported (e.g. an
    isolated unit test hands us a hand-built module), we degrade to "nothing is
    registered" and let the caller fall back to syntactic detection.
    """
    try:
        from tandem.immutable import all_immutable_names
    except Exception:
        return frozenset()
    try:
        return frozenset(all_immutable_names(getattr(module, "__name__", "")))
    except Exception:
        return frozenset()


class _AnalysisContext:
    def __init__(self, module: ModuleType, index: ModuleIndex) -> None:
        self._module = module
        self._index = index
        self._immutable_names = _immutable_names_for_module(module)
        self._helper_cache: dict[str, tuple[Diagnostic, ...]] = {}
        self._helper_stack: set[str] = set()

    def analyze_task(self, task_name: str, task: Any) -> TaskAnalysis:
        function = getattr(task, "function", task)
        diagnostics = tuple(self._analyze_function(task_name, function))
        return TaskAnalysis(task_name=task_name, diagnostics=diagnostics)

    def _analyze_helper(
        self, helper_name: str, helper_func: Any
    ) -> tuple[Diagnostic, ...]:
        cache_key = f"{helper_func.__module__}:{helper_func.__qualname__}"
        if cache_key in self._helper_cache:
            return self._helper_cache[cache_key]

        if cache_key in self._helper_stack:
            node = _find_function_node(self._index, helper_func)
            line = getattr(node, "lineno", 1) if node is not None else 1
            column = getattr(node, "col_offset", 0) if node is not None else 0
            diagnostic = Diagnostic(
                level="error",
                code="TANDEM006",
                message=(
                    f"Helper `{helper_name}` is recursive or mutually recursive. "
                    "Recursive helper compilation is not supported in this scaffold."
                ),
                file=str(self._index.path),
                line=line,
                column=column,
                task=helper_name,
            )
            return (diagnostic,)

        self._helper_stack.add(cache_key)
        diagnostics = tuple(self._analyze_function(helper_name, helper_func))
        self._helper_stack.remove(cache_key)
        self._helper_cache[cache_key] = diagnostics
        return diagnostics

    def _analyze_function(self, task_name: str, func: Any) -> list[Diagnostic]:
        node = _find_function_node(self._index, func)
        if node is None:
            return [
                Diagnostic(
                    level="error",
                    code="TANDEM005",
                    message=(
                        f"Could not locate source AST for `{func.__qualname__}`. "
                        "Only source-backed Python functions can be analyzed and compiled."
                    ),
                    file=str(self._index.path),
                    line=1,
                    column=0,
                    task=task_name,
                )
            ]

        diagnostics: list[Diagnostic] = []
        closure_vars = inspect.getclosurevars(func)

        for name in sorted(closure_vars.nonlocals):
            line, column = _find_name_location(node, name)
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="TANDEM001",
                    message=(
                        f"Task captures outer-scope value `{name}`. Tandem tasks must not depend "
                        "on closure state unless the value is moved to a module-level "
                        "`tandem.Immutable(...)` binding."
                    ),
                    file=str(self._index.path),
                    line=line,
                    column=column,
                    task=task_name,
                )
            )

        for name, value in closure_vars.globals.items():
            if name in self._index.module_assignments:
                assignment = self._index.module_assignments[name]
                marked_immutable = (
                    assignment.kind in {"immutable", "constant"}
                    or name in self._immutable_names
                )
                if not marked_immutable:
                    line, column = _find_name_location(node, name)
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="TANDEM002",
                            message=(
                                f"Task reads module-level value `{name}` which is not marked "
                                "immutable. Wrap it in a module-level `tandem.Immutable(...)` "
                                "binding so every node freezes in the same value."
                            ),
                            file=str(self._index.path),
                            line=line,
                            column=column,
                            task=task_name,
                        )
                    )
                continue

            if inspect.isfunction(value) and value.__module__ == self._module.__name__:
                helper_diagnostics = self._analyze_helper(name, value)
                if any(
                    diagnostic.level == "error" for diagnostic in helper_diagnostics
                ):
                    line, column = _find_name_location(node, name)
                    diagnostics.append(
                        Diagnostic(
                            level="error",
                            code="TANDEM003",
                            message=(
                                f"Task reads or calls helper `{name}` which violates the Tandem "
                                "independence rule."
                            ),
                            file=str(self._index.path),
                            line=line,
                            column=column,
                            task=task_name,
                        )
                    )

        for call, call_name in _find_disallowed_calls(node):
            diagnostics.append(
                Diagnostic(
                    level="error",
                    code="TANDEM004",
                    message=(
                        f"Task performs unrouted I/O via `{call_name}`. Use a Tandem runtime "
                        "interface instead of direct file or network access."
                    ),
                    file=str(self._index.path),
                    line=call.lineno,
                    column=call.col_offset,
                    task=task_name,
                )
            )

        return diagnostics


def analyze_tasks(module: ModuleType, tasks: dict[str, Any]) -> AnalysisReport:
    """Run static validation over all discovered Tandem tasks in a module."""

    index = _build_module_index(module)
    context = _AnalysisContext(module, index)
    analyses = [
        context.analyze_task(task_name, task)
        for task_name, task in sorted(tasks.items())
    ]
    return AnalysisReport(task_analyses=tuple(analyses))
