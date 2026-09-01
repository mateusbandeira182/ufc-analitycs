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
# Faixa plausível de peso de lutador, em kg. O teto acomoda outliers reais do UFC antigo
# (Emmanuel Yarborough, 349,27 kg); o piso barra zero, negativo e ruído de parsing.
_WEIGHT_RANGE_KG = (40.0, 400.0)
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
    weight: str
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
    weight_kg: float | None
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


def _parse_weight_kg(value: str) -> float | None:
    """Peso em quilos (o dataset já publica kg com decimais) -> ``float`` ou ``None``.

    Não há conversão de libras a fazer: o ``fighter_details.csv`` traz o limite da divisão
    já convertido (``70.31`` = 155 lb, ``83.91`` = 185 lb). A faixa plausível vai até
    ``_WEIGHT_RANGE_KG`` para acomodar outliers reais do UFC antigo (Emmanuel Yarborough,
    349,27 kg) -- validação que descarta dado verdadeiro é pior que validação nenhuma.
    Ausente, não-numérico ou fora da faixa degrada para ``None`` com log: nunca zero,
    nunca sentinela.
    """
    stripped = value.strip()
    if not stripped:
        return None
    try:
        weight = float(stripped)
    except ValueError:
        logger.warning("Peso não-numérico %r no CSV; gravado como nulo", value)
        return None

    minimum, maximum = _WEIGHT_RANGE_KG
    if not minimum <= weight <= maximum:
        logger.warning("Peso %.2f kg fora da faixa plausível; gravado como nulo", weight)
        return None
    return weight


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
        weight_kg=_parse_weight_kg(row["weight"]),
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


# --- Desempate por idade (M6, Slice 06) -----------------------------------------------
#
# O perfil real da Cito (``GET /api/v1/ufc/fighters/{slug}``) devolve ``birthDate`` nulo e
# publica ``age`` -- medido na sondagem de 2026-09-01. Sem a data, o desempate original do
# RF-13 fica sem o dado do lado da fonte, e o homônimo travaria a ingestão do card inteiro.
#
# A idade calculada a partir do ``date_of_birth`` persistido é o **mesmo atributo de
# identidade**, só em granularidade mais grossa -- diferente de cartel ou divisão, que mudam
# com a carreira e seriam heurística. Decisão do humano em 2026-09-01.


def _age_at(date_of_birth: date, today: date) -> int:
    """Idade em anos completos na data ``today`` (a subtração de aniversário, sem biblioteca)."""
    aniversario_passou = (today.month, today.day) >= (date_of_birth.month, date_of_birth.day)
    return today.year - date_of_birth.year - (0 if aniversario_passou else 1)


def match_fighter_id_by_age(
    name: str,
    age: int | None,
    existing: Sequence[ExistingFighter],
    *,
    today: date,
) -> int | None:
    """Desempata homônimos pela idade informada no perfil; sem unicidade -> ``None``.

    Considera apenas os já persistidos de mesmo nome normalizado **com** ``date_of_birth``:
    sem a data não há idade a calcular, e estimá-la seria inventar o dado que o desempate
    existe para conferir. O casamento é **exato**: idade calculada igual à informada, e nada
    além disso.

    Não há tolerância de um ano, e a ausência dela é deliberada. Ela existia para cobrir a
    hipótese de um perfil servido de cache anterior ao último aniversário -- especulativa, que
    nenhum caso observado exigiu. Em compensação, o único caso real em que ela disparava
    produzia um casamento **errado**: o `bruno-silva-blindado` da sondagem de 2026-09-01 informa
    ``age: 35`` contra homônimos de 36 e 37 anos, e a tolerância o casaria com o de 36 -- o
    peso-mosca "Bulldog" (163 cm, cartel 14-7-2) --, quando o perfil descreve o peso-médio
    "Blindado". Uma regra cujo único uso concreto é produzir erro não se conserta com mais
    condições: remove-se. **Não reintroduzir sem um caso real de perfil defasado.**

    Devolve o id **apenas** quando exatamente um candidato tem a idade informada. Zero
    candidatos, mais de um, ou ``age`` ausente devolvem ``None``, e quem chama trata isso como
    ambiguidade irresolvível: nunca escolher o "mais provável" (mesma política de
    ``match_fighter_id``). Um evento a menos, com o motivo registrado, é melhor que um evento a
    mais com dois lutadores fundidos.

    A ``today`` entra por parâmetro (determinismo no teste; ``date.today()`` é proibido pela
    regra DTZ011 do ruff).
    """
    if age is None:
        return None

    normalized = normalize_name(name)
    matches = [
        fighter
        for fighter in existing
        if fighter.name_normalized == normalized
        and fighter.date_of_birth is not None
        and _age_at(fighter.date_of_birth, today) == age
    ]
    if len(matches) != 1:
        logger.info(
            "Desempate por idade de %r inconclusivo: %d candidato(s) com %d anos.",
            name,
            len(matches),
            age,
        )
        return None
    logger.info("Desempate por idade de %r resolvido ao id %d", name, matches[0].id)
    return matches[0].id
