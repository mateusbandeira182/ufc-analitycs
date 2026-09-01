"""Schemas Pydantic de saída do app de predições.

``MatchupPredictionOut`` é o contrato do palpite neutro de canto para um confronto A vs B:
os dois lutadores resolvidos (id + nome), as probabilidades complementares e o vencedor
previsto. As probabilidades já são neutralizadas de canto (média das duas ordens), então
``prob_a_wins``/``prob_b_wins`` não dependem da ordem dos parâmetros.

``EventPredictionOut`` é o mesmo palpite aplicado ao card inteiro de um evento, uma entrada por
luta. Diferença de contrato deliberada: no card, uma luta impredizível **não** derruba a
resposta -- ela volta com probabilidades nulas e ``unavailable_reason`` preenchido, enquanto no
matchup isolado a mesma situação é erro da requisição (422).
"""

from __future__ import annotations

from pydantic import BaseModel


class MatchupFighterOut(BaseModel):
    """Lutador resolvido do banco, exposto no palpite (id + nome)."""

    id: int
    name: str


class MatchupPredictionOut(BaseModel):
    """Palpite neutro de canto para um confronto hipotético A vs B.

    ``prob_a_wins`` e ``prob_b_wins`` são complementares (somam 1) e já neutralizadas de
    canto; ``predicted_winner_id`` é o ``fighter_id`` de maior probabilidade neutra.
    """

    fighter_a: MatchupFighterOut
    fighter_b: MatchupFighterOut
    prob_a_wins: float
    prob_b_wins: float
    predicted_winner_id: int


class CardBoutPredictionOut(BaseModel):
    """Predição neutra de canto de uma luta do card, ou o motivo da ausência.

    Quando a luta não pôde ser predita, as probabilidades e o vencedor previsto são ``null`` e
    ``unavailable_reason`` explica o porquê -- a luta continua listada, para o consumidor ver o
    card inteiro. Os cantos também são ``null`` quando a luta não tem os dois cadastrados.
    """

    bout_id: int
    fighter_red: MatchupFighterOut | None
    fighter_blue: MatchupFighterOut | None
    prob_red_wins: float | None
    prob_blue_wins: float | None
    predicted_winner_id: int | None
    unavailable_reason: str | None


class EventPredictionOut(BaseModel):
    """Predição do card completo de um evento, com a identidade do modelo que a produziu.

    ``model_version`` é o ``trained_at`` do artefato treinado -- a mesma versão gravada em
    ``bout_predictions``, o que liga a resposta ao registro persistido.
    """

    event_id: int
    model_version: str
    n_features: int
    bouts: list[CardBoutPredictionOut]
