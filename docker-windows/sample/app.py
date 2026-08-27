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


# Deliberately greedy, to prove the node's memory cap holds: asking for more than
# a guest is allowed must fail the task, not take the node down with it.
@tandem.compute(batch=1, timeout_ms=30000)
def hog(mb):
    blocks = []
    for _ in range(mb // 16):
        blocks.append(bytearray(16 * 1024 * 1024))
    return 16 * len(blocks)
