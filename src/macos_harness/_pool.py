"""Drain temporary Cocoa objects even when the caller has no AppKit event loop."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from types import FunctionType
from typing import ParamSpec, TypeVar

try:
    import objc
except ImportError:
    objc = None

_P = ParamSpec("_P")
_R = TypeVar("_R")
_C = TypeVar("_C", bound=type)


def _pooled(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def invoke(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with objc.autorelease_pool():
            return function(*args, **kwargs)

    return invoke


def pooled_methods(cls: _C) -> _C:
    if objc is not None:
        for name, method in vars(cls).items():
            if not name.startswith("_") and isinstance(method, FunctionType):
                setattr(cls, name, _pooled(method))
    return cls
