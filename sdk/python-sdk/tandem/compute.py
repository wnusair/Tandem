"""
@tandem.compute(batch=1, timeout_ms=50)

Marks a function as a Tandem compute task.

Calling the function normally just runs it locally -- handy for testing, and
it's exactly what happens inside a node:

    @tandem.compute(batch=1, timeout_ms=50)
    def crunch(n):
        return sum(i * i for i in range(n))

    crunch(1000)                 # runs locally, returns an int

To run it on a node instead, use `.submit(...)`, which returns a ComputeFuture
right away so you can start many at once and collect them later:

    future = crunch.submit(10_000_000)
    future.result(timeout=30)    # blocks for the remote answer

    results = tandem.gather(*[crunch.submit(n) for n in sizes])

Parameters
----------
batch : int
    Hint: the server may collect this many calls before dispatching them as a
    group to a node. Default 1.
timeout_ms : int
    Per-call time budget, converted into the node's fuel budget. Default 50ms.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from tandem.validator import validate_independence

F = TypeVar("F", bound=Callable[..., Any])


def compute(batch: int = 1, timeout_ms: int = 50) -> Callable[[F], F]:
    """
    Decorator factory. Validates that the function is split-independent, attaches
    Tandem metadata, and returns a wrapper you can call locally or `.submit()`.

    Raises
    ------
    TandemValidationError
        If the decorated function is not split-independent.
    """

    def decorator(func: F) -> F:
        validate_independence(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **call_kwargs: Any) -> Any:
            # A bare call runs locally: least-surprising, unit-test friendly, and
            # it's what runs inside a node. Use `.submit(...)` to go remote.
            return func(*args, **call_kwargs)

        def submit(*args: Any, **call_kwargs: Any):
            # Send this call to a node and get a ComputeFuture back immediately.
            from tandem.rpc import submit_task

            task_name = f"{func.__module__}:{func.__name__}"
            return submit_task(task_name, args, call_kwargs)

        wrapper.submit = submit                    # type: ignore[attr-defined]
        wrapper.__tandem_kind__ = "compute"        # type: ignore[attr-defined]
        wrapper.__tandem_batch__ = batch           # type: ignore[attr-defined]
        wrapper.__tandem_timeout_ms__ = timeout_ms # type: ignore[attr-defined]
        wrapper.__tandem_original__ = func         # type: ignore[attr-defined]
        # Expose the raw function as `.function` so the CLI's static analysis can
        # inspect the user's real code, not this wrapper.
        wrapper.function = func                    # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
