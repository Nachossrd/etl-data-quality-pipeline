"""Tests del parser de precios. Incluye regresiones de bugs históricos."""

import pandas as pd
import pytest

from rules.prices import parse_price


# ─── Casos básicos (pre-existentes) ──────────────────────────────────────────
def test_parse_price_basics():
    assert parse_price("USD 15.50") == 15.50
    assert parse_price("15,50") == 15.50
    assert parse_price("40 mil") == 40000.0
    assert parse_price("un millon") == 1_000_000.0
    assert parse_price(None) is None
    assert pd.isna(parse_price(pd.NA))


# ─── Regresión: multi-thousand-separator ─────────────────────────────────────
def test_multi_dot_thousands_es_locale():
    """BUG histórico: $1.200.000 devolvía 1.2 (regex solo capturaba primer .X)."""
    assert parse_price("$1.200.000") == 1_200_000.0
    assert parse_price("1.500.000") == 1_500_000.0
    assert parse_price("10.000.000") == 10_000_000.0


def test_multi_comma_thousands_us_locale():
    """BUG histórico: $1,200,000 devolvía 1200 (mismo regex bug)."""
    assert parse_price("$1,200,000") == 1_200_000.0
    assert parse_price("10,000,000") == 10_000_000.0


def test_mixed_separators_es():
    """ES: dot=miles, comma=decimal."""
    assert parse_price("1.200,50") == 1200.50
    assert parse_price("10.000,99") == 10_000.99


def test_mixed_separators_en():
    """EN: comma=miles, dot=decimal."""
    assert parse_price("1,200.50") == 1200.50
    assert parse_price("10,000.99") == 10_000.99


# ─── Regresión: double-magnitude ─────────────────────────────────────────────
def test_text_magnitudes_no_double_apply():
    """BUG histórico: 'un millon' devolvía 10^12 (text_to_number aplicaba
    'millon' Y LUEGO el loop de magnitude lo aplicaba de nuevo)."""
    assert parse_price("un millon") == 1_000_000.0
    assert parse_price("sesenta lucas") == 60_000.0
    assert parse_price("dos millones") == 2_000_000.0
    assert parse_price("cuatro mil") == 4_000.0


def test_digit_with_magnitude_word():
    """Magnitud aplicada UNA vez cuando el número viene de regex."""
    assert parse_price("40 mil") == 40_000.0
    assert parse_price("3.5 millones") == 3_500_000.0
    assert parse_price("100 lucas") == 100_000.0


# ─── Negativos y signos ──────────────────────────────────────────────────────
def test_signed_amounts():
    assert parse_price("-500") == -500.0
    assert parse_price("+1500") == 1500.0
    assert parse_price("-1.500,75") == -1500.75


# ─── Casos degenerados ──────────────────────────────────────────────────────
def test_degenerate_inputs():
    assert parse_price("") is None
    assert parse_price("   ") is None
    assert parse_price("abc") is None
    # Una sola palabra de magnitud sin número → 1 * magnitude
    assert parse_price("mil") == 1000.0


def test_unicode_whitespace():
    """No-break-space y narrow-no-break-space comunes en exports de Excel."""
    assert parse_price("$1 500") == 1500.0
    assert parse_price("1 200") == 1200.0
