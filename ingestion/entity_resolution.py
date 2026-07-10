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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TypedDict

from apps.fighters.enums import Stance
from ingestion.normalize import normalize_name

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
