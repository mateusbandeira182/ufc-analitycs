"""Escrita do registro de predições e orquestração da predição de um card.

Primeira escrita do repositório fora de ``ingestion/``: a Slice 07 da SPEC 007 (M6,
RF-10) dá memória às predições para que a curva de refino do walk-forward exista como
dado. A gravação é idempotente na chave natural ``(bout_id, model_version)``, seguindo o
mesmo upsert nativo do Postgres já usado em ``ingestion.features.materialize``.

O acerto **não** é gravado aqui: ele deriva-se de ``bouts.winner_id`` sob demanda, em
``apps.predictions.selectors.get_prediction_outcomes``.

A Slice 08 acrescenta ``predict_event_card``: orquestra a predição de todas as lutas de um
evento em **duas** execuções da pipeline (uma por ordem de canto, não duas por luta) e grava
cada predição com ``source="api"``. A neutralização de canto vive aqui, e não em
``analysis.predict``, porque é decisão de apresentação da API (o modelo cru aprendeu a
vantagem do canto vermelho); o router segue fino.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from analysis.predict import CardMatchupPrediction, predict_card
from apps.bouts.enums import Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.selectors import list_event_bouts
from apps.predictions.models import BoutPrediction

# Origens do registro: predição servida por request e predição do loop walk-forward.
SOURCE_API = "api"
SOURCE_WALK_FORWARD = "walk_forward"


def record_prediction(
    session: Session,
    *,
    bout_id: int,
    predicted_winner_id: int | None,
    prob_predicted_winner: float,
    model_version: str,
    n_features: int,
    source: str,
) -> int:
    """Grava a predição de uma luta por ``(bout_id, model_version)``; devolve o id da linha.

    Idempotente na chave natural: reexecutar com a mesma versão de modelo **atualiza** a
    linha existente (mesmo id, contagem inalterada) em vez de duplicar. Versão de modelo
    diferente gera linha nova -- é isso que forma a série temporal do walk-forward.

    ``model_version`` e ``source`` são parâmetros, não derivados aqui: o walk-forward usa
    uma versão determinística e **não tem artefato persistido** para consultar, enquanto o
    serving via API usa o ``trained_at`` cru de ``analysis.model.LoadedModel``. Passar o
    valor de ``trained_at`` **sem reformatar** é o que preserva a idempotência.

    ``predicted_at`` é carimbado aqui (``datetime.now(UTC)``), não recebido: o chamador não
    tem como passar um instante naive. ``DO UPDATE`` (e não ``DO NOTHING``) porque o
    re-treino é reexecutável por RNF e a linha deve refletir a última afirmação daquele
    modelo sobre aquela luta -- além de ``DO NOTHING`` não devolver linha no ``RETURNING``,
    o que apagaria a prova de idempotência pelo id.

    O ``DO UPDATE`` atualiza **só** o que é a predição recomputada (vencedor previsto,
    probabilidade, contagem de features). ``predicted_at`` e ``source`` são deliberadamente
    preservados da primeira gravação: o instante é o eixo da série temporal que a tabela
    existe para guardar (a curva walk-forward ordena por ele) e a origem diz de onde a
    predição veio. Refrescá-los faria cada reexecução -- inclusive cada request de
    ``GET /api/v1/predict/event/{event_id}``, que grava -- envelhecer o registro histórico
    para a frente. Com eles fixos, regravar é idempotente de verdade: sem consequência
    observável quando a predição não muda.

    Não faz commit: a transação é do chamador.
    """
    stmt = insert(BoutPrediction).values(
        bout_id=bout_id,
        predicted_winner_id=predicted_winner_id,
        prob_predicted_winner=prob_predicted_winner,
        model_version=model_version,
        n_features=n_features,
        predicted_at=datetime.now(UTC),
        source=source,
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=["bout_id", "model_version"],
        set_={
            "predicted_winner_id": stmt.excluded.predicted_winner_id,
            "prob_predicted_winner": stmt.excluded.prob_predicted_winner,
            "n_features": stmt.excluded.n_features,
        },
    ).returning(BoutPrediction.id)
    # ``scalar_one()`` devolve ``Any`` (fronteira dinâmica do Core): estreitado na borda.
    prediction_id = int(session.execute(upsert).scalar_one())
    session.flush()
    return prediction_id


@dataclass(frozen=True)
class BoutCardPrediction:
    """Predição neutra de canto de uma luta do card, ou o motivo de ela não ter sido predita.

    ``red``/``blue`` são os cantos já resolvidos (com a identidade do lutador eager-loaded),
    ``None`` quando a luta não tem os dois cadastrados. Probabilidades e vencedor previsto são
    ``None`` sempre que ``unavailable_reason`` está preenchido -- e vice-versa.
    """

    bout_id: int
    red: BoutFighter | None
    blue: BoutFighter | None
    prob_red_wins: float | None
    prob_blue_wins: float | None
    predicted_winner_id: int | None
    unavailable_reason: str | None


@dataclass(frozen=True)
class EventCardPrediction:
    """Card completo de um evento predito, com a identidade do modelo que o produziu."""

    event_id: int
    model_version: str
    n_features: int
    bouts: list[BoutCardPrediction]


def _corners(bout: Bout) -> tuple[BoutFighter, BoutFighter] | None:
    """Devolve ``(red, blue)`` da luta, ou ``None`` se ela não tem exatamente os dois cantos.

    Card mal formado (canto faltando ou duplicado) é estado de **uma** luta, não erro do card:
    quem chama marca a luta como indisponível e segue predizendo as demais.
    """
    red = [bf for bf in bout.bout_fighters if bf.corner == Corner.RED]
    blue = [bf for bf in bout.bout_fighters if bf.corner == Corner.BLUE]
    if len(red) != 1 or len(blue) != 1:
        return None
    return red[0], blue[0]


def _repeated_fighter_ids(corners_por_luta: dict[int, tuple[BoutFighter, BoutFighter]]) -> set[int]:
    """Lutadores escalados em mais de uma luta do mesmo card (dado sujo).

    Não é hipótese teórica: a predição em lote exige uma única luta sintética por lutador
    (``analysis.predict.predict_card``), então as lutas envolvidas ficam fora do lote em vez de
    derrubá-lo.
    """
    ocorrencias = Counter(
        bout_fighter.fighter_id for cantos in corners_por_luta.values() for bout_fighter in cantos
    )
    return {fighter_id for fighter_id, total in ocorrencias.items() if total > 1}


def _neutral_prob_red_wins(forward_prob_a_wins: float, reverse_prob_a_wins: float) -> float:
    """Probabilidade neutra de o canto vermelho vencer: média das duas ordens de canto.

    ``forward`` avalia o vermelho como A; ``reverse`` inverte o par, então ``prob_a_wins`` de
    lá é a probabilidade do **azul** -- daí o complemento. A média cancela a vantagem de canto
    que o modelo aprendeu (ADR 0001), tornando o palpite invariante à ordem.
    """
    return (forward_prob_a_wins + (1.0 - reverse_prob_a_wins)) / 2.0


def predicted_winner_id(fighter_a_id: int, fighter_b_id: int, prob_a_wins: float) -> int:
    """Vencedor previsto do par: função pura do par, não da ordem dos parâmetros.

    Maior probabilidade vence; no empate exato desempata pelo menor id, para que (A, B) e
    (B, A) elejam sempre o mesmo lutador -- coerente com a neutralidade de canto. Compartilhada
    pelo matchup isolado e pelo card justamente para os dois nunca divergirem sobre o mesmo par.
    """
    prob_b_wins = 1.0 - prob_a_wins
    if prob_a_wins > prob_b_wins:
        return fighter_a_id
    if prob_b_wins > prob_a_wins:
        return fighter_b_id
    return min(fighter_a_id, fighter_b_id)


def _missing_history_reason(fighter_ids: tuple[int, ...]) -> str:
    """Motivo da indisponibilidade quando falta histórico no granular a um dos lutadores."""
    ids = ", ".join(str(fighter_id) for fighter_id in fighter_ids)
    return (
        f"Sem histórico de lutas no granular para o(s) lutador(es) de id {ids}; "
        f"sem lutas passadas não há features as-of para predizer."
    )


def predict_event_card(session: Session, event_id: int, directory: Path) -> EventCardPrediction:
    """Prediz todas as lutas do card de um evento, neutro de canto.

    Reusa ``list_event_bouts`` (cantos e identidade já eager-loaded, sem N+1) e chama
    ``predict_card`` **duas vezes** -- uma por ordem de canto -- em vez de duas vezes por luta:
    2 execuções da pipeline por card, não 2 por luta. As duas ordens continuam sendo passadas
    separadas porque cada lutador só pode ter uma luta sintética por passada.

    Lutas impredizíveis (card mal formado, lutador escalado duas vezes, lutador sem histórico)
    saem com ``unavailable_reason`` e **não** derrubam as demais -- aqui a ausência é estado de
    um item, não erro da requisição. Propaga ``FileNotFoundError`` (artefato ausente) para o
    router traduzir em 503.

    Cada luta predita é registrada em ``bout_predictions`` com ``source="api"``, idempotente por
    ``(bout_id, model_version)``, e a transação é commitada (ver ``_record_card``). O
    ``source="api"`` distingue estas linhas das do walk-forward: prever por aqui um evento **já
    ocorrido** usa features as-of agora, com a própria luta já no granular -- é um palpite, não
    medida retrospectiva de acerto.
    """
    bouts = list_event_bouts(session, event_id)
    cantos_por_luta = {bout.id: cantos for bout in bouts if (cantos := _corners(bout)) is not None}
    repetidos = _repeated_fighter_ids(cantos_por_luta)

    elegiveis = [
        bout_id
        for bout_id, (red, blue) in cantos_por_luta.items()
        if not ({red.fighter_id, blue.fighter_id} & repetidos)
    ]
    pares = [
        (cantos_por_luta[bout_id][0].fighter_id, cantos_por_luta[bout_id][1].fighter_id)
        for bout_id in elegiveis
    ]
    # As duas ordens saem da MESMA lista de pares elegíveis, então casam posição a posição.
    forward = predict_card(session, pares, directory)
    reverse = predict_card(session, [(b, a) for a, b in pares], directory)
    previstos = {
        bout_id: (ida, volta)
        for bout_id, ida, volta in zip(elegiveis, forward.matchups, reverse.matchups, strict=True)
    }

    card = EventCardPrediction(
        event_id=event_id,
        model_version=forward.model_version,
        n_features=forward.n_features,
        bouts=[
            _to_bout_card_prediction(bout.id, cantos_por_luta.get(bout.id), repetidos, previstos)
            for bout in bouts
        ],
    )
    _record_card(session, card)
    return card


def _record_card(session: Session, card: EventCardPrediction) -> None:
    """Registra as lutas preditas do card e **commita** a transação.

    Só as lutas com palpite viram linha: sem vencedor previsto não há o que afirmar. A
    probabilidade gravada é a atribuída ao vencedor previsto (a maior das duas), como o
    registro espera.

    O ``commit`` é explícito porque ``mma_analytics.db.get_session`` nunca commita -- a API v1
    nasceu somente-leitura e este é o primeiro endpoint que escreve. Sem ele, o endpoint
    responderia 200 com a sessão do request descartada no teardown e nada gravado.
    """
    for bout in card.bouts:
        if (
            bout.predicted_winner_id is None
            or bout.prob_red_wins is None
            or bout.prob_blue_wins is None
        ):
            continue
        record_prediction(
            session,
            bout_id=bout.bout_id,
            predicted_winner_id=bout.predicted_winner_id,
            prob_predicted_winner=max(bout.prob_red_wins, bout.prob_blue_wins),
            model_version=card.model_version,
            n_features=card.n_features,
            source=SOURCE_API,
        )
    session.commit()


def _unavailable(
    bout_id: int, cantos: tuple[BoutFighter, BoutFighter] | None, reason: str
) -> BoutCardPrediction:
    """Entrada de card sem predição: os cantos conhecidos e o motivo, tudo mais nulo."""
    red, blue = cantos if cantos is not None else (None, None)
    return BoutCardPrediction(
        bout_id=bout_id,
        red=red,
        blue=blue,
        prob_red_wins=None,
        prob_blue_wins=None,
        predicted_winner_id=None,
        unavailable_reason=reason,
    )


def _to_bout_card_prediction(
    bout_id: int,
    cantos: tuple[BoutFighter, BoutFighter] | None,
    repetidos: set[int],
    previstos: dict[int, tuple[CardMatchupPrediction, CardMatchupPrediction]],
) -> BoutCardPrediction:
    """Monta a entrada do card: a predição neutra, ou o motivo de a luta não ter sido predita.

    Uma luta só é elegível quando tem os dois cantos e nenhum lutador repetido no card -- e
    exatamente essas estão em ``previstos``, por construção do lote.
    """
    if cantos is None:
        return _unavailable(
            bout_id,
            None,
            "Luta sem os dois cantos cadastrados (é preciso exatamente um lutador no canto "
            "vermelho e um no azul).",
        )
    red, blue = cantos
    repetidos_na_luta = sorted({red.fighter_id, blue.fighter_id} & repetidos)
    if repetidos_na_luta:
        ids = ", ".join(str(fighter_id) for fighter_id in repetidos_na_luta)
        return _unavailable(
            bout_id,
            cantos,
            f"Lutador(es) de id {ids} escalado(s) em mais de uma luta deste card; "
            f"a predição exige um lutador por luta.",
        )

    ida, volta = previstos[bout_id]
    if ida.prob_a_wins is None or volta.prob_a_wins is None:
        return _unavailable(bout_id, cantos, _missing_history_reason(ida.missing_history_ids))

    prob_red_wins = _neutral_prob_red_wins(ida.prob_a_wins, volta.prob_a_wins)
    return BoutCardPrediction(
        bout_id=bout_id,
        red=red,
        blue=blue,
        prob_red_wins=prob_red_wins,
        prob_blue_wins=1.0 - prob_red_wins,
        predicted_winner_id=predicted_winner_id(red.fighter_id, blue.fighter_id, prob_red_wins),
        unavailable_reason=None,
    )
