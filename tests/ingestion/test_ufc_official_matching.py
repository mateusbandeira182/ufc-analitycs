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


def _item(
    event_id: str, nome: str, quando: date, roster: frozenset[str] = frozenset()
) -> OfficialCatalogItem:
    """Item de catálogo já projetado do payload oficial (a projeção é da descoberta).

    O default vazio de ``roster`` é uma conveniência **do teste**: na produção o campo é
    obrigatório, porque um default silenciaria a ausência do roster num call site esquecido.
    """
    return OfficialCatalogItem(
        event_id=event_id, name=nome, local_date=quando, fighter_names=roster
    )


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

    casamento = resolve_official_event_match(
        evento, official_event_candidates(evento, catalogo), roster=frozenset()
    )

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

    casamento = resolve_official_event_match(
        evento, official_event_candidates(evento, catalogo), roster=frozenset()
    )

    assert casamento is not None
    assert casamento.item.event_id == "700"


def test_candidato_fora_da_janela_de_data_nao_e_candidato() -> None:
    """CA-02: item a mais de um dia de distância não entra sequer na lista de candidatos."""
    evento = _evento("UFC 282: Blachowicz vs Ankalaev", date(2022, 12, 10))
    catalogo = [_item("1124", "UFC 282: Blachowicz vs Ankalaev", date(2022, 12, 13))]

    candidatos = official_event_candidates(evento, catalogo)

    assert candidatos == ()
    assert resolve_official_event_match(evento, candidatos, roster=frozenset()) is None


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
    assert resolve_official_event_match(evento, candidatos, roster=frozenset()) is None


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
        resolve_official_event_match(
            evento, official_event_candidates(evento, catalogo), roster=frozenset()
        )

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

    candidatos = official_event_candidates(evento, catalogo)

    assert resolve_official_event_match(evento, candidatos, roster=frozenset()) is None


# --------------------------------------------------------------------------- #
# Sprint 008-08 -- tier 2: corroboração por roster
# --------------------------------------------------------------------------- #

# Roster do card real de ``Noche UFC`` (2025-09-13), já normalizado dos dois lados.
_NOCHE = frozenset(
    {
        "diego lopes",
        "jean silva",
        "kelvin gastelum",
        "dustin stoltzfus",
        "david martinez",
        "rob font",
    }
)


def test_tier_de_nome_tem_precedencia_sobre_o_roster() -> None:
    """CA-01: evento que casa por nome continua casando por nome, com o **mesmo** valor.

    O cenário é adversarial de propósito: na mesma janela de data existe um candidato cujo
    roster coincide inteiro com o do evento. Ainda assim o resultado é o casamento por nome --
    o tier 2 só é consultado quando o tier 1 **não** resolve.

    É a trava que impede um evento já mapeado de mudar de ``ufc_event_id`` quando o tier novo
    entra: um remapeamento silencioso corrigiria o canto com o card do evento errado.
    """
    evento = _evento("Noche UFC", date(2025, 9, 13))
    catalogo = [
        _item("1300", "Noche UFC", date(2025, 9, 13), frozenset()),
        _item("1301", "Noche UFC: Lopes vs. Silva", date(2025, 9, 13), _NOCHE),
    ]

    casamento = resolve_official_event_match(
        evento, official_event_candidates(evento, catalogo), roster=_NOCHE
    )

    assert casamento is not None
    assert casamento.item.event_id == "1300"
    assert casamento.matched_by == "name"


def test_roster_corrobora_o_candidato_unico_quando_a_grafia_diverge() -> None:
    """CA-04: com o nome divergindo, quatro nomes em comum bastam para casar por roster.

    Caso real da janela: a nossa base tem ``Noche UFC`` (nome criado pelo ``gap_sync`` do M6,
    quando o evento ainda estava agendado) e a fonte tem ``Noche UFC: Lopes vs. Silva``. A
    corroboração é o **roster** -- quem lutou naquele card é fato dos dois lados --, nunca a
    forma do rótulo.
    """
    evento = _evento("Noche UFC", date(2025, 9, 13))
    catalogo = [_item("1301", "Noche UFC: Lopes vs. Silva", date(2025, 9, 13), _NOCHE)]

    casamento = resolve_official_event_match(
        evento, official_event_candidates(evento, catalogo), roster=_NOCHE
    )

    assert casamento is not None
    assert casamento.item.event_id == "1301"
    assert casamento.matched_by == "roster"


def test_cobertura_parcial_de_roster_nao_casa() -> None:
    """CA-03: três nomes em comum ficam abaixo do limiar -- o evento permanece em revisão.

    O limiar é uma **barreira**, não uma sugestão: abaixo dele não há corroboração, e id errado
    é pior que id ausente porque o consumidor a jusante escreve canto.
    """
    evento = _evento("Noche UFC", date(2025, 9, 13))
    parcial = frozenset({"diego lopes", "jean silva", "kelvin gastelum"})
    catalogo = [_item("1301", "Noche UFC: Lopes vs. Silva", date(2025, 9, 13), parcial)]

    candidatos = official_event_candidates(evento, catalogo)

    assert len(candidatos) == 1  # havia candidato: o diagnóstico é revisão, não ausência
    assert resolve_official_event_match(evento, candidatos, roster=_NOCHE) is None


def test_dois_candidatos_acima_do_limiar_falham_alto_sem_eleger_o_maior() -> None:
    """CA-02: dois acima do limiar é ambiguidade, **não** competição por tamanho.

    Um candidato tem 5 nomes em comum e o outro 9. A regra não é "vence o de maior interseção":
    escolher o maior seria decidir no escuro com aparência de critério, e a diferença entre 9 e
    5 pode ser só um card mais longo. Falha alto, quem chama conta, nada é escrito.
    """
    roster = frozenset(f"lutador {indice}" for indice in range(12))
    evento = _evento("UFC Fight Night: Ninguem vs Ninguem", date(2024, 5, 10))
    catalogo = [
        _item("900", "Card A", date(2024, 5, 10), frozenset(f"lutador {i}" for i in range(5))),
        _item("901", "Card B", date(2024, 5, 11), frozenset(f"lutador {i}" for i in range(9))),
    ]

    with pytest.raises(AmbiguousOfficialEventMatchError) as excinfo:
        resolve_official_event_match(
            evento, official_event_candidates(evento, catalogo), roster=roster
        )

    # A mensagem nomeia os dois ids -- e o de 9 nomes em comum não é eleito vencedor.
    assert "900" in str(excinfo.value)
    assert "901" in str(excinfo.value)


def test_roster_vazio_nunca_casa_por_intersecao_vazia() -> None:
    """CA-03: evento persistido sem luta não casa com candidato nenhum.

    Interseção vazia não é corroboração; sem essa guarda, um evento sem card persistido casaria
    com qualquer candidato cujo roster também estivesse vazio.
    """
    evento = _evento("Noche UFC", date(2025, 9, 13))
    catalogo = [_item("1301", "Noche UFC: Lopes vs. Silva", date(2025, 9, 13), frozenset())]

    candidatos = official_event_candidates(evento, catalogo)

    assert resolve_official_event_match(evento, candidatos, roster=frozenset()) is None


def test_prefixo_de_nome_com_roster_disjunto_nao_casa() -> None:
    """CA-03: a corroboração é o roster, **nunca** a forma do nome.

    O padrão medido dos eventos não mapeados é prefixo estrito (``Noche UFC`` contra
    ``Noche UFC: Lopes vs. Silva``), e é justamente por isso que duas linhas de ``startswith``
    parecem resolver. Não resolvem: ``UFC 32`` é prefixo de ``UFC 320``, ``UFC 31`` de
    ``UFC 310``, e o custo do engano não é um evento sem id -- é canto gravado com o card do
    evento errado pela Sprint 008-04.
    """
    evento = _evento("UFC 32", date(2025, 10, 4))
    catalogo = [
        _item(
            "1310",
            "UFC 320: Ankalaev vs. Pereira 2",
            date(2025, 10, 4),
            frozenset({"magomed ankalaev", "alex pereira", "merab dvalishvili", "cory sandhagen"}),
        )
    ]

    candidatos = official_event_candidates(evento, catalogo)

    assert len(candidatos) == 1
    assert (
        resolve_official_event_match(
            evento, candidatos, roster=frozenset({"ricco rodriguez", "andrei arlovski"})
        )
        is None
    )


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
    """As duas constantes de 2010 coincidem em VALOR e continuam distintas em PROPÓSITO.

    Desde a Slice 00B da SPEC 009 as duas valem 2010-03-21 -- por **medição convergente**,
    não por unificação: ``OFFICIAL_WINDOW_START`` é a janela de AUTORIDADE DA FONTE (M7,
    SPEC 008) e ``FIRST_RELIABLE_CORNER_DATE`` é o filtro do TREINO (ADR 0006, emendada).
    Uma pode mudar sem a outra.

    Três asserções, cada uma cobrindo uma forma de a separação se perder: identidade
    (alias ou importação cruzada), módulo (redeclaração no lugar errado) e valor (deriva
    silenciosa de qualquer uma das duas). A coincidência de hoje é **resultado**, não
    definição -- é a mesma configuração que produziu o defeito de ``IMAGE_WINDOW_START``
    (duas definições da mesma constante), com a diferença de que aqui a separação é
    intencional e necessária.
    """
    from analysis import dataset as analysis_dataset
    from ingestion import ufc_official

    filtro_do_treino: date = vars(analysis_dataset)["FIRST_RELIABLE_CORNER_DATE"]
    janela_da_fonte: date = vars(ufc_official)["OFFICIAL_WINDOW_START"]

    assert filtro_do_treino is not janela_da_fonte
    assert "OFFICIAL_WINDOW_START" not in vars(analysis_dataset)
    assert "FIRST_RELIABLE_CORNER_DATE" not in vars(ufc_official)
    assert filtro_do_treino == date(2010, 3, 21) == janela_da_fonte
