"""Testes do comando de sincronização do catálogo Cito (Slice 03), todos em modo fixture.

Cobrem CA-05, CA-07 e CA-08 da sprint 007-03 contra o Postgres de teste (sessão transacional):
o comando preenche ``events.cito_slug``/``cito_event_id`` de eventos numerados **e** de Fight
Nights com o slug vindo do catálogo; emite o relatório de cobertura (casados por nome, casados
só por data, não casados, ambíguos); é idempotente; conta e **pula** o evento ambíguo sem
escrever nada para ele; e aborta antes de qualquer chamada quando o gate humano não foi
confirmado.

Nenhum teste toca a rede: o cliente roda sobre o conjunto derivado de duas páginas
(``fixtures/catalog_paginado_derivado/``, itens reais da captura de 2026-08-31) e o gate é
exercitado com um spy que prova zero chamadas.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.client import CallBudget, CitoClient
from ingestion.cito.dto import CitoCatalogItem
from ingestion.cito.sync_catalog import CatalogSyncReport, main, sync_event_catalog

_FIXTURES = Path(__file__).parent / "fixtures"
_CATALOGO_PAGINADO = _FIXTURES / "catalog_paginado_derivado"


def _cliente(budget: CallBudget) -> CitoClient:
    """Cliente em modo fixture sobre o conjunto derivado de duas páginas (zero quota real)."""
    return CitoClient(
        token="",
        base_url="https://api.citoapi.com",
        fixture_dir=_CATALOGO_PAGINADO,
        budget=budget,
    )


def _semeia(session: Session, nome: str, quando: date) -> Event:
    """Insere um evento do seed Kaggle (sem identificador Cito) e devolve-o materializado."""
    evento = Event(name=nome, date=quando, location=None, source="kaggle")
    session.add(evento)
    session.flush()
    return evento


def _semeia_cenario(session: Session) -> dict[str, Event]:
    """Semeia um evento para cada caminho do casamento, contra o catálogo derivado.

    - ``numerado`` e os dois ``fight_night`` casam por nome.
    - ``so_por_data``: único candidato na janela ('cryptocom-ufc-331', data local 2026-09-19),
      mas com grafia de nome que não concorda -- casa marcado para inspeção humana.
    - ``ambiguo``: 2026-05-10 tem dois candidatos reais na janela ('ufc-315' em 2026-05-10 e
      'ufc-328' em 2026-05-09) e nenhum concorda por nome.
    - ``sem_candidato``: fora de qualquer janela do catálogo.
    """
    return {
        "numerado": _semeia(session, "UFC 335", date(2026, 12, 12)),
        "fight_night_agosto": _semeia(
            session, "UFC Fight Night: Hernandez vs Rodrigues", date(2026, 8, 22)
        ),
        "fight_night_outubro": _semeia(session, "UFC Fight Night: TBD vs TBD", date(2026, 10, 31)),
        "so_por_data": _semeia(session, "UFC 331: Grafia Divergente", date(2026, 9, 19)),
        "ambiguo": _semeia(session, "UFC Fight Night: Ninguem vs Ninguem", date(2026, 5, 10)),
        "sem_candidato": _semeia(session, "UFC 1: The Beginning", date(1993, 11, 12)),
    }


# --------------------------------------------------------------------------- #
# CA-07 -- preenchimento e relatório de cobertura
# --------------------------------------------------------------------------- #


def test_sync_preenche_numerado_e_fight_nights_com_o_slug_do_catalogo(
    db_session: Session,
) -> None:
    """CA-07: eventos numerados **e** Fight Nights ganham ``cito_slug``/``cito_event_id``.

    O slug vem do item do catálogo -- em particular 'ufc-fight-night-august-22-2026', cuja data
    é a local e não a UTC de ``startsAt``; nenhuma regra sobre o nome poderia produzi-lo.
    """
    eventos = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    sync_event_catalog(db_session, _cliente(budget), budget)

    assert eventos["numerado"].cito_slug == "ufc-335"
    assert eventos["numerado"].cito_event_id == "db421fc1-05eb-47de-be41-21f2c5c313ec"
    assert eventos["fight_night_agosto"].cito_slug == "ufc-fight-night-august-22-2026"
    assert eventos["fight_night_outubro"].cito_slug == "ufc-fight-night-october-31-2026"
    assert eventos["fight_night_agosto"].cito_event_id is not None


def test_relatorio_conta_as_quatro_categorias_de_cobertura(db_session: Session) -> None:
    """CA-07: o relatório separa casados por nome, casados só por data, não casados e ambíguos."""
    eventos = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    relatorio = sync_event_catalog(db_session, _cliente(budget), budget)

    assert isinstance(relatorio, CatalogSyncReport)
    assert relatorio.matched_by_name == 3
    assert relatorio.matched_by_date_only == (
        (eventos["so_por_data"].id, "UFC 331: Grafia Divergente"),
    )
    assert relatorio.unmatched == ((eventos["sem_candidato"].id, "UFC 1: The Beginning"),)
    assert relatorio.ambiguous == ((eventos["ambiguo"].id, "UFC Fight Night: Ninguem vs Ninguem"),)
    assert relatorio.matched == 4
    assert relatorio.total == 6
    assert relatorio.cito_calls_used == 2  # duas páginas de catálogo
    # DWCS e Road to UFC são descartados antes do casamento (CA-06).
    assert relatorio.catalog_items == 7


def test_casamento_so_por_data_e_persistido_e_marcado(db_session: Session) -> None:
    """CA-07: o casamento só-por-data escreve o slug, mas é reportado à parte para inspeção."""
    eventos = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    sync_event_catalog(db_session, _cliente(budget), budget)

    assert eventos["so_por_data"].cito_slug == "cryptocom-ufc-331"


def test_evento_sem_candidato_permanece_nulo(db_session: Session) -> None:
    """CA-07: sem correspondência no catálogo, os identificadores seguem nulos.

    Ausência explícita: nunca sentinela, nunca slug inventado.
    """
    eventos = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    sync_event_catalog(db_session, _cliente(budget), budget)

    assert eventos["sem_candidato"].cito_slug is None
    assert eventos["sem_candidato"].cito_event_id is None


def test_relatorio_calcula_cobertura_sem_divisao_por_zero(db_session: Session) -> None:
    """CA-07: base sem evento nenhum devolve cobertura ``0.0`` em vez de estourar."""
    budget = CallBudget(limit=10)

    relatorio = sync_event_catalog(db_session, _cliente(budget), budget)

    assert relatorio.total == 0
    assert relatorio.coverage == 0.0


def test_relatorio_e_logado_via_logging(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-07: a cobertura sai por ``logging`` (``print`` é proibido pela regra T20)."""
    _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    with caplog.at_level(logging.INFO, logger="ingestion.cito.sync_catalog"):
        sync_event_catalog(db_session, _cliente(budget), budget)

    assert "cobertura" in caplog.text.lower()


def test_rerun_e_idempotente(db_session: Session) -> None:
    """CA-07: reexecutar não altera valores nem contagem -- idempotência observável."""
    eventos = _semeia_cenario(db_session)
    primeiro_budget = CallBudget(limit=10)
    primeiro = sync_event_catalog(db_session, _cliente(primeiro_budget), primeiro_budget)
    db_session.flush()
    slugs_apos_primeira = {
        evento.id: evento.cito_slug for evento in db_session.scalars(select(Event))
    }

    segundo_budget = CallBudget(limit=10)
    segundo = sync_event_catalog(db_session, _cliente(segundo_budget), segundo_budget)
    db_session.flush()

    assert {evento.id: evento.cito_slug for evento in db_session.scalars(select(Event))} == (
        slugs_apos_primeira
    )
    assert segundo.matched_by_name == primeiro.matched_by_name
    assert segundo.matched_by_date_only == primeiro.matched_by_date_only
    assert segundo.unmatched == primeiro.unmatched
    assert segundo.ambiguous == primeiro.ambiguous
    assert eventos["numerado"].cito_slug == "ufc-335"


def test_janela_de_datas_restringe_os_eventos_considerados(db_session: Session) -> None:
    """CA-07: ``date_from``/``date_to`` limitam os eventos persistidos considerados.

    A mesma janela é enviada à Cito e aplicada ao ``select`` de ``events`` -- os dois lados
    enxergam o mesmo recorte.
    """
    eventos = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    relatorio = sync_event_catalog(
        db_session,
        _cliente(budget),
        budget,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )

    assert relatorio.total == 1
    assert eventos["fight_night_agosto"].cito_slug == "ufc-fight-night-august-22-2026"
    assert eventos["numerado"].cito_slug is None


# --------------------------------------------------------------------------- #
# CA-05 -- ambiguidade é contada e pulada, sem derrubar o run
# --------------------------------------------------------------------------- #


def test_evento_ambiguo_fica_nulo_e_execucao_nao_aborta(db_session: Session) -> None:
    """CA-05: ambiguidade é contada e o evento é pulado **sem escrita**; o run segue.

    Os demais eventos do mesmo run continuam sendo casados -- a ambiguidade não derruba tudo.
    """
    eventos = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    relatorio = sync_event_catalog(db_session, _cliente(budget), budget)

    assert eventos["ambiguo"].cito_slug is None
    assert eventos["ambiguo"].cito_event_id is None
    assert len(relatorio.ambiguous) == 1
    assert eventos["numerado"].cito_slug == "ufc-335"


# --------------------------------------------------------------------------- #
# CA-08 -- gate humano antes da rede real
# --------------------------------------------------------------------------- #


def test_main_rede_real_sem_confirmacao_nao_dispara_nenhuma_chamada(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-08: o CLI em rede real sem ``--confirmar-gasto-de-quota`` aborta antes de qualquer fetch.

    Um spy sobre ``CitoClient.fetch_event_catalog`` prova ZERO chamadas: o gate bloqueia antes
    mesmo de instanciar o cliente ou abrir a sessão. O comando encerra com exit code != 0.
    """
    chamadas: list[str] = []

    def _spy(self: CitoClient, **kwargs: object) -> list[CitoCatalogItem]:
        chamadas.append("catalogo")
        raise AssertionError("a rede não deveria ser tocada sem o gate confirmado")

    monkeypatch.setattr(CitoClient, "fetch_event_catalog", _spy)

    with (
        caplog.at_level(logging.ERROR, logger="ingestion.cito.sync_catalog"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--call-budget", "5"])

    assert excinfo.value.code != 0
    assert chamadas == []
    assert "gate" in caplog.text.lower()


def test_sync_repassa_o_cache_ao_cliente(db_session: Session, tmp_path: Path) -> None:
    """CA-07: o comando repassa o ``cache`` ao cliente -- a segunda execução gasta ZERO quota.

    Sem o repasse, cada execução (e cada sprint seguinte que releia o catálogo) re-gastaria as
    ~9 páginas. Este teste existe porque o repasse já falhou uma vez de forma silenciosa: o
    comando funcionava e apenas não economizava nada.
    """
    _semeia_cenario(db_session)
    cache = CatalogPageCache(tmp_path)

    primeiro_budget = CallBudget(limit=10)
    sync_event_catalog(db_session, _cliente(primeiro_budget), primeiro_budget, cache=cache)

    segundo_budget = CallBudget(limit=10)
    relatorio = sync_event_catalog(
        db_session, _cliente(segundo_budget), segundo_budget, cache=cache
    )

    assert primeiro_budget.used == 2
    assert segundo_budget.used == 0
    assert relatorio.catalog_items == 7  # o catálogo veio inteiro do disco
    assert sorted(p.name for p in tmp_path.glob("*.json")) != []
