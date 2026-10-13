"""
core/serializer.py

Centralized serialization layer for converting Python and MySQL types into
JSON-safe primitives. Every API response and streaming response must pass through
this single serialization boundary before leaving the backend.
"""

import datetime as dt
import decimal
import json
from typing import Any
from flask import jsonify


def serialize(val: Any) -> Any:
    """
    Recursively convert Python / MySQL types into JSON-serializable primitives.

    Handles:
      - datetime.datetime  -> ISO 8601 string
      - datetime.date      -> ISO 8601 string (YYYY-MM-DD)
      - datetime.time      -> ISO 8601 string (HH:MM:SS)
      - datetime.timedelta -> HH:MM:SS formatted string (with sign for negative)
      - decimal.Decimal    -> int (if whole) or float
      - bytes / bytearray  -> UTF-8 decoded string (with replacement for invalid bytes)
      - dict               -> recursive dict sanitization
      - list / tuple / set -> recursive list sanitization
      - primitive types    -> returned unchanged
    """
    if val is None:
        return None

    if isinstance(val, (dt.datetime, dt.date, dt.time)):
        return val.isoformat()

    if isinstance(val, dt.timedelta):
        total_seconds = int(val.total_seconds())
        sign = "-" if total_seconds < 0 else ""
        total_seconds = abs(total_seconds)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"

    if isinstance(val, decimal.Decimal):
        if val % 1 == 0:
            return int(val)
        return float(val)

    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")

    if isinstance(val, dict):
        return {str(k): serialize(v) for k, v in val.items()}

    if isinstance(val, (list, tuple, set)):
        return [serialize(x) for x in val]

    return val


def safe_dumps(obj: Any, **kwargs) -> str:
    """
    Serialize object to a JSON string after recursively processing through
    the central serializer.
    """
    return json.dumps(serialize(obj), **kwargs)


def safe_jsonify(*args, **kwargs):
    """
    Flask response wrapper that recursively converts objects via the central
    serializer before invoking Flask's jsonify.
    """
    if args:
        if len(args) == 1:
            return jsonify(serialize(args[0]))
        return jsonify(serialize(list(args)))
    return jsonify(serialize(kwargs))
