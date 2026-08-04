"""
tandem.split(runnable, chunk=1) -> g(list[arg]) -> list[result]

Creates a new function that takes a list of arguments, splits them into
chunks, and guarantees that results are returned in the same order as
the inputs.

    def foo(x):
        return x + 3

    goo = tandem.split(foo, chunk=5)

    goo([7, 2, 9])   # returns [10, 5, 12]

On a Tandem server, the input list is divided into chunks (up to the
specified chunk size) and each chunk may be processed on a different
node. The returned list always preserves the original input order.

Parameters
----------
runnable : callable
    A function that accepts a single argument and returns a result.
chunk : int
    Hint to the server for the maximum number of input items to send
    to each node. Default 1.

Returns
-------
callable
    A new function with signature:

        g(list[arg]) -> list[result]

    Calling the returned function locally simply invokes `runnable`
    for each input and returns the collected results in order.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Sequence, TypeVar

from tandem.validator import validate_independence

A = TypeVar("A")
R = TypeVar("R")


def split(
    runnable: Callable[[A], R],
    chunk: int = 1,
) -> Callable[[Sequence[A]], list[R]]:
    """
    Validates that `runnable` is split-independent, attaches Tandem
    metadata, and returns a wrapper.

    The returned function accepts a sequence of inputs, invokes
    `runnable` for each element, and returns the collected results in
    the same order.

    Raises
    ------
    TandemValidationError
        If `runnable` is not split-independent.
    """
    validate_independence(runnable)
    chunk_size = max(1, chunk)

    @functools.wraps(runnable)
    def g(args: Sequence[Any]) -> list[Any]:
        # A bare call runs locally: apply the runnable to each input and return
        # the results in the same order. Running the pieces across nodes goes
        # through the compute path (`.submit()`).
        return [runnable(item) for item in args]

    g.__tandem_kind__ = "split"          # type: ignore[attr-defined]
    g.__tandem_chunk__ = chunk_size      # type: ignore[attr-defined]
    g.__tandem_original__ = runnable     # type: ignore[attr-defined]
    # Expose the raw runnable so the CLI's static analysis inspects the user's
    # real function rather than this wrapper.
    g.function = runnable                # type: ignore[attr-defined]
    return g
