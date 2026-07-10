"""
Validador declarativo de reglas de calidad por dataset.

Las reglas viven en YAML (`rules/datasets/<dataset>.yaml`) — separadas del
código para que un analista pueda ajustar umbrales sin saber Python.

Tipos de regla soportados:
    - required_columns:    el dataset DEBE tener estas columnas
    - dtype:               casteable al tipo declarado (int/float/string/date)
    - range:               valor numérico entre min y max
    - allowed_values:      valor presente en lista cerrada
    - regex:               valor matchea regex
    - not_null:            columna no admite NULLs
    - unique:              columna debe ser PK candidata

Acciones (`on_violation`):
    - warn        registra la violación pero deja pasar la fila
    - quarantine  saca la fila del dataset principal (va a quarantine_engine)
    - fail        si alguna fila viola, abortar el pipeline completo

Resultado de `validate()`:
    ValidationOutcome(
        valid_df,            # filas que pasaron (sin quarantine)
        quarantine_df,       # filas que cayeron por reglas con on_violation=quarantine
        violations_count,    # {rule_name: cantidad_de_filas_afectadas}
        fatal_violations,    # lista de errores que deben abortar el pipeline
    )

Diseñado para integrarse al pipeline chunked: validate() opera sobre un chunk
a la vez. No agrega estado entre chunks (eso lo hace QualityReportGenerator).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yaml

from core.logging_engine import setup_logger

logger = setup_logger("rule_validator")

DEFAULT_RULES_DIR = Path(__file__).resolve().parent.parent / "rules" / "datasets"


@dataclass
class ValidationOutcome:
    valid_df: pd.DataFrame
    quarantine_df: pd.DataFrame
    violations_count: Dict[str, int] = field(default_factory=dict)
    fatal_violations: List[str] = field(default_factory=list)

    @property
    def is_fatal(self) -> bool:
        return bool(self.fatal_violations)


class RuleViolationError(RuntimeError):
    """Reglas con on_violation=fail dispararon esta excepción."""


class RuleValidator:
    """Carga reglas de un archivo YAML y las aplica a DataFrames.

    Ejemplo de uso:
        validator = RuleValidator.from_yaml("rules/datasets/car_prices.yaml")
        outcome = validator.validate(df)
        if outcome.is_fatal:
            raise RuleViolationError(...)
    """

    def __init__(self, spec: Dict[str, Any], source: Optional[str] = None):
        self.spec = spec
        self.source = source or "<inline>"
        self.dataset_name: str = spec.get("dataset", "unknown")
        self.required_columns: List[str] = list(spec.get("required_columns", []))
        self.rules: List[Dict[str, Any]] = list(spec.get("rules", []))

    # ─── Constructores ────────────────────────────────────────────────────
    @classmethod
    def from_yaml(cls, path: str | Path) -> "RuleValidator":
        p = Path(path)
        with open(p, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
        return cls(spec, source=str(p))

    @classmethod
    def for_dataset(cls, dataset: str,
                     rules_dir: Path = DEFAULT_RULES_DIR) -> Optional["RuleValidator"]:
        """Busca `rules/datasets/<dataset>.yaml`. Devuelve None si no existe."""
        candidate = Path(rules_dir) / f"{dataset}.yaml"
        if not candidate.exists():
            return None
        return cls.from_yaml(candidate)

    # ─── Aplicación ───────────────────────────────────────────────────────
    def validate(self, df: pd.DataFrame) -> ValidationOutcome:
        """Aplica todas las reglas al DataFrame. No muta el df de entrada."""
        if df.empty:
            return ValidationOutcome(valid_df=df.copy(), quarantine_df=df.copy().iloc[0:0])

        outcome = ValidationOutcome(valid_df=df.copy(),
                                     quarantine_df=df.iloc[0:0].copy())

        # 1. Required columns (fatal si faltan)
        missing = [c for c in self.required_columns if c not in df.columns]
        if missing:
            msg = (f"[{self.dataset_name}] columnas requeridas ausentes: {missing}")
            logger.error(msg)
            outcome.fatal_violations.append(msg)
            return outcome

        # 2. Cada regla itera y marca filas
        quarantine_mask = pd.Series(False, index=df.index)
        for rule in self.rules:
            rule_name, mask = self._evaluate_rule(rule, df)
            if mask is None:
                continue
            n_violations = int(mask.sum())
            if n_violations == 0:
                continue
            outcome.violations_count[rule_name] = n_violations
            action = rule.get("on_violation", "warn")
            if action == "fail":
                msg = (f"[{self.dataset_name}] regla fatal '{rule_name}': "
                       f"{n_violations} fila(s) violan")
                logger.error(msg)
                outcome.fatal_violations.append(msg)
            elif action == "quarantine":
                quarantine_mask = quarantine_mask | mask
                logger.warning(f"[{self.dataset_name}] '{rule_name}': "
                                f"{n_violations} filas -> cuarentena")
            else:  # warn
                logger.warning(f"[{self.dataset_name}] '{rule_name}': "
                                f"{n_violations} filas (warn)")

        if quarantine_mask.any():
            outcome.quarantine_df = df.loc[quarantine_mask].copy()
            outcome.valid_df = df.loc[~quarantine_mask].copy()

        return outcome

    # ─── Evaluadores por tipo de regla ────────────────────────────────────
    def _evaluate_rule(self, rule: Dict[str, Any],
                        df: pd.DataFrame) -> Tuple[str, Optional[pd.Series]]:
        col = rule.get("column")
        rule_type = rule.get("type", "").lower()
        rule_name = rule.get("name") or f"{col}:{rule_type}"

        if col and col not in df.columns:
            return rule_name, None  # columna ausente; ya manejado por required_columns

        series = df[col] if col else None

        if rule_type == "range":
            numeric = pd.to_numeric(series, errors="coerce")
            mask = numeric.notna()
            if "min" in rule:
                mask &= numeric >= rule["min"]
            if "max" in rule:
                mask &= numeric <= rule["max"]
            return rule_name, ~mask & series.notna()

        if rule_type == "allowed_values":
            allowed = set(rule.get("values", []))
            mask = series.isin(allowed) | series.isna()
            return rule_name, ~mask

        if rule_type == "regex":
            pattern = rule.get("pattern", "")
            compiled = re.compile(pattern)
            mask = series.astype(str).str.match(compiled)
            return rule_name, ~mask.fillna(False) & series.notna()

        if rule_type == "not_null":
            return rule_name, series.isna()

        if rule_type == "unique":
            dup_mask = series.duplicated(keep=False) & series.notna()
            return rule_name, dup_mask

        if rule_type == "dtype":
            expected = str(rule.get("expected", "")).lower()
            if expected in ("int", "integer"):
                coerced = pd.to_numeric(series, errors="coerce")
                mask = coerced.isna() & series.notna()
                mask |= (coerced.fillna(0) % 1 != 0) & coerced.notna()
                return rule_name, mask
            if expected in ("float", "number"):
                coerced = pd.to_numeric(series, errors="coerce")
                return rule_name, coerced.isna() & series.notna()
            if expected == "date":
                coerced = pd.to_datetime(series, errors="coerce")
                return rule_name, coerced.isna() & series.notna()

        logger.warning(f"Tipo de regla desconocido: {rule_type} (regla {rule_name})")
        return rule_name, None
