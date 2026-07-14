"""Schemas Pydantic de saída do app de predições.

``MatchupPredictionOut`` é o contrato do palpite neutro de canto para um confronto A vs B:
os dois lutadores resolvidos (id + nome), as probabilidades complementares e o vencedor
previsto. As probabilidades já são neutralizadas de canto no router (média das duas ordens),
então ``prob_a_wins``/``prob_b_wins`` não dependem da ordem dos parâmetros.
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
