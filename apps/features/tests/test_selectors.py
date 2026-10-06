"""Testes de Selector do cache as-of e da guarda barata de defasagem (SPEC 011, Slice 01).

Camada de backend do M9: leitura de ``fighter_asof_features`` e o diagnóstico de defasagem
que acompanha **toda** resposta que lê esse cache (RF-05). A guarda aqui é deliberadamente
barata -- dois ``COUNT`` --, ao contrário de
``ingestion.features.freshness.check_cache_freshness``, que recompõe a pipeline inteira:
aquela é correta no gate de treino e proibitiva num request HTTP.

O teste-guarda do CA-P2 (``count_source_bouts`` enxerga o mesmo recorte que
``read_granular``) é a amarra que a Sprint 02 tem de honrar ao introduzir
``status='completed'``: quando ela filtrar lá sem filtrar aqui, este arquivo **falha alto**,
em vez de a divergência virar combinado verbal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from apps.features.selectors import (
    count_source_bouts,
    get_features_freshness,
    read_asof_features,
)
from ingestion.features.asof import build_asof_materialization, materialize_asof_features
from ingestion.features.long_frame import read_granular
from tests.analysis.test_predict import _seed_future_bout, _seed_history

# ``as_of`` fixo: o histórico sintético termina em 2019-2020, e fixar a data mantém o recorte
# "luta anterior" determinístico, sem depender do relógio.
_AS_OF = date(2026, 1, 1)


def _materializar(session: Session) -> None:
    """Materializa o cache as-of sem commitar (a transação é a do teste)."""
    materialize_asof_features(session, build_asof_materialization(session, _AS_OF))


def test_read_asof_features_devolve_so_os_ids_pedidos(db_session: Session) -> None:
    """A leitura é por chave: devolve exatamente os lutadores pedidos, indexados por id."""
    ids = _seed_history(db_session)
    _materializar(db_session)
    pedidos = [ids["Fighter Alpha"], ids["Fighter Bravo"]]

    cache = read_asof_features(db_session, pedidos)

    assert set(cache) == set(pedidos)
    assert all(cache[fighter_id].fighter_id == fighter_id for fighter_id in pedidos)
    assert all(cache[fighter_id].features for fighter_id in pedidos)


def test_read_asof_features_ignora_id_inexistente(db_session: Session) -> None:
    """Id sem linha no cache simplesmente não aparece -- ausência, nunca ``KeyError``.

    É o que permite ao serving distinguir "lutador sem histórico" de "lutador conhecido":
    quem não tem linha volta em ``missing_history_ids``, e a resposta segue de pé.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)
    inexistente = max(ids.values()) + 10_000

    cache = read_asof_features(db_session, [ids["Fighter Alpha"], inexistente])

    assert set(cache) == {ids["Fighter Alpha"]}


def test_read_asof_features_sem_ids_nao_consulta(db_session: Session) -> None:
    """Lista vazia devolve dicionário vazio -- sustenta o card sem lutas, sem tocar o banco."""
    _seed_history(db_session)
    _materializar(db_session)

    assert read_asof_features(db_session, []) == {}


def test_get_features_freshness_com_cache_em_dia(db_session: Session) -> None:
    """CA-04: recém-materializado, o diagnóstico diz que o cache está em dia."""
    _seed_history(db_session)
    _materializar(db_session)

    freshness = get_features_freshness(db_session)

    assert freshness.n_bouts_source == freshness.n_bouts_current == count_source_bouts(db_session)
    assert freshness.stale is False
    assert freshness.materialized_at is not None
    assert freshness.materialized_at <= datetime.now(UTC)


def test_get_features_freshness_luta_nova_torna_o_cache_defasado(db_session: Session) -> None:
    """CA-04: inserir uma luta **sem** re-materializar faz ``stale`` virar ``True``.

    É a cicatriz do ``bout_features`` fechada: o cache envelheceu em silêncio por meses porque
    o único sinal era um ``logger.warning`` que ninguém leu. Aqui a defasagem é um ``COUNT``
    barato, servido na mesma resposta que entrega o dado defasado.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)
    antes = get_features_freshness(db_session)

    _seed_future_bout(db_session, ids["Fighter Alpha"], ids["Fighter Bravo"])
    depois = get_features_freshness(db_session)

    assert antes.stale is False
    assert depois.stale is True
    assert depois.n_bouts_current == antes.n_bouts_current + 1
    # O denominador gravado NÃO muda: ele descreve a materialização, não o granular de agora.
    assert depois.n_bouts_source == antes.n_bouts_source
    assert depois.materialized_at == antes.materialized_at


def test_get_features_freshness_com_cache_nunca_materializado(db_session: Session) -> None:
    """Cache vazio: ``materialized_at`` é ``None``, ``n_bouts_source`` é ``0`` e ``stale``.

    **Emenda DP-05 à SPEC**: o snippet aprovado escreve ``materialized_at`` não-nulo. Com o
    cache nunca materializado não existe valor, e um campo obrigatório forçaria os três
    endpoints a falhar exatamente no estado que o diagnóstico existe para reportar. É também
    o que distingue "estreante" de "cache nunca materializado" quando toda predição volta
    indisponível.
    """
    _seed_history(db_session)

    freshness = get_features_freshness(db_session)

    assert freshness.materialized_at is None
    assert freshness.n_bouts_source == 0
    assert freshness.n_bouts_current == count_source_bouts(db_session)
    assert freshness.stale is True


def test_get_features_freshness_sem_lutas_e_sem_cache_nao_e_defasado(db_session: Session) -> None:
    """Banco vazio: zero contra zero não é defasagem -- é ausência de dado, e não alarma.

    Sem este caso, um ambiente recém-provisionado exibiria aviso de cache velho antes de
    existir qualquer luta, e o aviso da RF-06 perderia credibilidade logo no primeiro uso.
    """
    freshness = get_features_freshness(db_session)

    assert freshness.n_bouts_current == 0
    assert freshness.n_bouts_source == 0
    assert freshness.stale is False


def test_count_source_bouts_casa_com_o_recorte_de_read_granular(db_session: Session) -> None:
    """CA-P2: a guarda e a pipeline enxergam **o mesmo** conjunto de lutas.

    Teste-guarda de integração entre sprints, não detalhe de implementação: as duas respondem
    a mesma pergunta ("quais lutas alimentam as features"), e um ``COUNT`` mais largo que a
    pipeline marcaria o cache como defasado para sempre.

    **Handoff para a Sprint 02 desta SPEC**: quando ``read_granular`` passar a filtrar
    ``status='completed'``, ``count_source_bouts`` precisa do mesmo filtro. Este teste falha
    alto se esquecerem -- a dependência entre sprints vira falha, não combinado verbal.
    """
    ids = _seed_history(db_session)
    assert count_source_bouts(db_session) == len(read_granular(db_session).bouts)

    _seed_future_bout(db_session, ids["Fighter Alpha"], ids["Fighter Bravo"])

    assert count_source_bouts(db_session) == len(read_granular(db_session).bouts)


def test_n_bouts_source_gravado_e_a_contagem_da_guarda(db_session: Session) -> None:
    """O denominador gravado na materialização é exatamente ``count_source_bouts``.

    Fecha o circuito do CA-P2 pelo outro lado: se a ingestão contasse um recorte e a guarda
    outro, o cache nasceria defasado e ninguém saberia dizer qual dos dois estava errado.
    """
    _seed_history(db_session)

    materialization = build_asof_materialization(db_session, _AS_OF)

    assert materialization.n_bouts_source == count_source_bouts(db_session)
