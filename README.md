# 🧹 ETL Data Quality Pipeline

> Pipeline de limpieza y calidad de datos **zero-click** para datos transaccionales sucios: detecta el esquema, normaliza semánticamente, valida reglas de negocio, pone en cuarentena lo dudoso y genera reportes de calidad, modelo dimensional y carga analítica — sin configuración manual.

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

## 🛠️ Stack

`Python` · `pandas` · `NumPy` · `SQLAlchemy` · `pyodbc` · `PyArrow` · `FastAPI` · `Uvicorn` · `openpyxl` · `rapidfuzz`

---

## ✍️ Autor

**Nacho** — [@Nachossrd](https://github.com/Nachossrd)

Hecho con ☕ y un profundo desprecio por los datos sucios.

## 📄 Licencia

Distribuido bajo licencia MIT. Ver [`LICENSE`](LICENSE).
