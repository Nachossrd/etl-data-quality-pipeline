#!/usr/bin/env python
import importlib
import subprocess
import sys
import os

REQUIRED_PACKAGES = {
    "pandas": "pandas",
    "numpy": "numpy",
    "sqlalchemy": "sqlalchemy",
    "pyodbc": "pyodbc",
    "openpyxl": "openpyxl",
    "psutil": "psutil",
    "pyarrow": "pyarrow"
}

def _check_and_install_dependencies():
    """Bootstrap: checks and installs missing dependencies."""
    missing = []
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            missing.append(pip_name)

    if missing:
        print("\n[BOOTSTRAP] Missing dependencies detected:")
        for pkg in missing:
            print(f"  - {pkg}")
        print("\n[BOOTSTRAP] Installing automatically...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
            print("[BOOTSTRAP] [OK] Dependencies installed successfully\n")
        except subprocess.CalledProcessError as e:
            print(f"[BOOTSTRAP] [ERROR] Failed to install packages: {e}")
            sys.exit(1)

_check_and_install_dependencies()

import argparse
import time
from core.docker_manager import EphemeralSQLServer
from core.sql_restore import BackupRestorer
from core.extraction import ExtractionEngine
from core.file_extraction import FileExtractor
from core.ingest import UnsupportedFormatError, discover_tables
from core.cleaning_engine import CleaningEngine
from core.quarantine_engine import QuarantineEngine
from core.export_engine import ExportEngine
from core.quality_report import QualityReportGenerator
from core.logging_engine import setup_logger
from core.manifest import RunManifest
from core.rule_validator import RuleValidator, RuleViolationError
from core.schema_registry import SchemaRegistry
from core.alerts import get_notifier
from config.settings import PipelineConfig
from semantic.semantic_pipeline import SemanticEnricher
from core.presentation_layer import ReportGenerator
import pandas as pd

logger = setup_logger("auto_pipeline")

def parse_args():
    parser = argparse.ArgumentParser(description="Zero-Click Enterprise Data Pipeline")
    parser.add_argument("--input", required=False, help="Path to the input file (.bak, .csv, .xlsx)")
    parser.add_argument("--output", default="./output", help="Output directory (parent — actual run lives in <output>/<run_id>/)")
    parser.add_argument("--quarantine-threshold", type=float, default=None,
                        help="Fracción máxima de filas en cuarentena antes de fallar (default 0.05 = 5%%)")
    parser.add_argument("--ui", action="store_true", help="Launch the web UI interface")
    return parser.parse_args()

def process_table_chunks(chunk_iterator, table_name: str,
                          report_gen: QualityReportGenerator,
                          manifest: "RunManifest" = None):
    extracted_count = 0
    cleaned_count = 0
    quarantined_count = 0

    # Acumulador para la capa de presentación final
    full_df_list = []
    global_classifications = {}

    # Cargar reglas declarativas si existen para este dataset. El nombre del
    # archivo manda; si no hay YAML con ese nombre se usa el que declare el
    # cleaner (se resuelve más abajo, con el primer chunk en mano).
    validator = RuleValidator.for_dataset(table_name)
    if validator:
        logger.info(f"Reglas declarativas cargadas para '{table_name}': "
                    f"{len(validator.rules)} reglas desde {validator.source}")
    rules_resolved = validator is not None
    semantic_options = {}

    # Schema registry: detectar drift contra corridas anteriores
    schema_reg = SchemaRegistry()
    schema_checked = False

    for chunk in chunk_iterator:
        extracted_count += len(chunk)

        # Clean — el cleaner se elige por auto-detección, con hint del table_name.
        # Se resuelve la clase primero porque el cleaner declara cómo debe
        # tratarlo el resto del pipeline (fases semánticas, reglas aplicables).
        cleaner_cls = CleaningEngine.select_cleaner(chunk, hint=table_name)
        if cleaner_cls is not None and not semantic_options:
            semantic_options = dict(getattr(cleaner_cls, "semantic_options", {}) or {})
            if semantic_options:
                logger.info(f"Cleaner '{cleaner_cls.name}' ajusta el enriquecimiento "
                            f"semántico: {semantic_options}")
        if cleaner_cls is not None and not rules_resolved:
            rules_resolved = True
            declared = getattr(cleaner_cls, "rules_dataset", None)
            if declared:
                validator = RuleValidator.for_dataset(declared)
                if validator:
                    logger.info(f"Reglas declarativas cargadas vía cleaner "
                                f"'{cleaner_cls.name}': {len(validator.rules)} "
                                f"reglas desde {validator.source}")

        clean_chunk = CleaningEngine.clean_chunk(
            chunk,
            cleaner_name=cleaner_cls.name if cleaner_cls else None,
            hint=table_name,
        )

        # Schema drift detection (solo sobre el primer chunk no vacío)
        if not schema_checked and not clean_chunk.empty:
            schema_result = schema_reg.evaluate(
                table_name, clean_chunk,
                run_id=manifest.run_id if manifest else None,
            )
            if schema_result["is_fatal"]:
                msg = f"Schema drift fatal en '{table_name}': {schema_result['diff']}"
                logger.error(msg)
                if manifest:
                    manifest.register_error(msg)
                raise RuleViolationError(msg)
            if schema_result["first_time"]:
                logger.info(f"Schema baseline congelado para '{table_name}' "
                            f"(fingerprint={schema_result['fingerprint']})")
            elif schema_result["diff"]:
                logger.warning(f"Schema drift (no-fatal) en '{table_name}': "
                                f"{schema_result['diff']}")
            schema_checked = True

        # Quarantine (basado en quality_score y flags)
        clean_chunk = QuarantineEngine.process(clean_chunk, table_name)

        # Semantic Enrichment
        clean_chunk, metrics = SemanticEnricher(**semantic_options).enrich(clean_chunk)
        if metrics and 'classifications' in metrics:
            global_classifications.update(metrics['classifications'])

        # Validación declarativa post-limpieza
        if validator and not clean_chunk.empty:
            outcome = validator.validate(clean_chunk)
            if outcome.is_fatal:
                msg = "; ".join(outcome.fatal_violations)
                logger.error(f"VIOLACIÓN FATAL en {table_name}: {msg}")
                raise RuleViolationError(msg)
            if not outcome.quarantine_df.empty:
                # Reusa la infra de cuarentena existente: dump al CSV de quarantine
                import os
                os.makedirs(PipelineConfig.QUARANTINE_DIR, exist_ok=True)
                q_path = os.path.join(PipelineConfig.QUARANTINE_DIR,
                                       f"{table_name}_rule_violations.csv")
                mode = 'a' if os.path.exists(q_path) else 'w'
                header = not os.path.exists(q_path)
                outcome.quarantine_df.to_csv(q_path, mode=mode, header=header, index=False)
                logger.warning(f"Reglas declarativas: {len(outcome.quarantine_df)} "
                                f"filas → {q_path}")
            clean_chunk = outcome.valid_df
            if outcome.violations_count:
                report_gen.register_violations(table_name, outcome.violations_count)

        quarantined_count += (len(chunk) - len(clean_chunk))
        cleaned_count += len(clean_chunk)

        # Registrar métricas por columna en el quality report
        report_gen.register_chunk(table_name, clean_chunk)

        # Original Export (Forensic Record / Parquet Base)
        is_first_chunk = (extracted_count == len(chunk))
        ExportEngine.export_chunk(clean_chunk, table_name,
                                    mode='a' if not is_first_chunk else 'w',
                                    header=is_first_chunk)

        full_df_list.append(clean_chunk)

    # Reconstruir el dataframe final para aplicar la presentación multi-pestaña de forma segura
    if full_df_list:
        final_df = pd.concat(full_df_list, ignore_index=True)
        # Capa de Presentación (Generar reportes ejecutivos y forenses)
        ReportGenerator.generate_reports(final_df, table_name, global_classifications)

    report_gen.add_table_metrics(table_name, extracted_count, cleaned_count, quarantined_count)
    ExportEngine.convert_csv_to_parquet(table_name)

def run_pipeline(input_path: str, output_dir: str,
                  quarantine_threshold: float = None):
    # Versionado por run_id: nunca sobreescribimos corridas anteriores.
    # output_dir es ahora el padre; los artefactos viven en output_dir/<run_id>/
    manifest = RunManifest()
    manifest.register_input(input_path)

    run_dir = os.path.join(output_dir, manifest.run_id)
    os.makedirs(run_dir, exist_ok=True)
    PipelineConfig.OUTPUT_DIR = run_dir
    PipelineConfig.QUARANTINE_DIR = os.path.join(run_dir, "quarantine")

    # Marker "latest" para que dashboards/scripts encuentren la corrida vigente
    # sin tener que ordenar timestamps. Usamos archivo (no symlink) por Windows.
    latest_marker = os.path.join(output_dir, "latest.txt")
    try:
        with open(latest_marker, "w", encoding="utf-8") as f:
            f.write(manifest.run_id)
    except OSError as e:
        logger.warning(f"No se pudo escribir latest.txt: {e}")

    # Threshold para fallar el pipeline si la tasa de cuarentena es excesiva
    if quarantine_threshold is None:
        quarantine_threshold = getattr(
            PipelineConfig, "QUARANTINE_FAIL_THRESHOLD", 0.05
        )

    report_gen = QualityReportGenerator()
    start_time = time.time()

    logger.info("="*60)
    logger.info(f"ENTERPRISE DATA PIPELINE INITIATED — run_id={manifest.run_id}")
    logger.info(f"Output dir: {run_dir}")
    logger.info(f"Quarantine fail threshold: {quarantine_threshold*100:.1f}%")
    logger.info("="*60)
    
    _, ext = os.path.splitext(input_path.lower())
    if os.path.isdir(input_path):
        ext = ""

    if ext == ".bak":
        logger.info(f"Detected SQL Server Backup file. Spinning up ephemeral Docker container...")
        sql_server = EphemeralSQLServer(input_path)
        try:
            conn_info = sql_server.start_sql_container()
            if not conn_info:
                logger.error("Failed to start SQL Server container. Exiting.")
                sys.exit(1)
                
            restorer = BackupRestorer(sql_server, input_path, conn_info['host'], conn_info['port'])
            if not restorer.restore():
                logger.error("Failed to restore backup. Exiting.")
                sys.exit(1)
                
            extractor = ExtractionEngine(sql_server)
            tables = extractor.detect_tables()
            
            for table_info in tables:
                schema = table_info["schema"]
                table_name = table_info["table"]
                logger.info(f"Processing SQL table: {table_name}")
                chunk_iterator = extractor.extract_table_chunked(schema, table_name)
                process_table_chunks(chunk_iterator, table_name, report_gen, manifest)
        finally:
            sql_server.destroy_container()
                
    else:
        # Ingesta genérica: un archivo puede contener varias tablas (hojas de
        # Excel, tablas SQLite, archivos dentro de un ZIP o de una carpeta).
        # Cada una se procesa por separado, con su propio cleaner, su reporte
        # de calidad y su tabla de salida.
        try:
            sources = discover_tables(input_path)
        except UnsupportedFormatError as e:
            logger.error(str(e))
            sys.exit(1)

        logger.info(f"{len(sources)} tabla(s) detectada(s): "
                    f"{', '.join(s.name for s in sources)}")
        for source in sources:
            logger.info(f"Procesando tabla '{source.name}' "
                        f"({source.kind} · {os.path.basename(source.origin)})")
            try:
                process_table_chunks(source.chunks(), source.name,
                                     report_gen, manifest)
            except RuleViolationError:
                raise
            except Exception as e:
                # Una tabla rota no puede tumbar las otras N-1 del mismo run,
                # pero queda registrada como error del run, no ignorada.
                msg = f"Tabla '{source.name}' falló: {e}"
                logger.error(msg)
                manifest.register_error(msg)

    quality_metrics = report_gen.finalize()
    manifest.attach_quality_summary(
        totals=quality_metrics.get("totals", {}),
        tables_summary=quality_metrics.get("tables", {}),
    )

    # Gate de cuarentena: si demasiadas filas fallaron limpieza/reglas, el
    # output completo es sospechoso. Mejor fallar que entregar datos sesgados.
    totals = quality_metrics.get("totals", {})
    q_ratio = totals.get("quarantine_ratio", 0.0)
    quarantine_failed = q_ratio > quarantine_threshold
    notifier = get_notifier()
    if quarantine_failed:
        msg = (f"Quarantine threshold excedido: {q_ratio*100:.2f}% > "
               f"{quarantine_threshold*100:.1f}% (filas cuarentenadas / total). "
               f"El output NO es confiable para decisiones empresariales.")
        logger.error(msg)
        manifest.register_error(msg)
        # Alerta out-of-band a Slack/Discord/Teams si está configurado
        dataset_name = next(iter(quality_metrics.get("tables", {})), "unknown")
        notifier.quarantine_breach(
            run_id=manifest.run_id, dataset=dataset_name,
            ratio=q_ratio, threshold=quarantine_threshold,
            total_rows=totals.get("extracted", 0),
            quarantined=totals.get("quarantined", 0),
        )

    manifest.auto_discover_outputs()
    manifest.finalize()

    elapsed = time.time() - start_time
    logger.info("="*60)
    logger.info(f"PIPELINE COMPLETED IN {elapsed:.2f}s — run_id={manifest.run_id}")
    logger.info(f"Manifest: {os.path.join(run_dir, 'manifest.json')}")
    logger.info("="*60)

    return {"run_id": manifest.run_id, "run_dir": run_dir,
            "quarantine_failed": quarantine_failed, "q_ratio": q_ratio}

if __name__ == "__main__":
    args = parse_args()
    if args.ui:
        from core.webapp import WebApp
        WebApp.serve()
    elif args.input:
        result = run_pipeline(args.input, args.output,
                              quarantine_threshold=args.quarantine_threshold)
        # Exit code 2 = pipeline completó pero los datos son sospechosos.
        # Exit code 0 = todo OK. Cualquier crash será exit code 1 (default).
        sys.exit(2 if result.get("quarantine_failed") else 0)
    else:
        logger.error("You must provide --input <file> or use --ui to launch the interface.")
        sys.exit(1)
