"""Number settings read from the environment and checked (§1.11, §6.9).

``seconds`` and ``whole`` are the one reader of a number setting that meeting-api reads after
boot, inside a sweep, an item or a request: an unset or empty key takes its default, and a value
the service can't run on raises ``SettingsError`` naming the key. The entrypoint reads every such
setting when meeting-api boots (``__main__._check_settings``), so a bad value refuses to start
instead of failing each item that reads it.
"""

from __future__ import annotations

import math
import os

__all__ = ["SettingsError", "seconds", "whole"]


class SettingsError(ValueError):
    """A setting meeting-api can't run on: it refuses to start."""


def _raw(key: str, default: str) -> str:
    return (os.environ.get(key) or "").strip() or default


def seconds(key: str, default: str, *, zero: bool = False) -> float:
    """``key`` as a finite number of seconds above 0 (0 or more with ``zero``)."""
    try:
        value = float(_raw(key, default))
    except ValueError:
        value = math.nan
    if math.isfinite(value) and (value > 0 or (zero and value == 0)):
        return value
    least = "of 0 or more" if zero else "above 0"
    raise SettingsError(f"{key} must be a number of seconds {least}")


def whole(key: str, default: str, *, zero: bool = False) -> int:
    """``key`` as a whole number of at least 1 (of at least 0 with ``zero``)."""
    raw = _raw(key, default)
    least = 0 if zero else 1
    if raw.isascii() and raw.isdigit() and int(raw) >= least:
        return int(raw)
    raise SettingsError(f"{key} must be a whole number of at least {least}")
