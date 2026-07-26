"""
Tandem Build Engine
===================

Turns Python functions decorated with ``@tandem.compute`` or created via
``tandem.split`` into ``.wasm`` artifacts and a ``manifest.json``.

Usage (from the CLI)::

    from tandem.builder import build_project
    results = build_project(entry_path="app.py", build_dir=".tandem_build")

The build pipeline has three phases:

1. **Discovery** — walk the source tree, import the entry module, and
   collect every callable stamped with ``__tandem_kind__``.
2. **Transpilation** — translate each function's Python AST into a
   self-contained Rust source string.  Only the restricted subset that
   ``validate_independence`` permits is supported.
3. **Compilation** — invoke ``cargo build --target wasm32-wasip2 --release``
   on the generated Rust, writing the resulting ``.wasm`` artifacts
   into ``<build_dir>/<task_name>.wasm``.

A ``manifest.json`` is written last, referencing every produced ``.wasm``
file by relative path.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable

from tandem.errors import TandemBuildError
from tandem.validator import validate_wasm_types


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class BuildResult:
    """Outcome of a single task compilation."""

    __slots__ = ("task_name", "wasm_path", "wasm_size", "warnings")

    def __init__(
        self,
        task_name: str,
        wasm_path: Path,
        wasm_size: int,
        warnings: list[str],
    ) -> None:
        self.task_name = task_name
        self.wasm_path = wasm_path
        self.wasm_size = wasm_size
        self.warnings = warnings

    def __repr__(self) -> str:
        kb = self.wasm_size / 1024
        return f"<BuildResult {self.task_name} ({kb:.1f} KB)>"


def build_project(
    *,
    entry_path: str,
    build_dir: str = ".tandem_build",
    toml_path: str | None = None,
    on_task_start: Callable[[str], None] | None = None,
    on_task_done: Callable[[BuildResult], None] | None = None,
    on_task_error: Callable[[str, Exception], None] | None = None,
) -> list[BuildResult]:
    """
    Build every Tandem-decorated function discovered from *entry_path*
    into WASM artifacts under *build_dir*.

    Parameters
    ----------
    entry_path : str
        Path to the Python source file to scan for decorated functions.
    build_dir : str
        Output directory for ``.wasm`` artifacts and ``manifest.json``.
    toml_path : str | None
        Optional path to ``tandem.toml`` for deployment metadata.
    on_task_start : callable | None
        Called with the task name when compilation begins.
    on_task_done : callable | None
        Called with the :class:`BuildResult` on success.
    on_task_error : callable | None
        Called with ``(task_name, exception)`` on failure.

    Returns
    -------
    list[BuildResult]
        One result per successfully compiled task.

    Raises
    ------
    TandemBuildError
        If discovery finds zero tasks, or a critical build step fails.
    """
    # Phase 1: Discover
    tasks = discover_tasks(entry_path)
    if not tasks:
        raise TandemBuildError(
            f"No @tandem.compute or tandem.split() functions found in {entry_path}",
            hint="Decorate at least one function with @tandem.compute.",
        )

    # Check for duplicate names
    seen_names: set[str] = set()
    for task_name, _func in tasks:
        if task_name in seen_names:
            raise TandemBuildError(
                f"Duplicate task name '{task_name}' — each @tandem.compute "
                f"function must have a unique name.",
            )
        seen_names.add(task_name)

    # Prepare output directory
    build_path = Path(build_dir)
    build_path.mkdir(parents=True, exist_ok=True)

    # Remove stale .wasm files that no longer correspond to tasks
    for existing in build_path.glob("*.wasm"):
        if existing.stem not in seen_names:
            existing.unlink()

    # Phase 2 & 3: Transpile and compile each task
    results: list[BuildResult] = []
    for task_name, func in tasks:
        if on_task_start:
            on_task_start(task_name)

        try:
            # Validate WASM types
            warnings = validate_wasm_types(func)

            # Transpile Python -> Rust source
            rust_source = transpile_function(func, task_name)

            # Compile Rust -> WASM
            wasm_path = compile_to_wasm(
                rust_source=rust_source,
                task_name=task_name,
                build_dir=build_path,
            )

            result = BuildResult(
                task_name=task_name,
                wasm_path=wasm_path,
                wasm_size=wasm_path.stat().st_size,
                warnings=warnings,
            )
            results.append(result)

            if on_task_done:
                on_task_done(result)

        except Exception as exc:
            if on_task_error:
                on_task_error(task_name, exc)
            else:
                raise

    # Phase 4: Write manifest
    manifest = _build_manifest(results, tasks, toml_path)
    manifest_path = build_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    return results


# ---------------------------------------------------------------------------
# Phase 1: Discovery
# ---------------------------------------------------------------------------


def discover_tasks(entry_path: str) -> list[tuple[str, Callable]]:
    """
    Import the module at *entry_path* and return a list of
    ``(task_name, original_function)`` for every Tandem-decorated callable.
    """
    path = Path(entry_path).resolve()
    if not path.exists():
        raise TandemBuildError(f"Entry file not found: {path}")

    module_name = path.stem
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise TandemBuildError(f"Cannot load module from {path}")

    module = importlib.util.module_from_spec(spec)

    # Add the parent directory to sys.path so relative imports work.
    parent = str(path.parent)
    added = parent not in sys.path
    if added:
        sys.path.insert(0, parent)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(parent)

    tasks: list[tuple[str, Callable]] = []

    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if not callable(obj):
            continue
        kind = getattr(obj, "__tandem_kind__", None)
        if kind not in ("compute", "split"):
            continue

        # Get the original unwrapped function
        original = getattr(obj, "__tandem_original__", obj)
        task_name = getattr(original, "__name__", attr_name)
        tasks.append((task_name, original))

    return tasks


# ---------------------------------------------------------------------------
# Phase 2: Transpilation (Python AST -> Rust source)
# ---------------------------------------------------------------------------

# Maps Python built-in names to Rust equivalents.
_BUILTIN_MAP: dict[str, str] = {
    "len": ".len()",
    "abs": ".abs()",
    "min": "std::cmp::min",
    "max": "std::cmp::max",
    "sum": ".iter().sum()",
    "range": "range",
    "sorted": "sorted_vec",
    "zip": "zip",
    "enumerate": "enumerate",
    "map": "map",
    "filter": "filter",
    "print": "println!",
    "str": "to_string",
    "int": "as i64",
    "float": "as f64",
    "bool": "as bool",
}

# Python type annotation -> Rust type
_TYPE_MAP: dict[str, str] = {
    "int": "i64",
    "float": "f64",
    "bool": "bool",
    "str": "String",
    "bytes": "Vec<u8>",
    "None": "()",
}


def _rust_type(annotation: ast.expr | None) -> str:
    """Convert a Python type annotation AST node to a Rust type string."""
    if annotation is None:
        return "i64"  # default fallback

    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return "()"

    if isinstance(annotation, ast.Name):
        if annotation.id == "None":
            return "()"
        return _TYPE_MAP.get(annotation.id, "i64")

    if isinstance(annotation, ast.Subscript):
        origin = annotation.value
        if isinstance(origin, ast.Name):
            if origin.id in ("list", "List"):
                inner = _rust_type(annotation.slice)
                return f"Vec<{inner}>"
            if origin.id in ("dict", "Dict"):
                if isinstance(annotation.slice, ast.Tuple) and len(annotation.slice.elts) == 2:
                    k = _rust_type(annotation.slice.elts[0])
                    v = _rust_type(annotation.slice.elts[1])
                    return f"std::collections::HashMap<{k}, {v}>"
            if origin.id in ("tuple", "Tuple"):
                if isinstance(annotation.slice, ast.Tuple):
                    parts = [_rust_type(e) for e in annotation.slice.elts]
                    return f"({', '.join(parts)})"

    return "i64"


class _RustTranspiler(ast.NodeVisitor):
    """
    Walks a Python function AST and emits equivalent Rust source.

    Only supports the restricted subset that ``validate_independence``
    permits: arithmetic, comparisons, built-in functions, list/dict
    comprehensions, ``if``/``else``, ``for``, ``while``, early
    ``return``, and f-strings.
    """

    def __init__(self, indent: int = 1) -> None:
        self.lines: list[str] = []
        self._indent = indent

    def _emit(self, line: str) -> None:
        prefix = "    " * self._indent
        self.lines.append(f"{prefix}{line}")

    def _emit_raw(self, line: str) -> None:
        self.lines.append(line)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is None:
            self._emit("return;")
        else:
            val = self._expr(node.value)
            self._emit(f"return {val};")

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._expr(node.value)
        for target in node.targets:
            name = self._expr(target)
            self._emit(f"let mut {name} = {value};")

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        target = self._expr(node.target)
        op = self._binop(node.op)
        value = self._expr(node.value)
        self._emit(f"{target} {op}= {value};")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        target = self._expr(node.target)
        rust_ty = _rust_type(node.annotation)
        if node.value:
            value = self._expr(node.value)
            self._emit(f"let mut {target}: {rust_ty} = {value};")
        else:
            self._emit(f"let mut {target}: {rust_ty};")

    def visit_If(self, node: ast.If) -> None:
        test = self._expr(node.test)
        self._emit(f"if {test} {{")
        self._indent += 1
        for stmt in node.body:
            self.visit(stmt)
        self._indent -= 1
        if node.orelse:
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                self._emit("} else")
                self.visit(node.orelse[0])
            else:
                self._emit("} else {")
                self._indent += 1
                for stmt in node.orelse:
                    self.visit(stmt)
                self._indent -= 1
                self._emit("}")
        else:
            self._emit("}")

    def visit_For(self, node: ast.For) -> None:
        target = self._expr(node.target)
        iter_expr = self._expr(node.iter)
        self._emit(f"for {target} in {iter_expr} {{")
        self._indent += 1
        for stmt in node.body:
            self.visit(stmt)
        self._indent -= 1
        self._emit("}")

    def visit_While(self, node: ast.While) -> None:
        test = self._expr(node.test)
        self._emit(f"while {test} {{")
        self._indent += 1
        for stmt in node.body:
            self.visit(stmt)
        self._indent -= 1
        self._emit("}")

    def visit_Expr(self, node: ast.Expr) -> None:
        # Standalone expression (e.g., a function call)
        val = self._expr(node.value)
        self._emit(f"{val};")

    def visit_Break(self, _node: ast.Break) -> None:
        self._emit("break;")

    def visit_Continue(self, _node: ast.Continue) -> None:
        self._emit("continue;")

    def visit_Pass(self, _node: ast.Pass) -> None:
        # No-op in Rust
        pass

    # --- expression helpers ------------------------------------------------

    def _expr(self, node: ast.expr) -> str:
        """Translate a Python expression AST node to a Rust expression string."""
        if isinstance(node, ast.Constant):
            return self._const(node)
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.BinOp):
            left = self._expr(node.left)
            right = self._expr(node.right)
            op = self._binop(node.op)
            if isinstance(node.op, ast.Pow):
                return f"i64::pow({left}, {right} as u32)"
            if isinstance(node.op, ast.FloorDiv):
                return f"({left} / {right})"
            return f"({left} {op} {right})"
        if isinstance(node, ast.UnaryOp):
            operand = self._expr(node.operand)
            if isinstance(node.op, ast.USub):
                return f"(-{operand})"
            if isinstance(node.op, ast.Not):
                return f"(!{operand})"
            if isinstance(node.op, ast.UAdd):
                return operand
            return operand
        if isinstance(node, ast.Compare):
            return self._compare(node)
        if isinstance(node, ast.BoolOp):
            op = " && " if isinstance(node.op, ast.And) else " || "
            parts = [self._expr(v) for v in node.values]
            return f"({op.join(parts)})"
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Subscript):
            value = self._expr(node.value)
            sl = self._expr(node.slice)
            return f"{value}[{sl} as usize]"
        if isinstance(node, ast.List):
            elts = ", ".join(self._expr(e) for e in node.elts)
            return f"vec![{elts}]"
        if isinstance(node, ast.Tuple):
            elts = ", ".join(self._expr(e) for e in node.elts)
            return f"({elts})"
        if isinstance(node, ast.Dict):
            pairs = []
            for k, v in zip(node.keys, node.values):
                if k is None:
                    continue
                pairs.append(f"({self._expr(k)}, {self._expr(v)})")
            return f"std::collections::HashMap::from([{', '.join(pairs)}])"
        if isinstance(node, ast.IfExp):
            test = self._expr(node.test)
            body = self._expr(node.body)
            orelse = self._expr(node.orelse)
            return f"if {test} {{ {body} }} else {{ {orelse} }}"
        if isinstance(node, ast.ListComp):
            return self._list_comp(node)
        if isinstance(node, ast.JoinedStr):
            return self._fstring(node)
        if isinstance(node, ast.Attribute):
            value = self._expr(node.value)
            return f"{value}.{node.attr}"

        # Fallback — emit a comment marking unsupported syntax
        try:
            source = ast.unparse(node)
        except Exception:
            source = "<unparseable>"
        raise TandemBuildError(
            f"Unsupported Python expression: {source}",
            line=getattr(node, "lineno", None),
            hint="Simplify the expression to use only arithmetic, "
            "comparisons, and built-in functions.",
        )

    def _const(self, node: ast.Constant) -> str:
        if isinstance(node.value, bool):
            return "true" if node.value else "false"
        if isinstance(node.value, int):
            return f"{node.value}_i64"
        if isinstance(node.value, float):
            return f"{node.value}_f64"
        if isinstance(node.value, str):
            escaped = node.value.replace("\\", "\\\\").replace('"', '\\"')
            return f'String::from("{escaped}")'
        if isinstance(node.value, bytes):
            byte_list = ", ".join(str(b) for b in node.value)
            return f"vec![{byte_list}]"
        if node.value is None:
            return "()"
        return repr(node.value)

    def _binop(self, op: ast.operator) -> str:
        ops = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.FloorDiv: "/",
            ast.Mod: "%",
            ast.Pow: "pow",
            ast.BitAnd: "&",
            ast.BitOr: "|",
            ast.BitXor: "^",
            ast.LShift: "<<",
            ast.RShift: ">>",
        }
        return ops.get(type(op), "+")

    def _compare(self, node: ast.Compare) -> str:
        parts = []
        left = self._expr(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self._expr(comparator)
            cmp_op = self._cmpop(op)
            parts.append(f"({left} {cmp_op} {right})")
            left = right
        return " && ".join(parts)

    def _cmpop(self, op: ast.cmpop) -> str:
        ops = {
            ast.Eq: "==",
            ast.NotEq: "!=",
            ast.Lt: "<",
            ast.LtE: "<=",
            ast.Gt: ">",
            ast.GtE: ">=",
        }
        return ops.get(type(op), "==")

    def _call(self, node: ast.Call) -> str:
        args = ", ".join(self._expr(a) for a in node.args)

        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name == "len":
                return f"{self._expr(node.args[0])}.len() as i64"
            if name == "abs":
                return f"{self._expr(node.args[0])}.abs()"
            if name == "range":
                if len(node.args) == 1:
                    return f"0..{self._expr(node.args[0])}"
                if len(node.args) == 2:
                    return f"{self._expr(node.args[0])}..{self._expr(node.args[1])}"
                if len(node.args) == 3:
                    return (
                        f"({self._expr(node.args[0])}..{self._expr(node.args[1])})"
                        f".step_by({self._expr(node.args[2])} as usize)"
                    )
            if name == "print":
                fmt_args = ", ".join("{}")
                return f'println!("{fmt_args}", {args})'
            if name == "sum":
                return f"{self._expr(node.args[0])}.iter().sum::<i64>()"
            if name == "min":
                if len(node.args) == 2:
                    return f"std::cmp::min({args})"
                return f"*{self._expr(node.args[0])}.iter().min().unwrap()"
            if name == "max":
                if len(node.args) == 2:
                    return f"std::cmp::max({args})"
                return f"*{self._expr(node.args[0])}.iter().max().unwrap()"
            if name == "sorted":
                return f"{{ let mut v = {self._expr(node.args[0])}.clone(); v.sort(); v }}"
            if name == "int":
                return f"({self._expr(node.args[0])} as i64)"
            if name == "float":
                return f"({self._expr(node.args[0])} as f64)"
            if name == "str":
                return f"{self._expr(node.args[0])}.to_string()"
            if name == "bool":
                return f"({self._expr(node.args[0])} != 0)"
            # Generic function call
            return f"{name}({args})"

        if isinstance(node.func, ast.Attribute):
            obj = self._expr(node.func.value)
            method = node.func.attr
            if method == "append":
                return f"{obj}.push({args})"
            if method == "pop":
                return f"{obj}.pop().unwrap()"
            if method == "insert":
                return f"{obj}.insert({args})"
            if method == "extend":
                return f"{obj}.extend({args})"
            return f"{obj}.{method}({args})"

        func_expr = self._expr(node.func)
        return f"{func_expr}({args})"

    def _list_comp(self, node: ast.ListComp) -> str:
        if len(node.generators) != 1:
            raise TandemBuildError(
                "Only single-generator list comprehensions are supported "
                "for WASM compilation.",
                line=node.lineno,
            )
        gen = node.generators[0]
        target = self._expr(gen.target)
        iter_expr = self._expr(gen.iter)
        elt = self._expr(node.elt)

        base = f"({iter_expr}).into_iter().map(|{target}| {elt})"

        for if_clause in gen.ifs:
            cond = self._expr(if_clause)
            base = f"{base}.filter(|{target}| {cond})"

        return f"{base}.collect::<Vec<_>>()"

    def _fstring(self, node: ast.JoinedStr) -> str:
        fmt_parts = []
        args = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                fmt_parts.append(value.value.replace("{", "{{").replace("}", "}}"))
            elif isinstance(value, ast.FormattedValue):
                fmt_parts.append("{}")
                args.append(self._expr(value.value))
            else:
                fmt_parts.append("{}")
                args.append(self._expr(value))
        fmt_str = "".join(fmt_parts)
        if args:
            return f'format!("{fmt_str}", {", ".join(args)})'
        return f'String::from("{fmt_str}")'


def transpile_function(func: Callable, task_name: str) -> str:
    """
    Transpile a Python function to a self-contained Rust source string
    that exports a WASI component function.
    """
    source = inspect.getsource(func)
    tree = ast.parse(textwrap.dedent(source))

    fn_node: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_node = node
            break

    if fn_node is None:
        raise TandemBuildError(f"Could not locate function definition for '{task_name}'")

    # Build parameter list
    params: list[str] = []
    all_args = [*fn_node.args.args]
    for arg in all_args:
        rust_ty = _rust_type(arg.annotation)
        params.append(f"{arg.arg}: {rust_ty}")

    ret_ty = _rust_type(fn_node.returns)
    param_str = ", ".join(params)

    # Transpile body
    transpiler = _RustTranspiler(indent=1)
    for stmt in fn_node.body:
        transpiler.visit(stmt)

    body = "\n".join(transpiler.lines)

    # Build complete Rust source
    rust_source = f"""\
//! Auto-generated by Tandem SDK builder. Do not edit.
//!
//! Task: {task_name}

use std::io::Write;

fn {task_name}({param_str}) -> {ret_ty} {{
{body}
}}

fn main() {{
    // WASI entry point — reads input from stdin, calls the task function,
    // and writes output to stdout.
    let mut input = String::new();
    std::io::stdin().read_line(&mut input).unwrap();
    let input = input.trim();

    // Parse input arguments (simple space-separated for scalar types)
    let args: Vec<&str> = input.split_whitespace().collect();

    // Call the task function with parsed arguments
    let result = {task_name}({_generate_arg_parse(all_args)});

    // Write result to stdout
    let output = format!("{{result}}");
    std::io::stdout().write_all(output.as_bytes()).unwrap();
}}
"""
    return rust_source


def _generate_arg_parse(args: list[ast.arg]) -> str:
    """Generate Rust code to parse CLI arguments into function parameters."""
    parts = []
    for i, arg in enumerate(args):
        rust_ty = _rust_type(arg.annotation)
        if rust_ty == "i64":
            parts.append(f"args[{i}].parse::<i64>().unwrap()")
        elif rust_ty == "f64":
            parts.append(f"args[{i}].parse::<f64>().unwrap()")
        elif rust_ty == "bool":
            parts.append(f"args[{i}].parse::<bool>().unwrap()")
        elif rust_ty == "String":
            parts.append(f"args[{i}].to_string()")
        else:
            parts.append(f"args[{i}].parse().unwrap()")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Phase 3: Compilation (Rust source -> WASM binary)
# ---------------------------------------------------------------------------


def compile_to_wasm(
    *,
    rust_source: str,
    task_name: str,
    build_dir: Path,
) -> Path:
    """
    Compile a Rust source string to a WASM binary.

    Creates a temporary Cargo project, writes the source, invokes
    ``cargo build --target wasm32-wasip2 --release``, and copies the
    resulting ``.wasm`` file to *build_dir*.

    The temporary project directory is always cleaned up — Rust source
    never persists on disk.
    """
    wasm_target = "wasm32-wasip2"

    with tempfile.TemporaryDirectory(prefix="tandem_build_") as tmp_dir:
        tmp = Path(tmp_dir)

        # Write Cargo.toml
        cargo_toml = tmp / "Cargo.toml"
        cargo_toml.write_text(
            f'[package]\n'
            f'name = "{task_name}"\n'
            f'version = "0.1.0"\n'
            f'edition = "2021"\n'
            f'\n'
            f'[[bin]]\n'
            f'name = "{task_name}"\n'
            f'path = "src/main.rs"\n'
        )

        # Write source
        src_dir = tmp / "src"
        src_dir.mkdir()
        (src_dir / "main.rs").write_text(rust_source)

        # Compile
        cmd = [
            "cargo", "build",
            "--target", wasm_target,
            "--release",
            "--manifest-path", str(cargo_toml),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise TandemBuildError(
                "cargo not found on PATH. Install Rust: https://rustup.rs",
                hint="Run `rustup target add wasm32-wasip2` after installing.",
            )
        except subprocess.TimeoutExpired:
            raise TandemBuildError(
                f"Compilation of task '{task_name}' timed out after 120 seconds.",
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Try to extract the most useful error line
            error_lines = [
                line for line in stderr.split("\n")
                if "error" in line.lower() and not line.startswith("warning")
            ]
            summary = error_lines[0] if error_lines else stderr[:200]
            raise TandemBuildError(
                f"Rust compilation failed for task '{task_name}': {summary}",
                hint="Check that your function only uses supported Python constructs.",
            )

        # Find the output .wasm file
        wasm_file = (
            tmp / "target" / wasm_target / "release" / f"{task_name}.wasm"
        )
        if not wasm_file.exists():
            raise TandemBuildError(
                f"Compilation succeeded but .wasm file not found at {wasm_file}",
            )

        # Copy to build directory
        dest = build_dir / f"{task_name}.wasm"
        shutil.copy2(str(wasm_file), str(dest))

    return dest


# ---------------------------------------------------------------------------
# Phase 4: Manifest generation
# ---------------------------------------------------------------------------


def _build_manifest(
    results: list[BuildResult],
    tasks: list[tuple[str, Callable]],
    toml_path: str | None,
) -> dict[str, Any]:
    """Build the ``manifest.json`` content."""
    manifest: dict[str, Any] = {}

    # Read deployment name from tandem.toml if available
    if toml_path:
        toml_file = Path(toml_path)
        if toml_file.exists():
            try:
                import tomllib

                parsed = tomllib.loads(toml_file.read_text())
                name = parsed.get("name") or parsed.get("app", {}).get("name")
                if name:
                    manifest["name"] = name
            except Exception:
                pass

    # Build task entries
    func_map = {name: func for name, func in tasks}
    task_entries: list[dict[str, Any]] = []

    for result in results:
        func = func_map.get(result.task_name)
        entry: dict[str, Any] = {
            "name": result.task_name,
            "wasm": f"{result.task_name}.wasm",
        }

        # Pull metadata from the decorated wrapper if available
        if func:
            # Check for split chunk hint
            chunk = getattr(func, "__tandem_chunk__", None)
            if chunk and chunk > 1:
                entry["split"] = {
                    "strategy": "data_parallel",
                    "max_shards": chunk,
                }

            # Check for timeout
            timeout = getattr(func, "__tandem_timeout_ms__", None)
            if timeout:
                entry["timeout_ms"] = timeout

        task_entries.append(entry)

    manifest["tasks"] = task_entries
    return manifest

