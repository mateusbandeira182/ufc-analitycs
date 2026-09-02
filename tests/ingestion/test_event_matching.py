"""Testes do casamento evento persistido <-> item do catálogo Cito (Slice 03).

Cobrem CA-04, CA-05 e CA-06 da sprint 007-03: normalização de título de evento, filtro de
promoção (o que é UFC e o que não é), e ``resolve_event_match`` -- a função pura que decide,
por **data local + nome normalizado**, qual item do catálogo corresponde a um evento já
persistido. Nenhum slug é derivado por regra em ponto algum: o slug vem do item.

Os itens usados são **reais**, recortados da captura de 2026-08-31
(``fixtures/events_catalog_page_1.json``): os casos-armadilha existem no dado, não foram
inventados.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from apps.events.models import Event
from ingestion.cito.dto import CitoCatalogItem
from ingestion.cito.matching import (
    AmbiguousEventMatchError,
    is_ufc_catalog_item,
    resolve_event_match,
)
from ingestion.normalize import normalize_event_name

_FIXTURES = Path(__file__).parent / "fixtures"


def _catalogo_real() -> dict[str, CitoCatalogItem]:
    """Itens reais da captura do catálogo, indexados por slug."""
    payload = json.loads((_FIXTURES / "events_catalog_page_1.json").read_text(encoding="utf-8"))
    itens = [CitoCatalogItem.model_validate(bruto) for bruto in payload["data"]]
    return {item.slug: item for item in itens}


def _evento(nome: str, quando: date) -> Event:
    """``Event`` não-persistido (o casamento é função pura -- não toca o banco)."""
    return Event(name=nome, date=quando, location=None, source="kaggle")


# --------------------------------------------------------------------------- #
# CA-04 -- normalização do título do evento
# --------------------------------------------------------------------------- #


def test_normalize_event_name_colapsa_pontuacao_na_mesma_chave() -> None:
    """CA-04: dois-pontos, ponto e hífen colapsam -- a grafia da fonte não decide o casamento."""
    assert (
        normalize_event_name("UFC 319: Du Plessis vs. Chimaev") == "ufc 319 du plessis vs chimaev"
    )
    assert (
        normalize_event_name("UFC 319 - Du Plessis vs Chimaev") == "ufc 319 du plessis vs chimaev"
    )


def test_normalize_event_name_remove_acento_sem_comer_letra() -> None:
    """CA-04: o NFKD roda ANTES da limpeza de pontuação -- letra acentuada vira letra, não some.

    Se a ordem fosse invertida, ``Šarić`` viraria ``ari`` (as letras acentuadas seriam
    descartadas como não-alfanuméricas antes de serem decompostas).
    """
    assert normalize_event_name("UFC 300: Šarić vs. Ferreira") == "ufc 300 saric vs ferreira"


def test_normalize_event_name_colapsa_espacos() -> None:
    """CA-04: espaços múltiplos colapsam num só; sem sobra nas pontas."""
    assert normalize_event_name("  UFC   Fight  Night:  Hernandez   vs Rodrigues ") == (
        "ufc fight night hernandez vs rodrigues"
    )


# --------------------------------------------------------------------------- #
# CA-06 -- filtro de promoção (por exclusão, nunca por prefixo do slug)
# --------------------------------------------------------------------------- #


def test_filtro_descarta_dwcs_e_road_to_ufc() -> None:
    """CA-06: DWCS e Road to UFC ficam fora nesta fase (decisão do humano, SPEC 007).

    ``road-to-ufc-season-5-semifinals`` contém 'ufc' no meio do slug -- um filtro por
    substring o aceitaria por engano.
    """
    catalogo = _catalogo_real()

    assert is_ufc_catalog_item(catalogo["dwcs-10-8"]) is False
    assert is_ufc_catalog_item(catalogo["road-to-ufc-season-5-semifinals"]) is False


def test_filtro_mantem_ufc_com_patrocinador_e_nome_atipico() -> None:
    """CA-06: patrocinador no slug e nome atípico são UFC e precisam sobreviver ao filtro.

    ``cryptocom-ufc-331`` não começa com 'ufc-': um allowlist por prefixo o descartaria.
    """
    catalogo = _catalogo_real()

    assert is_ufc_catalog_item(catalogo["cryptocom-ufc-331"]) is True
    assert is_ufc_catalog_item(catalogo["ufc-freedom-250"]) is True
    assert is_ufc_catalog_item(catalogo["ufc-335"]) is True
    assert is_ufc_catalog_item(catalogo["ufc-fight-night-august-22-2026"]) is True


# --------------------------------------------------------------------------- #
# CA-04 / CA-05 -- casamento evento persistido <-> item do catálogo
# --------------------------------------------------------------------------- #


def test_fight_night_casa_apesar_da_data_do_slug_ser_um_dia_antes() -> None:
    """CA-04: a armadilha central -- o slug usa a data local, ``startsAt`` é UTC do dia seguinte.

    O evento persistido em 2026-08-22 casa com o item cujo ``startsAt`` é 2026-08-23 UTC, e o
    slug vem do item ('ufc-fight-night-august-22-2026'), nunca de uma regra sobre o nome.
    """
    catalogo = _catalogo_real()
    evento = _evento("UFC Fight Night: Hernandez vs Rodrigues", date(2026, 8, 22))

    casamento = resolve_event_match(evento, list(catalogo.values()))

    assert casamento is not None
    assert casamento.item.slug == "ufc-fight-night-august-22-2026"
    assert casamento.item.starts_at.date() == date(2026, 8, 23)
    assert casamento.matched_by_name is True


def test_segundo_fight_night_com_deslocamento_de_data_tambem_casa() -> None:
    """CA-04: segundo caso real do deslocamento (``startsAt`` 2026-11-01 -> slug de 10-31)."""
    catalogo = _catalogo_real()
    evento = _evento("UFC Fight Night: TBD vs TBD", date(2026, 10, 31))

    casamento = resolve_event_match(evento, list(catalogo.values()))

    assert casamento is not None
    assert casamento.item.slug == "ufc-fight-night-october-31-2026"


def test_nome_concordante_desempata_entre_varios_candidatos_na_janela() -> None:
    """CA-04: com dois candidatos na janela, o nome normalizado concordante vence.

    ``ufc-315`` (data local 2026-05-10) e ``ufc-328`` (2026-05-09) são ambos candidatos de um
    evento persistido em 2026-05-10; só o nome desempata.
    """
    catalogo = _catalogo_real()
    evento = _evento("UFC 328: Chimaev vs. Strickland", date(2026, 5, 10))

    casamento = resolve_event_match(evento, list(catalogo.values()))

    assert casamento is not None
    assert casamento.item.slug == "ufc-328"
    assert casamento.matched_by_name is True


def test_dois_candidatos_sem_nome_concordante_levanta_ambiguidade() -> None:
    """CA-05: dois candidatos na janela, nenhum nome concordante -- nunca escolhe sozinho."""
    catalogo = _catalogo_real()
    evento = _evento("UFC Fight Night: Desconhecido vs Ninguem", date(2026, 5, 10))

    with pytest.raises(AmbiguousEventMatchError):
        resolve_event_match(evento, list(catalogo.values()))


def test_dois_candidatos_com_o_mesmo_nome_normalizado_levanta_ambiguidade() -> None:
    """CA-05: nome concordante que casa com mais de um item também falha alto."""
    catalogo = _catalogo_real()
    gemeo = catalogo["ufc-335"].model_copy(update={"id": "outro-id", "slug": "ufc-335-duplicado"})
    evento = _evento("UFC 335", date(2026, 12, 12))

    with pytest.raises(AmbiguousEventMatchError):
        resolve_event_match(evento, [catalogo["ufc-335"], gemeo])


def test_candidato_unico_sem_nome_concordante_casa_marcado_como_so_por_data() -> None:
    """CA-04: candidato único na janela é aceito, mas **marcado** para inspeção humana.

    O casamento só-por-data é reportado à parte justamente porque não teve confirmação de nome
    -- a Slice 05 não deve gastar quota nesses slugs antes de o humano olhar.
    """
    catalogo = _catalogo_real()
    evento = _evento("UFC Fight Night: Grafia Divergente", date(2026, 8, 22))

    casamento = resolve_event_match(evento, [catalogo["ufc-fight-night-august-22-2026"]])

    assert casamento is not None
    assert casamento.item.slug == "ufc-fight-night-august-22-2026"
    assert casamento.matched_by_name is False


def test_sem_candidato_na_janela_devolve_none() -> None:
    """CA-07: nenhum item na janela -> ``None`` (não casado, reportado por quem chama)."""
    catalogo = _catalogo_real()
    evento = _evento("UFC 1: The Beginning", date(1993, 11, 12))

    assert resolve_event_match(evento, list(catalogo.values())) is None


def test_janela_de_tolerancia_e_simetrica_e_de_um_dia() -> None:
    """CA-04: a janela cobre +/-1 dia e nada além disso (dois dias de distância não é candidato)."""
    catalogo = _catalogo_real()
    item = catalogo["ufc-fight-night-august-22-2026"]  # data local 2026-08-22

    assert resolve_event_match(_evento("X", date(2026, 8, 21)), [item]) is not None
    assert resolve_event_match(_evento("X", date(2026, 8, 23)), [item]) is not None
    assert resolve_event_match(_evento("X", date(2026, 8, 20)), [item]) is None
    assert resolve_event_match(_evento("X", date(2026, 8, 24)), [item]) is None
