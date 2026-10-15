"""Renormalização das chaves de dedup já persistidas em ``fighters`` (M9, SPEC 011, RF-09).

A correção do ``normalize_name`` translitera letra atômica (``ł`` -> ``l``) em vez de apagá-la,
e isso muda a chave de linhas que **já estão** em ``fighters.name_normalized`` -- coluna
persistida e indexada, comparada por igualdade por ``entity_resolution.match_fighter_id``,
``seed_bouts.build_fighter_index`` e todo o pacote ``ufc_official``.

Corrigir a função **sem** renormalizar a coluna é pior que o defeito original: a chave nova
deixa de casar com a linha antiga, e o 'Michał Oleksiejczuk' de um card agendado deixa de
reconhecer o 'michal oleksiejczuk' semeado do Kaggle -- a entity resolution cria um lutador
novo, a contagem de estreantes infla e nada falha. Daí este comando existir, e daí o default
dele ser **medir**.

Invariantes
-----------
- **Medir antes de aplicar** (``CLAUDE.md``): sem ``--aplicar``, zero escrita. É o modo que
  produz o número que o relatório de implementação exige.
- **Colisão nova PARA tudo** (CA-10b): se a chave nova fizer dois lutadores hoje distintos
  colapsarem (mesma DOB) ou ficarem ambíguos (DOB nula de um lado), nada é aplicado -- o
  desempate é decisão humana, nunca escolha do script.
- **Homônimo real não bloqueia**: par que **já hoje** compartilha a chave com DOBs conhecidas e
  distintas (os dois ``Bruno Silva`` do seed) é estado legítimo e pré-existente. Contá-lo
  transformaria o gate num falso positivo permanente.
- **Idempotência**: só a linha cuja chave muda recebe UPDATE; reexecutar devolve ``changed=0``.
- **``source`` intocado**: ``source`` é a origem da LINHA, não do campo. Recalcular a chave de
  dedup de uma linha semeada do Kaggle a mantém ``"kaggle"``.

Mesclagem da duplicata (``--mesclar``)
--------------------------------------
Quando a colisão detectada é um lutador em **duas linhas** -- e não homônimo real --, o remédio
é mesclar, e ele mora aqui: a ferramenta que **detecta** a colisão é o lugar coeso para
**resolvê-la**, em vez de SQL cru sem registro ou de um script que morre depois do uso.

Não é migration, e isso é deliberado: o defeito **não recorre** depois da correção do
``normalize_name`` (um seed + incremental do zero nunca recria o par), então isto é reparo
único de um banco existente. Medido em 2026-10-14: **1** lutador em 2.754 tem letra atômica no
nome, então a exposição é pontual e não pede ferramenta genérica.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.sql.elements import ColumnElement

from apps.bouts.enums import BoutStatus
from apps.bouts.models import Bout, BoutFighter
from apps.features.models import FighterAsOfFeatures
from apps.fighters.models import Fighter
from apps.predictions.models import BoutPrediction
from ingestion.normalize import normalize_name
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)


class NewCollisionError(RuntimeError):
    """A renormalização colidiria duas linhas hoje distintas; nada foi escrito (CA-10b)."""


class FighterMergeError(RuntimeError):
    """A mesclagem de duas linhas de lutador foi recusada; nada foi escrito."""


@dataclass(frozen=True)
class PersistedName:
    """A chave de dedup de um lutador já persistido, como o planejador a enxerga.

    ``name_normalized`` é o valor **em disco** (possivelmente produzido pela versão antiga da
    normalização); ``name`` é a grafia original, da qual a chave nova é recalculada.
    """

    id: int
    name: str
    name_normalized: str
    date_of_birth: date | None


@dataclass(frozen=True)
class RenormalizationPlan:
    """O que a correção do ``normalize_name`` faz com as chaves JÁ persistidas.

    ``changed`` traz ``(fighter_id, chave antiga, chave nova)`` de cada linha a atualizar, em
    ordem de id -- a ordem existe para o relatório não mudar de forma entre execuções.
    ``new_collisions`` traz ``(id_menor, id_maior, chave nova em comum)`` dos pares que a
    correção faria colapsar.
    """

    changed: tuple[tuple[int, str, str], ...]
    new_collisions: tuple[tuple[int, int, str], ...]

    @property
    def is_safe_to_apply(self) -> bool:
        """Aplicar é seguro só quando NENHUMA colisão nova aparece (CA-10b)."""
        return not self.new_collisions


def _colapsaria(a: PersistedName, b: PersistedName) -> bool:
    """A entity resolution colapsaria ou ficaria ambígua entre ``a`` e ``b``?

    Sim quando as DOBs são iguais (mesma pessoa, do ponto de vista da chave) ou quando alguma
    é nula -- ``match_fighter_id`` levanta ``AmbiguousFighterMatchError`` diante de um
    persistido sem DOB indescartável. Não quando as duas são conhecidas e distintas: aí a DOB
    continua separando os homônimos, que é exatamente a função dela.
    """
    if a.date_of_birth is None or b.date_of_birth is None:
        return True
    return a.date_of_birth == b.date_of_birth


def plan_renormalization(rows: Sequence[PersistedName]) -> RenormalizationPlan:
    """Função pura: compara a chave persistida com ``normalize_name(name)`` corrigido.

    Colisão é **nova** quando o par não compartilhava a chave antiga, passa a compartilhar a
    nova e colapsaria sob a entity resolution. Par que já colidia (homônimo real separado por
    DOB) não entra: ele não é efeito desta correção, e contá-lo transformaria o gate num falso
    positivo permanente.
    """
    chave_nova = {row.id: normalize_name(row.name) for row in rows}
    changed = tuple(
        (row.id, row.name_normalized, chave_nova[row.id])
        for row in sorted(rows, key=lambda row: row.id)
        if chave_nova[row.id] != row.name_normalized
    )

    por_chave_antiga: dict[str, list[PersistedName]] = {}
    por_chave_nova: dict[str, list[PersistedName]] = {}
    for row in sorted(rows, key=lambda row: row.id):
        por_chave_antiga.setdefault(row.name_normalized, []).append(row)
        por_chave_nova.setdefault(chave_nova[row.id], []).append(row)

    pares_antigos = {
        (a.id, b.id)
        for grupo in por_chave_antiga.values()
        for indice, a in enumerate(grupo)
        for b in grupo[indice + 1 :]
    }
    collisions = tuple(
        (a.id, b.id, chave)
        for chave, grupo in sorted(por_chave_nova.items())
        for indice, a in enumerate(grupo)
        for b in grupo[indice + 1 :]
        if (a.id, b.id) not in pares_antigos and _colapsaria(a, b)
    )
    return RenormalizationPlan(changed=changed, new_collisions=collisions)


def load_persisted_names(session: Session) -> list[PersistedName]:
    """Materializa ``(id, name, name_normalized, date_of_birth)`` de todos os lutadores."""
    return [
        PersistedName(id=fighter_id, name=name, name_normalized=name_normalized, date_of_birth=dob)
        for fighter_id, name, name_normalized, dob in session.execute(
            select(Fighter.id, Fighter.name, Fighter.name_normalized, Fighter.date_of_birth)
        )
    ]


def apply_renormalization(session: Session, plan: RenormalizationPlan) -> int:
    """Grava as chaves novas; devolve quantas linhas mudaram. Rerun é no-op.

    Recusa-se a aplicar um plano com colisão nova (CA-10b): colapsar dois lutadores hoje
    distintos é achado que PARA a sprint, não efeito colateral aceitável. O ``source`` da linha
    **não** é tocado -- ele é a origem da linha, não do campo.
    """
    if not plan.is_safe_to_apply:
        raise NewCollisionError(
            f"A renormalização faria {len(plan.new_collisions)} par(es) de lutadores hoje "
            "distintos colidirem na chave de dedup; nada foi escrito. O desempate (mesclar ou "
            "separar) é decisão humana: "
            + "; ".join(
                f"ids {id_a} e {id_b} em {chave!r}" for id_a, id_b, chave in plan.new_collisions
            )
        )

    atualizadas = 0
    for fighter_id, _antiga, nova in plan.changed:
        fighter = session.get(Fighter, fighter_id)
        if fighter is None or fighter.name_normalized == nova:
            continue
        fighter.name_normalized = nova
        atualizadas += 1
    return atualizadas


# --------------------------------------------------------------------------- #
# Mesclagem de duas linhas que descrevem o MESMO lutador.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MergeReport:
    """O que a mesclagem fez (ou faria, em modo medição), contagem por contagem.

    Nada de total mudo: cada referência reaponta num caminho próprio, e é a separação entre
    elas que permite conferir o resultado contra a medição feita antes.
    """

    duplicate_id: int
    target_id: int
    applied: bool
    bout_fighters_repointed: int
    winners_repointed: int
    predictions_repointed: int
    asof_cache_deleted: int
    adopted_name: str | None
    record_delta: tuple[int, int, int]

    @property
    def is_noop(self) -> bool:
        """A duplicata não existe: nada a fazer, e isso **não** é erro (rerun)."""
        return not self.applied


_NOTHING_TO_DO = MergeReport(
    duplicate_id=0,
    target_id=0,
    applied=False,
    bout_fighters_repointed=0,
    winners_repointed=0,
    predictions_repointed=0,
    asof_cache_deleted=0,
    adopted_name=None,
    record_delta=(0, 0, 0),
)


def _record_delta(session: Session, duplicate_id: int) -> tuple[int, int, int]:
    """Desfecho ``(vitórias, derrotas, empates)`` das lutas **realizadas** da duplicata.

    É o delta a somar ao snapshot de cartel do sobrevivente -- e **não** um recálculo total do
    granular, que seria errado aqui: o snapshot de ``fighters`` é de **carreira inteira** (vem
    do dataset do Kaggle e inclui luta fora do UFC), enquanto ``bouts`` só tem UFC. Um recálculo
    total trocaria o cartel real por um parcial (medido no caso Błachowicz: 29-11-1 viraria
    12-8-1) e deixaria essa linha incoerente com as outras 2.753.

    Vencedor nulo conta como empate/no contest, a mesma convenção do schema (ADR 0001: não há
    valor ``DRAW`` no método -- o empate mora na nulabilidade do vencedor). Luta **agendada**
    fica fora: ela não tem desfecho, e contá-la inventaria resultado.
    """
    linhas = session.execute(
        select(Bout.winner_id)
        .join(BoutFighter, BoutFighter.bout_id == Bout.id)
        .where(BoutFighter.fighter_id == duplicate_id, Bout.status == BoutStatus.COMPLETED)
    ).all()
    vencedores = [winner_id for (winner_id,) in linhas]
    wins = sum(1 for winner_id in vencedores if winner_id == duplicate_id)
    losses = sum(1 for winner_id in vencedores if winner_id not in (None, duplicate_id))
    draws = sum(1 for winner_id in vencedores if winner_id is None)
    return wins, losses, draws


def _count(session: Session, condition: ColumnElement[bool], model: type[DeclarativeBase]) -> int:
    """Quantas linhas de ``model`` satisfazem ``condition`` (medição antes da escrita)."""
    return session.scalar(select(func.count()).select_from(model).where(condition)) or 0


def _bouts_com_os_dois(session: Session, duplicate_id: int, target_id: int) -> list[int]:
    """Lutas em que a duplicata **e** o sobrevivente aparecem -- impedimento à mesclagem.

    Reapontar ali produziria um auto-confronto (os dois cantos no mesmo ``fighter_id``), que
    viola ``uq_bout_fighter`` e, pior, descreveria uma luta que não existe. Decidido **antes**
    de qualquer escrita, então não há parcial a reverter.
    """
    por_luta: dict[int, set[int]] = {}
    for bout_id, fighter_id in session.execute(
        select(BoutFighter.bout_id, BoutFighter.fighter_id).where(
            BoutFighter.fighter_id.in_((duplicate_id, target_id))
        )
    ):
        por_luta.setdefault(bout_id, set()).add(fighter_id)
    return sorted(bout_id for bout_id, ids in por_luta.items() if len(ids) == 2)


def merge_fighters(session: Session, *, duplicate_id: int, target_id: int) -> MergeReport:
    """Mescla ``duplicate_id`` em ``target_id``; devolve o que foi repontado.

    A ordem é: **recusar antes de escrever**, reapontar as referências, adotar a grafia,
    recalcular o cartel a partir do delta derivado e só então remover a linha duplicada.

    Recusa (``FighterMergeError``, nada escrito): ids iguais; sobrevivente inexistente; alguma
    luta contendo **os dois** (seria auto-confronto); e chaves normalizadas divergentes -- se os
    dois nomes não colapsam na mesma chave, mesclá-los é outra operação, não a resolução de uma
    colisão de dedup.

    **Idempotente**: duplicata ausente devolve ``applied=False`` e não toca nada -- rerun não é
    erro. ``source`` do sobrevivente **não** muda (é a origem da linha, não do campo), e
    ``bout_fighter_rounds`` não precisa de nada: ele pende de ``bout_fighters.id``, que não
    muda, então os rounds seguem o canto repontado de graça.
    """
    if duplicate_id == target_id:
        raise FighterMergeError(
            f"A duplicata e o sobrevivente são a mesma linha (id {duplicate_id}); "
            "mesclar uma linha consigo mesma é erro de operador, não no-op."
        )
    target = session.get(Fighter, target_id)
    if target is None:
        raise FighterMergeError(
            f"O sobrevivente (id {target_id}) não existe; nunca mesclar para um id ausente."
        )
    duplicate = session.get(Fighter, duplicate_id)
    if duplicate is None:
        logger.info(
            "Lutador %d não existe; a mesclagem em %d já foi feita (rerun é no-op).",
            duplicate_id,
            target_id,
        )
        return _NOTHING_TO_DO

    if normalize_name(duplicate.name) != normalize_name(target.name):
        raise FighterMergeError(
            f"As chaves normalizadas de {duplicate.name!r} (id {duplicate_id}) e "
            f"{target.name!r} (id {target_id}) divergem "
            f"({normalize_name(duplicate.name)!r} != {normalize_name(target.name)!r}); "
            "isto não é resolução de colisão de dedup. Nada foi escrito."
        )
    compartilhadas = _bouts_com_os_dois(session, duplicate_id, target_id)
    if compartilhadas:
        raise FighterMergeError(
            f"As lutas {', '.join(str(bout_id) for bout_id in compartilhadas)} contêm os dois "
            f"ids ({duplicate_id} e {target_id}); reapontar criaria auto-confronto e violaria "
            "uq_bout_fighter. Nada foi escrito."
        )

    delta = _record_delta(session, duplicate_id)
    nome_adotado = duplicate.name

    # As contagens são medidas ANTES de cada escrita, em vez de lidas do ``rowcount``: é o que
    # permite conferir o relatório da execução aplicada contra o da medição (os dois números
    # saem do mesmo ``select``, não de um efeito colateral do driver).
    bout_fighters = _count(session, BoutFighter.fighter_id == duplicate_id, BoutFighter)
    winners = _count(session, Bout.winner_id == duplicate_id, Bout)
    predictions = _count(
        session, BoutPrediction.predicted_winner_id == duplicate_id, BoutPrediction
    )
    asof = _count(session, FighterAsOfFeatures.fighter_id == duplicate_id, FighterAsOfFeatures)

    session.execute(
        update(BoutFighter)
        .where(BoutFighter.fighter_id == duplicate_id)
        .values(fighter_id=target_id)
    )
    session.execute(update(Bout).where(Bout.winner_id == duplicate_id).values(winner_id=target_id))
    session.execute(
        update(BoutPrediction)
        .where(BoutPrediction.predicted_winner_id == duplicate_id)
        .values(predicted_winner_id=target_id)
    )
    # Cache derivado do granular (ADR 0001): descartável, rematerializado por
    # ``python -m ingestion.features.cli build --stage materialize-asof``. Reapontá-lo seria pior
    # que apagá-lo -- as features da duplicata foram calculadas sobre duas lutas, não sobre a
    # carreira inteira que o sobrevivente passa a ter.
    session.execute(
        delete(FighterAsOfFeatures).where(FighterAsOfFeatures.fighter_id == duplicate_id)
    )

    target.name = nome_adotado
    target.name_normalized = normalize_name(nome_adotado)
    wins, losses, draws = delta
    target.wins += wins
    target.losses += losses
    target.draws += draws

    session.delete(duplicate)
    session.flush()
    return MergeReport(
        duplicate_id=duplicate_id,
        target_id=target_id,
        applied=True,
        bout_fighters_repointed=bout_fighters,
        winners_repointed=winners,
        predictions_repointed=predictions,
        asof_cache_deleted=asof,
        adopted_name=nome_adotado,
        record_delta=delta,
    )


def _log_merge(report: MergeReport, *, aplicado: bool) -> None:
    """Emite o relatório da mesclagem via ``logging`` (``print`` é proibido -- regra T20)."""
    if report.is_noop:
        logger.info(
            "Mesclagem de %d em %d: nada a fazer (a duplicata não existe).",
            report.duplicate_id,
            report.target_id,
        )
        return
    wins, losses, draws = report.record_delta
    logger.info(
        "Mesclagem de %d em %d (%s): cantos repontados=%d; winner_id repontados=%d; "
        "predições repontadas=%d; cache as-of apagado=%d; grafia adotada=%r; "
        "delta de cartel=+%dV/+%dD/+%dE",
        report.duplicate_id,
        report.target_id,
        "aplicada" if aplicado else "modo medição (nada escrito)",
        report.bout_fighters_repointed,
        report.winners_repointed,
        report.predictions_repointed,
        report.asof_cache_deleted,
        report.adopted_name,
        wins,
        losses,
        draws,
    )
    if aplicado:
        logger.info(
            "Rematerialize o cache as-of: python -m ingestion.features.cli --stage materialize-asof"
        )


def _log_plan(plan: RenormalizationPlan, *, total: int, aplicado: int | None) -> None:
    """Emite o relatório da medição via ``logging`` (``print`` é proibido -- regra T20)."""
    logger.info(
        "Renormalização de fighters.name_normalized: %d linha(s) persistida(s); %d mudam de "
        "chave; %d colisão(ões) nova(s); aplicado=%s",
        total,
        len(plan.changed),
        len(plan.new_collisions),
        "não (modo medição)" if aplicado is None else str(aplicado),
    )
    for fighter_id, antiga, nova in plan.changed:
        logger.info("  id %d: %r -> %r", fighter_id, antiga, nova)
    for id_a, id_b, chave in plan.new_collisions:
        logger.warning(
            "  COLISÃO NOVA: os ids %d e %d passariam a compartilhar a chave %r. Nada foi "
            "aplicado -- o desempate é decisão humana.",
            id_a,
            id_b,
            chave,
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando da renormalização."""
    parser = argparse.ArgumentParser(
        description=(
            "Mede (e, com --aplicar, grava) a renormalização de fighters.name_normalized "
            "depois da correção da transliteração de letra atômica."
        ),
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="Grava as chaves novas. Sem a flag, o comando apenas MEDE (zero escrita).",
    )
    parser.add_argument(
        "--mesclar",
        nargs=2,
        type=int,
        metavar=("ID_DUPLICATA", "ID_SOBREVIVENTE"),
        default=None,
        help=(
            "Mescla a primeira linha na segunda (reaponta as referências, adota a grafia da "
            "duplicata, soma o desfecho das lutas movidas ao cartel e remove a duplicata). "
            "Respeita o --aplicar: sem ele, apenas MEDE."
        ),
    )
    return parser.parse_args(argv)


def _main_merge(*, duplicate_id: int, target_id: int, aplicar: bool) -> None:
    """Executa (ou apenas mede) a mesclagem de duas linhas de lutador.

    Em modo medição a mesclagem roda de verdade dentro de um **SAVEPOINT**
    (``session.begin_nested()``, o mesmo idioma de ``ingestion.cito.gap_sync``) e é revertida:
    é o único jeito honesto de reportar o que ela faria sem duplicar a lógica num caminho
    "dry-run" que divergiria do real em silêncio.

    O SAVEPOINT -- e não um ``session.rollback()`` solto -- porque a reversão precisa alcançar
    **só** esta operação: num ``rollback`` da sessão inteira, a transação externa de quem
    hospeda a sessão (a fixture transacional da suíte, por exemplo) morreria junto.
    """
    with SessionLocal() as session:
        savepoint = session.begin_nested()
        try:
            report = merge_fighters(session, duplicate_id=duplicate_id, target_id=target_id)
        except FighterMergeError as exc:
            savepoint.rollback()
            logger.error("Mesclagem recusada: %s", exc)
            sys.exit(1)
        _log_merge(report, aplicado=aplicar)
        if aplicar:
            savepoint.commit()
            session.commit()
        else:
            savepoint.rollback()


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint ``python -m ingestion.renormalize_names [--mesclar A B] [--aplicar]``.

    **Default é medir**, nos dois modos: lê, planeja, loga o relatório e sai sem escrever nada
    -- é esse número que o relatório de implementação da sprint precisa. Com ``--aplicar``,
    grava e commita só no sucesso; um plano com colisão nova (ou uma mesclagem recusada) aborta
    com ``sys.exit(1)`` sem escrever.

    ``--mesclar`` resolve a colisão em vez de medi-la, e é operação **separada**: não faz
    sentido mesclar e renormalizar na mesma execução, porque é a mesclagem que torna a
    renormalização segura -- a medição entre as duas é o que prova isso.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    if args.mesclar is not None:
        _main_merge(duplicate_id=args.mesclar[0], target_id=args.mesclar[1], aplicar=args.aplicar)
        return

    with SessionLocal() as session:
        rows = load_persisted_names(session)
        plan = plan_renormalization(rows)
        if not args.aplicar:
            _log_plan(plan, total=len(rows), aplicado=None)
            return
        try:
            atualizadas = apply_renormalization(session, plan)
        except NewCollisionError as exc:
            _log_plan(plan, total=len(rows), aplicado=None)
            logger.error("Renormalização recusada: %s", exc)
            sys.exit(1)
        _log_plan(plan, total=len(rows), aplicado=atualizadas)
        session.commit()


if __name__ == "__main__":
    main()
