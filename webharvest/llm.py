"""Cliente LLM opcional para el planificador (Qwen local vía vLLM).

Filosofía de uso: el LLM **no reemplaza** las heurísticas del planner, las
asiste SOLO cuando la decisión heurística es dudosa (ver `planner.py`). Con
una sola GPU esto es lo que hace viable el enfoque: en un crawl de cientos
de semillas, el modelo se invoca en el puñado de portadas ambiguas, no en
cada página.

El servidor es OpenAI-compatible (vLLM `serve`). Si no está disponible o
tarda, `available()` devuelve False y el planner cae a su heurística — el
pipeline nunca depende del LLM para funcionar.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger("webharvest.llm")


class LLMClient:
    def __init__(self, endpoint: str, model: str, timeout: float = 20.0,
                 max_tokens: int = 300):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._client = httpx.Client(timeout=timeout)
        self._available: bool | None = None

    def available(self) -> bool:
        """Chequeo perezoso y cacheado del servidor (barato: /health)."""
        if self._available is None:
            try:
                r = self._client.get(f"{self.endpoint}/health", timeout=5)
                self._available = r.status_code < 500
            except Exception as exc:
                log.info("LLM no disponible en %s (%s); se usa solo heurística",
                         self.endpoint, exc)
                self._available = False
        return self._available

    def ask_json(self, system: str, user: str) -> dict | None:
        """Una consulta que debe devolver JSON. None si falla o no hay servidor."""
        if not self.available():
            return None
        try:
            r = self._client.post(f"{self.endpoint}/v1/chat/completions", json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": 0.0,
                "max_tokens": self.max_tokens,
            })
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("Consulta LLM falló (%s); se cae a heurística", exc)
            self._available = False          # no reintentar el resto de la corrida
            return None
        return _extract_json(content)

    def close(self) -> None:
        self._client.close()


def _extract_json(text: str) -> dict | None:
    """Rescata el primer objeto JSON del texto (tolera ```json y prosa)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
