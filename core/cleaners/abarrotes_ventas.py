"""Cleaner para el dataset de ventas de Abarrotes CL (retail, es-CL).

Contexto: un workbook con las ventas mensuales 2019-2022 a cadenas de retail,
partido en dos hojas con esquemas parecidos pero no idénticos:

    Hoja 2019-2020: PERIODO | GRUPO CLIENTES | Código del Cliente |
                    Nombre del Cliente | Tipo de Reposición |
                    Descripción Producto 2 | Unidades | VENTA ($) |
                    COSTO PRODUCTO | Region | Comuna
    Hoja 2021-2022: idem + COD_Local_Descripcion

Este cleaner deja ambas hojas en un **único esquema canónico**, apto para
modelo dimensional y proyección de demanda, y marca (sin borrar) todo lo que un
analista debería mirar antes de firmar una proyección.

Por qué un cleaner propio y no el fallback `transactional_es`:

    - `Código del Cliente` matchea el patrón `^codigo` del detector de IDs
      genérico, que reescribiría `J501` como `TRX-0501`: destruye la llave de
      negocio y funde clientes distintos (`J501` y `N501`) en el mismo ID.
    - `COSTO PRODUCTO` matchea los tokens monetarios del enriquecedor
      semántico, que aplicaría corrección de escala ×1000 a los costos bajos
      contra la mediana de la columna. Sobre montos ya limpios eso no corrige
      nada: inventa datos. Por eso `semantic_options` apaga esas fases (ver
      `Cleaner.semantic_options`); la fase temporal sigue activa.

Normalizaciones aplicadas:

    - PERIODO `202101` -> fecha, año, mes, trimestre, semestre.
    - Nombre de cliente: quita el prefijo de código repetido que aparece en la
      hoja 2021-2022 (`J501-JUMBO BILBAO`, `N678 - N678 - CONCEPCIÓN…`), para
      que el mismo local no cuente como dos clientes distintos entre hojas.
    - Producto: colapsa espacios dobles, corrige typos de marca (`MARITNI`,
      `MART INI`), separa el formato (`500 GR.` -> 500 g) y el tipo de empaque
      (`RRP` = packaging de reposición) del nombre base, y clasifica en
      categoría/familia.
    - Métricas derivadas: margen, % margen, precio y costo unitario.

Flags de calidad (`data_quality_flags`) y su tratamiento:

    unidades_no_positivas / venta_no_positiva  -> cuarentena (devoluciones y
        notas de crédito mezcladas con la venta: distorsionan la proyección)
    costo_no_positivo -> se conserva, pero el margen de esa fila es ficticio
    margen_negativo   -> se conserva: vender bajo costo es real (promociones)
    periodo_invalido  -> cuarentena
"""

import re
import unicodedata

import numpy as np
import pandas as pd

from core.cleaners import Cleaner
from core.logging_engine import setup_logger
from core.validation_engine import ValidationEngine

logger = setup_logger("abarrotes_ventas_cleaner")

# Firma del dataset: normalizada (sin acentos/espacios) para sobrevivir a los
# headers con tilde y a los espacios finales de `Unidades `.
_REQUIRED_SIGNALS = ("periodo", "unidades", "venta", "comuna")
_SUPPORT_SIGNALS = ("grupoclientes", "codigodelcliente", "descripcionproducto2",
                    "costoproducto", "tipodereposicion", "region")

_MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio",
    7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre",
    11: "Noviembre", 12: "Diciembre",
}

# Typos de marca observados en el maestro de productos.
_TYPOS = (
    (re.compile(r"\bMARITNI\b"), "MARTINI"),
    (re.compile(r"\bMART\s+INI\b"), "MARTINI"),
)

# Orden importa: los preparados llevan el nombre de la legumbre que contienen
# ("GUISO LENTEJAS", "SUPREMO DE ARVEJAS"), así que se evalúan primero.
_CATEGORIAS = (
    ("PREPARADOS Y SOPAS", ("SOPA", "GUISO", "POTAGE", "SUPREMO")),
    ("LEGUMBRES", ("ARVEJA", "LENTEJA", "POROTO", "GARBANZO")),
    ("CEREALES Y HARINAS", ("CHUCHOCA", "CHUÑO", "CHUNO", "HARINA", "MAIZ",
                            "POLENTA", "CEBADA", "TRIGO", "CURAGUA")),
)

_FAMILIAS = (
    ("SOPA MIXTA", ("SOPA",)),
    ("GUISO DE LENTEJAS", ("GUISO",)),
    ("POTAGE DE GARBANZO", ("POTAGE",)),
    ("SUPREMO DE ARVEJAS", ("SUPREMO",)),
    ("ARVEJAS", ("ARVEJA",)),
    ("LENTEJAS", ("LENTEJA",)),
    ("POROTOS", ("POROTO",)),
    ("GARBANZOS", ("GARBANZO",)),
    ("CHUCHOCA", ("CHUCHOCA",)),
    ("CHUÑO", ("CHUÑO", "CHUNO")),
    ("HARINA TOSTADA", ("HARINA",)),
    ("MAIZ", ("MAIZ", "CURAGUA")),
    ("POLENTA", ("POLENTA",)),
    ("PERLAS DE CEBADA", ("CEBADA",)),
    ("TRIGO MOTE", ("TRIGO",)),
)

# `500 GR.`, `500GR`, `250 G`, `500 GRS`, `270 G`, `500G`
_FORMATO_RE = re.compile(r"(\d{2,4})\s*(KG|GRS|GR|G)\b\.?", re.IGNORECASE)
# Prefijo de código de local pegado al nombre: `J501-`, `N678 - `, `0100 - `,
# `113-`. Se aplica en bucle porque a veces viene duplicado.
_PREFIJO_CODIGO_RE = re.compile(r"^\s*[A-Z]{0,2}\d{2,5}\s*[-–]\s*", re.IGNORECASE)


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_col(col: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(col).lower())


def _clean_text(value: object) -> object:
    """Trim + colapso de espacios internos. Preserva acentos y mayúsculas."""
    if pd.isna(value):
        return pd.NA
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or pd.NA


def _strip_code_prefix(value: object) -> object:
    """`J501-JUMBO BILBAO` -> `JUMBO BILBAO`; `N678 - N678 - X` -> `X`."""
    text = _clean_text(value)
    if pd.isna(text):
        return pd.NA
    previous = None
    while previous != text:
        previous = text
        text = _PREFIJO_CODIGO_RE.sub("", text).strip()
    # `CORONEL-MANUEL MONTT` y `CORONEL MANUEL MONTT` son el mismo local escrito
    # de dos formas en meses distintos: el guion interno se normaliza a espacio.
    text = re.sub(r"\s*[-–]\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip().upper()
    return text or pd.NA


def _normalize_producto(value: object) -> object:
    if pd.isna(value):
        return pd.NA
    text = re.sub(r"\s+", " ", str(value)).strip().upper().rstrip(".")
    for pattern, replacement in _TYPOS:
        text = pattern.sub(replacement, text)
    return text or pd.NA


def _parse_formato(producto: object) -> tuple:
    """Devuelve (gramos, etiqueta) — `LENTEJAS 5 MM 500 G` -> (500, '500 g')."""
    if pd.isna(producto):
        return (np.nan, pd.NA)
    match = _FORMATO_RE.search(str(producto))
    if not match:
        return (np.nan, pd.NA)
    valor = float(match.group(1))
    unidad = match.group(2).upper()
    gramos = valor * 1000 if unidad == "KG" else valor
    etiqueta = f"{int(gramos)} g" if gramos == int(gramos) else f"{gramos} g"
    return (gramos, etiqueta)


def _producto_base(producto: object) -> object:
    """Nombre sin formato ni marcador de empaque: `ARVEJAS AMARILLAS`."""
    if pd.isna(producto):
        return pd.NA
    text = _FORMATO_RE.sub(" ", str(producto))
    text = re.sub(r"\bRRP\b", " ", text)
    text = re.sub(r"[^A-ZÁÉÍÓÚÑ0-9/ ]+", " ", text.upper())
    text = re.sub(r"\s+", " ", text).strip()
    return text or pd.NA


def _clasificar(producto: object, tabla) -> object:
    if pd.isna(producto):
        return pd.NA
    text = str(producto).upper()
    for etiqueta, tokens in tabla:
        if any(tok in text for tok in tokens):
            return etiqueta
    return "OTROS"


class AbarrotesVentasCleaner(Cleaner):
    """Ventas mensuales de abarrotes a retail: esquema canónico + flags."""

    name = "abarrotes_ventas"
    rules_dataset = "abarrotes_ventas"
    # El dataset queda canonizado aquí; las heurísticas monetarias/dominio del
    # enriquecedor semántico sólo tienen sentido sobre datos sucios y aquí
    # corromperían montos válidos. La fase temporal no se apaga (no es opcional).
    semantic_options = {
        "enable_money": False,
        "enable_movement": False,
        "enable_domain": False,
    }

    # Esquema canónico de salida, en orden de lectura.
    COLUMNS = [
        "data_quality_flags",
        "periodo", "fecha", "anio", "mes", "mes_nombre", "trimestre", "semestre",
        "grupo_cliente", "cliente_codigo", "cliente_nombre",
        "local_codigo", "local_nombre",
        "region_codigo", "region", "comuna",
        "tipo_reposicion",
        "producto", "producto_base", "categoria", "familia",
        "empaque", "formato_gramos", "formato",
        "unidades", "venta_clp", "costo_clp", "margen_clp", "margen_pct",
        "precio_unitario_clp", "costo_unitario_clp",
        "hoja_origen", "quality_score",
    ]

    @staticmethod
    def matches(df: pd.DataFrame) -> bool:
        cols = {_norm_col(c) for c in df.columns}
        if not all(any(sig in c for c in cols) for sig in _REQUIRED_SIGNALS):
            return False
        # Al menos 3 columnas de apoyo: evita capturar cualquier CSV que tenga
        # una columna "venta" y otra "comuna".
        return sum(1 for sig in _SUPPORT_SIGNALS if sig in cols) >= 3

    @staticmethod
    def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
        df = ValidationEngine.validate_structure(df)
        if df.empty:
            return df

        src = {_norm_col(c): c for c in df.columns}

        def col(*candidates):
            for cand in candidates:
                if cand in src:
                    return df[src[cand]]
            return pd.Series(pd.NA, index=df.index)

        out = pd.DataFrame(index=df.index)
        flags = pd.Series([[] for _ in range(len(df))], index=df.index)

        def add_flag(mask: pd.Series, tag: str) -> None:
            mask = mask.fillna(False)
            if not mask.any():
                return
            flags.loc[mask] = flags.loc[mask].apply(lambda tags: tags + [tag])
            logger.warning(f"{int(mask.sum())} filas marcadas '{tag}'")

        # ── Tiempo ───────────────────────────────────────────────────────────
        periodo = pd.to_numeric(col("periodo"), errors="coerce")
        anio = periodo // 100
        mes = periodo % 100
        periodo_ok = periodo.notna() & anio.between(1990, 2100) & mes.between(1, 12)
        add_flag(~periodo_ok, "periodo_invalido")

        anio = anio.where(periodo_ok)
        mes = mes.where(periodo_ok)
        out["periodo"] = periodo.astype("Int64")
        out["fecha"] = pd.to_datetime(
            {"year": anio.fillna(1900), "month": mes.fillna(1), "day": 1},
            errors="coerce",
        ).where(periodo_ok)
        out["anio"] = anio.astype("Int64")
        out["mes"] = mes.astype("Int64")
        out["mes_nombre"] = mes.map(_MESES).astype("string")
        out["trimestre"] = mes.apply(
            lambda m: f"T{int((m - 1) // 3 + 1)}" if pd.notna(m) else pd.NA
        ).astype("string")
        out["semestre"] = mes.apply(
            lambda m: f"S{1 if m <= 6 else 2}" if pd.notna(m) else pd.NA
        ).astype("string")

        # ── Cliente / geografía ──────────────────────────────────────────────
        out["grupo_cliente"] = col("grupoclientes", "grupocliente").apply(_clean_text).astype("string")
        # El código es llave de negocio: se preserva tal cual (sólo trim/upper).
        out["cliente_codigo"] = (
            col("codigodelcliente", "codigocliente")
            .apply(_clean_text).astype("string").str.upper()
        )
        out["cliente_nombre"] = col("nombredelcliente", "nombrecliente").apply(_strip_code_prefix).astype("string")

        local = col("codlocaldescripcion", "locald escripcion")
        out["local_codigo"] = (
            local.astype("string").str.extract(r"^\s*([A-Z]{0,2}\d{2,5})\b", expand=False).str.upper()
        )
        out["local_nombre"] = local.apply(_strip_code_prefix).astype("string")

        region = col("region").apply(_clean_text).astype("string")
        # `VI - Region de O'Higgins` -> código `VI` + nombre limpio.
        out["region_codigo"] = region.str.extract(r"^\s*([IVXRM]{1,4})\s*-", expand=False)
        out["region"] = region.str.replace(r"^\s*[IVXRM]{1,4}\s*-\s*", "", regex=True).str.strip()
        out["comuna"] = col("comuna").apply(_clean_text).astype("string").str.title()
        out["tipo_reposicion"] = col("tipodereposicion", "tiporeposicion").apply(_clean_text).astype("string")

        # ── Producto ─────────────────────────────────────────────────────────
        producto = col("descripcionproducto2", "descripcionproducto", "producto").apply(_normalize_producto)
        out["producto"] = producto.astype("string")
        out["producto_base"] = producto.apply(_producto_base).astype("string")
        out["categoria"] = producto.apply(lambda p: _clasificar(p, _CATEGORIAS)).astype("string")
        out["familia"] = producto.apply(lambda p: _clasificar(p, _FAMILIAS)).astype("string")
        out["empaque"] = np.where(
            producto.fillna("").str.contains(r"\bRRP\b", regex=True), "RRP", "ESTANDAR"
        )
        formato = producto.apply(_parse_formato)
        out["formato_gramos"] = [f[0] for f in formato]
        out["formato"] = pd.Series([f[1] for f in formato], index=df.index, dtype="string")

        # ── Métricas ─────────────────────────────────────────────────────────
        unidades = pd.to_numeric(col("unidades"), errors="coerce")
        venta = pd.to_numeric(col("venta", "ventas"), errors="coerce")
        costo = pd.to_numeric(col("costoproducto", "costo"), errors="coerce")

        add_flag(unidades.isna() | venta.isna(), "metrica_no_numerica")
        add_flag(unidades.notna() & (unidades <= 0), "unidades_no_positivas")
        add_flag(venta.notna() & (venta <= 0), "venta_no_positiva")
        add_flag(costo.notna() & (costo <= 0), "costo_no_positivo")
        add_flag(costo.notna() & venta.notna() & (costo > venta) & (venta > 0), "margen_negativo")

        out["unidades"] = unidades
        out["venta_clp"] = venta
        out["costo_clp"] = costo
        out["margen_clp"] = venta - costo
        out["margen_pct"] = np.where(
            venta.notna() & (venta != 0), (venta - costo) / venta * 100, np.nan
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            out["precio_unitario_clp"] = np.where(
                unidades.notna() & (unidades != 0), venta / unidades, np.nan
            )
            out["costo_unitario_clp"] = np.where(
                unidades.notna() & (unidades != 0), costo / unidades, np.nan
            )

        out["hoja_origen"] = col("hojaorigen").astype("string")
        out.insert(0, "data_quality_flags", flags)
        out["quality_score"] = AbarrotesVentasCleaner._score(out, flags)

        # Esquema canónico estable: las dos hojas del workbook salen idénticas,
        # así el CSV/Parquet de salida no se desalinea al concatenar chunks.
        out = out.reindex(columns=AbarrotesVentasCleaner.COLUMNS)
        return out.reset_index(drop=True)

    @staticmethod
    def _score(out: pd.DataFrame, flags: pd.Series) -> pd.Series:
        """Score 0-100. <50 va a cuarentena (ver QuarantineEngine).

        Las filas irrecuperables para análisis de venta (sin período, sin
        unidades, con venta o unidades no positivas) caen bajo 50. Las
        recuperables pero sospechosas (costo cero, margen negativo) sólo
        descuentan: siguen siendo venta real y deben sumar en los totales.
        """
        score = pd.Series(100, index=out.index, dtype="int64")
        penalties = {
            "periodo_invalido": 60,
            "metrica_no_numerica": 60,
            "unidades_no_positivas": 60,
            "venta_no_positiva": 60,
            "costo_no_positivo": 15,
            "margen_negativo": 10,
        }
        for tag, penalty in penalties.items():
            hit = flags.apply(lambda tags, t=tag: t in tags)
            score -= hit.astype(int) * penalty
        for critical in ("cliente_codigo", "producto", "comuna"):
            score -= out[critical].isna().astype(int) * 20
        return score.clip(lower=0, upper=100)
