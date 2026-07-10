"""Fixtures globales para los tests.

Aísla el directorio de schemas congelados por test para que la corrida de
unos tests no contamine el estado de otros (especialmente importante para
los E2E que usan el orquestador real).
"""

import pytest


@pytest.fixture(autouse=True)
def isolate_schemas_dir(tmp_path, monkeypatch):
    """Cada test obtiene su propio schemas/ vacío."""
    from core import schema_registry
    schemas_dir = tmp_path / "_test_schemas"
    schemas_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(schema_registry, "DEFAULT_SCHEMAS_DIR", schemas_dir)
    yield
