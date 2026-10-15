"""Testes de API dos endpoints de predição (``/predict/matchup`` e ``/predict/event/{id}``).

Cobrem o router fino de predições: valida a entrada, resolve os lutadores do banco, chama o
serving nas duas ordens de canto e devolve o palpite **neutro de canto**. No matchup, as
asserções-chave são a neutralidade (trocar ``fighter_a`` e ``fighter_b`` dá o mesmo resultado,
com os lados espelhados) e o contrato de erro (404 lutador inexistente, 422 mesmo lutador ou
sem histórico, 503 artefato ausente). "Sem histórico" tem dois cenários distintos e ambos
respondem 422: lutador sem nenhuma aparição no granular e lutador cuja **única** luta cadastrada
é futura -- o segundo é o que discrimina o recorte por data da Slice 08.

No card de evento, o que se afirma é a consistência com o matchup para o mesmo par (o batching
não muda o palpite) e o comportamento **por luta**: uma luta impredizível volta com
``unavailable_reason`` e status 200, sem derrubar as demais -- diferente do matchup, onde a
mesma situação é 422 porque é erro da requisição. Contrato de erro do card: 404 evento
inexistente, 503 artefato ausente, 200 com ``bouts`` vazio para evento sem lutas.

Estratégia de teste (Postgres transacional, sem tocar quota externa): reusa o histórico
sintético e o treino do teste de serving (``tests.analysis.test_predict``), persistindo um
modelo pequeno num ``tmp_path``. O diretório do artefato é injetado no endpoint sobrepondo a
dependência ``get_artifacts_dir``, e a ``Session`` é a sessão transacional da fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from analysis.ablation import BLOCK_REGISTRY, WINDOW_BLOCK_NAME
from analysis.model import load_artifact
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.events.selectors import list_event_bouts
from apps.features.models import FighterAsOfFeatures
from apps.features.selectors import read_asof_features
from apps.predictions.api import get_artifacts_dir
from apps.predictions.services import MATCHUP_ATTRIBUTES
from ingestion.features.cli import run_materialize_asof
from mma_analytics.app import create_app
from mma_analytics.db import get_session
from tests.analysis.test_predict import (
    _make_fighter,
    _seed_future_bout,
    _seed_history,
    _train_and_persist,
)


@contextmanager
def _client(db_session: Session, artifacts_dir: Path) -> Iterator[TestClient]:
    """``TestClient`` com ``get_session`` e ``get_artifacts_dir`` sobrepostos.

    A sessão aponta para a transação do teste; o diretório do artefato aponta para o
    ``tmp_path`` onde o modelo pequeno foi persistido (ou um diretório vazio, no caso 503).
    Context manager: o ``with`` garante que o ``TestClient`` fecha e que os
    ``dependency_overrides`` são limpos ao fim de cada teste (sem vazamento entre casos).
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_artifacts_dir] = lambda: artifacts_dir
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_matchup_devolve_probabilidades_complementares(db_session: Session, tmp_path: Path) -> None:
    """Confronto válido responde 200 com probabilidades somando ~1.0 e vencedor coerente."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["fighter_a"] == {"id": ids["Fighter Alpha"], "name": "Fighter Alpha"}
    assert body["fighter_b"] == {"id": ids["Fighter Delta"], "name": "Fighter Delta"}
    assert 0.0 <= body["prob_a_wins"] <= 1.0
    assert 0.0 <= body["prob_b_wins"] <= 1.0
    assert body["prob_a_wins"] + body["prob_b_wins"] == pytest.approx(1.0)
    esperado = (
        ids["Fighter Alpha"] if body["prob_a_wins"] >= body["prob_b_wins"] else ids["Fighter Delta"]
    )
    assert body["predicted_winner_id"] == esperado


def test_matchup_e_neutro_de_canto(db_session: Session, tmp_path: Path) -> None:
    """Trocar a ordem dos parâmetros dá o MESMO resultado (palpite neutro de canto).

    Asserção-chave: a probabilidade de cada lutador vencer independe de ele ter sido passado
    como ``fighter_a`` ou ``fighter_b``. O modelo cru é sensível ao canto; a média das duas
    ordens neutraliza essa vantagem, então (A, B) e (B, A) elegem o mesmo vencedor previsto.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    a, b = ids["Fighter Alpha"], ids["Fighter Delta"]

    with _client(db_session, tmp_path) as client:
        ab = client.get("/api/v1/predict/matchup", params={"fighter_a": a, "fighter_b": b}).json()
        ba = client.get("/api/v1/predict/matchup", params={"fighter_a": b, "fighter_b": a}).json()

    # A probabilidade de A vencer é a mesma, esteja A no lado a (ab) ou no lado b (ba).
    assert ab["prob_a_wins"] == pytest.approx(ba["prob_b_wins"])
    assert ab["prob_b_wins"] == pytest.approx(ba["prob_a_wins"])
    # O vencedor previsto é idêntico -- a ordem dos parâmetros não muda o palpite.
    assert ab["predicted_winner_id"] == ba["predicted_winner_id"]


def test_matchup_lutador_inexistente_responde_404(db_session: Session, tmp_path: Path) -> None:
    """Um dos ids não corresponde a nenhum lutador -> 404."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    inexistente = max(ids.values()) + 10_000

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": inexistente},
        )

    assert resp.status_code == 404


def test_matchup_mesmo_lutador_responde_422(db_session: Session, tmp_path: Path) -> None:
    """Mesmo lutador dos dois lados -> 422 (o confronto exige lutadores distintos)."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    alpha = ids["Fighter Alpha"]

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup", params={"fighter_a": alpha, "fighter_b": alpha}
        )

    assert resp.status_code == 422


def test_matchup_mesmo_lutador_tem_precedencia_sobre_inexistencia(
    db_session: Session, tmp_path: Path
) -> None:
    """Dois ids iguais e inexistentes ainda respondem 422 (validado antes da existência)."""
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup", params={"fighter_a": 999_999, "fighter_b": 999_999}
        )

    assert resp.status_code == 422


def test_matchup_artefato_ausente_responde_503(db_session: Session, tmp_path: Path) -> None:
    """Sem artefato de modelo treinado -> 503 (mensagem clara), nunca 500 cru."""
    ids = _seed_history(db_session)
    # Não treina: tmp_path fica sem o artefato joblib.

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        )

    assert resp.status_code == 503
    assert "modelo" in resp.json()["detail"].lower()


def test_matchup_lutador_sem_historico_responde_200_indisponivel(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-14 / D-04: estreante -> **200** com probabilidades nulas e ``missing_history``.

    **Mudança de contrato deliberada** (não regressão). Até o M9 isto respondia 422, e a
    divergência em relação ao card era proposital ("no matchup é erro da própria requisição").
    Ela deixa de fazer sentido quando a página de confronto passa a ser alcançada por um clique
    vindo do card de um evento futuro: o usuário não formulou requisição errada nenhuma, clicou
    numa luta que vai acontecer. Manter o 422 forçaria a SPA a parsear a mensagem de erro para
    distinguir "estreante" de falha real.

    ``blocks`` e ``attributes`` saem **vazios**, nunca listas de nulos: lista vazia diz "não foi
    produzido", uma lista de nulos diria "foi medido e deu ausente" (DP-05e). A ablação exige os
    dois lados e os atributos são comparação lado a lado -- sem um dos lados não há o que medir.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()
    orfao = int(sem_historico.id)

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": orfao},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["prob_a_wins"] is None
    assert body["prob_b_wins"] is None
    assert body["predicted_winner_id"] is None
    assert body["unavailable_code"] == "missing_history"
    # A prosa segue existindo, para humano, com o id embutido.
    assert str(orfao) in body["unavailable_reason"]
    assert body["blocks"] == []
    assert body["attributes"] == []
    # Os dois lados continuam resolvidos: a tela precisa dos nomes para montar o cabeçalho.
    assert body["fighter_a"]["id"] == ids["Fighter Alpha"]
    assert body["fighter_b"]["id"] == orfao
    # O diagnóstico de defasagem acompanha a resposta indisponível: é o que separa "estreante"
    # de "ninguém rodou a materialização".
    assert set(body["features_freshness"]) == _CAMPOS_FRESHNESS


def test_matchup_lutador_so_com_luta_futura_responde_200_indisponivel(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-14: lutador cuja ÚNICA luta é **futura** -> 200 indisponível, não palpite fabricado.

    O caso que discrimina o recorte por data: a luta do card já está no granular, mas ainda não
    aconteceu -- não há box-score nenhum a extrair dela. Sem o recorte, o estreante passaria na
    checagem de histórico pela própria luta que se quer prever, e a resposta traria 0,5
    fabricado sobre um vetor inteiramente ``NaN``.

    Mesmo contrato do caso anterior, e por isso mesmo o par de testes vale: um lutador sem
    nenhuma aparição e um com aparição **só no futuro** chegam ao mesmo estado, por caminhos
    diferentes. O 422 de antes virou 200 pela D-04 -- a luta futura é exatamente o clique que
    motivou a mudança.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    estreante = _make_fighter("Fighter Estreante", 195)
    db_session.add(estreante)
    db_session.flush()
    estreante_id = int(estreante.id)
    _seed_future_bout(db_session, ids["Fighter Alpha"], estreante_id)

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": estreante_id},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["prob_a_wins"] is None
    assert body["unavailable_code"] == "missing_history"
    assert str(estreante_id) in body["unavailable_reason"]
    assert body["blocks"] == []


def _seed_card(db_session: Session, ids: dict[str, int], pares: list[tuple[str, str]]) -> int:
    """Semeia um evento cujo card tem uma luta por par (nomes do roster) e devolve o event_id.

    As lutas ainda não têm vencedor nem box-score (``winner_id`` e as estatísticas nulas): é um
    card **futuro**, que é o que o endpoint de predição de evento existe para servir. O
    ``method`` é preenchido porque a coluna é NOT NULL no schema atual (que nasceu para
    eventos já ocorridos) -- é placeholder, nada no endpoint o consome.
    """
    event = Event(name="UFC Card Futuro", date=date(2026, 12, 31), location=None, source="kaggle")
    db_session.add(event)
    db_session.flush()
    for nome_red, nome_blue in pares:
        bout = Bout(
            event_id=event.id,
            winner_id=None,
            method=BoutMethod.DECISION,
            round=None,
            ending_time_seconds=None,
            weight_class=None,
            source="kaggle",
        )
        db_session.add(bout)
        db_session.flush()
        db_session.add_all(
            [
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=ids[nome_red],
                    corner=Corner.RED,
                    source="kaggle",
                ),
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=ids[nome_blue],
                    corner=Corner.BLUE,
                    source="kaggle",
                ),
            ]
        )
        db_session.flush()
    return int(event.id)


def test_event_preve_todas_as_lutas_do_card(db_session: Session, tmp_path: Path) -> None:
    """Card completo responde 200 com uma entrada por luta, na ordem determinística do card."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session, ids, [("Fighter Alpha", "Fighter Delta"), ("Fighter Bravo", "Fighter Charlie")]
    )
    esperado = list_event_bouts(db_session, event_id)

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    body = resp.json()
    artefato = load_artifact(tmp_path)
    assert body["event_id"] == event_id
    assert body["model_version"] == artefato.trained_at
    assert body["n_features"] == len(artefato.feature_names)
    assert [item["bout_id"] for item in body["bouts"]] == [bout.id for bout in esperado]
    for item in body["bouts"]:
        assert item["unavailable_reason"] is None
        assert 0.0 <= item["prob_red_wins"] <= 1.0
        assert item["prob_red_wins"] + item["prob_blue_wins"] == pytest.approx(1.0)
        assert item["predicted_winner_id"] in {
            item["fighter_red"]["id"],
            item["fighter_blue"]["id"],
        }


def test_event_bate_com_o_matchup_do_mesmo_par(db_session: Session, tmp_path: Path) -> None:
    """A predição do card é a MESMA do endpoint de matchup para o par -- neutra de canto.

    Prova que o batching (uma passada por ordem de canto para o card inteiro) não muda o
    resultado e que os dois caminhos elegem o mesmo vencedor previsto.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session, ids, [("Fighter Alpha", "Fighter Delta"), ("Fighter Bravo", "Fighter Charlie")]
    )

    with _client(db_session, tmp_path) as client:
        card = client.get(f"/api/v1/predict/event/{event_id}").json()
        matchup = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        ).json()

    luta = next(item for item in card["bouts"] if item["fighter_red"]["id"] == ids["Fighter Alpha"])
    assert luta["prob_red_wins"] == pytest.approx(matchup["prob_a_wins"])
    assert luta["prob_blue_wins"] == pytest.approx(matchup["prob_b_wins"])
    assert luta["predicted_winner_id"] == matchup["predicted_winner_id"]


def test_openapi_inclui_path_de_predicao_de_evento(client: TestClient) -> None:
    """O contrato do card entra no OpenAPI (e, por tabela, nos tipos gerados no web)."""
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/predict/event/{event_id}" in paths


def test_event_inexistente_responde_404(db_session: Session, tmp_path: Path) -> None:
    """Id que não corresponde a nenhum evento -> 404, antes de qualquer predição."""
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        resp = client.get("/api/v1/predict/event/999999")

    assert resp.status_code == 404


def test_event_artefato_ausente_responde_503(db_session: Session, tmp_path: Path) -> None:
    """Sem artefato de modelo treinado -> 503 (mensagem clara), nunca 500 cru."""
    ids = _seed_history(db_session)
    event_id = _seed_card(db_session, ids, [("Fighter Alpha", "Fighter Delta")])
    # Não treina: tmp_path fica sem o artefato joblib.

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 503
    assert "modelo" in resp.json()["detail"].lower()


def test_event_sem_lutas_responde_card_vazio(db_session: Session, tmp_path: Path) -> None:
    """Evento existente sem lutas cadastradas -> 200 com ``bouts`` vazio (não 404)."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(db_session, ids, [])

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["bouts"] == []
    assert body["model_version"] == load_artifact(tmp_path).trained_at


def test_event_luta_sem_historico_nao_derruba_o_card(db_session: Session, tmp_path: Path) -> None:
    """Uma luta impredizível volta com motivo; as demais do card seguem preditas (200).

    Diferença deliberada em relação ao matchup isolado: lá "sem histórico" é 422 (erro da
    requisição), aqui é estado de **um item** do card.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()
    ids["Fighter Sem Historico"] = int(sem_historico.id)
    event_id = _seed_card(
        db_session,
        ids,
        [("Fighter Alpha", "Fighter Sem Historico"), ("Fighter Bravo", "Fighter Charlie")],
    )

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    impredizivel, predita = resp.json()["bouts"]
    assert impredizivel["prob_red_wins"] is None
    assert impredizivel["prob_blue_wins"] is None
    assert impredizivel["predicted_winner_id"] is None
    assert str(ids["Fighter Sem Historico"]) in impredizivel["unavailable_reason"]
    # Os cantos continuam expostos: o consumidor vê a luta no card, só sem palpite.
    assert impredizivel["fighter_red"]["id"] == ids["Fighter Alpha"]
    # A outra luta do MESMO card foi predita normalmente.
    assert predita["unavailable_reason"] is None
    assert predita["prob_red_wins"] is not None


# ---------------------------------------------------------------------------
# Diagnóstico de defasagem do cache as-of (SPEC 011, Slice 01 -- RF-05/CA-04)
# ---------------------------------------------------------------------------

# Os quatro campos do contrato de ``FeaturesFreshnessOut``. Nomeados aqui porque os três
# endpoints que o carregam asserem exatamente o mesmo conjunto -- e é esse conjunto, não uma
# chave solta, que a interface da RF-06 consome.
_CAMPOS_FRESHNESS = {"materialized_at", "n_bouts_source", "n_bouts_current", "stale"}


def test_matchup_traz_o_diagnostico_de_defasagem(db_session: Session, tmp_path: Path) -> None:
    """CA-04: ``/predict/matchup`` devolve ``features_freshness`` com os quatro campos."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        )

    assert resp.status_code == 200
    freshness = resp.json()["features_freshness"]
    assert set(freshness) == _CAMPOS_FRESHNESS
    assert freshness["stale"] is False
    assert freshness["materialized_at"] is not None
    assert freshness["n_bouts_source"] == freshness["n_bouts_current"] > 0


def test_event_traz_o_diagnostico_de_defasagem_no_envelope(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-04/D-09: o campo vive no ``EventPredictionOut``, não dentro de cada luta.

    A defasagem é do **cache**, não de uma luta: repeti-la por item seria ruído, e omiti-la do
    envelope deixaria a tela do card previsto sem o que exibir (RF-06).
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(db_session, ids, [("Fighter Alpha", "Fighter Delta")])
    # Re-materializa DEPOIS de semear o card: é a ordem da produção (ingere o evento, então
    # refresca o cache) e é o que torna a asserção de "em dia" significativa.
    run_materialize_asof(db_session)

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["features_freshness"]) == _CAMPOS_FRESHNESS
    assert body["features_freshness"]["stale"] is False
    assert all("features_freshness" not in luta for luta in body["bouts"])


def test_luta_nova_torna_a_resposta_defasada_sem_rematerializar(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-04: inserir uma luta entre dois requests faz ``stale`` virar ``true``.

    É a cicatriz do ``bout_features`` fechada onde ela dói: na resposta que entrega o dado
    defasado. Antes, o cache podia envelhecer meses e o único sinal era um log que ninguém
    lia -- aqui a defasagem chega junto com o palpite, sem custo de recomputo.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    params = {"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]}

    with _client(db_session, tmp_path) as client:
        antes = client.get("/api/v1/predict/matchup", params=params).json()
        _seed_future_bout(db_session, ids["Fighter Bravo"], ids["Fighter Charlie"])
        depois = client.get("/api/v1/predict/matchup", params=params).json()

    assert antes["features_freshness"]["stale"] is False
    assert depois["features_freshness"]["stale"] is True
    assert (
        depois["features_freshness"]["n_bouts_current"]
        == antes["features_freshness"]["n_bouts_current"] + 1
    )
    # O carimbo da materialização NÃO se move: nada foi re-materializado entre os requests.
    assert (
        depois["features_freshness"]["materialized_at"]
        == (antes["features_freshness"]["materialized_at"])
    )


def test_matchup_com_cache_nunca_materializado_reporta_a_causa(
    db_session: Session, tmp_path: Path
) -> None:
    """Sem cache, a predição é 200 indisponível -- e o ``freshness`` distingue a causa.

    Todo lutador parece "sem histórico" quando o cache nunca foi materializado, então o
    ``unavailable_code`` **não** basta para separar as duas causas: ele diz ``missing_history``
    nas duas. Quem as separa é o ``features_freshness`` da **mesma** resposta --
    ``n_bouts_source == 0`` com granular cheio significa "ninguém rodou a materialização", não
    "os dois são estreantes". É exatamente o estado que este diagnóstico existe para reportar.

    O 422 de antes virou 200 pela D-04 (emenda DP-05: ``materialized_at`` é nulável, senão o
    endpoint falharia justo aqui).
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    db_session.execute(delete(FighterAsOfFeatures))

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        )
        stats = client.get(f"/api/v1/fighters/{ids['Fighter Alpha']}/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["prob_a_wins"] is None
    assert body["unavailable_code"] == "missing_history"
    # Os DOIS lados aparecem na prosa: sem cache, nenhum tem linha.
    assert str(ids["Fighter Alpha"]) in body["unavailable_reason"]
    assert str(ids["Fighter Delta"]) in body["unavailable_reason"]
    # A causa real está aqui, na mesma resposta -- não no código, que diz "sem histórico".
    freshness = body["features_freshness"]
    assert freshness["materialized_at"] is None
    assert freshness["n_bouts_source"] == 0
    assert freshness["n_bouts_current"] > 0
    assert freshness["stale"] is True
    assert stats.status_code == 200
    assert stats.json()["features_freshness"] == freshness


# ---------------------------------------------------------------------------
# Código legível por máquina da indisponibilidade (SPEC 011, Slice 05 -- RF-28/D-08/CA-24)
# ---------------------------------------------------------------------------

# Os três membros de ``UnavailableCode``. Nomeados aqui porque a CA-24 exige que a causa seja
# decidida pelo CÓDIGO: um teste que casasse texto da prosa reintroduziria exatamente o parse
# que a D-08 existe para eliminar.
_CODIGOS_DE_INDISPONIBILIDADE = {"missing_corners", "repeated_fighter", "missing_history"}


def test_event_luta_sem_os_dois_cantos_carimba_unavailable_code(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-24: luta sem os dois cantos devolve ``unavailable_code: "missing_corners"``.

    A prosa continua preenchida (é para humano); o código vem **ao lado** dela, para a
    interface escolher o aviso sem parsear texto.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(db_session, ids, [])
    # Luta com UM canto só: o card mal formado é o estado que o código nomeia. Semeada assim
    # de origem (e não por remoção de canto) porque é exatamente como ela chega do dado sujo.
    bout = Bout(
        event_id=event_id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add(
        BoutFighter(
            bout_id=bout.id, fighter_id=ids["Fighter Alpha"], corner=Corner.RED, source="kaggle"
        )
    )
    db_session.flush()

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    (luta,) = resp.json()["bouts"]
    assert luta["unavailable_code"] == "missing_corners"
    assert luta["unavailable_reason"] is not None


def test_event_lutador_repetido_no_card_carimba_unavailable_code(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-24: lutador escalado em duas lutas do card -> ``unavailable_code: "repeated_fighter"``."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session,
        ids,
        [("Fighter Alpha", "Fighter Delta"), ("Fighter Alpha", "Fighter Bravo")],
    )

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    lutas = resp.json()["bouts"]
    assert [luta["unavailable_code"] for luta in lutas] == ["repeated_fighter"] * 2
    assert all(luta["unavailable_reason"] is not None for luta in lutas)


def test_event_estreante_carimba_unavailable_code_missing_history(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-24: estreante no card -> ``missing_history``, e a luta segue listada no card."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    estreante = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(estreante)
    db_session.flush()
    ids["Fighter Sem Historico"] = int(estreante.id)
    event_id = _seed_card(
        db_session,
        ids,
        [("Fighter Alpha", "Fighter Sem Historico"), ("Fighter Bravo", "Fighter Charlie")],
    )

    with _client(db_session, tmp_path) as client:
        resp = client.get(f"/api/v1/predict/event/{event_id}")

    assert resp.status_code == 200
    impredizivel, predita = resp.json()["bouts"]
    assert impredizivel["unavailable_code"] == "missing_history"
    assert impredizivel["unavailable_reason"] is not None
    # A luta predita não carrega código nenhum: ela tem palpite.
    assert predita["unavailable_code"] is None
    assert predita["unavailable_reason"] is None


def test_unavailable_code_do_card_pertence_ao_enum_sem_parsear_a_prosa(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-24: a causa é decidida pelo **código**, nunca por parse de ``unavailable_reason``.

    O teste compara o código contra os três valores do enum e **não** inspeciona o texto. Um
    consumidor que derivasse a causa da prosa quebraria à primeira reescrita da mensagem (que
    é livre, por ser para humano) sem quebrar teste nenhum -- o anti-padrão da D-08.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    estreante = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(estreante)
    db_session.flush()
    ids["Fighter Sem Historico"] = int(estreante.id)
    event_id = _seed_card(
        db_session,
        ids,
        [("Fighter Alpha", "Fighter Sem Historico"), ("Fighter Bravo", "Fighter Charlie")],
    )

    with _client(db_session, tmp_path) as client:
        lutas = client.get(f"/api/v1/predict/event/{event_id}").json()["bouts"]

    indisponiveis = [luta for luta in lutas if luta["unavailable_reason"] is not None]
    # Guarda de não-vacuidade: sem uma luta indisponível, as asserções abaixo passariam vazias.
    assert indisponiveis
    for luta in indisponiveis:
        assert luta["unavailable_code"] in _CODIGOS_DE_INDISPONIBILIDADE
    # A recíproca: código preenchido se e somente se há motivo.
    for luta in lutas:
        assert (luta["unavailable_code"] is None) == (luta["unavailable_reason"] is None)


def test_matchup_estreante_com_artefato_ausente_responde_503(
    db_session: Session, tmp_path: Path
) -> None:
    """DP-05f: o **503 precede** o 200-indisponível -- o artefato é carregado antes do cache.

    Servidor sem modelo é falha de servidor, não estado do par. Se a ordem fosse a inversa, um
    deploy sem artefato responderia 200 "estreante" para todo mundo, e a tela mostraria o aviso
    de lutador sem histórico em vez de dizer que o serviço está fora.
    """
    ids = _seed_history(db_session)
    # Não treina: tmp_path fica sem o artefato joblib.
    estreante = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(estreante)
    db_session.flush()

    with _client(db_session, tmp_path) as client:
        resp = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": int(estreante.id)},
        )

    assert resp.status_code == 503
    assert "modelo" in resp.json()["detail"].lower()


def test_matchup_precedencia_dos_erros_e_422_depois_404_depois_503(
    db_session: Session, tmp_path: Path
) -> None:
    """DP-05f, a cadeia inteira: 422 (mesmo lutador) -> 404 (inexistente) -> 503 (sem artefato).

    Cada degrau é verificado com o degrau de baixo **também** violado, que é a única forma de
    provar a ordem: um 404 só demonstra precedência sobre o 503 se o artefato estiver ausente
    ao mesmo tempo.
    """
    ids = _seed_history(db_session)
    inexistente = max(ids.values()) + 10_000
    # tmp_path sem artefato: o 503 está "armado" em todos os casos abaixo.

    with _client(db_session, tmp_path) as client:
        # Mesmo lutador E inexistente E sem artefato -> 422 vence.
        mesmo = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": inexistente, "fighter_b": inexistente},
        )
        # Inexistente E sem artefato -> 404 vence.
        ausente = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": inexistente},
        )
        # Lutadores válidos, sem artefato -> 503.
        sem_modelo = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        )

    assert mesmo.status_code == 422
    assert ausente.status_code == 404
    assert sem_modelo.status_code == 503


def test_matchup_unavailable_code_pertence_ao_enum_sem_parsear_a_prosa(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-24 no matchup: a causa é o **código**, nunca um ``in`` sobre ``unavailable_reason``.

    A prosa é livre (texto para gente, com ids embutidos) e pode ser reescrita a qualquer
    momento sem aviso. Um consumidor que derivasse a causa dela quebraria em silêncio na
    primeira reescrita -- e é o próprio motivo de a D-04 ter rejeitado o 422.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    estreante = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(estreante)
    db_session.flush()

    with _client(db_session, tmp_path) as client:
        indisponivel = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": int(estreante.id)},
        ).json()
        disponivel = client.get(
            "/api/v1/predict/matchup",
            params={"fighter_a": ids["Fighter Alpha"], "fighter_b": ids["Fighter Delta"]},
        ).json()

    assert indisponivel["unavailable_code"] in _CODIGOS_DE_INDISPONIBILIDADE
    # Código preenchido se e somente se há motivo -- nos dois sentidos.
    assert (indisponivel["unavailable_code"] is None) == (
        indisponivel["unavailable_reason"] is None
    )
    assert disponivel["unavailable_code"] is None
    assert disponivel["unavailable_reason"] is None
    # ``missing_corners``/``repeated_fighter`` são causas exclusivas do CARD: não existe luta
    # nem card no matchup isolado, então a única causa possível aqui é a falta de histórico.
    assert indisponivel["unavailable_code"] == "missing_history"


# ---------------------------------------------------------------------------
# Explicabilidade do matchup: blocos, cobertura medida e atributos crus
# (SPEC 011, Slice 05 -- RF-17/RF-18/RF-19; CA-12/CA-13/CA-P1)
# ---------------------------------------------------------------------------


def _matchup(client: TestClient, fighter_a: int, fighter_b: int) -> dict[str, object]:
    """``GET /predict/matchup`` do par, já desembrulhado (200 garantido por asserção)."""
    resp = client.get(
        "/api/v1/predict/matchup", params={"fighter_a": fighter_a, "fighter_b": fighter_b}
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, object] = resp.json()
    return body


def _lista_de_dicts(body: dict[str, object], campo: str) -> list[dict[str, object]]:
    """Desembrulha uma lista de objetos do JSON **estreitando o tipo** por asserção.

    O corpo de um response é ``dict[str, object]``: sem estreitar, ``mypy --strict`` recusa
    comparar ou iterar os valores, e silenciar isso com ``type: ignore`` esconderia um erro de
    forma de verdade (a resposta deixar de ser uma lista) atrás de um comentário.
    """
    valor = body[campo]
    assert isinstance(valor, list), f"{campo} não é lista: {valor!r}"
    itens: list[dict[str, object]] = []
    for item in valor:
        assert isinstance(item, dict), f"item de {campo} não é objeto: {item!r}"
        itens.append(item)
    return itens


def _inteiro(valor: object) -> int:
    """Estreita para ``int`` por asserção -- a comparação numérica exige tipo, não ``object``."""
    assert isinstance(valor, int), f"esperado inteiro, recebido {valor!r}"
    return valor


def test_matchup_devolve_uma_entrada_por_bloco_do_registro(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-12: ``blocks`` tem **11** entradas, exatamente as chaves de ``BLOCK_REGISTRY``.

    ``janela-canto-fabricado`` não está lá: ele não é conjunto de colunas, é comparação de duas
    variantes de dataset com holdouts de tamanhos diferentes (e por isso nem passa por
    ``measure_block``). A resposta tem 11 entradas, não 12 -- o auditor não deve cobrar a
    décima segunda.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        body = _matchup(client, ids["Fighter Alpha"], ids["Fighter Delta"])

    blocos = _lista_de_dicts(body, "blocks")
    assert len(blocos) == 11
    assert {bloco["name"] for bloco in blocos} == set(BLOCK_REGISTRY)
    assert WINDOW_BLOCK_NAME not in {bloco["name"] for bloco in blocos}
    # Cada entrada traz o contrato inteiro, não só o número.
    for bloco in blocos:
        assert set(bloco) == {
            "name",
            "description",
            "n_columns",
            "n_columns_evaluated",
            "coverage",
            "delta_prob_a",
        }
        assert bloco["coverage"] in {"full", "partial", "none"}
        nome = bloco["name"]
        assert isinstance(nome, str)
        assert bloco["description"] == BLOCK_REGISTRY[nome].description


def test_matchup_bloco_sem_cobertura_tem_delta_nulo_e_existe_pelo_menos_um(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-13: existe bloco ``coverage == "none"`` **e** nenhum deles traz ``0.0``.

    O teste é **duplo** de propósito. Sem o primeiro ``assert``, o segundo passaria sem provar
    nada -- um laço sobre lista vazia é verde. A degradação declarada do serving (CA-16 da SPEC
    009) garante ao menos um: as features bout-level de contexto e
    ``southpaw_opponents_faced_prior`` são ``NaN`` num confronto hipotético.

    "Não avaliado" e "não influenciou" são afirmações diferentes: ``0.0`` diria que aquela
    dimensão não importa para este confronto, quando ela nem foi medida. É o modo de falha que
    escondeu 21 features do M5 por meses, levado para a tela.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        body = _matchup(client, ids["Fighter Alpha"], ids["Fighter Delta"])

    blocos = _lista_de_dicts(body, "blocks")
    sem_cobertura = [bloco for bloco in blocos if bloco["coverage"] == "none"]
    # Não-vacuidade: sem esta asserção, o laço abaixo passaria vazio.
    assert sem_cobertura, "nenhum bloco sem cobertura -- o laço abaixo não provaria nada"
    for bloco in sem_cobertura:
        assert bloco["delta_prob_a"] is None
        assert bloco["n_columns_evaluated"] == 0
    # A recíproca (DP-05c): delta nulo se e somente se a cobertura é ``none``.
    for bloco in blocos:
        assert (bloco["delta_prob_a"] is None) == (bloco["coverage"] == "none")


def test_matchup_bloco_com_colunas_no_modelo_mas_vazias_e_none_nao_partial(
    db_session: Session, tmp_path: Path
) -> None:
    """DP-05a/DP-05c: coluna que existe no modelo mas chega vazia conta como **não avaliada**.

    O caso real é ``confronto-de-bases``: as colunas estão no vetor que o modelo come, mas o
    serving não as calcula (degradação declarada), então chegam ``NaN``. ``n_columns`` conta o
    universo **no modelo** e ``n_columns_evaluated`` o que de fato tem valor -- e é o segundo
    que decide a cobertura. Um bloco assim é ``none``, não ``partial``: nada dele foi medido.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        body = _matchup(client, ids["Fighter Alpha"], ids["Fighter Delta"])

    blocos = _lista_de_dicts(body, "blocks")
    vazios_apesar_de_existirem = [
        bloco
        for bloco in blocos
        if _inteiro(bloco["n_columns"]) > 0 and bloco["n_columns_evaluated"] == 0
    ]
    # Não-vacuidade: a degradação declarada do serving produz ao menos um desses.
    assert vazios_apesar_de_existirem
    for bloco in vazios_apesar_de_existirem:
        assert bloco["coverage"] == "none"
        assert bloco["delta_prob_a"] is None


def test_matchup_cobertura_reage_a_remocao_de_uma_base_do_payload(
    db_session: Session, tmp_path: Path
) -> None:
    """RF-18: a cobertura vem da contagem **real**, não de lista fixa -- e por isso **reage**.

    O teste remove uma base do payload JSONB de um lado e exige que o ``n_columns_evaluated``
    do bloco dono daquela base **diminua**. Lista fixa não reage: ela devolveria o mesmo número
    sobre um payload mais pobre, que é exatamente como o cache ``bout_features`` apodreceu --
    21 features descartadas em silêncio por meses, com o log avisando e ninguém lendo.

    ``n_columns`` **não** muda: o universo do bloco vem do modelo, não do payload. É a
    distinção entre "o bloco tem 3 colunas" e "2 delas foram medidas".
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    a, b = ids["Fighter Alpha"], ids["Fighter Delta"]

    with _client(db_session, tmp_path) as client:
        antes = _matchup(client, a, b)
        blocos_antes = {bloco["name"]: bloco for bloco in _lista_de_dicts(antes, "blocks")}

        linha = read_asof_features(db_session, [a])[a]
        # Procura a primeira base as-of que (1) está preenchida no payload de A e (2) pertence
        # a um bloco com colunas no modelo -- só aí a remoção tem efeito observável.
        alvo: tuple[str, str] | None = None
        for nome, bloco in BLOCK_REGISTRY.items():
            if blocos_antes[nome]["n_columns_evaluated"] == 0:
                continue
            for base in bloco.asof_bases:
                if linha.features.get(base) is not None:
                    alvo = (nome, base)
                    break
            if alvo is not None:
                break
        if alvo is None:
            pytest.fail(
                "nenhuma base as-of preenchida encontrada num bloco com colunas avaliadas; "
                "sem alvo, o teste passaria não fazendo nada"
            )
        nome_do_bloco, base_removida = alvo

        # Reatribuição (não mutação in-place): o JSONB só é marcado como sujo se a chave do
        # atributo receber um objeto novo.
        linha.features = {
            chave: valor for chave, valor in linha.features.items() if chave != base_removida
        }
        db_session.flush()

        depois = _matchup(client, a, b)

    bloco_antes = blocos_antes[nome_do_bloco]
    bloco_depois = next(
        bloco for bloco in _lista_de_dicts(depois, "blocks") if bloco["name"] == nome_do_bloco
    )
    avaliadas_antes = _inteiro(bloco_antes["n_columns_evaluated"])
    # Não-vacuidade: o bloco alvo foi escolhido por ter colunas avaliadas.
    assert avaliadas_antes > 0
    assert _inteiro(bloco_depois["n_columns_evaluated"]) < avaliadas_antes
    assert bloco_depois["coverage"] != "full"
    # O universo do bloco é do MODELO: remover dado do payload não encolhe o denominador.
    assert bloco_depois["n_columns"] == bloco_antes["n_columns"]


def test_matchup_devolve_os_atributos_as_of_crus_dos_dois_lados(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-P1 / RF-19: ``attributes`` traz as oito chaves, com rótulo e os dois valores crus.

    São os números que formam as porcentagens da tela. Crus de propósito: a tela decide o
    formato, e pré-formatá-los aqui esconderia a unidade (segundos de controle não é uma
    porcentagem).
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with _client(db_session, tmp_path) as client:
        body = _matchup(client, ids["Fighter Alpha"], ids["Fighter Delta"])

    atributos = _lista_de_dicts(body, "attributes")
    assert [atributo["key"] for atributo in atributos] == [chave for chave, _ in MATCHUP_ATTRIBUTES]
    for atributo in atributos:
        assert set(atributo) == {"key", "label", "value_a", "value_b"}
        assert atributo["label"]
        for lado in ("value_a", "value_b"):
            assert atributo[lado] is None or isinstance(atributo[lado], float)
    # Não-vacuidade: o histórico sintético preenche os atributos dos dois lados, então um
    # payload todo nulo aqui significaria que a leitura do cache parou de funcionar.
    assert any(atributo["value_a"] is not None for atributo in atributos)
    assert any(atributo["value_b"] is not None for atributo in atributos)


def test_matchup_atributo_ausente_no_payload_vira_nulo_e_nao_zero(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-P1: chave ausente no payload as-of -> ``null``, **nunca** ``0.0``.

    Zero é um valor legítimo de taxa (lutador que nunca finalizou tem ``finish_rate_prior``
    zero de verdade). Usá-lo para "não tenho o dado" faria a tela afirmar uma medição que não
    existe -- e o leitor não teria como distinguir as duas.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    a, b = ids["Fighter Alpha"], ids["Fighter Delta"]
    chave_removida = MATCHUP_ATTRIBUTES[0][0]

    with _client(db_session, tmp_path) as client:
        antes = _matchup(client, a, b)
        linha = read_asof_features(db_session, [a])[a]
        linha.features = {
            chave: valor for chave, valor in linha.features.items() if chave != chave_removida
        }
        db_session.flush()
        depois = _matchup(client, a, b)

    def _atributo(body: dict[str, object], chave: str) -> dict[str, object]:
        return next(
            atributo for atributo in _lista_de_dicts(body, "attributes") if atributo["key"] == chave
        )

    # Não-vacuidade: o valor existia antes da remoção.
    assert _atributo(antes, chave_removida)["value_a"] is not None
    assert _atributo(depois, chave_removida)["value_a"] is None
    # O outro lado é intocado -- a ausência é de um lutador, não do atributo.
    assert _atributo(depois, chave_removida)["value_b"] is not None
