"""
tandem.Immutable

A read-only wrapper for module-level constants. All Tandem computed
tasks must only read globals that are wrapped in tandem.Immutable,
or else the independence validator will raise an error.

Usage:
    NUM = Immutable(15)

The value is stored inside the wrapper. Read it back with .value. Any
attempt to modify the wrapper raises AttributeError.
"""

from __future__ import annotations

import inspect
from typing import Any

# Registry

_IMMUTABLE_REGISTRY: dict[str, set[str]] = {}


def _register(module_name: str, var_name: str) -> None:
    _IMMUTABLE_REGISTRY.setdefault(module_name, set()).add(var_name)


def all_immutable_names(module_name: str) -> set[str]:
    """All immutable names registered for a module.
    Called by the independence validator and the compiler scanner."""
    return set(_IMMUTABLE_REGISTRY.get(module_name, set()))


# Name inference

def _infer_name(depth: int) -> tuple[str, str | None]:
    """Return (module_name, var_name) from `depth` frames up."""
    frame = inspect.currentframe()
    try:
        for _ in range(depth):
            if frame is None:
                return "<unknown>", None
            frame = frame.f_back
        if frame is None:
            return "<unknown>", None
        module = frame.f_globals.get("__name__", "<unknown>")
        try:
            source_lines, _ = inspect.findsource(frame)
        except (OSError, TypeError):
            return module, None
        lineno = frame.f_lineno
        if not (0 <= lineno - 1 < len(source_lines)):
            return module, None
        line = source_lines[lineno - 1].strip()
        if "=" not in line:
            return module, None
        lhs = line.split("=", 1)[0].strip()
        if ":" in lhs:
            lhs = lhs.split(":", 1)[0].strip()
        return module, (lhs if lhs.isidentifier() else None)
    finally:
        del frame


# Immutable wrapper

class Immutable:
    """
    Read-only wrapper for a module-level constant.

        NUM = Immutable(15)
        NUM.value        # -> 15

    Creating one registers its variable name so the independence validator
    knows a task is allowed to read it. Writing to it raises AttributeError.
    """

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        object.__setattr__(self, "_value", value)
        module, name = _infer_name(depth=2)
        if name:
            _register(module, name)

    # read-only enforcement

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Immutable is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Immutable is read-only")

    # inner value access

    @property
    def value(self) -> Any:
        """Unwrap the inner value explicitly."""
        return object.__getattribute__(self, "_value")

    def __repr__(self) -> str:
        return f"Immutable({object.__getattribute__(self, '_value')!r})"

    def __eq__(self, other: Any) -> bool:
        v = object.__getattribute__(self, "_value")
        if isinstance(other, Immutable):
            return v == object.__getattribute__(other, "_value")
        return v == other

    def __hash__(self) -> int:
        v = object.__getattribute__(self, "_value")
        try:
            return hash(v)
        except TypeError:
            return id(v)
