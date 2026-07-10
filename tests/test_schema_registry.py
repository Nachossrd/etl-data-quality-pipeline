"""Tests del SchemaRegistry: detección de drift."""

import json

import pandas as pd
import pytest

from core.schema_registry import SchemaRegistry, _fingerprint, _column_dtypes


@pytest.fixture
def reg(tmp_path):
    return SchemaRegistry(schemas_dir=tmp_path)


def test_fingerprint_is_deterministic():
    schema = [{"column": "a", "dtype": "int64"}, {"column": "b", "dtype": "str"}]
    assert _fingerprint(schema) == _fingerprint(schema)
    assert len(_fingerprint(schema)) == 16


def test_fingerprint_differs_on_dtype_change():
    a = [{"column": "x", "dtype": "int64"}]
    b = [{"column": "x", "dtype": "float64"}]
    assert _fingerprint(a) != _fingerprint(b)


def test_first_run_freezes_schema(reg):
    df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    result = reg.evaluate("ventas", df, run_id="r1")
    assert result["first_time"] is True
    assert result["is_fatal"] is False
    assert result["diff"] is None
    assert reg.has("ventas")


def test_second_run_with_same_schema_is_no_op(reg):
    df = pd.DataFrame({"id": [1], "name": ["a"]})
    reg.evaluate("ventas", df)
    result = reg.evaluate("ventas", df)
    assert result["first_time"] is False
    assert result["diff"] is None
    assert result["is_fatal"] is False


def test_missing_column_is_fatal(reg):
    df1 = pd.DataFrame({"id": [1], "name": ["a"]})
    reg.evaluate("ventas", df1)
    df2 = pd.DataFrame({"id": [1]})  # falta "name"
    result = reg.evaluate("ventas", df2)
    assert result["is_fatal"] is True
    assert "name" in result["diff"]["missing_columns"]


def test_dtype_change_int_to_string_is_fatal(reg):
    df1 = pd.DataFrame({"x": [1, 2]})
    reg.evaluate("ds", df1)
    df2 = pd.DataFrame({"x": ["a", "b"]})  # int -> object
    result = reg.evaluate("ds", df2)
    assert result["is_fatal"] is True
    assert "x" in result["diff"]["dtype_changes"]


def test_dtype_promotion_int_to_float_is_warn(reg):
    df1 = pd.DataFrame({"x": [1, 2]})
    reg.evaluate("ds", df1)
    df2 = pd.DataFrame({"x": [1.0, 2.0]})  # int64 -> float64 (compatible)
    result = reg.evaluate("ds", df2)
    assert result["is_fatal"] is False
    assert "x" in result["diff"]["dtype_changes"]


def test_new_column_is_warn(reg):
    df1 = pd.DataFrame({"x": [1]})
    reg.evaluate("ds", df1)
    df2 = pd.DataFrame({"x": [1], "y": [2]})
    result = reg.evaluate("ds", df2)
    assert result["is_fatal"] is False
    assert "y" in result["diff"]["new_columns"]


def test_freeze_does_not_overwrite_existing(reg):
    df1 = pd.DataFrame({"x": [1]})
    record1 = reg.freeze("ds", df1)
    df2 = pd.DataFrame({"x": [1], "y": [2]})
    record2 = reg.freeze("ds", df2)
    # Devuelve el ya congelado, no machacó
    assert record1["fingerprint"] == record2["fingerprint"]
    assert len(record2["schema"]) == 1


def test_load_returns_none_when_missing(reg):
    assert reg.load("no_existe") is None


def test_evaluate_persists_to_disk(reg, tmp_path):
    df = pd.DataFrame({"a": [1]})
    reg.evaluate("xyz", df, run_id="run-1")
    file = tmp_path / "xyz.json"
    assert file.exists()
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["dataset"] == "xyz"
    assert data["run_id"] == "run-1"
    assert data["fingerprint"]
