"""Proyección de demanda mensual con selección de modelo por backtest.

Filosofía: no se elige el modelo por elegante, se elige por el error que comete
contra datos que no vio. Se ajusta una familia corta de modelos sobre la
historia menos el último año, se mide el MAPE de cada uno prediciendo ese año,
y se proyecta con el ganador. La tabla completa de la competencia queda en el
resultado — incluido el modelo perdedor — porque un consultor tiene que poder
mostrar por qué descartó lo demás.

Todos los modelos son reproducibles en una planilla. No hay caja negra:

    naive_estacional        ŷ(t) = y(t-12)
    nivel_estacional_Nm     nivel de los últimos N meses x índice estacional
    tendencia_total         recta sobre toda la serie desestacionalizada
    tendencia_Nm            recta sobre los últimos N meses desestacionalizados
    promedio_movil_12m      media de los últimos 12 meses (sin estacionalidad)

El índice estacional sale siempre de una descomposición multiplicativa clásica:
media móvil centrada de 12 meses -> ratios y/MM -> promedio por mes calendario
normalizado a media 1.

Supuesto explícito de todos ellos: el pasado reciente, sin quiebres de
portafolio ni pérdida de cadenas, representa al futuro. Ganar o perder un
cliente grande invalida la proyección y obliga a intervenirla a mano.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

# Media móvil centrada de período par: 13 puntos con extremos a peso 0.5.
_CMA_WEIGHTS = np.array([0.5] + [1.0] * 11 + [0.5]) / 12.0

# z de la normal estándar por nivel de servicio (stock de seguridad).
Z_SERVICE_LEVEL = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263}


# ────────────────────────── utilidades base ───────────────────────────────
def _centered_moving_average(y: pd.Series) -> pd.Series:
    if len(y) < 13:
        return pd.Series(np.nan, index=y.index)
    return y.rolling(13, center=True).apply(
        lambda w: float(np.dot(w, _CMA_WEIGHTS)), raw=True
    )


def seasonal_index(y: pd.Series) -> Dict[int, float]:
    """Índice estacional multiplicativo por mes, normalizado a media 1."""
    cma = _centered_moving_average(y)
    ratios = (y / cma).dropna()
    if ratios.empty:
        return {m: 1.0 for m in range(1, 13)}
    by_month = ratios.groupby(ratios.index.month).mean().reindex(range(1, 13)).fillna(1.0)
    normalized = by_month / by_month.mean()
    return {int(m): float(v) for m, v in normalized.items()}


def _future_index(y: pd.Series, horizon: int) -> pd.DatetimeIndex:
    return pd.date_range(y.index.max() + pd.offsets.MonthBegin(1),
                         periods=horizon, freq="MS")


def _seasonal_factors(index: pd.DatetimeIndex, idx: Dict[int, float]) -> pd.Series:
    return pd.Series(index.month.map(idx), index=index, dtype=float)


def mape(actual: pd.Series, predicted: pd.Series) -> float:
    actual, predicted = actual.align(predicted, join="inner")
    mask = actual != 0
    if not mask.any():
        return float("nan")
    return float((np.abs(actual[mask] - predicted[mask]) / np.abs(actual[mask])).mean() * 100)


def complete_monthly_index(series: pd.Series) -> tuple:
    """Rellena meses ausentes con nivel local x estacionalidad del mes.

    Un mes faltante no es un cero: si se deja el hueco, la media móvil se
    desalinea y el índice estacional queda sesgado. Devuelve también qué
    períodos se imputaron, para que salgan marcados en el reporte.
    """
    if series.empty:
        return series, []

    full_index = pd.date_range(series.index.min(), series.index.max(), freq="MS")
    reindexed = series.reindex(full_index)
    missing = reindexed[reindexed.isna()].index
    if len(missing) == 0:
        return reindexed, []

    provisional = reindexed.interpolate(method="linear", limit_direction="both")
    idx = seasonal_index(provisional)
    level = (reindexed / reindexed.index.month.map(idx)).interpolate(
        method="linear", limit_direction="both"
    )
    imputed = reindexed.copy()
    for ts in missing:
        imputed.at[ts] = float(level.at[ts] * idx[ts.month])
    return imputed, [ts.strftime("%Y-%m") for ts in missing]


# ─────────────────────── familia de modelos ───────────────────────────────
# Cada modelo: (train, horizon) -> predicciones para los `horizon` meses
# siguientes al final de `train`.

def naive_estacional(train: pd.Series, horizon: int) -> pd.Series:
    future = _future_index(train, horizon)
    values = [
        float(train.get(ts - pd.DateOffset(years=1), train.iloc[-12:].mean()))
        for ts in future
    ]
    return pd.Series(values, index=future)


def _nivel_estacional(train: pd.Series, horizon: int, window: int) -> pd.Series:
    idx = seasonal_index(train)
    deseason = train / _seasonal_factors(train.index, idx)
    nivel = float(deseason.iloc[-window:].mean())
    future = _future_index(train, horizon)
    return pd.Series(nivel, index=future) * _seasonal_factors(future, idx)


def _tendencia(train: pd.Series, horizon: int, window: Optional[int]) -> pd.Series:
    idx = seasonal_index(train)
    deseason = train / _seasonal_factors(train.index, idx)
    t = np.arange(1, len(deseason) + 1, dtype=float)
    values = deseason.to_numpy(dtype=float)
    if window and 0 < window < len(values):
        t, values = t[-window:], values[-window:]
    slope, intercept = np.polyfit(t, values, 1)
    future = _future_index(train, horizon)
    t_future = np.arange(len(deseason) + 1, len(deseason) + horizon + 1, dtype=float)
    return pd.Series(intercept + slope * t_future, index=future) * _seasonal_factors(future, idx)


def promedio_movil_12m(train: pd.Series, horizon: int) -> pd.Series:
    return pd.Series(float(train.iloc[-12:].mean()), index=_future_index(train, horizon))


MODELS: Dict[str, Callable[[pd.Series, int], pd.Series]] = {
    "naive_estacional": naive_estacional,
    "nivel_estacional_12m": lambda tr, h: _nivel_estacional(tr, h, 12),
    "nivel_estacional_6m": lambda tr, h: _nivel_estacional(tr, h, 6),
    "tendencia_total": lambda tr, h: _tendencia(tr, h, None),
    "tendencia_24m": lambda tr, h: _tendencia(tr, h, 24),
    "promedio_movil_12m": promedio_movil_12m,
}

MODEL_LABELS = {
    "naive_estacional": "Naive estacional (mismo mes del año anterior)",
    "nivel_estacional_12m": "Nivel 12m × índice estacional",
    "nivel_estacional_6m": "Nivel 6m × índice estacional",
    "tendencia_total": "Descomposición clásica + tendencia sobre toda la serie",
    "tendencia_24m": "Descomposición clásica + tendencia últimos 24m",
    "promedio_movil_12m": "Promedio móvil 12m (sin estacionalidad)",
}


@dataclass
class ForecastResult:
    history: pd.Series
    imputed_periods: List[str] = field(default_factory=list)
    best_model: str = ""
    comparison: List[dict] = field(default_factory=list)
    seasonal_index: Dict[int, float] = field(default_factory=dict)
    forecast: Optional[pd.Series] = None
    backtest_actual: Optional[pd.Series] = None
    backtest_pred: Optional[pd.Series] = None
    mape_backtest: float = float("nan")
    residual_std: float = float("nan")


def select_and_forecast(series: pd.Series, horizon: int = 12,
                        holdout: int = 12,
                        force_model: Optional[str] = None) -> ForecastResult:
    """Compite los modelos en el holdout, proyecta con el ganador.

    El ganador se reajusta sobre TODA la historia antes de proyectar: el
    backtest sirve para elegir, no para producir el número final.
    """
    y, imputed = complete_monthly_index(series.astype(float).sort_index())
    result = ForecastResult(history=y, imputed_periods=imputed,
                            seasonal_index=seasonal_index(y))

    if len(y) < holdout + 13:
        # Sin historia para competir: naive estacional y a otra cosa.
        result.best_model = "naive_estacional"
        result.forecast = naive_estacional(y, horizon)
        return result

    train, holdout_actual = y.iloc[:-holdout], y.iloc[-holdout:]
    scored = []
    predictions = {}
    for name, model in MODELS.items():
        try:
            pred = model(train, holdout).reindex(holdout_actual.index)
            score = mape(holdout_actual, pred)
        except Exception:  # un modelo roto no debe voltear la comparación
            continue
        predictions[name] = pred
        scored.append({"modelo": name, "etiqueta": MODEL_LABELS.get(name, name),
                       "mape_backtest": round(score, 2)})

    scored.sort(key=lambda r: (np.isnan(r["mape_backtest"]), r["mape_backtest"]))
    result.comparison = scored
    result.best_model = force_model or (scored[0]["modelo"] if scored else "naive_estacional")

    winner = next((r for r in scored if r["modelo"] == result.best_model), None)
    result.mape_backtest = winner["mape_backtest"] if winner else float("nan")
    result.backtest_actual = holdout_actual
    result.backtest_pred = predictions.get(result.best_model)

    result.forecast = MODELS[result.best_model](y, horizon)
    if result.backtest_pred is not None:
        result.residual_std = float((holdout_actual - result.backtest_pred).std())
    return result


def safety_stock(monthly_values: pd.Series, service_level: float = 0.95) -> float:
    """Stock de seguridad mensual = z × desviación de la demanda mensual.

    Cubre la variabilidad de la demanda, no el error del modelo: es el colchón
    para que un mes sobre el promedio no deje al cliente sin producto.
    """
    z = Z_SERVICE_LEVEL.get(round(service_level, 3), 1.6449)
    std = float(monthly_values.astype(float).std())
    return 0.0 if np.isnan(std) else z * std
