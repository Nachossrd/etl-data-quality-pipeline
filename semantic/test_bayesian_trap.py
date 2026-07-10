import pytest
import pandas as pd
from semantic.column_scale_normalizer import CurrencyInferenceEngine, ColumnScaleNormalizer
from semantic.integration_fix import normalize_monetary_column_full

def test_bayesian_trap():
    # Array where only one is USD, but the median of the rest is huge (CLP scale)
    df = pd.DataFrame({
        "producto": ["Monitor", "Mouse", "Laptop", "Teclado", "Audifonos"],
        "precio": ["USD 200", "850", "1200000", "350000", "45000"]
    })
    
    # Run integration fix
    df_out = normalize_monetary_column_full(df, "precio")
    
    # The first should be USD, but the rest should fallback to CLP because the median of unlabeled is > 5000
    assert df_out["precio_final_currency"].iloc[0] == "USD"
    assert df_out["precio_final_currency"].iloc[1] == "CLP"
    assert df_out["precio_final_currency"].iloc[2] == "CLP"
    
    # 850 should be corrected to 850000 because its row currency is CLP, triggering the 1000 multiplier
    assert df_out["precio_final"].iloc[1] == 850000.0

def test_empanada_cartel():
    # Array where empanadas are present
    df = pd.DataFrame({
        "producto": ["Notebook Asus", "Monitor LG", "empanadas de queso"],
        "precio": ["1200000", "350000", "1500"]
    })
    
    # Run integration fix
    df_out = normalize_monetary_column_full(df, "precio")
    
    # Empanadas should be excluded from scaling!
    # If they were scaled, 1500 might be inflated to 1,500,000 depending on median.
    # Actually, 1500 is very small compared to 775,000 median, so it WOULD be scaled by 1000!
    # Let's ensure it is NOT scaled.
    assert df_out["precio_scale_corrected"].iloc[2] == False
    assert df_out["precio_final"].iloc[2] == 1500.0
