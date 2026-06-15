import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = ""
    for _ in range(26):
        n, r = divmod(n, 32)
        out = _CROCKFORD[r] + out
    return out
