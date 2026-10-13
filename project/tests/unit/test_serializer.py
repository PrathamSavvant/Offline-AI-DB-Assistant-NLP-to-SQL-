"""
tests/unit/test_serializer.py

Comprehensive unit tests for core/serializer.py covering:
  - datetime.datetime, datetime.date, datetime.time
  - datetime.timedelta (positive, negative, zero)
  - decimal.Decimal (integers and floats)
  - bytes and bytearray
  - Nested dicts, lists, tuples, and sets
  - safe_dumps and safe_jsonify
"""

import datetime as dt
import decimal
import json
import pytest
from flask import Flask

from core.serializer import safe_dumps, safe_jsonify, serialize


def test_serialize_primitives():
    assert serialize(None) is None
    assert serialize(123) == 123
    assert serialize(45.67) == 45.67
    assert serialize("hello") == "hello"
    assert serialize(True) is True


def test_serialize_datetime_types():
    now = dt.datetime(2026, 7, 28, 15, 30, 45)
    assert serialize(now) == "2026-07-28T15:30:45"

    today = dt.date(2026, 7, 28)
    assert serialize(today) == "2026-07-28"

    t = dt.time(9, 15, 30)
    assert serialize(t) == "09:15:30"


def test_serialize_timedelta():
    td_positive = dt.timedelta(hours=9, minutes=30, seconds=15)
    assert serialize(td_positive) == "09:30:15"

    td_negative = dt.timedelta(hours=-2, minutes=-15)
    assert serialize(td_negative) == "-02:15:00"

    td_zero = dt.timedelta(seconds=0)
    assert serialize(td_zero) == "00:00:00"

    td_multiday = dt.timedelta(days=1, hours=2, minutes=3, seconds=4)
    assert serialize(td_multiday) == "26:03:04"


def test_serialize_decimal():
    dec_int = decimal.Decimal("100.00")
    assert serialize(dec_int) == 100
    assert isinstance(serialize(dec_int), int)

    dec_float = decimal.Decimal("99.99")
    assert serialize(dec_float) == 99.99
    assert isinstance(serialize(dec_float), float)


def test_serialize_bytes():
    b = b"test bytes"
    assert serialize(b) == "test bytes"

    ba = bytearray(b"bytearray test")
    assert serialize(ba) == "bytearray test"


def test_serialize_nested_structures():
    nested = {
        "schedule": [
            {
                "day": "Monday",
                "start_time": dt.timedelta(hours=9),
                "end_time": dt.timedelta(hours=10, minutes=30),
                "created": dt.datetime(2026, 1, 1, 0, 0, 0),
                "ratio": decimal.Decimal("0.75"),
                "data": b"sample",
            }
        ],
        "tuple_data": (dt.date(2026, 7, 28), decimal.Decimal("10")),
        "set_data": {dt.time(12, 0)},
    }
    result = serialize(nested)
    assert result["schedule"][0]["start_time"] == "09:00:00"
    assert result["schedule"][0]["end_time"] == "10:30:00"
    assert result["schedule"][0]["created"] == "2026-01-01T00:00:00"
    assert result["schedule"][0]["ratio"] == 0.75
    assert result["schedule"][0]["data"] == "sample"
    assert result["tuple_data"] == ["2026-07-28", 10]
    assert result["set_data"] == ["12:00:00"]

    # Verify JSON serializability of the entire output
    dumped = json.dumps(result)
    assert isinstance(dumped, str)


def test_safe_dumps():
    payload = {"duration": dt.timedelta(minutes=45), "cost": decimal.Decimal("15.50")}
    json_str = safe_dumps(payload)
    data = json.loads(json_str)
    assert data["duration"] == "00:45:00"
    assert data["cost"] == 15.5


def test_safe_jsonify():
    app = Flask(__name__)
    with app.app_context():
        response = safe_jsonify(status="ok", duration=dt.timedelta(hours=1, minutes=20))
        assert response.status_code == 200
        data = json.loads(response.get_data(as_text=True))
        assert data["duration"] == "01:20:00"
        assert data["status"] == "ok"
