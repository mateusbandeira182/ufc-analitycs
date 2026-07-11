"""Testes de integração da resolução cross-source de lutadores -- CA-01..CA-06.

Contra o Postgres de teste (sessão transacional com rollback ao final): um lutador já
semeado do Kaggle é reconciliado ao ``fighter_id`` existente sem duplicar e preservando o
``source`` original; um lutador ausente é inserido com ``source="cito"``; o rerun é no-op;
homônimo com DOB divergente vira lutador novo; e a ambiguidade levanta erro em vez de
duplicar/mesclar. Cobre também o orquestrador ``resolve_event_fighters`` (mapa
``slug -> fighter_id``, com economia de quota: um ``get_fighter`` por slug único).
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.cito.client import CitoClient
from ingestion.cito.dto import CitoBout, CitoCorner, CitoEvent, CitoFighter
from ingestion.entity_resolution import AmbiguousFighterMatchError
from ingestion.incremental import resolve_event_fighters, resolve_or_create_fighter


def _seed_fighter(session: Session, name: str, dob: date | None, source: str = "kaggle") -> Fighter:
    from ingestion.normalize import normalize_name

    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=dob,
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=0,
        losses=0,
        draws=0,
        source=source,
    )
    session.add(fighter)
    session.flush()
    return fighter


def _count_fighters(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Fighter)) or 0


def test_reconcilia_fighter_do_seed_preservando_source(db_session: Session) -> None:
    """CA-01: lutador do seed (source='kaggle') é reconciliado; contagem estável; source intacto."""
    seeded = _seed_fighter(db_session, "Alexander Volkanovski", date(1988, 9, 29))

    resolved_id = resolve_or_create_fighter(
        db_session,
        CitoFighter(
            slug="alexander-volkanovski",
            name="Alexander Volkanovski",
            date_of_birth=date(1988, 9, 29),
            wins=27,
            losses=4,
            draws=0,
        ),
    )

    assert resolved_id == seeded.id
    assert _count_fighters(db_session) == 1
    db_session.refresh(seeded)
    assert seeded.source == "kaggle"  # o registro reaproveitado não é tocado


def test_insere_fighter_novo_com_source_cito(db_session: Session) -> None:
    """CA-02: lutador ausente é inserido com source='cito', mapeando bio e cartel."""
    new_id = resolve_or_create_fighter(
        db_session,
        CitoFighter(
            slug="ilia-topuria",
            name="Ilia Topuria",
            date_of_birth=date(1997, 1, 21),
            nickname="El Matador",
            height_cm=170,
            reach_cm=175,
            stance=Stance.ORTHODOX,
            wins=16,
            losses=0,
            draws=0,
        ),
    )

    fighter = db_session.get(Fighter, new_id)
    assert fighter is not None
    assert fighter.source == "cito"
    assert fighter.name_normalized == "ilia topuria"
    assert fighter.date_of_birth == date(1997, 1, 21)
    assert fighter.nickname == "El Matador"
    assert fighter.stance is Stance.ORTHODOX
    assert (fighter.wins, fighter.losses, fighter.draws) == (16, 0, 0)


def test_resolucao_e_idempotente_no_rerun(db_session: Session) -> None:
    """CA-03: resolver o mesmo lutador novo duas vezes não insere na segunda (no-op)."""
    candidate = CitoFighter(
        slug="ilia-topuria", name="Ilia Topuria", date_of_birth=date(1997, 1, 21)
    )

    first_id = resolve_or_create_fighter(db_session, candidate)
    second_id = resolve_or_create_fighter(db_session, candidate)

    assert first_id == second_id
    assert _count_fighters(db_session) == 1


def test_homonimo_com_dob_divergente_cria_novo(db_session: Session) -> None:
    """CA-04: homônimo com DOB conhecida e divergente não é fundido -- vira lutador novo."""
    _seed_fighter(db_session, "Bruno Silva", date(1989, 7, 13))

    new_id = resolve_or_create_fighter(
        db_session,
        CitoFighter(slug="bruno-silva-2", name="Bruno Silva", date_of_birth=date(1990, 3, 16)),
    )

    fighter = db_session.get(Fighter, new_id)
    assert fighter is not None
    assert fighter.source == "cito"
    assert _count_fighters(db_session) == 2


def test_ambiguidade_levanta_erro_sem_inserir(db_session: Session) -> None:
    """CA-06: candidato sem DOB casando com >1 existente levanta erro e nada é inserido."""
    _seed_fighter(db_session, "Bruno Silva", date(1989, 7, 13))
    _seed_fighter(db_session, "Bruno Silva", date(1990, 3, 16))

    with pytest.raises(AmbiguousFighterMatchError):
        resolve_or_create_fighter(
            db_session,
            CitoFighter(slug="bruno-silva", name="Bruno Silva", date_of_birth=None),
        )

    assert _count_fighters(db_session) == 2  # nenhuma duplicata criada


def _event_two_bouts_sharing_a_corner() -> CitoEvent:
    """Evento em que o slug ``fighter-a`` aparece em duas lutas (para provar a dedupe por slug)."""
    return CitoEvent(
        event_id="ufc-test",
        name="UFC Test",
        date=date(2025, 1, 1),
        bouts=[
            CitoBout(
                bout_id="b1",
                corners=(
                    CitoCorner(slug="fighter-a", name="Fighter A"),
                    CitoCorner(slug="fighter-b", name="Fighter B"),
                ),
            ),
            CitoBout(
                bout_id="b2",
                corners=(
                    CitoCorner(slug="fighter-a", name="Fighter A"),
                    CitoCorner(slug="fighter-c", name="Fighter C"),
                ),
            ),
        ],
    )


def test_resolve_event_fighters_mapeia_slugs_e_economiza_quota(db_session: Session) -> None:
    """CA-02 + RNF quota: mapa slug->id cobre todos os cantos; um get_fighter por slug único."""
    calls: dict[str, int] = {}
    profiles = {
        "fighter-a": {"slug": "fighter-a", "name": "Fighter A", "date_of_birth": "1990-01-01"},
        "fighter-b": {"slug": "fighter-b", "name": "Fighter B", "date_of_birth": "1991-01-01"},
        "fighter-c": {"slug": "fighter-c", "name": "Fighter C", "date_of_birth": "1992-01-01"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.rsplit("/", 1)[-1]
        calls[slug] = calls.get(slug, 0) + 1
        return httpx.Response(200, json=profiles[slug])

    client = CitoClient(
        token="token-fake",
        base_url="https://mmaapi.dev",
        transport=httpx.MockTransport(handler),
    )

    fighter_ids = resolve_event_fighters(db_session, _event_two_bouts_sharing_a_corner(), client)

    assert set(fighter_ids) == {"fighter-a", "fighter-b", "fighter-c"}
    assert _count_fighters(db_session) == 3
    # ``fighter-a`` aparece em duas lutas, mas o perfil é buscado uma única vez (economia de quota).
    assert calls == {"fighter-a": 1, "fighter-b": 1, "fighter-c": 1}
