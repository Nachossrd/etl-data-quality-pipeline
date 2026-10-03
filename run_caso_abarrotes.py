#!/usr/bin/env python
"""Caso Abarrotes CL de punta a punta, en un comando.

    python run_caso_abarrotes.py

Encadena las tres etapas y deja todo listo para presentar:

    1. Pipeline ETL      archivo crudo -> dataset limpio + auditoría de calidad
    2. Capa analítica    dataset limpio -> insights.json (KPIs + proyección)
    3. Presentación      insights.json -> dashboard HTML autocontenido

El dashboard se escribe donde lo pida `--dashboard` (por defecto, la carpeta
"El cubo" del usuario) y además queda una copia junto al resto de los
artefactos del run, para que la corrida sea autocontenida.

Flags útiles:
    --skip-pipeline   reusa el último run (útil para iterar el dashboard sin
                      reprocesar 315k filas)
    --input / --output / --dashboard  rutas alternativas
"""

import argparse
import os
import shutil
import sys
import time

DEFAULT_INPUT = "Ventas_2019-al-2022.xlsx"
DEFAULT_OUTPUT = "./output"
DEFAULT_DASHBOARD = r"C:\Users\marku\Downloads\El cubo\abarrotes-cl\index.html"


def parse_args():
    p = argparse.ArgumentParser(description="Caso Abarrotes CL end-to-end")
    p.add_argument("--input", default=DEFAULT_INPUT)
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    p.add_argument("--dashboard", default=DEFAULT_DASHBOARD)
    p.add_argument("--skip-pipeline", action="store_true",
                   help="No reprocesa: usa el último run existente")
    p.add_argument("--quarantine-threshold", type=float, default=None)
    return p.parse_args()


def _step(n: int, title: str) -> None:
    print(f"\n{'='*66}\n  {n}. {title}\n{'='*66}", flush=True)


def main() -> int:
    args = parse_args()
    started = time.time()

    from analytics import abarrotes_bi, dashboard

    # ── 1. Pipeline ───────────────────────────────────────────────────────
    if args.skip_pipeline:
        run_dir = abarrotes_bi.resolve_run_dir(args.output)
        _step(1, f"Pipeline omitido — reusando {run_dir}")
    else:
        _step(1, f"Pipeline ETL sobre {args.input}")
        if not os.path.exists(args.input):
            print(f"[ERROR] No existe el archivo de entrada: {args.input}")
            return 1
        from auto_pipeline import run_pipeline
        result = run_pipeline(args.input, args.output,
                              quarantine_threshold=args.quarantine_threshold)
        run_dir = result["run_dir"]
        if result["quarantine_failed"]:
            # El dato no es confiable: se corta antes de publicar conclusiones.
            print(f"[ERROR] Cuarentena {result['q_ratio']*100:.2f}% sobre el umbral. "
                  f"No se genera el análisis: revisar {run_dir}/quarantine/")
            return 2

    # ── 2. Analítica ──────────────────────────────────────────────────────
    _step(2, "Capa analítica — KPIs, estacionalidad y proyección")
    df = abarrotes_bi.load_clean_dataset(run_dir)
    insights = abarrotes_bi.build_insights(df, run_dir=run_dir, source_file=args.input)
    insights_path = os.path.join(run_dir, "insights.json")
    import json
    with open(insights_path, "w", encoding="utf-8") as f:
        json.dump(insights, f, indent=2, ensure_ascii=False, default=str)

    k, p = insights["kpis"], insights["proyeccion"]
    print(f"  Venta 2019-2022 ....... ${k['venta_total']:,.0f}")
    print(f"  Unidades .............. {k['unidades_total']:,}")
    print(f"  Margen ................ {k['margen_pct']:.2f}%")
    print(f"  Modelo elegido ........ {p['metodo']}")
    print(f"  Error validado (MAPE) . {p['mape_backtest_unidades']}%")
    print(f"  Proyección {p['anio_proyectado']} ........ {p['unidades_total']:,} unidades "
          f"(${p['venta_total']:,.0f})")

    # ── 3. Dashboard ──────────────────────────────────────────────────────
    _step(3, "Dashboard HTML autocontenido")
    dashboard.build(insights_path, args.dashboard)
    local_copy = os.path.join(run_dir, "dashboard.html")
    shutil.copyfile(args.dashboard, local_copy)
    print(f"  Dashboard ......... {args.dashboard}")
    print(f"  Copia del run ..... {local_copy}")
    print(f"  Insights .......... {insights_path}")
    print(f"\nListo en {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
