"""Testes do backfill round-a-round da Cito (M5, Slice 05), todos em modo fixture.

Cobrem o CA-05 da SPEC decomposto: escrita idempotente em ``bout_fighter_rounds`` por
``(bout_fighter_id, round)`` com ``source="cito"``; cache em disco resumável (hit não chama
``fetch`` nem cobra ``CallBudget``); janela do piloto 2023-2025; SAVEPOINT por evento;
``CallBudget``
cobrado por fetch não-cacheado com teto respeitado; rate-limit entre eventos não-cacheados; e o
gate humano que aborta antes de qualquer chamada contra a rede real. Nenhum teste toca a rede: o
cliente roda em modo fixture (JSON local) e o gate é exercitado com um spy que prova zero chamadas.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.backfill_rounds import (
    WINDOW_END,
    WINDOW_START,
    BackfillRoundsSummary,
    _parse_args,
    _select_events_in_window,
    fill_bout_context,
    fill_bout_fighter_totals,
    main,
    run_backfill_rounds,
    upsert_bout_fighter_rounds,
)
from ingestion.cito.cache import EventStatsCache
from ingestion.cito.client import CallBudget, CitoClient, QuotaExceededError
from ingestion.cito.dto import (
    CitoBoutBlock,
    CitoBoutStatLine,
    CitoEventStats,
    CitoRoundStatLine,
)
from ingestion.cito.gate import HumanGateNotConfirmedError, enforce_human_gate
from ingestion.normalize import normalize_name

_FIXTURES = Path(__file__).parent / "fixtures"

# Canto vermelho de UFC 319 na fixture -- as linhas de stat identificam o lutador pelo slug.
_RED_SLUG_319 = "dricus-du-plessis"

# Fixture do payload REAL da Cito (sondagem autorizada de 2026-08-31): 13 lutas, 26 totais,
# 52 rounds. É a única que exercita o caminho completo sobre dado não editado.
_REAL_PAYLOAD_SLUG = "ufc-fight-night-august-22-2026"

# Fixture mínima de evento **sem** round-a-round (``roundStats: []``): prova que a ausência
# vira zero inserção e cobertura 0, nunca dado fabricado.
_SEM_ROUNDS_SLUG = "ufc-321-sem-rounds"


# --------------------------------------------------------------------------- #
# Builders de estado (Postgres de teste transacional) e utilidades de fixture.
# --------------------------------------------------------------------------- #


def _seed_fighter(session: Session, name: str) -> int:
    """Insere um ``Fighter`` mínimo (do seed Kaggle) e devolve o id materializado."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=None,
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    session.add(fighter)
    session.flush()
    return fighter.id


def _seed_event_bout(
    session: Session,
    *,
    name: str,
    event_date: date,
    red_name: str,
    blue_name: str,
    cito_slug: str | None,
    weight_class: str | None = "Middleweight",
) -> tuple[Event, dict[str, int]]:
    """Semeia um evento com uma luta e os dois cantos; devolve o evento e os ``bout_fighter`` ids.

    Os nomes normalizam para as chaves que os ``fighter_slug`` das fixtures produzem, reproduzindo
    o matching por nome. O ``cito_slug`` é o identificador que a sincronização de catálogo (Sprint
    007-03) persistiu; ``None`` reproduz um evento que o catálogo não casou.
    """
    event = Event(name=name, date=event_date, location=None, source="kaggle", cito_slug=cito_slug)
    session.add(event)
    session.flush()

    red_id = _seed_fighter(session, red_name)
    blue_id = _seed_fighter(session, blue_name)

    bout = Bout(
        event_id=event.id,
        winner_id=red_id,
        method=BoutMethod.DECISION,
        round=3,
        ending_time_seconds=None,
        weight_class=weight_class,
        source="kaggle",
    )
    session.add(bout)
    session.flush()

    red_bf = BoutFighter(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED, source="kaggle")
    blue_bf = BoutFighter(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE, source="kaggle")
    session.add_all([red_bf, blue_bf])
    session.flush()
    return event, {"red": red_bf.id, "blue": blue_bf.id}


def _seed_ufc319(
    session: Session, *, event_date: date = date(2025, 8, 16)
) -> tuple[Event, dict[str, int]]:
    """Evento UFC 319 casável com a fixture ``event_stats_ufc-319.json``.

    A ``event_date`` é parametrizável porque os testes de teto por execução precisam ordenar
    vários eventos dentro da janela; o default é a data real do card.
    """
    return _seed_event_bout(
        session,
        name="UFC 319: Du Plessis vs. Chimaev",
        event_date=event_date,
        red_name="Dricus du Plessis",
        blue_name="Khamzat Chimaev",
        cito_slug="ufc-319",
    )


def _seed_ufc320(
    session: Session,
    *,
    event_date: date = date(2025, 10, 4),
    weight_class: str | None = "Middleweight",
) -> tuple[Event, dict[str, int]]:
    """Evento UFC 320 casável com a fixture ``event_stats_ufc-320.json``.

    A ``weight_class`` é parametrizável para separar os dois casos do contexto de card: nula
    (o payload preenche) e já presente do seed (o payload nunca sobrescreve).
    """
    return _seed_event_bout(
        session,
        name="UFC 320: Jones vs. Miocic",
        event_date=event_date,
        red_name="Jon Jones",
        blue_name="Stipe Miocic",
        cito_slug="ufc-320",
        weight_class=weight_class,
    )


def _seed_fight_night(
    session: Session, *, event_date: date = date(2025, 3, 1)
) -> tuple[Event, dict[str, int]]:
    """Evento casável com a fixture do **payload real** (Fight Night de 2026-08-22).

    Só a luta principal do card é semeada; as outras 12 lutas do payload real ficam sem
    ``bout_fighter`` correspondente -- é exatamente o cenário de canto não-casado (CA-08). A
    ``event_date`` persistida não precisa ser a do payload: o identificador vem do catálogo
    (``cito_slug``), nunca da data.
    """
    return _seed_event_bout(
        session,
        name="UFC Fight Night: Hernandez vs Rodrigues",
        event_date=event_date,
        red_name="Anthony Hernandez",
        blue_name="Gregory Rodrigues",
        cito_slug=_REAL_PAYLOAD_SLUG,
    )


def _bout_id_de(session: Session, bout_fighter_id: int) -> int:
    """O ``bouts.id`` do canto persistido (os builders devolvem ``bout_fighter`` ids)."""
    bout_fighter = session.get(BoutFighter, bout_fighter_id)
    assert bout_fighter is not None
    return bout_fighter.bout_id


def _fixture_client(budget: CallBudget | None = None) -> CitoClient:
    """``CitoClient`` em modo fixture (lê JSON local, sem tocar rede/quota)."""
    return CitoClient(
        token="", base_url="https://api.citoapi.com", fixture_dir=_FIXTURES, budget=budget
    )


class _RecordingClient(CitoClient):
    """``CitoClient`` de fixture que registra cada ``fetch_event_stats`` (spy de cache hit)."""

    def __init__(self, budget: CallBudget | None = None) -> None:
        super().__init__(
            token="", base_url="https://api.citoapi.com", fixture_dir=_FIXTURES, budget=budget
        )
        self.fetched: list[str] = []

    def fetch_event_stats(self, slug: str) -> CitoEventStats:
        self.fetched.append(slug)
        return super().fetch_event_stats(slug)


def _fixture_event_stats(slug: str) -> CitoEventStats:
    """Carrega a fixture de stats de um evento como DTO tipado, sem tocar rede/quota."""
    return _fixture_client().fetch_event_stats(slug)


def _round_lines(slug: str, fighter_slug: str | None = None) -> list[CitoRoundStatLine]:
    """As linhas round-a-round da fixture do evento ``slug``, opcionalmente de um só lutador.

    O recorte é por ``fighter_slug`` porque a API real não traz ``corner`` na linha de stat.
    """
    lines = _fixture_event_stats(slug).round_stats
    if fighter_slug is None:
        return lines
    return [line for line in lines if line.fighter_slug == fighter_slug]


# --------------------------------------------------------------------------- #
# CA-01 -- escrita idempotente em bout_fighter_rounds
# --------------------------------------------------------------------------- #


def test_upsert_bout_fighter_rounds_insere_uma_linha_por_round(db_session: Session) -> None:
    """CA-01: cada ``round`` vira uma linha com ``source="cito"`` e stats mapeadas 1:1 do DTO."""
    _event, bf_ids = _seed_ufc319(db_session)
    red_lines = _round_lines("ufc-319", _RED_SLUG_319)

    inserted = upsert_bout_fighter_rounds(db_session, bf_ids["red"], red_lines)
    db_session.flush()

    rows = (
        db_session.execute(
            select(BoutFighterRound)
            .where(BoutFighterRound.bout_fighter_id == bf_ids["red"])
            .order_by(BoutFighterRound.round)
        )
        .scalars()
        .all()
    )
    assert inserted == len(red_lines)
    assert [row.round for row in rows] == [line.round for line in red_lines]
    assert all(row.source == "cito" for row in rows)
    # Stats mapeadas 1:1 da fixture (round 1 do canto vermelho de UFC 319).
    first = rows[0]
    assert first.knockdowns == 0
    assert (first.sig_strikes_landed, first.sig_strikes_attempted) == (12, 30)
    assert (first.head_landed, first.head_attempted) == (6, 18)
    assert (first.takedowns_landed, first.takedowns_attempted) == (0, 1)
    assert first.control_time_seconds == 10
    # A Cito expõe ``totalStrikes`` por round (payload real de 2026-08-31, ADR 0005).
    assert (first.total_strikes_landed, first.total_strikes_attempted) == (18, 38)


def test_upsert_bout_fighter_rounds_ausencia_vira_none(db_session: Session) -> None:
    """CA-01: split ausente na fixture degrada para None, jamais zero inventado."""
    _event, bf_ids = _seed_ufc319(db_session)
    # Constrói uma linha round-a-round com todos os splits ausentes.
    empty_line = CitoRoundStatLine.model_validate(
        {
            "boutId": "ufc-319-bout-1",
            "fighterSlug": _RED_SLUG_319,
            "round": 5,
        }
    )

    upsert_bout_fighter_rounds(db_session, bf_ids["red"], [empty_line])
    db_session.flush()

    row = db_session.execute(
        select(BoutFighterRound).where(
            BoutFighterRound.bout_fighter_id == bf_ids["red"],
            BoutFighterRound.round == 5,
        )
    ).scalar_one()
    assert row.sig_strikes_landed is None
    assert row.sig_strikes_attempted is None
    assert row.total_strikes_landed is None
    assert row.total_strikes_attempted is None
    assert row.control_time_seconds is None
    assert row.knockdowns is None


def test_upsert_bout_fighter_rounds_idempotente(db_session: Session) -> None:
    """CA-01: rerun devolve 0 inseridos e não altera contagem nem conteúdo."""
    _event, bf_ids = _seed_ufc319(db_session)
    red_lines = _round_lines("ufc-319", _RED_SLUG_319)

    first = upsert_bout_fighter_rounds(db_session, bf_ids["red"], red_lines)
    db_session.flush()
    count_after_first = db_session.scalar(
        select(func.count())
        .select_from(BoutFighterRound)
        .where(BoutFighterRound.bout_fighter_id == bf_ids["red"])
    )

    second = upsert_bout_fighter_rounds(db_session, bf_ids["red"], red_lines)
    db_session.flush()
    count_after_second = db_session.scalar(
        select(func.count())
        .select_from(BoutFighterRound)
        .where(BoutFighterRound.bout_fighter_id == bf_ids["red"])
    )

    assert first == len(red_lines)
    assert second == 0
    assert count_after_first == count_after_second == len(red_lines)


# --------------------------------------------------------------------------- #
# CA-02 -- cache em disco resumável
# --------------------------------------------------------------------------- #


def test_event_stats_cache_primeira_chamada_busca_e_grava(tmp_path: Path) -> None:
    """CA-02: a 1a chamada invoca ``fetch`` e grava o JSON em disco (cache miss)."""
    cache = EventStatsCache(tmp_path)
    calls: list[str] = []

    def _fetch(slug: str) -> CitoEventStats:
        calls.append(slug)
        return _fixture_event_stats(slug)

    stats, hit = cache.get_or_fetch("ufc-319", _fetch)

    assert hit is False
    assert calls == ["ufc-319"]
    assert (tmp_path / "event_stats_ufc-319.json").is_file()
    assert stats == _fixture_event_stats("ufc-319")


def test_event_stats_cache_segunda_chamada_e_hit_sem_fetch(tmp_path: Path) -> None:
    """CA-02: a 2a chamada do mesmo slug é cache hit -- sem ``fetch``, mesmo DTO."""
    cache = EventStatsCache(tmp_path)
    calls: list[str] = []

    def _fetch(slug: str) -> CitoEventStats:
        calls.append(slug)
        return _fixture_event_stats(slug)

    first_stats, first_hit = cache.get_or_fetch("ufc-319", _fetch)
    second_stats, second_hit = cache.get_or_fetch("ufc-319", _fetch)

    assert first_hit is False
    assert second_hit is True
    assert calls == ["ufc-319"]  # o fetch não foi refeito na 2a chamada
    assert second_stats == first_stats


def test_event_stats_cache_hit_preserva_splits_round_a_round(tmp_path: Path) -> None:
    """CA-02: o DTO reconstruído do cache preserva os splits round-a-round (round-trip fiel)."""
    cache = EventStatsCache(tmp_path)
    cache.get_or_fetch("ufc-319", _fixture_event_stats)

    reconstructed, hit = cache.get_or_fetch("ufc-319", _fixture_event_stats)

    assert hit is True
    assert reconstructed == _fixture_event_stats("ufc-319")


# --------------------------------------------------------------------------- #
# CA-05 -- janela do piloto: 2023-2025
# --------------------------------------------------------------------------- #


def test_window_constantes_incluem_fronteiras() -> None:
    """CA-05: a janela do piloto é 2023-01-01 a 2025-12-31 (fronteiras inclusivas)."""
    assert date(2023, 1, 1) == WINDOW_START
    assert date(2025, 12, 31) == WINDOW_END


def test_select_events_in_window_inclui_2023_2025_exclui_2022_2026(db_session: Session) -> None:
    """CA-05: só eventos com ``date`` em [2023, 2025]; 2019, 2022 e 2026 ficam de fora.

    A janela encolheu de 2019-2025 para 2023-2025 quando o piloto medido substituiu o lote
    (decisão do humano após o bootstrap pareado da Sprint 007-01): 2019 e 2022 provam que o
    corte inferior se moveu, 2026 prova que o superior não.
    """
    e2019 = Event(name="UFC 234", date=date(2019, 2, 9), location=None, source="kaggle")
    e2022 = Event(name="UFC 282", date=date(2022, 12, 10), location=None, source="kaggle")
    e2023 = Event(name="UFC 283", date=date(2023, 1, 21), location=None, source="kaggle")
    e2025 = Event(name="UFC 319", date=date(2025, 8, 16), location=None, source="kaggle")
    e2026 = Event(name="UFC 400", date=date(2026, 1, 17), location=None, source="kaggle")
    db_session.add_all([e2019, e2022, e2023, e2025, e2026])
    db_session.flush()

    selected = _select_events_in_window(db_session)

    names = {event.name for event in selected}
    assert names == {"UFC 283", "UFC 319"}


# --------------------------------------------------------------------------- #
# CA-01 -- teto de eventos por execução (--max-events), a trava da sondagem
# --------------------------------------------------------------------------- #


def test_run_backfill_rounds_max_events_processa_so_os_n_mais_antigos(
    db_session: Session,
) -> None:
    """CA-01: ``max_events=2`` processa só os DOIS eventos mais antigos da janela.

    É a trava da sondagem: o piloto autoriza 115 chamadas, e um erro de laço deve interromper
    cedo em vez de consumir o orçamento inteiro. A ordem é a cronológica de
    ``_select_events_in_window`` (``date, id``), então o evento de 2025 fica de fora.
    """
    _seed_ufc319(db_session, event_date=date(2023, 1, 21))
    _seed_ufc320(db_session, event_date=date(2024, 6, 1))
    _seed_fight_night(db_session, event_date=date(2025, 8, 16))
    budget = CallBudget(limit=10)
    client = _RecordingClient(budget)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, client, budget, cache, max_events=2)
    db_session.flush()

    assert client.fetched == ["ufc-319", "ufc-320"]
    assert budget.used == 2
    assert summary.events_processed == 2
    assert summary.cito_calls_used == 2


def test_run_backfill_rounds_max_events_none_processa_a_janela_inteira(
    db_session: Session,
) -> None:
    """CA-01: sem teto (``max_events=None``, o default), a janela inteira é processada."""
    _seed_ufc319(db_session, event_date=date(2023, 1, 21))
    _seed_ufc320(db_session, event_date=date(2024, 6, 1))
    budget = CallBudget(limit=10)
    client = _RecordingClient(budget)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, client, budget, cache)

    assert client.fetched == ["ufc-319", "ufc-320"]
    assert summary.events_processed == 2


def test_run_backfill_rounds_sondagem_limitada_vira_cache_hit_no_lote(db_session: Session) -> None:
    """CA-01/CA-09: a sondagem limitada não re-gasta quota no lote completo (mesmo ``cache-dir``).

    É o que faz as 10 chamadas da sondagem contarem **dentro** das 115 do piloto: o run 1
    (``max_events=1``) baixa o evento mais antigo; o run 2, sem teto e com o mesmo diretório de
    cache, o reaproveita por cache hit e só cobra quota do evento novo.
    """
    _seed_ufc319(db_session, event_date=date(2023, 1, 21))
    _seed_ufc320(db_session, event_date=date(2024, 6, 1))
    cache_dir = _cache_dir(db_session)

    budget_sondagem = CallBudget(limit=10)
    client_sondagem = _RecordingClient(budget_sondagem)
    run_backfill_rounds(
        db_session,
        client_sondagem,
        budget_sondagem,
        EventStatsCache(cache_dir),
        max_events=1,
    )
    db_session.flush()

    budget_lote = CallBudget(limit=10)
    client_lote = _RecordingClient(budget_lote)
    lote = run_backfill_rounds(db_session, client_lote, budget_lote, EventStatsCache(cache_dir))
    db_session.flush()

    assert client_sondagem.fetched == ["ufc-319"]
    assert budget_sondagem.used == 1
    # O lote só bateu na Cito pelo evento que a sondagem não baixou.
    assert client_lote.fetched == ["ufc-320"]
    assert budget_lote.used == 1
    assert lote.cache_hits == 1


def test_cli_expoe_max_events_com_default_sem_teto() -> None:
    """CA-01: a CLI expõe ``--max-events``; ausente, o default é ``None`` (janela inteira)."""
    assert _parse_args(["--max-events", "10"]).max_events == 10
    assert _parse_args([]).max_events is None


# --------------------------------------------------------------------------- #
# CA-01 + CA-06 -- orquestrador em modo fixture (SAVEPOINT + idempotência ponta a ponta)
# --------------------------------------------------------------------------- #


def test_run_backfill_rounds_fixture_popula_e_e_idempotente(db_session: Session) -> None:
    """CA-01/CA-06: popula ``bout_fighter_rounds`` (source=cito); rerun = 0 inserido, count fixo."""
    _event, _bf = _seed_ufc319(db_session)
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)
    db_session.flush()

    total = db_session.scalar(select(func.count()).select_from(BoutFighterRound))
    assert isinstance(summary, BackfillRoundsSummary)
    assert summary.source == "cito"
    assert summary.rounds_inserted == 2  # round 1 de cada canto de UFC 319
    assert total == 2
    sources = db_session.execute(select(BoutFighterRound.source).distinct()).scalars().all()
    assert sources == ["cito"]

    budget_rerun = CallBudget(limit=10)
    rerun = run_backfill_rounds(db_session, _fixture_client(budget_rerun), budget_rerun, cache)
    db_session.flush()

    assert rerun.rounds_inserted == 0
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 2


def test_run_backfill_rounds_pula_evento_sem_cito_slug(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-05: evento sem ``cito_slug`` persistido é PULADO com aviso; o run não aborta.

    O identificador vem do catálogo (Sprint 007-03). Um evento que o catálogo não casou não tem
    identificador, e nenhum é inventado: o backfill o pula, conta no summary e segue. Nenhum fetch
    é disparado para o evento pulado (o cliente registra só o slug do evento com identificador).

    O evento pulado é **numerado** de propósito: o critério do skip é a ausência do identificador
    persistido, não o formato do nome -- que era o critério da decisão anterior.
    """
    # Sem cito_slug e mais antigo -> processado primeiro na ordem cronológica (date, id).
    _seed_event_bout(
        db_session,
        name="UFC 250: Nunes vs. Spencer",
        event_date=date(2023, 6, 6),
        red_name="Amanda Nunes",
        blue_name="Felicia Spencer",
        cito_slug=None,
    )
    _seed_ufc319(db_session)
    budget = CallBudget(limit=10)
    client = _RecordingClient(budget)
    cache = EventStatsCache(_cache_dir(db_session))

    with caplog.at_level(logging.WARNING, logger="ingestion.cito.backfill_rounds"):
        summary = run_backfill_rounds(db_session, client, budget, cache)
    db_session.flush()

    assert summary.events_skipped == 1
    assert summary.events_processed == 1
    assert summary.rounds_inserted == 2  # só os rounds do evento com identificador (UFC 319)
    assert client.fetched == ["ufc-319"]
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 2
    assert "pulado" in caplog.text.lower()


def test_run_backfill_rounds_processa_fight_night_com_cito_slug(db_session: Session) -> None:
    """CA-05: um **Fight Night** com ``cito_slug`` do catálogo é processado normalmente.

    É o caso que a decisão anterior excluía por construção: o nome não deriva 'ufc-<n>', então o
    M5 pularia o evento. Com o identificador vindo do catálogo, o nome do evento deixa de importar
    -- Fight Nights entram (347 dos 745 eventos persistidos).
    """
    _event, _bf = _seed_event_bout(
        db_session,
        name="UFC Fight Night: Du Plessis vs. Chimaev",
        event_date=date(2025, 8, 16),
        red_name="Dricus du Plessis",
        blue_name="Khamzat Chimaev",
        cito_slug="ufc-319",
    )
    budget = CallBudget(limit=10)
    client = _RecordingClient(budget)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, client, budget, cache)
    db_session.flush()

    assert summary.events_skipped == 0
    assert summary.events_processed == 1
    assert summary.rounds_inserted == 2
    assert client.fetched == ["ufc-319"]
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 2


def test_run_backfill_rounds_savepoint_reverte_so_o_evento_com_falha(db_session: Session) -> None:
    """CA-06: falha no matching de um evento reverte só aquele evento (sem parcial), via SAVEPOINT.

    UFC 319 casa e persiste; UFC 320 tem os dois cantos com o mesmo nome normalizado -> o matching
    levanta ``AmbiguousBoutFighterMatchError`` no meio do 2o evento. O ``begin_nested`` reverte só
    o 2o evento; as linhas do 1o permanecem.
    """
    from ingestion.cito.matching import AmbiguousBoutFighterMatchError

    _e319, _bf319 = _seed_ufc319(db_session)
    # UFC 320 ambíguo: ambos os cantos normalizam para 'jon jones'.
    _seed_event_bout(
        db_session,
        name="UFC 320: Jones vs. Miocic",
        event_date=date(2025, 10, 4),
        red_name="Jon Jones",
        blue_name="Jon Jones",
        cito_slug="ufc-320",
    )
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    with pytest.raises(AmbiguousBoutFighterMatchError):
        run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)

    # Só as linhas de UFC 319 sobreviveram; o SAVEPOINT reverteu o evento ambíguo.
    total = db_session.scalar(select(func.count()).select_from(BoutFighterRound))
    assert total == 2


def test_run_backfill_rounds_resumivel_nao_refaz_fetch_do_evento_ja_cacheado(
    db_session: Session,
) -> None:
    """CA-02/CA-06: após interrupção no 2o evento, o rerun não refaz o ``fetch`` do 1o (cache hit).

    O cache é gravado em disco mesmo quando o SAVEPOINT do evento seguinte reverte (a escrita de
    cache não é transacional): a retomada não re-gasta a quota do evento já baixado.
    """
    from ingestion.cito.matching import AmbiguousBoutFighterMatchError

    _e319, _bf319 = _seed_ufc319(db_session)
    _seed_event_bout(
        db_session,
        name="UFC 320: Jones vs. Miocic",
        event_date=date(2025, 10, 4),
        red_name="Jon Jones",
        blue_name="Jon Jones",
        cito_slug="ufc-320",
    )
    cache_dir = _cache_dir(db_session)

    budget_1 = CallBudget(limit=10)
    client_1 = _RecordingClient(budget_1)
    with pytest.raises(AmbiguousBoutFighterMatchError):
        run_backfill_rounds(db_session, client_1, budget_1, EventStatsCache(cache_dir))

    # Run 1 baixou ambos os eventos (UFC 320 é cacheado antes do matching falhar).
    assert client_1.fetched == ["ufc-319", "ufc-320"]

    budget_2 = CallBudget(limit=10)
    client_2 = _RecordingClient(budget_2)
    with pytest.raises(AmbiguousBoutFighterMatchError):
        run_backfill_rounds(db_session, client_2, budget_2, EventStatsCache(cache_dir))

    # Run 2 não refez nenhum fetch: ambos vieram do cache em disco (0 quota gasta na retomada).
    assert client_2.fetched == []
    assert budget_2.used == 0


# --------------------------------------------------------------------------- #
# CA-02 -- cobertura round-a-round observável no resumo e no log por evento
# --------------------------------------------------------------------------- #


def test_run_backfill_rounds_resumo_reporta_cobertura_round_a_round(db_session: Session) -> None:
    """CA-02: o resumo traz lutas do payload x lutas com round-a-round, e a fração derivada.

    A cobertura é medida **sobre o payload**, não sobre o que foi gravado: é ela que responde a
    pergunta do piloto -- a Cito expõe round-a-round para estes eventos? -- independentemente de
    quantos cantos casaram com a base persistida.
    """
    _seed_fight_night(db_session, event_date=date(2025, 3, 1))
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)

    # Payload real: 13 lutas com box-score, 13 com round-a-round -> cobertura total.
    assert summary.bouts_total == 13
    assert summary.bouts_with_rounds == 13
    assert summary.round_coverage == 1.0
    assert summary.events_without_round_stats == 0


def test_run_backfill_rounds_evento_sem_round_stats_e_ausencia_legitima(
    db_session: Session,
) -> None:
    """CA-02/CA-04: evento sem ``roundStats`` dá cobertura 0, 0 inserção e nenhuma fabricação.

    A API marca o round-a-round como enriquecimento "quando disponível": a ausência é achado
    legítimo do piloto, vira nulo e é contada -- nunca preenchida com zero.
    """
    _seed_event_bout(
        db_session,
        name="UFC 321: Sem round-a-round",
        event_date=date(2025, 11, 8),
        red_name="Jon Jones",
        blue_name="Stipe Miocic",
        cito_slug=_SEM_ROUNDS_SLUG,
    )
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)
    db_session.flush()

    assert summary.events_processed == 1
    assert summary.bouts_total == 1
    assert summary.bouts_with_rounds == 0
    assert summary.round_coverage == 0.0
    assert summary.events_without_round_stats == 1
    assert summary.rounds_inserted == 0
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 0


def test_run_backfill_rounds_loga_uma_linha_por_evento_processado(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-02: cada evento processado emite uma linha de log com a própria cobertura."""
    _seed_ufc319(db_session, event_date=date(2023, 1, 21))
    _seed_ufc320(db_session, event_date=date(2024, 6, 1))
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    with caplog.at_level(logging.INFO, logger="ingestion.cito.backfill_rounds"):
        run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)

    # A linha do resumo agregado também cita cobertura; o recorte é pelas linhas POR EVENTO.
    linhas_por_evento = [
        registro
        for registro in caplog.records
        if registro.getMessage().startswith("Evento ") and "cobertura" in registro.getMessage()
    ]
    assert len(linhas_por_evento) == 2
    assert "ufc-319" in linhas_por_evento[0].getMessage()
    assert "ufc-320" in linhas_por_evento[1].getMessage()


def test_round_coverage_sem_lutas_nao_divide_por_zero(db_session: Session) -> None:
    """CA-02: janela sem nenhum evento devolve cobertura ``0.0`` em vez de dividir por zero."""
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    vazio = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)

    assert vazio.bouts_total == 0
    assert vazio.round_coverage == 0.0


# --------------------------------------------------------------------------- #
# CA-05 -- reversals e tempo de controle: preenche só o que está nulo
# --------------------------------------------------------------------------- #


def _bout_stat_line(slug: str, fighter_slug: str) -> CitoBoutStatLine:
    """A linha de ``boutStats`` de um lutador na fixture do evento ``slug``."""
    linhas = [
        line for line in _fixture_event_stats(slug).bout_stats if line.fighter_slug == fighter_slug
    ]
    return linhas[0]


def test_fill_bout_fighter_totals_preenche_reversals_e_tempo_de_controle_nulos(
    db_session: Session,
) -> None:
    """CA-05: ``reversals`` e o tempo de controle nulos são preenchidos a partir de ``boutStats``.

    A linha do canto continua com ``source="kaggle"``: ``source`` é a origem da LINHA (quem a
    criou), não de cada campo. A procedência do enriquecimento fica rastreável nas linhas de
    ``bout_fighter_rounds``, que gravam ``source="cito"``.
    """
    _event, bf_ids = _seed_ufc320(db_session)
    canto = db_session.get(BoutFighter, bf_ids["red"])
    assert canto is not None
    assert canto.reversals is None
    assert canto.control_time_seconds is None

    preenchidos = fill_bout_fighter_totals(
        db_session, bf_ids["red"], _bout_stat_line("ufc-320", "jon-jones")
    )
    db_session.flush()

    assert preenchidos == 2
    assert canto.reversals == 1
    assert canto.control_time_seconds == 135  # "2:15"
    assert canto.source == "kaggle"


def test_fill_bout_fighter_totals_nunca_sobrescreve_valor_ja_presente(
    db_session: Session,
) -> None:
    """CA-05: valor já gravado pelo seed permanece intacto; só o nulo é preenchido."""
    _event, bf_ids = _seed_ufc320(db_session)
    canto = db_session.get(BoutFighter, bf_ids["red"])
    assert canto is not None
    canto.control_time_seconds = 999  # já veio do seed do Kaggle
    db_session.flush()

    preenchidos = fill_bout_fighter_totals(
        db_session, bf_ids["red"], _bout_stat_line("ufc-320", "jon-jones")
    )
    db_session.flush()

    assert preenchidos == 1  # só ``reversals`` estava nulo
    assert canto.control_time_seconds == 999
    assert canto.reversals == 1


def test_fill_bout_fighter_totals_ausencia_no_payload_permanece_nula(
    db_session: Session,
) -> None:
    """CA-05: campo ausente no payload não vira zero -- a coluna permanece nula."""
    _event, bf_ids = _seed_ufc320(db_session)
    linha_sem_dado = CitoBoutStatLine.model_validate(
        {"boutId": "ufc-320-bout-1", "fighterSlug": "jon-jones"}
    )

    preenchidos = fill_bout_fighter_totals(db_session, bf_ids["red"], linha_sem_dado)
    db_session.flush()

    canto = db_session.get(BoutFighter, bf_ids["red"])
    assert canto is not None
    assert preenchidos == 0
    assert canto.reversals is None
    assert canto.control_time_seconds is None


def test_fill_bout_fighter_totals_rerun_nao_altera_nada(db_session: Session) -> None:
    """CA-05: a segunda execução é no-op -- 0 campos preenchidos e nenhum valor alterado."""
    _event, bf_ids = _seed_ufc320(db_session)
    linha = _bout_stat_line("ufc-320", "jon-jones")

    fill_bout_fighter_totals(db_session, bf_ids["red"], linha)
    db_session.flush()
    segundo = fill_bout_fighter_totals(db_session, bf_ids["red"], linha)
    db_session.flush()

    canto = db_session.get(BoutFighter, bf_ids["red"])
    assert canto is not None
    assert segundo == 0
    assert (canto.reversals, canto.control_time_seconds) == (1, 135)


# --------------------------------------------------------------------------- #
# CA-06 -- contexto de card em bouts (aditivo, nunca sobrescreve o desfecho do seed)
# --------------------------------------------------------------------------- #


def _bout_block(slug: str, cito_bout_id: str) -> CitoBoutBlock:
    """A linha do card (``bouts[]``) de uma luta na fixture do evento ``slug``."""
    return next(b for b in _fixture_event_stats(slug).bouts if b.id == cito_bout_id)


def test_fill_bout_context_grava_card_section_e_bout_order(db_session: Session) -> None:
    """CA-06: ``card_section`` recebe o rótulo **cru** e ``bout_order`` a posição do payload.

    ``bout_order`` é ordenável, não sequencial: o payload real traz 1001 na luta principal.
    Nada aqui assume intervalo, densidade, nem que 1 seja a principal.
    """
    _event, bf_ids = _seed_ufc320(db_session)
    bout_id = _bout_id_de(db_session, bf_ids["red"])

    preenchidos = fill_bout_context(db_session, bout_id, _bout_block("ufc-320", "ufc-320-bout-1"))
    db_session.flush()

    bout = db_session.get(Bout, bout_id)
    assert bout is not None
    assert preenchidos == 2  # weight_class já estava preenchida pelo seed
    assert bout.card_section == "Main Card"
    assert bout.bout_order == 1001


def test_fill_bout_context_preenche_weight_class_nula_e_preserva_a_existente(
    db_session: Session,
) -> None:
    """CA-06: ``weight_class`` nula é preenchida; a já presente do seed permanece intacta."""
    _e_nula, bf_nula = _seed_ufc320(db_session, weight_class=None)
    _e_cheia, bf_cheia = _seed_ufc320(db_session, event_date=date(2025, 10, 5))
    linha = _bout_block("ufc-320", "ufc-320-bout-1")

    fill_bout_context(db_session, _bout_id_de(db_session, bf_nula["red"]), linha)
    fill_bout_context(db_session, _bout_id_de(db_session, bf_cheia["red"]), linha)
    db_session.flush()

    bout_nula = db_session.get(Bout, _bout_id_de(db_session, bf_nula["red"]))
    bout_cheia = db_session.get(Bout, _bout_id_de(db_session, bf_cheia["red"]))
    assert bout_nula is not None and bout_cheia is not None
    assert bout_nula.weight_class == "Heavyweight"  # veio do payload
    assert bout_cheia.weight_class == "Middleweight"  # do seed, preservada


def test_fill_bout_context_nao_sobrescreve_method_nem_winner_do_seed(
    db_session: Session,
) -> None:
    """CA-06: o **desfecho** continua sendo do seed -- ``method`` e ``winner_id`` intactos.

    Trocar a fonte da verdade do resultado no meio de um backfill de enriquecimento mudaria
    silenciosamente o dado que o modelo treina como rótulo. O payload traz o desfecho, e ele é
    deliberadamente ignorado aqui.
    """
    _event, bf_ids = _seed_ufc320(db_session)
    bout_id = _bout_id_de(db_session, bf_ids["red"])
    bout = db_session.get(Bout, bout_id)
    assert bout is not None
    method_do_seed, winner_do_seed = bout.method, bout.winner_id

    fill_bout_context(db_session, bout_id, _bout_block("ufc-320", "ufc-320-bout-1"))
    db_session.flush()

    assert bout.method == method_do_seed
    assert bout.winner_id == winner_do_seed


def test_fill_bout_context_bout_order_nulo_bloqueia_tambem_o_card_section(
    db_session: Session,
) -> None:
    """CA-06: ``boutOrder`` nulo -> **ambas** as colunas de card ficam nulas, mesmo com seção.

    Nos eventos de 2023 a Cito devolve ``cardSection: "Main Card"`` para **todas** as lutas --
    as 15 de UFC 283 inclusive, que obviamente não são todas card principal. O campo é um
    **default não-discriminante**, não um rótulo. Persistir isso verbatim não é gravar dado
    ausente: é gravar dado **falso**, que é pior, porque uma feature ou uma tela construída
    depois herdaria o erro com aparência de sinal.

    Os dois campos viajam juntos no enriquecimento da Cito (quando um existe, o outro existe),
    então ``boutOrder`` nulo é o sinal confiável de que o enriquecimento não está disponível
    para aquele evento -- e serve de porteiro dos dois. ``weight_class`` não é afetada: ela é
    dado real do card, independente do enriquecimento.
    """
    _event, bf_ids = _seed_ufc320(db_session, weight_class=None)
    bout_id = _bout_id_de(db_session, bf_ids["red"])
    sem_enriquecimento = CitoBoutBlock.model_validate(
        {
            "id": "ufc-320-bout-1",
            "cardSection": "Main Card",
            "boutOrder": None,
            "weightClass": "Heavyweight",
            "fighters": [],
        }
    )

    preenchidos = fill_bout_context(db_session, bout_id, sem_enriquecimento)
    db_session.flush()

    bout = db_session.get(Bout, bout_id)
    assert bout is not None
    assert preenchidos == 1  # só o peso, que é dado real do card
    assert bout.weight_class == "Heavyweight"
    assert bout.card_section is None
    assert bout.bout_order is None


def test_fill_bout_context_rerun_nao_altera_nada(db_session: Session) -> None:
    """CA-06: a segunda execução é no-op -- 0 campos preenchidos, nenhum valor alterado."""
    _event, bf_ids = _seed_ufc320(db_session, weight_class=None)
    bout_id = _bout_id_de(db_session, bf_ids["red"])
    linha = _bout_block("ufc-320", "ufc-320-bout-1")

    fill_bout_context(db_session, bout_id, linha)
    db_session.flush()
    segundo = fill_bout_context(db_session, bout_id, linha)
    db_session.flush()

    bout = db_session.get(Bout, bout_id)
    assert bout is not None
    assert segundo == 0
    assert (bout.card_section, bout.bout_order, bout.weight_class) == (
        "Main Card",
        1001,
        "Heavyweight",
    )


def test_fill_bout_context_ausencia_no_payload_permanece_nula(db_session: Session) -> None:
    """CA-06: contexto ausente no payload não vira sentinela -- as colunas seguem nulas."""
    _event, bf_ids = _seed_ufc320(db_session, weight_class=None)
    bout_id = _bout_id_de(db_session, bf_ids["red"])
    sem_contexto = CitoBoutBlock.model_validate({"id": "ufc-320-bout-1", "fighters": []})

    preenchidos = fill_bout_context(db_session, bout_id, sem_contexto)
    db_session.flush()

    bout = db_session.get(Bout, bout_id)
    assert bout is not None
    assert preenchidos == 0
    assert bout.card_section is None
    assert bout.bout_order is None
    assert bout.weight_class is None


# --------------------------------------------------------------------------- #
# CA-07 -- efeito observável na API (ponta a ponta) + CA-08 (não-casados contados)
# --------------------------------------------------------------------------- #


def test_api_apos_backfill_devolve_rounds_populado_e_reversals_nao_nulo(
    db_session: Session, client: TestClient
) -> None:
    """CA-07: após o backfill, ``GET /api/v1/bouts/{id}`` devolve ``rounds`` e ``reversals``.

    É o valor demonstrável da slice: hoje ``rounds`` vem ``[]`` e ``reversals`` vem nulo em
    16.632 linhas. O teste roda o backfill em modo fixture sobre a mesma ``Session``
    transacional da API, então a asserção enxerga exatamente o que o backfill gravou.
    """
    _event, bf_ids = _seed_ufc320(db_session)
    bout_id = _bout_id_de(db_session, bf_ids["red"])
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)
    db_session.flush()

    payload = client.get(f"/api/v1/bouts/{bout_id}").json()

    # Uma linha por (canto, round) do payload: 2 cantos x 1 round na fixture de UFC 320.
    assert len(payload["rounds"]) == 2
    assert {(r["fighter_id"], r["round"]) for r in payload["rounds"]} == {
        (row["fighter_id"], 1) for row in payload["fighters"]
    }
    assert all(r["source"] == "cito" for r in payload["rounds"])
    # ``reversals`` deixa de vir nulo nos totais por canto (preenchido de ``boutStats``).
    assert [row["reversals"] for row in payload["fighters"]] == [1, 0]
    # As linhas do canto continuam sendo do seed -- ``source`` é a origem da LINHA.
    assert all(row["source"] == "kaggle" for row in payload["fighters"])


def test_run_backfill_rounds_conta_cantos_nao_casados_sem_fabricar(db_session: Session) -> None:
    """CA-08: canto do payload sem ``bout_fighter`` correspondente é contado, nunca gravado.

    A fixture do payload real traz 13 lutas (26 cantos) e a base persiste só a luta principal:
    24 cantos ficam sem correspondência. Nada é criado para eles -- o restante do evento é
    processado normalmente e o resumo reporta a lacuna.
    """
    _event, bf_ids = _seed_fight_night(db_session)
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)
    db_session.flush()

    assert summary.unmatched_stat_lines == 24
    assert summary.events_processed == 1
    # Só os rounds dos dois cantos casados foram gravados (5 rounds cada).
    linhas = db_session.execute(select(BoutFighterRound)).scalars().all()
    assert {linha.bout_fighter_id for linha in linhas} == {bf_ids["red"], bf_ids["blue"]}


def test_run_backfill_rounds_ambiguidade_de_nome_continua_falhando_alto(
    db_session: Session,
) -> None:
    """CA-08: dois cantos com o mesmo nome normalizado seguem levantando erro tipado.

    Não-casado é lacuna (contada e reportada); ambíguo é risco de gravar no lutador errado --
    a política de entity resolution do M1 falha alto e não muda nesta slice.
    """
    from ingestion.cito.matching import AmbiguousBoutFighterMatchError

    _seed_event_bout(
        db_session,
        name="UFC 320: Jones vs. Miocic",
        event_date=date(2025, 10, 4),
        red_name="Jon Jones",
        blue_name="Jon Jones",
        cito_slug="ufc-320",
    )
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    with pytest.raises(AmbiguousBoutFighterMatchError):
        run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)


def test_run_backfill_rounds_resumo_conta_campos_preenchidos(db_session: Session) -> None:
    """CA-05/CA-06: o resumo reporta quantos campos de canto e de card foram preenchidos."""
    _event, _bf = _seed_ufc320(db_session, weight_class=None)
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)
    db_session.flush()

    # 2 cantos x (reversals + control time) = 4; card = weight_class + section + order = 3.
    assert summary.bout_fighter_fields_filled == 4
    assert summary.bout_context_fields_filled == 3


# --------------------------------------------------------------------------- #
# CA-03 -- CallBudget cobrado e teto respeitado + rate-limit
# --------------------------------------------------------------------------- #


def test_run_backfill_rounds_call_budget_cobra_por_fetch_nao_cacheado(db_session: Session) -> None:
    """CA-03: cada fetch não-cacheado cobra o ``CallBudget`` (2 eventos -> 2 chamadas)."""
    _seed_ufc319(db_session)
    _seed_ufc320(db_session)
    budget = CallBudget(limit=10)
    cache = EventStatsCache(_cache_dir(db_session))

    summary = run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)

    assert summary.cito_calls_used == 2
    assert budget.used == 2


def test_run_backfill_rounds_teto_estoura_antes_de_gastar(db_session: Session) -> None:
    """CA-03: com teto 1 e 2 eventos, o 2o fetch estoura ``QuotaExceededError`` antes de gastar.

    O 1o evento é processado; o 2o estoura no fetch e o SAVEPOINT o reverte -- o teto para a
    execução antes de exceder a quota (``used`` permanece em 1).
    """
    _seed_ufc319(db_session)
    _seed_ufc320(db_session)
    budget = CallBudget(limit=1)
    cache = EventStatsCache(_cache_dir(db_session))

    with pytest.raises(QuotaExceededError):
        run_backfill_rounds(db_session, _fixture_client(budget), budget, cache)

    assert budget.used == 1
    # Só as linhas do 1o evento (UFC 319) foram persistidas antes do estouro.
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 2


def test_run_backfill_rounds_cache_hit_nao_cobra_budget(db_session: Session) -> None:
    """CA-03: uma execução totalmente cacheada consome 0 do ``CallBudget`` (hit não cobra)."""
    _seed_ufc319(db_session)
    _seed_ufc320(db_session)
    cache = EventStatsCache(_cache_dir(db_session))

    warm_budget = CallBudget(limit=10)
    run_backfill_rounds(db_session, _fixture_client(warm_budget), warm_budget, cache)
    assert warm_budget.used == 2

    cached_budget = CallBudget(limit=10)
    run_backfill_rounds(db_session, _fixture_client(cached_budget), cached_budget, cache)
    assert cached_budget.used == 0


def test_run_backfill_rounds_rate_limit_entre_eventos_nao_cacheados(db_session: Session) -> None:
    """CA-03: o sleeper de rate-limit é chamado entre eventos não-cacheados; não em cache hit."""
    _seed_ufc319(db_session)
    _seed_ufc320(db_session)
    cache = EventStatsCache(_cache_dir(db_session))
    intervals: list[float] = []

    def _sleeper(seconds: float) -> None:
        intervals.append(seconds)

    warm_budget = CallBudget(limit=10)
    run_backfill_rounds(
        db_session,
        _fixture_client(warm_budget),
        warm_budget,
        cache,
        min_interval_seconds=0.5,
        sleeper=_sleeper,
    )
    # Dois eventos não-cacheados -> rate-limit aplicado uma vez (entre o 1o e o 2o).
    assert intervals == [0.5]

    intervals.clear()
    cached_budget = CallBudget(limit=10)
    run_backfill_rounds(
        db_session,
        _fixture_client(cached_budget),
        cached_budget,
        cache,
        min_interval_seconds=0.5,
        sleeper=_sleeper,
    )
    # Tudo cache hit -> nenhum rate-limit.
    assert intervals == []


# --------------------------------------------------------------------------- #
# CA-04 -- gate humano antes da rede real
# --------------------------------------------------------------------------- #


def test_enforce_human_gate_rede_real_sem_confirmacao_levanta() -> None:
    """CA-04: modo rede real sem confirmação explícita levanta ``HumanGateNotConfirmedError``."""
    with pytest.raises(HumanGateNotConfirmedError):
        enforce_human_gate(fixture=False, confirmed=False)


def test_enforce_human_gate_demais_combinacoes_nao_levantam() -> None:
    """CA-04: fixture (com/sem confirmação) e rede real confirmada não bloqueiam."""
    enforce_human_gate(fixture=True, confirmed=False)
    enforce_human_gate(fixture=True, confirmed=True)
    enforce_human_gate(fixture=False, confirmed=True)


def test_main_rede_real_sem_confirmacao_nao_dispara_nenhuma_chamada(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-04: o CLI em rede real sem ``--confirmar-gasto-de-quota`` aborta antes de qualquer fetch.

    Um spy sobre ``CitoClient.fetch_event_stats`` prova ZERO chamadas: o gate bloqueia antes mesmo
    de instanciar o cliente ou abrir a sessão. O comando encerra com exit code != 0.
    """
    chamadas: list[str] = []

    def _spy(self: CitoClient, slug: str) -> CitoEventStats:
        chamadas.append(slug)
        raise AssertionError("a rede não deveria ser tocada sem o gate confirmado")

    monkeypatch.setattr(CitoClient, "fetch_event_stats", _spy)

    with (
        caplog.at_level(logging.ERROR, logger="ingestion.cito.backfill_rounds"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--cache-dir", str(tmp_path)])

    assert excinfo.value.code != 0
    assert chamadas == []
    assert "gate" in caplog.text.lower()


# --------------------------------------------------------------------------- #
# Utilidade de diretório de cache por teste (isolado do estado global).
# --------------------------------------------------------------------------- #


def _cache_dir(session: Session) -> Path:
    """Diretório de cache efêmero para o teste, distinto por chamada (isola execuções)."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="cito-cache-"))
