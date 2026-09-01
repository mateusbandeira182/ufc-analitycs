"""Testes do casamento evento persistido <-> catálogo da fonte oficial (Slice 02).

Duas metades:

1. **Casamento puro** (``ingestion.ufc_official.matching``) -- função sem banco: janela de data
   de +/-1 dia, desempate por nome normalizado e ambiguidade que falha alto. O filtro de
   promoção não está aqui: o catálogo que chega ao casamento já é só do UFC, porque a
   descoberta o aplica na projeção (ver ``test_ufc_official_discovery_cli.py``).
2. **Janela da RF-03** (``ingestion.ufc_official.discovery.select_window_events``) -- contra o
   Postgres de teste, com sessão transacional: nenhum evento anterior a
   ``OFFICIAL_WINDOW_START`` entra na seleção.

A política aqui é **mais restritiva** que a da Cito de propósito: lá, um candidato único na
janela de data era aceito sem concordância de nome (``matched_by_name=False``, reportado para
inspeção). Aqui não se escreve nada sem corroboração de nome, porque o consumidor a jusante é a
escrita de **canto autoritativo** (Slice 04): um ``ufc_event_id`` errado corrigiria o canto com
o card do evento errado. Id errado é pior que id ausente.

Nenhum teste toca a rede.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.discovery import select_window_events
from ingestion.ufc_official.matching import (
    AmbiguousOfficialEventMatchError,
    OfficialCatalogItem,
    official_event_candidates,
    resolve_official_event_match,
)


def _evento(nome: str, quando: date) -> Event:
    """``Event`` não-persistido (o casamento é função pura -- não toca o banco)."""
    return Event(name=nome, date=quando, location=None, source="kaggle")


def _item(event_id: str, nome: str, quando: date) -> OfficialCatalogItem:
    """Item de catálogo já projetado do payload oficial (a projeção é da descoberta)."""
    return OfficialCatalogItem(event_id=event_id, name=nome, local_date=quando)


# --------------------------------------------------------------------------- #
# CA-02 -- casamento por data + nome normalizado
# --------------------------------------------------------------------------- #


def test_candidato_unico_com_nome_concordante_casa() -> None:
    """CA-02: um candidato na janela cujo nome normalizado concorda resolve o casamento.

    A grafia difere na pontuação ('vs.' x 'vs'), que ``normalize_event_name`` colapsa -- é a
    mesma chave usada no casamento da Cito, de propósito: uma normalização divergente entre as
    fontes quebraria o casamento em silêncio.
    """
    evento = _evento("UFC 282: Blachowicz vs. Ankalaev", date(2022, 12, 10))
    catalogo = [_item("1124", "UFC 282: Blachowicz vs Ankalaev", date(2022, 12, 10))]

    casamento = resolve_official_event_match(evento, official_event_candidates(evento, catalogo))

    assert casamento is not None
    assert casamento.item.event_id == "1124"
    assert casamento.matched_by == "name"


def test_janela_de_data_admite_um_dia_de_diferenca() -> None:
    """CA-02: a data local pode divergir em um dia entre as fontes e ainda casar.

    A data persistida vem do seed e a da fonte sai do instante UTC com o fuso do evento; a
    tolerância de +/-1 dia absorve a divergência residual. Não é heurística: a data é dado real
    dos dois lados.
    """
    evento = _evento("UFC 184: Rousey vs Zingano", date(2015, 2, 28))
    catalogo = [_item("700", "UFC 184: Rousey vs Zingano", date(2015, 3, 1))]

    casamento = resolve_official_event_match(evento, official_event_candidates(evento, catalogo))

    assert casamento is not None
    assert casamento.item.event_id == "700"


def test_candidato_fora_da_janela_de_data_nao_e_candidato() -> None:
    """CA-02: item a mais de um dia de distância não entra sequer na lista de candidatos."""
    evento = _evento("UFC 282: Blachowicz vs Ankalaev", date(2022, 12, 10))
    catalogo = [_item("1124", "UFC 282: Blachowicz vs Ankalaev", date(2022, 12, 13))]

    candidatos = official_event_candidates(evento, catalogo)

    assert candidatos == ()
    assert resolve_official_event_match(evento, candidatos) is None


def test_candidato_unico_sem_concordancia_de_nome_nao_casa() -> None:
    """CA-02: candidato único na janela, mas sem concordância de nome, **não** é escrito.

    Divergência deliberada do precedente da Cito (``matched_by_name=False``, que era aceito):
    aqui o consumidor a jusante é a correção de canto da Slice 04, e um ``ufc_event_id`` errado
    corrigiria o canto com o card do evento errado. Sem corroboração, nada é escrito -- o
    evento fica em revisão.
    """
    evento = _evento("UFC on Versus 1", date(2010, 3, 21))
    catalogo = [_item("281", "UFC Live: Vera vs Jones", date(2010, 3, 21))]

    candidatos = official_event_candidates(evento, catalogo)

    assert len(candidatos) == 1  # há candidato: o evento vai para revisão, não para não-casado
    assert resolve_official_event_match(evento, candidatos) is None


# --------------------------------------------------------------------------- #
# CA-03 -- ambiguidade falha alto
# --------------------------------------------------------------------------- #


def test_dois_candidatos_concordando_por_nome_falha_alto() -> None:
    """CA-03: dois candidatos concordando por nome levantam em vez de escolher um.

    Nunca casar no escuro: quem chama conta a ambiguidade, não escreve nada para o evento e
    segue para o próximo.
    """
    evento = _evento("UFC Fight Night: Ninguem vs Ninguem", date(2024, 5, 10))
    catalogo = [
        _item("900", "UFC Fight Night: Ninguem vs Ninguem", date(2024, 5, 10)),
        _item("901", "UFC Fight Night: Ninguem vs. Ninguem", date(2024, 5, 11)),
    ]

    with pytest.raises(AmbiguousOfficialEventMatchError) as excinfo:
        resolve_official_event_match(evento, official_event_candidates(evento, catalogo))

    # A mensagem nomeia o evento e os ids candidatos -- ambiguidade sem nome é caça ao tesouro.
    assert "900" in str(excinfo.value)
    assert "901" in str(excinfo.value)


def test_dois_candidatos_sem_concordancia_de_nome_nao_casam_nem_levantam() -> None:
    """CA-03: sem concordância de nome não há o que desempatar -- ninguém é escrito.

    A ambiguidade só é erro quando existiria escrita a decidir. Aqui nenhum candidato seria
    escrito de qualquer forma, então o evento entra em revisão em silêncio contado.
    """
    evento = _evento("UFC Fight Night: Alguem vs Alguem", date(2024, 5, 10))
    catalogo = [
        _item("900", "UFC Fight Night: Outro vs Outro", date(2024, 5, 10)),
        _item("901", "UFC Fight Night: Terceiro vs Terceiro", date(2024, 5, 11)),
    ]

    assert resolve_official_event_match(evento, official_event_candidates(evento, catalogo)) is None


# --------------------------------------------------------------------------- #
# CA-06 -- janela da RF-03 (2010-03-21)
# --------------------------------------------------------------------------- #


def _semeia(session: Session, nome: str, quando: date) -> Event:
    """Insere um evento do seed Kaggle (sem identificador oficial) e devolve-o materializado."""
    evento = Event(name=nome, date=quando, location=None, source="kaggle")
    session.add(evento)
    session.flush()
    return evento


def test_janela_comeca_em_2010_03_21_e_exclui_a_vespera(db_session: Session) -> None:
    """CA-06: 2010-03-20 fica fora da seleção; 2010-03-21 entra.

    A fronteira não é arbitrária: 2010-03-21 (UFC Live: Vera vs Jones) é a primeira data em que
    o canto foi REALMENTE registrado. Antes disso as três fontes trazem o mesmo canto fabricado
    (ADR 0006), então não há o que a fonte oficial autorize.
    """
    vespera = _semeia(db_session, "UFC 111: St-Pierre vs Hardy", date(2010, 3, 20))
    fronteira = _semeia(db_session, "UFC Live: Vera vs Jones", OFFICIAL_WINDOW_START)
    depois = _semeia(db_session, "UFC 182: Jones vs Cormier", date(2015, 1, 1))

    selecionados = select_window_events(db_session)

    assert [evento.id for evento in selecionados] == [fronteira.id, depois.id]
    assert vespera.id not in {evento.id for evento in selecionados}


def test_janela_devolve_em_ordem_cronologica_estavel(db_session: Session) -> None:
    """CA-06: a seleção sai ordenada por ``(date, id)`` -- ordem estável entre execuções."""
    tardio = _semeia(db_session, "UFC 182: Jones vs Cormier", date(2015, 1, 1))
    cedo = _semeia(db_session, "UFC Live: Vera vs Jones", OFFICIAL_WINDOW_START)
    mesmo_dia = _semeia(db_session, "UFC Live: Outro Card", OFFICIAL_WINDOW_START)

    selecionados = select_window_events(db_session)

    esperado = [*sorted([cedo.id, mesmo_dia.id]), tardio.id]
    assert [evento.id for evento in selecionados] == esperado


def test_constante_da_janela_nao_e_a_do_filtro_de_treino() -> None:
    """``OFFICIAL_WINDOW_START`` e ``FIRST_RELIABLE_CORNER_DATE`` são constantes distintas.

    Valores e propósitos diferentes: 2010-03-21 é a autoridade da **fonte** (limitação medida,
    SPEC 008) e 2010-01-01 é o corte do **treino** (ADR 0006). Unificá-las mudaria em silêncio
    o recorte de uma das duas.
    """
    from analysis.dataset import FIRST_RELIABLE_CORNER_DATE

    assert date(2010, 3, 21) == OFFICIAL_WINDOW_START
    assert OFFICIAL_WINDOW_START != FIRST_RELIABLE_CORNER_DATE
