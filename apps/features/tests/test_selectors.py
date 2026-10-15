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

A Sprint 06 acrescenta ``read_current_form`` -- a projeção dos treze nomes da seção FORMA
ATUAL (RF-15) sobre o mesmo payload JSONB. Ela traz o **segundo** teste-guarda do arquivo
(CA-05 do Plano 011-06): os nomes são redigitados em ``apps/`` em vez de importados de
``ingestion.features.rolling`` (mesmo precedente de ``count_source_bouts``), e é a guarda que
torna a duplicação segura -- um rename na pipeline falha alto, em vez de esvaziar a seção da
tela em silêncio.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from apps.features.models import FeaturePayload, FighterAsOfFeatures
from apps.features.schemas import CurrentFormOut
from apps.features.selectors import (
    CURRENT_FORM_FEATURES,
    count_source_bouts,
    get_features_freshness,
    read_asof_features,
    read_current_form,
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


def test_read_current_form_devolve_os_treze_nomes_da_secao(db_session: Session) -> None:
    """CA-01: lutador com linha no cache devolve exatamente os treze campos de FORMA ATUAL.

    A projeção é fechada de propósito: a seção da tela contrasta a janela das três lutas
    anteriores com o agregado de carreira, e deixar o payload inteiro vazar para o contrato
    faria a tela escolher o que mostrar -- decisão que pertence ao backend.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)

    forma = read_current_form(db_session, ids["Fighter Alpha"])

    assert forma is not None
    assert set(forma) == set(CURRENT_FORM_FEATURES)
    assert len(CURRENT_FORM_FEATURES) == 13


def test_read_current_form_sem_linha_no_cache_devolve_none(db_session: Session) -> None:
    """CA-02: lutador sem linha as-of devolve ``None`` -- nunca um dicionário de zeros.

    Zero em golpe significativo é uma afirmação sobre o lutador: diria que ele subiu no
    octógono e não acertou nada. Quem não tem histórico as-of não tem o que afirmar.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)
    sem_linha = max(ids.values()) + 10_000

    assert read_current_form(db_session, sem_linha) is None


def test_read_current_form_com_cache_nunca_materializado_devolve_none(
    db_session: Session,
) -> None:
    """CA-03: sem materialização, todo lutador devolve ``None``.

    O selector não adivinha o motivo da ausência -- quem distingue "estreante" de "cache
    nunca materializado" na resposta é ``features_freshness`` (``materialized_at`` nulo e
    ``n_bouts_source`` zero).
    """
    ids = _seed_history(db_session)

    assert read_current_form(db_session, ids["Fighter Alpha"]) is None


def test_read_current_form_preserva_nulo_sem_virar_zero(db_session: Session) -> None:
    """CA-04: valor nulo no payload sai ``None``; zero gravado continua ``0.0``.

    Os dois casos convivem na mesma linha de propósito: é a única forma de provar que a
    conversão de borda **distingue** ausência de zero em vez de colapsar as duas. A asserção
    é ``is None`` -- ``assert not forma[...]`` passaria com ``0.0`` e afirmaria o contrário.
    """
    ids = _seed_history(db_session)
    payload: FeaturePayload = dict.fromkeys(CURRENT_FORM_FEATURES, 1.0)
    payload["takedown_defense_r3"] = None
    payload["ko_losses_prior"] = 0
    db_session.add(
        FighterAsOfFeatures(
            fighter_id=ids["Fighter Alpha"],
            features=payload,
            style_history=[],
            n_bouts_source=0,
            source="test",
            materialized_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    forma = read_current_form(db_session, ids["Fighter Alpha"])

    assert forma is not None
    assert forma["takedown_defense_r3"] is None
    assert forma["ko_losses_prior"] == 0.0
    assert isinstance(forma["ko_losses_prior"], float)


def test_current_form_le_os_nomes_que_a_pipeline_realmente_materializa(
    db_session: Session,
) -> None:
    """CA-05: um rename na pipeline falha ALTO, em vez de esvaziar a seção em silêncio.

    Mesma tese do ``test_count_source_bouts_casa_com_o_recorte_de_read_granular``: a
    dependência entre a pipeline e a leitura vira falha de teste, não combinado verbal. Sem
    esta guarda, renomear uma feature em ``ingestion.features.rolling`` deixaria
    ``read_current_form`` devolvendo treze nulos -- e a tela mostraria "sem dado" para um
    lutador com histórico completo.
    """
    ids = _seed_history(db_session)
    _materializar(db_session)

    linha = read_asof_features(db_session, [ids["Fighter Alpha"]])[ids["Fighter Alpha"]]

    for nome in CURRENT_FORM_FEATURES:
        assert nome in linha.features, nome


def test_current_form_features_casa_com_os_campos_do_schema() -> None:
    """CA-05: a tupla do selector e os campos de ``CurrentFormOut`` são o mesmo conjunto.

    ``CurrentFormOut`` tem ``extra="forbid"``, então a divergência já falharia na validação
    do endpoint -- em 500, no request do usuário. Esta asserção a antecipa para o gate: as
    duas listas são redigitadas lado a lado (DP-08 proíbe camada de tradução) e precisam de
    uma amarra mecânica, não de disciplina.
    """
    assert set(CURRENT_FORM_FEATURES) == set(CurrentFormOut.model_fields)
