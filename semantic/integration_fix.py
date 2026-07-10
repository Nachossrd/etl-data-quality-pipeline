import re
import pandas as pd
from typing import Tuple
from semantic.monetary_parser import MonetaryParserBatch
from semantic.column_scale_normalizer import ColumnScaleNormalizer, CurrencyInferenceEngine

def extract_currency(text: str) -> str:
    if pd.isna(text):
        return None
    text_str = str(text).upper()
    # Simple extraction of common currencies
    match = re.search(r'\b(CLP|USD|EUR|GBP|MXN|ARS|BRL)\b', text_str)
    if match:
        return match.group(1)
    if '$' in text_str:
        # Defaulting $ to inferred if no other explicit currency is found
        return '$'
    return None

def normalize_monetary_column_full(
    df: pd.DataFrame,
    target_col: str,
    domain_series: pd.Series = None,
    exchange_rates: dict = None,
    base_currency: str = 'CLP'
) -> pd.DataFrame:
    """
    Full pipeline: clean, infer currency, normalize scale, and FX-liquidate
    every row to a single base currency.

    exchange_rates: e.g. {'USD': 950, 'EUR': 1050}. Multiplier from source→base.
    base_currency: target currency stamped on every row after conversion.
    """
    if exchange_rates is None:
        exchange_rates = {'USD': 950.0, 'EUR': 1050.0}

    # Normalize key casing so 'usd', 'Usd', 'USD' all hit
    exchange_rates = {str(k).strip().upper(): float(v) for k, v in exchange_rates.items()}
    base_currency = str(base_currency).strip().upper()

    df_out = df.copy()

    # Bug cleanup: Remove old corrupted '_sucio' columns
    cols_to_drop = [c for c in df_out.columns if str(c).endswith('_sucio')]
    if cols_to_drop:
        df_out = df_out.drop(columns=cols_to_drop)

    if target_col not in df_out.columns:
        return df_out

    raw_series = df_out[target_col]

    # Optional: Find a domain column to classify if not provided
    if domain_series is None:
        desc_col = next((c for c in df_out.columns if any(k in str(c).lower() for k in ['producto', 'item', 'descripcion', 'nombre'])), None)
        if desc_col:
            from semantic.domain_classifier import DomainClassifier
            domain_series = DomainClassifier.process_series(df_out[desc_col])

    # Step 1: Run MonetaryParserBatch
    parsed_amounts = MonetaryParserBatch.process_series(raw_series)

    # Step 2: Extract and Infer Currency
    extracted_currencies = raw_series.apply(extract_currency)
    inferred_currencies = CurrencyInferenceEngine.infer_currency(extracted_currencies, parsed_amounts)

    # Column Profiling for Scale Normalizer Parameters
    col_name_lower = str(target_col).lower()
    if any(k in col_name_lower for k in ['envio', 'flete', 'descuento', 'shipping']):
        domain_min = 1000.0
        domain_max = 50000.0
        disable_scale_down = True
    else:
        # Principal columns (precio, monto, total)
        domain_min = 5000.0
        domain_max = 5000000.0
        disable_scale_down = False

    # Step 3: Run ColumnScaleNormalizer
    corrected_amounts, correction_flags, report = ColumnScaleNormalizer.normalize_series(
        parsed_amounts, 
        domain_series=domain_series,
        currency_series=inferred_currencies,
        domain_range_min=domain_min,
        domain_range_max=domain_max,
        disable_scale_down=disable_scale_down
    )

    # Audit snapshot: amount + currency BEFORE FX, so the user can still see
    # "the original 200 USD" next to the converted 190.000 CLP.
    pre_fx_amounts = corrected_amounts.copy()
    pre_fx_currencies = inferred_currencies.copy()

    # Step 4: FX Converter (Liquidación Cambiaria)
    # Every row gets stamped with base_currency; foreign rows get multiplied
    # by their exchange rate. Rows already in base_currency are left untouched
    # numerically but normalized in the currency column.
    final_currency_series = inferred_currencies.copy()
    for idx, row_curr in final_currency_series.items():
        if pd.isna(corrected_amounts.at[idx]):
            continue
        if pd.isna(row_curr):
            continue

        row_curr_upper = str(row_curr).strip().upper()

        if row_curr_upper == base_currency:
            final_currency_series.at[idx] = base_currency
            continue

        if row_curr_upper in exchange_rates:
            rate = exchange_rates[row_curr_upper]
            corrected_amounts.at[idx] = corrected_amounts.at[idx] * rate
            final_currency_series.at[idx] = base_currency

    # Output columns
    df_out[f"{target_col}_parsed"] = parsed_amounts
    df_out[f"{target_col}_final"] = corrected_amounts
    df_out[f"{target_col}_final_currency"] = final_currency_series
    df_out[f"{target_col}_scale_corrected"] = correction_flags
    # Audit trail: original amount + original currency, kept untouched by FX.
    df_out[f"{target_col}_monto_origen"] = pre_fx_amounts
    df_out[f"{target_col}_moneda_origen"] = pre_fx_currencies

    return df_out
