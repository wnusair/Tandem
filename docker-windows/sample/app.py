import tandem


# A tiny compute task. It uses a helper function and a plain loop -- ordinary
# pure-Python -- to show the whole compile-and-run path handles real code, not
# just single self-contained functions.
def _sum_up_to(stop):
    total = 0
    for i in range(stop):
        total += i
    return total


@tandem.compute(batch=1, timeout_ms=5000)
def crunch(n):
    return _sum_up_to(n)
