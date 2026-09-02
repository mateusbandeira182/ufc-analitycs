"""Testes da escrita do canto autoritativo em ``bout_fighters`` (Sprint 008-04).

Cobrem CA-05 a CA-09 do Plano 008-04. O que estes testes guardam, acima de tudo, é a ordem que
o RF-02 impõe: ``apply_official_corners`` **só** aceita uma ``CornerComparison`` já produzida --
ela não varre a janela e não busca payload --, e o modo default do comando não escreve.

As três invariantes que a escrita não pode violar:

1. só ``bout_fighters.corner`` muda;
2. ``bout_fighters.source`` **não** muda (``source`` é a origem da LINHA, não do campo);
3. ``bouts.winner_id`` **nunca** é tocado (o vencedor é um ``fighter_id``; trocar o rótulo de
   canto muda em qual lado ele cai, nunca quem ele é).

O cenário e as fixtures são os mesmos da comparação, importados de
``test_ufc_official_corner_comparacao`` para que a semeadura tenha uma definição só.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import Corner
from apps.bouts.models import Bout, BoutFighter
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.corner import (
    CornerComparison,
    _parse_args,
    apply_official_corners,
    compare_event_corners,
    run_corner_command,
    run_corner_comparison,
)
from tests.ingestion.test_ufc_official_corner_comparacao import (
    _NOMES,
    _PIMBLETT_X_GORDON,
    _TILL_X_DU_PLESSIS,
    _UFC_282,
    _cantos_persistidos,
    _card_oficial,
    _semeia_evento,
    _semeia_luta,
    _semeia_lutador,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"


def _cenario_invertido(session: Session, *, source: str = "kaggle") -> tuple[Bout, int, int]:
    """UFC 282 com Till x Du Plessis persistido com os cantos **invertidos** face à fonte.

    Devolve ``(luta, fighter_id do Till, fighter_id do Du Plessis)``. Na fonte Till é o
    vermelho; aqui ele entra no azul, então a luta diverge nos dois cantos.
    """
    _, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(session)
    till = _semeia_lutador(session, _NOMES[vermelho], vermelho)
    du_plessis = _semeia_lutador(session, _NOMES[azul], azul)
    luta = _semeia_luta(session, evento, du_plessis, till, winner=du_plessis, source=source)
    return luta, till.id, du_plessis.id


def _comparacao(session: Session) -> CornerComparison:
    """Comparação da janela inteira em modo offline (fixtures como cache, sem rede)."""
    return run_corner_comparison(session, None, OfficialEventCache(_FIXTURES))


# --------------------------------------------------------------------------- #
# CA-05 / CA-06 -- a escrita adota o canto da fonte, e só ele
# --------------------------------------------------------------------------- #


def test_aplicacao_adota_o_canto_da_fonte_nos_dois_cantos_da_luta(db_session: Session) -> None:
    """CA-05/CA-06: os dois cantos passam a ser os da fonte, e o resultado diz quanto escreveu.

    A divergência vem em par, então a luta conta **uma** vez em ``bouts_updated`` e **duas** em
    ``rows_updated`` -- os dois números respondem perguntas diferentes.
    """
    luta, till_id, du_plessis_id = _cenario_invertido(db_session)

    resultado = apply_official_corners(db_session, _comparacao(db_session))

    assert resultado.bouts_updated == 1
    assert resultado.rows_updated == 2
    assert resultado.skipped_out_of_window == ()
    assert _cantos_persistidos(db_session, luta.id) == {
        till_id: Corner.RED,
        du_plessis_id: Corner.BLUE,
    }


def test_source_da_linha_semeada_do_kaggle_permanece_kaggle(db_session: Session) -> None:
    """CA-05: ``source`` é a origem da LINHA, não do campo -- corrigir o canto não a compõe.

    Decisão do humano em 2026-09-01, registrada no CLAUDE.md: nada de ``"kaggle+cito"``, porque
    ``source`` é comparado por igualdade por consumidores reais.
    """
    luta, _, _ = _cenario_invertido(db_session)

    apply_official_corners(db_session, _comparacao(db_session))

    linhas = db_session.scalars(select(BoutFighter).where(BoutFighter.bout_id == luta.id))
    assert {linha.source for linha in linhas} == {"kaggle"}


def test_winner_id_da_luta_nao_e_alterado(db_session: Session) -> None:
    """CA-05: o vencedor é um ``fighter_id``; trocar o canto muda o lado dele, nunca quem é."""
    luta, _, du_plessis_id = _cenario_invertido(db_session)

    apply_official_corners(db_session, _comparacao(db_session))

    db_session.refresh(luta)
    assert luta.winner_id == du_plessis_id


def test_luta_nao_coberta_pela_fonte_nao_e_escrita(db_session: Session) -> None:
    """CA-05: sem chave de casamento não há divergência, e sem divergência não há escrita."""
    _, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(db_session)
    till = _semeia_lutador(db_session, _NOMES[vermelho], None)  # sem identificador externo
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, du_plessis, till)

    comparacao = compare_event_corners(db_session, evento, _card_oficial(_TILL_X_DU_PLESSIS[0]))
    resultado = apply_official_corners(db_session, comparacao)

    assert comparacao.uncovered_bouts == (luta.id,)
    assert resultado.rows_updated == 0
    assert _cantos_persistidos(db_session, luta.id) == {
        du_plessis.id: Corner.RED,
        till.id: Corner.BLUE,
    }


# --------------------------------------------------------------------------- #
# CA-07 -- idempotência: reexecutar não altera contagem nem valores
# --------------------------------------------------------------------------- #


def test_reaplicacao_apos_recomparar_nao_escreve_nada(db_session: Session) -> None:
    """CA-06/CA-07: aplicada a correção, a comparação volta a zero e a segunda escrita é vazia.

    É a forma verificável do CA-04 da SPEC: zero divergências onde a fonte cobre, medido pelo
    mesmo caminho que produziu a lista.
    """
    luta, till_id, du_plessis_id = _cenario_invertido(db_session)

    primeira = apply_official_corners(db_session, _comparacao(db_session))
    db_session.flush()
    recomparacao = _comparacao(db_session)
    segunda = apply_official_corners(db_session, recomparacao)

    assert primeira.rows_updated == 2
    assert recomparacao.divergences == ()
    assert recomparacao.agreeing_bouts == 1
    assert segunda.bouts_updated == 0
    assert segunda.rows_updated == 0
    assert _cantos_persistidos(db_session, luta.id) == {
        till_id: Corner.RED,
        du_plessis_id: Corner.BLUE,
    }


def test_reaplicar_a_mesma_comparacao_e_um_no_op_observavel(db_session: Session) -> None:
    """CA-07: rodar a escrita duas vezes com a MESMA comparação não reescreve nada.

    O caso é o do operador que reexecuta o comando com o relatório antigo em mãos. Reemitir o
    mesmo valor deixaria a idempotência invisível: o resultado diz explicitamente que as linhas
    já estavam no canto da fonte.
    """
    luta, till_id, du_plessis_id = _cenario_invertido(db_session)
    comparacao = _comparacao(db_session)

    apply_official_corners(db_session, comparacao)
    db_session.flush()
    segunda = apply_official_corners(db_session, comparacao)

    assert segunda.rows_updated == 0
    assert segunda.rows_already_official == 2
    assert _cantos_persistidos(db_session, luta.id) == {
        till_id: Corner.RED,
        du_plessis_id: Corner.BLUE,
    }


# --------------------------------------------------------------------------- #
# CA-08 -- a trava de janela existe NO ESCRITOR, não só no leitor
# --------------------------------------------------------------------------- #


def test_escritor_recusa_divergencia_de_evento_anterior_a_fronteira(db_session: Session) -> None:
    """CA-08: divergência anterior a 2010-03-21 é recusada e contada, mesmo já comparada.

    A comparação aqui é obtida com a janela **alargada** -- é exatamente o cenário que a trava
    do leitor não cobre. Quem decide o que pode ser escrito é a constante
    ``OFFICIAL_WINDOW_START``, consultada de novo dentro do escritor: uma comparação alargada
    não pode alargar a autoridade da escrita junto.
    """
    _, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(
        db_session,
        "Evento anterior à fronteira",
        OFFICIAL_WINDOW_START - timedelta(days=1),
        _UFC_282[2],
    )
    till = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, du_plessis, till)

    alargada = run_corner_comparison(
        db_session, None, OfficialEventCache(_FIXTURES), window_start=date(2000, 1, 1)
    )
    resultado = apply_official_corners(db_session, alargada)

    assert len(alargada.divergences) == 2  # o leitor alargado enxergou a luta
    assert resultado.rows_updated == 0
    assert resultado.bouts_updated == 0
    assert resultado.skipped_out_of_window == (luta.id, luta.id)
    assert _cantos_persistidos(db_session, luta.id) == {
        du_plessis.id: Corner.RED,
        till.id: Corner.BLUE,
    }


# --------------------------------------------------------------------------- #
# CA-09 -- dentro da janela e onde a fonte cobre, o fallback não é a última palavra
# --------------------------------------------------------------------------- #


def test_canto_do_fallback_deterministico_nao_sobrevive_onde_a_fonte_cobre(
    db_session: Session,
) -> None:
    """CA-09: o canto que a ordem alfabética produziria é substituído pelo da fonte.

    O par é real e escolhido porque as duas regras **discordam**: pelo fallback determinístico
    (menor nome normalizado vira vermelho) ``jared gordon`` iria para o vermelho; a fonte
    oficial põe Paddy Pimblett lá. O fallback acerta 39,29% -- pior que cara-ou-coroa, porque
    ordenação por nome não tem relação causal com o canto.

    A aposentadoria é de **precedência**, não de código: ``assign_deterministic_corners``
    continua existindo e continua sendo o último recurso onde a fonte não cobre.
    """
    fight_id, vermelho, azul = _PIMBLETT_X_GORDON
    evento = _semeia_evento(db_session)
    pimblett = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    gordon = _semeia_lutador(db_session, _NOMES[azul], azul)
    # Canto como o fallback determinístico o produziria: 'jared gordon' < 'paddy pimblett'.
    ordem_do_fallback = sorted(
        (pimblett, gordon), key=lambda lutador: (lutador.name_normalized, lutador.id)
    )
    luta = _semeia_luta(db_session, evento, *ordem_do_fallback)
    assert _cantos_persistidos(db_session, luta.id)[gordon.id] is Corner.RED

    comparacao = compare_event_corners(db_session, evento, _card_oficial(fight_id))
    apply_official_corners(db_session, comparacao)

    assert _cantos_persistidos(db_session, luta.id) == {
        pimblett.id: Corner.RED,
        gordon.id: Corner.BLUE,
    }


# --------------------------------------------------------------------------- #
# CA-01 / CA-05 -- o comando: relatório por padrão, escrita só com --aplicar
# --------------------------------------------------------------------------- #


def test_modo_default_do_comando_compara_e_nao_escreve(db_session: Session) -> None:
    """CA-01: sem ``--aplicar`` o comando só compara e reporta -- nenhuma linha é tocada.

    É o RF-02 traduzido em estrutura: a escrita em massa é fisicamente posterior ao relatório,
    e não depende de o operador lembrar de olhar antes.
    """
    luta, till_id, du_plessis_id = _cenario_invertido(db_session)

    comparacao, resultado = run_corner_command(
        db_session, None, OfficialEventCache(_FIXTURES), apply=False
    )

    assert len(comparacao.divergences) == 2
    assert resultado is None
    assert _cantos_persistidos(db_session, luta.id) == {
        du_plessis_id: Corner.RED,
        till_id: Corner.BLUE,
    }


def test_comando_com_aplicar_compara_e_escreve(db_session: Session) -> None:
    """CA-05: com ``--aplicar`` o comando roda a comparação e, na sequência, a escrita."""
    luta, till_id, du_plessis_id = _cenario_invertido(db_session)

    comparacao, resultado = run_corner_command(
        db_session, None, OfficialEventCache(_FIXTURES), apply=True
    )

    assert len(comparacao.divergences) == 2
    assert resultado is not None
    assert resultado.rows_updated == 2
    assert _cantos_persistidos(db_session, luta.id) == {
        till_id: Corner.RED,
        du_plessis_id: Corner.BLUE,
    }


def test_parse_args_nao_aplica_por_padrao_e_expoe_o_modo_offline() -> None:
    """A flag de escrita é opt-in, e a janela **não** é exposta na linha de comando.

    **Sem gate de quota**: ``--confirmar-gasto-de-quota`` é da Cito. A fonte oficial é gratuita
    e não autenticada, e acrescentar o gate aqui seria cerimônia sem custo a proteger.
    """
    padrao = _parse_args([])
    assert padrao.aplicar is False
    assert padrao.cache_dir == Path(".cache") / "ufc_official"
    assert padrao.fixture_dir is None
    assert not hasattr(padrao, "window_start")

    assert _parse_args(["--aplicar"]).aplicar is True
    assert _parse_args(["--fixture-dir", str(_FIXTURES)]).fixture_dir == _FIXTURES
