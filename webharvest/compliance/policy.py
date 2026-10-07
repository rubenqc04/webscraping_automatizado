"""Cumplimiento y cortesía de crawling.

Este módulo implementa las tres piezas que hacen al scraper un "buen
ciudadano" de la web:

1. RobotsPolicy   -> lee y respeta robots.txt (Disallow + Crawl-delay).
2. DomainThrottle -> limita la frecuencia de requests por dominio.
3. BlockDetector  -> detecta captchas / muros anti-bot / rate limiting.

Filosofía de diseño: cuando un sitio dice "no" (robots.txt, captcha,
403/429 persistente), el pipeline lo REGISTRA y lo OMITE. No se
implementa ninguna técnica de evasión: además de los riesgos legales,
un crawler que respeta las reglas es más estable y mantenible.
"""

from __future__ import annotations

import re

import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

try:
    # Parser robusto (el de Scrapy): soporta comodines * y $ estilo Google,
    # que muchos sitios usan (p.ej. "Disallow: /*?*"). El robotparser de la
    # stdlib los trata como texto literal y daría permisos de más.
    from protego import Protego
except ImportError:                       # pragma: no cover
    Protego = None
    import urllib.robotparser as robotparser

log = logging.getLogger("webharvest.compliance")


# ----------------------------------------------------------------------
# robots.txt
# ----------------------------------------------------------------------
class _RobotsRules:
    """Fachada mínima sobre Protego o robotparser."""

    def __init__(self, text: str):
        if Protego is not None:
            self._rp = Protego.parse(text)
            self._proto = True
        else:
            self._rp = robotparser.RobotFileParser()
            self._rp.parse(text.splitlines())
            self._proto = False

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self._proto:
            return self._rp.can_fetch(url, user_agent)
        return self._rp.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str):
        return self._rp.crawl_delay(user_agent)


class RobotsPolicy:
    """Cachea y consulta robots.txt por dominio."""

    def __init__(self, user_agent: str, respect: bool = True, default_delay: float = 2.0):
        self.user_agent = user_agent
        self.respect = respect
        self.default_delay = default_delay
        self._parsers: dict[str, _RobotsRules] = {}
        self._lock = threading.Lock()

    def _get_parser(self, url: str) -> _RobotsRules:
        netloc = urlparse(url).netloc
        with self._lock:
            if netloc in self._parsers:
                return self._parsers[netloc]
        robots_url = f"{urlparse(url).scheme}://{netloc}/robots.txt"
        text = ""
        try:
            resp = httpx.get(robots_url, timeout=10,
                             headers={"User-Agent": self.user_agent},
                             follow_redirects=True)
            if resp.status_code < 400:
                text = resp.text          # >=400: sin robots.txt => todo permitido
        except Exception as exc:      # red caída, DNS, etc.
            log.warning("No se pudo leer robots.txt de %s (%s); se asume permitido", netloc, exc)
        rules = _RobotsRules(text)
        with self._lock:
            self._parsers[netloc] = rules
        return rules

    def can_fetch(self, url: str) -> bool:
        if not self.respect:
            return True
        rp = self._get_parser(url)
        allowed = rp.can_fetch(self.user_agent, url) if rp else True
        if not allowed:
            log.info("robots.txt prohíbe %s -> se omite", url)
        return allowed

    def crawl_delay(self, url: str) -> float:
        """Delay declarado en robots.txt o el default de configuración."""
        rp = self._get_parser(url)
        try:
            delay = rp.crawl_delay(self.user_agent) if rp else None
        except Exception:
            delay = None
        return float(delay) if delay else self.default_delay


# ----------------------------------------------------------------------
# Rate limiting por dominio
# ----------------------------------------------------------------------
@dataclass
class _DomainState:
    last_request: float = 0.0
    request_count: int = 0
    blocked: bool = False
    block_reason: str = ""


class DomainThrottle:
    """Espaciado de requests y presupuesto por dominio."""

    def __init__(self, robots: RobotsPolicy, max_requests_per_domain: int = 500):
        self.robots = robots
        self.max_requests = max_requests_per_domain
        self._domains: dict[str, _DomainState] = {}
        self._lock = threading.Lock()

    def _state(self, domain: str) -> _DomainState:
        with self._lock:
            return self._domains.setdefault(domain, _DomainState())

    def wait_turn(self, url: str) -> bool:
        """Bloquea hasta que sea educado pedir la URL.

        Devuelve False si el dominio está agotado o marcado como bloqueado.
        """
        domain = urlparse(url).netloc
        st = self._state(domain)
        if st.blocked:
            return False
        if st.request_count >= self.max_requests:
            log.info("Presupuesto de requests agotado para %s", domain)
            return False

        delay = self.robots.crawl_delay(url)
        elapsed = time.monotonic() - st.last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)
        st.last_request = time.monotonic()
        st.request_count += 1
        return True

    def mark_blocked(self, url: str, reason: str) -> None:
        domain = urlparse(url).netloc
        st = self._state(domain)
        st.blocked = True
        st.block_reason = reason
        log.warning("Dominio %s marcado como bloqueado: %s", domain, reason)

    def blocked_domains(self) -> dict[str, str]:
        return {d: s.block_reason for d, s in self._domains.items() if s.blocked}


# ----------------------------------------------------------------------
# Detección de captchas y muros anti-bot
# ----------------------------------------------------------------------
# Marcadores técnicos (ids/hosts de proveedores de desafíos) y frases
# humanas. Las frases se comparan como PALABRAS COMPLETAS: "eres humano"
# aparecía dentro de "seres humanos" en un artículo de 577 palabras sobre
# paleontología, y esa única coincidencia marcó el dominio entero como
# bloqueado: 79 URLs sin visitar (hallazgo del benchmark de cobertura).
CAPTCHA_MARKERS = [
    "g-recaptcha", "recaptcha/api", "h-captcha", "hcaptcha.com",
    "cf-challenge", "cf-turnstile", "challenge-platform",
    "are you a robot", "verify you are human", "unusual traffic",
    "px-captcha", "datadome", "geo.captcha-delivery.com",
    "no eres un robot", "no soy un robot", "eres humano",
    "verificación de seguridad", "não é um robô",
    "_incapsula_", "distil_r_captcha",
]
_MARKER_RE = re.compile(
    "|".join(r"(?<![\w-])" + re.escape(m) + r"(?![\w-])" for m in CAPTCHA_MARKERS))

# Una página de desafío es CORTA: un formulario y una frase. Un artículo
# largo que trae un widget reCAPTCHA en su formulario de comentarios o de
# contacto no es un muro, es una página normal; sin este tope cualquier
# sitio con un formulario protegido quedaba "bloqueado" a la primera.
CHALLENGE_MAX_WORDS = 250

_TAG_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>|<[^>]+>", re.S | re.I)


def visible_word_count(html: str) -> int:
    return len(_TAG_RE.sub(" ", html).split())


BLOCK_STATUS = {401, 403, 407, 429, 503}


@dataclass
class BlockCheck:
    is_blocked: bool
    kind: str = ""          # "captcha" | "http_block" | ""
    detail: str = ""


class BlockDetector:
    """Inspecciona respuestas para saber si el sitio nos está desafiando."""

    @staticmethod
    def check(status_code: int | None, html: str | None) -> BlockCheck:
        if status_code in BLOCK_STATUS:
            return BlockCheck(True, "http_block", f"HTTP {status_code}")
        if html:
            low = html[:20000].lower()   # los desafíos aparecen al inicio
            m = _MARKER_RE.search(low)
            if m and visible_word_count(html) <= CHALLENGE_MAX_WORDS:
                return BlockCheck(True, "captcha", f"marcador '{m.group(0)}'")
        return BlockCheck(False)
