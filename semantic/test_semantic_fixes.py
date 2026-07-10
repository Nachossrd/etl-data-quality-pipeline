import pytest
import pandas as pd
import numpy as np
from semantic.monetary_parser import MonetaryParser, MonetaryParserBatch
from semantic.temporal_parser import SerialDateDetector, TemporalParser
from semantic.domain_classifier import DomainClassifier
from semantic.movement_classifier import MovementClassifier
from semantic.semantic_pipeline import SemanticEnricher

def test_monetary_parser_exact_matches():
    # Asegura que MonetaryParser retorne 1200000 para "1.200.000"
    assert MonetaryParser.parse_value("1.200.000") == 1200000.0
    # Asegura que MonetaryParser retorne 60000 para "sesenta lucas"
    assert MonetaryParser.parse_value("sesenta lucas") == 60000.0
    
    # Otros casos
    assert MonetaryParser.parse_value("350k") == 350000.0
    assert MonetaryParser.parse_value("350 k") == 350000.0
    assert MonetaryParser.parse_value("1,200.00") == 1200.0
    assert MonetaryParser.parse_value("un palo") == 1000000.0

def test_serial_date_detector():
    # Asegura que SerialDateDetector reconozca 45425 como el año 2024
    assert SerialDateDetector.convert_serial(45425).year == 2024
    assert SerialDateDetector.convert_serial(45425).month == 5
    assert SerialDateDetector.convert_serial(45425).day == 13 # Approx due to leap year logic, depends on exactly 1899-12-30

    # Test DataFrame column detection
    s_valid = pd.Series([45425, 45426, 45427, 45428])
    assert SerialDateDetector.is_serial_date_column(s_valid) == True
    
    s_invalid_neg = pd.Series([-1, 45426])
    assert SerialDateDetector.is_serial_date_column(s_invalid_neg) == False
    
    s_invalid_float = pd.Series([45425.5, 45426.0])
    assert SerialDateDetector.is_serial_date_column(s_invalid_float) == False

def test_domain_classifier():
    # Asegura que DomainClassifier devuelva un score negativo o estado 'foreign' para "EMPANADAS DE QUESO"
    assert DomainClassifier.classify("EMPANADAS DE QUESO") == 'foreign'
    assert DomainClassifier.score_text("EMPANADAS DE QUESO") < 0

    assert DomainClassifier.classify("Mouse Logitech inalambrico") == 'tech_product'
    assert DomainClassifier.score_text("Mouse Logitech inalambrico") > 0
    
    assert DomainClassifier.classify("Notebook RTX 4090 16GB") == 'tech_product'

def test_movement_classifier():
    # Asegura que MovementClassifier retorne PRESTAMO cuando la nota dice "se lo presté" a pesar de que el estado diga "vendido"
    assert MovementClassifier.classify("se lo presté el lunes", "vendido") == 'PRESTAMO'
    
    # Caso sin contradicción
    assert MovementClassifier.classify("cliente devolvio el producto", "pendiente") == 'DEVOLUCION'
    
    # Caso sin señal en texto libre
    assert MovementClassifier.classify("entregar urgente", "VENTA") == 'VENTA'

def test_semantic_pipeline():
    df = pd.DataFrame({
        "precio": ["1.200.000", "sesenta lucas", "1500"],
        "fecha_compra": [45425, 45426, 45427],
        "producto": ["Notebook RTX 4090", "EMPANADAS", "Mouse Logitech"],
        "estado": ["Vendido", "Vendido", "Pendiente"],
        "nota": ["se lo presté", "almuerzo", "devolucion"]
    })
    
    enriched, metrics = SemanticEnricher().enrich(df)
    
    # No destruction
    assert "precio" in enriched.columns
    assert "precio_parsed" in enriched.columns
    assert "fecha_compra_as_date" in enriched.columns
    assert "producto_domain_flag" in enriched.columns
    assert "estado_semantic" in enriched.columns
    
    # Metric correctness
    assert "precio" in metrics["monetary_columns_parsed"]
    assert "fecha_compra" in metrics["temporal_columns_parsed"]
    assert metrics["movement_reclassifications"] > 0
