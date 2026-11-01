from __future__ import annotations

import platform

import pytest

import tokentracker.collector.memory as memory


def test_release_memory_is_safe_to_call() -> None:
    memory.release_memory()


@pytest.mark.skipif(platform.libc_ver()[0] != "glibc", reason="malloc_trim is a glibc function")
def test_malloc_trim_is_found_on_glibc() -> None:
    assert memory._malloc_trim is not None
