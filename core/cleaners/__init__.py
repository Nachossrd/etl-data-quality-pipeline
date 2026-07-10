"""Cleaners por dominio + registry para selección.

Diseño: cada dataset tiene un cleaner que conoce su semántica (qué columnas
existen, qué se considera basura, qué transformaciones aplicar). El registry
permite registrar cleaners por nombre y seleccionarlos por:

    1. Hint explícito (CLI / config / filename pattern)
    2. Auto-detección por columnas del primer chunk
    3. Fallback al cleaner 'transactional_es' (negocio chileno legacy)

Para agregar un cleaner nuevo:

    from core.cleaners import REGISTRY, Cleaner

    class MiCleaner(Cleaner):
        name = "mi_dataset"
        @staticmethod
        def matches(df): ...
        @staticmethod
        def clean_chunk(df): ...

    REGISTRY.register(MiCleaner)

Ver también: rules/datasets/<name>.yaml para reglas de validación declarativas.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Type

import pandas as pd

from core.logging_engine import setup_logger

logger = setup_logger("cleaners_registry")


class Cleaner(ABC):
    """Interfaz que todo cleaner debe implementar."""

    name: str = "unnamed"

    @staticmethod
    @abstractmethod
    def matches(df: pd.DataFrame) -> bool:
        """¿Este cleaner aplica al chunk dado? Usa columnas/dtypes como señales."""

    @staticmethod
    @abstractmethod
    def clean_chunk(df: pd.DataFrame) -> pd.DataFrame:
        """Aplica la limpieza al chunk y retorna un nuevo DataFrame."""


class CleanerRegistry:
    """Singleton-ish. Mantiene un mapa name -> cleaner_cls + orden de prioridad."""

    def __init__(self) -> None:
        self._by_name: Dict[str, Type[Cleaner]] = {}
        # El orden importa para auto-detección: cleaners más específicos primero.
        # 'transactional_es' va último como fallback.
        self._priority: list[str] = []

    def register(self, cleaner_cls: Type[Cleaner], *, fallback: bool = False) -> None:
        name = cleaner_cls.name
        if name in self._by_name:
            logger.warning(f"Cleaner '{name}' ya registrado, sobreescribiendo")
        self._by_name[name] = cleaner_cls
        if fallback:
            if name in self._priority:
                self._priority.remove(name)
            self._priority.append(name)  # último
        else:
            if name not in self._priority:
                self._priority.insert(0, name)  # más específico primero
        logger.info(f"Cleaner registrado: {name} (fallback={fallback})")

    def get(self, name: str) -> Optional[Type[Cleaner]]:
        return self._by_name.get(name)

    def list_names(self) -> list[str]:
        return list(self._priority)

    def auto_detect(self, df: pd.DataFrame,
                    hint: Optional[str] = None) -> Optional[Type[Cleaner]]:
        """Selecciona el cleaner adecuado para un DataFrame.

        Orden:
            1. Si hint coincide con un nombre registrado: lo usa.
            2. Si hint coincide parcialmente (substring): busca match.
            3. Itera priority order y devuelve el primero que matches() retorna True.
        """
        if hint:
            direct = self.get(hint)
            if direct:
                return direct
            # Substring match para hints del estilo "car_prices_train.csv"
            for name in self._priority:
                if name in hint.lower():
                    return self._by_name[name]

        for name in self._priority:
            cleaner_cls = self._by_name[name]
            try:
                if cleaner_cls.matches(df):
                    return cleaner_cls
            except Exception as e:
                logger.debug(f"matches() de {name} lanzó {type(e).__name__}: {e}")
        return None


REGISTRY = CleanerRegistry()


# ─── Registro automático al importar el package ───────────────────────────────
# (las importaciones se hacen lazy para evitar ciclos)
def _bootstrap_default_cleaners() -> None:
    """Importa y registra los cleaners built-in."""
    from core.cleaners.transactional_es import TransactionalEsCleaner
    from core.cleaners.car_prices import CarPricesPipelineCleaner
    from core.cleaners.imdb_basics import ImdbTitleBasicsCleaner, ImdbTitleRatingsCleaner

    REGISTRY.register(ImdbTitleBasicsCleaner)
    REGISTRY.register(ImdbTitleRatingsCleaner)
    REGISTRY.register(CarPricesPipelineCleaner)
    REGISTRY.register(TransactionalEsCleaner, fallback=True)


_bootstrap_default_cleaners()
