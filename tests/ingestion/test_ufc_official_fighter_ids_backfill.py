"""Testes do backfill de ``fighters.ufc_fighter_id``/``ufc_mma_id`` (Sprint 008-03).

Cobrem CA-04 e CA-05 contra o Postgres de teste (sessão transacional), no molde de
``tests/ingestion/test_sync_catalog.py``: cobertura e relatório, idempotência (segunda execução
sem escrita nenhuma), divergência reportada e **não** sobrescrita, colisão de identificador
entre dois lutadores persistidos, e a camada de argumentos do comando.

O cenário sai da captura **verbatim** de UFC 282: Blachowicz vs. Ankalaev
(``fixtures/ufc_official/event_live_1124.json``). Nenhum teste toca a rede: a execução é
offline, lendo o diretório de fixtures como cache.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.fighter_ids import (
    FighterIdBackfillReport,
    _parse_args,
    run_fighter_id_backfill,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

_UFC_282 = ("UFC 282: Blachowicz vs. Ankalaev", date(2022, 12, 10), "1124")
# Dois pares reais do card, com os identificadores que a fonte traz para cada canto.
_TILL = ("Darren Till", "2544", "143974")
_DU_PLESSIS = ("Dricus Du Plessis", "3599", "163843")
_BLACHOWICZ = ("Jan Blachowicz", "2300", "140580")
_ANKALAEV = ("Magomed Ankalaev", "3046", "155703")


def _semeia_lutador(
    session: Session,
    nome: str,
    *,
    dob: date | None = None,
    ufc_fighter_id: str | None = None,
) -> Fighter:
    lutador = Fighter(
        name=nome,
        name_normalized=normalize_name(nome),
        nickname=None,
        date_of_birth=dob,
        height_cm=None,
        reach_cm=None,
        stance=None,
        weight_kg=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
        ufc_fighter_id=ufc_fighter_id,
    )
    session.add(lutador)
    session.flush()
    return lutador


def _semeia_evento(session: Session, nome: str, quando: date, ufc_event_id: str | None) -> Event:
    evento = Event(
        name=nome, date=quando, location=None, source="kaggle", ufc_event_id=ufc_event_id
    )
    session.add(evento)
    session.flush()
    return evento


def _semeia_luta(session: Session, evento: Event, *lutadores: Fighter) -> Bout:
    luta = Bout(
        event_id=evento.id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    session.add(luta)
    session.flush()
    # ``bout_fighters.corner`` é NOT NULL; o canto não participa desta slice (o que se coleta é
    # **identidade**, nunca desfecho), então a atribuição aqui é só para semear.
    for canto, lutador in zip((Corner.RED, Corner.BLUE), lutadores, strict=True):
        session.add(
            BoutFighter(bout_id=luta.id, fighter_id=lutador.id, corner=canto, source="kaggle")
        )
    session.flush()
    return luta


def _cenario(db_session: Session) -> dict[str, Fighter]:
    """Duas lutas reais do card de UFC 282, com os quatro lutadores sem identificador."""
    evento = _semeia_evento(db_session, *_UFC_282)
    lutadores = {
        nome: _semeia_lutador(db_session, nome)
        for nome, _, _ in (_TILL, _DU_PLESSIS, _BLACHOWICZ, _ANKALAEV)
    }
    _semeia_luta(db_session, evento, lutadores[_TILL[0]], lutadores[_DU_PLESSIS[0]])
    _semeia_luta(db_session, evento, lutadores[_BLACHOWICZ[0]], lutadores[_ANKALAEV[0]])
    return lutadores


def _backfill(db_session: Session) -> FighterIdBackfillReport:
    """Execução offline: lê o diretório de fixtures como cache, sem tocar a rede."""
    return run_fighter_id_backfill(db_session, None, OfficialEventCache(_FIXTURES))


# --------------------------------------------------------------------------- #
# CA-04 -- cobertura e relatório
# --------------------------------------------------------------------------- #


def test_backfill_preenche_os_dois_identificadores_dos_lutadores_da_janela(
    db_session: Session,
) -> None:
    """CA-04: cada lutador da janela recebe ``ufc_fighter_id`` e ``ufc_mma_id`` da fonte."""
    lutadores = _cenario(db_session)

    _backfill(db_session)

    for nome, ufc_fighter_id, ufc_mma_id in (_TILL, _DU_PLESSIS, _BLACHOWICZ, _ANKALAEV):
        assert lutadores[nome].ufc_fighter_id == ufc_fighter_id
        assert lutadores[nome].ufc_mma_id == ufc_mma_id


def test_relatorio_traz_cobertura_atribuidos_e_ja_preenchidos(db_session: Session) -> None:
    """CA-04: o relatório é acionável -- contagem da janela, escritas e cobertura."""
    _cenario(db_session)

    relatorio = _backfill(db_session)

    assert isinstance(relatorio, FighterIdBackfillReport)
    assert relatorio.fighters_in_window == 4
    assert relatorio.assigned == 4
    assert relatorio.already_filled == 0
    assert relatorio.coverage == pytest.approx(1.0)
    assert relatorio.divergent == ()
    assert relatorio.conflicting == ()
    assert relatorio.colliding == ()


def test_backfill_preserva_o_source_da_linha_semeada_do_kaggle(db_session: Session) -> None:
    """``source`` é origem da LINHA, não do campo: preencher o id não muda ``source``.

    Decisão do humano registrada no CLAUDE.md em 2026-09-01 -- e não se compõe valor
    (``"kaggle+ufc"`` é comparado por igualdade por consumidores reais e quebraria).
    """
    lutadores = _cenario(db_session)

    _backfill(db_session)

    assert {lutador.source for lutador in lutadores.values()} == {"kaggle"}


def test_base_sem_lutador_na_janela_nao_divide_por_zero(db_session: Session) -> None:
    """CA-04: cobertura de uma base vazia é ``0.0``, não uma exceção."""
    relatorio = _backfill(db_session)

    assert relatorio.fighters_in_window == 0
    assert relatorio.coverage == 0.0


def test_luta_sem_correspondencia_na_fonte_aparece_no_relatorio(db_session: Session) -> None:
    """CA-04: o relatório nomeia a luta que não casou -- contagem sozinha não é acionável."""
    evento = _semeia_evento(db_session, *_UFC_282)
    luta = _semeia_luta(
        db_session,
        evento,
        _semeia_lutador(db_session, "Ninguem Um"),
        _semeia_lutador(db_session, "Ninguem Dois"),
    )

    relatorio = _backfill(db_session)

    assert relatorio.unmatched_bouts == ((luta.id, _UFC_282[0]),)
    assert relatorio.assigned == 0


def test_relatorio_sai_por_logging(db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    """CA-04: a cobertura é logada (``print`` é proibido pela regra T20 do ruff)."""
    _cenario(db_session)

    with caplog.at_level(logging.INFO, logger="ingestion.ufc_official.fighter_ids"):
        _backfill(db_session)

    assert "cobertura" in caplog.text.lower()


# --------------------------------------------------------------------------- #
# CA-05 -- idempotência, divergência e colisão
# --------------------------------------------------------------------------- #


@pytest.fixture
def updates_emitidos(db_session: Session) -> Iterator[list[str]]:
    """Espia o SQL emitido na sessão e coleta os ``UPDATE`` de ``fighters``."""
    coletados: list[str] = []

    def _antes_do_cursor(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE FIGHTERS"):
            coletados.append(statement)

    bind = db_session.get_bind()
    sa_event.listen(bind, "before_cursor_execute", _antes_do_cursor)
    try:
        yield coletados
    finally:
        sa_event.remove(bind, "before_cursor_execute", _antes_do_cursor)


def test_rerun_nao_atribui_nada_e_nao_emite_update(
    db_session: Session, updates_emitidos: list[str]
) -> None:
    """CA-05: a segunda execução reporta ``assigned == 0`` e **não** escreve no banco.

    O campo só é atribuído quando o valor muda: sem isso, cada reexecução sujaria o log de
    replicação com UPDATEs que não mudam nada.
    """
    lutadores = _cenario(db_session)
    primeiro = _backfill(db_session)
    db_session.flush()
    updates_emitidos.clear()

    segundo = _backfill(db_session)
    db_session.flush()

    assert primeiro.assigned == 4
    assert segundo.assigned == 0
    assert segundo.already_filled == 4
    assert segundo.coverage == primeiro.coverage
    assert updates_emitidos == []
    assert lutadores[_TILL[0]].ufc_fighter_id == _TILL[1]


def test_valor_divergente_e_reportado_e_nunca_sobrescrito(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-05 (RF-02 por analogia): id persistido diferente do observado -> reportado, mantido.

    O log nomeia o lutador, o valor persistido e o observado: sem os três, a divergência não é
    inspecionável.
    """
    evento = _semeia_evento(db_session, *_UFC_282)
    till = _semeia_lutador(db_session, _TILL[0], ufc_fighter_id="9999")
    du_plessis = _semeia_lutador(db_session, _DU_PLESSIS[0])
    _semeia_luta(db_session, evento, till, du_plessis)

    with caplog.at_level(logging.WARNING, logger="ingestion.ufc_official.fighter_ids"):
        relatorio = _backfill(db_session)

    assert till.ufc_fighter_id == "9999"
    assert relatorio.divergent == ((till.id, _TILL[0], "9999", _TILL[1]),)
    assert relatorio.assigned == 1  # o adversário, que estava nulo, foi escrito
    for esperado in (_TILL[0], "9999", _TILL[1]):
        assert esperado in caplog.text


def test_identificador_que_ja_pertence_a_outro_lutador_nao_e_escrito(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-05: colisão de identificador é reportada e **nada** é escrito para o lutador.

    É o consumo concreto do branch preferencial por id: ``match_fighter_id`` devolve um
    ``fighter_id`` diferente do alvo, e escrever assim mesmo criaria duas linhas com o mesmo
    identificador externo -- exatamente a violação que a entity resolution passa a detectar.
    """
    evento = _semeia_evento(db_session, *_UFC_282)
    till = _semeia_lutador(db_session, _TILL[0])
    du_plessis = _semeia_lutador(db_session, _DU_PLESSIS[0])
    _semeia_luta(db_session, evento, till, du_plessis)
    intruso = _semeia_lutador(db_session, "Outro Lutador", ufc_fighter_id=_TILL[1])

    with caplog.at_level(logging.WARNING, logger="ingestion.ufc_official.fighter_ids"):
        relatorio = _backfill(db_session)

    assert till.ufc_fighter_id is None
    assert intruso.ufc_fighter_id == _TILL[1]
    assert relatorio.colliding == ((till.id, _TILL[0], _TILL[1], intruso.id),)
    assert relatorio.assigned == 1  # o adversário segue sendo escrito
    assert _TILL[0] in caplog.text


def test_evento_da_janela_sem_ufc_event_id_e_contado(db_session: Session) -> None:
    """CA-04: evento da janela que a Slice 02 não mapeou é o teto da cobertura possível.

    Ele não é falha desta slice -- é dado que a descoberta não conseguiu resolver --, mas
    precisa aparecer no relatório, senão a cobertura fica sem denominador explicável.
    """
    _cenario(db_session)
    _semeia_evento(db_session, "UFC 300: Pereira vs Hill", date(2024, 4, 13), None)

    relatorio = _backfill(db_session)

    assert relatorio.events_without_ufc_event_id == 1


# --------------------------------------------------------------------------- #
# CLI -- só a camada de argumentos (a lógica está coberta acima)
# --------------------------------------------------------------------------- #


def test_parse_args_tem_default_de_cache_e_modo_offline() -> None:
    """O comando roda sem argumento algum, e ``--fixture-dir`` liga o modo offline.

    **Sem gate de quota**: ``--confirmar-gasto-de-quota`` é da Cito. A fonte oficial é gratuita
    e não autenticada, e acrescentar o gate aqui seria cerimônia sem custo a proteger.
    """
    padrao = _parse_args([])
    assert padrao.cache_dir == Path(".cache") / "ufc_official"
    assert padrao.fixture_dir is None

    offline = _parse_args(["--fixture-dir", str(_FIXTURES)])
    assert offline.fixture_dir == _FIXTURES
