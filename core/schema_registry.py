"""Registry de schemas esperados por dataset con detección de drift.

Problema que resuelve: si una fuente upstream renombra una columna
(`monto_venta` -> `sale_amount`) o cambia un dtype (`int64` -> `float64`),
el pipeline procesa los datos sin avisar — los outputs quedan incompletos
o sesgados sin que nadie sepa.

Solución:
    - Primera vez que se procesa un dataset, congelar su schema en
      `schemas/<dataset>.json` (columnas + dtypes + fingerprint).
    - Corridas siguientes: comparar el schema entrante contra el congelado.
        * Match exacto                  -> ok
        * Nuevas columnas               -> warn (additions son seguras)
        * Columnas faltantes            -> fatal (rompe pipeline)
        * Dtype cambió (int -> str)     -> fatal
        * Dtype cambió (int -> float)   -> warn (cast compatible)

El usuario puede regenerar el schema manualmente borrando
`schemas/<dataset>.json` o usando `--refresh-schema`.
"""

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from core.logging_engine import setup_logger

logger = setup_logger("schema_registry")

DEFAULT_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

# Mapa de coerciones consideradas "compatibles" (warn, no fatal).
# Si dtype evoluciona a uno de estos, no rompemos: castear es seguro.
_COMPATIBLE_PROMOTIONS = {
    "int64": {"float64", "Float64", "Int64"},
    "Int64": {"float64", "Float64"},
    "float32": {"float64", "Float64"},
}


@dataclass
class SchemaDiff:
    new_columns: List[str] = field(default_factory=list)
    missing_columns: List[str] = field(default_factory=list)
    dtype_changes: Dict[str, Dict[str, str]] = field(default_factory=dict)
    fatal_changes: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.new_columns or self.missing_columns or self.dtype_changes)

    @property
    def is_fatal(self) -> bool:
        return bool(self.fatal_changes)


def _column_dtypes(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [{"column": str(c), "dtype": str(df[c].dtype)} for c in df.columns]


def _fingerprint(schema: List[Dict[str, str]]) -> str:
    payload = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class SchemaRegistry:
    """Lee/escribe schemas congelados a disco y compara contra los entrantes."""

    def __init__(self, schemas_dir: Path = None):
        # Resolver al call-time (no default arg fijado en signature) para que
        # monkeypatch de DEFAULT_SCHEMAS_DIR funcione en tests.
        self.dir = Path(schemas_dir if schemas_dir is not None else DEFAULT_SCHEMAS_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, dataset: str) -> Path:
        return self.dir / f"{dataset}.json"

    def has(self, dataset: str) -> bool:
        return self._path(dataset).exists()

    def load(self, dataset: str) -> Optional[Dict[str, Any]]:
        p = self._path(dataset)
        if not p.exists():
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    def freeze(self, dataset: str, df: pd.DataFrame,
                run_id: Optional[str] = None) -> Dict[str, Any]:
        """Congela el schema de un DataFrame. Si ya existía, NO sobreescribe."""
        if self.has(dataset):
            return self.load(dataset)

        schema = _column_dtypes(df)
        record = {
            "dataset": dataset,
            "schema": schema,
            "fingerprint": _fingerprint(schema),
            "captured_at": datetime.datetime.now().isoformat(),
            "run_id": run_id,
        }
        with open(self._path(dataset), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
        logger.info(f"Schema congelado para '{dataset}': "
                    f"fingerprint={record['fingerprint']}, "
                    f"path={self._path(dataset)}")
        return record

    def diff(self, dataset: str, df: pd.DataFrame) -> Optional[SchemaDiff]:
        """Compara schema entrante vs congelado. None si no hay congelado."""
        baseline = self.load(dataset)
        if baseline is None:
            return None

        baseline_map = {item["column"]: item["dtype"] for item in baseline["schema"]}
        current_map = {str(c): str(df[c].dtype) for c in df.columns}

        diff = SchemaDiff()
        for col in current_map:
            if col not in baseline_map:
                diff.new_columns.append(col)

        for col, expected_dtype in baseline_map.items():
            if col not in current_map:
                diff.missing_columns.append(col)
                diff.fatal_changes.append(f"missing required column '{col}'")
                continue
            actual_dtype = current_map[col]
            if actual_dtype != expected_dtype:
                diff.dtype_changes[col] = {
                    "expected": expected_dtype,
                    "actual": actual_dtype,
                }
                compatible = _COMPATIBLE_PROMOTIONS.get(expected_dtype, set())
                if actual_dtype not in compatible:
                    diff.fatal_changes.append(
                        f"column '{col}' dtype changed {expected_dtype} -> {actual_dtype}"
                    )

        return diff

    def evaluate(self, dataset: str, df: pd.DataFrame,
                 run_id: Optional[str] = None) -> Dict[str, Any]:
        """Lógica completa: si no hay schema, congelarlo; si hay, comparar.

        Retorna dict con:
            - first_time: bool (si se congeló ahora)
            - fingerprint: el del schema baseline (o el congelado)
            - diff: dict con cambios, si los hay
            - is_fatal: bool
        """
        if not self.has(dataset):
            record = self.freeze(dataset, df, run_id=run_id)
            return {
                "first_time": True,
                "fingerprint": record["fingerprint"],
                "diff": None,
                "is_fatal": False,
            }

        baseline = self.load(dataset)
        diff = self.diff(dataset, df)
        result = {
            "first_time": False,
            "fingerprint": baseline["fingerprint"],
            "diff": None,
            "is_fatal": False,
        }
        if diff and diff.has_changes:
            result["diff"] = {
                "new_columns": diff.new_columns,
                "missing_columns": diff.missing_columns,
                "dtype_changes": diff.dtype_changes,
                "fatal_changes": diff.fatal_changes,
            }
            result["is_fatal"] = diff.is_fatal
            level = logger.error if diff.is_fatal else logger.warning
            level(f"[schema drift] {dataset}: {result['diff']}")
        return result
