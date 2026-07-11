"""Entity resolution de fighters: borda tipada do CSV -> domínio + dedup.

O ``fighter_details.csv`` da fonte-seed (ADR 0002) traz uma linha por lutador, mas
o mesmo lutador ainda pode aparecer mais de uma vez (grafias diferentes do nome) e
há homônimos reais (ex.: dois ``Bruno Silva`` com datas de nascimento distintas).
A dedup usa a chave natural ``(name_normalized, date_of_birth)``: variações do mesmo
nome com a mesma DOB colapsam; homônimos com DOB distinta permanecem separados; sem
DOB, o desempate degrada para o nome normalizado.

Esta é a fronteira dinâmica do Pandas: cada linha crua (strings) é tipada em
``FighterRow`` e convertida em ``ResolvedFighter`` antes de propagar para a carga --
nenhum ``Any`` do ``DataFrame`` entra no domínio.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TypedDict

from apps.fighters.enums import Stance
from ingestion.normalize import normalize_name

logger = logging.getLogger(__name__)

_DOB_FORMAT = "%b %d, %Y"
_STANCE_BY_LABEL = {
    "orthodox": Stance.ORTHODOX,
    "southpaw": Stance.SOUTHPAW,
    "switch": Stance.SWITCH,
}


class FighterRow(TypedDict):
    """Linha crua de ``fighter_details.csv`` (todos os campos como texto)."""

    name: str
    nick_name: str
    dob: str
    height: str
    reach: str
    stance: str
    wins: str
    losses: str
    draws: str


@dataclass(frozen=True)
class ResolvedFighter:
    """Lutador já tipado e deduplicado, pronto para a carga em ``fighters``."""

    name: str
    name_normalized: str
    nickname: str | None
    date_of_birth: date | None
    height_cm: int | None
    reach_cm: int | None
    stance: Stance | None
    wins: int
    losses: int
    draws: int


def _parse_optional_text(value: str) -> str | None:
    """Texto opcional: vazio (após trim) vira ``None``."""
    stripped = value.strip()
    return stripped or None


def _parse_measurement_cm(value: str) -> int | None:
    """Medida em centímetros (o dataset já traz cm com decimais) -> inteiro ou ``None``."""
    stripped = value.strip()
    if not stripped:
        return None
    return round(float(stripped))


def _parse_dob(value: str) -> date | None:
    """Data de nascimento no formato ``"May 08, 1982"`` -> ``date`` de calendário ou ``None``.

    É uma data de calendário (sem instante/timezone); por isso ``strptime`` sem ``%z``.
    """
    stripped = value.strip()
    if not stripped:
        return None
    return datetime.strptime(stripped, _DOB_FORMAT).date()  # noqa: DTZ007  # data de nascimento, sem timezone


def _parse_stance(value: str) -> Stance | None:
    """Mapeia o rótulo de stance ao enum; fora de orthodox/southpaw/switch -> ``None``."""
    return _STANCE_BY_LABEL.get(value.strip().casefold())


def _to_resolved(row: FighterRow) -> ResolvedFighter:
    """Converte uma linha crua tipada em um ``ResolvedFighter`` do domínio."""
    name = row["name"].strip()
    return ResolvedFighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=_parse_optional_text(row["nick_name"]),
        date_of_birth=_parse_dob(row["dob"]),
        height_cm=_parse_measurement_cm(row["height"]),
        reach_cm=_parse_measurement_cm(row["reach"]),
        stance=_parse_stance(row["stance"]),
        wins=int(row["wins"]),
        losses=int(row["losses"]),
        draws=int(row["draws"]),
    )


def resolve_fighters(rows: Iterable[FighterRow]) -> list[ResolvedFighter]:
    """Deduplica lutadores por ``(name_normalized, date_of_birth)``.

    Preserva a ordem de aparição e a primeira ocorrência de cada chave (first wins).
    """
    seen: dict[tuple[str, object], ResolvedFighter] = {}
    for row in rows:
        fighter = _to_resolved(row)
        key = (fighter.name_normalized, fighter.date_of_birth)
        if key not in seen:
            seen[key] = fighter
    return list(seen.values())


# --- Resolução cross-source (Kaggle x Cito) -------------------------------------------
#
# Enquanto ``resolve_fighters`` deduplica dentro de uma fonte (o seed), a resolução
# cross-source reconcilia um lutador vindo da Cito contra os lutadores **já persistidos**
# (tipicamente do seed Kaggle). Mesma chave load-bearing -- nome normalizado + data de
# nascimento como desempate --, mas aqui a ambiguidade **falha alto**
# (``AmbiguousFighterMatchError``) em vez de colapsar: nunca duplicar nem mesclar em
# silêncio (invariante do CLAUDE.md; precedente de ``seed_bouts.build_fighter_index``,
# que pula o nome ambíguo em vez de chutar um id).


class AmbiguousFighterMatchError(Exception):
    """Nome casa com >1 fighter sem desempate por DOB -- nunca duplicar/mesclar em silêncio."""


@dataclass(frozen=True)
class FighterCandidate:
    """Lutador vindo da Cito a reconciliar: só o nome e a DOB entram no matching."""

    name: str
    date_of_birth: date | None


@dataclass(frozen=True)
class ExistingFighter:
    """Lutador já persistido (chave de matching materializada da ``Session``)."""

    id: int
    name_normalized: str
    date_of_birth: date | None


def match_fighter_id(
    candidate: FighterCandidate, existing: Sequence[ExistingFighter]
) -> int | None:
    """Reconcilia ``candidate`` contra os lutadores já persistidos.

    Retorna o ``fighter_id`` existente (mesma pessoa) ou ``None`` (lutador novo). Levanta
    ``AmbiguousFighterMatchError`` quando o nome normalizado casa com mais de um fighter e
    a DOB não desempata para exatamente um -- nunca funde nem duplica em silêncio.

    Política (Decisões em aberto 3 e 4 da SPEC 004):

    - Sem candidato de mesmo nome normalizado -> ``None`` (novo).
    - DOB conhecida: match exato por DOB único -> id; nenhum exato mas há existente com DOB
      desconhecida (indescartável) -> ambíguo; nenhum exato e todos com DOB conhecida e
      diferente -> ``None`` (homônimo real).
    - DOB ausente: exatamente um existente daquele nome -> id (match sem DOB, logado);
      mais de um -> ambíguo.
    """
    normalized = normalize_name(candidate.name)
    same_name = [fighter for fighter in existing if fighter.name_normalized == normalized]
    if not same_name:
        return None

    if candidate.date_of_birth is not None:
        exact = [f for f in same_name if f.date_of_birth == candidate.date_of_birth]
        if len(exact) == 1:
            return exact[0].id
        if len(exact) > 1:
            raise AmbiguousFighterMatchError(
                f"Nome {candidate.name!r} casa com {len(exact)} fighters de mesma DOB "
                f"({candidate.date_of_birth}); resolução ambígua."
            )
        if any(f.date_of_birth is None for f in same_name):
            raise AmbiguousFighterMatchError(
                f"Nome {candidate.name!r} casa com fighter(s) sem DOB indescartável(is); "
                "resolução ambígua com o candidato com DOB conhecida."
            )
        return None

    if len(same_name) == 1:
        logger.info(
            "Match sem DOB para %r: único fighter existente de mesmo nome (id=%d)",
            candidate.name,
            same_name[0].id,
        )
        return same_name[0].id

    raise AmbiguousFighterMatchError(
        f"Candidato {candidate.name!r} sem DOB casa com {len(same_name)} fighters "
        "de mesmo nome; resolução ambígua."
    )
