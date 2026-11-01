from __future__ import annotations

import ctypes

try:
    _malloc_trim = ctypes.CDLL(None).malloc_trim
except (AttributeError, OSError, TypeError):  # not glibc: macOS, musl, Windows
    _malloc_trim = None
else:
    _malloc_trim.argtypes = [ctypes.c_size_t]
    _malloc_trim.restype = ctypes.c_int


def release_memory() -> None:
    """Give free heap pages back to the operating system.

    CPython frees parsed transcripts and query results, but glibc keeps the
    pages for reuse, so without this the resident size stays at the peak of
    the largest scan or request. Where glibc is not the allocator, this does
    nothing.
    """
    if _malloc_trim is not None:
        _malloc_trim(0)
