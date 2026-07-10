# 🧹 ETL Data Quality Pipeline

> Pipeline experimental de limpieza y calidad de datos para datos transaccionales sucios: detecta el esquema, normaliza semánticamente, valida reglas de negocio, pone en cuarentena lo dudoso y genera reportes de calidad, modelo dimensional y carga analítica — buscando minimizar la configuración manual.

**Por qué este proyecto:** desarrollado tras observar que, en BI, la mayor parte del tiempo se va en *limpiar* datos, no en analizarlos. La idea fue investigar cuánto de ese trabajo repetitivo puede automatizarse de forma auditable, sin ocultar las decisiones detrás de una caja negra.

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="pandas" src="https://img.shields.io/badge/pandas-2.2-150458?logo=pandas&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-UI-009688?logo=fastapi&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

---

## 🎯 El problema

Los datos del mundo real llegan rotos: fechas en cinco formatos distintos en la misma columna, precios como `$850.000`, `350k` o `45425` (un serial de Excel disfrazado de monto), IDs inconsistentes (`TRX-001`, `002`, `id_003`), unidades mezcladas y notas humanas en celdas numéricas.

Este pipeline toma un archivo crudo (`.csv`, `.xlsx` o backup `.bak` de SQL Server) y produce un dataset **limpio, validado y auditado** — decidiendo por sí mismo qué es cada columna y qué filas no son de fiar.

## ✨ Características

- **Detección automática de esquema** — infiere tipos, escalas y dominio de cada columna sin configuración.
- **Normalización semántica** — parsers dedicados para fechas, monedas/precios, IDs, escalas monetarias y clasificación de movimientos.
- **Motor de reglas de negocio** — valida contra reglas declarativas; las violaciones no rompen el run, se aíslan.
- **Cuarentena inteligente** — las filas dudosas se separan a `quarantine/` con el motivo; si superan un umbral (`5%` por defecto) el pipeline **falla con exit code ≠ 0** en lugar de reportar éxito con datos sospechosos.
- **Reportes de calidad** — `quality_report.json`, manifiesto del run y reporte legible por run.
- **Capa analítica** — genera modelo dimensional, esquema SQL, metadata OLAP y artefactos SSAS/XMLA; carga a SQL Server (Docker efímero incluido).
- **Web UI opcional** (FastAPI) para subir archivos y descargar resultados, con autenticación por Bearer token.
- **Bootstrap automático** de dependencias en el primer arranque.

## 🏗️ Arquitectura

```
                    ┌─────────────────────────────────────────────┐
  archivo crudo ──▶ │  extraction → schema detection → cleaning    │
  (.csv/.xlsx/.bak) │  → semantic enrichment → rule validation     │
                    │  → quarantine → export → quality report      │
                    └───────────────┬─────────────────────────────┘
                                    │
             ┌──────────────────────┼──────────────────────┐
             ▼                      ▼                      ▼
     dataset limpio          cuarentena +           modelo dimensional
     + manifest.json         motivos                + SQL / OLAP / SSAS
```

| Módulo        | Rol                                                             |
|---------------|----------------------------------------------------------------|
| `core/`       | extracción, detección de esquema, motor de limpieza, cuarentena, export, reportes |
| `semantic/`   | parsers semánticos (fechas, montos, escalas, clasificación de dominio) |
| `rules/`      | reglas de negocio declarativas (fechas, monedas, IDs, precios, productos) |
| `config/`     | configuración centralizada vía `.env`                          |
| `tests/`      | suite de pruebas (pytest) con fixtures sintéticos              |

## 🚀 Uso

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Copiar y ajustar variables de entorno
cp .env.example .env

# 3. Ejecutar el pipeline sobre el dataset demo
python auto_pipeline.py --input infierno.csv --output ./output

# 4. (opcional) Levantar la Web UI
python auto_pipeline.py --ui
```

**Flags:**

| Flag                        | Descripción                                                |
|-----------------------------|------------------------------------------------------------|
| `--input <archivo>`         | Archivo de entrada (`.csv`, `.xlsx`, `.bak`)               |
| `--output <dir>`            | Directorio de salida (el run vive en `<output>/<run_id>/`) |
| `--quarantine-threshold <f>`| Umbral de cuarentena para fallar el run                    |
| `--ui`                      | Levanta la interfaz web (FastAPI)                          |

## 📁 Dataset de ejemplo — `infierno.csv`

El repositorio incluye `infierno.csv`, un dataset **100% sintético** diseñado para exhibir cada tipo de suciedad: fechas mixtas, precios en formatos caóticos, IDs inconsistentes, seriales de Excel disfrazados de monto y notas humanas en columnas de datos. *No contiene datos reales de ninguna persona ni empresa.*

## 🧪 Tests

```bash
python -m pytest tests/ -v
```

## 🔒 Configuración y secretos

Toda la configuración sensible vive en `.env` (nunca versionado). Ver [`.env.example`](.env.example) para la lista completa de variables: credenciales SQL, webhook de alertas, token de la Web UI, tamaños de chunk y umbrales.

## 📊 Benchmarks

> ⚠️ **Pendiente de medición.** Prefiero dejar la tabla vacía a rellenarla con cifras no verificadas. Las siguientes son las métricas que reporta el pipeline (`quality_report.json` + `manifest.json` por run) y que voy a medir sobre un dataset representativo.

| Métrica | Cómo se mide | Valor |
|---------|--------------|-------|
| Throughput (filas/seg) | filas totales / tiempo de run (`manifest.json`) | _por medir_ |
| Tiempo por 100k filas | run completo sobre dataset de referencia | _por medir_ |
| RAM pico | `psutil` durante el run (chunk = 50k) | _por medir_ |
| % filas en cuarentena | `quality_summary.quarantine_ratio` | _por medir_ |
| Precisión detección de esquema | columnas bien tipadas / total, sobre un set etiquetado | _por medir_ |

_Entorno de referencia: por definir (CPU / RAM / dataset)._

## ⚠️ Limitaciones

- **No reemplaza** un data warehouse ni el criterio de un *data steward*: automatiza el trabajo repetitivo, no la decisión de negocio.
- Las reglas semánticas están calibradas para datos **transaccionales en español (es-CL)**; otros dominios requieren añadir/ajustar reglas.
- En datasets con dominios nuevos conviene **revisar la cuarentena a mano** antes de confiar en el resultado.
- No incluye anonimización: **no procesar PII** sin una capa previa de tratamiento.
- La carga a SQL Server / OLAP asume un entorno con Docker o un servidor accesible.

## 🛠️ Stack

`Python` · `pandas` · `NumPy` · `SQLAlchemy` · `pyodbc` · `PyArrow` · `FastAPI` · `Uvicorn` · `openpyxl` · `rapidfuzz`

---

## ✍️ Autor

**Nacho** — [@Nachossrd](https://github.com/Nachossrd)

Hecho con ☕ y un profundo desprecio por los datos sucios.

## 📄 Licencia

Distribuido bajo licencia MIT. Ver [`LICENSE`](LICENSE).
