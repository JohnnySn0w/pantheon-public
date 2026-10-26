"""Shared test guard: PMB-1 tests never touch the network and never construct
``OllamaBackbone`` (unless a module opts in to test the Ollama shape itself,
and even then the network stays blocked)."""
from __future__ import annotations

import contextlib
from unittest import mock

from . import backbone


def install(*, allow_ollama: bool = False) -> contextlib.ExitStack:
    """Start the patches; call ``.close()`` on the result to stop them."""
    stack = contextlib.ExitStack()
    boom = AssertionError("PMB-1 tests must not touch the network")
    stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=boom))
    stack.enter_context(mock.patch("socket.socket.connect", side_effect=boom))
    if not allow_ollama:
        stack.enter_context(mock.patch.object(
            backbone.OllamaBackbone, "__init__", side_effect=AssertionError("OllamaBackbone constructed")))
    return stack
