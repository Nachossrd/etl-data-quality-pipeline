"""
Generador de reporte de calidad con métricas accionables por columna.

Diseñado para alimentar decisiones empresariales: cualquier número que aparezca
en un dashboard debe poder rastrearse a "X filas válidas de N totales, con Y%
nulls en columna Z, schema Z' vs esperado Z''".

Compatibilidad: mantiene `add_table_metrics(table, extracted, cleaned, quarantined)`
y `finalize()` para no romper `auto_pipeline.py`.

Capacidades nuevas:
    - register_chunk(table, df): agrega métricas por columna a medida que
      los chunks pasan por el pipeline. Acumula sin guardar el df en memoria.
    - register_violations(table, violations): captura violaciones de reglas
      declarativas (ver core.rule_validator).
    - schema_fingerprint: hash determinista de (columna, dtype) por tabla.
      Cualquier drift contra runs previos es detectable comparando este hash.
    - flag_breakdown: cuenta cada tag de `data_quality_flag` por separado
      (un row con 'id_fixed|invalid_date' suma 1 a ambos).
    - quality_score_distribution: min/p25/p50/p75/p95/max + mean por tabla.
"""

import datetime
import hashlib
import json
import os
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

import pandas as pd

from config.settings import PipelineConfig

# Límite para tracking exacto de uniques por columna. Por encima de esto la
# columna se marca high_cardinality=True y no se sigue acumulando un set.
# Justificación: con 50k uniques el cost de memoria sigue siendo bajo (<5 MB)
# pero filtra columnas con UUIDs/timestamps donde el unique_count exacto no
# aporta valor de decisión.
UNIQUE_TRACKING_CAP = 50_000


class _TableAccumulator:
    """Acumula métricas de calidad de una tabla a través de múltiples chunks.

    Diseñado para chunked pipelines: nunca mantiene el DataFrame completo en
    memoria, solo agregaciones incrementales.
    """

    __slots__ = (
        "table_name", "extracted", "cleaned", "quarantined",
        "null_counts", "non_null_counts", "dtypes",
        "uniques", "high_cardinality_cols",
        "flag_counter", "quality_scores", "schema_first_chunk",
        "violations_by_rule",
    )

    def __init__(self, table_name: str):
        self.table_name = table_name
        self.extracted = 0
        self.cleaned = 0
        self.quarantined = 0
        self.null_counts: Dict[str, int] = defaultdict(int)
        self.non_null_counts: Dict[str, int] = defaultdict(int)
        self.dtypes: Dict[str, str] = {}
        self.uniques: Dict[str, set] = defaultdict(set)
        self.high_cardinality_cols: set = set()
        self.flag_counter: Counter = Counter()
        self.quality_scores: List[float] = []
        self.schema_first_chunk: Optional[List[tuple]] = None
        self.violations_by_rule: Counter = Counter()

    def ingest_chunk(self, df: pd.DataFrame) -> None:
        if df.empty:
            return

        if self.schema_first_chunk is None:
            self.schema_first_chunk = [(c, str(df[c].dtype)) for c in df.columns]

        for col in df.columns:
            s = df[col]
            self.null_counts[col] += int(s.isna().sum())
            self.non_null_counts[col] += int(s.notna().sum())
            # Solo se guarda el dtype más reciente; útil para detectar coerciones
            # en el último chunk vs el primero (schema_first_chunk).
            self.dtypes[col] = str(s.dtype)

            if col not in self.high_cardinality_cols:
                try:
                    new_uniques = set(s.dropna().unique())
                    self.uniques[col].update(new_uniques)
                    if len(self.uniques[col]) > UNIQUE_TRACKING_CAP:
                        self.high_cardinality_cols.add(col)
                        del self.uniques[col]
                except TypeError:
                    # Tipos unhashable (listas, dicts) → marcar y dejar de trackear
                    self.high_cardinality_cols.add(col)
                    self.uniques.pop(col, None)

        if "data_quality_flags" in df.columns:
            # Estructura nativa list[str]: iterar directo, sin parsing de pipes.
            for flags in df["data_quality_flags"]:
                if isinstance(flags, list):
                    for tag in flags:
                        if tag:
                            self.flag_counter[str(tag)] += 1
                elif isinstance(flags, str) and flags:
                    # Backwards-compat: si por algún motivo viene string legacy
                    for tag in flags.split("|"):
                        tag = tag.strip()
                        if tag:
                            self.flag_counter[tag] += 1

        if "quality_score" in df.columns:
            self.quality_scores.extend(
                df["quality_score"].dropna().astype(float).tolist()
            )

    def add_violations(self, violations: Dict[str, int]) -> None:
        for rule_name, count in violations.items():
            self.violations_by_rule[rule_name] += int(count)

    def schema_fingerprint(self) -> str:
        if not self.schema_first_chunk:
            return ""
        payload = json.dumps(self.schema_first_chunk, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def serialize(self) -> Dict[str, Any]:
        total_rows = self.cleaned or sum(self.non_null_counts.values()) // max(
            len(self.non_null_counts), 1
        )

        per_column = {}
        for col, dtype in self.dtypes.items():
            nulls = self.null_counts[col]
            non_nulls = self.non_null_counts[col]
            total = nulls + non_nulls
            is_high_card = col in self.high_cardinality_cols
            unique_count = (
                f">{UNIQUE_TRACKING_CAP}" if is_high_card else len(self.uniques.get(col, set()))
            )
            per_column[col] = {
                "dtype": dtype,
                "null_count": nulls,
                "null_ratio": round(nulls / total, 6) if total else 0.0,
                "unique_count": unique_count,
                "high_cardinality": is_high_card,
            }

        qs = self.quality_scores
        qs_dist = None
        if qs:
            s = pd.Series(qs)
            qs_dist = {
                "min": float(s.min()),
                "p25": float(s.quantile(0.25)),
                "p50": float(s.median()),
                "p75": float(s.quantile(0.75)),
                "p95": float(s.quantile(0.95)),
                "max": float(s.max()),
                "mean": float(s.mean()),
                "below_50_ratio": float((s < 50).mean()),
            }

        return {
            "table": self.table_name,
            "row_counts": {
                "extracted": self.extracted,
                "cleaned": self.cleaned,
                "quarantined": self.quarantined,
                "quarantine_ratio": (
                    round(self.quarantined / self.extracted, 6)
                    if self.extracted else 0.0
                ),
            },
            "schema_fingerprint": self.schema_fingerprint(),
            "per_column": per_column,
            "flag_breakdown": dict(self.flag_counter.most_common()),
            "quality_score_distribution": qs_dist,
            "rule_violations": dict(self.violations_by_rule.most_common()),
        }


class QualityReportGenerator:
    """API pública. Compatible con auto_pipeline.py."""

    def __init__(self) -> None:
        self.metrics: Dict[str, Any] = {
            "start_time": datetime.datetime.now().isoformat(),
            "tables": {},
        }
        self._accumulators: Dict[str, _TableAccumulator] = {}

    # ─── API legacy (mantenida) ────────────────────────────────────────────
    def add_table_metrics(self, table_name: str, extracted: int,
                          cleaned: int, quarantined: int) -> None:
        acc = self._get(table_name)
        acc.extracted = extracted
        acc.cleaned = cleaned
        acc.quarantined = quarantined

    # ─── API nueva ─────────────────────────────────────────────────────────
    def register_chunk(self, table_name: str, df: pd.DataFrame) -> None:
        """Llamar por cada chunk procesado tras limpieza/cuarentena."""
        self._get(table_name).ingest_chunk(df)

    def register_violations(self, table_name: str,
                             violations: Dict[str, int]) -> None:
        """Llamar tras evaluar las reglas declarativas (rule_validator)."""
        self._get(table_name).add_violations(violations)

    def _get(self, table_name: str) -> _TableAccumulator:
        if table_name not in self._accumulators:
            self._accumulators[table_name] = _TableAccumulator(table_name)
        return self._accumulators[table_name]

    # ─── Finalización ──────────────────────────────────────────────────────
    def finalize(self) -> Dict[str, Any]:
        self.metrics["end_time"] = datetime.datetime.now().isoformat()

        totals = {"extracted": 0, "cleaned": 0, "quarantined": 0}
        for table_name, acc in self._accumulators.items():
            self.metrics["tables"][table_name] = acc.serialize()
            totals["extracted"] += acc.extracted
            totals["cleaned"] += acc.cleaned
            totals["quarantined"] += acc.quarantined
        totals["quarantine_ratio"] = (
            round(totals["quarantined"] / totals["extracted"], 6)
            if totals["extracted"] else 0.0
        )
        self.metrics["totals"] = totals

        os.makedirs(PipelineConfig.OUTPUT_DIR, exist_ok=True)
        json_path = os.path.join(PipelineConfig.OUTPUT_DIR, "quality_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(self.metrics, f, indent=2, ensure_ascii=False, default=str)

        html_path = os.path.join(PipelineConfig.OUTPUT_DIR, "quality_report.html")
        self._generate_html(html_path)
        return self.metrics

    # ─── Renderizado HTML legible ──────────────────────────────────────────
    def _generate_html(self, path: str) -> None:
        totals = self.metrics["totals"]
        tables_html = []
        for table_name, data in self.metrics["tables"].items():
            rc = data["row_counts"]
            qrat = rc["quarantine_ratio"]
            qcolor = "#10b981" if qrat < 0.05 else ("#f59e0b" if qrat < 0.20 else "#ef4444")

            cols_html = "".join(
                f"<tr><td>{c}</td><td>{m['dtype']}</td><td>{m['null_count']:,}</td>"
                f"<td>{m['null_ratio']*100:.2f}%</td><td>{m['unique_count']}</td></tr>"
                for c, m in data["per_column"].items()
            )

            flag_html = "".join(
                f"<tr><td>{flag}</td><td>{count:,}</td></tr>"
                for flag, count in data["flag_breakdown"].items()
            ) or "<tr><td colspan='2'><em>Sin flags</em></td></tr>"

            viol_html = "".join(
                f"<tr><td>{rule}</td><td>{count:,}</td></tr>"
                for rule, count in data["rule_violations"].items()
            ) or "<tr><td colspan='2'><em>Sin violaciones de reglas</em></td></tr>"

            qs = data["quality_score_distribution"]
            qs_html = (
                f"<p>min={qs['min']:.1f} | p25={qs['p25']:.1f} | "
                f"p50={qs['p50']:.1f} | p75={qs['p75']:.1f} | "
                f"p95={qs['p95']:.1f} | max={qs['max']:.1f} | "
                f"mean={qs['mean']:.1f} | %&lt;50={qs['below_50_ratio']*100:.2f}%</p>"
            ) if qs else "<p><em>Sin quality_score capturado.</em></p>"

            tables_html.append(f"""
            <section>
              <h2>{table_name} <small style="color:{qcolor}">({qrat*100:.2f}% cuarentena)</small></h2>
              <p>extracted={rc['extracted']:,} | cleaned={rc['cleaned']:,} | quarantined={rc['quarantined']:,}</p>
              <p><strong>schema_fingerprint</strong>: <code>{data['schema_fingerprint']}</code></p>
              <h3>Distribución de quality_score</h3>
              {qs_html}
              <h3>Por columna</h3>
              <table><tr><th>Columna</th><th>dtype</th><th>nulls</th><th>null %</th><th>uniques</th></tr>{cols_html}</table>
              <h3>Flag breakdown</h3>
              <table><tr><th>Flag</th><th>Filas</th></tr>{flag_html}</table>
              <h3>Violaciones de reglas declarativas</h3>
              <table><tr><th>Regla</th><th>Filas</th></tr>{viol_html}</table>
            </section>
            """)

        html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><title>Quality Report</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#0f172a;}}
h1{{color:#0284c7;}}h2{{border-bottom:2px solid #e2e8f0;padding-bottom:4px;margin-top:32px;}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px;}}
th,td{{border:1px solid #cbd5e1;padding:6px 10px;text-align:left;}}
th{{background:#f1f5f9;}}code{{background:#f1f5f9;padding:2px 6px;border-radius:3px;}}
section{{margin-bottom:32px;}}
</style></head><body>
<h1>ETL Quality Report</h1>
<p><strong>Run:</strong> {self.metrics.get('start_time','?')} → {self.metrics.get('end_time','?')}</p>
<p><strong>Totales:</strong> extracted={totals['extracted']:,} | cleaned={totals['cleaned']:,} | quarantined={totals['quarantined']:,} ({totals['quarantine_ratio']*100:.2f}%)</p>
{''.join(tables_html)}
</body></html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
