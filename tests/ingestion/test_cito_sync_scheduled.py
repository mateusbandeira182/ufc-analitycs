"""Testes da ingestão de eventos agendados do catálogo Cito (SPEC 011, Slice 04).

Cobrem CA-08 (upsert idempotente de evento/luta/lutador agendados), CA-09 (gate humano antes
da primeira chamada) e CA-28 (exatamente um canto ``red`` e um ``blue``, sem persistência pela
metade), contra o Postgres de teste na sessão transacional.

**Nenhum teste lê ``.cache/cito/``.** A fixture é um recorte dos itens **reais** da captura de
2026-09-01 (``fixtures/catalog_agendados_derivado/``), com o ``meta`` ajustado para encerrar a
paginação -- mesma disciplina de ``catalog_paginado_derivado`` e ``catalog_imagens_derivado``.
Teste que dependesse do cache real ficaria frágil e silenciosamente desatualizado, que é a
cicatriz do ``bout_features``. Os números do cache real vão para o relatório de implementação,
nunca para asserção.

O recorte da fixture contém, de propósito, um item de cada caminho:

- ``ufc-fight-night-september-05-2026`` -- UFC ``scheduled`` **com** card (3 lutas) e
  ``local_date`` **anterior** ao ``today`` injetado: é o caso da CA-29, que a Decisão 5 do
  plano manda **manter**;
- ``ufc-fight-night-october-31-2026`` e ``-november-07-2026`` -- UFC ``scheduled`` futuros;
- ``ufc-335`` -- UFC ``scheduled`` **sem** card (``"bouts": []``), ausência legítima;
- ``dwcs-10-5`` -- promoção fora do escopo, descartada antes do casamento;
- ``ufc-324`` -- ``completed``;
- ``ufc-306-riyadh-season-noche-ufc`` -- o **terceiro** status, ``"live"``, que nem o filtro de
  agendado nem o de realizado captura.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito import sync_scheduled
from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.client import CATALOG_PAGE_LIMIT, CallBudget, CitoClient
from ingestion.cito.dto import CitoBout, CitoCatalogItem, CitoCorner
from ingestion.cito.gap_sync import CornerOrigin, map_bout_blocks
from ingestion.cito.matching import AmbiguousEventMatchError
from ingestion.cito.sync_scheduled import (
    CATALOG_WINDOW_START,
    ScheduledSkipReason,
    ingest_scheduled_event,
    ingest_scheduled_events,
    main,
    persisted_bout_corners,
    reject_half_card,
    run_scheduled_sync,
    select_scheduled_events,
    upsert_scheduled_event,
)
from ingestion.normalize import normalize_name

_FIXTURES = Path(__file__).parent / "fixtures"
_CATALOGO_AGENDADOS = _FIXTURES / "catalog_agendados_derivado"

# ``today`` injetado em todos os testes: posterior a 'ufc-fight-night-september-05-2026' e
# anterior aos dois Fight Nights de outubro/novembro. ``date.today()`` é proibido (DTZ011).
_HOJE = date(2026, 10, 14)


def _cliente(budget: CallBudget) -> CitoClient:
    """Cliente em modo fixture sobre o catálogo derivado de agendados (zero quota real)."""
    return CitoClient(
        token="",
        base_url="https://api.citoapi.com",
        fixture_dir=_CATALOGO_AGENDADOS,
        budget=budget,
    )


def _catalogo() -> list[CitoCatalogItem]:
    """O catálogo da fixture, já validado pelos DTOs da borda."""
    budget = CallBudget(limit=10)
    return _cliente(budget).fetch_event_catalog(include_bouts=True)


# --------------------------------------------------------------------------- #
# CA-08 -- seleção: quem entra e quem é descartado, antes de qualquer casamento
# --------------------------------------------------------------------------- #


def test_select_descarta_promocao_fora_do_escopo() -> None:
    """DWCS e Road to UFC saem por ``is_ufc_catalog_item``, nunca por prefixo de slug."""
    selecionados = select_scheduled_events(_catalogo())

    assert "dwcs-10-5" not in {item.slug for item in selecionados}


def test_select_descarta_evento_realizado() -> None:
    """``completed`` não é card por vir: não tem o que agendar."""
    selecionados = select_scheduled_events(_catalogo())

    assert "ufc-324" not in {item.slug for item in selecionados}


def test_select_descarta_o_terceiro_status_live() -> None:
    """``live`` não é ``scheduled``: o filtro é por igualdade, então ele fica fora.

    Decisão **consciente**, não omissão: um evento em andamento tem desfecho parcial e quem o
    ingere é o ``gap_sync`` quando ele virar ``completed``. O que esta asserção impede é o
    silêncio -- status desconhecido tratado por omissão é o defeito que esta SPEC já cometeu
    quatro vezes.
    """
    selecionados = select_scheduled_events(_catalogo())

    assert "ufc-306-riyadh-season-noche-ufc" not in {item.slug for item in selecionados}


def test_select_mantem_agendado_de_data_passada() -> None:
    """Evento ``scheduled`` cuja data já passou **fica** -- é exatamente o caso da CA-29.

    Filtrar por ``local_date > today`` tornaria aquela CA inalcançável: ela cobre o card cujo
    evento já aconteceu e cujas lutas seguem ``scheduled`` (a janela entre o evento e o
    ``gap_sync`` manual).
    """
    selecionados = select_scheduled_events(_catalogo())

    slugs = {item.slug for item in selecionados}
    assert "ufc-fight-night-september-05-2026" in slugs
    assert any(item.local_date < _HOJE for item in selecionados)


def test_select_mantem_agendado_sem_card() -> None:
    """Evento agendado sem card é ausência legítima: entra, e o card vazio é reportado."""
    selecionados = select_scheduled_events(_catalogo())

    assert "ufc-335" in {item.slug for item in selecionados}


def test_select_devolve_os_agendados_ufc_em_ordem_estavel() -> None:
    """A ordem é determinística (data, slug): relatório e execução não mudam de forma."""
    selecionados = select_scheduled_events(_catalogo())

    assert [item.slug for item in selecionados] == [
        "ufc-fight-night-september-05-2026",
        "ufc-fight-night-october-31-2026",
        "ufc-fight-night-november-07-2026",
        "ufc-335",
    ]


def test_select_e_funcao_pura_nao_depende_de_hoje() -> None:
    """A seleção não recebe ``today``: não há corte por data a parametrizar (Decisão 5)."""
    primeiro = select_scheduled_events(_catalogo())
    segundo = select_scheduled_events(_catalogo())

    assert [item.slug for item in primeiro] == [item.slug for item in segundo]


# --------------------------------------------------------------------------- #
# CA-28 -- a guarda da RF-32: a luta não é persistida pela metade
# --------------------------------------------------------------------------- #


def _bout(bout_id: str, *slugs: str) -> CitoBout:
    """Um ``CitoBout`` agendado com os cantos dados (desfecho todo nulo)."""
    corners = tuple(CitoCorner(slug=slug, name=slug.replace("-", " ")) for slug in slugs)
    return CitoBout(bout_id=bout_id, corners=corners)  # type: ignore[arg-type]  # o teste controla o par


def test_reject_libera_a_luta_valida() -> None:
    """Dois cantos distintos, os dois resolvidos, sem luta persistida: escreve."""
    bout = _bout("1", "dan-hooker", "salahdine-parnasse")
    ids = {"dan-hooker": 1, "salahdine-parnasse": 2}

    assert reject_half_card(bout, ids, {}) is None


def test_reject_recusa_quando_os_dois_cantos_resolvem_ao_mesmo_lutador() -> None:
    """Dois slugs para o MESMO ``fighter_id``: é o furo que ``upsert_bout_fighter_totals`` tem.

    Ele indexa por ``fighter_id``, então inseriria **uma** linha e a luta nasceria com um canto
    só, sem erro nenhum -- e aí ``_corners`` marcaria a luta como indisponível e o card perderia
    o palpite. Recusar antes de escrever é o único jeito de isso não acontecer em silêncio.
    """
    bout = _bout("1", "jan-blachowicz", "jan-blachowicz-duplicado")
    ids = {"jan-blachowicz": 7, "jan-blachowicz-duplicado": 7}

    assert reject_half_card(bout, ids, {}) is ScheduledSkipReason.REPEATED_FIGHTER


def test_reject_recusa_quando_um_canto_nao_resolveu() -> None:
    """Lutador ambíguo não tem ``fighter_id``: a luta dele é pulada, com o slug no relatório."""
    bout = _bout("1", "dan-hooker", "bruno-silva")
    ids = {"dan-hooker": 1}

    assert reject_half_card(bout, ids, {}) is ScheduledSkipReason.AMBIGUOUS_FIGHTER


def test_reject_recusa_luta_ja_persistida_com_um_canto_so() -> None:
    """Meia luta persistida não se completa às cegas: reporta.

    Sem esta recusa, o upsert inseriria uma **segunda** luta para o mesmo par -- é o que
    aconteceria se o índice de persistidos descartasse a luta degenerada, como
    ``incremental._existing_bout_index`` faz.
    """
    bout = _bout("1", "dan-hooker", "salahdine-parnasse")
    ids = {"dan-hooker": 1, "salahdine-parnasse": 2}
    persistida = {frozenset({1, 2}): (99, frozenset({Corner.RED}))}

    assert reject_half_card(bout, ids, persistida) is ScheduledSkipReason.HALF_PERSISTED


def test_reject_libera_luta_ja_persistida_com_os_dois_cantos() -> None:
    """Luta completa já persistida é reuso idempotente, não meia luta."""
    bout = _bout("1", "dan-hooker", "salahdine-parnasse")
    ids = {"dan-hooker": 1, "salahdine-parnasse": 2}
    persistida = {frozenset({1, 2}): (99, frozenset({Corner.RED, Corner.BLUE}))}

    assert reject_half_card(bout, ids, persistida) is None


def test_map_bout_blocks_descarta_luta_sem_dois_cantos_distintos() -> None:
    """A metade da RF-32 que mora em ``map_bout_blocks``: ela nunca chega à persistência.

    Reusada de ``gap_sync`` em vez de copiada -- duas implementações da mesma invariante em
    módulos diferentes é a duplicação que mais machuca aqui.
    """
    catalogo = {item.slug: item for item in _catalogo()}
    bloco = catalogo["ufc-fight-night-october-31-2026"].bouts[0]
    um_canto = bloco.model_copy(update={"fighters": bloco.fighters[:1]})

    assert map_bout_blocks([um_canto]) == []
    assert len(map_bout_blocks([bloco])) == 1


def test_persisted_bout_corners_indexa_a_luta_de_um_canto_so(db_session: Session) -> None:
    """O índice **inclui** a luta degenerada que ``_existing_bout_index`` descarta.

    Sem ela no índice, o upsert inseriria uma SEGUNDA luta para o mesmo par em vez de reportar
    meia luta -- e a chave natural de ``bouts`` passaria a ter dois donos.
    """
    evento = Event(name="UFC Fight Night: Teste", date=date(2026, 10, 31), source="cito")
    db_session.add(evento)
    db_session.flush()
    lutador = Fighter(
        name="Nick Klein",
        name_normalized="nick klein",
        nickname=None,
        date_of_birth=None,
        wins=0,
        losses=0,
        draws=0,
        source="cito",
    )
    db_session.add(lutador)
    db_session.flush()
    luta = Bout(event_id=evento.id, status=BoutStatus.SCHEDULED, source="cito")
    db_session.add(luta)
    db_session.flush()
    db_session.add(
        BoutFighter(bout_id=luta.id, fighter_id=lutador.id, corner=Corner.RED, source="cito")
    )
    db_session.flush()

    indice = persisted_bout_corners(db_session, evento.id)

    assert indice == {frozenset({lutador.id}): (luta.id, frozenset({Corner.RED}))}


# --------------------------------------------------------------------------- #
# CA-08 -- upsert idempotente do evento agendado
# --------------------------------------------------------------------------- #


def _item(slug: str) -> CitoCatalogItem:
    """O item de catálogo real da fixture, pelo slug."""
    return {item.slug: item for item in _catalogo()}[slug]


def test_upsert_evento_insere_com_source_location_e_identificadores(db_session: Session) -> None:
    """Evento novo nasce com ``source="cito"``, local e os dois identificadores carimbados."""
    item = _item("ufc-fight-night-september-05-2026")

    evento, inserido = upsert_scheduled_event(db_session, item)

    db_session.flush()
    assert inserido
    assert evento.name == item.title
    assert evento.date == item.local_date
    assert evento.location == item.location_text
    assert evento.cito_slug == "ufc-fight-night-september-05-2026"
    assert evento.cito_event_id == item.id
    assert evento.source == "cito"


def test_upsert_evento_e_idempotente_sem_insert_nem_update(db_session: Session) -> None:
    """Reexecutar reencontra por ``cito_event_id`` e não produz UPDATE (valor só muda se mudou)."""
    item = _item("ufc-fight-night-september-05-2026")
    primeiro, _ = upsert_scheduled_event(db_session, item)
    db_session.flush()

    segundo, inserido = upsert_scheduled_event(db_session, item)

    db_session.flush()
    assert not inserido
    assert segundo.id == primeiro.id
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_upsert_evento_reencontra_por_nome_normalizado_em_mais_um_dia(
    db_session: Session,
) -> None:
    """Evento semeado sem identificador casa por nome normalizado dentro de +/-1 dia.

    É a divergência real entre a data local do slug e a data UTC do payload, que a ADR 0005
    documenta -- e o motivo de o slug **nunca** ser derivado por regra.
    """
    item = _item("ufc-fight-night-september-05-2026")
    semeado = Event(
        name=item.title,
        date=item.local_date + timedelta(days=1),
        location=None,
        source="kaggle",
    )
    db_session.add(semeado)
    db_session.flush()

    evento, inserido = upsert_scheduled_event(db_session, item)

    db_session.flush()
    assert not inserido
    assert evento.id == semeado.id
    assert evento.cito_slug == item.slug
    assert evento.source == "kaggle"  # ``source`` é a origem da LINHA, não do campo


def test_upsert_evento_levanta_em_ambiguidade_sem_escrever(db_session: Session) -> None:
    """Dois candidatos na janela e nenhum desempate: levanta, nada escrito."""
    item = _item("ufc-fight-night-september-05-2026")
    for offset in (0, 1):
        db_session.add(
            Event(
                name=item.title,
                date=item.local_date + timedelta(days=offset),
                location=None,
                source="kaggle",
            )
        )
    db_session.flush()

    with pytest.raises(AmbiguousEventMatchError):
        upsert_scheduled_event(db_session, item)

    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Event)) == 2
    assert (
        db_session.scalar(
            select(func.count()).select_from(Event).where(Event.cito_slug.isnot(None))
        )
        == 0
    )


# --------------------------------------------------------------------------- #
# CA-08 / CA-28 -- a luta agendada e os seus dois cantos
# --------------------------------------------------------------------------- #


def test_luta_agendada_nasce_sem_desfecho_nenhum(db_session: Session) -> None:
    """``method``/``winner_id``/``round``/``ending_time`` nulos e ``status='scheduled'``.

    ``map_bout_core`` não é usada justamente por isto: ela degradaria ``method=None`` para
    ``NO_CONTEST``, gravando dado FALSO no rótulo que o preditivo treina.
    """
    resultado = ingest_scheduled_event(db_session, _item("ufc-fight-night-october-31-2026"))

    db_session.flush()
    (luta,) = db_session.scalars(select(Bout)).all()
    assert luta.status is BoutStatus.SCHEDULED
    assert luta.method is None
    assert luta.winner_id is None
    assert luta.round is None
    assert luta.ending_time_seconds is None
    assert luta.source == "cito"
    assert resultado.bouts.inserted == 1


def test_luta_agendada_tem_exatamente_um_red_e_um_blue(db_session: Session) -> None:
    """CA-28: a pré-condição de o card ter palpite, medida no banco."""
    ingest_scheduled_event(db_session, _item("ufc-fight-night-september-05-2026"))

    db_session.flush()
    for luta in db_session.scalars(select(Bout)).all():
        cantos = db_session.scalars(select(BoutFighter).where(BoutFighter.bout_id == luta.id)).all()
        por_canto = [canto.corner for canto in cantos]
        assert por_canto.count(Corner.RED) == 1
        assert por_canto.count(Corner.BLUE) == 1


def test_cantos_da_luta_agendada_tem_stats_todas_nulas(db_session: Session) -> None:
    """Nunca zero: zero em golpe significativo é afirmação sobre uma luta que não aconteceu."""
    ingest_scheduled_event(db_session, _item("ufc-fight-night-october-31-2026"))

    db_session.flush()
    for canto in db_session.scalars(select(BoutFighter)).all():
        assert canto.source == "cito"
        assert canto.sig_strikes_landed is None
        assert canto.sig_strikes_attempted is None
        assert canto.knockdowns is None
        assert canto.takedowns_landed is None
        assert canto.control_time_seconds is None
        assert canto.reversals is None


def test_contexto_de_card_e_preenchido_e_title_bout_fica_nulo(db_session: Session) -> None:
    """``weight_class``/``card_section``/``bout_order`` entram; ``title_bout`` **não**.

    O ``titleBout: false`` de um card distante é provisório, e ``fill_bout_context`` nunca
    sobrescreve valor presente -- um ``false`` gravado hoje ficaria congelado errado quando a
    luta virasse disputa de cinturão (Decisão 7 do plano).
    """
    ingest_scheduled_event(db_session, _item("ufc-fight-night-september-05-2026"))

    db_session.flush()
    lutas = db_session.scalars(select(Bout).order_by(Bout.bout_order)).all()
    assert [luta.card_section for luta in lutas] == ["Main Card", "Main Card", "Main Card"]
    assert all(luta.bout_order is not None for luta in lutas)
    assert all(luta.weight_class is not None for luta in lutas)
    assert all(luta.title_bout is None for luta in lutas)


def test_lutador_inedito_do_card_e_criado_com_source_cito(db_session: Session) -> None:
    """O ``profile`` embutido basta: lutador novo nasce sem nenhuma chamada de perfil."""
    resultado = ingest_scheduled_event(db_session, _item("ufc-fight-night-october-31-2026"))

    db_session.flush()
    criados = db_session.scalars(select(Fighter)).all()
    assert len(criados) == 2
    assert {fighter.source for fighter in criados} == {"cito"}
    assert resultado.fighters_created == 2


def test_lutador_ja_persistido_e_reusado_nao_duplicado(db_session: Session) -> None:
    """Entity resolution: o lutador do seed é reconciliado, nunca duplicado."""
    db_session.add(
        Fighter(
            name="Dan Hooker",
            name_normalized=normalize_name("Dan Hooker"),
            nickname=None,
            date_of_birth=None,
            wins=0,
            losses=0,
            draws=0,
            source="kaggle",
        )
    )
    db_session.flush()

    ingest_scheduled_event(db_session, _item("ufc-fight-night-september-05-2026"))

    db_session.flush()
    hookers = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "dan hooker")
    ).all()
    assert len(hookers) == 1
    assert hookers[0].source == "kaggle"


def test_canto_vem_da_arte_do_card_quando_ela_traz_o_lado(db_session: Session) -> None:
    """A arte do card agendado traz o sufixo de lado, então o canto é REAL, não atribuído.

    Medido na captura: 'HOOKER_DAN_L_01-31.png' (``_L_`` = vermelho) e
    'PARNASSE_SALAHDINE_R.png' (``_R`` = azul).
    """
    resultado = ingest_scheduled_event(db_session, _item("ufc-fight-night-september-05-2026"))

    db_session.flush()
    assert resultado.corner_origins.get(CornerOrigin.ART, 0) >= 1
    hooker = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "dan hooker")
    ).one()
    canto = db_session.scalars(select(BoutFighter).where(BoutFighter.fighter_id == hooker.id)).one()
    assert canto.corner is Corner.RED


def test_rerun_do_evento_nao_insere_luta_nem_canto(db_session: Session) -> None:
    """Idempotência no grão do evento: a segunda ingestão reusa tudo."""
    item = _item("ufc-fight-night-september-05-2026")
    ingest_scheduled_event(db_session, item)
    db_session.flush()
    antes = (
        db_session.scalar(select(func.count()).select_from(Bout)),
        db_session.scalar(select(func.count()).select_from(BoutFighter)),
        db_session.scalar(select(func.count()).select_from(Fighter)),
    )

    segundo = ingest_scheduled_event(db_session, item)

    db_session.flush()
    depois = (
        db_session.scalar(select(func.count()).select_from(Bout)),
        db_session.scalar(select(func.count()).select_from(BoutFighter)),
        db_session.scalar(select(func.count()).select_from(Fighter)),
    )
    assert depois == antes
    assert segundo.bouts.inserted == 0
    assert segundo.bout_fighters_inserted == 0
    assert segundo.fighters_created == 0


def test_evento_sem_card_e_persistido_e_reportado(db_session: Session) -> None:
    """Card vazio é ausência legítima: o evento entra e aparece em ``without_card``."""
    resultado = ingest_scheduled_event(db_session, _item("ufc-335"))

    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0
    assert resultado.without_card


# --------------------------------------------------------------------------- #
# CA-08 / CA-08a -- lote, relatório sem total mudo e idempotência ponta-a-ponta
# --------------------------------------------------------------------------- #


def _contagens(session: Session) -> tuple[int, int, int, int]:
    """``(events, bouts, bout_fighters, fighters)`` -- o que a idempotência tem de congelar."""
    return (
        session.scalar(select(func.count()).select_from(Event)) or 0,
        session.scalar(select(func.count()).select_from(Bout)) or 0,
        session.scalar(select(func.count()).select_from(BoutFighter)) or 0,
        session.scalar(select(func.count()).select_from(Fighter)) or 0,
    )


def test_lote_grava_os_agendados_ufc_e_so_eles(db_session: Session) -> None:
    """Os 4 agendados UFC da fixture entram; DWCS, ``completed`` e ``live`` não."""
    budget = CallBudget(limit=10)
    relatorio = run_scheduled_sync(db_session, _cliente(budget), budget, today=_HOJE)

    db_session.flush()
    nomes = {evento.cito_slug for evento in db_session.scalars(select(Event)).all()}
    assert nomes == {
        "ufc-fight-night-september-05-2026",
        "ufc-fight-night-october-31-2026",
        "ufc-fight-night-november-07-2026",
        "ufc-335",
    }
    assert relatorio.scheduled_items == 4
    assert relatorio.catalog_items == 7


def test_relatorio_separa_inserido_de_reusado_e_nao_tem_total_mudo(db_session: Session) -> None:
    """CA-08a: é a diferença entre inserido e reusado que PROVA a idempotência."""
    budget = CallBudget(limit=10)
    primeiro = run_scheduled_sync(db_session, _cliente(budget), budget, today=_HOJE)
    db_session.flush()

    budget_rerun = CallBudget(limit=10)
    segundo = run_scheduled_sync(db_session, _cliente(budget_rerun), budget_rerun, today=_HOJE)

    assert primeiro.events.inserted == 4
    assert primeiro.bouts.inserted == 5  # 3 + 1 + 1; 'ufc-335' não tem card
    assert primeiro.bout_fighters_inserted == 10
    assert segundo.events.inserted == 0
    assert segundo.events.updated == 4
    assert segundo.bouts.inserted == 0
    assert segundo.bouts.updated == 5
    assert segundo.bout_fighters_inserted == 0
    assert segundo.fighters_created == 0


def test_idempotencia_ponta_a_ponta_as_contagens_nao_mudam(db_session: Session) -> None:
    """CA-08: rodar duas vezes contra a mesma fonte não altera nenhuma contagem."""
    budget = CallBudget(limit=10)
    run_scheduled_sync(db_session, _cliente(budget), budget, today=_HOJE)
    db_session.flush()
    antes = _contagens(db_session)

    budget_rerun = CallBudget(limit=10)
    run_scheduled_sync(db_session, _cliente(budget_rerun), budget_rerun, today=_HOJE)

    db_session.flush()
    assert _contagens(db_session) == antes


def test_relatorio_mede_quantos_agendados_sao_futuros(db_session: Session) -> None:
    """A quantidade de futuros é **medida**, nunca assumida -- não há corte por data."""
    budget = CallBudget(limit=10)
    relatorio = run_scheduled_sync(db_session, _cliente(budget), budget, today=_HOJE)

    # 3 futuros (31/10, 07/11, 12/12) e 1 passado ainda agendado (05/09) -- o caso da CA-29.
    assert relatorio.events_in_the_future == 3
    assert relatorio.scheduled_items - relatorio.events_in_the_future == 1


def test_relatorio_lista_evento_sem_card(db_session: Session) -> None:
    """Card vazio aparece **nominalmente**: um número sozinho não diz qual evento ficou sem."""
    budget = CallBudget(limit=10)
    relatorio = run_scheduled_sync(db_session, _cliente(budget), budget, today=_HOJE)

    assert relatorio.events_without_card == ("ufc-335",)


def test_catalogo_todo_em_cache_nao_cobra_o_budget(db_session: Session, tmp_path: Path) -> None:
    """Cache hit não chama o cliente e, portanto, não cobra quota: é o custo zero da CA-08."""
    cache = CatalogPageCache(tmp_path)
    primeiro = CallBudget(limit=10)
    run_scheduled_sync(db_session, _cliente(primeiro), primeiro, today=_HOJE, cache=cache)
    assert primeiro.used == 1

    segundo = CallBudget(limit=10)
    run_scheduled_sync(db_session, _cliente(segundo), segundo, today=_HOJE, cache=cache)

    assert segundo.used == 0


def test_relatorio_conta_a_procedencia_do_canto(db_session: Session) -> None:
    """``corner_origins`` é o único lugar onde a procedência aparece: ``source`` é 'cito' sempre."""
    budget = CallBudget(limit=10)
    relatorio = run_scheduled_sync(db_session, _cliente(budget), budget, today=_HOJE)

    assert sum(relatorio.corner_origins.values()) == 5
    assert relatorio.corner_origins.get(CornerOrigin.ART, 0) >= 1


def test_relatorio_reporta_descarte_item_a_item_com_motivo(db_session: Session) -> None:
    """Descarte vem com o id da luta e o motivo -- número sozinho não diz qual luta se perdeu."""
    budget = CallBudget(limit=10)
    cliente = _cliente(budget)
    catalogo = cliente.fetch_event_catalog(include_bouts=True)
    alvo = next(item for item in catalogo if item.slug == "ufc-fight-night-october-31-2026")
    bloco = alvo.bouts[0]
    mutilado = alvo.model_copy(
        update={"bouts": [bloco.model_copy(update={"fighters": bloco.fighters[:1]})]}
    )

    relatorio = ingest_scheduled_events(db_session, [mutilado], today=_HOJE)

    db_session.flush()
    assert relatorio.skipped_bouts == (
        ("ufc-fight-night-october-31-2026", bloco.id, ScheduledSkipReason.MISSING_CORNERS),
    )
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0


def test_relatorio_reporta_lutador_em_mais_de_uma_luta_sem_descartar(
    db_session: Session,
) -> None:
    """Lutador repetido no card é **reportado**, não descartado: pode ser dado legítimo.

    Mas é a segunda via pela qual o card perde palpite --
    ``apps.predictions.services._repeated_fighter_ids`` tira essas lutas do lote --, então
    silenciá-lo esvaziaria a tela sem ninguém saber por quê.
    """
    budget = CallBudget(limit=10)
    cliente = _cliente(budget)
    catalogo = cliente.fetch_event_catalog(include_bouts=True)
    alvo = next(item for item in catalogo if item.slug == "ufc-fight-night-september-05-2026")
    primeira, segunda = alvo.bouts[0], alvo.bouts[1]
    # O canto azul da segunda luta passa a ser o lutador do canto vermelho da primeira.
    reescalado = segunda.model_copy(
        update={"fighters": [segunda.fighters[0], primeira.fighters[0]]}
    )
    com_repetido = alvo.model_copy(update={"bouts": [primeira, reescalado]})

    relatorio = ingest_scheduled_events(db_session, [com_repetido], today=_HOJE)

    db_session.flush()
    assert relatorio.fighters_in_multiple_bouts == (
        ("ufc-fight-night-september-05-2026", "dan-hooker"),
    )
    assert relatorio.bouts.inserted == 2  # reportado, NÃO descartado


# --------------------------------------------------------------------------- #
# CA-09 -- o gate humano aborta ANTES da primeira chamada
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_isolado(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Aponta o ``SessionLocal`` do comando para a sessão transacional do teste."""

    @contextmanager
    def _sessao() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(sync_scheduled, "SessionLocal", _sessao)
    yield


@pytest.fixture
def rede_proibida(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Espião que FALHA se qualquer fetch de catálogo acontecer; devolve o log de chamadas."""
    chamadas: list[str] = []

    def _spy(*_args: object, **_kwargs: object) -> object:
        chamadas.append("fetch_event_catalog")
        raise AssertionError("a rede não deveria ser tocada neste caminho")

    monkeypatch.setattr(CitoClient, "fetch_event_catalog", _spy)
    yield chamadas


def test_rede_real_sem_confirmacao_aborta_antes_da_primeira_chamada(
    rede_proibida: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    """CA-09: sem ``--confirmar-gasto-de-quota``, aborta com o gate e **nada** é chamado.

    A asserção mais importante é a última: o espião prova zero chamadas, então o gate não
    aconteceu "depois de instanciar o cliente" nem "depois de abrir a sessão".
    """
    with (
        caplog.at_level(logging.ERROR, logger="ingestion.cito.sync_scheduled"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--call-budget", "5"])

    assert excinfo.value.code == 1
    assert "Gate humano não confirmado" in caplog.text
    assert rede_proibida == []


def test_somente_cache_dispensa_a_confirmacao(
    cli_isolado: None, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--somente-cache`` roda sem confirmação porque o teto 0 o impede de alcançar a rede."""
    monkeypatch.setattr(sync_scheduled, "_DEFAULT_FIXTURE_DIR", _CATALOGO_AGENDADOS)
    cache = CatalogPageCache(tmp_path)
    cache.get_or_fetch(
        CitoClient._catalog_cache_key(1, CATALOG_PAGE_LIMIT, CATALOG_WINDOW_START, None, True),
        lambda: json.loads(
            (_CATALOGO_AGENDADOS / "events_catalog_page_1.json").read_text(encoding="utf-8")
        ),
    )

    main(["--somente-cache", "--cache-dir", str(tmp_path)])

    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Event)) == 4


def test_somente_cache_levanta_em_vez_de_sair_para_a_rede(
    cli_isolado: None, db_session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """O teto é a proteção quando o gate é dispensado: tentar buscar **levanta**.

    É o risco que ``--somente-cache`` cria -- ele contorna o gate, então a garantia passa a ser
    o ``CallBudget`` de teto 0. Com o cache vazio, a primeira página é um miss e o
    ``QuotaExceededError`` interrompe **antes** de qualquer escrita.
    """
    with (
        caplog.at_level(logging.ERROR, logger="ingestion.cito.sync_scheduled"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--somente-cache", "--cache-dir", str(tmp_path)])

    assert excinfo.value.code == 1
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Event)) == 0


def test_fixture_dispensa_a_confirmacao(
    cli_isolado: None, db_session: Session, tmp_path: Path
) -> None:
    """``--fixture`` dispensa o gate, como nos demais comandos do pacote."""
    main(["--fixture", "--fixture-dir", str(_CATALOGO_AGENDADOS), "--cache-dir", str(tmp_path)])

    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Event)) == 4


def test_cli_nao_oferece_flag_to(cli_isolado: None, capsys: pytest.CaptureFixture[str]) -> None:
    """Não existe ``--to``: incluí-lo mudaria a chave de cache e tornaria a execução paga."""
    with pytest.raises(SystemExit):
        main(["--to", "2026-12-31"])

    assert "unrecognized arguments" in capsys.readouterr().err
