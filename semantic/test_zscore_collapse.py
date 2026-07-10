import pytest
import pandas as pd
from semantic.integration_fix import normalize_monetary_column_full

def test_zscore_collapse_massacre():
    # Array simulating: 1200000 (valid), 850 (needs scaling up), 2500 (empanada, ignore), 350 (needs scaling up), 450000 (valid)
    df = pd.DataFrame({
        "producto": ["Notebook Lenovo", "Mouse", "empanadas de pino", "Cable", "Monitor Samsung"],
        "precio": ["1200000", "850", "2500", "350", "450000"]
    })
    
    # Run pipeline
    df_out = normalize_monetary_column_full(df, "precio")
    
    # The valid ones must remain intact
    assert df_out["precio_final"].iloc[0] == 1200000.0
    assert df_out["precio_final"].iloc[4] == 450000.0
    
    # The outliers must be scaled up (because anchors correctly exclude them and the median is based on 1.2M and 450K -> ~825k)
    assert df_out["precio_final"].iloc[1] == 850000.0
    assert df_out["precio_final"].iloc[3] == 350000.0
    
    # The empanada must be ignored
    assert df_out["precio_final"].iloc[2] == 2500.0

def test_zscore_collapse_shipping():
    # Array simulating shipping cost which shouldn't be downscaled
    df = pd.DataFrame({
        "producto": ["Notebook Lenovo", "Mouse", "Cable", "Monitor Samsung"],
        "costo_envio": ["5000", "3500", "8000", "10000"]
    })
    
    # Run pipeline
    df_out = normalize_monetary_column_full(df, "costo_envio")
    
    # Everything should remain intact without downscaling 5000 to 5
    assert df_out["costo_envio_final"].iloc[0] == 5000.0
    assert df_out["costo_envio_final"].iloc[1] == 3500.0
    assert df_out["costo_envio_final"].iloc[2] == 8000.0
    assert df_out["costo_envio_final"].iloc[3] == 10000.0
