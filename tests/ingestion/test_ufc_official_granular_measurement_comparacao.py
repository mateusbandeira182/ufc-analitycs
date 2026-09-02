"""Comparação do granular oficial contra o persistido, e a garantia de que a medição só lê.

Três blocos:

1. **Leitura do persistido** contra o Postgres de teste transacional -- as duas tabelas do
   granular, cada uma com o ``source`` da própria linha (elas têm linhagens diferentes no mesmo
   evento), e a janela da SPEC 008 fechando a porta para eventos anteriores a 2010-03-21.
2. **Comparação**, que é função pura: concordância total, divergência isolada num campo,
   ausência saindo do denominador, linha sem contraparte, ambiguidade falhando alto e a
   tolerância declarada do tempo de controle.
3. **Guarda de leitura pura**: rodar a medição inteira não altera a contagem de
   ``bout_fighters`` nem de ``bout_fighter_rounds`` (CA-07 da SPEC).

Nenhum teste toca a rede (``httpx.MockTransport`` serve as capturas) nem a Cito -- este arquivo
não importa nada de ``ingestion.cito``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.granular_measurement import (
    BLOCK_ROUNDS,
    BLOCK_TOTALS,
    MEASURED_FIELDS,
    AmbiguousGranularMatchError,
    EventOutsideWindowError,
    GranularLine,
    compare_granular,
    load_persisted_lines,
    run_measurement,
    select_corroboration_events,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

# Evento e luta reais da captura verbatim: UFC Fight Night: Imavov vs. Borralho (2025-09-06),
# o evento de referência do portão.
_EVENT_ID = 1271
_FIGHT_ID = 12204
_RED_NAME = "Nassourdine Imavov"
_BLUE_NAME = "Caio Borralho"


# --------------------------------------------------------------------------- #
# Builders de estado e de linhas sintéticas.
# --------------------------------------------------------------------------- #


def _valores(**overrides: int | None) -> Mapping[str, int | None]:
    """Os 22 campos medidos, todos em 1 por padrão, com os sobrescritos que o teste precisa."""
    return dict.fromkeys(MEASURED_FIELDS, 1) | overrides


def _linha(nome: str, rodada: int | None, **overrides: int | None) -> GranularLine:
    return GranularLine(normalize_name(nome), rodada, _valores(**overrides))


def _seed_fighter(session: Session, name: str) -> int:
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


def _seed_event_com_granular(
    session: Session,
    *,
    event_date: date = date(2025, 9, 6),
    ufc_event_id: str | None = str(_EVENT_ID),
    rounds: int = 5,
    stat_overrides: Mapping[str, int | None] | None = None,
) -> Event:
    """Semeia o evento de referência com uma luta, dois cantos e ``rounds`` rounds por canto.

    Os totais entram com ``source="kaggle"`` e o round-a-round com ``source="cito"`` -- as
    linhagens reais das duas tabelas, que a medição precisa reportar em separado.
    """
    event = Event(
        name="UFC Fight Night: Imavov vs. Borralho",
        date=event_date,
        location=None,
        source="kaggle",
        cito_slug="ufc-fight-night-september-06-2025",
        ufc_event_id=ufc_event_id,
    )
    session.add(event)
    session.flush()

    red_id = _seed_fighter(session, _RED_NAME)
    blue_id = _seed_fighter(session, _BLUE_NAME)
    bout = Bout(
        event_id=event.id,
        winner_id=red_id,
        method=BoutMethod.DECISION,
        round=5,
        ending_time_seconds=None,
        weight_class="Middleweight",
        source="kaggle",
    )
    session.add(bout)
    session.flush()

    for fighter_id, corner in ((red_id, Corner.RED), (blue_id, Corner.BLUE)):
        bout_fighter = BoutFighter(
            bout_id=bout.id,
            fighter_id=fighter_id,
            corner=corner,
            source="kaggle",
            **dict.fromkeys(MEASURED_FIELDS, 7),
        )
        session.add(bout_fighter)
        session.flush()
        for numero in range(1, rounds + 1):
            session.add(
                BoutFighterRound(
                    bout_fighter_id=bout_fighter.id,
                    round=numero,
                    source="cito",
                    **(dict.fromkeys(MEASURED_FIELDS, 3) | dict(stat_overrides or {})),
                )
            )
    session.flush()
    return event


def _captura(nome: str) -> object:
    return json.loads((_FIXTURES / nome).read_text(encoding="utf-8"))


def _client_da_captura() -> UfcOfficialClient:
    """Cliente que serve a captura verbatim do evento de referência e das lutas do card.

    O evento 1271 não tem captura versionada (são 13 lutas), então o card servido é o da luta
    12204 sozinha -- o suficiente para provar o caminho ponta a ponta sem inventar payload.
    """
    card = {
        "LiveEventDetail": {
            "EventId": _EVENT_ID,
            "Name": "UFC Fight Night: Imavov vs. Borralho",
            "StartTime": "2025-09-06T16:00Z",
            "TimeZone": "GMT+02:00",
            "Status": "Final",
            "Organization": {"OrganizationId": 1, "Name": "Ultimate Fighting Championship"},
            "FightCard": [{"FightId": _FIGHT_ID, "Fighters": []}],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/event/live/{_EVENT_ID}.json":
            return httpx.Response(200, json=card)
        assert request.url.path == f"/api/v3/fight/live/{_FIGHT_ID}.json"
        return httpx.Response(200, json=_captura(f"fight_live_{_FIGHT_ID}.json"))

    return UfcOfficialClient(
        base_url="https://d29dxerjsp82wz.cloudfront.net",
        transport=httpx.MockTransport(handler),
    )


def _contagens(session: Session) -> tuple[int, int]:
    """``COUNT(*)`` das duas tabelas do granular -- o que a guarda de leitura pura compara."""
    return (
        session.scalar(select(func.count()).select_from(BoutFighter)) or 0,
        session.scalar(select(func.count()).select_from(BoutFighterRound)) or 0,
    )


# --------------------------------------------------------------------------- #
# Leitura do granular persistido.
# --------------------------------------------------------------------------- #


def test_leitura_devolve_as_duas_tabelas_com_o_source_da_linha(db_session: Session) -> None:
    """CA-05: totais (``round=None``) e round-a-round, cada linha com o ``source`` dela.

    As duas tabelas têm **linhagens diferentes** no mesmo evento: os totais vieram do Kaggle e o
    round-a-round da Cito (o backfill da Cito só preencheu campos nulos nos totais e manteve
    ``source="kaggle"``, porque ``source`` é a origem da LINHA, não do campo). Sem carregar o
    ``source`` junto, a medição misturaria as duas e responderia a pergunta errada.
    """
    event = _seed_event_com_granular(db_session, rounds=3)

    linhas = load_persisted_lines(db_session, event)

    por_bloco = {(line.block, source) for line, source in linhas}
    assert por_bloco == {(BLOCK_TOTALS, "kaggle"), (BLOCK_ROUNDS, "cito")}
    totais = [line for line, _ in linhas if line.round is None]
    rounds = [line for line, _ in linhas if line.round is not None]
    assert {line.fighter_name_normalized for line in totais} == {
        normalize_name(_RED_NAME),
        normalize_name(_BLUE_NAME),
    }
    assert len(rounds) == 6  # 2 cantos x 3 rounds
    assert {line.round for line in rounds} == {1, 2, 3}
    assert totais[0].values["sig_strikes_landed"] == 7
    assert rounds[0].values["sig_strikes_landed"] == 3


def test_evento_anterior_a_2010_03_21_nao_e_lido(db_session: Session) -> None:
    """CA-07 / CA-08 da SPEC: a janela é explícita no código, não implícita na escolha do evento.

    Antes de 2010-03-21 o canto nunca foi registrado e as três fontes trazem o mesmo dado
    fabricado (ADR 0006). Medir concordância ali compararia duas cópias da mesma invenção.
    """
    event = _seed_event_com_granular(db_session, event_date=date(2010, 3, 20))

    with pytest.raises(EventOutsideWindowError, match="2010-03-21"):
        load_persisted_lines(db_session, event)


def test_primeiro_dia_da_janela_e_lido(db_session: Session) -> None:
    """A fronteira é inclusiva: 2010-03-21 é o primeiro evento com canto real, e entra."""
    event = _seed_event_com_granular(db_session, event_date=OFFICIAL_WINDOW_START)

    assert load_persisted_lines(db_session, event)


def test_leitura_do_persistido_nao_escreve(db_session: Session) -> None:
    """CA-06: a leitura não altera a contagem das tabelas do granular."""
    event = _seed_event_com_granular(db_session)
    antes = _contagens(db_session)

    load_persisted_lines(db_session, event)

    assert _contagens(db_session) == antes


# --------------------------------------------------------------------------- #
# Comparação (função pura).
# --------------------------------------------------------------------------- #


def _compara(
    oficial: list[GranularLine], persistido: list[tuple[GranularLine, str]]
) -> dict[tuple[str, str], dict[str, tuple[int, int]]]:
    """Atalho: devolve ``{(bloco, source): {campo: (agreed, compared)}}``."""
    return {
        (report.block, report.persisted_source): {
            field.field: (field.agreed, field.compared) for field in report.fields
        }
        for report in compare_granular(
            oficial, persistido, event_id=1, event_name="Evento de teste"
        )
    }


def test_concordancia_total_devolve_cem_por_cento_em_todos_os_campos() -> None:
    """CA-04: dois lados idênticos, todos os 22 campos em 100%."""
    oficial = [_linha(_RED_NAME, 1), _linha(_BLUE_NAME, 1)]
    persistido = [(_linha(_RED_NAME, 1), "cito"), (_linha(_BLUE_NAME, 1), "cito")]

    resultado = _compara(oficial, persistido)[(BLOCK_ROUNDS, "cito")]

    assert all(resultado[field] == (2, 2) for field in MEASURED_FIELDS)


def test_divergencia_num_campo_derruba_so_aquele_campo() -> None:
    """CA-04: um campo errado não contamina os outros 21 -- é por isso que a medição é por campo.

    Um booleano "bate/não bate" transformaria uma divergência de um campo em reprovação do
    conjunto, e esconderia qual campo é o problema.
    """
    oficial = [_linha(_RED_NAME, 1, sig_strikes_landed=9), _linha(_BLUE_NAME, 1)]
    persistido = [(_linha(_RED_NAME, 1), "cito"), (_linha(_BLUE_NAME, 1), "cito")]

    resultado = _compara(oficial, persistido)[(BLOCK_ROUNDS, "cito")]

    assert resultado["sig_strikes_landed"] == (1, 2)
    assert all(
        resultado[field] == (2, 2) for field in MEASURED_FIELDS if field != "sig_strikes_landed"
    )


def test_divergencia_e_nomeada_no_relatorio() -> None:
    """A divergência aparece nomeada -- um percentual sem o nome não é inspecionável."""
    oficial = [_linha(_RED_NAME, 3, knockdowns=2)]
    persistido = [(_linha(_RED_NAME, 3), "cito")]

    (report,) = compare_granular(oficial, persistido, event_id=1, event_name="Evento de teste")
    (knockdowns,) = [field for field in report.fields if field.field == "knockdowns"]

    assert knockdowns.disagreements == ("nassourdine imavov (round 3): oficial=2 persistido=1",)


def test_nulo_em_um_dos_lados_sai_do_denominador() -> None:
    """CA-04: ausência **não** é divergência -- a linha sai do denominador daquele campo.

    Contá-la como discordância inventaria divergência onde só há dado faltando, e reprovaria a
    fonte oficial por um buraco do nosso backfill.
    """
    oficial = [_linha(_RED_NAME, 1)]
    persistido = [(_linha(_RED_NAME, 1, reversals=None), "cito")]

    resultado = _compara(oficial, persistido)[(BLOCK_ROUNDS, "cito")]

    assert resultado["reversals"] == (0, 0)
    assert resultado["knockdowns"] == (1, 1)


def test_campo_sem_linha_comparavel_e_reportado_como_nao_medido() -> None:
    """Denominador zero é "não medido", nunca "aprovado" -- 0/0 não é 100%."""
    persistido = [(_linha(_RED_NAME, 1, reversals=None), "cito")]

    (report,) = compare_granular(
        [_linha(_RED_NAME, 1)], persistido, event_id=1, event_name="Evento de teste"
    )
    (reversals,) = [field for field in report.fields if field.field == "reversals"]

    assert reversals.measured is False
    assert reversals.rate == 0.0


def test_linha_presente_num_lado_so_conta_como_nao_casada() -> None:
    """CA-04: linha sem contraparte é não casada, nunca divergência.

    A cobertura de casamento é contada sobre as linhas **persistidas**: uma linha oficial sem
    contraparte é buraco do nosso backfill, não falha da fonte.
    """
    oficial = [_linha(_RED_NAME, 1), _linha(_BLUE_NAME, 1), _linha("Fulano de Tal", 1)]
    persistido = [
        (_linha(_RED_NAME, 1), "cito"),
        (_linha(_BLUE_NAME, 1), "cito"),
        (_linha("Sicrano de Tal", 1), "cito"),
    ]

    (report,) = compare_granular(oficial, persistido, event_id=1, event_name="Evento de teste")

    assert report.matched_lines == 2
    assert report.persisted_lines == 3
    assert report.unmatched_persisted == ("sicrano de tal (round 1)",)
    assert report.unmatched_official == ("fulano de tal (round 1)",)
    assert report.match_rate == pytest.approx(2 / 3)
    assert all(field.compared == 2 for field in report.fields)


def test_nome_ambiguo_no_evento_falha_alto() -> None:
    """CA-04: duas linhas com a mesma chave levantam -- nunca casar no escuro.

    Um casamento errado aqui não corrompe o banco, mas corrompe o número que decide a
    arquitetura de ingestão do projeto, o que é pior: passa por evidência.
    """
    persistido = [(_linha(_RED_NAME, 1), "cito"), (_linha("nassourdine imavov", 1), "cito")]

    with pytest.raises(AmbiguousGranularMatchError, match="nassourdine imavov"):
        compare_granular([], persistido, event_id=1, event_name="Evento de teste")


def test_tempo_de_controle_com_um_segundo_de_diferenca_concorda() -> None:
    """CA-04: a tolerância de +/-1s foi declarada **antes** da medição, e vale só para o relógio.

    É o campo com maior chance de divergir por forma; um segundo de folga cobre arredondamento
    de operação. Dois segundos, não.
    """
    persistido = [(_linha(_RED_NAME, 1, control_time_seconds=60), "cito")]

    dentro = _compara([_linha(_RED_NAME, 1, control_time_seconds=61)], persistido)
    fora = _compara([_linha(_RED_NAME, 1, control_time_seconds=62)], persistido)

    assert dentro[(BLOCK_ROUNDS, "cito")]["control_time_seconds"] == (1, 1)
    assert fora[(BLOCK_ROUNDS, "cito")]["control_time_seconds"] == (0, 1)


def test_a_tolerancia_nao_vaza_para_os_demais_campos() -> None:
    """Só o relógio tolera; os 21 campos restantes são inteiros comparados por igualdade exata."""
    persistido = [(_linha(_RED_NAME, 1, sig_strikes_landed=10), "cito")]

    resultado = _compara([_linha(_RED_NAME, 1, sig_strikes_landed=11)], persistido)

    assert resultado[(BLOCK_ROUNDS, "cito")]["sig_strikes_landed"] == (0, 1)


def test_blocos_e_linhagens_sao_reportados_em_separado() -> None:
    """CA-05: totais (Kaggle) e round-a-round (Cito) nunca viram um percentual agregado.

    O portão pergunta se a fonte oficial substitui a **Cito**; o bloco de totais não é Cito, e
    misturar os dois responderia outra pergunta.
    """
    oficial = [_linha(_RED_NAME, None), _linha(_RED_NAME, 1)]
    persistido = [
        (_linha(_RED_NAME, None, knockdowns=99), "kaggle"),
        (_linha(_RED_NAME, 1), "cito"),
    ]

    resultado = _compara(oficial, persistido)

    assert set(resultado) == {(BLOCK_TOTALS, "kaggle"), (BLOCK_ROUNDS, "cito")}
    assert resultado[(BLOCK_TOTALS, "kaggle")]["knockdowns"] == (0, 1)
    assert resultado[(BLOCK_ROUNDS, "cito")]["knockdowns"] == (1, 1)


def test_relatorio_ordena_chaves_de_blocos_diferentes_sem_estourar() -> None:
    """Regressão: ``round=None`` e ``round=N`` convivem na mesma ordenação de não casadas.

    Encontrado na primeira execução real (evento de referência, 2026-09-01): o relatório do
    bloco de totais ordenava um conjunto que continha tanto o total (``round=None``) quanto
    rounds, e ``None < 1`` levanta ``TypeError`` em Python. O defeito só aparece com **duas ou
    mais** linhas oficiais não casadas de blocos diferentes -- razão de ele ter passado pelos
    testes sintéticos e ter sido pego pelo dado real.
    """
    oficial = [_linha("Fulano de Tal", None), _linha("Fulano de Tal", 1), _linha(_RED_NAME, None)]
    persistido = [(_linha(_RED_NAME, None), "kaggle")]

    (report,) = compare_granular(oficial, persistido, event_id=1, event_name="Evento de teste")

    assert report.block == BLOCK_TOTALS
    assert report.unmatched_official == ("fulano de tal (total)",)


def test_linha_oficial_de_outro_bloco_nao_conta_como_nao_casada() -> None:
    """O total oficial não é "linha oficial sem contraparte" do relatório do round-a-round."""
    oficial = [_linha(_RED_NAME, None), _linha(_RED_NAME, 1)]
    persistido = [(_linha(_RED_NAME, 1), "cito")]

    (report,) = compare_granular(oficial, persistido, event_id=1, event_name="Evento de teste")

    assert report.block == BLOCK_ROUNDS
    assert report.unmatched_official == ()


# --------------------------------------------------------------------------- #
# Medição ponta a ponta e a guarda de leitura pura.
# --------------------------------------------------------------------------- #


def test_medicao_ponta_a_ponta_compara_a_captura_com_o_persistido(db_session: Session) -> None:
    """CA-05: a medição casa a captura verbatim contra o banco e reporta por campo.

    O evento semeado tem os mesmos dois lutadores e cinco rounds da captura, com valores
    propositalmente diferentes: o teste prova que o número sai do dado real, não de constante.
    """
    _seed_event_com_granular(db_session)

    reports = run_measurement(
        db_session,
        _client_da_captura(),
        event_slug="ufc-fight-night-september-06-2025",
    )

    por_bloco = {(report.block, report.persisted_source): report for report in reports}
    assert set(por_bloco) == {(BLOCK_TOTALS, "kaggle"), (BLOCK_ROUNDS, "cito")}
    rounds = por_bloco[(BLOCK_ROUNDS, "cito")]
    assert rounds.matched_lines == 10  # 2 cantos x 5 rounds
    assert rounds.match_rate == 1.0
    # A captura traz 10 golpes significativos conectados por Imavov no round 1 e o banco tem 3:
    # a divergência é real e sai do dado, não de um valor fixo no teste.
    (sig,) = [field for field in rounds.fields if field.field == "sig_strikes_landed"]
    assert sig.compared == 10
    assert sig.agreed == 0


def test_medicao_nao_altera_a_contagem_do_granular(db_session: Session) -> None:
    """CA-06: a medição é **leitura pura** -- ``COUNT(*)`` idêntico antes e depois.

    É o que separa esta sprint de uma ingestão: ela mede e decide, não escreve. A guarda existe
    porque "eu não escrevi nada" é exatamente o tipo de afirmação que ninguém confere.
    """
    _seed_event_com_granular(db_session)
    antes = _contagens(db_session)

    run_measurement(
        db_session,
        _client_da_captura(),
        event_slug="ufc-fight-night-september-06-2025",
    )

    assert _contagens(db_session) == antes


def test_evento_sem_ufc_event_id_falha_alto(db_session: Session) -> None:
    """Sem o identificador da fonte não há como localizar o evento -- nunca adivinhar."""
    _seed_event_com_granular(db_session, ufc_event_id=None)

    with pytest.raises(Exception, match="ufc_event_id"):
        run_measurement(
            db_session,
            _client_da_captura(),
            event_slug="ufc-fight-night-september-06-2025",
        )


def test_regra_de_corroboracao_ordena_por_cobertura_e_e_deterministica(
    db_session: Session,
) -> None:
    """A escolha dos eventos de corroboração é regra, não dedo.

    Entre os eventos de 2023-2025 com ``ufc_event_id``, os de maior cobertura de round-a-round
    primeiro. Declarar a regra antes de medir é o que impede escolher, depois, o evento cujo
    número ficou melhor.
    """
    magro = _seed_event_com_granular(db_session, event_date=date(2024, 1, 6), rounds=1)
    gordo = _seed_event_com_granular(db_session, event_date=date(2024, 2, 6), rounds=5)
    _seed_event_com_granular(db_session, event_date=date(2024, 3, 6), ufc_event_id=None, rounds=5)

    escolhidos = select_corroboration_events(db_session, limit=2)

    assert [event.id for event in escolhidos] == [gordo.id, magro.id]


_Handler = Callable[[httpx.Request], httpx.Response]
