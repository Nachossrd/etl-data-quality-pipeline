"""
Generador de manifest.json por corrida del pipeline.

Propósito empresarial: cuando alguien cuestione un número producido por el
ETL, este archivo permite rastrear EXACTAMENTE de qué archivos vino, qué
versión del código lo procesó y con qué configuración.

Cada corrida genera `output/manifest.json` con:
    - run_id          ID único de la corrida (timestamp + 8 hex)
    - timestamps      inicio, fin, duración
    - environment     Python, OS, código (git hash o "unknown")
    - input_files     lista de {path, sha256, size_bytes, mtime}
    - output_files    lista análoga para archivos generados
    - config_snapshot CHUNK_SIZE, thresholds, OUTPUT_DIR en uso
    - quality_summary referencias a quality_report.json + totales clave

Decisiones de diseño:
    - SHA256 (no MD5) por estándar de auditoría
    - mtime como int (UNIX) para evitar discrepancias de TZ
    - run_id es ordenable lexicográficamente (YYYYMMDD-HHMMSS-XXXXXXXX)
    - "code_version" intenta git → fallback a env var DATA_CLEAN_VERSION → "unknown"
      No usamos pip.__version__ porque este proyecto no se distribuye como package
"""

import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import PipelineConfig


def _sha256_file(path: str, chunk_size: int = 1 << 20) -> str:
    """SHA256 streaming: nunca carga el archivo completo en memoria."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _file_descriptor(path: str) -> Dict[str, Any]:
    """Captura metadata + hash de un archivo. Tolerante a archivos faltantes."""
    p = Path(path)
    if not p.exists():
        return {"path": str(path), "exists": False}
    stat = p.stat()
    return {
        "path": str(p.resolve()),
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_unix": int(stat.st_mtime),
        "sha256": _sha256_file(str(p)),
    }


def _detect_code_version() -> str:
    """git rev-parse → env var → 'unknown'. Sin levantar excepción."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return os.environ.get("DATA_CLEAN_VERSION", "unknown")


def _detect_package_versions() -> Dict[str, str]:
    """Versiones de librerías críticas para reproducibilidad."""
    out: Dict[str, str] = {}
    for pkg in ("pandas", "numpy", "pyarrow", "openpyxl", "pyspark"):
        try:
            mod = __import__(pkg)
            out[pkg] = getattr(mod, "__version__", "unknown")
        except ImportError:
            pass
    return out


class RunManifest:
    """Construye y serializa el manifest a `OUTPUT_DIR/manifest.json`."""

    def __init__(self) -> None:
        now = datetime.datetime.now()
        self.run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.timestamp_start = now.isoformat()
        self.timestamp_end: Optional[str] = None
        self.duration_seconds: Optional[float] = None
        self.input_files: List[Dict[str, Any]] = []
        self.output_files: List[Dict[str, Any]] = []
        self.quality_summary: Dict[str, Any] = {}
        self.errors: List[str] = []

    def register_input(self, path: str) -> None:
        self.input_files.append(_file_descriptor(path))

    def register_output(self, path: str) -> None:
        self.output_files.append(_file_descriptor(path))

    def register_error(self, message: str) -> None:
        self.errors.append(message)

    def attach_quality_summary(self, totals: Dict[str, Any],
                                tables_summary: Dict[str, Any]) -> None:
        """Resumen compacto referenciable; el detalle vive en quality_report.json."""
        self.quality_summary = {
            "totals": totals,
            "tables": {
                t: {
                    "row_counts": data["row_counts"],
                    "schema_fingerprint": data["schema_fingerprint"],
                    "rule_violations_total": sum(data["rule_violations"].values()),
                }
                for t, data in tables_summary.items()
            },
        }

    def auto_discover_outputs(self) -> None:
        """Escanea OUTPUT_DIR y registra todo lo generado durante el run."""
        out_dir = Path(PipelineConfig.OUTPUT_DIR)
        if not out_dir.exists():
            return
        for p in out_dir.rglob("*"):
            if p.is_file() and p.name != "manifest.json":
                self.register_output(str(p))

    def finalize(self) -> Dict[str, Any]:
        end = datetime.datetime.now()
        self.timestamp_end = end.isoformat()
        start = datetime.datetime.fromisoformat(self.timestamp_start)
        self.duration_seconds = round((end - start).total_seconds(), 3)

        manifest = {
            "run_id": self.run_id,
            "timestamps": {
                "start": self.timestamp_start,
                "end": self.timestamp_end,
                "duration_seconds": self.duration_seconds,
            },
            "environment": {
                "python_version": sys.version.split()[0],
                "platform": platform.platform(),
                "code_version": _detect_code_version(),
                "package_versions": _detect_package_versions(),
            },
            "config_snapshot": {
                "CHUNK_SIZE": PipelineConfig.CHUNK_SIZE,
                "OUTPUT_DIR": str(PipelineConfig.OUTPUT_DIR),
                "QUARANTINE_DIR": str(PipelineConfig.QUARANTINE_DIR),
                "CONVERSION_THRESHOLD": PipelineConfig.CONVERSION_THRESHOLD,
                "DETECTION_THRESHOLD": PipelineConfig.DETECTION_THRESHOLD,
            },
            "input_files": self.input_files,
            "output_files": self.output_files,
            "quality_summary": self.quality_summary,
            "errors": self.errors,
        }

        os.makedirs(PipelineConfig.OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(PipelineConfig.OUTPUT_DIR, "manifest.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False, default=str)
        return manifest
