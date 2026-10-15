"""Enums do app de predições."""

from __future__ import annotations

from enum import StrEnum


class UnavailableCode(StrEnum):
    """Causa legível por máquina de uma previsão indisponível (RF-28 / D-08).

    A prosa de ``unavailable_reason`` continua existindo, com ids embutidos, para humano.
    O **código** é o que a interface consome: sem ele, a SPA precisaria parsear a prosa para
    escolher o aviso específico de cada causa -- o mesmo anti-padrão que a D-04 nomeia ao
    rejeitar o 422 para o estreante. A prosa é livre (texto para gente); o código é contrato.
    """

    # A luta não tem exatamente um canto vermelho e um azul (card mal formado).
    MISSING_CORNERS = "missing_corners"
    # O mesmo lutador escalado em duas lutas do card (dado sujo; quebra o batching).
    REPEATED_FIGHTER = "repeated_fighter"
    # Estreante: lutador sem histórico no granular, logo sem features as-of.
    MISSING_HISTORY = "missing_history"
