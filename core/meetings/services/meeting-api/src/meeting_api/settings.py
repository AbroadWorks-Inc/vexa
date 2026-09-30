"""Number settings read from the environment and checked (§1.11, §6.9).

``seconds`` and ``whole`` are meeting-api's one reader of a number setting, read at boot or
later inside a sweep, an item or a request: an unset or empty key takes its default, and a value
the service can't run on raises ``SettingsError`` naming the key. ``env`` is the environment to
read (``os.environ`` when omitted). The entrypoint reads every setting read after boot when
meeting-api boots (``__main__._check_settings``), so a bad value refuses to start instead of
failing each item that reads it.
"""

from __future__ import annotations

import math
import os
from typing import Mapping, Optional

__all__ = ["SettingsError", "seconds", "whole"]


class SettingsError(ValueError):
    """A setting meeting-api can't run on: it refuses to start."""


def _raw(key: str, default: str, env: Optional[Mapping[str, str]]) -> str:
    values = os.environ if env is None else env
    return (values.get(key) or "").strip() or default


def seconds(
    key: str,
    default: str,
    *,
    zero: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> float:
    """``key`` as a finite number of seconds above 0 (0 or more with ``zero``)."""
    try:
        value = float(_raw(key, default, env))
    except ValueError:
        value = math.nan
    if math.isfinite(value) and (value > 0 or (zero and value == 0)):
        return value
    least = "of 0 or more" if zero else "above 0"
    raise SettingsError(f"{key} must be a number of seconds {least}")


def whole(
    key: str,
    default: str,
    *,
    zero: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """``key`` as a whole number of at least 1 (of at least 0 with ``zero``)."""
    raw = _raw(key, default, env)
    least = 0 if zero else 1
    if raw.isascii() and raw.isdigit() and int(raw) >= least:
        return int(raw)
    raise SettingsError(f"{key} must be a whole number of at least {least}")
