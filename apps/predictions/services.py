"""Escrita do registro de predições (``bout_predictions``).

Primeira escrita do repositório fora de ``ingestion/``: a Slice 07 da SPEC 007 (M6,
RF-10) dá memória às predições para que a curva de refino do walk-forward exista como
dado. A gravação é idempotente na chave natural ``(bout_id, model_version)``, seguindo o
mesmo upsert nativo do Postgres já usado em ``ingestion.features.materialize``.

O acerto **não** é gravado aqui: ele deriva-se de ``bouts.winner_id`` sob demanda, em
``apps.predictions.selectors.get_prediction_outcomes``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

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
            "predicted_at": stmt.excluded.predicted_at,
            "source": stmt.excluded.source,
        },
    ).returning(BoutPrediction.id)
    # ``scalar_one()`` devolve ``Any`` (fronteira dinâmica do Core): estreitado na borda.
    prediction_id = int(session.execute(upsert).scalar_one())
    session.flush()
    return prediction_id
