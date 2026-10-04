"""Search result parsers.

Every engine Muninn can talk to has an entry here, whether or not it is currently
in the rotation: ``PARSER_REGISTRY`` is what is *implemented*,
:data:`app.config.SUPPORTED_ENGINES` is what is *enabled*. The two differ
deliberately - Yandex, Qwant and Ecosia are implemented and tested but parked,
because each answered every live probe with a wall rather than results. Enabling
one is a single edit to that tuple.
"""

from __future__ import annotations

import logging

from drivers.parsers.base import BaseParser, EngineBlockedError
from drivers.parsers.bing import BingParser
from drivers.parsers.brave import BraveParser
from drivers.parsers.ddg import DuckDuckGoParser
from drivers.parsers.ecosia import EcosiaParser
from drivers.parsers.google import GoogleParser
from drivers.parsers.mojeek import MojeekParser
from drivers.parsers.qwant import QwantParser
from drivers.parsers.yahoo import YahooParser
from drivers.parsers.yandex import YandexParser

logger = logging.getLogger(__name__)

__all__ = [
    "BaseParser",
    "EngineBlockedError",
    "GoogleParser",
    "BingParser",
    "BraveParser",
    "EcosiaParser",
    "DuckDuckGoParser",
    "MojeekParser",
    "QwantParser",
    "YandexParser",
    "YahooParser",
]

# Engine name -> parser class registry (matches app.config.SUPPORTED_ENGINES).
PARSER_REGISTRY: dict[str, type[BaseParser]] = {
    GoogleParser.ENGINE_NAME: GoogleParser,
    BingParser.ENGINE_NAME: BingParser,
    BraveParser.ENGINE_NAME: BraveParser,
    DuckDuckGoParser.ENGINE_NAME: DuckDuckGoParser,
    MojeekParser.ENGINE_NAME: MojeekParser,
    YandexParser.ENGINE_NAME: YandexParser,
    QwantParser.ENGINE_NAME: QwantParser,
    EcosiaParser.ENGINE_NAME: EcosiaParser,
    YahooParser.ENGINE_NAME: YahooParser,
}


def apply_search_region(region: str) -> str:
    """Set the market every parser requests from ``Settings.search_region``.

    Engines name the parameter differently, and some have none that works (see
    the notes in each parser), so this sets one value they each interpret in
    their own URL. Returns the normalised code actually applied, and refuses
    anything that is not a two-letter country code rather than sending a
    nonsense market to six engines.

    Idempotent, and safe to call before the app serves traffic.
    """
    code = (region or "").strip().lower()
    if len(code) != 2 or not code.isalpha():
        raise ValueError(
            f"SEARCH_REGION must be a two-letter country code, got {region!r}"
        )
    for parser in PARSER_REGISTRY.values():
        parser.REGION = code
    logger.info("search region set to %s", code.upper())
    return code


def get_parser(engine: str) -> type[BaseParser]:
    """Return the parser for ``engine`` or raise ``KeyError``."""
    try:
        return PARSER_REGISTRY[engine]
    except KeyError:
        raise KeyError(f"unsupported engine: {engine!r}") from None