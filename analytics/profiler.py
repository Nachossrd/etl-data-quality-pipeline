"""Perfilado semántico de un dataset limpio: qué es cada columna.

Para graficar un dataset que nunca vi, primero hay que responder qué papel
juega cada columna. No es el dtype: `2024` es un entero y es un año; un
`codigo_cliente` es texto y no es una categoría para graficar (tiene 841
valores); una columna de 3 valores repetidos sí lo es.

Roles que asigna:

    date        eje temporal — hay serie de tiempo que mostrar
    measure     magnitud sumable — es lo que va en el eje Y
    dimension   categoría con pocos valores — sirve para agrupar y rankear
    identifier  llave o casi-llave — se cuenta distinto, nunca se suma
    flag        booleano o binario
    text        texto libre de alta cardinalidad — no se grafica
    constant    un solo valor — no aporta información

La regla de oro: **la cardinalidad manda sobre el tipo**. Un entero con 4
valores distintos en 300.000 filas es una dimensión, no una medida; sumarlo
no significaría nada.
"""

import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# Una columna categórica deja de serlo cuando tiene demasiados valores: el
# gráfico se vuelve ilegible y el "top 10" deja de representar al total.
MAX_DIMENSION_UNIQUES = 200
MAX_DIMENSION_RATIO = 0.5
# Por encima de esto, la columna es prácticamente una llave.
IDENTIFIER_RATIO = 0.92

# Nombres que delatan una llave aunque los valores parezcan numéricos.
_ID_TOKENS = ("id", "codigo", "code", "folio", "rut", "sku", "uuid", "key",
              "llave", "numero_documento", "nro", "guid", "hash")
_DATE_TOKENS = ("fecha", "date", "periodo", "period", "mes", "month", "dia",
                "day", "anio", "año", "year", "timestamp", "creado", "updated")
# Columnas que el propio pipeline agrega: son metadatos, no negocio.
_INTERNAL = ("data_quality_flags", "quality_score", "hoja_origen",
             "estado_normalizado", "tipo_movimiento")

# Sufijos que delatan una columna derivada por el propio pipeline. El motor
# semántico deja rastro de cada transformación (`monto`, `monto_original`,
# `monto_parsed`, `monto_final`, `monto_monto_origen`…): son la pista de
# auditoría, no ocho medidas distintas. Si no se excluyen, el tablero muestra
# el mismo total repetido cinco veces y la "medida principal" es una copia.
_DERIVED_SUFFIXES = (
    "_parsed", "_final", "_scale_corrected", "_monto_origen", "_moneda_origen",
    "_currency", "_domain_flag", "_as_date", "_normalized", "_semantic",
    "_original", "_confidence", "_is_serial_date", "_parse_error", "_raw",
)


def _is_internal(name: str) -> bool:
    lower = str(name).lower()
    return lower in _INTERNAL or lower.endswith(_DERIVED_SUFFIXES)


@dataclass
class ColumnProfile:
    name: str
    dtype: str
    role: str
    nulls: int = 0
    null_ratio: float = 0.0
    uniques: int = 0
    unique_ratio: float = 0.0
    internal: bool = False
    stats: Dict[str, Any] = field(default_factory=dict)
    top_values: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "dtype": self.dtype, "role": self.role,
            "nulls": self.nulls, "null_ratio": round(self.null_ratio, 6),
            "uniques": self.uniques, "unique_ratio": round(self.unique_ratio, 6),
            "internal": self.internal, "stats": self.stats,
            "top_values": self.top_values,
        }


@dataclass
class DatasetProfile:
    table: str
    rows: int
    columns: List[ColumnProfile]
    primary_date: Optional[str] = None
    measures: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    identifiers: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "table": self.table, "rows": self.rows,
            "primary_date": self.primary_date,
            "measures": self.measures, "dimensions": self.dimensions,
            "identifiers": self.identifiers,
            "columns": [c.to_dict() for c in self.columns],
        }


def _name_has(name: str, tokens) -> bool:
    """¿El nombre contiene alguno de estos tokens?

    Los tokens cortos ("id", "mes", "dia") se comparan como **palabra
    completa**, no como subcadena: si no, `unidades` contiene "id" y termina
    clasificada como llave, y `trimestre` contiene "mes" y termina como fecha.
    Los tokens largos sí van por subcadena (`codigo` dentro de `cliente_codigo`).
    """
    lower = str(name).lower()
    parts = set(re.split(r"[^a-z0-9]+", lower))
    return any(tok in parts if len(tok) <= 3 else tok in lower for tok in tokens)


def _looks_like_dates(series: pd.Series) -> bool:
    """¿Esta columna de texto/entero es realmente una fecha?"""
    sample = series.dropna().head(400)
    if sample.empty:
        return False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")       # el fallback a dateutil avisa; da igual
        try:
            parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
        except (ValueError, TypeError):
            try:
                parsed = pd.to_datetime(sample, errors="coerce")
            except Exception:
                return False
    return parsed.notna().mean() > 0.9


def _profile_column(series: pd.Series, rows: int) -> ColumnProfile:
    name = str(series.name)
    dtype = str(series.dtype)
    non_null = series.dropna()
    nulls = int(series.isna().sum())

    # Las listas (data_quality_flags) no son hashables: nunique() explota.
    try:
        uniques = int(non_null.nunique())
    except TypeError:
        uniques = -1

    prof = ColumnProfile(
        name=name, dtype=dtype, role="text", nulls=nulls,
        null_ratio=nulls / rows if rows else 0.0,
        uniques=uniques,
        unique_ratio=uniques / len(non_null) if len(non_null) and uniques >= 0 else 0.0,
        internal=_is_internal(name),
    )

    if non_null.empty:
        prof.role = "constant"
        return prof
    if uniques == 1:
        prof.role = "constant"
        prof.stats = {"value": str(non_null.iloc[0])}
        return prof
    if uniques == -1:
        prof.role = "text"
        return prof

    is_numeric = pd.api.types.is_numeric_dtype(series)
    is_datetime = pd.api.types.is_datetime64_any_dtype(series)
    is_bool = pd.api.types.is_bool_dtype(series)

    # ── fecha ────────────────────────────────────────────────────────────
    if is_datetime or (_name_has(name, _DATE_TOKENS) and not is_numeric
                       and _looks_like_dates(series)):
        parsed = pd.to_datetime(series, errors="coerce")
        prof.role = "date"
        prof.stats = {
            "min": parsed.min().strftime("%Y-%m-%d") if parsed.notna().any() else None,
            "max": parsed.max().strftime("%Y-%m-%d") if parsed.notna().any() else None,
            "coverage": float(parsed.notna().mean()),
        }
        return prof

    # ── flag ─────────────────────────────────────────────────────────────
    if is_bool or (uniques == 2 and set(map(str, non_null.unique()[:2]))
                   <= {"0", "1", "True", "False", "true", "false", "S", "N",
                       "Si", "No", "SI", "NO"}):
        prof.role = "flag"
        prof.top_values = _top_values(non_null)
        return prof

    # ── identificador ────────────────────────────────────────────────────
    # Nombre de llave con suficientes valores, o valores casi todos distintos.
    # `region_codigo` con 16 valores es una dimensión aunque se llame código:
    # lo que decide es cuántos valores tiene, no cómo se llama.
    #
    # Los decimales quedan fuera por definición: nadie usa 1.234,56 como
    # llave. Un monto continuo tiene casi todos sus valores distintos, y sin
    # esta excepción terminaba clasificado como identificador — con lo que la
    # medida principal del tablero pasaba a ser cualquier otra columna.
    is_float = is_numeric and not pd.api.types.is_integer_dtype(series)
    if not is_float and (
        prof.unique_ratio > IDENTIFIER_RATIO
        or (_name_has(name, _ID_TOKENS) and uniques > MAX_DIMENSION_UNIQUES)
    ):
        prof.role = "identifier"
        return prof

    # ── medida ───────────────────────────────────────────────────────────
    if is_numeric:
        # Un numérico cuyo nombre es temporal (periodo=202101, anio, mes) es
        # un eje de agrupación, no una magnitud: sumar los períodos no
        # significa nada.
        if _name_has(name, _DATE_TOKENS):
            prof.role = "dimension"
            prof.top_values = _top_values(non_null)
            return prof
        # Un numérico con poquísimos valores distintos es una categoría
        # codificada (escala 1-5, código de estado, mes), no una magnitud.
        # El corte va en 12 y no más arriba: `unidades` con valores 1..25 es
        # una cantidad que se suma, no una categoría.
        if uniques <= 12 and prof.unique_ratio < 0.01:
            prof.role = "dimension"
            prof.top_values = _top_values(non_null)
            return prof
        numeric = pd.to_numeric(series, errors="coerce")
        prof.role = "measure"
        prof.stats = {
            "min": float(numeric.min()), "max": float(numeric.max()),
            "mean": float(numeric.mean()), "sum": float(numeric.sum()),
            "p50": float(numeric.median()),
            "negatives": int((numeric < 0).sum()),
            "zeros": int((numeric == 0).sum()),
        }
        return prof

    # ── dimensión vs texto libre ─────────────────────────────────────────
    if uniques <= MAX_DIMENSION_UNIQUES and prof.unique_ratio <= MAX_DIMENSION_RATIO:
        prof.role = "dimension"
        prof.top_values = _top_values(non_null)
        return prof

    prof.role = "text"
    return prof


def _top_values(series: pd.Series, n: int = 10) -> List[Dict[str, Any]]:
    counts = series.value_counts().head(n)
    total = len(series)
    return [{"value": str(v), "count": int(c), "pct": round(c / total * 100, 2)}
            for v, c in counts.items()]


def profile_dataset(df: pd.DataFrame, table: str = "dataset") -> DatasetProfile:
    rows = len(df)
    columns = [_profile_column(df[c], rows) for c in df.columns]

    business = [c for c in columns if not c.internal]
    dates = [c for c in business if c.role == "date"]
    measures = [c for c in business if c.role == "measure"]
    dimensions = [c for c in business if c.role == "dimension"]
    identifiers = [c for c in business if c.role == "identifier"]

    # Eje temporal: la fecha con mejor cobertura; a igualdad, la primera.
    primary_date = None
    if dates:
        primary_date = max(dates, key=lambda c: c.stats.get("coverage", 0)).name

    # Las medidas se ordenan por magnitud total: la que mueve la aguja primero.
    measures.sort(key=lambda c: abs(c.stats.get("sum", 0)), reverse=True)
    # Las dimensiones, por cardinalidad ascendente: las de pocos valores
    # producen los gráficos más legibles.
    dimensions.sort(key=lambda c: c.uniques)

    return DatasetProfile(
        table=table, rows=rows, columns=columns, primary_date=primary_date,
        measures=[c.name for c in measures],
        dimensions=[c.name for c in dimensions],
        identifiers=[c.name for c in identifiers],
    )
