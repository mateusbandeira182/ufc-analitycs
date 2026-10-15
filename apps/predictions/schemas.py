"""Schemas Pydantic de saída do app de predições.

``MatchupPredictionOut`` é o contrato do palpite neutro de canto para um confronto A vs B:
os dois lutadores resolvidos (id + nome), as probabilidades complementares e o vencedor
previsto. As probabilidades já são neutralizadas de canto (média das duas ordens), então
``prob_a_wins``/``prob_b_wins`` não dependem da ordem dos parâmetros.

``EventPredictionOut`` é o mesmo palpite aplicado ao card inteiro de um evento, uma entrada por
luta. Diferença de contrato deliberada: no card, uma luta impredizível **não** derruba a
resposta -- ela volta com probabilidades nulas e ``unavailable_reason`` preenchido, enquanto no
matchup isolado a mesma situação é erro da requisição (422).

Desde o M9 (SPEC 011) os dois carregam ``features_freshness``: o palpite vem do cache as-of, e
quem lê o cache reporta se ele está defasado. No card o campo vive no **envelope**, não em cada
luta (D-09) -- a defasagem é do cache, não de uma luta.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from apps.features.schemas import FeaturesFreshnessOut
from apps.predictions.enums import UnavailableCode


class MatchupFighterOut(BaseModel):
    """Lutador resolvido do banco, exposto no palpite (id + nome)."""

    id: int
    name: str


class MatchupBlockInfluenceOut(BaseModel):
    """Influência de um bloco de ``BLOCK_REGISTRY`` sobre a probabilidade corrente (RF-17).

    Medida por **ablação local**: neutraliza as colunas do bloco com ``NaN`` e re-prediz nas
    duas ordens de canto. São sempre 11 entradas -- ``janela-canto-fabricado`` fica fora porque
    não é conjunto de colunas, e sim comparação de duas variantes de dataset.
    """

    name: str  # chave em BLOCK_REGISTRY
    description: str  # a descrição já registrada no bloco
    n_columns: int  # colunas do bloco que o MODELO consome
    # Contagem **real** de não-nulos na linha servida (RF-18) -- nunca uma lista fixa, que
    # apodreceria do mesmo modo que o cache ``bout_features`` apodreceu.
    n_columns_evaluated: int
    coverage: Literal["full", "partial", "none"]
    # ``prob_a_base - prob_a_ablado`` (D-11): positivo favorece A -- é dele que sai o rótulo
    # "favorece Fulano". Sinal deliberadamente OPOSTO, em semântica, ao de ``MetricsDelta``
    # (onde negativo é bom): são grandezas diferentes, perda contra atribuição de
    # probabilidade. NÃO harmonizar -- inverteria a tela sem quebrar teste.
    # ``None`` -- NUNCA 0.0 -- quando ``coverage == "none"``: a interface rotula "não
    # avaliado", porque "não avaliado" e "não influenciou" são afirmações diferentes.
    delta_prob_a: float | None


class MatchupAttributeOut(BaseModel):
    """Um atributo as-of **cru** dos dois lados: o número que forma a porcentagem (RF-19)."""

    key: str
    label: str
    # Chave ausente ou nula no payload as-of -> ``null``, nunca ``0.0``: zero é um valor
    # legítimo de taxa, e usá-lo para "não tenho" faria a tela afirmar uma medição inexistente.
    value_a: float | None
    value_b: float | None


class MatchupPredictionOut(BaseModel):
    """Palpite neutro de canto para um confronto hipotético A vs B, com a explicabilidade.

    ``prob_a_wins`` e ``prob_b_wins`` são complementares (somam 1) e já neutralizadas de
    canto; ``predicted_winner_id`` é o ``fighter_id`` de maior probabilidade neutra.

    **Os três passaram a nulável no M9** (D-04): lutador sem histórico responde 200 com
    ``unavailable_reason`` + ``unavailable_code``, não mais 422. A página de confronto é
    alcançada por um clique vindo do card de um evento futuro -- o usuário não formulou
    requisição errada, clicou numa luta que vai acontecer.

    No estado indisponível, ``blocks`` e ``attributes`` vêm **vazios**, nunca listas de nulos
    (DP-05e): lista vazia diz "não foi produzido", uma lista de nulos diria "foi medido e deu
    ausente".
    """

    fighter_a: MatchupFighterOut
    fighter_b: MatchupFighterOut
    prob_a_wins: float | None
    prob_b_wins: float | None
    predicted_winner_id: int | None
    unavailable_reason: str | None  # prosa, para humano, com ids embutidos
    unavailable_code: UnavailableCode | None  # para a interface decidir (D-08)
    blocks: list[MatchupBlockInfluenceOut]
    attributes: list[MatchupAttributeOut]
    features_freshness: FeaturesFreshnessOut


class CardBoutPredictionOut(BaseModel):
    """Predição neutra de canto de uma luta do card, ou o motivo da ausência.

    Quando a luta não pôde ser predita, as probabilidades e o vencedor previsto são ``null`` e
    ``unavailable_reason`` explica o porquê -- a luta continua listada, para o consumidor ver o
    card inteiro. Os cantos também são ``null`` quando a luta não tem os dois cadastrados.

    O motivo tem **duas faces** desde o M9 (RF-28 / D-08): ``unavailable_reason`` é prosa para
    humano e ``unavailable_code`` é o enum que a interface consome. As duas causas exclusivas do
    card (``missing_corners`` e ``repeated_fighter``) nascem aqui, e só aqui.
    """

    bout_id: int
    fighter_red: MatchupFighterOut | None
    fighter_blue: MatchupFighterOut | None
    prob_red_wins: float | None
    prob_blue_wins: float | None
    predicted_winner_id: int | None
    unavailable_reason: str | None
    unavailable_code: UnavailableCode | None


class EventPredictionOut(BaseModel):
    """Predição do card completo de um evento, com a identidade do modelo que a produziu.

    ``model_version`` é o ``trained_at`` do artefato treinado -- a mesma versão gravada em
    ``bout_predictions``, o que liga a resposta ao registro persistido.
    """

    event_id: int
    model_version: str
    n_features: int
    bouts: list[CardBoutPredictionOut]
    features_freshness: FeaturesFreshnessOut
