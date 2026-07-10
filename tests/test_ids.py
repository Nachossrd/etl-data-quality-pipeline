import pytest
import pandas as pd
from rules.ids import restore_id_prefix, is_id_column

def test_restore_id_prefix():
    s = pd.Series(["123", "TRX-45", "invalid", None])
    cleaned, stats = restore_id_prefix(s)
    
    assert cleaned[0] == "TRX-0123"
    assert cleaned[1] == "TRX-0045"
    assert pd.isna(cleaned[2])
    assert pd.isna(cleaned[3])
    
    assert stats["fixed_ids"] == 2
    assert stats["invalid_ids"] == 1
    assert stats["null_ids"] == 1

def test_is_id_column():
    assert is_id_column("id_cliente")
    assert is_id_column("cliente_id")
    assert is_id_column("codigo_producto")
    assert not is_id_column("nombre")
