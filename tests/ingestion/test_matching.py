"""Testes do matching persisted-driven de evento Cito <-> bout persistido -- Slice 04.

Cobrem, sem gastar quota real: a derivação do slug Cito a partir do ``name`` do evento
persistido (``event_cito_slug``); a normalização de um ``fighter_slug`` da Cito
(``_slug_to_normalized_name``); o contrato do ``MatchReport`` (cobertura); e, contra o
Postgres de teste (sessão transacional com rollback), a resolução de ``bout_fighter_id`` por
**nome normalizado** escopada ao evento persistido (``resolve_bout_fighter_ids``), incluindo
os caminhos de não-casamento (reportado, não levanta) e de ambiguidade (nome casando com >1
``bout_fighter`` do evento -> ``AmbiguousBoutFighterMatchError``).

Decisão arquitetural (2026-07-13): matching **persisted-driven** -- o evento já persistido é a
âncora (data + roster do seed) e o ``resolve_bout_fighter_ids`` casa cada linha de
``event_stats.bout_stats`` ao ``bout_fighter`` persistido por nome normalizado, sem janela de
data contra a Cito (o ``fetch_event_stats`` não entrega data).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.dto import CitoEventStats
from ingestion.cito.matching import (
    AmbiguousBoutFighterMatchError,
    BoutFighterMatchError,
    MatchReport,
    UnsupportedEventSlugError,
    _slug_to_normalized_name,
    event_cito_slug,
    resolve_bout_fighter_ids,
)
from ingestion.normalize import normalize_name


def _event(name: str = "UFC 319: Du Plessis vs. Chimaev") -> Event:
    """Constrói um ``Event`` não persistido (só o ``name`` importa para o slug)."""
    return Event(name=name, date=date(2025, 8, 16), location=None, source="kaggle")


def _seed_fighter(session: Session, name: str) -> int:
    """Insere um ``Fighter`` mínimo (do seed Kaggle) e devolve o id materializado."""
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


def _seed_ufc319(
    session: Session,
    *,
    red_name: str = "Dricus du Plessis",
    blue_name: str = "Khamzat Chimaev",
) -> tuple[Event, dict[str, int]]:
    """Semeia o evento UFC 319 com uma luta e os dois cantos; devolve o evento e os bf ids.

    Os nomes normalizam para as chaves que os ``fighter_slug`` da fixture (``dricus-du-plessis``
    / ``khamzat-chimaev``) produzem, reproduzindo o matching persisted-driven por nome.
    """
    event = Event(
        name="UFC 319: Du Plessis vs. Chimaev",
        date=date(2025, 8, 16),
        location=None,
        source="kaggle",
    )
    session.add(event)
    session.flush()

    red_id = _seed_fighter(session, red_name)
    blue_id = _seed_fighter(session, blue_name)

    bout = Bout(
        event_id=event.id,
        winner_id=blue_id,
        method=BoutMethod.SUBMISSION,
        round=3,
        ending_time_seconds=None,
        weight_class="Middleweight",
        source="kaggle",
    )
    session.add(bout)
    session.flush()

    red_bf = BoutFighter(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED, source="kaggle")
    blue_bf = BoutFighter(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE, source="kaggle")
    session.add_all([red_bf, blue_bf])
    session.flush()

    return event, {"red": red_bf.id, "blue": blue_bf.id}


def _fixture_event_stats() -> CitoEventStats:
    """Carrega a fixture ``event_stats_ufc-319.json`` como DTO tipado, sem tocar rede/quota."""
    from pathlib import Path

    from ingestion.cito.client import CitoClient

    fixtures = Path(__file__).parent / "fixtures"
    client = CitoClient(token="", base_url="https://api.citoapi.com", fixture_dir=fixtures)
    return client.fetch_event_stats("ufc-319")


def test_event_cito_slug_deriva_identificador_numerado() -> None:
    """CA-01: o slug Cito vem do ``name`` persistido ('UFC 319: ...' -> 'ufc-319')."""
    assert event_cito_slug(_event()) == "ufc-319"


def test_event_cito_slug_nome_nao_numerado_levanta() -> None:
    """CA-04: um nome fora do formato numerado não deriva slug em silêncio -- levanta claro."""
    with pytest.raises(ValueError, match="UFC"):
        event_cito_slug(_event(name="UFC Fight Night: Silva vs. Costa"))


def test_unsupported_event_slug_error_e_value_error() -> None:
    """Contrato: ``UnsupportedEventSlugError`` é um ``ValueError`` (preserva o contrato anterior).

    ``event_cito_slug`` já levantava ``ValueError`` para nomes fora do formato numerado; o erro
    tipado especializa esse contrato sem quebrar quem captura ``ValueError`` (ex.:
    ``_find_event_by_cito_slug``).
    """
    assert issubclass(UnsupportedEventSlugError, ValueError)


@pytest.mark.parametrize(
    "name",
    [
        "UFC Fight Night: Silva vs. Costa",
        "UFC on ESPN: Whittaker vs. Costa",
        "UFC on ABC: Emmett vs. Topuria",
    ],
)
def test_event_cito_slug_formato_nao_numerado_levanta_unsupported(name: str) -> None:
    """CA-04/05: formato não-numerado não deriva slug em silêncio -> ``UnsupportedEventSlugError``.

    A única convenção de slug Cito confirmada por dado real (fixtures ``event_stats_ufc-<n>.json``)
    é 'ufc-<n>', derivada do prefixo numerado; derivar um slug para esses formatos sem dado da Cito
    seria chutar e arriscar casar o evento errado (invariante "sem heurística silenciosa"). O
    backfill da Slice 05 captura este erro tipado e pula o evento com aviso.
    """
    with pytest.raises(UnsupportedEventSlugError):
        event_cito_slug(_event(name=name))


def test_event_cito_slug_numerado_continua_derivando() -> None:
    """Regressão: o formato numerado 'UFC <n>' segue derivando 'ufc-<n>'."""
    assert event_cito_slug(_event(name="UFC 300: Pereira vs. Hill")) == "ufc-300"


def test_slug_to_normalized_name_reusa_normalize_name() -> None:
    """CA-02: o ``fighter_slug`` da Cito normaliza pela mesma ``normalize_name`` do M0/M1."""
    assert _slug_to_normalized_name("dricus-du-plessis") == normalize_name("Dricus du Plessis")
    assert _slug_to_normalized_name("khamzat-chimaev") == normalize_name("Khamzat Chimaev")


def test_match_report_cobertura_total() -> None:
    """CA-02: cobertura 100% quando todos os cantos casam (nenhum slug sem correspondência)."""
    report = MatchReport(event_id=1, matched=2, unmatched_slugs=())
    assert report.total == 2
    assert report.coverage == 1.0


def test_match_report_cobertura_parcial_e_vazia() -> None:
    """CA-02: cobertura fracionária com não-casados; evento sem linhas -> cobertura 0.0."""
    parcial = MatchReport(event_id=1, matched=1, unmatched_slugs=("ghost-fighter",))
    assert parcial.total == 2
    assert parcial.coverage == 0.5

    vazio = MatchReport(event_id=1, matched=0, unmatched_slugs=())
    assert vazio.total == 0
    assert vazio.coverage == 0.0


def test_ambiguous_error_e_subtipo_de_bout_fighter_match_error() -> None:
    """CA-03: a ambiguidade é um ``BoutFighterMatchError`` (espelha o padrão do M1)."""
    assert issubclass(AmbiguousBoutFighterMatchError, BoutFighterMatchError)


def test_resolve_bout_fighter_ids_casa_por_nome_normalizado(db_session: Session) -> None:
    """CA-02: cada linha ``(bout_id, corner)`` casa ao ``bout_fighter`` do evento por nome."""
    event, bf_ids = _seed_ufc319(db_session)
    stats = _fixture_event_stats()

    resolved = resolve_bout_fighter_ids(db_session, event, stats)

    assert resolved == {
        ("ufc-319-bout-1", Corner.RED): bf_ids["red"],
        ("ufc-319-bout-1", Corner.BLUE): bf_ids["blue"],
    }


def test_resolve_bout_fighter_ids_escopa_ao_evento(db_session: Session) -> None:
    """CA-02: só os ``bout_fighters`` do evento âncora entram -- outro evento não interfere."""
    # Um evento estranho com um lutador homônimo do canto vermelho, que NÃO deve casar.
    outro = Event(name="UFC 300: Outro", date=date(2024, 4, 13), location=None, source="kaggle")
    db_session.add(outro)
    db_session.flush()
    intruso_id = _seed_fighter(db_session, "Dricus du Plessis")
    outro_bout = Bout(
        event_id=outro.id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    db_session.add(outro_bout)
    db_session.flush()
    db_session.add(
        BoutFighter(
            bout_id=outro_bout.id, fighter_id=intruso_id, corner=Corner.RED, source="kaggle"
        )
    )
    db_session.flush()

    event, bf_ids = _seed_ufc319(db_session)
    stats = _fixture_event_stats()

    resolved = resolve_bout_fighter_ids(db_session, event, stats)

    assert resolved[("ufc-319-bout-1", Corner.RED)] == bf_ids["red"]
    assert intruso_id not in resolved.values()


def test_resolve_bout_fighter_ids_slug_sem_correspondencia_nao_levanta(
    db_session: Session,
) -> None:
    """CA-02: ``fighter_slug`` sem ``bout_fighter`` casado é reportado (não entra), sem levantar."""
    # O canto azul persistido tem outro nome -> o slug 'khamzat-chimaev' fica sem correspondência.
    event, bf_ids = _seed_ufc319(db_session, blue_name="Outro Lutador")
    stats = _fixture_event_stats()

    resolved = resolve_bout_fighter_ids(db_session, event, stats)

    assert resolved == {("ufc-319-bout-1", Corner.RED): bf_ids["red"]}


def test_resolve_bout_fighter_ids_nome_ambiguo_levanta(db_session: Session) -> None:
    """CA-03: nome casando com >1 ``bout_fighter`` do evento -> ``AmbiguousBoutFighterMatchError``.

    Nunca duplica, mescla ou escolhe arbitrariamente (invariante do CLAUDE.md, espelha o M1).
    """
    # Ambos os cantos normalizam para 'dricus du plessis' -> o slug vermelho fica ambíguo.
    event, _ = _seed_ufc319(db_session, blue_name="Dricus Du Plessis")
    stats = _fixture_event_stats()

    with pytest.raises(AmbiguousBoutFighterMatchError):
        resolve_bout_fighter_ids(db_session, event, stats)
