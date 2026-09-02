"""Testes do fechamento do gap de 12 meses via catálogo Cito (M6, Slice 06), em modo fixture.

Cobrem a descoberta dos eventos ausentes (janela + filtro de promoção + descarte do já
persistido), os mapeadores puros do payload de stats, a entity resolution dos lutadores novos
(RF-13), a ingestão de um evento com **uma** chamada, a idempotência do rerun e o gate humano.
Nenhum teste toca a rede: o cliente roda em modo fixture (JSON local) e a quota é contabilizada
pelo ``CallBudget`` para que as asserções de custo sejam exatas.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.cache import EventStatsCache
from ingestion.cito.client import CallBudget, CitoClient
from ingestion.cito.dto import (
    CitoBout,
    CitoBoutBlock,
    CitoBoutFighterRef,
    CitoCatalogItem,
    CitoEventBlock,
    CitoEventStats,
    CitoFighter,
)
from ingestion.cito.gap_sync import (
    CornerOrigin,
    GapAmbiguityError,
    GapEventResult,
    GapSyncError,
    GapSyncSummary,
    _parse_args,
    art_side,
    assign_corners,
    assign_deterministic_corners,
    corner_profile_to_fighter,
    corner_sort_keys,
    ingest_gap_event,
    main,
    map_stats_to_bouts,
    resolve_gap_fighters,
    run_gap_sync,
    select_gap_events,
    warn_assigned_corners_in_official_window,
)
from ingestion.cito.gate import HumanGateNotConfirmedError, enforce_human_gate
from ingestion.cito.matching import is_ufc_catalog_item
from ingestion.entity_resolution import AmbiguousFighterMatchError
from ingestion.incremental import map_bout_core
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from mma_analytics.settings import settings

_FIXTURES = Path(__file__).parent / "fixtures"

# Payload REAL da Cito (sondagem autorizada de 2026-08-31): 13 lutas, 26 totais, 52 rounds,
# com o ``profile`` embutido em cada canto. É a única fixture de dado não editado.
_PAYLOAD_REAL = "ufc-fight-night-august-22-2026"

# Catálogo derivado da captura real de 2026-08-31 (duas páginas), já usado pela Sprint 007-03.
# Cobre os quatro casos do filtro de promoção e a mistura de eventos realizados e agendados.
_CATALOG_DIR = _FIXTURES / "catalog_paginado_derivado"

# Conjunto de fixtures da slice: catálogo e stats no MESMO diretório, que é o que o modo fixture
# do cliente exige para percorrer o fluxo inteiro (catálogo -> stats) sem tocar a rede. Ambos são
# **recortes** de capturas reais -- a página de catálogo recorta itens reais da captura de
# 2026-08-31, e cada payload de stats recorta lutas de um card real --, nunca dado inventado.
#
# ``event_stats_ufc-freedom-250.json`` foi refeito na revisão da SPEC 007 (R-04): a versão
# anterior era uma quimera (bloco ``event`` de um evento, luta de outro). O recorte atual sai da
# captura ``.cache/cito-includebouts/gap_includebouts_p1_l50.json`` (2026-09-01), com a luta
# principal do card verbatim; o bloco ``meta`` ficou de fora porque a captura disponível é a do
# catálogo com lutas, e escrever um ``meta`` seria voltar a afirmar sobre a API sem medição.
_GAP_FIXTURES = _FIXTURES / "gap_sync"

# Data corrente injetada nos testes (determinismo: ``date.today()`` é proibido pelo ruff/DTZ011).
_HOJE = date(2026, 9, 1)


def _catalog_items() -> list[CitoCatalogItem]:
    """Todos os itens das duas páginas do catálogo derivado, tipados na borda."""
    items: list[CitoCatalogItem] = []
    for page in sorted(_CATALOG_DIR.glob("events_catalog_page_*.json")):
        payload = json.loads(page.read_text(encoding="utf-8"))
        items.extend(CitoCatalogItem.model_validate(item) for item in payload["data"])
    return items


def _item(slug: str) -> CitoCatalogItem:
    """O item do catálogo derivado com o ``slug`` pedido (falha alto se a fixture mudar)."""
    for item in _catalog_items():
        if item.slug == slug:
            return item
    raise AssertionError(f"Slug {slug!r} ausente do catálogo de fixture.")


def _fixture_client(budget: CallBudget | None = None) -> CitoClient:
    """``CitoClient`` em modo fixture (lê JSON local, sem tocar rede nem quota real)."""
    return CitoClient(
        token=settings.cito_api_token,
        base_url=settings.cito_base_url,
        fixture_dir=_FIXTURES,
        budget=budget,
    )


def _stats_com_bouts(bouts: list[CitoBoutBlock]) -> CitoEventStats:
    """``CitoEventStats`` mínimo com o card informado (sem totais nem rounds)."""
    return CitoEventStats(
        event=CitoEventBlock(
            id="evento-sintetico",
            slug="evento-sintetico",
            title="Evento Sintético",
            status="completed",
            event_date=date(2026, 8, 22),
        ),
        bouts=bouts,
        bout_stats=[],
    )


def _seed_persisted_event(session: Session, *, name: str, event_date: date, cito_slug: str) -> None:
    """Semeia um evento já persistido (seed do Kaggle) com o identificador do catálogo."""
    session.add(
        Event(
            name=name,
            date=event_date,
            location=None,
            source="kaggle",
            cito_slug=cito_slug,
        )
    )
    session.flush()


# --------------------------------------------------------------------------- #
# CA-02 -- filtro de promoção (reuso de ``is_ufc_catalog_item``, Sprint 007-03).
# --------------------------------------------------------------------------- #


def test_promotion_filter_keeps_ufc_and_drops_dwcs_and_road_to_ufc() -> None:
    """Filtro por exclusão mantém UFC (inclusive prefixado) e descarta DWCS/Road to UFC.

    Guarda de regressão sobre a função da Sprint 007-03: uma allowlist por prefixo 'ufc-'
    descartaria 'cryptocom-ufc-331', que é UFC legítimo, e 'road-to-ufc-season-5-semifinals'
    passaria por conter 'ufc' no meio do slug.
    """
    assert is_ufc_catalog_item(_item("cryptocom-ufc-331")) is True
    assert is_ufc_catalog_item(_item("ufc-freedom-250")) is True
    assert is_ufc_catalog_item(_item("road-to-ufc-season-5-semifinals")) is False
    assert is_ufc_catalog_item(_item("dwcs-10-8")) is False


# --------------------------------------------------------------------------- #
# CA-01 -- descoberta dos eventos do gap.
# --------------------------------------------------------------------------- #


def test_select_gap_events_returns_only_missing_ufc_events_in_window(db_session: Session) -> None:
    """Devolve só os eventos UFC realizados, ainda ausentes, do corte até hoje, em ordem.

    O evento persistido está **dentro** da janela (o corte é inclusive) e ainda assim é
    descartado -- é isso que torna o rerun gratuito em quota. Agendado, data futura, DWCS e
    Road to UFC ficam fora.
    """
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )

    selected = select_gap_events(db_session, _catalog_items(), today=_HOJE)

    assert [item.slug for item in selected] == [
        "ufc-315",
        "ufc-freedom-250",
        "ufc-fight-night-august-22-2026",
    ]


def test_select_gap_events_discards_events_older_than_the_last_persisted_date(
    db_session: Session,
) -> None:
    """Evento realizado anterior ao último persistido fica fora -- a janela é o gap, não a base.

    'ufc-315' (2026-05-10) e 'ufc-328' (2026-05-09) são UFC, realizados e ausentes da base,
    mas anteriores ao corte: preenchê-los é backfill histórico, não fechamento de gap.
    """
    _seed_persisted_event(
        db_session,
        name="UFC Freedom 250",
        event_date=date(2026, 6, 14),
        cito_slug="ufc-freedom-250",
    )

    selected = select_gap_events(db_session, _catalog_items(), today=_HOJE)

    assert [item.slug for item in selected] == ["ufc-fight-night-august-22-2026"]


def test_select_gap_events_discards_event_persisted_without_cito_slug(
    db_session: Session,
) -> None:
    """Evento já persistido sem ``cito_slug`` é descartado pela chave natural (nome + data).

    O descarte não pode depender só do identificador do catálogo: um evento ingerido antes da
    sincronização de catálogo tem os dois campos nulos, e reingeri-lo criaria a duplicata que
    a chave natural existe para impedir.
    """
    db_session.add(
        Event(
            name="UFC 328: Chimaev vs. Strickland",
            date=date(2026, 5, 9),
            location=None,
            source="kaggle",
        )
    )
    db_session.flush()

    selected = select_gap_events(db_session, _catalog_items(), today=_HOJE)

    assert [item.slug for item in selected] == [
        "ufc-315",
        "ufc-freedom-250",
        "ufc-fight-night-august-22-2026",
    ]


def test_select_gap_events_uses_normalized_name_key(db_session: Session) -> None:
    """A chave natural é o nome **normalizado**: pontuação e caixa não recriam o evento.

    O catálogo grafa 'UFC 315' e a base pode ter 'ufc 315.' de outra fonte -- a mesma
    normalização de nome de evento da Sprint 007-03 colapsa as duas grafias.
    """
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )
    db_session.add(Event(name="ufc 315.", date=date(2026, 5, 10), location=None, source="kaggle"))
    db_session.flush()

    selected = select_gap_events(db_session, _catalog_items(), today=_HOJE)

    assert "ufc-315" not in [item.slug for item in selected]
    assert normalize_name("ufc 315.") != normalize_name("UFC 315")


# --------------------------------------------------------------------------- #
# CA-03 / CA-05 -- mapeadores puros do payload de stats.
# --------------------------------------------------------------------------- #


def _real_stats() -> CitoEventStats:
    """As stats do payload REAL da Cito (13 lutas, 26 totais, 52 rounds), lidas da fixture."""
    return _fixture_client().fetch_event_stats(_PAYLOAD_REAL)


def test_map_stats_to_bouts_orders_corners_deterministically_by_slug() -> None:
    """O par sai em ordem estável por slug, **ignorando** o rótulo ``corner`` do payload.

    A ordenação aqui é só posicionamento até os ``fighter_id`` existirem -- o canto de verdade
    é atribuído por ``assign_deterministic_corners``. Usar o rótulo da Cito seria trazer o
    desfecho disfarçado de canto (ver a trava de vazamento mais abaixo).
    """
    bouts = map_stats_to_bouts(_real_stats())

    principal = bouts[0]
    assert principal.bout_id == "fb77b08a90b92d5b"
    assert principal.corners[0].slug == "anthony-hernandez"
    assert principal.corners[1].slug == "gregory-rodrigues"


def test_assign_deterministic_corners_puts_the_smallest_normalized_name_in_red() -> None:
    """A atribuição é uma função pura da chave de ordenação -- e não toca o vencedor."""
    (principal,) = map_stats_to_bouts(_real_stats())[:1]
    invertido = assign_deterministic_corners(
        principal, {"anthony-hernandez": ("zz", 1), "gregory-rodrigues": ("aa", 2)}
    )
    mantido = assign_deterministic_corners(
        principal, {"anthony-hernandez": ("aa", 2), "gregory-rodrigues": ("zz", 1)}
    )

    assert invertido.corners[0].slug == "gregory-rodrigues"
    assert mantido.corners[0].slug == "anthony-hernandez"
    assert invertido.winner_slug == mantido.winner_slug == "gregory-rodrigues"


def test_corner_sort_keys_uses_the_normalized_name_with_the_id_as_tiebreak() -> None:
    """A chave é ``(nome normalizado, fighter_id)``: o id só desempata homônimo exato.

    O nome vem primeiro porque é a única parte independente da fonte; o id entra apenas para
    manter a ordem total e reprodutível no SQL do UPDATE das linhas já persistidas.
    """
    chaves = corner_sort_keys(
        {
            "bruno-silva": CitoFighter(slug="bruno-silva", name="Bruno Silva"),
            "aaron-pico": CitoFighter(slug="aaron-pico", name="Aaron Pico"),
        },
        {"bruno-silva": 10, "aaron-pico": 90},
    )

    assert chaves == {"bruno-silva": ("bruno silva", 10), "aaron-pico": ("aaron pico", 90)}
    assert chaves["aaron-pico"] < chaves["bruno-silva"]  # o id maior não decide


def test_map_stats_to_bouts_maps_result_fields() -> None:
    """Método, round, tempo, categoria e vencedor vêm do card, prontos para ``map_bout_core``."""
    principal = map_stats_to_bouts(_real_stats())[0]

    assert principal.method == "Decision - Unanimous"
    assert principal.finish_round == 5
    assert principal.finish_time_seconds == 300
    assert principal.weight_class == "Middleweight"
    assert principal.winner_slug == "gregory-rodrigues"


def test_map_stats_to_bouts_keeps_absent_result_as_none() -> None:
    """Campo ausente no card degrada para ``None`` -- nunca zero nem sentinela."""
    bloco = CitoBoutBlock.model_validate(
        {
            "id": "sem-resultado",
            "fighters": [
                {"fighterSlug": "a-fighter", "corner": "red"},
                {"fighterSlug": "b-fighter", "corner": "blue"},
            ],
        }
    )
    stats = _stats_com_bouts([bloco])

    (bout,) = map_stats_to_bouts(stats)

    assert bout.method is None
    assert bout.finish_round is None
    assert bout.finish_time_seconds is None
    assert bout.weight_class is None
    assert bout.winner_slug is None


def test_map_stats_to_bouts_skips_cancelled_and_incomplete_cards() -> None:
    """Luta cancelada ou sem os dois cantos é descartada -- nunca se inventa um adversário."""
    cancelada = CitoBoutBlock.model_validate(
        {
            "id": "cancelada",
            "isCancelled": True,
            "fighters": [
                {"fighterSlug": "a-fighter", "corner": "red"},
                {"fighterSlug": "b-fighter", "corner": "blue"},
            ],
        }
    )
    sem_adversario = CitoBoutBlock.model_validate(
        {"id": "sozinha", "fighters": [{"fighterSlug": "a-fighter", "corner": "red"}]}
    )

    assert map_stats_to_bouts(_stats_com_bouts([cancelada, sem_adversario])) == []


def test_corner_profile_to_fighter_reads_identity_and_record_from_payload() -> None:
    """O lutador é montado do ``profile`` embutido no canto -- zero chamadas de perfil (RF-13)."""
    principal = _real_stats().bouts[0]
    (canto,) = [f for f in principal.fighters if f.fighter_slug == "anthony-hernandez"]

    fighter = corner_profile_to_fighter(canto)

    assert fighter.slug == "anthony-hernandez"
    assert fighter.name == "Anthony Hernandez"
    assert fighter.nickname == "Fluffy"
    assert (fighter.wins, fighter.losses, fighter.draws) == (15, 4, 0)


def test_corner_profile_to_fighter_leaves_bio_absent() -> None:
    """DOB, stance, altura e alcance ficam ``None``: o payload de stats não os traz.

    A ausência é explícita de propósito -- a antropometria não entra em lote (a chamada de
    perfil é gasta apenas no desempate de ambiguidade). A divisão do ``profile`` é descartada:
    não há coluna em ``fighters`` e ``bouts.weight_class`` já cobre o dado.
    """
    principal = _real_stats().bouts[0]
    (canto,) = [f for f in principal.fighters if f.fighter_slug == "anthony-hernandez"]

    fighter = corner_profile_to_fighter(canto)

    assert fighter.date_of_birth is None
    assert fighter.stance is None
    assert fighter.height_cm is None
    assert fighter.reach_cm is None


def test_corner_profile_to_fighter_degrades_missing_record_to_zeros() -> None:
    """Cartel ausente no ``profile`` degrada para 0/0/0: as colunas de ``fighters`` são NOT NULL."""
    canto = CitoBoutFighterRef.model_validate(
        {
            "fighterSlug": "novo-lutador",
            "fighterName": "Novo Lutador",
            "corner": "red",
            "profile": {"slug": "novo-lutador", "name": "Novo Lutador"},
        }
    )

    fighter = corner_profile_to_fighter(canto)

    assert (fighter.wins, fighter.losses, fighter.draws) == (0, 0, 0)
    assert fighter.nickname is None


def test_corner_profile_to_fighter_falls_back_to_card_name_without_profile() -> None:
    """Canto sem ``profile`` degrada para o nome do card; sem nome nenhum, falha alto.

    Nunca se deriva o nome de exibição do slug: 'st-pierre' não é 'St Pierre', e um nome
    inventado entra na chave de entity resolution e cria a duplicata que ela existe para
    impedir.
    """
    sem_profile = CitoBoutFighterRef.model_validate(
        {"fighterSlug": "novo-lutador", "fighterName": "Novo Lutador", "corner": "red"}
    )
    assert corner_profile_to_fighter(sem_profile).name == "Novo Lutador"

    anonimo = CitoBoutFighterRef.model_validate({"fighterSlug": "novo-lutador", "corner": "red"})
    with pytest.raises(GapSyncError):
        corner_profile_to_fighter(anonimo)


# --------------------------------------------------------------------------- #
# CA-05 / CA-06 -- entity resolution dos lutadores novos (RF-13).
# --------------------------------------------------------------------------- #


class _ClienteEspiao(CitoClient):
    """``CitoClient`` de fixture que conta as chamadas de perfil e devolve um perfil fixo.

    A contagem é o que prova a invariante de quota do RF-13: o lutador novo nasce do payload
    já pago (zero chamadas) e só a ambiguidade autoriza gastar uma unidade.
    """

    def __init__(
        self,
        perfil: CitoFighter | None = None,
        *,
        fixture_dir: Path = _FIXTURES,
        budget: CallBudget | None = None,
    ) -> None:
        super().__init__(
            token=settings.cito_api_token,
            base_url=settings.cito_base_url,
            fixture_dir=fixture_dir,
            budget=budget,
        )
        self._perfil = perfil
        self.profile_calls = 0

    def get_fighter(self, slug: str) -> CitoFighter:
        """Conta a chamada e devolve o perfil canned (sem tocar rede nem fixture de disco)."""
        self.profile_calls += 1
        if self._perfil is None:
            raise AssertionError(f"Chamada de perfil inesperada para {slug!r}.")
        return self._perfil


def _seed_fighter(
    session: Session, name: str, *, date_of_birth: date | None = None, source: str = "kaggle"
) -> int:
    """Insere um ``Fighter`` mínimo já persistido e devolve o id materializado."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=date_of_birth,
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=0,
        losses=0,
        draws=0,
        source=source,
    )
    session.add(fighter)
    session.flush()
    return fighter.id


def test_resolve_gap_fighters_creates_new_fighter_without_profile_call(
    db_session: Session,
) -> None:
    """CA-05: lutador inédito nasce do ``profile`` do payload, com zero chamadas de perfil."""
    client = _ClienteEspiao()
    fighters = {
        "anthony-hernandez": CitoFighter(
            slug="anthony-hernandez", name="Anthony Hernandez", nickname="Fluffy", wins=15, losses=4
        )
    }

    ids, desempates = resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    criado = db_session.get(Fighter, ids["anthony-hernandez"])
    assert criado is not None
    assert criado.name == "Anthony Hernandez"
    assert criado.nickname == "Fluffy"
    assert (criado.wins, criado.losses) == (15, 4)
    assert criado.source == "cito"
    assert criado.date_of_birth is None
    assert (desempates, client.profile_calls) == (0, 0)


def test_resolve_gap_fighters_reuses_persisted_fighter_without_profile_call(
    db_session: Session,
) -> None:
    """Lutador já persistido é reusado pelo nome normalizado -- nada é criado nem gasto."""
    existente = _seed_fighter(db_session, "Anthony Hernandez")
    client = _ClienteEspiao()
    fighters = {
        "anthony-hernandez": CitoFighter(slug="anthony-hernandez", name="Anthony Hernandez")
    }

    ids, desempates = resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    assert ids == {"anthony-hernandez": existente}
    assert (desempates, client.profile_calls) == (0, 0)


def test_resolve_gap_fighters_spends_one_profile_call_to_break_a_tie(
    db_session: Session,
) -> None:
    """CA-06: homônimo gasta **uma** chamada de perfil e desempata pela data de nascimento."""
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1989, 10, 1))
    esperado = _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1994, 3, 22))
    client = _ClienteEspiao(
        CitoFighter(slug="bruno-silva", name="Bruno Silva", date_of_birth=date(1994, 3, 22))
    )
    fighters = {"bruno-silva": CitoFighter(slug="bruno-silva", name="Bruno Silva")}

    ids, desempates = resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    assert ids == {"bruno-silva": esperado}
    assert (desempates, client.profile_calls) == (1, 1)
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == 2


def test_resolve_gap_fighters_propagates_unresolvable_ambiguity(db_session: Session) -> None:
    """Ambiguidade que o perfil não resolve propaga -- nunca duplicar nem mesclar em silêncio."""
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1989, 10, 1))
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1994, 3, 22))
    client = _ClienteEspiao(CitoFighter(slug="bruno-silva", name="Bruno Silva"))
    fighters = {"bruno-silva": CitoFighter(slug="bruno-silva", name="Bruno Silva")}

    with pytest.raises(AmbiguousFighterMatchError):
        resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    assert client.profile_calls == 1
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == 2


# --------------------------------------------------------------------------- #
# CA-03 -- ingestão de um evento do gap (uma única chamada de stats).
# --------------------------------------------------------------------------- #


def _ingest_payload_real(
    session: Session, tmp_path: Path, *, budget: CallBudget | None = None
) -> tuple[GapEventResult, CallBudget]:
    """Ingere o evento do payload real e devolve o resultado e o orçamento consumido."""
    orcamento = budget or CallBudget(limit=10)
    resultado = ingest_gap_event(
        session,
        _item(_PAYLOAD_REAL),
        _fixture_client(orcamento),
        EventStatsCache(tmp_path),
        today=_HOJE,
    )
    return resultado, orcamento


def test_ingest_gap_event_persists_the_whole_card_with_one_call(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-03: o evento inteiro (card + totais + rounds) entra gastando uma única chamada.

    Treze lutas, vinte e seis cantos e cinquenta e dois rounds do payload real, tudo com
    ``source="cito"`` -- e o ``CallBudget`` marca exatamente uma unidade.
    """
    resultado, orcamento = _ingest_payload_real(db_session, tmp_path)

    evento = db_session.scalars(select(Event)).one()
    assert evento.name == "UFC Fight Night: Hernandez vs Rodrigues"
    assert evento.date == date(2026, 8, 22)
    assert evento.source == "cito"
    assert evento.cito_slug == _PAYLOAD_REAL
    assert evento.cito_event_id == "9f8aa2a4-286b-4385-b8a1-9324daa55485"
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 13
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == 26
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 52
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == 26
    assert orcamento.used == 1
    assert resultado.profile_calls_used == 0


def test_ingest_gap_event_maps_result_and_card_context(db_session: Session, tmp_path: Path) -> None:
    """A luta principal guarda resultado, categoria e contexto de card vindos do mesmo payload."""
    _ingest_payload_real(db_session, tmp_path)

    vencedor = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == normalize_name("Gregory Rodrigues"))
    ).one()
    principal = db_session.scalars(select(Bout).where(Bout.bout_order == 1001)).one()

    assert principal.method is BoutMethod.DECISION
    assert principal.round == 5
    assert principal.ending_time_seconds == 300
    assert principal.weight_class == "Middleweight"
    assert principal.winner_id == vencedor.id
    assert principal.card_section == "Main Card"
    assert principal.source == "cito"


def test_ingest_gap_event_keeps_granular_totals_per_corner(
    db_session: Session, tmp_path: Path
) -> None:
    """Os totais por canto entram granulares, com ``reversals`` e tempo de controle não-nulos.

    O caminho do M1 (``upsert_bout_fighters``) descartaria estes campos: o ``CitoBoutStats``
    que ele consome não tem ``reversals`` nem os oito splits que o payload já traz.
    """
    _ingest_payload_real(db_session, tmp_path)

    canto = db_session.scalars(
        select(BoutFighter)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Fighter.name_normalized == normalize_name("Anthony Hernandez"))
    ).one()

    assert canto.knockdowns == 0
    assert (canto.sig_strikes_landed, canto.sig_strikes_attempted) == (90, 159)
    assert (canto.total_strikes_landed, canto.total_strikes_attempted) == (111, 184)
    assert (canto.takedowns_landed, canto.takedowns_attempted) == (3, 29)
    assert (canto.head_landed, canto.body_landed, canto.leg_landed) == (89, 1, 0)
    assert (canto.distance_landed, canto.clinch_landed, canto.ground_landed) == (64, 20, 6)
    assert canto.reversals == 0
    assert canto.control_time_seconds == 640
    assert canto.source == "cito"


def test_ingest_gap_event_writes_rounds_for_the_corner(db_session: Session, tmp_path: Path) -> None:
    """O round-a-round é gravado no canto certo, um por round, com ``source="cito"``."""
    _ingest_payload_real(db_session, tmp_path)

    canto = db_session.scalars(
        select(BoutFighter)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Fighter.name_normalized == normalize_name("Anthony Hernandez"))
    ).one()
    rounds = db_session.scalars(
        select(BoutFighterRound)
        .where(BoutFighterRound.bout_fighter_id == canto.id)
        .order_by(BoutFighterRound.round)
    ).all()

    assert [linha.round for linha in rounds] == [1, 2, 3, 4, 5]
    assert (rounds[0].sig_strikes_landed, rounds[0].sig_strikes_attempted) == (10, 19)
    assert rounds[0].control_time_seconds == 168
    assert {linha.source for linha in rounds} == {"cito"}


def test_ingest_gap_event_creates_fighters_from_the_payload(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-05: os 26 lutadores do card nascem do ``profile`` embutido, sem chamada de perfil."""
    resultado, orcamento = _ingest_payload_real(db_session, tmp_path)

    lutador = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == normalize_name("Anthony Hernandez"))
    ).one()
    assert lutador.nickname == "Fluffy"
    assert (lutador.wins, lutador.losses, lutador.draws) == (15, 4, 0)
    assert lutador.source == "cito"
    assert resultado.fighters_created == 26
    assert orcamento.used == 1


# --------------------------------------------------------------------------- #
# CA-04 / CA-06 / CA-08 -- execução completa, idempotência e resumo.
# --------------------------------------------------------------------------- #


def _contagens(session: Session) -> tuple[int, int, int, int, int]:
    """Contagem das cinco tabelas que a slice escreve (base da asserção de idempotência)."""
    return (
        session.scalar(select(func.count()).select_from(Event)) or 0,
        session.scalar(select(func.count()).select_from(Bout)) or 0,
        session.scalar(select(func.count()).select_from(BoutFighter)) or 0,
        session.scalar(select(func.count()).select_from(BoutFighterRound)) or 0,
        session.scalar(select(func.count()).select_from(Fighter)) or 0,
    )


def _run_gap(
    session: Session, tmp_path: Path, *, budget: CallBudget | None = None
) -> tuple[GapSyncSummary, CallBudget]:
    """Roda o fechamento do gap contra o conjunto de fixtures da slice (catálogo + stats)."""
    orcamento = budget or CallBudget(limit=20)
    resumo = run_gap_sync(
        session,
        _ClienteEspiao(fixture_dir=_GAP_FIXTURES, budget=orcamento),
        orcamento,
        EventStatsCache(tmp_path),
        today=_HOJE,
    )
    return resumo, orcamento


def test_run_gap_sync_ingests_the_missing_events_in_chronological_order(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-08: os dois eventos do gap entram, e o resumo reporta deltas, quota e defasagem."""
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )

    resumo, orcamento = _run_gap(db_session, tmp_path)

    persistidos = db_session.scalars(select(Event).order_by(Event.date)).all()
    assert [evento.cito_slug for evento in persistidos] == [
        "ufc-328",
        "ufc-freedom-250",
        "ufc-fight-night-august-22-2026",
    ]
    assert resumo.events_ingested == 2
    assert resumo.events_skipped == 0
    assert resumo.bouts.inserted == 3
    assert resumo.bout_fighters.inserted == 6
    assert resumo.rounds_inserted == 20  # 8 rounds do card de junho + 12 do de agosto
    assert resumo.fighters_created == 6
    assert resumo.profile_calls_used == 0
    assert resumo.stats_calls_used == 2
    assert resumo.source == "cito"
    assert orcamento.used == 3  # 1 página de catálogo + 2 chamadas de stats


def test_run_gap_sync_reports_days_behind_after_closing_the_gap(
    db_session: Session, tmp_path: Path
) -> None:
    """A defasagem cai a zero: o último persistido passa a ser o último realizado do catálogo."""
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )

    resumo, _ = _run_gap(db_session, tmp_path)

    assert resumo.latest_catalog_date == date(2026, 8, 22)
    assert resumo.latest_persisted_date == date(2026, 8, 22)
    assert resumo.days_behind == 0


def test_run_gap_sync_is_idempotent_and_spends_no_stats_call_on_rerun(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-04: rerun não muda contagem, zera os deltas e **não** gasta chamada de stats.

    O evento já persistido é descartado na descoberta, antes do fetch -- é isso que torna a
    reexecução gratuita em quota, e não o cache (que só evita re-baixar o que já se pagou).

    ``events_skipped`` cai a 1 (e não 2) porque o corte avançou: fechado o gap, a janela passa
    a começar no evento recém-ingerido, e o anterior deixa de ser sequer candidato. Estreitar a
    janela é o que faz a execução seguinte custar uma página de catálogo em vez do catálogo.
    """
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )
    _run_gap(db_session, tmp_path)
    antes = _contagens(db_session)

    resumo, _ = _run_gap(db_session, tmp_path)

    assert _contagens(db_session) == antes
    assert resumo.events_ingested == 0
    assert resumo.events_skipped == 1
    assert (resumo.bouts.inserted, resumo.bout_fighters.inserted) == (0, 0)
    assert resumo.rounds_inserted == 0
    assert resumo.fighters_created == 0
    assert resumo.stats_calls_used == 0


def _seed_homonimos_ambiguos(session: Session) -> None:
    """Semeia dois 'Anthony Hernandez' com DOBs distintas: a ambiguidade que o perfil não quebra."""
    _seed_fighter(session, "Anthony Hernandez", date_of_birth=date(1993, 12, 26))
    _seed_fighter(session, "Anthony Hernandez", date_of_birth=date(1990, 1, 1))


def test_ingest_gap_event_propagates_unresolvable_ambiguity(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-06: no evento, a ambiguidade irresolvível **propaga** e o SAVEPOINT não deixa parcial."""
    _seed_homonimos_ambiguos(db_session)
    orcamento = CallBudget(limit=20)
    client = _ClienteEspiao(
        fixture_dir=_GAP_FIXTURES,
        budget=orcamento,
        perfil=CitoFighter(slug="anthony-hernandez", name="Anthony Hernandez"),
    )

    with pytest.raises(AmbiguousFighterMatchError) as excinfo:
        ingest_gap_event(
            db_session, _item(_PAYLOAD_REAL), client, EventStatsCache(tmp_path), today=_HOJE
        )

    assert db_session.scalar(select(func.count()).select_from(Event)) == 0
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0
    assert client.profile_calls == 1
    # A quota gasta antes da falha viaja na exceção: um evento pulado ainda custou quota, e um
    # resumo que a esconde reporta consumo menor que o real.
    assert isinstance(excinfo.value, GapAmbiguityError)
    assert (excinfo.value.profile_calls_used, excinfo.value.stats_calls_used) == (1, 1)


def test_run_gap_sync_skips_the_ambiguous_event_and_keeps_going(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-06 no lote: o evento ambíguo é pulado e contado; os demais entram normalmente.

    A ambiguidade continua **falhando alto** dentro do evento (o SAVEPOINT reverte tudo dele),
    mas no lote ela é capturada, contada e reportada -- mesmo tratamento que a Sprint 007-03
    deu ao evento ambíguo do catálogo (RF-05). Deixá-la derrubar a execução inteira jogaria
    fora quota já paga e impediria o gap de fechar para sempre, já que a reexecução pararia no
    mesmo ponto.
    """
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )
    _seed_homonimos_ambiguos(db_session)
    orcamento = CallBudget(limit=20)
    client = _ClienteEspiao(
        fixture_dir=_GAP_FIXTURES,
        budget=orcamento,
        perfil=CitoFighter(slug="anthony-hernandez", name="Anthony Hernandez"),
    )

    resumo = run_gap_sync(db_session, client, orcamento, EventStatsCache(tmp_path), today=_HOJE)

    persistidos = db_session.scalars(select(Event).order_by(Event.date)).all()
    assert [evento.cito_slug for evento in persistidos] == ["ufc-328", "ufc-freedom-250"]
    assert resumo.events_ingested == 1
    assert resumo.events_ambiguous == ("ufc-fight-night-august-22-2026",)
    assert client.profile_calls == 1


def test_run_gap_sync_honours_the_max_events_ceiling(db_session: Session, tmp_path: Path) -> None:
    """A sondagem processa só os N eventos mais antigos; o cache faz o lote reaproveitá-los."""
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )
    orcamento = CallBudget(limit=20)

    resumo = run_gap_sync(
        db_session,
        _ClienteEspiao(fixture_dir=_GAP_FIXTURES, budget=orcamento),
        orcamento,
        EventStatsCache(tmp_path),
        today=_HOJE,
        max_events=1,
    )

    assert resumo.events_ingested == 1
    assert resumo.stats_calls_used == 1
    assert [
        evento.cito_slug for evento in db_session.scalars(select(Event).order_by(Event.date))
    ] == [
        "ufc-328",
        "ufc-freedom-250",
    ]


# --------------------------------------------------------------------------- #
# CA-07 -- gate humano do CLI (reuso de ``ingestion.cito.gate``).
# --------------------------------------------------------------------------- #


def test_main_against_the_real_network_without_confirmation_spends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-07: sem ``--confirmar-gasto-de-quota`` o CLI aborta antes da primeira unidade de quota.

    Dois spies (catálogo e stats) provam ZERO chamadas: o gate bloqueia antes de instanciar o
    cliente e antes de abrir a sessão. O comando encerra com exit code diferente de zero.
    """
    chamadas: list[str] = []

    def _spy_catalog(self: CitoClient, **kwargs: object) -> list[CitoCatalogItem]:
        chamadas.append("catalog")
        raise AssertionError("a rede não deveria ser tocada sem o gate confirmado")

    def _spy_stats(self: CitoClient, slug: str) -> CitoEventStats:
        chamadas.append(slug)
        raise AssertionError("a rede não deveria ser tocada sem o gate confirmado")

    monkeypatch.setattr(CitoClient, "fetch_event_catalog", _spy_catalog)
    monkeypatch.setattr(CitoClient, "fetch_event_stats", _spy_stats)

    with (
        caplog.at_level(logging.ERROR, logger="ingestion.cito.gap_sync"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--call-budget", "5", "--cache-dir", str(tmp_path)])

    assert excinfo.value.code != 0
    assert chamadas == []
    assert "gate" in caplog.text.lower()


def test_main_reuses_the_shared_human_gate() -> None:
    """O gate é o módulo compartilhado (Slice 03), não uma segunda cópia da regra.

    O modo fixture (JSON local, zero quota) não exige confirmação; a rede real, sim.
    """
    with pytest.raises(HumanGateNotConfirmedError):
        enforce_human_gate(fixture=False, confirmed=False)
    enforce_human_gate(fixture=True, confirmed=False)


def test_parse_args_defaults_to_the_slice_fixture_set() -> None:
    """O ``--fixture-dir`` default aponta para o conjunto da slice (catálogo + stats juntos)."""
    args = _parse_args([])

    assert args.fixture is False
    assert args.confirmar_gasto_de_quota is False
    assert args.fixture_dir.name == "gap_sync"
    assert args.max_events is None


# --------------------------------------------------------------------------- #
# Vocabulário de método: a sondagem de 2026-09-01 achou DUAS grafias no mesmo catálogo.
# --------------------------------------------------------------------------- #


def _bloco_com_metodo(token: str | None) -> CitoBoutBlock:
    """Uma luta do card com o token de método informado e os dois cantos."""
    return CitoBoutBlock.model_validate(
        {
            "id": "luta-de-teste",
            "method": token,
            "winnerFighterSlug": "a-fighter",
            "fighters": [
                {"fighterSlug": "a-fighter", "fighterName": "A Fighter", "corner": "red"},
                {"fighterSlug": "b-fighter", "fighterName": "B Fighter", "corner": "blue"},
            ],
        }
    )


@pytest.mark.parametrize(
    ("token", "esperado"),
    [
        ("U-DEC", BoutMethod.DECISION),
        ("S-DEC", BoutMethod.DECISION),
        ("M-DEC", BoutMethod.DECISION),
        ("SUB", BoutMethod.SUBMISSION),
        ("KO/TKO", BoutMethod.KO_TKO),
        ("CNC", BoutMethod.NO_CONTEST),
        ("Decision - Unanimous", BoutMethod.DECISION),
        ("Submission", BoutMethod.SUBMISSION),
        ("Could Not Continue", BoutMethod.NO_CONTEST),
        ("TKO - Doctor's Stoppage", BoutMethod.KO_TKO),
        ("Other", BoutMethod.NO_CONTEST),
    ],
)
def test_map_bout_core_understands_both_method_spellings(token: str, esperado: BoutMethod) -> None:
    """As duas grafias de método da Cito mapeiam para o mesmo desfecho.

    A sondagem de 2026-09-01 mediu que o catálogo serve **as duas**: 'UFC 320' e o Fight Night
    de setembro/2025 trazem a forma longa ('Decision - Unanimous'), enquanto
    'ufc-fight-night-lopes-vs-silva' traz os códigos curtos do ufcstats ('U-DEC', 'S-DEC',
    'SUB', 'CNC'). Sem os curtos, uma decisão unânime viraria ``NO_CONTEST`` -- o rótulo que o
    modelo treina, gravado errado e em silêncio.
    """
    (bout,) = map_stats_to_bouts(_stats_com_bouts([_bloco_com_metodo(token)]))

    assert map_bout_core(bout)["method"] is esperado


def test_ingest_gap_event_refuses_an_unknown_method_token(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token de método desconhecido **aborta o evento** em vez de gravar ``NO_CONTEST`` falso.

    Degradar em silêncio é o pior desfecho possível aqui: o método é o rótulo do preditivo, e
    um 'no contest' inventado no lugar de uma finalização é dado falso com aparência de dado.
    O SAVEPOINT reverte só este evento; o operador acrescenta o token ao mapa e reexecuta sem
    gastar quota (o payload já está no cache).
    """
    original = CitoClient.fetch_event_stats

    def _com_metodo_exotico(self: CitoClient, slug: str) -> CitoEventStats:
        stats = original(self, slug)
        return stats.model_copy(
            update={
                "bouts": [b.model_copy(update={"method": "Vitória Moral"}) for b in stats.bouts]
            }
        )

    monkeypatch.setattr(CitoClient, "fetch_event_stats", _com_metodo_exotico)

    with pytest.raises(GapSyncError, match="Vitória Moral"):
        ingest_gap_event(
            db_session,
            _item(_PAYLOAD_REAL),
            _fixture_client(CallBudget(limit=10)),
            EventStatsCache(tmp_path),
            today=_HOJE,
        )

    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0


# --------------------------------------------------------------------------- #
# RF-13 -- desempate por idade (decisão do humano de 2026-09-01).
#
# O perfil real devolve ``birthDate`` nulo e publica ``age``; sem isso o homônimo travaria a
# ingestão do card inteiro. A chamada de perfil continua sendo gasta APENAS na ambiguidade.
# --------------------------------------------------------------------------- #


def test_resolve_gap_fighters_breaks_the_tie_by_age_when_the_profile_has_no_dob(
    db_session: Session,
) -> None:
    """Perfil sem DOB mas com ``age`` desempata o homônimo, gastando uma única chamada.

    Reproduz o caso real: os dois 'Bruno Silva' da base (1990-03-16 e 1989-07-13) e o perfil da
    Cito informando 36 anos, que casa só com o primeiro.
    """
    esperado = _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1990, 3, 16))
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1989, 7, 13))
    client = _ClienteEspiao(CitoFighter(slug="bruno-silva", name="Bruno Silva", age=36))
    fighters = {"bruno-silva": CitoFighter(slug="bruno-silva", name="Bruno Silva")}

    ids, desempates = resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    assert ids == {"bruno-silva": esperado}
    assert (desempates, client.profile_calls) == (1, 1)
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == 2


def test_resolve_gap_fighters_fails_high_when_age_does_not_discriminate(
    db_session: Session,
) -> None:
    """Idade que casa com mais de um homônimo falha alto -- nunca o 'mais provável'.

    Os dois nasceram em anos que dão a mesma idade corrente: sem discriminação, o evento é
    pulado e registrado, e nenhum lutador é criado ou fundido.
    """
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1990, 3, 16))
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1989, 9, 5))
    client = _ClienteEspiao(CitoFighter(slug="bruno-silva", name="Bruno Silva", age=36))
    fighters = {"bruno-silva": CitoFighter(slug="bruno-silva", name="Bruno Silva")}

    with pytest.raises(AmbiguousFighterMatchError):
        resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    assert client.profile_calls == 1
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == 2


def test_resolve_gap_fighters_fails_high_when_the_profile_has_no_age(
    db_session: Session,
) -> None:
    """Perfil sem DOB e sem ``age`` não desempata nada: falha alto, como antes."""
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1990, 3, 16))
    _seed_fighter(db_session, "Bruno Silva", date_of_birth=date(1989, 7, 13))
    client = _ClienteEspiao(CitoFighter(slug="bruno-silva", name="Bruno Silva"))
    fighters = {"bruno-silva": CitoFighter(slug="bruno-silva", name="Bruno Silva")}

    with pytest.raises(AmbiguousFighterMatchError):
        resolve_gap_fighters(db_session, fighters, client, today=_HOJE)

    assert client.profile_calls == 1


def test_get_fighter_expoe_a_idade_do_perfil_real() -> None:
    """O ``age`` do payload real chega ao DTO de domínio -- é o insumo do desempate."""
    fighter = _fixture_client().get_fighter("bruno-silva")

    assert fighter.date_of_birth is None
    assert fighter.age == 36


def test_run_gap_sync_accepts_an_explicit_window_start(db_session: Session, tmp_path: Path) -> None:
    """``date_from`` explícito reabre a janela para trás e recupera evento deixado para trás.

    O corte default é ``max(events.date)``, o que é certo para o uso normal mas orfana um
    evento pulado: fechado o gap à frente dele, ele passa a ficar atrás do corte e nenhuma
    reexecução o reencontra. O parâmetro devolve esse controle ao operador -- mesmo
    ``--from``/``--to`` que a sincronização de catálogo já expõe. O descarte do já persistido
    continua valendo, então reabrir a janela não reingere nada e não gasta quota à toa.
    """
    _seed_persisted_event(
        db_session,
        name="UFC Fight Night: Hernandez vs Rodrigues",
        event_date=date(2026, 8, 22),
        cito_slug=_PAYLOAD_REAL,
    )
    orcamento = CallBudget(limit=20)

    resumo = run_gap_sync(
        db_session,
        _ClienteEspiao(fixture_dir=_GAP_FIXTURES, budget=orcamento),
        orcamento,
        EventStatsCache(tmp_path),
        today=_HOJE,
        date_from=date(2026, 1, 1),
    )

    persistidos = db_session.scalars(select(Event).order_by(Event.date)).all()
    assert [evento.cito_slug for evento in persistidos] == [
        "ufc-freedom-250",
        _PAYLOAD_REAL,
    ]
    assert resumo.events_ingested == 1
    assert resumo.latest_persisted_date == date(2026, 8, 22)


def test_parse_args_exposes_the_window_start() -> None:
    """A CLI expõe ``--from`` (default: o último evento persistido)."""
    assert _parse_args([]).date_from is None
    assert _parse_args(["--from", "2025-10-01"]).date_from == date(2025, 10, 1)


# --------------------------------------------------------------------------- #
# Canto atribuído (fallback) -- trava do vazamento de rótulo (decisão do humano, 2026-09-01).
#
# O `corner` da Cito é o DESFECHO, não o canto de caminhada: 1.882 de 1.888 lutas decididas
# trazem o vencedor em `red`, contra 64,6% na base do Kaggle, que é a taxa real do UFC.
# Persistir aquele rótulo fixava o alvo do modelo (`target_winner_corner`) em `red` para toda
# luta vinda da Cito. Estes testes existem para que ninguém "conserte" o código de volta.
#
# Continuam valendo depois da rodada de correção de 2026-09-01: eles agora exercitam o
# **fallback**, sobre um card real cuja arte não traz o lado -- que é exatamente o caminho em
# que o vazamento poderia voltar a entrar.
# --------------------------------------------------------------------------- #


def test_ingest_gap_event_derives_the_corner_from_the_normalized_name_not_from_the_payload(
    db_session: Session, tmp_path: Path
) -> None:
    """Sem lado na arte, o canto sai do menor nome normalizado -- nunca do `corner` da Cito.

    Critério independente do resultado **e** da fonte: é isso que transforma o rótulo em ruído
    em vez de viés. Um teste que só verificasse "gravou um canto" passaria com o vazamento
    intacto.
    """
    _ingest_payload_sem_arte(db_session, tmp_path)

    cantos_por_luta: dict[int, dict[Corner, str]] = {}
    for canto in db_session.scalars(select(BoutFighter)):
        lutador = db_session.get(Fighter, canto.fighter_id)
        assert lutador is not None
        cantos_por_luta.setdefault(canto.bout_id, {})[canto.corner] = lutador.name_normalized

    assert len(cantos_por_luta) == 6
    for bout_id, por_canto in cantos_por_luta.items():
        assert set(por_canto) == {Corner.RED, Corner.BLUE}, bout_id
        assert por_canto[Corner.RED] < por_canto[Corner.BLUE], bout_id


def test_ingest_gap_event_does_not_leak_the_winner_into_the_red_corner(
    db_session: Session, tmp_path: Path
) -> None:
    """A taxa de vitória do vermelho não pode ser degenerada, mesmo com o payload enviesado.

    Nas seis lutas deste card real a Cito põe o vencedor em `red` em **todas**. Se o canto
    viesse do payload, a taxa seria 100% e o alvo do modelo ficaria perfeitamente preditivo.
    Atribuído pelo nome normalizado, o vencedor cai no vermelho aproximadamente metade das
    vezes (quatro de seis aqui).
    """
    _ingest_payload_sem_arte(db_session, tmp_path)

    lutas = db_session.scalars(select(Bout).where(Bout.winner_id.is_not(None))).all()
    vermelho_venceu = [
        luta
        for luta in lutas
        if db_session.scalars(
            select(BoutFighter).where(
                BoutFighter.bout_id == luta.id, BoutFighter.corner == Corner.RED
            )
        )
        .one()
        .fighter_id
        == luta.winner_id
    ]

    assert 0 < len(vermelho_venceu) < len(lutas)


def test_ingest_gap_event_keeps_the_winner_correct_after_reassigning_corners(
    db_session: Session, tmp_path: Path
) -> None:
    """Reatribuir o canto **não** mexe em quem venceu: o vencedor vem do slug, não do canto.

    Se o ``winner_id`` fosse derivado do canto reatribuído, a correção do vazamento trocaria o
    vencedor de metade das lutas -- corrupção muito pior que a que ela conserta.
    """
    _ingest_payload_real(db_session, tmp_path)

    vencedor = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == normalize_name("Gregory Rodrigues"))
    ).one()
    principal = db_session.scalars(select(Bout).where(Bout.bout_order == 1001)).one()

    assert principal.winner_id == vencedor.id


def test_ingest_gap_event_does_not_park_every_newcomer_in_the_same_corner(
    db_session: Session, tmp_path: Path
) -> None:
    """Lutador inédito na base não pode cair sempre no mesmo canto.

    Esta é a trava do defeito que o critério por ``fighter_id`` tinha: como os ids do seed vão
    até 2.611 e os novos começam depois, o estreante **sempre** perdia a comparação e ia para o
    azul -- 133 de 133 no lote real. Isso não é canto neutro, é um marcador de "estreante na
    base" disfarçado de canto, e contamina as features (``layoff_days_diff`` médio de +57,8 nas
    lutas da Cito contra -11,7 nas do Kaggle).

    O cenário: metade dos cantos já existe na base (semeados alternando o índice do array do
    payload, para não correlacionar com o desfecho nem com a ordem alfabética) e a outra metade
    é criada pela ingestão. A distribuição dos estreantes entre vermelho e azul não pode ser
    degenerada.
    """
    stats = _stats_sem_arte()
    for indice, bloco in enumerate(stats.bouts):
        veterano = bloco.fighters[indice % 2]
        nome = (veterano.profile.name if veterano.profile else veterano.fighter_name) or ""
        _seed_fighter(db_session, nome)
    veteranos = {fighter_id for (fighter_id,) in db_session.execute(select(Fighter.id))}

    _ingest_payload_sem_arte(db_session, tmp_path)

    estreantes_no_vermelho = sum(
        1
        for canto in db_session.scalars(select(BoutFighter).where(BoutFighter.corner == Corner.RED))
        if canto.fighter_id not in veteranos
    )

    assert 0 < estreantes_no_vermelho < 6


# --------------------------------------------------------------------------- #
# Canto REAL, recuperado da arte oficial do card (rodada de correção, 2026-09-01).
#
# O sufixo `_L_`/`_R_` do nome do arquivo da arte é o canto de caminhada. Medido contra o canto
# verdadeiro do Kaggle nos 25 eventos semeados de 2025: 306 cantos com sufixo, 305 acertos
# (99,7%) com L=vermelho e R=azul -- enquanto o campo `corner` da API acerta 289 de 513 (56,3%).
# A atribuição determinística continua existindo, agora como **fallback**.
# --------------------------------------------------------------------------- #

# Prefixo real das URLs de arte de card da Cito (o caminho não importa; o sufixo está no nome).
_ARTE = "https://ufc.com/images/styles/event_fight_card_upper_body_of_standing_athlete/s3/2026-08/"

# Payload REAL de um evento cuja arte **não** traz o lado (o padrão é por evento: ou todas as
# lutas do card têm sufixo, ou nenhuma tem). É o card que exercita o fallback determinístico.
_PAYLOAD_SEM_ARTE = "ufc-fight-night-whittaker-vs-de-ridder"


def _stats_sem_arte() -> CitoEventStats:
    """As stats do card real **sem** lado na arte (6 lutas, sem box-score), lidas da fixture."""
    return _fixture_client().fetch_event_stats(_PAYLOAD_SEM_ARTE)


@pytest.mark.parametrize(
    ("arquivo", "esperado"),
    [
        ("HERNANDEZ_ANTHONY_L_08-22.png?itok=fVO3QnPN", "L"),
        ("TORRES_MANUEL_R_06-27.png?itok=OkqxReP6", "R"),
        ("CORTES_ACOSTA_WALDO_L.png?itok=seL9kGfA", "L"),
        ("STOLTZFUS_DUSTIN-L_05-17.png?itok=TRnfXkfq", "L"),
        ("GANE_CIRYL_R_BELT_01-22.png?itok=_OPJ_6G0", "R"),
        ("NAKAMURA_RINYA_L%298_26.png?itok=LZEKuEBM", "L"),
        ("MINGYANG_ZHANG_05-30.png?itok=VzfxgMon", None),
        ("SILVA_L_R_08-22.png", None),
    ],
)
def test_art_side_reads_the_side_from_the_official_art_filename(
    arquivo: str, esperado: str | None
) -> None:
    """O lado sai do nome do arquivo da arte -- nas variações reais que a Cito serve.

    Os seis primeiros casos são nomes de arquivo **reais** da captura de 2026-09-01: o padrão
    dominante (`_L_08-22`), o sufixo no fim sem underscore, o separado por hífen, a variante com
    'BELT' e o percent-encoded. Arte sem lado devolve ``None`` (o card cai no fallback), e nome
    com os dois lados é tratado como ausência: adivinhar seria pior que não saber.
    """
    assert art_side(_ARTE + arquivo) == esperado


def test_art_side_treats_a_missing_image_as_absence() -> None:
    """Canto sem arte no payload não tem lado -- ausência explícita, nunca um lado default."""
    assert art_side(None) is None


def _bloco_com_arte(alfa: str | None, zeta: str | None) -> CitoBoutBlock:
    """Uma luta do card com as artes informadas; 'alfa' vence a ordem alfabética de 'zeta'.

    O par é nomeado assim de propósito: no fallback determinístico o vermelho é sempre 'alfa',
    então qualquer teste em que o vermelho seja 'zeta' só pode ter vindo da arte.
    """
    return CitoBoutBlock.model_validate(
        {
            "id": "luta-com-arte",
            "method": "KO/TKO",
            "winnerFighterSlug": "zeta-fighter",
            "fighters": [
                {
                    "fighterSlug": "alfa-fighter",
                    "fighterName": "Alfa Fighter",
                    "corner": "blue",
                    "imageUrl": alfa,
                },
                {
                    "fighterSlug": "zeta-fighter",
                    "fighterName": "Zeta Fighter",
                    "corner": "red",
                    "imageUrl": zeta,
                },
            ],
        }
    )


_CHAVES_ALFA_ZETA = {"alfa-fighter": ("alfa fighter", 1), "zeta-fighter": ("zeta fighter", 2)}


def _atribui(alfa: str | None, zeta: str | None) -> tuple[CitoBout, CornerOrigin]:
    """Roda ``assign_corners`` sobre a luta sintética com as artes informadas."""
    bloco = _bloco_com_arte(alfa, zeta)
    (bout,) = map_stats_to_bouts(_stats_com_bouts([bloco]))
    return assign_corners(bout, bloco, _CHAVES_ALFA_ZETA)


def test_assign_corners_uses_the_real_corner_when_both_sides_agree() -> None:
    """Os dois lados com sufixos coerentes -> canto real: `_L_` é o vermelho, `_R_` o azul.

    O caso é o oposto do fallback (que poria 'alfa' no vermelho), então o vermelho ser 'zeta'
    prova que o canto veio da arte e não da ordem alfabética.
    """
    bout, origem = _atribui(_ARTE + "ALFA_FIGHTER_R_08-22.png", _ARTE + "ZETA_FIGHTER_L_08-22.png")

    assert [canto.slug for canto in bout.corners] == ["zeta-fighter", "alfa-fighter"]
    assert origem is CornerOrigin.ART


def test_assign_corners_infers_the_other_side_from_a_single_suffix() -> None:
    """Um lado só com sufixo basta: o outro recebe o canto oposto (o card tem dois cantos)."""
    bout, origem = _atribui(_ARTE + "ALFA_FIGHTER_R_08-22.png", None)

    assert [canto.slug for canto in bout.corners] == ["zeta-fighter", "alfa-fighter"]
    assert origem is CornerOrigin.ART


def test_assign_corners_falls_back_when_both_sides_carry_the_same_suffix() -> None:
    """Conflito (os dois com o mesmo sufixo) cai para o determinístico, nunca escolhe um lado.

    O caso é real: em 'ufc-fight-night-july-12-2025' a arte de Derrick Lewis é a do evento
    anterior (`LEWIS_DERRICK_R_06-14`) e a de Tallison Teixeira é a do card
    (`TEIXEIRA_TALLISON_R_07-12`) -- as duas com `_R_`. Casar no escuro poria os dois no azul.
    """
    bout, origem = _atribui(
        _ARTE + "LEWIS_DERRICK_R_06-14.png?itok=dyVF9m0E",
        _ARTE + "TEIXEIRA_TALLISON_R_07-12.png?itok=OkqxReP6",
    )

    assert [canto.slug for canto in bout.corners] == ["alfa-fighter", "zeta-fighter"]
    assert origem is CornerOrigin.ASSIGNED_CONFLICTING_ART


def test_assign_corners_falls_back_when_no_side_carries_a_suffix() -> None:
    """Sem sufixo nenhum, o canto volta a ser atribuído pelo nome normalizado (o fallback)."""
    bout, origem = _atribui(_ARTE + "ALFA_FIGHTER_08-22.png", None)

    assert [canto.slug for canto in bout.corners] == ["alfa-fighter", "zeta-fighter"]
    assert origem is CornerOrigin.ASSIGNED_NO_ART


def test_assign_corners_never_touches_the_winner() -> None:
    """Recuperar o canto muda em que lado o vencedor cai, nunca quem ele é (vem do slug)."""
    real, _ = _atribui(_ARTE + "ALFA_FIGHTER_R_08-22.png", _ARTE + "ZETA_FIGHTER_L_08-22.png")
    atribuido, _ = _atribui(None, None)

    assert real.winner_slug == atribuido.winner_slug == "zeta-fighter"


def test_ingest_gap_event_persists_the_real_corner_from_the_art(
    db_session: Session, tmp_path: Path
) -> None:
    """As 13 lutas do card real têm canto recuperável: quem tem `_L_` na arte fica no vermelho."""
    resultado, _ = _ingest_payload_real(db_session, tmp_path)

    esperados = {
        normalize_name((canto.profile.name if canto.profile else canto.fighter_name) or "")
        for bloco in _real_stats().bouts
        for canto in bloco.fighters
        if art_side(canto.image_url) == "L"
    }
    vermelhos: set[str] = set()
    for canto_db in db_session.scalars(select(BoutFighter).where(BoutFighter.corner == Corner.RED)):
        lutador = db_session.get(Fighter, canto_db.fighter_id)
        assert lutador is not None
        vermelhos.add(lutador.name_normalized)

    assert len(esperados) == 13
    assert vermelhos == esperados
    assert resultado.corner_origins == {CornerOrigin.ART: 13}


def _item_sem_arte() -> CitoCatalogItem:
    """Item de catálogo do card sem arte (os campos são os do próprio evento na fixture)."""
    return CitoCatalogItem.model_validate(
        {
            "id": "0f357aa7-5664-498b-9e11-03b8767778d7",
            "slug": _PAYLOAD_SEM_ARTE,
            "title": "UFC Fight Night: Whittaker vs de Ridder",
            "status": "completed",
            "startsAt": "2025-07-26T19:00:00.000Z",
            "eventDate": "2025-07-26",
        }
    )


def _ingest_payload_sem_arte(session: Session, tmp_path: Path) -> GapEventResult:
    """Ingere o card real **sem** sufixo de lado na arte -- o caminho do fallback."""
    return ingest_gap_event(
        session,
        _item_sem_arte(),
        _fixture_client(CallBudget(limit=10)),
        EventStatsCache(tmp_path),
        today=_HOJE,
    )


def test_ingest_gap_event_falls_back_to_the_assigned_corner_without_art(
    db_session: Session, tmp_path: Path
) -> None:
    """Card sem sufixo nenhum: as seis lutas entram com canto atribuído, e o resumo diz isso."""
    resultado = _ingest_payload_sem_arte(db_session, tmp_path)

    assert resultado.corner_origins == {CornerOrigin.ASSIGNED_NO_ART: 6}
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 6


def test_run_gap_sync_reports_the_corner_origin_of_every_bout(
    db_session: Session, tmp_path: Path
) -> None:
    """O resumo do lote agrega a procedência do canto luta a luta (CA-08).

    Sem esse número não há como avaliar depois o impacto no modelo: ``bout_fighters.source`` é
    ``"cito"`` nas duas origens e não distingue canto real de canto atribuído.
    """
    _seed_persisted_event(
        db_session,
        name="UFC 328: Chimaev vs. Strickland",
        event_date=date(2026, 5, 9),
        cito_slug="ufc-328",
    )

    resumo, _ = _run_gap(db_session, tmp_path)

    assert resumo.corner_origins == {CornerOrigin.ART: 3}
    assert sum(resumo.corner_origins.values()) == resumo.bouts.inserted


# --------------------------------------------------------------------------- #
# CA-09 da Sprint 008-04 -- aviso quando o lote fecha com canto ATRIBUÍDO por nós
# dentro da janela da fonte oficial. O fallback e a arte continuam no código: o que
# muda é que deixar um canto provisório na janela passa a ser dito alto.
# --------------------------------------------------------------------------- #

_DENTRO_DA_JANELA = date(2025, 7, 26)
_ANTES_DA_JANELA = OFFICIAL_WINDOW_START - timedelta(days=1)


def test_warn_assigned_corners_names_the_event_and_the_authoritative_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Canto atribuído por nós dentro da janela: o aviso nomeia o evento e o que rodar.

    O fallback determinístico acerta 39,29% -- pior que cara-ou-coroa. Deixar isso persistido
    em silêncio foi o que produziu 17 das 21 divergências que a Sprint 008-04 corrigiu.
    """
    with caplog.at_level(logging.WARNING, logger="ingestion.cito.gap_sync"):
        warn_assigned_corners_in_official_window(
            "UFC Fight Night: Whittaker vs de Ridder",
            _DENTRO_DA_JANELA,
            {CornerOrigin.ART: 1, CornerOrigin.ASSIGNED_NO_ART: 5},
        )

    assert "Whittaker vs de Ridder" in caplog.text
    assert "ingestion.ufc_official.corner --aplicar" in caplog.text
    assert "5" in caplog.text


def test_warn_assigned_corners_stays_silent_when_the_corner_came_from_the_art(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Canto vindo da arte (99,58%) não pede passada autoritativa -- o aviso seria ruído."""
    with caplog.at_level(logging.WARNING, logger="ingestion.cito.gap_sync"):
        warn_assigned_corners_in_official_window(
            "UFC Fight Night: Whittaker vs de Ridder",
            _DENTRO_DA_JANELA,
            {CornerOrigin.ART: 6},
        )

    assert caplog.text == ""


def test_warn_assigned_corners_stays_silent_before_the_official_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Antes de 2010-03-21 a fonte oficial não ajuda: mandar rodá-la seria conselho errado.

    O canto anterior a essa data é fabricado nas três fontes (ADR 0006), e a Sprint 008-04 não
    escreve nada ali (RF-03).
    """
    with caplog.at_level(logging.WARNING, logger="ingestion.cito.gap_sync"):
        warn_assigned_corners_in_official_window(
            "Evento anterior à fronteira",
            _ANTES_DA_JANELA,
            {CornerOrigin.ASSIGNED_NO_ART: 6},
        )

    assert caplog.text == ""


def _fixture_dir_do_card_sem_arte(tmp_path: Path) -> Path:
    """Diretório de fixture com o catálogo de UMA página contendo só o card sem arte.

    O item do catálogo é o do próprio evento da fixture (mesmos id, slug, título, status e
    datas), e o payload de stats é copiado sem edição -- forma derivada, valores reais.
    """
    destino = tmp_path / "fixture_sem_arte"
    destino.mkdir()
    item = _item_sem_arte()
    (destino / "events_catalog_page_1.json").write_text(
        json.dumps(
            {
                "success": True,
                "data": [
                    {
                        "id": item.id,
                        "slug": item.slug,
                        "title": item.title,
                        "status": item.status,
                        "startsAt": "2025-07-26T19:00:00.000Z",
                        "eventDate": str(item.local_date),
                    }
                ],
                "meta": {
                    "page": 1,
                    "limit": 100,
                    "total": 1,
                    "totalPages": 1,
                    "hasNextPage": False,
                    "hasPreviousPage": False,
                    "nextPage": None,
                    "previousPage": None,
                },
            }
        ),
        encoding="utf-8",
    )
    nome = f"event_stats_{_PAYLOAD_SEM_ARTE}.json"
    (destino / nome).write_text((_FIXTURES / nome).read_text(encoding="utf-8"), encoding="utf-8")
    return destino


def test_run_gap_sync_warns_when_it_closes_with_an_assigned_corner_in_the_window(
    db_session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """O lote que fecha com canto atribuído dentro da janela avisa alto, nomeando o evento.

    A guarda vive no laço de ``run_gap_sync``, e não só numa função pura: é ali que o operador
    descobre, no fim da execução, que ficou dado provisório para corrigir.
    """
    orcamento = CallBudget(limit=20)
    cliente = CitoClient(
        token=settings.cito_api_token,
        base_url=settings.cito_base_url,
        fixture_dir=_fixture_dir_do_card_sem_arte(tmp_path),
        budget=orcamento,
    )

    with caplog.at_level(logging.WARNING, logger="ingestion.cito.gap_sync"):
        resumo = run_gap_sync(
            db_session,
            cliente,
            orcamento,
            EventStatsCache(tmp_path / "cache"),
            today=_HOJE,
            date_from=date(2025, 1, 1),
        )

    assert resumo.corner_origins == {CornerOrigin.ASSIGNED_NO_ART: 6}
    assert "Whittaker vs de Ridder" in caplog.text
    assert "ingestion.ufc_official.corner --aplicar" in caplog.text
