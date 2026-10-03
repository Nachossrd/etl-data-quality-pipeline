"""Tests del cleaner de ventas Abarrotes CL y de la unión multi-hoja.

Cubren lo que puede romperse en silencio y arruinar el análisis aguas abajo:
que se pierda media planilla, que se destruya la llave del cliente, que un
esquema distinto entre hojas desalinee el CSV de salida, y que las
devoluciones se cuelen como demanda.
"""

import numpy as np
import pandas as pd
import pytest

from core.cleaners import REGISTRY
from core.cleaners.abarrotes_ventas import AbarrotesVentasCleaner
from core.cleaning_engine import CleaningEngine
from core.file_extraction import FileExtractor
from core.quarantine_engine import QuarantineEngine


# ─── Fixtures: las dos hojas del workbook, con sus esquemas reales ──────────
def hoja_antigua(rows=3) -> pd.DataFrame:
    """Hoja 2019-2020: sin columna de local, nombre de cliente sin prefijo."""
    return pd.DataFrame({
        "PERIODO": [201901, 201902, 201903][:rows],
        "GRUPO CLIENTES": ["CENCOSUD RETAIL"] * rows,
        "Código del Cliente": ["J501", "N805", "100"][:rows],
        "Nombre del Cliente": ["JUMBO BILBAO", "VALPARAÍSO BRASIL", "ML PILOTO I"][:rows],
        "Tipo de Reposición": ["Interna", "Externa", "Sin reposicion"][:rows],
        "Descripción Producto 2": ["ARVEJAS AMARILLAS  500 GR.",
                                    "CHUÑO  RRP 250 G",
                                    "POROTO MARITNI A LA CHILENA 500 G"][:rows],
        "Unidades ": [49, 10, 5][:rows],
        "VENTA ($)": [43975.99, 12000.0, 6000.0][:rows],
        "COSTO PRODUCTO": [31684.0, 9000.0, 4000.0][:rows],
        "Region": ["RM - Region Metropolitana de Santiago",
                   "V - Region de Valparaíso",
                   "VI - Region de O’Higgins"][:rows],
        "Comuna": ["Las Condes", "Valparaiso", "Doñihue"][:rows],
    })


def hoja_nueva(rows=2) -> pd.DataFrame:
    """Hoja 2021-2022: columna de local extra y nombre con prefijo de código."""
    return pd.DataFrame({
        "PERIODO": [202101, 202102][:rows],
        "GRUPO CLIENTES": ["CENCOSUD RETAIL"] * rows,
        "Código del Cliente": ["J501", "N983"][:rows],
        "Nombre del Cliente": ["J501-JUMBO BILBAO", "983 - CORONEL-MANUEL MONTT"][:rows],
        "COD_Local_Descripcion": ["J501 - JUMBO BILBAO",
                                   "N983 - N983 - CORONEL MANUEL MONTT"][:rows],
        "Tipo de Reposición": ["Interna", "Interna"][:rows],
        "Descripción Producto 2": ["ARVEJAS AMARILLAS  500 GR.",
                                    "GUISO LENTEJAS  500GR"][:rows],
        "Unidades ": [58, 4][:rows],
        "VENTA ($)": [53793.88, 5474.0][:rows],
        "COSTO PRODUCTO": [41349.0, 0.0][:rows],
        "Region": ["RM - Region Metropolitana de Santiago",
                   "VIII - Region del Biobío"][:rows],
        "Comuna": ["Las Condes", "Coronel"][:rows],
    })


# ─── Detección ─────────────────────────────────────────────────────────────
def test_autodetect_selecciona_el_cleaner_de_abarrotes():
    assert REGISTRY.auto_detect(hoja_antigua()).name == "abarrotes_ventas"
    assert REGISTRY.auto_detect(hoja_nueva()).name == "abarrotes_ventas"


def test_no_captura_datasets_ajenos():
    """Una tabla con 'venta' pero sin la firma del caso no debe caer aquí."""
    df = pd.DataFrame({"venta": [1, 2], "comuna": ["A", "B"],
                       "periodo": [1, 2], "unidades": [1, 1]})
    assert not AbarrotesVentasCleaner.matches(df)


def test_registrado_antes_del_fallback():
    names = REGISTRY.list_names()
    assert names.index("abarrotes_ventas") < names.index("transactional_es")


# ─── Esquema canónico ──────────────────────────────────────────────────────
def test_ambas_hojas_producen_el_mismo_esquema():
    """Si los esquemas difieren, el CSV por chunks queda desalineado."""
    a = CleaningEngine.clean_chunk(hoja_antigua())
    b = CleaningEngine.clean_chunk(hoja_nueva())
    assert list(a.columns) == list(b.columns) == AbarrotesVentasCleaner.COLUMNS


def test_columnas_de_local_ausentes_quedan_nulas_no_rompen():
    a = CleaningEngine.clean_chunk(hoja_antigua())
    assert a["local_codigo"].isna().all()
    assert a["local_nombre"].isna().all()


# ─── La llave de negocio se preserva ───────────────────────────────────────
def test_codigo_de_cliente_no_se_reescribe_como_trx():
    """El detector de IDs genérico convertiría J501 en TRX-0501: eso funde
    clientes distintos y destruye la llave. Aquí debe sobrevivir intacto."""
    out = CleaningEngine.clean_chunk(hoja_antigua())
    assert out["cliente_codigo"].tolist() == ["J501", "N805", "100"]
    assert not out["cliente_codigo"].astype(str).str.startswith("TRX").any()


def test_nombre_de_cliente_pierde_el_prefijo_de_codigo():
    out = CleaningEngine.clean_chunk(hoja_nueva())
    assert out.loc[0, "cliente_nombre"] == "JUMBO BILBAO"
    # Prefijo duplicado y guion interno normalizados.
    assert out.loc[1, "cliente_nombre"] == "CORONEL MANUEL MONTT"
    assert out.loc[1, "local_nombre"] == "CORONEL MANUEL MONTT"


def test_el_mismo_local_converge_al_mismo_nombre_entre_hojas():
    """J501 aparece como 'JUMBO BILBAO' en una hoja y 'J501-JUMBO BILBAO' en
    la otra: tras la limpieza deben ser el mismo cliente."""
    a = CleaningEngine.clean_chunk(hoja_antigua())
    b = CleaningEngine.clean_chunk(hoja_nueva())
    assert a.loc[0, "cliente_nombre"] == b.loc[0, "cliente_nombre"]


# ─── Tiempo ────────────────────────────────────────────────────────────────
def test_periodo_se_descompone_en_calendario():
    out = CleaningEngine.clean_chunk(hoja_antigua())
    assert out.loc[0, "anio"] == 2019
    assert out.loc[0, "mes"] == 1
    assert out.loc[0, "fecha"] == pd.Timestamp("2019-01-01")
    assert out.loc[0, "trimestre"] == "T1"
    assert out.loc[0, "semestre"] == "S1"
    assert out.loc[2, "trimestre"] == "T1" and out.loc[2, "mes_nombre"] == "Marzo"


def test_periodo_invalido_va_a_cuarentena():
    df = hoja_antigua(1)
    df.loc[0, "PERIODO"] = 201913          # mes 13 no existe
    out = CleaningEngine.clean_chunk(df)
    assert "periodo_invalido" in out.loc[0, "data_quality_flags"]
    assert out.loc[0, "quality_score"] < 50


# ─── Producto ──────────────────────────────────────────────────────────────
def test_producto_normaliza_espacios_typos_formato_y_empaque():
    out = CleaningEngine.clean_chunk(hoja_antigua())
    assert out.loc[0, "producto"] == "ARVEJAS AMARILLAS 500 GR"
    assert out.loc[0, "producto_base"] == "ARVEJAS AMARILLAS"
    assert out.loc[0, "formato_gramos"] == 500
    assert out.loc[0, "empaque"] == "ESTANDAR"
    assert out.loc[1, "empaque"] == "RRP"          # 'CHUÑO  RRP 250 G'
    assert out.loc[1, "formato_gramos"] == 250
    assert "MARTINI" in out.loc[2, "producto"]     # typo MARITNI corregido


def test_clasificacion_por_categoria_y_familia():
    out = CleaningEngine.clean_chunk(hoja_antigua())
    assert out.loc[0, "categoria"] == "LEGUMBRES"
    assert out.loc[0, "familia"] == "ARVEJAS"
    assert out.loc[1, "categoria"] == "CEREALES Y HARINAS"
    assert out.loc[1, "familia"] == "CHUÑO"


def test_preparados_ganan_a_la_legumbre_que_contienen():
    """'GUISO LENTEJAS' es un preparado, no la legumbre suelta."""
    out = CleaningEngine.clean_chunk(hoja_nueva())
    assert out.loc[1, "categoria"] == "PREPARADOS Y SOPAS"
    assert out.loc[1, "familia"] == "GUISO DE LENTEJAS"


# ─── Geografía ─────────────────────────────────────────────────────────────
def test_region_se_separa_en_codigo_y_nombre():
    out = CleaningEngine.clean_chunk(hoja_antigua())
    assert out.loc[0, "region_codigo"] == "RM"
    assert out.loc[0, "region"] == "Region Metropolitana de Santiago"
    assert out.loc[1, "region_codigo"] == "V"


# ─── Métricas y flags ──────────────────────────────────────────────────────
def test_metricas_derivadas():
    out = CleaningEngine.clean_chunk(hoja_antigua(1))
    assert out.loc[0, "margen_clp"] == pytest.approx(43975.99 - 31684.0)
    assert out.loc[0, "margen_pct"] == pytest.approx(
        (43975.99 - 31684.0) / 43975.99 * 100)
    assert out.loc[0, "precio_unitario_clp"] == pytest.approx(43975.99 / 49)


def test_costo_no_positivo_se_marca_pero_la_fila_sobrevive():
    """Costo 0 con venta real: el margen no es confiable, pero la venta sí."""
    out = CleaningEngine.clean_chunk(hoja_nueva())
    assert "costo_no_positivo" in out.loc[1, "data_quality_flags"]
    assert out.loc[1, "quality_score"] >= 50
    limpio = QuarantineEngine.process(out, "t_costo")
    assert len(limpio) == 2


def test_margen_negativo_se_marca_y_se_conserva():
    """Vender bajo costo es real (promoción): marcarlo, no borrarlo — si se
    borra, el total vendido deja de cuadrar con la contabilidad."""
    df = hoja_antigua(1)
    df.loc[0, "COSTO PRODUCTO"] = 99999.0
    out = CleaningEngine.clean_chunk(df)
    assert "margen_negativo" in out.loc[0, "data_quality_flags"]
    assert out.loc[0, "quality_score"] >= 50


def test_devoluciones_van_a_cuarentena():
    """Unidades y venta negativas son devoluciones: no son demanda futura."""
    df = hoja_antigua(1)
    df.loc[0, "Unidades "] = -4
    df.loc[0, "VENTA ($)"] = -5916.0
    out = CleaningEngine.clean_chunk(df)
    flags = out.loc[0, "data_quality_flags"]
    assert "unidades_no_positivas" in flags and "venta_no_positiva" in flags
    assert out.loc[0, "quality_score"] < 50
    assert QuarantineEngine.process(out, "t_devol").empty


def test_flags_son_listas_no_strings():
    out = CleaningEngine.clean_chunk(hoja_antigua())
    assert all(isinstance(v, list) for v in out["data_quality_flags"])


def test_chunk_vacio_no_rompe():
    assert AbarrotesVentasCleaner.clean_chunk(pd.DataFrame()).empty


# ─── Contrato con el resto del pipeline ────────────────────────────────────
def test_declara_su_yaml_de_reglas_y_apaga_las_fases_heuristicas():
    """El enriquecedor semántico aplicaría corrección de escala ×1000 sobre
    'costo': sobre montos ya limpios eso inventa datos, no los corrige."""
    assert AbarrotesVentasCleaner.rules_dataset == "abarrotes_ventas"
    opts = AbarrotesVentasCleaner.semantic_options
    assert opts["enable_money"] is False
    assert opts["enable_domain"] is False
    assert opts["enable_movement"] is False


def test_select_cleaner_expone_la_clase_al_orquestador():
    cls = CleaningEngine.select_cleaner(hoja_nueva(), hint="Ventas_2019-al-2022")
    assert cls is AbarrotesVentasCleaner


def test_las_reglas_declarativas_cargan_y_aceptan_la_salida_limpia():
    from core.rule_validator import RuleValidator
    validator = RuleValidator.for_dataset("abarrotes_ventas")
    assert validator is not None
    out = CleaningEngine.clean_chunk(hoja_antigua())
    outcome = validator.validate(out)
    assert not outcome.is_fatal
    assert outcome.quarantine_df.empty


# ─── Unión multi-hoja del extractor ────────────────────────────────────────
def test_extract_excel_une_hojas_con_esquema_compatible(tmp_path):
    """El bug original: se elegía una hoja y la otra se perdía en silencio."""
    path = tmp_path / "ventas.xlsx"
    with pd.ExcelWriter(path) as w:
        hoja_antigua().to_excel(w, sheet_name="Ventas 2019 - 2020", index=False)
        hoja_nueva().to_excel(w, sheet_name="Ventas 2021 - 2022", index=False)

    chunks = list(FileExtractor.extract_excel(str(path)))
    assert len(chunks) == 2
    assert sum(len(c) for c in chunks) == 5
    # Trazabilidad: cada fila sabe de qué hoja vino.
    origenes = set()
    for c in chunks:
        assert "_hoja_origen" in c.columns
        origenes.update(c["_hoja_origen"].unique())
    assert origenes == {"Ventas 2019 - 2020", "Ventas 2021 - 2022"}


def test_extract_excel_descarta_hojas_de_otro_esquema(tmp_path):
    path = tmp_path / "mixto.xlsx"
    with pd.ExcelWriter(path) as w:
        hoja_antigua().to_excel(w, sheet_name="Ventas", index=False)
        pd.DataFrame({"glosario": ["RRP"], "significado": ["empaque"]}).to_excel(
            w, sheet_name="Diccionario", index=False)

    chunks = list(FileExtractor.extract_excel(str(path)))
    assert len(chunks) == 1
    assert len(chunks[0]) == 3


def test_extract_excel_una_sola_hoja_no_agrega_columnas(tmp_path):
    """Sin unión no hay columna de procedencia: comportamiento previo intacto."""
    path = tmp_path / "simple.xlsx"
    hoja_antigua().to_excel(path, index=False, sheet_name="Datos")
    chunks = list(FileExtractor.extract_excel(str(path)))
    assert len(chunks) == 1
    assert "_hoja_origen" not in chunks[0].columns


def test_la_procedencia_llega_al_dataset_limpio(tmp_path):
    path = tmp_path / "ventas.xlsx"
    with pd.ExcelWriter(path) as w:
        hoja_antigua().to_excel(w, sheet_name="Ventas 2019 - 2020", index=False)
        hoja_nueva().to_excel(w, sheet_name="Ventas 2021 - 2022", index=False)
    limpio = pd.concat(
        [CleaningEngine.clean_chunk(c) for c in FileExtractor.extract_excel(str(path))],
        ignore_index=True,
    )
    assert set(limpio["hoja_origen"].dropna().unique()) == {
        "Ventas 2019 - 2020", "Ventas 2021 - 2022"}
    assert len(limpio) == 5
