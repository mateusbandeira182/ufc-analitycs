"""Parsers puros das strings da Cito API (borda dinâmica -> tipos do domínio).

A Cito expressa as estatísticas de golpe como ``"landed of attempted"`` (ex.: ``"66 of 125"``)
e o tempo de controle como ``"m:ss"`` (ex.: ``"7:13"``). Estas funções convertem essas strings
nos tipos do domínio na entrada, com uma distinção inegociável:

- **Ausência** (``None``/``""``) degrada de forma explícita (``(None, None)`` / ``None``) --
  não se inventa zero para dado faltante.
- **Formato inesperado** levanta ``CitoParseError`` -- nunca silencia nem propaga ``Any``.

Módulo sem dependência de ``client.py``/``dto.py`` para evitar ciclo de import (é ``dto.py`` que
importa daqui). Por isso ``CitoParseError`` é subclasse de ``ValueError``, não de ``CitoError``.
"""

from __future__ import annotations

import re

_STAT_PATTERN = re.compile(r"^(\d+) of (\d+)$")
_CLOCK_PATTERN = re.compile(r"^(\d+):([0-5]\d)$")
_CLOCK_MISSING = frozenset({"", "--"})


class CitoParseError(ValueError):
    """String de estatística/tempo da Cito em formato inesperado.

    Subclasse de ``ValueError`` (e não de ``CitoError``) para manter ``parsers.py`` livre de
    import de ``client.py`` -- não silenciar nem inventar valor para um formato desconhecido.
    """


def parse_stat(raw: str | None) -> tuple[int | None, int | None]:
    """Converte ``"66 of 125"`` em ``(66, 125)`` (landed, attempted).

    Ausência (``None`` ou string vazia/só espaços) degrada para ``(None, None)`` -- não se
    inventa zero. Formato diferente de ``"N of M"`` (inteiros) levanta ``CitoParseError``.
    """
    if raw is None:
        return (None, None)
    text = raw.strip()
    if not text:
        return (None, None)
    match = _STAT_PATTERN.match(text)
    if match is None:
        raise CitoParseError(
            f"Estatística da Cito em formato inesperado: {raw!r} (esperado 'N of M')."
        )
    return (int(match.group(1)), int(match.group(2)))


def parse_clock(raw: str | None) -> int | None:
    """Converte ``"7:13"`` em ``433`` segundos (``minutos*60 + segundos``).

    Ausência (``None``, string vazia/só espaços ou o marcador ``"--"``) degrada para ``None``.
    Formato diferente de ``"m:ss"`` com ``0 <= ss < 60`` levanta ``CitoParseError``.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text in _CLOCK_MISSING:
        return None
    match = _CLOCK_PATTERN.match(text)
    if match is None:
        raise CitoParseError(f"Tempo da Cito em formato inesperado: {raw!r} (esperado 'm:ss').")
    return int(match.group(1)) * 60 + int(match.group(2))
