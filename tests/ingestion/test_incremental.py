"""Testes do upsert incremental de eventos da Cito -- CA-01, CA-02, CA-05.

Cobrem, contra o Postgres de teste (sessão transacional com rollback ao final):
a inserção de um evento com ``source="cito"``; a idempotência (rodar duas vezes não
duplica); a reconciliação cross-source (um evento já semeado do Kaggle com grafia
divergente não é duplicado e mantém o ``source`` original); e o orquestrador
``run_incremental`` (fetch no modo fixture + upsert) ponta a ponta.

A partir da Slice 03, cobrem também o upsert de ``bouts`` + ``bout_fighters`` (long):
o DTO/cliente de stats por canto (``GET /bouts/{boutId}/stats``); o mapeamento puro do
core da luta (token de método Cito -> ``BoutMethod``, canto vencedor); o upsert de
``bouts`` por chave natural order-independent (cantos R/B trocados não duplicam a luta);
e o upsert de ``bout_fighters`` com as stats granulares por lutador (red != blue, nunca
pré-agregado), tudo idempotente e com ``source="cito"``.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.client import (
    DEFAULT_CALL_BUDGET,
    CallBudget,
    CitoClient,
    QuotaExceededError,
)
from ingestion.cito.dto import CitoBout, CitoBoutStats, CitoCorner, CitoEvent
from ingestion.incremental import (
    main,
    map_bout_core,
    resolve_call_budget,
    run_incremental,
    upsert_bout,
    upsert_bout_fighters,
    upsert_event,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_EVENT_ID = "ufc-319"
# Custo determinístico do evento fixture: 1 fetch_event + 4 get_fighter + 2 fetch_bout_stats.
_EVENT_CITO_CALLS = 7


def _cito_event(name: str = "UFC 319: Du Plessis vs. Chimaev") -> CitoEvent:
    return CitoEvent(event_id=_EVENT_ID, name=name, date=date(2025, 8, 16), bouts=[])


def _fixture_client(budget: CallBudget | None = None) -> CitoClient:
    return CitoClient(token="", base_url="https://mmaapi.dev", fixture_dir=_FIXTURES, budget=budget)


def _cito_bout(
    *,
    method: str | None = "KO/TKO",
    winner_slug: str | None = "red-fighter",
    finish_round: int | None = 2,
    finish_time_seconds: int | None = 143,
    weight_class: str | None = "Lightweight",
) -> CitoBout:
    """Constrói uma ``CitoBout`` com red=``red-fighter`` e blue=``blue-fighter``."""
    return CitoBout(
        bout_id="bout-x",
        method=method,
        finish_round=finish_round,
        finish_time_seconds=finish_time_seconds,
        weight_class=weight_class,
        winner_slug=winner_slug,
        corners=(
            CitoCorner(slug="red-fighter", name="Red Fighter"),
            CitoCorner(slug="blue-fighter", name="Blue Fighter"),
        ),
    )


def _seed_fighter(session: Session, name: str) -> int:
    """Insere um ``Fighter`` mínimo (do seed Kaggle) e devolve o id materializado."""
    from ingestion.normalize import normalize_name

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


def _seed_event(session: Session) -> int:
    """Insere um ``Event`` e devolve o id materializado."""
    event = Event(
        name="UFC 319: Du Plessis vs. Chimaev",
        date=date(2025, 8, 16),
        location=None,
        source="cito",
    )
    session.add(event)
    session.flush()
    return event.id


def _bout_stats(
    *, red_id_slug: str = "red-fighter", blue_id_slug: str = "blue-fighter"
) -> CitoBoutStats:
    """Stats granulares com red e blue distintos (red != blue -- nunca média)."""
    return CitoBoutStats.model_validate(
        {
            "bout_id": "bout-x",
            "fighters": [
                {
                    "corner": "red",
                    "fighter_slug": red_id_slug,
                    "knockdowns": 1,
                    "sig_strikes_landed": 50,
                    "sig_strikes_attempted": 100,
                    "takedowns_landed": 2,
                    "takedowns_attempted": 4,
                    "submission_attempts": 1,
                    "control_time_seconds": 120,
                },
                {
                    "corner": "blue",
                    "fighter_slug": blue_id_slug,
                    "knockdowns": 0,
                    "sig_strikes_landed": 40,
                    "sig_strikes_attempted": 90,
                    "takedowns_landed": 0,
                    "takedowns_attempted": 1,
                    "submission_attempts": 0,
                    "control_time_seconds": 15,
                },
            ],
        }
    )


def test_upsert_event_insere_com_source_cito(db_session: Session) -> None:
    """CA-01: o upsert insere o evento e grava ``source="cito"``."""
    inserted = upsert_event(db_session, _cito_event())
    assert inserted == 1

    (event,) = db_session.scalars(select(Event)).all()
    assert event.name == "UFC 319: Du Plessis vs. Chimaev"
    assert event.date == date(2025, 8, 16)
    assert event.source == "cito"


def test_upsert_event_e_idempotente(db_session: Session) -> None:
    """CA-02: a segunda chamada com o mesmo evento não insere nada; contagem estável."""
    first = upsert_event(db_session, _cito_event())
    second = upsert_event(db_session, _cito_event())

    assert first == 1
    assert second == 0
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_upsert_event_nao_duplica_cross_source_com_grafia_divergente(db_session: Session) -> None:
    """CA-05: evento já semeado do Kaggle (grafia divergente) não duplica; source preservado."""
    db_session.add(
        Event(
            name="UFC 319: Du Plessis vs. Chimaev",
            date=date(2025, 8, 16),
            location="Chicago, Illinois, USA",
            source="kaggle",
        )
    )
    db_session.flush()

    # Mesmo evento, grafia divergente em caixa/acento/espaço.
    inserted = upsert_event(db_session, _cito_event(name="  UFC 319:  DU PLESSIS vs. CHIMÁEV  "))
    assert inserted == 0

    (event,) = db_session.scalars(select(Event)).all()
    assert event.source == "kaggle"  # a linha existente não é tocada
    assert db_session.scalar(select(func.count()).select_from(Event)) == 1


def test_resolve_call_budget_cli_vence_env_vence_default() -> None:
    """CA-07: a resolução do teto respeita CLI > env (``CITO_CALL_BUDGET``) > default (500)."""
    assert resolve_call_budget(42, {}) == 42
    assert resolve_call_budget(42, {"CITO_CALL_BUDGET": "123"}) == 42
    assert resolve_call_budget(None, {"CITO_CALL_BUDGET": "123"}) == 123
    assert resolve_call_budget(None, {}) == DEFAULT_CALL_BUDGET


def test_resolve_call_budget_env_nao_numerico_falha_com_mensagem_clara() -> None:
    """CA-07: ``CITO_CALL_BUDGET`` não-numérico levanta ``ValueError`` explícito (não o cru do int).

    Uma string vazia recai no default (ausência de override); um valor não-inteiro é erro de
    configuração e deve falhar com mensagem clara que nomeia a variável e o valor recebido.
    """
    assert resolve_call_budget(None, {"CITO_CALL_BUDGET": ""}) == DEFAULT_CALL_BUDGET

    with pytest.raises(ValueError, match="CITO_CALL_BUDGET") as excinfo:
        resolve_call_budget(None, {"CITO_CALL_BUDGET": "quinhentos"})
    assert "quinhentos" in str(excinfo.value)


def test_main_env_call_budget_invalido_encerra_com_erro_de_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-07: env ``CITO_CALL_BUDGET`` inválido encerra o CLI com exit code != 0 e log claro.

    O erro de configuração é resolvido antes de qualquer fetch ou escrita; ``main`` captura o
    ``ValueError`` de ``resolve_call_budget``, emite via ``logger.error`` e sai com código != 0,
    sem propagar traceback cru ao operador.
    """
    monkeypatch.setenv("CITO_CALL_BUDGET", "nao-numerico")

    with (
        caplog.at_level(logging.ERROR, logger="ingestion.incremental"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--event", _EVENT_ID, "--fixture"])

    assert excinfo.value.code != 0
    assert "config" in caplog.text.lower()
    error_records = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert error_records
    assert all(rec.exc_info is None for rec in error_records)


def test_run_incremental_resumo_reporta_deltas_e_chamadas(db_session: Session) -> None:
    """CA-01/CA-02/CA-07: o resumo traz inseridos/atualizados por tabela + chamadas gastas.

    Primeira execução insere evento + 4 lutadores + 2 lutas + 4 ``bout_fighters`` (todos com
    ``source="cito"``) e contabiliza as chamadas dentro do teto; a reexecução reporta 0
    inseridos e os mesmos N como atualizados (idempotência refletida no resumo).
    """
    first_budget = CallBudget(limit=DEFAULT_CALL_BUDGET)
    first = run_incremental(
        db_session, event_id=_EVENT_ID, client=_fixture_client(first_budget), budget=first_budget
    )

    assert first.source == "cito"
    assert (first.events.inserted, first.events.updated) == (1, 0)
    assert (first.fighters.inserted, first.fighters.updated) == (4, 0)
    assert (first.bouts.inserted, first.bouts.updated) == (2, 0)
    assert (first.bout_fighters.inserted, first.bout_fighters.updated) == (4, 0)
    assert first.cito_calls_used == _EVENT_CITO_CALLS
    assert 0 < first.cito_calls_used <= first_budget.limit

    second_budget = CallBudget(limit=DEFAULT_CALL_BUDGET)
    second = run_incremental(
        db_session, event_id=_EVENT_ID, client=_fixture_client(second_budget), budget=second_budget
    )

    # Rerun: nada inserido; cada linha vira "atualizada" (encontrada por chave natural).
    assert (second.events.inserted, second.events.updated) == (0, 1)
    assert (second.fighters.inserted, second.fighters.updated) == (0, 4)
    assert (second.bouts.inserted, second.bouts.updated) == (0, 2)
    assert (second.bout_fighters.inserted, second.bout_fighters.updated) == (0, 4)
    assert second.cito_calls_used == _EVENT_CITO_CALLS

    (event,) = db_session.scalars(select(Event)).all()
    assert event.name == "UFC 319: Du Plessis vs. Chimaev"
    assert event.source == "cito"


def test_run_incremental_estouro_de_teto_reverte_escritas_do_evento(db_session: Session) -> None:
    """CA-08: estourar o teto no meio do evento reverte tudo via SAVEPOINT (nada parcial).

    Com teto 1, ``fetch_event`` consome a única unidade e insere o evento; o primeiro
    ``get_fighter`` estoura o teto. O ``begin_nested`` reverte o evento já inserido e a exceção
    propaga -- nenhuma das quatro tabelas retém linha parcial, deixando o banco consistente
    para o retry idempotente.
    """
    budget = CallBudget(limit=1)

    with pytest.raises(QuotaExceededError):
        run_incremental(
            db_session, event_id=_EVENT_ID, client=_fixture_client(budget), budget=budget
        )

    assert db_session.scalar(select(func.count()).select_from(Event)) == 0
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == 0
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == 0


def test_main_estouro_de_teto_sai_com_erro_e_loga_sem_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-08: estourar o teto no CLI encerra com exit code != 0 e mensagem clara via ``logger``.

    Com teto 0, o primeiro ``fetch_event`` já estoura antes de qualquer escrita; o SAVEPOINT
    garante zero parcial. O ``main`` deve capturar a ``QuotaExceededError``, emitir a mensagem
    via ``logger.error`` (sem propagar traceback cru) e sair com ``sys.exit(1)``.
    """
    with (
        caplog.at_level(logging.ERROR, logger="ingestion.incremental"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--event", _EVENT_ID, "--fixture", "--call-budget", "0"])

    assert excinfo.value.code != 0
    assert "interrompida" in caplog.text.lower()
    # A exceção foi tratada (log de erro), não propagada como traceback cru ao operador.
    error_records = [rec for rec in caplog.records if rec.levelno == logging.ERROR]
    assert error_records
    assert all(rec.exc_info is None for rec in error_records)


def test_cito_bout_stats_dto_parseia_dois_cantos_e_degrada_ausencia() -> None:
    """CA-01: o payload de stats vira ``CitoBoutStats`` com dois cantos; ausência -> ``None``."""
    payload = (_FIXTURES / "cito_bout_stats_sample.json").read_text(encoding="utf-8")
    stats = CitoBoutStats.model_validate_json(payload)

    assert stats.bout_id == "sample-bout"
    assert [f.corner for f in stats.fighters] == [Corner.RED, Corner.BLUE]

    red, blue = stats.fighters
    assert red.fighter_slug == "fighter-red"
    assert red.knockdowns == 1
    assert red.sig_strikes_landed == 50
    assert red.control_time_seconds == 120
    # O canto azul omite ``submission_attempts`` e ``control_time_seconds`` no payload.
    assert blue.submission_attempts is None
    assert blue.control_time_seconds is None


def test_fetch_bout_stats_em_modo_fixture() -> None:
    """CA-01: o cliente em modo fixture devolve ``CitoBoutStats`` sem tocar a rede."""
    client = _fixture_client()

    stats = client.fetch_bout_stats("ufc-319-bout-1")

    assert stats.bout_id == "ufc-319-bout-1"
    by_corner = {f.corner: f for f in stats.fighters}
    assert by_corner[Corner.RED].fighter_slug == "dricus-du-plessis"
    assert by_corner[Corner.BLUE].fighter_slug == "khamzat-chimaev"
    assert by_corner[Corner.BLUE].control_time_seconds == 1300


def test_map_bout_core_cito_mapeia_metodo_e_canto_vencedor() -> None:
    """CA-02: token de método -> ``BoutMethod``, round/tempo, canto vencedor pelo slug."""
    core = map_bout_core(
        _cito_bout(method="Decision - Unanimous", winner_slug="blue-fighter", finish_round=3)
    )

    assert core["method"] is BoutMethod.DECISION
    assert core["round"] == 3
    assert core["ending_time_seconds"] == 143
    assert core["weight_class"] == "Lightweight"
    assert core["winner_corner"] is Corner.BLUE


def test_map_bout_core_cito_token_nao_previsto_degrada_para_no_contest() -> None:
    """CA-02: token de método fora do mapa Cito degrada conservadoramente para ``NO_CONTEST``."""
    core = map_bout_core(_cito_bout(method="Alien Finish", winner_slug="red-fighter"))

    assert core["method"] is BoutMethod.NO_CONTEST
    # No contest nunca tem vencedor, mesmo com ``winner_slug`` no payload.
    assert core["winner_corner"] is None


def test_map_bout_core_cito_sem_vencedor_tem_canto_nulo() -> None:
    """CA-02: luta sem ``winner_slug`` (empate) tem canto vencedor nulo, método preservado."""
    core = map_bout_core(_cito_bout(method="Decision - Split", winner_slug=None))

    assert core["method"] is BoutMethod.DECISION
    assert core["winner_corner"] is None


def test_upsert_bout_insere_com_source_e_resultado(db_session: Session) -> None:
    """CA-02: o upsert cria a luta com ``source="cito"``, vencedor, método, round e tempo."""
    event_id = _seed_event(db_session)
    red_id = _seed_fighter(db_session, "Red Fighter")
    blue_id = _seed_fighter(db_session, "Blue Fighter")
    core = map_bout_core(_cito_bout(method="Submission", winner_slug="red-fighter", finish_round=2))

    bout_id = upsert_bout(db_session, event_id, red_id, blue_id, core)

    bout = db_session.get(Bout, bout_id)
    assert bout is not None
    assert bout.event_id == event_id
    assert bout.winner_id == red_id
    assert bout.method is BoutMethod.SUBMISSION
    assert bout.round == 2
    assert bout.ending_time_seconds == 143
    assert bout.weight_class == "Lightweight"
    assert bout.source == "cito"


def test_upsert_bout_e_idempotente(db_session: Session) -> None:
    """CA-03: reexecutar o upsert do mesmo bout não cria segunda linha; devolve o mesmo id.

    A chave natural da luta materializa-se pelos ``bout_fighters`` (ADR 0001: não há par de
    lutadores na própria ``bouts``), por isso a idempotência é observada com as linhas long
    persistidas -- exatamente como o job faz (``upsert_bout`` sempre seguido de
    ``upsert_bout_fighters``).
    """
    event_id = _seed_event(db_session)
    red_id = _seed_fighter(db_session, "Red Fighter")
    blue_id = _seed_fighter(db_session, "Blue Fighter")
    core = map_bout_core(_cito_bout())
    fighter_id_by_corner = {Corner.RED: red_id, Corner.BLUE: blue_id}

    first = upsert_bout(db_session, event_id, red_id, blue_id, core)
    upsert_bout_fighters(db_session, first, _bout_stats(), fighter_id_by_corner)
    second = upsert_bout(db_session, event_id, red_id, blue_id, core)

    assert first == second
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 1


def test_upsert_bout_chave_order_independent_cantos_invertidos(db_session: Session) -> None:
    """CA-04: trocar os cantos R/B entre execuções não cria luta duplicada (par não-ordenado).

    Com os ``bout_fighters`` persistidos, o índice reconstrói a chave por ``sorted((fid, fid))``;
    a segunda execução com os lutadores nos cantos trocados casa a mesma luta.
    """
    event_id = _seed_event(db_session)
    red_id = _seed_fighter(db_session, "Red Fighter")
    blue_id = _seed_fighter(db_session, "Blue Fighter")
    core = map_bout_core(_cito_bout())

    first = upsert_bout(db_session, event_id, red_id, blue_id, core)
    upsert_bout_fighters(
        db_session, first, _bout_stats(), {Corner.RED: red_id, Corner.BLUE: blue_id}
    )
    # Segunda execução com os lutadores nos cantos trocados.
    second = upsert_bout(db_session, event_id, blue_id, red_id, core)

    assert first == second
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 1


def test_upsert_bout_fighters_grava_stats_granulares_por_canto(db_session: Session) -> None:
    """CA-02/CA-05: uma linha por canto, stats do fixture (red != blue), ``source="cito"``."""
    event_id = _seed_event(db_session)
    red_id = _seed_fighter(db_session, "Red Fighter")
    blue_id = _seed_fighter(db_session, "Blue Fighter")
    bout_id = upsert_bout(db_session, event_id, red_id, blue_id, map_bout_core(_cito_bout()))
    fighter_id_by_corner = {Corner.RED: red_id, Corner.BLUE: blue_id}

    inserted = upsert_bout_fighters(db_session, bout_id, _bout_stats(), fighter_id_by_corner)

    assert inserted == 2
    rows = {
        row.fighter_id: row
        for row in db_session.scalars(
            select(BoutFighter).where(BoutFighter.bout_id == bout_id)
        ).all()
    }
    assert set(rows) == {red_id, blue_id}
    assert rows[red_id].corner is Corner.RED
    assert rows[red_id].sig_strikes_landed == 50
    assert rows[red_id].control_time_seconds == 120
    assert rows[red_id].source == "cito"
    # As stats do azul são distintas das do vermelho -- granular, nunca média.
    assert rows[blue_id].corner is Corner.BLUE
    assert rows[blue_id].sig_strikes_landed == 40
    assert rows[blue_id].control_time_seconds == 15


def test_upsert_bout_fighters_e_idempotente(db_session: Session) -> None:
    """CA-03: reexecutar o upsert das linhas por canto não duplica ``bout_fighters``."""
    event_id = _seed_event(db_session)
    red_id = _seed_fighter(db_session, "Red Fighter")
    blue_id = _seed_fighter(db_session, "Blue Fighter")
    bout_id = upsert_bout(db_session, event_id, red_id, blue_id, map_bout_core(_cito_bout()))
    fighter_id_by_corner = {Corner.RED: red_id, Corner.BLUE: blue_id}

    first = upsert_bout_fighters(db_session, bout_id, _bout_stats(), fighter_id_by_corner)
    second = upsert_bout_fighters(db_session, bout_id, _bout_stats(), fighter_id_by_corner)

    assert first == 2
    assert second == 0
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == 2


def test_incremental_bouts_end_to_end_idempotente_com_teto(db_session: Session) -> None:
    """CA-01/CA-02/CA-07: o job cria bouts+bout_fighters com ``source``; rerun completo é no-op.

    A idempotência ponta a ponta é observável no estado do banco (contagens estáveis) e nas
    chamadas contabilizadas dentro do teto, exatamente o valor demonstrável da Slice 04.
    """
    first_budget = CallBudget(limit=DEFAULT_CALL_BUDGET)
    first = run_incremental(
        db_session, event_id=_EVENT_ID, client=_fixture_client(first_budget), budget=first_budget
    )

    assert (first.bouts.inserted, first.bout_fighters.inserted) == (2, 4)
    assert first.cito_calls_used == _EVENT_CITO_CALLS
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 2
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == 4
    # Toda escrita da luta rastreia a origem.
    assert all(source == "cito" for source in db_session.scalars(select(Bout.source)).all())
    assert all(source == "cito" for source in db_session.scalars(select(BoutFighter.source)).all())
    # O vencedor da luta principal (Chimaev, canto azul) foi gravado.
    main_bout = db_session.scalars(select(Bout).where(Bout.weight_class == "Middleweight")).one()
    assert main_bout.method is BoutMethod.DECISION
    assert main_bout.round == 5

    second_budget = CallBudget(limit=DEFAULT_CALL_BUDGET)
    second = run_incremental(
        db_session, event_id=_EVENT_ID, client=_fixture_client(second_budget), budget=second_budget
    )

    assert (second.bouts.inserted, second.bout_fighters.inserted) == (0, 0)
    assert second.cito_calls_used == _EVENT_CITO_CALLS
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 2
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == 4
