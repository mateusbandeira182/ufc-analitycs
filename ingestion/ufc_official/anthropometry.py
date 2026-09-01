"""Antropometria da fonte oficial: preenche o que está nulo, nunca sobrescreve (Slice 05).

Fecha os buracos de ``reach_cm``, ``height_cm``, ``date_of_birth``, ``stance`` e ``weight_kg``
dos lutadores da janela (2010-03-21 em diante) a **custo zero de quota** -- a fonte oficial é
gratuita, ao contrário da Cito. Depois disso ``GET /api/v1/fighters/{id}`` passa a devolver os
atributos sem nenhuma mudança em ``apps/``: as cinco colunas já existem e o ``FighterOut`` já as
expõe desde o M5.

A unidade é convertida AQUI, e só aqui
--------------------------------------
O DTO da Sprint 008-01 guarda a unidade da fonte no nome do campo (``height_inches``,
``reach_inches``, ``weight_lbs``) e não converte nada: converter na borda esconderia a unidade
de origem, que foi exatamente como a Cito devolveu polegadas onde o DTO esperava centímetros
sem ninguém notar até a SPEC 007. A conversão acontece neste módulo, uma única vez, e o
resultado já é o domínio (cm, kg).

A tolerância existe porque arredondar duas vezes não fecha
-----------------------------------------------------------
Comparar o nosso centímetro com o da fonte exige folga: as duas linhagens partiram da mesma
polegada e arredondaram em momentos diferentes. Medido na SPEC: 369 de 371 "diferenças" eram só
arredondamento. Daí ``TOLERANCE_CM``. A folga é apertada de propósito -- 180 cm lidos como 180
polegadas viram 457 cm, e é a tolerância apertada que faz um erro de unidade aparecer como
divergência massiva em vez de passar por dado ruim da fonte.

O que este módulo NUNCA faz
---------------------------
- **Não sobrescreve valor já preenchido** (RF-04). Divergência acima da tolerância é contada e
  nomeada no log; a escrita não acontece.
- **Não inventa valor.** Ausência na fonte permanece nula -- nunca zero, nunca sentinela. Vale
  em especial para o alcance, que a fonte só tem para ~59,7% dos lutadores.
- **Não casa por nome.** A chave é ``fighters.ufc_fighter_id`` (Sprint 008-03); lutador sem id
  é pulado e contado. Cair para o nome reintroduziria a ambiguidade de homônimo que a 008-03
  existe para eliminar (RF-07).
- **Não muda ``source``.** A linha semeada do Kaggle continua ``source="kaggle"`` depois de
  receber antropometria da fonte oficial: ``source`` é a origem da **linha**, não de cada campo
  (decisão do humano em 2026-09-01, registrada no CLAUDE.md). Nunca ``"kaggle+ufc"``.
- **Não insere.** Só ``UPDATE``; perfil da fonte sem lutador correspondente é contado.
- **Não tem gate de quota.** O ``--confirmar-gasto-de-quota`` protege o free tier da Cito; aqui
  não há quota a proteger, e a ausência do gate é deliberada.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.dto import (
    UfcOfficialError,
    UfcOfficialFighter,
    is_absent_event_payload,
    parse_event,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Diretório default do cache em disco por ``eventId`` -- o mesmo das Slices 02 e 03, de
# propósito: o payload de evento que a descoberta já baixou traz a antropometria dos cantos, e
# um cache já materializado torna esta execução offline.
_DEFAULT_CACHE_DIR = Path(".cache") / "ufc_official"

# A fonte publica altura e alcance em POLEGADAS e peso em LIBRAS (medido, SPEC 008).
_CM_PER_INCH: Final = 2.54
# Libra avoirdupois internacional -- fator EXATO por definição, não aproximação.
_KG_PER_POUND: Final = 0.45359237
# Casas decimais do peso persistido: o ``fighter_details.csv`` do Kaggle publica o limite da
# divisão já em quilos com duas casas (185 lb -> 83.91). Converter com a mesma precisão faz o
# peso da fonte oficial COINCIDIR com o nosso em vez de divergir por ruído de ponto flutuante.
_KG_DECIMALS: Final = 2

# Medido na SPEC: 369 de 371 "diferenças" de centímetro eram arredondamento de polegada. Uma
# polegada inteira vale 2,54 cm, então 2 cm é a maior folga que ainda não engole uma polegada.
TOLERANCE_CM: Final = 2
# DERIVADO, não medido (a SPEC mediu só os ±2 cm): 1 lb = 0,4536 kg, logo qualquer diferença
# abaixo de meia libra convertida é arredondamento. Na prática a folga é generosa por duas
# ordens de grandeza -- as duas fontes publicam o limite da divisão em libras e convertem pelo
# mesmo fator exato, então o resíduo aritmético fica abaixo de 0,005 kg. Divergência de peso
# concentrada logo acima deste limiar é sinal de que a constante precisa ser MEDIDA, não de que
# a fonte esteja errada.
TOLERANCE_KG: Final = 0.5

# Rótulos de base (guarda) que a fonte publica e que o nosso enum representa.
_STANCE_BY_LABEL: Final[dict[str, Stance]] = {
    "orthodox": Stance.ORTHODOX,
    "southpaw": Stance.SOUTHPAW,
    "switch": Stance.SWITCH,
}


def inches_to_cm(value: float | None) -> int | None:
    """Polegadas -> centímetros inteiros; ausente -> ``None`` (nunca zero, nunca sentinela).

    Aceita meia polegada (``73.5``, o alcance real de Darren Till na captura verbatim): o
    arredondamento acontece uma única vez, aqui, e não na borda -- arredondar na borda perderia
    a meia polegada antes de ela virar centímetro.
    """
    return None if value is None else round(value * _CM_PER_INCH)


def pounds_to_kg(value: float | None) -> float | None:
    """Libras -> quilos com duas casas; ausente -> ``None`` (nunca zero, nunca sentinela).

    As duas casas são a convenção já persistida: o Kaggle gravou ``83.91`` para os 185 lb dos
    médios, e converter com a mesma precisão faz os dois lados coincidirem em vez de divergirem
    na oitava casa decimal.
    """
    return None if value is None else round(value * _KG_PER_POUND, _KG_DECIMALS)


def is_divergent_cm(*, existing: int | None, from_source: int | None) -> bool:
    """``True`` só quando ambos existem e diferem além de ``TOLERANCE_CM``.

    Ausência de qualquer um dos lados **não** é divergência: é ausência, e ausência não
    discorda de nada.
    """
    if existing is None or from_source is None:
        return False
    return abs(existing - from_source) > TOLERANCE_CM


def is_divergent_kg(*, existing: float | None, from_source: float | None) -> bool:
    """``True`` só quando ambos existem e diferem além de ``TOLERANCE_KG``."""
    if existing is None or from_source is None:
        return False
    return abs(existing - from_source) > TOLERANCE_KG


class UnknownStanceError(ValueError):
    """Rótulo de base fora de ``orthodox``/``southpaw``/``switch`` -- falha alto (RF-10).

    Deliberadamente **diferente** de ``ingestion.entity_resolution._parse_stance``, que degrada
    para ``None`` num rótulo desconhecido do CSV: ali o dado é um snapshot congelado, aqui é uma
    fonte viva que pode passar a publicar um rótulo novo. Degradar em silêncio gravaria "base
    desconhecida" como "sem base" e ninguém saberia.

    Quem chama decide o que fazer com a falha. O backfill a captura por lutador, conta e segue:
    o rótulo novo de um lutador não custa a antropometria dos outros.
    """


def parse_stance(value: str | None) -> Stance | None:
    """Rótulo da fonte -> enum; ausente -> ``None``; desconhecido -> ``UnknownStanceError``."""
    if value is None:
        return None
    stance = _STANCE_BY_LABEL.get(value.strip().casefold())
    if stance is None:
        raise UnknownStanceError(
            f"Base desconhecida na fonte oficial: {value!r}; esperado um de "
            f"{sorted(_STANCE_BY_LABEL)}."
        )
    return stance


@dataclass(frozen=True)
class OfficialAnthropometry:
    """Antropometria de um lutador na fonte oficial, já no domínio (cm, kg, ``date``, enum).

    Nada aqui está na unidade da fonte: quem constrói é ``build_anthropometry``, e depois deste
    ponto polegada e libra não existem mais. ``ufc_fighter_id`` é texto porque é assim que
    ``fighters`` guarda identificador externo -- opaco, nunca em aritmética.

    ``name`` não é escrito em lugar nenhum: existe para o relatório nomear quem diverge. Contar
    divergências sem dizer de quem obrigaria a uma segunda consulta para agir sobre elas.
    """

    ufc_fighter_id: str
    name: str
    height_cm: int | None
    reach_cm: int | None
    weight_kg: float | None
    date_of_birth: date | None
    stance: Stance | None


def build_anthropometry(fighter: UfcOfficialFighter) -> OfficialAnthropometry:
    """Projeta um canto do DTO na antropometria do domínio. Função **pura**.

    Levanta ``UnknownStanceError`` num rótulo de base não mapeado (RF-10) -- quem chama decide
    se pula o lutador ou aborta.
    """
    return OfficialAnthropometry(
        ufc_fighter_id=str(fighter.fighter_id),
        name=f"{fighter.name.first_name} {fighter.name.last_name}".strip(),
        height_cm=inches_to_cm(fighter.height_inches),
        reach_cm=inches_to_cm(fighter.reach_inches),
        weight_kg=pounds_to_kg(fighter.weight_lbs),
        date_of_birth=fighter.date_of_birth,
        stance=parse_stance(fighter.stance),
    )


# --------------------------------------------------------------------------- #
# Coleta: lê os cards dos eventos da janela e projeta os cantos. NÃO escreve nada.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnthropometryCollection:
    """O que a coleta viu, sem nada escrito.

    ``unknown_stance`` guarda ``(ufc_fighter_id, nome, rótulo)`` -- o rótulo cru inclusive, que
    é o dado que decide se vale estender o enum ``Stance`` ou se foi ruído da fonte.
    """

    profiles: tuple[OfficialAnthropometry, ...]
    unknown_stance: tuple[tuple[str, str, str], ...]
    events_without_payload: tuple[tuple[int, str], ...]


def _fetch_payload(client: UfcOfficialClient | None, event_id: int) -> object | None:
    """Payload cru de um id; ``None`` quando o id não existe ou a busca falha.

    ``client=None`` é o **modo offline**: nada é buscado e a coleta enxerga só o cache.

    Terceira ocorrência desta forma no pacote (as outras estão em ``discovery`` e em
    ``fighter_ids``), o que finalmente satisfaz a rule of three e justifica a extração para um
    lugar comum. Ela **não** foi feita aqui de propósito: as Sprints 008-04 e 008-06 estão
    editando esses mesmos módulos em paralelo, e uma refatoração de três arquivos alheios no
    meio disso trocaria duplicação barata por conflito caro. Fica registrada como follow-up no
    relatório da sprint.
    """
    if client is None:
        return None
    try:
        payload = client.fetch_event_payload(event_id)
    except UfcOfficialError as exc:
        logger.warning("Evento %d pulado (falha transitória na fonte): %s", event_id, exc)
        return None
    return None if is_absent_event_payload(payload) else payload


def _window_events_with_official_id(session: Session) -> list[Event]:
    """Eventos da janela que a Slice 02 mapeou -- os únicos cujos cards podem ser lidos.

    O filtro por ``Event.date >= OFFICIAL_WINDOW_START`` é **explícito**, além da exigência de
    ``ufc_event_id``. As duas condições são redundantes hoje (a Slice 02 só preencheu a janela),
    e a redundância é deliberada: CA-08 é invariante de SPEC e não pode depender do acerto de
    outra sprint.
    """
    statement = (
        select(Event)
        .where(Event.date >= OFFICIAL_WINDOW_START, Event.ufc_event_id.is_not(None))
        .order_by(Event.id)
    )
    return list(session.scalars(statement))


def _window_fighters_by_identifier(session: Session) -> dict[str, Fighter]:
    """Lutadores com ao menos uma luta na janela **e** ``ufc_fighter_id``, indexados pelo id.

    É a chave de casamento inteira desta slice. Quem não tem id não entra -- e cair para o nome
    reintroduziria a ambiguidade de homônimo que a Sprint 008-03 existe para eliminar (RF-07).
    """
    statement = (
        select(Fighter)
        .join(BoutFighter, BoutFighter.fighter_id == Fighter.id)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Event, Event.id == Bout.event_id)
        .where(Event.date >= OFFICIAL_WINDOW_START, Fighter.ufc_fighter_id.is_not(None))
        .distinct()
    )
    # O ``select`` garante que o id não é nulo; o ``str`` existe para o mypy --strict.
    return {str(fighter.ufc_fighter_id): fighter for fighter in session.scalars(statement)}


def _count_window_fighters_without_identifier(session: Session) -> int:
    """Lutadores da janela que a Slice 03 não conseguiu identificar -- teto da cobertura."""
    statement = (
        select(Fighter.id)
        .join(BoutFighter, BoutFighter.fighter_id == Fighter.id)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Event, Event.id == Bout.event_id)
        .where(Event.date >= OFFICIAL_WINDOW_START, Fighter.ufc_fighter_id.is_(None))
        .distinct()
    )
    return len(list(session.scalars(statement)))


def collect_anthropometry(
    session: Session, client: UfcOfficialClient | None, cache: OfficialEventCache
) -> AnthropometryCollection:
    """Antropometria dos lutadores da janela, já no domínio. **Não escreve nada.**

    Persisted-driven (mesmo padrão de ``fighter_ids`` e de ``cito.backfill_rounds``): percorre
    os eventos da janela que têm ``ufc_event_id`` e projeta apenas os cantos cujo
    ``FighterId`` pertence a um lutador nosso da janela. Filtrar aqui, e não depois, mantém o
    relatório legível -- um card traz 24 cantos e quase nenhum é de quem estamos preenchendo.

    Custa **uma requisição por evento**, e nenhuma quando o cache já está materializado.
    """
    conhecidos = set(_window_fighters_by_identifier(session))
    perfis: list[OfficialAnthropometry] = []
    desconhecidos: list[tuple[str, str, str]] = []
    sem_payload: list[tuple[int, str]] = []

    for event in _window_events_with_official_id(session):
        # O ``select`` garante que não é nulo; o ``assert`` existe para o mypy --strict.
        assert event.ufc_event_id is not None  # noqa: S101
        ufc_event_id = int(event.ufc_event_id)
        payload, _ = cache.get_or_fetch(
            ufc_event_id, lambda id_alvo: _fetch_payload(client, id_alvo)
        )
        if payload is None:
            sem_payload.append((event.id, event.name))
            continue
        for fight in parse_event(payload, event_id=ufc_event_id).fight_card:
            for canto in fight.fighters:
                if str(canto.fighter_id) not in conhecidos:
                    continue
                perfis.append(_project(canto, desconhecidos))

    return AnthropometryCollection(
        profiles=tuple(perfis),
        unknown_stance=tuple(dict.fromkeys(desconhecidos)),
        events_without_payload=tuple(sem_payload),
    )


def _project(
    canto: UfcOfficialFighter, desconhecidos: list[tuple[str, str, str]]
) -> OfficialAnthropometry:
    """Projeta um canto; rótulo de base não mapeado zera **só a base**, e é registrado.

    A falha alta de ``parse_stance`` é o comportamento certo do parser (RF-10). O que este
    nível decide é o alcance do estrago: derrubar a execução inteira por causa de um lutador
    seria demais, e descartar o lutador inteiro custaria quatro atributos bons por causa de um
    rótulo ruim -- numa slice cujo propósito é justamente fechar buracos. A base fica nula,
    contada e nomeada; nunca adivinhada.

    Medido em 2026-09-01 sobre os 1.354 payloads do cache: ``Open Stance`` em 7 lutadores e
    ``Sideways`` em 3, todos de cartel antigo. Na janela persistida sobram dois (Nate Quarry e
    Krzysztof Soszynski), então o custo de descartá-los seria pequeno -- e ainda assim
    injustificado.
    """
    try:
        return build_anthropometry(canto)
    except UnknownStanceError as exc:
        nome = f"{canto.name.first_name} {canto.name.last_name}".strip()
        desconhecidos.append((str(canto.fighter_id), nome, str(canto.stance)))
        logger.warning(
            "Lutador %r (FighterId %d): %s A base dele permanece NULA; os demais atributos "
            "seguem normalmente.",
            nome,
            canto.fighter_id,
            exc,
        )
        return build_anthropometry(canto.model_copy(update={"stance": None}))


# --------------------------------------------------------------------------- #
# Escrita: só UPDATE, só onde o nosso está nulo.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AnthropometryDivergence:
    """Um campo já preenchido cujo valor difere do da fonte além da tolerância.

    Guarda o **nome** do lutador, não só o id: contar divergências sem dizer de quem obrigaria
    a uma segunda consulta antes de qualquer inspeção (RF-04).
    """

    ufc_fighter_id: str
    name: str
    field: str
    existing: str
    from_source: str


@dataclass(frozen=True)
class AnthropometryBackfillResult:
    """Resumo observável do backfill.

    Os contadores são separados de propósito, porque pedem correções diferentes:

    - ``absent_in_source``: o buraco continua aberto porque a fonte também não tem o dado;
    - ``skipped_without_id``: lutador da janela que a Slice 03 não identificou;
    - ``out_of_window``: perfil de lutador persistido cujas lutas são todas anteriores a
      2010-03-21 -- não escrito por decisão (RF-03), não por falta de dado;
    - ``profiles_without_fighter``: identificador da fonte que não é de nenhum lutador nosso.
    """

    updated_fields: int
    fighters_touched: int
    divergent: tuple[AnthropometryDivergence, ...]
    absent_in_source: int
    skipped_without_id: int
    out_of_window: int
    profiles_without_fighter: int

    @property
    def divergences(self) -> int:
        """Quantas divergências foram reportadas (e, por definição, não escritas)."""
        return len(self.divergent)


@dataclass(frozen=True)
class _FieldComparison:
    """Um dos cinco campos, com o nosso valor, o da fonte e o veredito de divergência."""

    name: str
    existing: object
    from_source: object
    divergent: bool


def _differs(existing: object, from_source: object) -> bool:
    """Sem tolerância: data e base ou são iguais ou discordam. Nulo não discorda de nada."""
    return existing is not None and from_source is not None and existing != from_source


def _compare(fighter: Fighter, profile: OfficialAnthropometry) -> tuple[_FieldComparison, ...]:
    """Os cinco campos lado a lado, cada um com o comparador da sua unidade.

    Escrito à mão, um campo por linha, em vez de um laço sobre nomes de atributo: é o que
    mantém a tolerância de centímetro e a de quilo visivelmente ligadas ao campo certo. Um laço
    genérico esconderia exatamente a decisão que esta slice precisa deixar explícita.
    """
    return (
        _FieldComparison(
            "height_cm",
            fighter.height_cm,
            profile.height_cm,
            is_divergent_cm(existing=fighter.height_cm, from_source=profile.height_cm),
        ),
        _FieldComparison(
            "reach_cm",
            fighter.reach_cm,
            profile.reach_cm,
            is_divergent_cm(existing=fighter.reach_cm, from_source=profile.reach_cm),
        ),
        _FieldComparison(
            "weight_kg",
            fighter.weight_kg,
            profile.weight_kg,
            is_divergent_kg(existing=fighter.weight_kg, from_source=profile.weight_kg),
        ),
        _FieldComparison(
            "date_of_birth",
            fighter.date_of_birth,
            profile.date_of_birth,
            _differs(fighter.date_of_birth, profile.date_of_birth),
        ),
        _FieldComparison(
            "stance",
            fighter.stance,
            profile.stance,
            _differs(fighter.stance, profile.stance),
        ),
    )


def _merge(anterior: OfficialAnthropometry, novo: OfficialAnthropometry) -> OfficialAnthropometry:
    """Consolida duas observações do mesmo lutador: a primeira ocorrência não nula vence.

    Medido em 2026-09-01 sobre os 1.354 payloads do cache: **nenhum** ``FighterId`` tem dois
    valores distintos de ``Weight``, ``Height``, ``Reach``, ``DOB`` ou ``Stance`` -- a
    antropometria é atributo do lutador na fonte, não do card. A regra é uma guarda barata para
    o caso de um payload trazer o campo nulo e outro trazê-lo preenchido, não um desempate
    frequente.
    """
    return OfficialAnthropometry(
        ufc_fighter_id=anterior.ufc_fighter_id,
        name=anterior.name,
        height_cm=anterior.height_cm if anterior.height_cm is not None else novo.height_cm,
        reach_cm=anterior.reach_cm if anterior.reach_cm is not None else novo.reach_cm,
        weight_kg=anterior.weight_kg if anterior.weight_kg is not None else novo.weight_kg,
        date_of_birth=(
            anterior.date_of_birth if anterior.date_of_birth is not None else novo.date_of_birth
        ),
        stance=anterior.stance if anterior.stance is not None else novo.stance,
    )


def _consolidate(
    profiles: Iterable[OfficialAnthropometry],
) -> dict[str, OfficialAnthropometry]:
    """Um perfil por ``ufc_fighter_id``: o mesmo lutador aparece uma vez por evento em que lutou."""
    consolidado: dict[str, OfficialAnthropometry] = {}
    for profile in profiles:
        anterior = consolidado.get(profile.ufc_fighter_id)
        consolidado[profile.ufc_fighter_id] = (
            profile if anterior is None else _merge(anterior, profile)
        )
    return consolidado


def backfill_anthropometry(
    session: Session, profiles: Iterable[OfficialAnthropometry]
) -> AnthropometryBackfillResult:
    """Preenche os cinco atributos SÓ onde o nosso está nulo. Commit é do chamador.

    Casa por ``fighters.ufc_fighter_id`` (Sprint 008-03), nunca por nome. Idempotente por
    construção: a escrita é condicionada a o campo estar nulo, então a segunda execução não
    encontra nada a fazer e devolve ``updated_fields=0`` -- sem flag, sem tabela de controle.

    Valor já preenchido **nunca** é sobrescrito: divergência acima da tolerância é contada,
    nomeada e logada, e o valor persistido permanece (RF-04). Só ``UPDATE``, nunca ``INSERT``.
    """
    consolidado = _consolidate(profiles)
    elegiveis = _window_fighters_by_identifier(session)
    persistidos = set(
        session.scalars(select(Fighter.ufc_fighter_id).where(Fighter.ufc_fighter_id.is_not(None)))
    )

    updated_fields = 0
    fighters_touched = 0
    absent_in_source = 0
    out_of_window = 0
    profiles_without_fighter = 0
    divergent: list[AnthropometryDivergence] = []

    for ufc_fighter_id, profile in consolidado.items():
        fighter = elegiveis.get(ufc_fighter_id)
        if fighter is None:
            # Distinguir os dois casos importa: o primeiro é decisão (RF-03), o segundo é
            # cobertura. Colapsá-los esconderia qual dos dois a execução tem.
            if ufc_fighter_id in persistidos:
                out_of_window += 1
            else:
                profiles_without_fighter += 1
            continue

        escritos = 0
        for comparison in _compare(fighter, profile):
            if comparison.from_source is None:
                if comparison.existing is None:
                    absent_in_source += 1
                continue
            if comparison.existing is None:
                setattr(fighter, comparison.name, comparison.from_source)
                escritos += 1
                continue
            if comparison.divergent:
                divergent.append(
                    AnthropometryDivergence(
                        ufc_fighter_id=ufc_fighter_id,
                        name=profile.name,
                        field=comparison.name,
                        existing=str(comparison.existing),
                        from_source=str(comparison.from_source),
                    )
                )

        updated_fields += escritos
        fighters_touched += 1 if escritos else 0

    session.flush()
    result = AnthropometryBackfillResult(
        updated_fields=updated_fields,
        fighters_touched=fighters_touched,
        divergent=tuple(divergent),
        absent_in_source=absent_in_source,
        skipped_without_id=_count_window_fighters_without_identifier(session),
        out_of_window=out_of_window,
        profiles_without_fighter=profiles_without_fighter,
    )
    _log_result(result)
    return result


def _log_result(result: AnthropometryBackfillResult) -> None:
    """Emite o resumo via ``logging`` (``print`` é proibido, regra T20), um caso por linha."""
    logger.info(
        "Backfill de antropometria da fonte oficial: %d campos escritos em %d lutadores; "
        "%d divergências reportadas (não escritas); %d buracos que a fonte também não fecha; "
        "%d lutadores da janela sem ufc_fighter_id; %d perfis fora da janela; "
        "%d perfis sem lutador correspondente.",
        result.updated_fields,
        result.fighters_touched,
        result.divergences,
        result.absent_in_source,
        result.skipped_without_id,
        result.out_of_window,
        result.profiles_without_fighter,
    )
    for divergencia in result.divergent:
        logger.warning(
            "Lutador %r (ufc_fighter_id %s) DIVERGENTE em %s: persistido %s, fonte %s; o valor "
            "persistido foi MANTIDO (nunca sobrescrever em silêncio).",
            divergencia.name,
            divergencia.ufc_fighter_id,
            divergencia.field,
            divergencia.existing,
            divergencia.from_source,
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill de antropometria."""
    parser = argparse.ArgumentParser(
        description=(
            "Preenche reach_cm, height_cm, date_of_birth, stance e weight_kg dos lutadores da "
            "janela (2010-03-21 em diante) a partir da API oficial da UFC, SÓ onde o nosso "
            "registro está nulo. Custo zero de quota."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help=(
            "Diretório do cache em disco por eventId (o mesmo da descoberta). "
            "Um cache já materializado torna a execução offline."
        ),
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=None,
        help=(
            "Modo OFFLINE: lê este diretório como cache e não toca a rede "
            "(demonstração sobre o conjunto de capturas versionadas)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """``python -m ingestion.ufc_official.anthropometry [--cache-dir DIR] [--fixture-dir DIR]``.

    **Sem gate de quota**: a fonte é gratuita e não autenticada, ao contrário da Cito -- a
    ausência do ``--confirmar-gasto-de-quota`` é deliberada, não esquecimento. Commita só no
    sucesso.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    offline = args.fixture_dir is not None
    cache = OfficialEventCache(args.fixture_dir if offline else args.cache_dir)
    client = None if offline else UfcOfficialClient()

    with SessionLocal() as session:
        collection = collect_anthropometry(session, client, cache)
        for ufc_fighter_id, nome, rotulo in collection.unknown_stance:
            logger.warning(
                "Base %r não mapeada para o lutador %r (ufc_fighter_id %s): a base dele "
                "permanece nula.",
                rotulo,
                nome,
                ufc_fighter_id,
            )
        backfill_anthropometry(session, collection.profiles)
        session.commit()


if __name__ == "__main__":
    main()
