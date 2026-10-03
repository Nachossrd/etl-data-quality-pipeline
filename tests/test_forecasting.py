"""Tests de la capa de proyección de demanda.

Lo que se protege aquí: que un mes ausente no se convierta en un cero, que la
selección de modelo sea realmente por error medido y no por orden de
declaración, y que la proyección salga con la forma y el horizonte correctos.
"""

import numpy as np
import pandas as pd
import pytest

from analytics.forecasting import (
    MODELS,
    complete_monthly_index,
    mape,
    naive_estacional,
    safety_stock,
    seasonal_index,
    select_and_forecast,
)


def serie_estacional(anios=4, nivel=1000, amplitud=0.3, tendencia=0.0,
                     inicio="2019-01") -> pd.Series:
    """Serie mensual sintética: nivel + estacionalidad senoidal + tendencia."""
    n = anios * 12
    idx = pd.date_range(inicio, periods=n, freq="MS")
    t = np.arange(n)
    estacional = 1 + amplitud * np.sin(2 * np.pi * (idx.month - 1) / 12)
    return pd.Series((nivel + tendencia * t) * estacional, index=idx)


# ─── Meses ausentes ────────────────────────────────────────────────────────
def test_mes_ausente_se_imputa_no_se_asume_cero():
    y = serie_estacional()
    con_hueco = y.drop(y.index[40])
    completa, imputados = complete_monthly_index(con_hueco)

    assert len(completa) == len(y)
    assert imputados == [y.index[40].strftime("%Y-%m")]
    assert completa.isna().sum() == 0
    # El valor imputado respeta el nivel de la serie, no la aplasta a cero.
    assert completa.iloc[40] > y.min() * 0.5


def test_serie_completa_no_se_toca():
    y = serie_estacional()
    completa, imputados = complete_monthly_index(y)
    assert imputados == []
    pd.testing.assert_series_equal(completa, y, check_freq=False)


def test_imputacion_conserva_la_estacionalidad_del_mes():
    """Un mes valle imputado no puede quedar con el nivel de un mes peak."""
    y = serie_estacional(amplitud=0.5)
    idx_valle = [i for i, ts in enumerate(y.index) if ts.month == 10][-1]
    completa, _ = complete_monthly_index(y.drop(y.index[idx_valle]))
    otros_octubres = y[(y.index.month == 10)].drop(y.index[idx_valle])
    assert abs(completa.iloc[idx_valle] - otros_octubres.mean()) < otros_octubres.mean() * 0.25


# ─── Índice estacional ─────────────────────────────────────────────────────
def test_indice_estacional_promedia_uno():
    idx = seasonal_index(serie_estacional())
    assert len(idx) == 12
    assert np.mean(list(idx.values())) == pytest.approx(1.0, abs=1e-6)


def test_indice_estacional_detecta_el_peak_correcto():
    y = serie_estacional(amplitud=0.4)
    idx = seasonal_index(y)
    mes_peak = max(idx, key=idx.get)
    assert idx[mes_peak] > 1.2
    # El seno con fase 0 tiene su máximo en abril (mes 4).
    assert mes_peak == 4


# ─── Selección de modelo ───────────────────────────────────────────────────
def test_gana_el_modelo_de_menor_mape():
    res = select_and_forecast(serie_estacional(), horizon=12)
    mapes = [r["mape_backtest"] for r in res.comparison]
    assert res.best_model == res.comparison[0]["modelo"]
    assert res.comparison[0]["mape_backtest"] == min(mapes)
    assert res.mape_backtest == min(mapes)


def test_todos_los_modelos_compiten():
    res = select_and_forecast(serie_estacional(), horizon=12)
    assert {r["modelo"] for r in res.comparison} == set(MODELS)


def test_serie_sin_tendencia_favorece_un_modelo_de_nivel():
    """Sin tendencia real, extrapolar una recta no debería ganar."""
    res = select_and_forecast(serie_estacional(tendencia=0.0), horizon=12)
    assert res.best_model.startswith(("nivel_estacional", "naive"))


def test_force_model_respeta_la_eleccion_externa():
    """La venta se proyecta con el modelo ganador de unidades, no con el suyo."""
    res = select_and_forecast(serie_estacional(), horizon=12,
                              force_model="naive_estacional")
    assert res.best_model == "naive_estacional"


# ─── Forma de la proyección ────────────────────────────────────────────────
def test_horizonte_y_continuidad_del_calendario():
    y = serie_estacional()
    res = select_and_forecast(y, horizon=12)
    assert len(res.forecast) == 12
    assert res.forecast.index[0] == y.index[-1] + pd.offsets.MonthBegin(1)
    assert res.forecast.index[-1] == y.index[-1] + pd.offsets.MonthBegin(12)


def test_la_proyeccion_conserva_el_perfil_estacional():
    y = serie_estacional(amplitud=0.4)
    res = select_and_forecast(y, horizon=12)
    f = res.forecast
    assert f.idxmax().month == y.groupby(y.index.month).mean().idxmax()


def test_proyeccion_de_serie_plana_es_plana():
    y = pd.Series(500.0, index=pd.date_range("2019-01", periods=48, freq="MS"))
    res = select_and_forecast(y, horizon=12)
    assert res.forecast.std() == pytest.approx(0.0, abs=1e-6)
    assert res.forecast.mean() == pytest.approx(500.0, rel=1e-6)


def test_historia_corta_cae_al_naive_sin_reventar():
    y = serie_estacional(anios=1)
    res = select_and_forecast(y, horizon=12)
    assert res.best_model == "naive_estacional"
    assert len(res.forecast) == 12


def test_naive_estacional_repite_el_mismo_mes_del_ano_anterior():
    y = serie_estacional()
    pred = naive_estacional(y, 12)
    np.testing.assert_allclose(pred.to_numpy(), y.iloc[-12:].to_numpy())


# ─── Métricas ──────────────────────────────────────────────────────────────
def test_mape_de_prediccion_perfecta_es_cero():
    y = serie_estacional()
    assert mape(y, y) == pytest.approx(0.0)


def test_mape_conocido():
    actual = pd.Series([100.0, 200.0], index=pd.date_range("2024-01", periods=2, freq="MS"))
    pred = pd.Series([110.0, 180.0], index=actual.index)
    assert mape(actual, pred) == pytest.approx(10.0)   # (10% + 10%) / 2


def test_stock_de_seguridad_crece_con_la_variabilidad():
    estable = pd.Series([100.0] * 12)
    volatil = pd.Series([50.0, 150.0] * 6)
    assert safety_stock(estable) == pytest.approx(0.0)
    assert safety_stock(volatil) > safety_stock(estable)


def test_stock_de_seguridad_crece_con_el_nivel_de_servicio():
    demanda = pd.Series([80.0, 120.0, 100.0, 90.0, 110.0, 95.0])
    assert safety_stock(demanda, 0.99) > safety_stock(demanda, 0.90)
