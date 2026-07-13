"""Testes do backfill round-a-round da Cito (M5, Slice 05), todos em modo fixture.

Cobrem o CA-05 da SPEC decomposto: escrita idempotente em ``bout_fighter_rounds`` por
``(bout_fighter_id, round)`` com ``source="cito"``; cache em disco resumável (hit não chama
``fetch`` nem cobra ``CallBudget``); janela fixa 2019-2025; SAVEPOINT por evento; ``CallBudget``
cobrado por fetch não-cacheado com teto respeitado; rate-limit entre eventos não-cacheados; e o
gate humano que aborta antes de qualquer chamada contra a rede real. Nenhum teste toca a rede: o
cliente roda em modo fixture (JSON local) e o gate é exercitado com um spy que prova zero chamadas.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
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
    HumanGateNotConfirmedError,
    _enforce_human_gate,
    _select_events_in_window,
    main,
    run_backfill_rounds,
    upsert_bout_fighter_rounds,
)
from ingestion.cito.cache import EventStatsCache
from ingestion.cito.client import CallBudget, CitoClient, QuotaExceededError
from ingestion.cito.dto import CitoEventStats, CitoRoundStatLine
from ingestion.normalize import normalize_name

_FIXTURES = Path(__file__).parent / "fixtures"


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
) -> tuple[Event, dict[str, int]]:
    """Semeia um evento com uma luta e os dois cantos; devolve o evento e os ``bout_fighter`` ids.

    Os nomes normalizam para as chaves que os ``fighter_slug`` das fixtures produzem, reproduzindo
    o matching persisted-driven por nome (mesmo padrão da Slice 04).
    """
    event = Event(name=name, date=event_date, location=None, source="kaggle")
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
        weight_class="Middleweight",
        source="kaggle",
    )
    session.add(bout)
    session.flush()

    red_bf = BoutFighter(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED, source="kaggle")
    blue_bf = BoutFighter(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE, source="kaggle")
    session.add_all([red_bf, blue_bf])
    session.flush()
    return event, {"red": red_bf.id, "blue": blue_bf.id}


def _seed_ufc319(session: Session) -> tuple[Event, dict[str, int]]:
    """Evento UFC 319 (2025-08-16) casável com a fixture ``event_stats_ufc-319.json``."""
    return _seed_event_bout(
        session,
        name="UFC 319: Du Plessis vs. Chimaev",
        event_date=date(2025, 8, 16),
        red_name="Dricus du Plessis",
        blue_name="Khamzat Chimaev",
    )


def _seed_ufc320(session: Session) -> tuple[Event, dict[str, int]]:
    """Evento UFC 320 (2025-10-04) casável com a fixture ``event_stats_ufc-320.json``."""
    return _seed_event_bout(
        session,
        name="UFC 320: Jones vs. Miocic",
        event_date=date(2025, 10, 4),
        red_name="Jon Jones",
        blue_name="Stipe Miocic",
    )


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


def _round_lines(slug: str) -> list[CitoRoundStatLine]:
    """As linhas round-a-round da fixture do evento ``slug``."""
    return _fixture_event_stats(slug).round_stats


# --------------------------------------------------------------------------- #
# CA-01 -- escrita idempotente em bout_fighter_rounds
# --------------------------------------------------------------------------- #


def test_upsert_bout_fighter_rounds_insere_uma_linha_por_round(db_session: Session) -> None:
    """CA-01: cada ``round`` vira uma linha com ``source="cito"`` e stats mapeadas 1:1 do DTO."""
    _event, bf_ids = _seed_ufc319(db_session)
    red_lines = [line for line in _round_lines("ufc-319") if line.corner is Corner.RED]

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
    # A Cito não expõe total de golpes por round -> ausência vira None (nunca zero inventado).
    assert first.total_strikes_landed is None
    assert first.total_strikes_attempted is None


def test_upsert_bout_fighter_rounds_ausencia_vira_none(db_session: Session) -> None:
    """CA-01: split ausente na fixture degrada para None, jamais zero inventado."""
    _event, bf_ids = _seed_ufc319(db_session)
    # Constrói uma linha round-a-round com todos os splits ausentes.
    empty_line = CitoRoundStatLine.model_validate(
        {
            "boutId": "ufc-319-bout-1",
            "corner": "red",
            "fighterSlug": "dricus-du-plessis",
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
    assert row.control_time_seconds is None
    assert row.knockdowns is None


def test_upsert_bout_fighter_rounds_idempotente(db_session: Session) -> None:
    """CA-01: rerun devolve 0 inseridos e não altera contagem nem conteúdo."""
    _event, bf_ids = _seed_ufc319(db_session)
    red_lines = [line for line in _round_lines("ufc-319") if line.corner is Corner.RED]

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
# CA-05 -- janela fixa 2019-2025
# --------------------------------------------------------------------------- #


def test_window_constantes_incluem_fronteiras() -> None:
    """CA-05: a janela é fixa 2019-01-01 a 2025-12-31 (inclui as fronteiras, exclui 2026)."""
    assert date(2019, 1, 1) == WINDOW_START
    assert date(2025, 12, 31) == WINDOW_END


def test_select_events_in_window_inclui_2019_2025_exclui_2018_2026(db_session: Session) -> None:
    """CA-05: só eventos com ``date`` em [2019, 2025]; 2018 e 2026 ficam de fora."""
    e2018 = Event(name="UFC 200", date=date(2018, 7, 9), location=None, source="kaggle")
    e2019 = Event(name="UFC 234", date=date(2019, 2, 9), location=None, source="kaggle")
    e2025 = Event(name="UFC 319", date=date(2025, 8, 16), location=None, source="kaggle")
    e2026 = Event(name="UFC 400", date=date(2026, 1, 17), location=None, source="kaggle")
    db_session.add_all([e2018, e2019, e2025, e2026])
    db_session.flush()

    selected = _select_events_in_window(db_session)

    names = {event.name for event in selected}
    assert names == {"UFC 234", "UFC 319"}


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


def test_run_backfill_rounds_pula_evento_com_slug_nao_derivavel(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-05: evento não-numerado (slug não-derivável) é PULADO com aviso; o run não aborta.

    Um 'UFC Fight Night' na janela não deriva slug Cito (a única convenção confirmada por dado real
    é 'ufc-<n>'); sem heurística silenciosa, o backfill o pula, conta no summary e segue para o
    evento numerado, que é processado normalmente. Nenhum fetch é disparado para o evento pulado
    (o cliente registra só o slug numerado).
    """
    # Fight Night (não-numerado) mais antigo -> processado primeiro na ordem cronológica (date, id).
    _seed_event_bout(
        db_session,
        name="UFC Fight Night: Silva vs. Costa",
        event_date=date(2020, 5, 9),
        red_name="Anderson Silva",
        blue_name="Uriah Hall",
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
    assert summary.rounds_inserted == 2  # só os rounds do evento numerado (UFC 319)
    # O evento pulado nunca tocou o cliente; só o numerado foi buscado.
    assert client.fetched == ["ufc-319"]
    assert db_session.scalar(select(func.count()).select_from(BoutFighterRound)) == 2
    assert "pulado" in caplog.text.lower()


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
        _enforce_human_gate(fixture=False, confirmed=False)


def test_enforce_human_gate_demais_combinacoes_nao_levantam() -> None:
    """CA-04: fixture (com/sem confirmação) e rede real confirmada não bloqueiam."""
    _enforce_human_gate(fixture=True, confirmed=False)
    _enforce_human_gate(fixture=True, confirmed=True)
    _enforce_human_gate(fixture=False, confirmed=True)


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
