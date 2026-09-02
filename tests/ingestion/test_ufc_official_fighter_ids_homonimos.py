"""Testes do homônimo resolvido pelo contexto da luta (Sprint 008-03, CA-03).

O caso é real e está na nossa base: dois ``Bruno Silva`` com o **mesmo** ``name_normalized``
(``bruno silva``) e datas de nascimento distintas -- o peso-mosca "Bulldog" (1990-03-16) e o
peso-médio "Blindado" (1989-07-13). O nome sozinho não os distingue; o par (nome, adversário,
evento) distingue, e é essa a chave da coleta.

As duas capturas usadas aqui são **verbatim** (``fixtures/ufc_official/event_live_1002.json`` e
``event_live_1073.json``, baixadas com ``curl -o`` em 2026-09-01) e trazem, cada uma, um dos
dois: ``FighterId`` 3283 (``Bulldog``, contra Tagir Ulanbekov) e 3314 (``Blindado``, contra Alex
Pereira). As datas de nascimento da fonte batem exatamente com as dos dois lutadores
persistidos -- o cenário semeado aqui é o caso real, não uma construção.

Nada nestes testes toca a rede: a coleta roda em modo offline, lendo o diretório de fixtures
como cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.fighter_ids import (
    IdentityObservation,
    collect_identity_observations,
    consolidate_observations,
    run_fighter_id_backfill,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"
_FIXTURES_HOMONIMOS = Path(__file__).parent / "fixtures" / "ufc_official_homonimos"

# Os dois eventos capturados, com o ``Bruno Silva`` que lutou em cada um.
_MORAES_SANDHAGEN = ("UFC Fight Night: Moraes vs. Sandhagen", date(2020, 10, 10), "1002")
_SANTOS_ANKALAEV = ("UFC Fight Night: Santos vs. Ankalaev", date(2022, 3, 12), "1073")
_BULLDOG = ("3283", "146112", date(1990, 3, 16), "Tagir Ulanbekov")
_BLINDADO = ("3314", "160594", date(1989, 7, 13), "Alex Pereira")


def _semeia_lutador(session: Session, nome: str, dob: date | None) -> Fighter:
    """Insere um lutador do seed Kaggle, sem identificador da fonte oficial."""
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
    # O canto persistido é irrelevante para esta slice (o que se coleta é **identidade**),
    # mas ``bout_fighters.corner`` é NOT NULL -- a atribuição aqui é só para semear.
    for canto, lutador in zip((Corner.RED, Corner.BLUE), lutadores, strict=True):
        session.add(
            BoutFighter(bout_id=luta.id, fighter_id=lutador.id, corner=canto, source="kaggle")
        )
    session.flush()
    return luta


def _semeia_os_dois_bruno_silva(session: Session) -> tuple[Fighter, Fighter]:
    """O caso real: dois ``Bruno Silva`` homônimos, um em cada evento capturado."""
    bulldog = _semeia_lutador(session, "Bruno Silva", _BULLDOG[2])
    blindado = _semeia_lutador(session, "Bruno Silva", _BLINDADO[2])
    for evento_dados, silva, adversario in (
        (_MORAES_SANDHAGEN, bulldog, _BULLDOG[3]),
        (_SANTOS_ANKALAEV, blindado, _BLINDADO[3]),
    ):
        nome, quando, ufc_event_id = evento_dados
        evento = _semeia_evento(session, nome, quando, ufc_event_id)
        _semeia_luta(session, evento, silva, _semeia_lutador(session, adversario, None))
    return bulldog, blindado


def test_dois_homonimos_recebem_identificadores_distintos_pelo_contexto_da_luta(
    db_session: Session,
) -> None:
    """CA-03: cada ``Bruno Silva`` recebe o ``FighterId`` da luta em que **ele** lutou.

    Só o contexto (evento + adversário) resolve: os dois têm o mesmo nome normalizado, e
    nenhuma chamada extra nem desempate por idade participa disso.
    """
    bulldog, blindado = _semeia_os_dois_bruno_silva(db_session)

    run_fighter_id_backfill(db_session, None, OfficialEventCache(_FIXTURES))

    assert bulldog.ufc_fighter_id == _BULLDOG[0]
    assert bulldog.ufc_mma_id == _BULLDOG[1]
    assert blindado.ufc_fighter_id == _BLINDADO[0]
    assert blindado.ufc_mma_id == _BLINDADO[1]


def test_homonimo_nao_passa_pelo_desempate_por_idade(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CA-03/CA-07: o desempate por idade do M6 não é acionado -- o id resolve antes.

    ``match_fighter_id_by_age`` custa uma chamada de perfil na Cito e é a última linha de
    defesa. Com o id externo em mãos, ele não deve nem ser cogitado: a prova é que a função
    explode se alguém a chamar durante o backfill.
    """

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("o desempate por idade não deve ser acionado quando há id externo")

    monkeypatch.setattr("ingestion.entity_resolution.match_fighter_id_by_age", _explode)
    bulldog, blindado = _semeia_os_dois_bruno_silva(db_session)

    run_fighter_id_backfill(db_session, None, OfficialEventCache(_FIXTURES))

    assert bulldog.ufc_fighter_id != blindado.ufc_fighter_id


def test_coleta_atribui_cada_canto_ao_lutador_persistido_de_mesmo_nome(
    db_session: Session,
) -> None:
    """CA-03: a coleta devolve uma observação por canto casado, sem escrever nada.

    Separar coleta de escrita é o que torna impossível gravar um conflito: a decisão de
    escrever só existe depois da consolidação.
    """
    bulldog, blindado = _semeia_os_dois_bruno_silva(db_session)

    coleta = collect_identity_observations(db_session, None, OfficialEventCache(_FIXTURES))

    por_lutador = {obs.fighter_id: obs.ufc_fighter_id for obs in coleta.observations}
    assert por_lutador[bulldog.id] == _BULLDOG[0]
    assert por_lutador[blindado.id] == _BLINDADO[0]
    # A coleta não escreve: os campos só são preenchidos pelo backfill.
    assert bulldog.ufc_fighter_id is None
    assert blindado.ufc_fighter_id is None


def test_luta_persistida_sem_correspondencia_na_fonte_e_contada_e_pulada(
    db_session: Session,
) -> None:
    """CA-04: par que não existe no card da fonte entra em ``unmatched_bouts``, nunca adivinhado.

    O evento é o mesmo (id 1002 casado), mas o par de nomes não está no card real -- o
    casamento é pelo par, não pelo evento.
    """
    evento = _semeia_evento(db_session, *_MORAES_SANDHAGEN)
    fantasma_a = _semeia_lutador(db_session, "Ninguem Um", None)
    fantasma_b = _semeia_lutador(db_session, "Ninguem Dois", None)
    luta = _semeia_luta(db_session, evento, fantasma_a, fantasma_b)

    coleta = collect_identity_observations(db_session, None, OfficialEventCache(_FIXTURES))

    assert coleta.observations == ()
    assert coleta.unmatched_bouts == ((luta.id, _MORAES_SANDHAGEN[0]),)
    assert fantasma_a.ufc_fighter_id is None


# --------------------------------------------------------------------------- #
# CA-03 -- os oito nomes com múltiplos FighterId na fonte
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OcorrenciaHomonimo:
    """Uma aparição real de um dos homônimos: quem ele é na fonte e onde lutou.

    ``data`` é a data **local** do evento na fonte, e é ela que decide se a ocorrência está
    dentro da janela da RF-03 -- por isso não há flag separada: a fronteira é derivada do dado,
    não afirmada ao lado dele.
    """

    ufc_fighter_id: str
    ufc_mma_id: str | None
    nome: str
    date_of_birth: date | None
    evento: str
    data: date
    ufc_event_id: str
    adversario: str


# Os oito nomes normalizados que mapeiam para mais de um ``FighterId`` na fonte oficial, com as
# duas ocorrências reais de cada um. A lista foi **obtida por medição**, não copiada da SPEC:
# agrupando os 1.341 payloads do cache da varredura por ``normalize_name`` e contando ids
# distintos (procedência completa no README de ``fixtures/ufc_official_homonimos/``).
#
# O número oito só aparece somando **todas** as promoções que a fonte cobre. Só no UFC são dois
# (``bruno silva`` e ``lance gibson``); no UFC dentro da janela, um (``bruno silva``). Cinco dos
# oito têm um dos ids **exclusivamente** em evento anterior a 2010-03-21, e é por isso que o
# teste abaixo espera identificador apenas para a ocorrência que cai dentro da janela: CA-03 e
# CA-08 se encontram aqui, e a guarda da janela vence.
HOMONIMOS_CONHECIDOS: tuple[tuple[str, tuple[OcorrenciaHomonimo, OcorrenciaHomonimo]], ...] = (
    (
        "bruno silva",
        (
            OcorrenciaHomonimo(
                "3283",
                "146112",
                "Bruno Silva",
                date(1990, 3, 16),
                "UFC 243: Whittaker vs. Adesanya",
                date(2019, 10, 6),
                "946",
                "Khalid Taha",
            ),
            OcorrenciaHomonimo(
                "3314",
                "160594",
                "Bruno Silva",
                date(1989, 7, 13),
                "UFC Fight Night: Jung vs. Ige",
                date(2021, 6, 19),
                "1035",
                "Wellington Turman",
            ),
        ),
    ),
    (
        "jean silva",
        (
            OcorrenciaHomonimo(
                "835",
                None,
                "Jean Silva",
                date(1977, 10, 8),
                "PRIDE Bushido 8",
                date(2005, 7, 17),
                "139",
                "Takanori Gomi",
            ),
            OcorrenciaHomonimo(
                "4064",
                "174668",
                "Jean Silva",
                date(1996, 12, 27),
                "UFC Fight Night: Ankalaev vs. Walker 2",
                date(2024, 1, 13),
                "1185",
                "Westin Wilson",
            ),
        ),
    ),
    (
        "joey gomez",
        (
            OcorrenciaHomonimo(
                "2738",
                "132086",
                "Joey Gomez",
                date(1986, 7, 21),
                "UFC Fight Night: Dillashaw vs Cruz",
                date(2016, 1, 17),
                "759",
                "Rob Font",
            ),
            OcorrenciaHomonimo(
                "3540",
                "148326",
                "Joey Gomez",
                date(1989, 8, 29),
                "DWCS 2.4",
                date(2018, 7, 10),
                "883",
                "Kevin Aguilar",
            ),
        ),
    ),
    (
        "lance gibson",
        (
            OcorrenciaHomonimo(
                "131",
                None,
                "Lance Gibson",
                date(1970, 11, 20),
                "UFC 29: Defense of the Belts",
                date(2000, 12, 16),
                "90",
                "Evan Tanner",
            ),
            OcorrenciaHomonimo(
                "4499",
                "159408",
                "Lance Gibson Jr.",
                date(1995, 2, 2),
                "UFC Fight Night: Royval vs. Kape",
                date(2025, 12, 13),
                "1292",
                "King Green",
            ),
        ),
    ),
    (
        "michael mcdonald",
        (
            OcorrenciaHomonimo(
                "1007",
                None,
                "Michael McDonald",
                date(1965, 2, 6),
                "K-1 Beast 2004",
                date(2004, 3, 14),
                "317",
                "Lyoto Machida",
            ),
            OcorrenciaHomonimo(
                "1650",
                "112637",
                "Michael McDonald",
                date(1991, 1, 15),
                "UFC Fight Night: Nogueira vs Davis",
                date(2011, 3, 26),
                "533",
                "Edwin Figueroa",
            ),
        ),
    ),
    (
        "mike davis",
        (
            OcorrenciaHomonimo(
                "1318",
                None,
                "Mike Davis",
                None,
                "Strikeforce - Young Guns 3",
                date(2008, 9, 12),
                "248",
                "OJ Dominguez",
            ),
            OcorrenciaHomonimo(
                "3135",
                "144530",
                "Mike Davis",
                date(1992, 10, 7),
                "UFC Fight Night: Jacare vs. Hermansson",
                date(2019, 4, 27),
                "916",
                "Gilbert Burns",
            ),
        ),
    ),
    (
        "tony johnson",
        (
            OcorrenciaHomonimo(
                "1359",
                None,
                "Tony Johnson",
                date(1983, 5, 2),
                "DWCS 3.2",
                date(2019, 6, 25),
                "928",
                "Alton Cunningham",
            ),
            OcorrenciaHomonimo(
                "2493",
                "154027",
                "Tony Johnson",
                None,
                "Bellator 136: Brooks vs. Jansen",
                date(2015, 4, 10),
                "715",
                "Alexander Volkov",
            ),
        ),
    ),
    (
        "victor valenzuela",
        (
            OcorrenciaHomonimo(
                "1259",
                None,
                "Victor Valenzuela",
                None,
                "Strikeforce - Shamrock vs. Baroni",
                date(2007, 6, 21),
                "255",
                "Edson Berto",
            ),
            OcorrenciaHomonimo(
                "4489",
                "164881",
                "Victor Valenzuela",
                date(1994, 2, 9),
                "UFC Fight Night: Sterling vs. Zalal",
                date(2026, 4, 25),
                "1307",
                "Max Griffin",
            ),
        ),
    ),
)


def test_a_medicao_dos_homonimos_cobre_os_oito_nomes_da_spec() -> None:
    """CA-03: a tabela medida tem os oito nomes, e cada um com dois ``FighterId`` distintos.

    Trava sobre a medição: se uma coleta futura encontrar um conjunto diferente, alguém precisa
    atualizar esta tabela **conscientemente**, com a nova medição no relatório -- nunca ajustar
    o número em silêncio.
    """
    assert len(HOMONIMOS_CONHECIDOS) == 8
    for nome, ocorrencias in HOMONIMOS_CONHECIDOS:
        assert len({ocorrencia.ufc_fighter_id for ocorrencia in ocorrencias}) == 2
        assert all(normalize_name(ocorrencia.nome) == nome for ocorrencia in ocorrencias)


@pytest.mark.parametrize(
    ("nome_normalizado", "ocorrencias"),
    HOMONIMOS_CONHECIDOS,
    ids=[nome for nome, _ in HOMONIMOS_CONHECIDOS],
)
def test_nome_com_multiplos_fighter_ids_resolve_pelo_contexto_da_luta(
    db_session: Session,
    nome_normalizado: str,
    ocorrencias: tuple[OcorrenciaHomonimo, OcorrenciaHomonimo],
) -> None:
    """CA-03: cada lutador recebe o id da luta em que **ele** lutou -- nunca o do homônimo.

    Os dois compartilham o ``name_normalized``, então só o par (nome, adversário, evento)
    decide. Onde a ocorrência é anterior a 2010-03-21, o esperado é **nulo**: a guarda da
    janela (CA-08) vence, mesmo com o evento mapeado e o par casando perfeitamente.
    """
    lutadores = []
    for ocorrencia in ocorrencias:
        lutador = _semeia_lutador(db_session, ocorrencia.nome, ocorrencia.date_of_birth)
        evento = _semeia_evento(
            db_session, ocorrencia.evento, ocorrencia.data, ocorrencia.ufc_event_id
        )
        _semeia_luta(
            db_session, evento, lutador, _semeia_lutador(db_session, ocorrencia.adversario, None)
        )
        lutadores.append(lutador)

    run_fighter_id_backfill(db_session, None, OfficialEventCache(_FIXTURES_HOMONIMOS))

    for lutador, ocorrencia in zip(lutadores, ocorrencias, strict=True):
        na_janela = ocorrencia.data >= OFFICIAL_WINDOW_START
        assert lutador.name_normalized == nome_normalizado
        assert lutador.ufc_fighter_id == (ocorrencia.ufc_fighter_id if na_janela else None)
        assert lutador.ufc_mma_id == (ocorrencia.ufc_mma_id if na_janela else None)


# --------------------------------------------------------------------------- #
# CA-03 -- o caso irresolúvel: um lutador persistido com dois ids na fonte
# --------------------------------------------------------------------------- #


def test_consolidacao_separa_o_conflito_e_nao_o_promove_a_escrita() -> None:
    """CA-03: lutador com dois ``FighterId`` observados vira conflito, não candidato a escrita.

    A consolidação é onde a decisão de escrever nasce; quem tem mais de um id observado
    simplesmente não chega lá, com **todos** os ids nomeados para inspeção.
    """
    observacoes = (
        IdentityObservation(fighter_id=7, ufc_fighter_id="3283", ufc_mma_id=None, event_id=1),
        IdentityObservation(fighter_id=7, ufc_fighter_id="3314", ufc_mma_id=None, event_id=2),
        IdentityObservation(fighter_id=8, ufc_fighter_id="3460", ufc_mma_id="163640", event_id=1),
    )

    resolvidos, conflitantes = consolidate_observations(observacoes)

    assert conflitantes == {7: ("3283", "3314")}
    assert resolvidos == {8: ("3460", "163640")}


def test_conflito_de_um_lutador_nao_derruba_o_run_e_nada_e_escrito_para_ele(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-03: o conflito é contado e nomeado, e os **outros** lutadores continuam sendo escritos.

    O cenário é real e sai das duas capturas: um único ``Bruno Silva`` persistido com lutas nos
    dois eventos é observado como 3283 num e 3314 no outro -- exatamente o que aconteceria se
    alguém tivesse colapsado os dois homônimos numa linha só. Ele fica sem id; os adversários,
    não.
    """
    silva = _semeia_lutador(db_session, "Bruno Silva", None)
    adversarios = []
    for nome_evento, dados in ((_MORAES_SANDHAGEN, _BULLDOG), (_SANTOS_ANKALAEV, _BLINDADO)):
        evento = _semeia_evento(db_session, *nome_evento)
        adversario = _semeia_lutador(db_session, dados[3], None)
        _semeia_luta(db_session, evento, silva, adversario)
        adversarios.append(adversario)

    with caplog.at_level(logging.WARNING, logger="ingestion.ufc_official.fighter_ids"):
        relatorio = run_fighter_id_backfill(db_session, None, OfficialEventCache(_FIXTURES))

    assert silva.ufc_fighter_id is None
    assert relatorio.conflicting == ((silva.id, "Bruno Silva", (_BULLDOG[0], _BLINDADO[0])),)
    assert "Bruno Silva" in caplog.text
    # O run seguiu: os dois adversários receberam os seus identificadores.
    assert all(adversario.ufc_fighter_id is not None for adversario in adversarios)
    assert relatorio.assigned == len(adversarios)
