"""
Split-independence validator.

A tandem task should be independent: running it on one node must give the same
answer as running it on any other. The compiler freezes the task's whole module
into its WASM component, so a task may freely *read* module globals -- helper
functions, imports, constants -- because every node runs the same frozen copy.

The one thing that breaks independence is *writing* to a module global and
expecting the change to be shared: each node has its own isolated copy, so the
write goes nowhere the other nodes can see. That's what this validator flags.
(Writing to a name declared `tandem.immutable(...)` is likewise an error.)

This runs at decoration time (inside @tandem.compute and tandem.split) so
violations surface immediately when the module is imported, rather than at build
or first call.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import textwrap
from typing import Callable

from tandem.errors import TandemValidationError
from tandem.immutable import all_immutable_names

_BUILTIN_NAMES = set(dir(builtins))


class _FreeVariableVisitor(ast.NodeVisitor):
    """
    Walk a function definition and collect every Name node that is *written*
    without being bound as a local/parameter in the function's own scope --
    those writes to shared module state are what break independence.

    Scope stack tracks:
      - function parameters
      - local assignments (including loop variables, with-targets,
        comprehension variables)
      - nested function/comprehension scopes (each gets its own frame)
    """

    def __init__(self) -> None:
        self.free_writes: list[tuple[str, int]] = []
        self._scopes: list[set[str]] = [set()]

    def _bound_in_any_scope(self, name: str) -> bool:
        return any(name in scope for scope in self._scopes)

    def _bind(self, name: str) -> None:
        self._scopes[-1].add(name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scopes.append(set())
        args = node.args
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            self._bind(a.arg)
        if args.vararg:
            self._bind(args.vararg.arg)
        if args.kwarg:
            self._bind(args.kwarg.arg)
        for stmt in node.body:
            self.visit(stmt)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _visit_comprehension(self, node: ast.AST) -> None:
        self._scopes.append(set())
        for gen in node.generators:  # type: ignore[attr-defined]
            self.visit(gen.iter)
            self._bind_target(gen.target)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        if hasattr(node, "elt"):
            self.visit(node.elt)  # type: ignore[attr-defined]
        if hasattr(node, "key"):
            self.visit(node.key)  # type: ignore[attr-defined]
            self.visit(node.value)  # type: ignore[attr-defined]
        self._scopes.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension
    visit_DictComp = _visit_comprehension

    def _bind_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target)
            if not isinstance(target, ast.Name):
                self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value:
            self.visit(node.value)
        self._bind_target(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # x += 1 both reads and writes x. Writing to a name that isn't bound
        # locally is a write to a module global, which is the thing we flag.
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            name = node.target.id
            if not self._bound_in_any_scope(name):
                self.free_writes.append((name, node.lineno))
            self._bind(name)
        else:
            self.visit(node.target)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind_target(node.target)
        for stmt in node.body:
            self.visit(stmt)
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scopes.append(set())
        for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            self._bind(a.arg)
        self.visit(node.body)
        self._scopes.pop()

    def visit_Name(self, node: ast.Name) -> None:
        # We only care about writes to unbound names. Reading a module global is
        # fine -- the compiler freezes the whole module in -- so Load names are
        # left alone; a Store/Del of a name that isn't bound yet makes it local.
        name = node.id
        if name in _BUILTIN_NAMES:
            return
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            if not self._bound_in_any_scope(name):
                self._bind(name)


def _parse_function_ast(func: Callable) -> ast.FunctionDef:
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError) as e:
        raise TandemValidationError(
            f"Cannot validate '{getattr(func, '__name__', func)}': source "
            f"unavailable ({e}). Functions defined in the REPL or compiled "
            f"extensions cannot be statically checked."
        ) from e

    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node  # type: ignore[return-value]

    raise TandemValidationError(
        f"Could not locate a function definition for "
        f"'{getattr(func, '__name__', func)}' in parsed source."
    )


def validate_independence(func: Callable) -> None:
    """
    Check that a task doesn't try to mutate shared module-level state.

    The compiler freezes the task's whole module into its WASM component, so
    *reading* module globals -- helper functions, imports, constants -- is
    perfectly fine: every node runs the same frozen copy, so everyone sees the
    same values. What doesn't make sense for a distributed task is *writing* to a
    module global and expecting the change to be shared, because each node has
    its own isolated copy. So that's the one thing we flag.

    Raises TandemValidationError on the first write to a module global, naming
    the offending variable and line number.
    """
    module_name = getattr(func, "__module__", "<unknown>")
    immutable_names = all_immutable_names(module_name)

    fn_node = _parse_function_ast(func)
    visitor = _FreeVariableVisitor()
    visitor.visit_FunctionDef(fn_node)

    label = getattr(func, "__name__", "<function>")

    for name, lineno in visitor.free_writes:
        if name in immutable_names:
            raise TandemValidationError(
                f"In '{label}' (line {lineno}): "
                f"immutable variable '{name}' cannot be modified inside a tandem task."
            )
        raise TandemValidationError(
            f"In '{label}' (line {lineno}): "
            f"global variable '{name}' cannot be modified inside a tandem task -- "
            f"each node runs its own frozen copy, so the change wouldn't be shared. "
            f"Use a parameter and return the result instead."
        )
