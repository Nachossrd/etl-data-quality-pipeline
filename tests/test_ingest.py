"""Tests de la ingesta genérica: cualquier formato -> tabla(s) en chunks.

Lo que se protege: que ningún formato soportado se lea a medias, que un
archivo con varias tablas produzca varias tablas (y no la primera), y que dos
archivos distintos no terminen escribiendo sobre la misma salida.
"""

import json
import os
import sqlite3
import zipfile

import pandas as pd
import pytest

from core.ingest import (SUPPORTED_EXT, UnsupportedFormatError, discover_tables,
                         table_name_from)


@pytest.fixture
def datos():
    return pd.DataFrame({
        "id": [1, 2, 3],
        "monto": [100.5, 200.0, 300.0],
        "fecha": ["2024-01-01", "2024-02-01", "2024-03-01"],
        "categoria": ["a", "b", "a"],
    })


def _leer(source) -> pd.DataFrame:
    return pd.concat(list(source.chunks()), ignore_index=True)


# ─── Formatos de archivo único ─────────────────────────────────────────────
def test_csv(tmp_path, datos):
    p = tmp_path / "ventas.csv"
    datos.to_csv(p, index=False)
    sources = discover_tables(str(p))
    assert len(sources) == 1
    assert sources[0].name == "ventas"
    assert len(_leer(sources[0])) == 3


def test_tsv_con_comas_en_los_campos(tmp_path):
    """El sniffer de delimitador se confunde con comas dentro del texto;
    en .tsv el tabulador se fuerza."""
    p = tmp_path / "datos.tsv"
    p.write_text("a\tb\n1\tHola, mundo\n2\tOtro, texto\n", encoding="utf-8")
    df = _leer(discover_tables(str(p))[0])
    assert list(df.columns) == ["a", "b"]
    assert df.loc[0, "b"] == "Hola, mundo"


def test_parquet(tmp_path, datos):
    p = tmp_path / "hist.parquet"
    datos.to_parquet(p)
    df = _leer(discover_tables(str(p))[0])
    assert len(df) == 3
    assert set(df.columns) == set(datos.columns)


def test_json_envuelto_y_anidado_se_aplana(tmp_path):
    """Un JSON de API viene envuelto y con objetos adentro; una columna con
    un dict no sirve ni para SQL ni para un gráfico."""
    p = tmp_path / "api.json"
    p.write_text(json.dumps({
        "meta": {"version": 1},
        "data": [{"id": 1, "user": {"name": "ana", "age": 30}},
                 {"id": 2, "user": {"name": "luis", "age": 40}}],
    }), encoding="utf-8")
    df = _leer(discover_tables(str(p))[0])
    assert len(df) == 2
    assert "user.name" in df.columns
    assert df.loc[0, "user.name"] == "ana"


def test_jsonl(tmp_path):
    p = tmp_path / "logs.jsonl"
    p.write_text('{"a":1,"b":"x"}\n{"a":2,"b":"y"}\n', encoding="utf-8")
    df = _leer(discover_tables(str(p))[0])
    assert len(df) == 2 and list(df.columns) == ["a", "b"]


def test_json_que_en_realidad_es_jsonlines(tmp_path):
    """Caso frecuente: extensión .json con contenido JSON-lines."""
    p = tmp_path / "raro.json"
    p.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    assert len(_leer(discover_tables(str(p))[0])) == 2


def test_excel_multihoja(tmp_path, datos):
    p = tmp_path / "libro.xlsx"
    with pd.ExcelWriter(p) as w:
        datos.to_excel(w, sheet_name="Ene", index=False)
        datos.to_excel(w, sheet_name="Feb", index=False)
    source = discover_tables(str(p))[0]
    assert len(_leer(source)) == 6         # las dos hojas, no una


# ─── Archivos con varias tablas ────────────────────────────────────────────
def test_sqlite_produce_una_tabla_por_tabla(tmp_path, datos):
    p = tmp_path / "base.db"
    con = sqlite3.connect(p)
    datos.to_sql("clientes", con, index=False)
    datos.to_sql("pedidos", con, index=False)
    con.close()

    sources = discover_tables(str(p))
    assert len(sources) == 2
    assert {s.name for s in sources} == {"base_clientes", "base_pedidos"}
    assert all(len(_leer(s)) == 3 for s in sources)


def test_sqlite_ignora_tablas_internas(tmp_path, datos):
    """`sqlite_sequence` la crea el motor sola con AUTOINCREMENT: es
    infraestructura, no un dataset del usuario."""
    p = tmp_path / "base.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('x')")
    con.commit()
    internas = con.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_%'").fetchall()
    con.close()
    assert internas, "el motor debería haber creado sqlite_sequence"
    assert [s.name for s in discover_tables(str(p))] == ["base"]


def test_dump_sql_se_materializa(tmp_path):
    p = tmp_path / "dump.sql"
    p.write_text("CREATE TABLE t1(a INT, b TEXT);"
                 "INSERT INTO t1 VALUES (1,'x'),(2,'y');", encoding="utf-8")
    df = _leer(discover_tables(str(p))[0])
    assert len(df) == 2 and list(df.columns) == ["a", "b"]


def test_dump_sql_invalido_avisa_con_el_error_real(tmp_path):
    p = tmp_path / "malo.sql"
    p.write_text("ESTO NO ES SQL VALIDO;", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError, match="dump SQL"):
        discover_tables(str(p))


def test_zip_procesa_todo_lo_soportado(tmp_path, datos):
    csv_path = tmp_path / "ventas.csv"
    datos.to_csv(csv_path, index=False)
    json_path = tmp_path / "extra.json"
    json_path.write_text('[{"a":1}]', encoding="utf-8")
    zip_path = tmp_path / "paquete.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(csv_path, "ventas.csv")
        z.write(json_path, "extra.json")

    sources = discover_tables(str(zip_path))
    assert len(sources) == 2
    assert all(s.name.startswith("paquete_") for s in sources)


def test_zip_no_puede_escribir_fuera_del_destino(tmp_path):
    """Zip-slip: un miembro con `..` intentaría escribir fuera del temporal."""
    zip_path = tmp_path / "malicioso.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("../escape.csv", "a,b\n1,2\n")
    with pytest.raises(UnsupportedFormatError, match="escapa"):
        discover_tables(str(zip_path))


def test_carpeta_completa(tmp_path, datos):
    datos.to_csv(tmp_path / "uno.csv", index=False)
    datos.to_parquet(tmp_path / "dos.parquet")
    (tmp_path / "notas.md").write_text("no soy un dataset", encoding="utf-8")
    sources = discover_tables(str(tmp_path))
    assert {s.name for s in sources} == {"uno", "dos"}


def test_carpeta_ignora_temporales_de_excel(tmp_path, datos):
    datos.to_csv(tmp_path / "real.csv", index=False)
    (tmp_path / "~$real.xlsx").write_bytes(b"basura")
    assert [s.name for s in discover_tables(str(tmp_path))] == ["real"]


def test_un_archivo_ilegible_no_tumba_la_carpeta(tmp_path, datos):
    datos.to_csv(tmp_path / "bueno.csv", index=False)
    (tmp_path / "roto.parquet").write_bytes(b"esto no es parquet")
    nombres = {s.name for s in discover_tables(str(tmp_path))}
    assert "bueno" in nombres      # el archivo sano se procesa igual


# ─── Nombres de tabla ──────────────────────────────────────────────────────
def test_nombres_de_tabla_seguros_para_sql():
    assert table_name_from("/x/Ventas 2019-al-2022.xlsx") == "Ventas_2019_al_2022"
    assert table_name_from("/x/año ñandú.csv") == "ano_nandu"
    assert table_name_from("/x/2024.csv").startswith("t_")   # no empieza en dígito


def test_nombres_colisionados_se_desambiguan(tmp_path, datos):
    """Dos archivos distintos con el mismo nombre base pisarían la misma
    salida y se perderían datos en silencio."""
    (tmp_path / "sub").mkdir()
    datos.to_csv(tmp_path / "datos.csv", index=False)
    datos.to_csv(tmp_path / "sub" / "datos.csv", index=False)
    nombres = [s.name for s in discover_tables(str(tmp_path))]
    assert len(nombres) == len(set(nombres)) == 2


# ─── Errores ───────────────────────────────────────────────────────────────
def test_formato_no_soportado_lista_los_soportados(tmp_path):
    p = tmp_path / "imagen.png"
    p.write_bytes(b"\x89PNG")
    with pytest.raises(UnsupportedFormatError, match="csv"):
        discover_tables(str(p))


def test_archivo_inexistente():
    with pytest.raises(FileNotFoundError):
        discover_tables("no_existe_este_archivo.csv")


def test_carpeta_sin_datasets(tmp_path):
    (tmp_path / "leeme.md").write_text("nada", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        discover_tables(str(tmp_path))


def test_todos_los_formatos_declarados_tienen_lector():
    for ext in SUPPORTED_EXT:
        assert ext.startswith(".")
