"""Testes da extração de identidades por canto e da fronteira da janela (Sprint 008-03).

Dois blocos:

1. **Extração pura** (CA-02) -- sobre as capturas **verbatim** de
   ``fixtures/ufc_official/``, que são o contrato de verdade da fonte: os dois cantos de cada
   luta saem com ``ufc_fighter_id``, ``ufc_mma_id`` e nome normalizado, e o campo ausente
   permanece nulo.
2. **Fronteira da janela** (CA-06) -- contra o Postgres de teste: lutador cujas lutas
   persistidas são todas anteriores a ``OFFICIAL_WINDOW_START`` não recebe identificador, e o
   evento **exatamente** em 2010-03-21 entra (a fronteira é inclusiva).

Nenhum teste toca a rede: a coleta roda em modo offline, lendo o diretório de fixtures como
cache (o mesmo caminho que o ``--fixture-dir`` do comando usa).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.dto import UfcOfficialEvent, parse_event
from ingestion.ufc_official.fighter_ids import (
    collect_identity_observations,
    extract_bout_identities,
    run_fighter_id_backfill,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

# UFC 282: Blachowicz vs. Ankalaev -- a captura principal da Sprint 008-01.
_UFC_282 = 1124
# Till x Du Plessis, a luta de canto azul vencedor da captura principal.
_TILL = "2544"
_DU_PLESSIS = "3599"
# UFC Fight Night: Moraes vs. Sandhagen -- captura desta sprint; traz KB Bhullar **sem**
# ``MMAId`` no payload real (medido: 1 de 26 cantos do card).
_MORAES_SANDHAGEN = 1002
_KB_BHULLAR = "3567"


def _evento(event_id: int) -> UfcOfficialEvent:
    payload = json.loads((_FIXTURES / f"event_live_{event_id}.json").read_text(encoding="utf-8"))
    return parse_event(payload, event_id=event_id)


# --------------------------------------------------------------------------- #
# CA-02 -- extração pura a partir do payload do evento
# --------------------------------------------------------------------------- #


def test_extracao_devolve_os_dois_cantos_com_identificador_e_nome_normalizado() -> None:
    """CA-02: cada luta do card rende os dois cantos com id, ``MMAId`` e nome normalizado.

    Os identificadores saem como **texto**, não inteiro: id externo é opaco e nunca entra em
    aritmética -- é assim que ``fighters.ufc_fighter_id`` os guarda.
    """
    lutas = extract_bout_identities(_evento(_UFC_282))

    assert len(lutas) == 12
    luta = next(
        item
        for item in lutas
        if {canto.ufc_fighter_id for canto in item.corners} == {_TILL, _DU_PLESSIS}
    )
    till = next(canto for canto in luta.corners if canto.ufc_fighter_id == _TILL)
    assert till.ufc_mma_id == "143974"
    assert till.name == "Darren Till"
    assert till.name_normalized == "darren till"
    assert luta.name_key == frozenset({"darren till", "dricus du plessis"})


def test_mma_id_ausente_no_payload_permanece_nulo() -> None:
    """CA-02: campo ausente na fonte permanece **nulo** -- nunca sentinela, nunca string vazia.

    ``MMAId`` falta em 69 de 450 lutadores medidos na sondagem da Sprint 008-01. KB Bhullar é
    o caso desta captura, e o ``FighterId`` dele **está** presente: a ausência é de um campo,
    não do canto inteiro.
    """
    lutas = extract_bout_identities(_evento(_MORAES_SANDHAGEN))

    bhullar = next(
        canto for luta in lutas for canto in luta.corners if canto.ufc_fighter_id == _KB_BHULLAR
    )
    assert bhullar.ufc_mma_id is None
    assert bhullar.name_normalized == "kb bhullar"


def test_chave_de_casamento_e_o_par_nao_ordenado_de_nomes() -> None:
    """CA-02: a chave é um par NÃO-ORDENADO -- troca de cantos não muda o casamento.

    É a mesma chave natural de ``bouts`` desde o M0 (evento + par de lutadores), e é por isso
    que ela é imune à ordem em que a fonte lista os cantos.
    """
    lutas = extract_bout_identities(_evento(_UFC_282))

    for luta in lutas:
        assert len(luta.name_key) == 2
        assert luta.name_key == frozenset(canto.name_normalized for canto in luta.corners)


# --------------------------------------------------------------------------- #
# CA-06 -- fronteira da janela (2010-03-21, inclusiva)
# --------------------------------------------------------------------------- #


def _semeia_luta(
    session: Session,
    *,
    evento: Event,
    nome_a: str,
    nome_b: str,
) -> tuple[Fighter, Fighter]:
    """Semeia dois lutadores do seed Kaggle e a luta que os liga naquele evento."""
    lutadores = []
    for nome in (nome_a, nome_b):
        lutador = Fighter(
            name=nome,
            name_normalized=normalize_name(nome),
            nickname=None,
            date_of_birth=None,
            height_cm=None,
            reach_cm=None,
            stance=None,
            weight_kg=None,
            wins=0,
            losses=0,
            draws=0,
            source="kaggle",
        )
        session.add(lutador)
        lutadores.append(lutador)
    session.flush()

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
    # ``bout_fighters.corner`` é NOT NULL; o canto não participa desta slice (o que se coleta
    # é **identidade**, nunca desfecho), então a atribuição aqui é só para semear.
    for canto, lutador in zip((Corner.RED, Corner.BLUE), lutadores, strict=True):
        session.add(
            BoutFighter(bout_id=luta.id, fighter_id=lutador.id, corner=canto, source="kaggle")
        )
    session.flush()
    return lutadores[0], lutadores[1]


def _semeia_evento(session: Session, nome: str, quando: date, ufc_event_id: str | None) -> Event:
    evento = Event(
        name=nome, date=quando, location=None, source="kaggle", ufc_event_id=ufc_event_id
    )
    session.add(evento)
    session.flush()
    return evento


def test_lutador_ativo_so_antes_da_janela_nao_recebe_identificador(db_session: Session) -> None:
    """CA-06: lutas todas anteriores a 2010-03-21 -> os dois campos permanecem nulos.

    A guarda é **explícita** no ``select`` da coleta, e não um efeito colateral de a Sprint
    008-02 só ter mapeado a janela: o evento aqui tem ``ufc_event_id`` preenchido, e ainda
    assim nada é escrito. CA-08 é invariante de SPEC -- não pode depender do acerto de outra
    sprint.
    """
    anterior = _semeia_evento(
        db_session,
        "UFC 111: St-Pierre vs Hardy",
        OFFICIAL_WINDOW_START - timedelta(days=1),
        ufc_event_id=str(_UFC_282),
    )
    till, du_plessis = _semeia_luta(
        db_session, evento=anterior, nome_a="Darren Till", nome_b="Dricus Du Plessis"
    )

    run_fighter_id_backfill(db_session, None, OfficialEventCache(_FIXTURES))

    assert till.ufc_fighter_id is None
    assert till.ufc_mma_id is None
    assert du_plessis.ufc_fighter_id is None


def test_evento_na_fronteira_exata_entra_na_coleta(db_session: Session) -> None:
    """CA-06: 2010-03-21 **entra** -- a fronteira é inclusiva (UFC Live: Vera vs Jones).

    É a primeira data em que o canto foi realmente registrado; excluí-la perderia um evento
    real por erro de sinal na comparação.
    """
    fronteira = _semeia_evento(
        db_session,
        "UFC 282: Blachowicz vs. Ankalaev",
        OFFICIAL_WINDOW_START,
        ufc_event_id=str(_UFC_282),
    )
    till, du_plessis = _semeia_luta(
        db_session, evento=fronteira, nome_a="Darren Till", nome_b="Dricus Du Plessis"
    )

    coleta = collect_identity_observations(db_session, None, OfficialEventCache(_FIXTURES))

    assert {obs.fighter_id for obs in coleta.observations} == {till.id, du_plessis.id}
    assert {obs.ufc_fighter_id for obs in coleta.observations} == {_TILL, _DU_PLESSIS}
