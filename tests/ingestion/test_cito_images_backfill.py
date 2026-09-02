"""Backfill das URLs de imagem contra o Postgres de teste (M7, Slice 06), em modo fixture.

Cobre CA-05, CA-06, CA-07 e CA-09 do plano 008-06: as URLs caem no ``fighters.id`` certo pelo
roster do evento; nome ambíguo e slug sem correspondência são **contados e pulados** (nunca
casados no escuro, nunca criados por INSERT); reexecutar não produz UPDATE nem gasta quota;
``None`` do payload não apaga URL já persistida; item anterior a 2010-03-21 não é tocado; e a
rede real sem ``--confirmar-gasto-de-quota`` aborta antes da primeira chamada.

O cliente roda sobre ``fixtures/catalog_imagens_derivado/`` -- uma página de catálogo com
``includeBouts`` **recortada** da captura crua já paga em ``.cache/cito-includebouts/``. Todos os
itens, cards, identificadores de luta e URLs são **reais**, inclusive a repetição de lutadores
entre dois eventos (que é um fato da captura, não uma montagem). Duas derivações, ambas
declaradas: a data/slug/título de um item foram deslocados para antes da janela de 2010-03-21
(a captura não tem evento tão antigo) e um canto teve o ``bodyImageUrl`` anulado, para exercitar
a ausência parcial. Nenhum teste toca a rede.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito import images
from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.client import CallBudget, CitoClient
from ingestion.cito.images import ImageBackfillReport, main, run_image_backfill
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START

_FIXTURES = Path(__file__).parent / "fixtures" / "catalog_imagens_derivado"

_SLUG_RECENTE = "ufc-fight-night-august-29-2026"
# Segundo evento REAL da janela, que traz de novo dois lutadores do card recente
# ('umar-nurmagomedov' e 'song-yadong'), em lutas distintas e com as mesmas URLs de perfil.
_SLUG_REPETIDO = "ufc-324"
_SLUG_ANTIGO = "ufc-108"

# Estilos reais da Cito, para asserir a variante certa em cada coluna.
_ESTILO_HEADSHOT = "event_results_athlete_headshot"
_ESTILO_BODY = "athlete_bio_full_body"


def _cliente(budget: CallBudget) -> CitoClient:
    """Cliente em modo fixture sobre a página derivada com card (zero quota real)."""
    return CitoClient(
        token="",
        base_url="https://api.citoapi.com",
        fixture_dir=_FIXTURES,
        budget=budget,
    )


def _semeia_lutador(session: Session, nome: str, **campos: object) -> Fighter:
    """Insere um lutador do seed Kaggle e devolve-o materializado."""
    fighter = Fighter(
        name=nome,
        name_normalized=normalize_name(nome),
        nickname=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
        **campos,
    )
    session.add(fighter)
    session.flush()
    return fighter


def _semeia_evento_com_roster(
    session: Session, *, cito_slug: str, quando: date, lutadores: list[Fighter]
) -> Event:
    """Insere um evento com ``cito_slug`` e uma luta por par de lutadores do roster."""
    evento = Event(name=cito_slug, date=quando, location=None, source="kaggle", cito_slug=cito_slug)
    session.add(evento)
    session.flush()

    for indice in range(0, len(lutadores), 2):
        par = lutadores[indice : indice + 2]
        bout = Bout(event_id=evento.id, method=BoutMethod.DECISION, source="kaggle")
        session.add(bout)
        session.flush()
        for canto, fighter in zip((Corner.RED, Corner.BLUE), par, strict=False):
            session.add(
                BoutFighter(bout_id=bout.id, fighter_id=fighter.id, corner=canto, source="kaggle")
            )
    session.flush()
    return evento


def _semeia_cenario(session: Session) -> dict[str, Fighter]:
    """Semeia o evento recente da fixture com quatro dos seis lutadores do card.

    Os dois que ficam de fora (``kai-asakura`` e ``aoriqileng``) exercitam o caminho de
    ``unmatched``: estão no payload da Cito, mas não no roster persistido.
    """
    lutadores = {
        "umar": _semeia_lutador(session, "Umar Nurmagomedov"),
        "song": _semeia_lutador(session, "Song Yadong"),
        "denise": _semeia_lutador(session, "Denise Gomes"),
        "yan": _semeia_lutador(session, "Xiaonan Yan"),
    }
    _semeia_evento_com_roster(
        session,
        cito_slug=_SLUG_RECENTE,
        quando=date(2026, 8, 29),
        lutadores=list(lutadores.values()),
    )
    return lutadores


# --------------------------------------------------------------------------- #
# CA-05 -- resolução pelo roster do evento
# --------------------------------------------------------------------------- #


def test_urls_caem_no_lutador_certo_do_roster(db_session: Session) -> None:
    """CA-05: cada variante cai na coluna certa do ``fighters.id`` resolvido pelo evento."""
    lutadores = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    run_image_backfill(db_session, _cliente(budget), budget)

    umar = lutadores["umar"]
    assert umar.headshot_url is not None
    assert _ESTILO_HEADSHOT in umar.headshot_url
    assert umar.body_image_url is not None
    assert _ESTILO_BODY in umar.body_image_url
    assert "NURMAGOMEDOV_UMAR" in umar.headshot_url


def test_todos_os_lutadores_do_roster_sao_preenchidos(db_session: Session) -> None:
    """CA-05: os quatro lutadores do roster recebem as duas URLs; o relatório conta a cobertura."""
    lutadores = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert isinstance(relatorio, ImageBackfillReport)
    assert all(fighter.headshot_url is not None for fighter in lutadores.values())
    assert all(fighter.body_image_url is not None for fighter in lutadores.values())
    assert relatorio.fighters_updated == 4
    assert relatorio.headshot_filled == 4
    assert relatorio.body_image_filled == 4


def test_slug_sem_correspondencia_e_contado_e_nao_cria_lutador(db_session: Session) -> None:
    """CA-05: lutador do payload fora do roster entra em ``unmatched`` -- zero escrita, zero INSERT.

    Criar lutador é responsabilidade do ``gap_sync``; este backfill **nunca** faz INSERT.
    """
    _semeia_cenario(db_session)
    antes = db_session.scalar(select(func.count()).select_from(Fighter))
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert set(relatorio.unmatched_slugs) == {"kai-asakura", "aoriqileng"}
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == antes


def test_nome_ambiguo_no_evento_nao_escreve_nada(db_session: Session) -> None:
    """CA-05: nome que casa com mais de um lutador do roster é pulado e contado.

    Nunca casar no escuro: escolher um dos dois gravaria a imagem no lutador errado, e o erro
    só apareceria quando alguém olhasse a página do homônimo.
    """
    lutadores = _semeia_cenario(db_session)
    homonimo = _semeia_lutador(db_session, "Umar Nurmagomedov")
    evento = db_session.scalars(select(Event).where(Event.cito_slug == _SLUG_RECENTE)).one()
    bout = Bout(event_id=evento.id, method=BoutMethod.DECISION, source="kaggle")
    db_session.add(bout)
    db_session.flush()
    db_session.add(
        BoutFighter(bout_id=bout.id, fighter_id=homonimo.id, corner=Corner.RED, source="kaggle")
    )
    db_session.flush()
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert "umar-nurmagomedov" in relatorio.ambiguous_slugs
    assert lutadores["umar"].headshot_url is None
    assert homonimo.headshot_url is None


def test_item_sem_evento_persistido_e_reportado(db_session: Session) -> None:
    """CA-05: item de catálogo sem ``Event`` de mesmo ``cito_slug`` é reportado, nunca casado."""
    _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert "ufc-330" in relatorio.events_without_match


def test_promocao_fora_do_escopo_nao_entra_no_backfill(db_session: Session) -> None:
    """CA-05: DWCS é descartado antes do casamento (só UFC nesta fase)."""
    _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert "dwcs-10-3" not in relatorio.events_without_match
    assert relatorio.catalog_items == 4  # os cinco itens menos o DWCS


# --------------------------------------------------------------------------- #
# CA-06 -- idempotência e ausência que não apaga
# --------------------------------------------------------------------------- #


def test_rerun_nao_atualiza_nada_nem_gasta_quota(db_session: Session, tmp_path: Path) -> None:
    """CA-06: a segunda execução devolve ``updated=0`` e não cobra o ``CallBudget``.

    O cache em disco das páginas de catálogo serve a segunda passada, então o custo em quota é
    zero -- é a diferença entre um backfill resumível e um que re-gasta o free tier a cada
    tentativa.
    """
    _semeia_cenario(db_session)
    cache = CatalogPageCache(tmp_path)

    primeiro = CallBudget(limit=10)
    run_image_backfill(db_session, _cliente(primeiro), primeiro, cache=cache)
    contagem = db_session.scalar(select(func.count()).select_from(Fighter))

    segundo = CallBudget(limit=10)
    relatorio = run_image_backfill(db_session, _cliente(segundo), segundo, cache=cache)

    assert primeiro.used == 1
    assert segundo.used == 0  # cache hit: nenhuma chamada nova
    assert relatorio.fighters_updated == 0
    assert relatorio.fighters_unchanged == 4
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == contagem


def test_ausencia_no_payload_nao_apaga_url_persistida(db_session: Session) -> None:
    """CA-06: ``None`` do payload nunca sobrescreve valor já persistido.

    O canto ``kai-asakura`` da fixture vem sem ``bodyImageUrl``; um lutador que já tenha a URL
    de corpo inteiro precisa mantê-la, e receber só o retrato que faltava.
    """
    asakura = _semeia_lutador(
        db_session, "Kai Asakura", body_image_url="https://exemplo/corpo-ja-persistido.png"
    )
    aoriqileng = _semeia_lutador(db_session, "Aoriqileng")
    _semeia_evento_com_roster(
        db_session,
        cito_slug=_SLUG_RECENTE,
        quando=date(2026, 8, 29),
        lutadores=[asakura, aoriqileng],
    )
    budget = CallBudget(limit=10)

    run_image_backfill(db_session, _cliente(budget), budget)

    assert asakura.body_image_url == "https://exemplo/corpo-ja-persistido.png"
    assert asakura.headshot_url is not None
    assert _ESTILO_HEADSHOT in asakura.headshot_url


def test_backfill_nao_altera_o_source_da_linha(db_session: Session) -> None:
    """CA-06: ``source`` é a origem da LINHA -- backfill da Cito sobre linha do Kaggle mantém.

    Regra travada no CLAUDE.md: não compor valores (``"kaggle+cito"``), porque consumidores
    reais comparam ``source`` por igualdade.
    """
    lutadores = _semeia_cenario(db_session)
    budget = CallBudget(limit=10)

    run_image_backfill(db_session, _cliente(budget), budget)

    assert all(fighter.source == "kaggle" for fighter in lutadores.values())


# --------------------------------------------------------------------------- #
# CA-07 -- janela de 2010-03-21
# --------------------------------------------------------------------------- #


def test_evento_anterior_a_janela_nao_recebe_escrita(db_session: Session) -> None:
    """CA-07: item com ``local_date`` anterior a 2010-03-21 é descartado antes de qualquer escrita.

    O evento existe na base, tem ``cito_slug`` casando com o item e o card traz as duas URLs --
    ainda assim nada é gravado, porque a janela da SPEC precede tudo.
    """
    garry = _semeia_lutador(db_session, "Ian Machado Garry")
    makhachev = _semeia_lutador(db_session, "Islam Makhachev")
    _semeia_evento_com_roster(
        db_session,
        cito_slug=_SLUG_ANTIGO,
        quando=date(2010, 1, 2),
        lutadores=[garry, makhachev],
    )
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert garry.headshot_url is None
    assert makhachev.headshot_url is None
    assert relatorio.items_out_of_window == 1
    assert relatorio.fighters_updated == 0


def test_janela_do_backfill_e_a_constante_unica_da_spec() -> None:
    """CA-07: a janela usada aqui é a própria ``OFFICIAL_WINDOW_START``, importada e não copiada.

    Duas asserções com dentes, cada uma cobrindo uma forma de a janela se partir em duas:

    - a **identidade** (``is``) falha se alguém voltar a declarar o literal dentro de
      ``ingestion.cito.images`` -- dois ``date(2010, 3, 21)`` são objetos distintos, então uma
      redeclaração de mesmo valor não passa;
    - a comparação com a data documentada (ADR 0006) falha se a constante única **mudar de
      valor**, que é exatamente o que a versão anterior deste teste não percebia: ela afirmava
      um literal contra uma cópia local, e ficava verde com a fonte da verdade em outra data.

    O nome do módulo é lido por ``vars()`` e não como atributo porque ``mypy --strict``
    (``no_implicit_reexport``) recusa ler um nome **importado** como atributo do módulo que o
    importou -- a recusa é, ela própria, a confirmação de que ali há importação e não declaração.
    """
    janela_do_modulo: date = vars(images)["OFFICIAL_WINDOW_START"]

    assert janela_do_modulo is OFFICIAL_WINDOW_START
    assert date(2010, 3, 21) == OFFICIAL_WINDOW_START


# --------------------------------------------------------------------------- #
# CA-09 -- gate humano antes da rede real
# --------------------------------------------------------------------------- #


def test_main_rede_real_sem_confirmacao_nao_dispara_nenhuma_chamada(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-09: o CLI em rede real sem ``--confirmar-gasto-de-quota`` aborta antes de qualquer fetch.

    Um spy sobre ``CitoClient.fetch_event_catalog`` prova ZERO chamadas: o gate bloqueia antes
    mesmo de instanciar o cliente ou abrir a sessão. O comando encerra com exit code != 0.
    """
    chamadas: list[str] = []

    def _spy(self: CitoClient, **kwargs: object) -> list[object]:
        chamadas.append("catalogo")
        raise AssertionError("a rede não deveria ser tocada sem o gate confirmado")

    monkeypatch.setattr(CitoClient, "fetch_event_catalog", _spy)

    with (
        caplog.at_level(logging.ERROR, logger="ingestion.cito.images"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--call-budget", "5"])

    assert excinfo.value.code != 0
    assert chamadas == []
    assert "gate" in caplog.text.lower()


def test_lutador_em_dois_eventos_da_janela_e_escrito_uma_vez_so(db_session: Session) -> None:
    """CA-06: o mesmo lutador em dois eventos não gera segunda escrita nem oscila de valor.

    Medido na captura crua já paga: dos 624 lutadores com retrato, **nenhum** tem URL diferente
    entre eventos -- o ``profile`` é do atleta, não do card. Este teste fixa o comportamento
    correspondente: a segunda ocorrência do mesmo lutador entra em ``fighters_unchanged``, e o
    valor persistido é o mesmo.
    """
    umar = _semeia_lutador(db_session, "Umar Nurmagomedov")
    song = _semeia_lutador(db_session, "Song Yadong")
    _semeia_evento_com_roster(
        db_session, cito_slug=_SLUG_RECENTE, quando=date(2026, 8, 29), lutadores=[umar, song]
    )
    _semeia_evento_com_roster(
        db_session, cito_slug=_SLUG_REPETIDO, quando=date(2026, 1, 24), lutadores=[umar, song]
    )
    budget = CallBudget(limit=10)

    relatorio = run_image_backfill(db_session, _cliente(budget), budget)

    assert relatorio.fighters_updated == 2  # uma escrita por lutador, não por aparição
    assert relatorio.fighters_unchanged == 2  # a segunda aparição não muda nada
    assert umar.headshot_url is not None
    assert "NURMAGOMEDOV_UMAR" in umar.headshot_url
