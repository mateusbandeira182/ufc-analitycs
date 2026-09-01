"""Testes do matching de evento Cito <-> bout persistido -- Slice 04 (revisado pela ADR 0005).

Cobrem, sem gastar quota real: a normalização de um ``fighter_slug`` da Cito
(``_slug_to_normalized_name``); o contrato do ``MatchReport`` (cobertura); a guarda de que a
derivação de slug por regex não existe mais; e, contra o Postgres de teste (sessão transacional
com rollback), a resolução de ``bout_fighter_id`` por **nome normalizado** escopada ao evento
persistido (``resolve_bout_fighter_ids``), incluindo os caminhos de não-casamento (reportado,
não levanta) e de ambiguidade (nome casando com >1 ``bout_fighter`` do evento ->
``AmbiguousBoutFighterMatchError``).

O evento já persistido segue sendo a âncora (data + roster do seed), mas o identificador Cito
vem do **catálogo** (``Event.cito_slug``, Sprint 007-03), nunca de uma regra sobre o nome. A
chave de saída é ``(cito_bout_id, fighter_slug)``: a API real não traz ``corner`` na linha de
stat, e chavear pelo canto colidiria nos dois cantos da mesma luta. Ver ADR 0005.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito import matching
from ingestion.cito.dto import CitoEventStats
from ingestion.cito.matching import (
    AmbiguousBoutFighterMatchError,
    BoutFighterMatchError,
    MatchReport,
    _slug_to_normalized_name,
    resolve_bout_fighter_ids,
)
from ingestion.normalize import normalize_name


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


# Identificador REAL da luta principal de UFC 319 na Cito (recorte verbatim da captura).
_BOUT_ID = "12cedec11b37ddc0"


def _seed_ufc319(
    session: Session,
    *,
    red_name: str = "Khamzat Chimaev",
    blue_name: str = "Dricus Du Plessis",
) -> tuple[Event, dict[str, int]]:
    """Semeia o evento UFC 319 com uma luta e os dois cantos; devolve o evento e os bf ids.

    Os nomes normalizam para as chaves que os ``fighter_slug`` da fixture (``khamzat-chimaev``
    / ``dricus-du-plessis``) produzem, reproduzindo o matching persisted-driven por nome. Os
    cantos são os da captura real: Chimaev, o vencedor, é o vermelho.
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


@pytest.mark.parametrize(
    "removed", ["event_cito_slug", "_EVENT_SLUG_PATTERN", "UnsupportedEventSlugError"]
)
def test_simbolos_de_slug_por_regex_nao_existem_mais(removed: str) -> None:
    """CA-05: nenhum slug Cito derivado por regex -- o catálogo é a fonte do identificador.

    A derivação do M5 acertava o slug de 2 dos 745 eventos persistidos (UFC 100 e UFC 319); a
    Cito usa a forma longa ('ufc-309-jones-vs-miocic') na quase totalidade do catálogo. Manter
    o símbolo disponível convidaria alguém a reintroduzir a heurística. Ver ADR 0005.
    """
    assert not hasattr(matching, removed)


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
    """CA-03: cada linha ``(bout_id, fighter_slug)`` casa ao ``bout_fighter`` do evento por nome."""
    event, bf_ids = _seed_ufc319(db_session)
    stats = _fixture_event_stats()

    resolved = resolve_bout_fighter_ids(db_session, event, stats)

    assert resolved == {
        (_BOUT_ID, "khamzat-chimaev"): bf_ids["red"],
        (_BOUT_ID, "dricus-du-plessis"): bf_ids["blue"],
    }


def test_resolve_bout_fighter_ids_cantos_da_mesma_luta_nao_colidem(db_session: Session) -> None:
    """CA-03: os dois cantos da mesma luta resolvem para ``bout_fighter`` distintos.

    A garantia que a chave antiga perderia. Como a API real não traz ``corner`` na linha de stat,
    manter o canto na chave produziria ``(bout_id, None)`` para os **dois** cantos -- uma colisão
    silenciosa que gravaria o round no ``bout_fighter`` errado. Por isso a chave é o slug.
    """
    event, bf_ids = _seed_ufc319(db_session)
    stats = _fixture_event_stats()

    resolved = resolve_bout_fighter_ids(db_session, event, stats)

    assert len(resolved) == 2
    assert len(set(resolved.values())) == 2
    assert set(resolved.values()) == {bf_ids["red"], bf_ids["blue"]}


def test_resolve_bout_fighter_ids_escopa_ao_evento(db_session: Session) -> None:
    """CA-02: só os ``bout_fighters`` do evento âncora entram -- outro evento não interfere."""
    # Um evento estranho com um lutador homônimo do canto vermelho, que NÃO deve casar.
    outro = Event(name="UFC 300: Outro", date=date(2024, 4, 13), location=None, source="kaggle")
    db_session.add(outro)
    db_session.flush()
    intruso_id = _seed_fighter(db_session, "Khamzat Chimaev")
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

    assert resolved[(_BOUT_ID, "khamzat-chimaev")] == bf_ids["red"]
    assert intruso_id not in resolved.values()


def test_resolve_bout_fighter_ids_slug_sem_correspondencia_nao_levanta(
    db_session: Session,
) -> None:
    """CA-02: ``fighter_slug`` sem ``bout_fighter`` casado é reportado (não entra), sem levantar."""
    # O canto azul persistido tem outro nome -> 'dricus-du-plessis' fica sem correspondência.
    event, bf_ids = _seed_ufc319(db_session, blue_name="Outro Lutador")
    stats = _fixture_event_stats()

    resolved = resolve_bout_fighter_ids(db_session, event, stats)

    assert resolved == {(_BOUT_ID, "khamzat-chimaev"): bf_ids["red"]}


def test_resolve_bout_fighter_ids_nome_ambiguo_levanta(db_session: Session) -> None:
    """CA-03: nome casando com >1 ``bout_fighter`` do evento -> ``AmbiguousBoutFighterMatchError``.

    Nunca duplica, mescla ou escolhe arbitrariamente (invariante do CLAUDE.md, espelha o M1).
    """
    # Ambos os cantos normalizam para 'khamzat chimaev' -> o slug vermelho fica ambíguo.
    event, _ = _seed_ufc319(db_session, blue_name="Khamzat Chimaev")
    stats = _fixture_event_stats()

    with pytest.raises(AmbiguousBoutFighterMatchError):
        resolve_bout_fighter_ids(db_session, event, stats)
