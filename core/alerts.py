"""Notifier de eventos del pipeline vía webhook HTTP.

Diseñado para integraciones con Slack, Discord, Microsoft Teams o cualquier
servicio que acepte un POST con JSON. Si `ALERT_WEBHOOK_URL` no está seteada,
el notifier es no-op (logueo local únicamente).

Disparadores soportados:
    - quarantine_threshold_breach: tasa de cuarentena excedió el umbral
    - schema_drift_fatal: schema entrante incompatible con baseline
    - pipeline_failure: excepción no manejada en orquestador

Diseño:
    - Bloqueante por defecto pero con timeout corto (3s)
    - Failures de webhook NO interrumpen el pipeline (logged y siguen)
    - Payload compatible con Slack incoming webhooks (mismo formato que Discord/Teams aceptan)
"""

import json
import logging
import os
import socket
from datetime import datetime
from typing import Any, Dict, Optional
from urllib import request as urlrequest
from urllib.error import URLError

from core.logging_engine import setup_logger

logger = setup_logger("alerts")

DEFAULT_TIMEOUT = 3.0  # segundos
DEFAULT_USERNAME = "Data Clean Pipeline"


class WebhookNotifier:
    """Cliente liviano para webhooks de chat (Slack/Discord/Teams compatible)."""

    def __init__(self, webhook_url: Optional[str] = None,
                  username: str = DEFAULT_USERNAME,
                  timeout: float = DEFAULT_TIMEOUT):
        self.webhook_url = webhook_url or os.environ.get("ALERT_WEBHOOK_URL", "").strip()
        self.username = username
        self.timeout = timeout
        self.hostname = socket.gethostname()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    # ─── Disparadores ────────────────────────────────────────────────────
    def quarantine_breach(self, *, run_id: str, dataset: str, ratio: float,
                           threshold: float, total_rows: int,
                           quarantined: int) -> None:
        """Disparado cuando quarantine_ratio > threshold."""
        title = ":warning: Quarantine threshold excedido"
        details = (
            f"*Run:* `{run_id}` en `{self.hostname}`\n"
            f"*Dataset:* `{dataset}`\n"
            f"*Cuarentena:* {ratio*100:.2f}% (umbral {threshold*100:.1f}%)\n"
            f"*Filas:* {quarantined:,} de {total_rows:,}"
        )
        self._send(title, details, color="warning")

    def schema_drift(self, *, run_id: str, dataset: str, diff: Dict[str, Any]) -> None:
        title = ":rotating_light: Schema drift detectado"
        details = (
            f"*Run:* `{run_id}` en `{self.hostname}`\n"
            f"*Dataset:* `{dataset}`\n"
            f"*Diff:* ```{json.dumps(diff, indent=2)[:600]}```"
        )
        self._send(title, details, color="danger")

    def pipeline_failure(self, *, run_id: str, error: str) -> None:
        title = ":x: Pipeline falló"
        details = (
            f"*Run:* `{run_id}` en `{self.hostname}`\n"
            f"*Error:* ```{error[:500]}```"
        )
        self._send(title, details, color="danger")

    def pipeline_success(self, *, run_id: str, dataset: str,
                          duration_s: float, q_ratio: float) -> None:
        title = ":white_check_mark: Pipeline OK"
        details = (
            f"*Run:* `{run_id}` en `{self.hostname}`\n"
            f"*Dataset:* `{dataset}` — {duration_s:.1f}s\n"
            f"*Cuarentena:* {q_ratio*100:.2f}%"
        )
        self._send(title, details, color="good")

    # ─── Envío ──────────────────────────────────────────────────────────
    def _send(self, title: str, body: str, color: str = "warning") -> bool:
        """Envía POST al webhook. Devuelve True si OK, False si falló (y no excepta)."""
        if not self.enabled:
            logger.debug(f"[alerts] webhook deshabilitado, evento descartado: {title}")
            return False

        # Payload Slack-compatible (Discord/Teams aceptan superset)
        payload = {
            "username": self.username,
            "attachments": [{
                "color": color,
                "title": title,
                "text": body,
                "footer": "Data Clean Pipeline",
                "ts": int(datetime.now().timestamp()),
            }],
        }
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urlrequest.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                code = resp.status
            if 200 <= code < 300:
                logger.info(f"[alerts] webhook OK ({code}): {title}")
                return True
            logger.warning(f"[alerts] webhook respondió {code} para: {title}")
            return False
        except (URLError, socket.timeout, OSError) as e:
            logger.warning(f"[alerts] webhook falló: {type(e).__name__}: {e}")
            return False


# Singleton conveniente
_default_notifier: Optional[WebhookNotifier] = None


def get_notifier() -> WebhookNotifier:
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = WebhookNotifier()
    return _default_notifier
