"""Os quatro consumidores de ``ufc_official`` diante de uma luta agendada (SPEC 011, Slice 02).

Pontos 7, 11, 12 e 13 da tabela de varredura de ``bouts.status``, reunidos num arquivo só
porque compartilham a mesma semeadura: um evento **dentro** da janela da fonte oficial, com
uma luta realizada e uma agendada, e um lutador que aparece **apenas** na agendada -- o
"estreante de card futuro", que é quem os pontos 7 e 13 existem para proteger.

Os quatro se dividem em duas decisões opostas, e é isso que o arquivo fixa:

- **Filtram ``completed``** (11 e 12) -- a trava desses módulos é ``event.date >=
  OFFICIAL_WINDOW_START``, que **só protege o passado**; o caminho ``--event-slug`` alcança
  evento agendado, que tem ``cito_slug`` como qualquer outro.
- **NÃO filtram** (7 e 13) -- e isso é **decisão ativa**, não omissão: filtrar excluiria o
  estreante de card futuro do casamento por roster e da população que recebe identificador,
  justamente quem mais precisa de um.

Toda luta agendada é semeada **à mão**: a ingestão que as escreve é da Slice 04, que esta
slice precede obrigatoriamente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.anthropometry import (
    _count_window_fighters_without_identifier,
    _window_fighters_by_identifier,
)
from ingestion.ufc_official.corner import _persisted_bouts
from ingestion.ufc_official.discovery import persisted_rosters
from ingestion.ufc_official.fighter_ids import _select_window_fighters
from ingestion.ufc_official.granular_measurement import load_persisted_lines

# Data dentro da janela da fonte oficial, para os módulos que a exigem não levantarem.
_DATA_NA_JANELA = date(OFFICIAL_WINDOW_START.year + 10, 6, 1)


@dataclass(frozen=True)
class _Cenario:
    """O evento semeado e os quatro lutadores, nomeados pelo papel que exercem no teste."""

    event: Event
    realizada_red: Fighter
    realizada_blue: Fighter
    estreante: Fighter  # aparece APENAS na luta agendada
    veterano: Fighter  # o outro canto da agendada; também luta na realizada


def _add_fighter(session: Session, name: str, *, ufc_fighter_id: str | None) -> Fighter:
    """Semeia um lutador mínimo; ``ufc_fighter_id`` nulo simula quem a Slice 03 não mapeou."""
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
        ufc_fighter_id=ufc_fighter_id,
        source="kaggle",
    )
    session.add(fighter)
    session.flush()
    return fighter


def _add_bout(
    session: Session,
    *,
    event: Event,
    red: Fighter,
    blue: Fighter,
    status: BoutStatus,
    com_rounds: bool,
) -> Bout:
    """Semeia uma luta com os dois cantos e, opcionalmente, o round-a-round de cada canto."""
    realizada = status is BoutStatus.COMPLETED
    bout = Bout(
        event_id=event.id,
        winner_id=red.id if realizada else None,
        method=BoutMethod.DECISION if realizada else None,
        round=3 if realizada else None,
        ending_time_seconds=300 if realizada else None,
        weight_class=None,
        status=status,
        source="kaggle",
    )
    session.add(bout)
    session.flush()
    for corner, fighter in ((Corner.RED, red), (Corner.BLUE, blue)):
        canto = BoutFighter(
            bout_id=bout.id,
            fighter_id=fighter.id,
            corner=corner,
            sig_strikes_landed=30 if realizada else None,
            source="kaggle",
        )
        session.add(canto)
        session.flush()
        if com_rounds:
            session.add(
                BoutFighterRound(
                    bout_fighter_id=canto.id,
                    round=1,
                    sig_strikes_landed=10,
                    source="cito",
                )
            )
    session.flush()
    return bout


@pytest.fixture
def cenario(db_session: Session) -> _Cenario:
    """Um evento da janela com uma luta realizada e uma agendada, e um estreante só na agendada."""
    event = Event(
        name="UFC Evento Da Janela",
        date=_DATA_NA_JANELA,
        location=None,
        source="kaggle",
        cito_slug="ufc-evento-da-janela",
        ufc_event_id="evento-oficial-1",
    )
    db_session.add(event)
    db_session.flush()

    realizada_red = _add_fighter(db_session, "Veterano Alpha", ufc_fighter_id="oficial-alpha")
    realizada_blue = _add_fighter(db_session, "Veterano Bravo", ufc_fighter_id="oficial-bravo")
    # O estreante não tem identificador externo: é ele que o ponto 13 conta no teto da cobertura.
    estreante = _add_fighter(db_session, "Estreante Charlie", ufc_fighter_id=None)

    _add_bout(
        db_session,
        event=event,
        red=realizada_red,
        blue=realizada_blue,
        status=BoutStatus.COMPLETED,
        com_rounds=True,
    )
    _add_bout(
        db_session,
        event=event,
        red=realizada_red,
        blue=estreante,
        status=BoutStatus.SCHEDULED,
        com_rounds=False,
    )
    return _Cenario(
        event=event,
        realizada_red=realizada_red,
        realizada_blue=realizada_blue,
        estreante=estreante,
        veterano=realizada_red,
    )


def test_a_semeadura_tem_mesmo_as_duas_lutas(db_session: Session, cenario: _Cenario) -> None:
    """Guarda de não-vacuidade: sem isto, os testes abaixo passariam com a semeadura quebrada.

    Quatro cantos ao todo -- dois da realizada, dois da agendada --, e é por ``bout_fighters``
    que a luta agendada vaza para todos os quatro consumidores deste arquivo.
    """
    lutas = db_session.query(Bout).filter(Bout.event_id == cenario.event.id).all()

    assert {luta.status for luta in lutas} == {BoutStatus.COMPLETED, BoutStatus.SCHEDULED}
    assert sum(len(luta.bout_fighters) for luta in lutas) == 4


def test_ponto_11_load_persisted_lines_ignora_a_agendada(
    db_session: Session, cenario: _Cenario
) -> None:
    """Ponto 11: a medição do granular só enxerga luta realizada, nas **duas** projeções.

    A trava do módulo (``event.date < OFFICIAL_WINDOW_START``) só protege o passado e deixa
    2026 passar; o caminho ``--event-slug`` alcança evento agendado, que tem ``cito_slug``.
    Sem o filtro, os dois cantos da agendada entrariam na projeção de totais com todos os
    campos nulos e seriam contados como divergência contra a fonte oficial.
    """
    lines = load_persisted_lines(db_session, cenario.event)

    nomes = {linha.fighter_name_normalized for linha, _ in lines}
    assert normalize_name(cenario.estreante.name) not in nomes
    assert nomes == {
        normalize_name(cenario.realizada_red.name),
        normalize_name(cenario.realizada_blue.name),
    }
    # Dois cantos (``round is None``) + dois rounds -- nada da agendada, que não tem rounds.
    assert sum(1 for linha, _ in lines if linha.round is None) == 2
    assert sum(1 for linha, _ in lines if linha.round is not None) == 2


def test_ponto_12_persisted_bouts_ignora_a_agendada(db_session: Session, cenario: _Cenario) -> None:
    """Ponto 12: a população que a comparação de canto percorre exclui a luta agendada.

    É esta consulta que decide a população -- a de ``:565`` já recebe ``bout_ids`` derivados
    daqui. Sem o filtro, a agendada entraria em ``uncovered_bouts`` a cada execução, poluindo
    a métrica de cobertura com uma luta que a fonte oficial ainda não tem como cobrir.
    """
    agendada = (
        db_session.query(Bout)
        .filter(Bout.event_id == cenario.event.id, Bout.status == BoutStatus.SCHEDULED)
        .one()
    )

    persistidas = _persisted_bouts(db_session, cenario.event)

    assert agendada.id not in {luta.bout_id for luta in persistidas}
    assert len(persistidas) == 1


def test_ponto_7_persisted_rosters_inclui_o_estreante(
    db_session: Session, cenario: _Cenario
) -> None:
    """Ponto 7: o roster **não** filtra ``status`` -- decisão ativa.

    O roster de um evento agendado é insumo legítimo do casamento por interseção (tier 2 do
    M7): o card futuro é exatamente o conjunto de nomes que a fonte publica antes do evento.
    Filtrar aqui empobreceria a interseção justamente no caso em que ela é mais necessária.
    """
    rosters = persisted_rosters(db_session, [cenario.event])

    assert normalize_name(cenario.estreante.name) in rosters[cenario.event.id]
    assert len(rosters[cenario.event.id]) == 3


def test_ponto_13_populacao_da_janela_inclui_o_estreante(
    db_session: Session, cenario: _Cenario
) -> None:
    """Ponto 13: as três consultas de população da janela **não** filtram ``status``.

    É o único ponto da varredura em que "não filtrar" é a decisão ativa. O estreante de card
    futuro é quem mais precisa de identificador externo -- é para ele que a próxima execução
    de ``fighter_ids`` existe --, e excluí-lo o deixaria permanentemente sem id, com a
    cobertura reportando 100% sobre uma população artificialmente encolhida.
    """
    da_janela = _select_window_fighters(db_session, OFFICIAL_WINDOW_START)
    por_identificador = _window_fighters_by_identifier(db_session)
    sem_identificador = _count_window_fighters_without_identifier(db_session)

    assert cenario.estreante.id in {fighter.id for fighter in da_janela}
    # O estreante não tem id externo, então não aparece no índice por identificador -- mas
    # **conta** no teto da cobertura, que é o número que revela o trabalho que falta.
    assert set(por_identificador) == {"oficial-alpha", "oficial-bravo"}
    assert sem_identificador == 1
