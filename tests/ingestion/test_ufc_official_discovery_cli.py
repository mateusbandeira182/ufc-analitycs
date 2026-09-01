"""Testes da varredura, do cache e do mapeamento de ``events.ufc_event_id`` (Slice 02).

Três blocos:

1. **Varredura com cache e fronteira** (CA-04) -- um cliente sobre ``httpx.MockTransport`` que
   conta requisições por id prova que a segunda execução não re-baixa o que já está em disco e
   que um id novo custa a fronteira, não o intervalo inteiro.
2. **Mapeamento e relatório** (CA-02, CA-03, CA-05) -- contra o Postgres de teste, com sessão
   transacional: as quatro categorias contadas, os ambíguos nomeados e sem escrita, e a
   reexecução que não muda nada.
3. **Fronteira da janela ponta a ponta** (CA-06) -- evento anterior a ``OFFICIAL_WINDOW_START``
   com candidato de nome idêntico permanece nulo.

Nenhum teste toca a rede: o transporte é mockado e o modo offline lê o diretório de fixtures
como cache. O ``MockTransport`` responde a id desconhecido exatamente como a fonte real
responde -- **200 com ``{"LiveEventDetail": {}}``**, nunca 404.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections import Counter
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.discovery import (
    FRONTIER_MISS_STREAK,
    CatalogScan,
    DiscoveryReport,
    discover_official_catalog,
    map_official_event_ids,
    run_discovery,
)
from ingestion.ufc_official.dto import parse_event
from ingestion.ufc_official.matching import OfficialCatalogItem

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official_catalog"
_BASE_URL = "https://d29dxerjsp82wz.cloudfront.net"

# Bloco denso de ids do conjunto derivado: é por ele que a varredura fria começa.
_BLOCO_DENSO = (1, 2, 3)
# Ausência na fonte: HTTP 200 com o envelope vazio (medido; a fonte nunca devolve 404).
_ENVELOPE_VAZIO: dict[str, dict[str, object]] = {"LiveEventDetail": {}}


def _payload(event_id: int) -> object:
    return json.loads((_FIXTURES / f"event_live_{event_id}.json").read_text(encoding="utf-8"))


class _FonteEspia:
    """Cliente da fonte oficial que serve um conjunto de ids e **conta** requisição por id."""

    def __init__(self, ids_servidos: tuple[int, ...]) -> None:
        self.ids_servidos = set(ids_servidos)
        self.chamadas: Counter[int] = Counter()

    def _handler(self, request: httpx.Request) -> httpx.Response:
        event_id = int(request.url.path.removeprefix("/api/v3/event/live/").removesuffix(".json"))
        self.chamadas[event_id] += 1
        if event_id not in self.ids_servidos:
            return httpx.Response(200, json=_ENVELOPE_VAZIO)
        return httpx.Response(200, json=_payload(event_id))

    @property
    def client(self) -> UfcOfficialClient:
        return UfcOfficialClient(base_url=_BASE_URL, transport=httpx.MockTransport(self._handler))

    @property
    def total(self) -> int:
        return sum(self.chamadas.values())


# --------------------------------------------------------------------------- #
# CA-04 -- varredura com cache em disco e fronteira crescente
# --------------------------------------------------------------------------- #


def test_varredura_fria_para_apos_a_sequencia_de_ausencias(tmp_path: Path) -> None:
    """CA-04: a varredura fria avança pelos ids e para após ``FRONTIER_MISS_STREAK`` ausências.

    Sem um critério de parada a varredura não teria fim: a fonte responde 200 com envelope
    vazio para **qualquer** id, inclusive 99999.
    """
    fonte = _FonteEspia(_BLOCO_DENSO)

    scan = discover_official_catalog(fonte.client, OfficialEventCache(tmp_path))

    assert {item.event_id for item in scan.items} == {"1", "2", "3"}
    assert scan.highest_id == 3
    assert scan.fetched == 3
    assert scan.misses == FRONTIER_MISS_STREAK
    # Percorreu o bloco denso e mais a sequência de ausências que decide a parada.
    assert fonte.total == len(_BLOCO_DENSO) + FRONTIER_MISS_STREAK
    assert max(fonte.chamadas) == 3 + FRONTIER_MISS_STREAK


def test_segunda_varredura_nao_rebaixa_id_ja_materializado(tmp_path: Path) -> None:
    """CA-04: id já em disco vira cache hit -- **zero** requisições para ele na reexecução."""
    cache = OfficialEventCache(tmp_path)
    discover_official_catalog(_FonteEspia(_BLOCO_DENSO).client, cache)

    fonte = _FonteEspia(_BLOCO_DENSO)
    scan = discover_official_catalog(fonte.client, cache)

    assert {item.event_id for item in scan.items} == {"1", "2", "3"}
    assert scan.cache_hits == 3
    assert scan.fetched == 0
    for event_id in _BLOCO_DENSO:
        assert fonte.chamadas[event_id] == 0
    # Só a fronteira custa rede na reexecução.
    assert fonte.total == FRONTIER_MISS_STREAK


def test_id_novo_acima_da_fronteira_e_descoberto_sem_revarrer_o_intervalo(
    tmp_path: Path,
) -> None:
    """CA-04 / RF-09: um evento novo custa a fronteira, não o intervalo inteiro.

    A fronteira é ancorada no **maior id já visto**, nunca na data do último evento ingerido --
    justamente a inferência que a não-ordenação dos ids por data proíbe.
    """
    cache = OfficialEventCache(tmp_path)
    discover_official_catalog(_FonteEspia(_BLOCO_DENSO).client, cache)

    fonte = _FonteEspia((*_BLOCO_DENSO, 4))
    scan = discover_official_catalog(fonte.client, cache)

    assert "4" in {item.event_id for item in scan.items}
    assert scan.highest_id == 4
    assert fonte.total == 1 + FRONTIER_MISS_STREAK
    assert fonte.chamadas[1] == 0


def test_ausencia_nao_e_cacheada_e_o_buraco_e_resondado(tmp_path: Path) -> None:
    """CA-04: id ausente hoje pode existir amanhã -- gravar a ausência congelaria o buraco.

    Aqui o id 2 some do conjunto servido na primeira execução e aparece na segunda: só é
    encontrado porque a ausência não foi cacheada.
    """
    cache = OfficialEventCache(tmp_path)
    discover_official_catalog(_FonteEspia((1, 3)).client, cache)
    assert not (tmp_path / "event_live_2.json").exists()

    fonte = _FonteEspia(_BLOCO_DENSO)
    scan = discover_official_catalog(fonte.client, cache)

    assert "2" in {item.event_id for item in scan.items}
    assert fonte.chamadas[2] == 1


def test_cache_guarda_o_payload_cru_e_o_round_trip_revalida_no_dto(tmp_path: Path) -> None:
    """CA-04: o disco guarda o payload **cru**, e o que volta dele revalida na borda.

    Lição direta da SPEC 007: o ``.cache/`` da Cito guardava a forma já desserializada pelos
    DTOs e escondeu uma quebra de contrato por meses. Aqui o arquivo em disco é byte-a-byte o
    JSON que a fonte devolveu, e a leitura passa pelo **mesmo** ``parse_event`` da rede.
    """
    discover_official_catalog(_FonteEspia(_BLOCO_DENSO).client, OfficialEventCache(tmp_path))

    em_disco = json.loads((tmp_path / "event_live_1.json").read_text(encoding="utf-8"))

    assert em_disco == _payload(1)
    # Round-trip: o payload lido do disco valida no DTO da borda, sem caminho alternativo.
    assert parse_event(em_disco, event_id=1).name == "UFC 92: The Ultimate 2008"
    # A captura guarda mais chaves do que o DTO declara -- prova de que é crua, não remontada.
    assert set(em_disco["LiveEventDetail"]) > {
        "EventId",
        "Name",
        "StartTime",
        "TimeZone",
        "Status",
        "Organization",
        "FightCard",
    }


def test_projecao_usa_a_data_local_e_nao_a_utc(tmp_path: Path) -> None:
    """CA-02: o item do catálogo carrega a data **local**, não a data do instante UTC.

    UFC 184 começa em ``2015-03-01T00:00Z`` sob ``GMT-08:00`` e é o evento de 2015-02-28 --
    exatamente como a nossa base o registra. Decidir a janela ou o casamento sobre a UTC
    erraria o dia em todo evento noturno das Américas.
    """
    shutil.copy(_FIXTURES / "event_live_700.json", tmp_path / "event_live_700.json")

    scan = discover_official_catalog(None, OfficialEventCache(tmp_path))

    item = next(item for item in scan.items if item.event_id == "700")
    assert item.local_date == date(2015, 2, 28)


def test_promocoes_fora_do_escopo_nao_entram_no_catalogo(tmp_path: Path) -> None:
    """Só ``OrganizationId == 1`` (UFC) vira item de catálogo; o resto é contado e descartado.

    O filtro é **exato** porque a promoção é campo de primeira classe do payload -- não uma
    inferência sobre o nome. 42% de uma amostra de 85 eventos da fonte não é do UFC, e as fases
    de promoção (travadas em 2026-08-31) mantêm DWCS (67) e Road to UFC (68) para depois.
    """
    for event_id in (700, 900, 1124):
        shutil.copy(_FIXTURES / f"event_live_{event_id}.json", tmp_path)

    scan = discover_official_catalog(None, OfficialEventCache(tmp_path))

    assert {item.event_id for item in scan.items} == {"700", "1124"}
    assert scan.discarded_other_promotions == 1  # DWCS Brazil 1.3


def test_evento_de_outra_promocao_sem_fuso_nao_derruba_a_varredura(tmp_path: Path) -> None:
    """O filtro de promoção vem **antes** da data local -- e é por isso que este caso passa.

    Regressão de um defeito real: o id 380 (``Gladiator FC - Day 2``, 2004, ``OrganizationId``
    33) devolve ``"TimeZone": null``, e calcular a data local antes de filtrar a promoção
    derrubava a varredura inteira num evento que nem nos interessa. Medido na varredura de
    2026-09-01 -- a sondagem de 24 eventos da Sprint 008-01 não o alcançara.
    """
    for event_id in (380, 1124):
        shutil.copy(_FIXTURES / f"event_live_{event_id}.json", tmp_path)

    scan = discover_official_catalog(None, OfficialEventCache(tmp_path))

    assert {item.event_id for item in scan.items} == {"1124"}
    assert scan.discarded_other_promotions == 1


def test_evento_do_ufc_sem_fuso_e_contado_e_nomeado_sem_derrubar_a_varredura(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Evento do UFC sem ``TimeZone`` não entra no catálogo, mas é **nomeado** -- e o run segue.

    Medido em 1.067 payloads da fonte (2026-09-01): exatamente um evento do UFC vem sem fuso --
    o id 1069, ``UFC 271: Adesanya vs. Whittaker 2`` --, além de quatro de outras promoções. É
    raro, mas é real, e derrubar a varredura inteira por causa dele seria pior do que pulá-lo.

    A data **não** é adivinhada a partir da UTC, nem com a tolerância de +/-1 dia do casamento
    para amortecer: a mesma data decide a janela da RF-03, e um dia de erro perto de 2010-03-21
    arrastaria para dentro da janela um evento que deve ficar fora.
    """
    for event_id in (1069, 1124):
        shutil.copy(_FIXTURES / f"event_live_{event_id}.json", tmp_path)

    with caplog.at_level(logging.WARNING, logger="ingestion.ufc_official.discovery"):
        scan = discover_official_catalog(None, OfficialEventCache(tmp_path))

    assert {item.event_id for item in scan.items} == {"1124"}
    assert scan.undatable == ((1069, "UFC 271: Adesanya vs. Whittaker 2"),)
    assert "UFC 271: Adesanya vs. Whittaker 2" in caplog.text


def test_evento_sem_fuso_aparece_no_relatorio_final(db_session: Session) -> None:
    """O relatório de cobertura carrega os eventos sem fuso -- senão o motivo se perde.

    Sem isso, o evento persistido correspondente apareceria como "não casado" (ausência na
    fonte), que é o diagnóstico errado: a fonte tem o evento, só não tem a data dele.
    """
    _semeia(db_session, "UFC 271: Adesanya vs. Whittaker 2", date(2022, 2, 12))
    scan = CatalogScan(
        items=(),
        fetched=0,
        cache_hits=1,
        misses=0,
        highest_id=1069,
        discarded_other_promotions=0,
        undatable=((1069, "UFC 271: Adesanya vs. Whittaker 2"),),
    )

    relatorio = map_official_event_ids(db_session, scan)

    assert relatorio.undatable == ((1069, "UFC 271: Adesanya vs. Whittaker 2"),)


# --------------------------------------------------------------------------- #
# CA-02 / CA-03 / CA-05 -- mapeamento, relatório e idempotência
# --------------------------------------------------------------------------- #


def _semeia(session: Session, nome: str, quando: date) -> Event:
    """Insere um evento do seed Kaggle (sem identificador oficial) e devolve-o materializado."""
    evento = Event(name=nome, date=quando, location=None, source="kaggle")
    session.add(evento)
    session.flush()
    return evento


def _item(event_id: str, nome: str, quando: date) -> OfficialCatalogItem:
    return OfficialCatalogItem(event_id=event_id, name=nome, local_date=quando)


def _scan(*itens: OfficialCatalogItem) -> CatalogScan:
    """``CatalogScan`` montado à mão: o mapeamento não precisa saber de onde os itens vieram."""
    return CatalogScan(
        items=itens,
        fetched=0,
        cache_hits=len(itens),
        misses=0,
        highest_id=max((int(item.event_id) for item in itens), default=0),
        discarded_other_promotions=0,
        undatable=(),
    )


def _cenario(db_session: Session) -> dict[str, Event]:
    """Um evento persistido para cada caminho do mapeamento."""
    return {
        "casado": _semeia(db_session, "UFC 282: Blachowicz vs. Ankalaev", date(2022, 12, 10)),
        "revisao": _semeia(db_session, "UFC on Versus 1", date(2010, 8, 1)),
        "ambiguo": _semeia(db_session, "UFC Fight Night: Ninguem vs Ninguem", date(2024, 5, 10)),
        "sem_candidato": _semeia(db_session, "UFC 300: Pereira vs Hill", date(2024, 4, 13)),
        "fora_da_janela": _semeia(db_session, "UFC 111: St-Pierre vs Hardy", date(2010, 3, 20)),
    }


def _catalogo() -> CatalogScan:
    """Catálogo que exercita casamento, revisão, ambiguidade e a fronteira da janela."""
    return _scan(
        _item("1124", "UFC 282: Blachowicz vs Ankalaev", date(2022, 12, 10)),
        _item("281", "UFC Live: Vera vs Jones", date(2010, 8, 1)),
        _item("900", "UFC Fight Night: Ninguem vs Ninguem", date(2024, 5, 10)),
        _item("901", "UFC Fight Night: Ninguem vs. Ninguem", date(2024, 5, 11)),
        # Candidato perfeito para o evento de 2010-03-20, que está FORA da janela.
        _item("599", "UFC 111: St-Pierre vs Hardy", date(2010, 3, 20)),
    )


def test_evento_da_janela_com_nome_concordante_ganha_o_identificador(
    db_session: Session,
) -> None:
    """CA-02: o evento casado por nome recebe ``ufc_event_id`` vindo da fonte."""
    eventos = _cenario(db_session)

    map_official_event_ids(db_session, _catalogo())

    assert eventos["casado"].ufc_event_id == "1124"


def test_relatorio_conta_as_quatro_categorias_e_nomeia_ambiguos(db_session: Session) -> None:
    """CA-02: o relatório separa casados, em revisão, não casados e ambíguos -- com nomes."""
    eventos = _cenario(db_session)

    relatorio = map_official_event_ids(db_session, _catalogo())

    assert isinstance(relatorio, DiscoveryReport)
    assert relatorio.matched_by_name == 1
    assert relatorio.needs_review == ((eventos["revisao"].id, "UFC on Versus 1"),)
    assert relatorio.ambiguous == ((eventos["ambiguo"].id, "UFC Fight Night: Ninguem vs Ninguem"),)
    assert relatorio.unmatched == ((eventos["sem_candidato"].id, "UFC 300: Pereira vs Hill"),)
    assert relatorio.matched == 1
    assert relatorio.total == 4  # o evento fora da janela não entra em contagem nenhuma
    assert relatorio.coverage == pytest.approx(0.25)


def test_evento_ambiguo_nao_recebe_escrita_e_o_laco_segue(db_session: Session) -> None:
    """CA-03: ambiguidade é contada, nada é escrito para o evento, e o próximo é mapeado."""
    eventos = _cenario(db_session)

    relatorio = map_official_event_ids(db_session, _catalogo())

    assert eventos["ambiguo"].ufc_event_id is None
    assert len(relatorio.ambiguous) == 1
    assert eventos["casado"].ufc_event_id == "1124"


def test_evento_em_revisao_permanece_nulo(db_session: Session) -> None:
    """CA-02: candidato sem corroboração de nome não é escrito -- id errado é pior que ausente."""
    eventos = _cenario(db_session)

    map_official_event_ids(db_session, _catalogo())

    assert eventos["revisao"].ufc_event_id is None


def test_relatorio_sai_por_logging(db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    """CA-02: a cobertura é logada (``print`` é proibido pela regra T20)."""
    _cenario(db_session)

    with caplog.at_level(logging.INFO, logger="ingestion.ufc_official.discovery"):
        map_official_event_ids(db_session, _catalogo())

    assert "cobertura" in caplog.text.lower()
    # Os ambíguos são nomeados no log, não só contados.
    assert "UFC Fight Night: Ninguem vs Ninguem" in caplog.text


def test_base_sem_evento_nao_divide_por_zero(db_session: Session) -> None:
    """CA-02: cobertura de uma base vazia é ``0.0``, não uma exceção."""
    relatorio = map_official_event_ids(db_session, _catalogo())

    assert relatorio.total == 0
    assert relatorio.coverage == 0.0


# --------------------------------------------------------------------------- #
# CA-05 -- idempotência: rerun não muda contagem e não emite UPDATE
# --------------------------------------------------------------------------- #


@pytest.fixture
def updates_emitidos(db_session: Session) -> Iterator[list[str]]:
    """Espia o SQL emitido na sessão e coleta os ``UPDATE`` de ``events``."""
    coletados: list[str] = []

    def _antes_do_cursor(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE EVENTS"):
            coletados.append(statement)

    bind = db_session.get_bind()
    sa_event.listen(bind, "before_cursor_execute", _antes_do_cursor)
    try:
        yield coletados
    finally:
        sa_event.remove(bind, "before_cursor_execute", _antes_do_cursor)


def test_rerun_nao_altera_contagem_nem_emite_update(
    db_session: Session, updates_emitidos: list[str]
) -> None:
    """CA-05: a segunda execução repete o relatório e **não** escreve no banco.

    O campo só é atribuído quando o valor muda: sem isso, cada reexecução sujaria o log de
    replicação com UPDATEs que não mudam nada.
    """
    eventos = _cenario(db_session)
    primeiro = map_official_event_ids(db_session, _catalogo())
    db_session.flush()
    updates_emitidos.clear()

    segundo = map_official_event_ids(db_session, _catalogo())
    db_session.flush()

    assert segundo == primeiro
    assert updates_emitidos == []
    assert eventos["casado"].ufc_event_id == "1124"


# --------------------------------------------------------------------------- #
# CA-06 -- fronteira da janela ponta a ponta
# --------------------------------------------------------------------------- #


def test_evento_anterior_a_janela_com_candidato_perfeito_permanece_nulo(
    db_session: Session,
) -> None:
    """CA-06: 2010-03-20 não é tocado, mesmo com candidato de nome e data idênticos.

    É o teste que prova que a janela é uma **guarda**, não um efeito colateral da cobertura da
    fonte: o candidato existe, casaria por nome, e ainda assim nada é escrito.
    """
    eventos = _cenario(db_session)

    relatorio = map_official_event_ids(db_session, _catalogo())

    assert eventos["fora_da_janela"].ufc_event_id is None
    ids_reportados = {
        event_id
        for grupo in (relatorio.needs_review, relatorio.unmatched, relatorio.ambiguous)
        for event_id, _ in grupo
    }
    assert eventos["fora_da_janela"].id not in ids_reportados


def test_evento_na_fronteira_exata_e_mapeado(db_session: Session) -> None:
    """CA-06: 2010-03-21, a primeira data da janela, é mapeado normalmente."""
    evento = _semeia(db_session, "UFC Live: Vera vs Jones", OFFICIAL_WINDOW_START)
    scan = _scan(_item("281", "UFC Live: Vera vs Jones", OFFICIAL_WINDOW_START))

    map_official_event_ids(db_session, scan)

    assert evento.ufc_event_id == "281"


# --------------------------------------------------------------------------- #
# CLI -- execução offline ponta a ponta, sem rede
# --------------------------------------------------------------------------- #


def test_execucao_offline_mapeia_a_partir_do_diretorio_de_fixtures(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Ponta a ponta sobre o conjunto derivado, sem tocar a rede.

    O modo offline (``--fixture-dir`` no CLI, ``client=None`` aqui) lê o diretório como cache e
    não busca nada. É o caminho que prova a projeção do payload cru em item de catálogo: sem
    ele, os testes de mapeamento nunca sairiam de itens montados à mão.

    ``main`` em si é fino (constrói cache, cliente e sessão, e commita) e é verificado por
    execução real contra a fonte -- mesmo precedente de ``ingestion/seed.py::main``.
    """
    evento = _semeia(db_session, "UFC 282: Blachowicz vs. Ankalaev", date(2022, 12, 10))
    fora_do_escopo = _semeia(db_session, "DWCS Brazil 1.3", date(2018, 8, 13))
    anterior_a_janela = _semeia(db_session, "UFC 92: The Ultimate 2008", date(2008, 12, 27))

    with caplog.at_level(logging.INFO, logger="ingestion.ufc_official.discovery"):
        relatorio = run_discovery(db_session, None, OfficialEventCache(_FIXTURES))

    assert evento.ufc_event_id == "1124"
    # DWCS é fase de promoção posterior: descartado pelo OrganizationId, nunca casado.
    assert fora_do_escopo.ufc_event_id is None
    assert anterior_a_janela.ufc_event_id is None
    assert relatorio.matched_by_name == 1
    assert relatorio.discarded_other_promotions == 3  # WEC 29, DWCS Brazil 1.3 e Gladiator FC
    assert "cobertura" in caplog.text.lower()


def test_modo_offline_nao_escreve_no_diretorio_de_fixtures(db_session: Session) -> None:
    """O modo offline é somente leitura: nada é gravado sobre as fixtures versionadas."""
    antes = {caminho.name: caminho.stat().st_mtime for caminho in _FIXTURES.iterdir()}

    run_discovery(db_session, None, OfficialEventCache(_FIXTURES))

    assert {caminho.name: caminho.stat().st_mtime for caminho in _FIXTURES.iterdir()} == antes
