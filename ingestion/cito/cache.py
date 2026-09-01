"""Caches em disco resumíveis das respostas da Cito (M5, Slice 05; M6, Slice 03).

O backfill round-a-round (``ingestion.cito.backfill_rounds``) consome a Cito uma vez por evento
(``CitoClient.fetch_event_stats``), o que gasta a quota do free tier (500 req/mês). Este cache
torna o backfill **resumível**: a resposta de cada evento é gravada em disco (um JSON por slug) e,
numa reexecução, o evento já baixado vira um **cache hit** -- lido do disco, sem chamar ``fetch``
nem cobrar o ``CallBudget``. Uma interrupção no meio do backfill não re-gasta a quota do que já foi
baixado.

Round-trip fiel sem ``Any``
---------------------------
O ``CitoEventStats`` já é o tipo de saída (fronteira dinâmica tipada na borda pelos DTOs). Persistir
o DTO parseado e revalidá-lo direto quebraria, porque os ``field_validator`` dos DTOs esperam a
forma **wire** da Cito (golpes como ``"L of A"``, tempo como ``"m:ss"``), não a forma parseada
(tuplas/segundos). Por isso a gravação reconstrói a forma wire (``_stats_to_storable``) e a leitura
revalida via ``CitoEventStats.model_validate`` -- o mesmo caminho de validação da borda, sem ``Any``
propagando do disco.

O round-trip cobre os **quatro** blocos do DTO (``event``, ``bouts``, ``bout_stats``,
``round_stats``) desde a ADR 0005. Gravar só as stats faria o cache hit nem revalidar (``event``
e ``bouts`` são obrigatórios) e tiraria de um backfill retomado a única fonte do ``corner``.

``CatalogPageCache`` (M6, Slice 03)
-----------------------------------
Mesma disciplina para as páginas do **catálogo** de eventos, com uma diferença: ali o payload
cru é gravado como veio, porque o ``CitoCatalogEnvelope`` valida a forma wire diretamente (não
há golpes ``"L of A"`` nem tempo ``"m:ss"`` a reconstruir).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from ingestion.cito.dto import (
    CitoBoutBlock,
    CitoBoutFighterRef,
    CitoBoutStatLine,
    CitoEventBlock,
    CitoEventStats,
    CitoFighterProfile,
    CitoRoundStatLine,
)

logger = logging.getLogger(__name__)


def _stat_to_wire(value: tuple[int | None, int | None]) -> str | None:
    """Reconstrói ``(landed, attempted)`` -> ``"L of A"``; ausência (algum lado None) -> ``None``.

    Inverso de ``ingestion.cito.parsers.parse_stat``: mantém a ausência explícita (nunca inventa
    zero) para que a revalidação do cache degrade igual à borda original.
    """
    landed, attempted = value
    if landed is None or attempted is None:
        return None
    return f"{landed} of {attempted}"


def _clock_to_wire(value: int | None) -> str | None:
    """Reconstrói segundos -> ``"m:ss"`` (inverso de ``parse_clock``); ausência -> ``None``."""
    if value is None:
        return None
    return f"{value // 60}:{value % 60:02d}"


def _line_to_storable(line: CitoBoutStatLine) -> dict[str, object]:
    """Serializa uma linha de stat na forma wire (nomes de campo do DTO, golpes/tempo como string).

    A revalidação usa ``populate_by_name`` dos DTOs, então os nomes de campo (snake_case) bastam;
    os nove splits e o tempo de controle voltam à string que os ``field_validator`` reparseiam.
    """
    storable: dict[str, object] = {
        "bout_id": line.bout_id,
        "fighter_slug": line.fighter_slug,
        "fighter_name": line.fighter_name,
        "knockdowns": line.knockdowns,
        "submission_attempts": line.submission_attempts,
        "reversals": line.reversals,
        "sig_strikes": _stat_to_wire(line.sig_strikes),
        "total_strikes": _stat_to_wire(line.total_strikes),
        "head": _stat_to_wire(line.head),
        "body": _stat_to_wire(line.body),
        "leg": _stat_to_wire(line.leg),
        "distance": _stat_to_wire(line.distance),
        "clinch": _stat_to_wire(line.clinch),
        "ground": _stat_to_wire(line.ground),
        "takedowns": _stat_to_wire(line.takedowns),
        "control_time_seconds": _clock_to_wire(line.control_time_seconds),
    }
    if isinstance(line, CitoRoundStatLine):
        storable["round"] = line.round
    return storable


def _event_to_storable(event: CitoEventBlock) -> dict[str, object]:
    """Serializa o bloco ``event`` (metadados) por nome de campo; datas em ISO-8601."""
    return {
        "id": event.id,
        "slug": event.slug,
        "title": event.title,
        "status": event.status,
        "event_date": event.event_date.isoformat(),
        "starts_at": event.starts_at.isoformat() if event.starts_at is not None else None,
    }


def _profile_to_storable(profile: CitoFighterProfile) -> dict[str, object]:
    """Serializa o ``profile`` embutido no canto (identidade + cartel), sem inventar campo.

    Cada componente do cartel mantém a própria ausência (``None`` permanece ``None``): é o
    mapeamento para ``fighters`` que degrada para zero, e antecipar isso aqui gravaria um
    cartel 0/0/0 indistinguível de um cartel realmente zerado.

    As duas URLs de imagem (M7, Slice 06) entram pelo mesmo motivo da ``image_url`` da arte: é
    delas que o backfill de imagem preenche ``fighters.headshot_url``/``body_image_url``.
    Esquecê-las faria uma execução retomada do cache gravar ``None`` achando que é ausência
    real -- falha silenciosa, porque campo opcional nulo não se distingue de campo ausente.
    """
    return {
        "slug": profile.slug,
        "name": profile.name,
        "nickname": profile.nickname,
        "headshot_url": profile.headshot_url,
        "body_image_url": profile.body_image_url,
        "record": {
            "wins": profile.record.wins,
            "losses": profile.record.losses,
            "draws": profile.record.draws,
        },
    }


def _fighter_ref_to_storable(fighter: CitoBoutFighterRef) -> dict[str, object]:
    """Serializa um canto do card; o ``corner`` vira o valor do enum (a forma wire da Cito).

    O ``profile`` entra porque é dele que o fechamento do gap (Slice 06) cria os lutadores
    inéditos sem gastar chamada de perfil (RF-13). Esquecê-lo faria um gap retomado do cache
    criar lutador sem apelido e com cartel zerado -- dado falso com aparência de dado.

    A ``image_url`` entra pelo mesmo motivo, e o estrago de esquecê-la é pior: é o sufixo do nome
    do arquivo da arte que carrega o canto REAL da luta
    (``ingestion.cito.gap_sync.art_side``). Sem ela, um gap retomado do cache cairia no canto
    atribuído sem que contagem nenhuma acusasse a diferença.
    """
    return {
        "fighter_slug": fighter.fighter_slug,
        "fighter_name": fighter.fighter_name,
        "corner": fighter.corner.value,
        "outcome": fighter.outcome,
        "image_url": fighter.image_url,
        "profile": (_profile_to_storable(fighter.profile) if fighter.profile is not None else None),
    }


def _bout_to_storable(bout: CitoBoutBlock) -> dict[str, object]:
    """Serializa uma luta do card; ``result_time_seconds`` volta ao relógio ``"m:ss"``.

    Mesma disciplina das linhas de stat: grava-se a forma **wire**, a única que os
    ``field_validator`` sabem reparsear na revalidação.
    """
    return {
        "id": bout.id,
        "card_section": bout.card_section,
        "card_section_order": bout.card_section_order,
        "bout_order": bout.bout_order,
        "weight_class": bout.weight_class,
        "title_bout": bout.title_bout,
        "status": bout.status,
        "is_cancelled": bout.is_cancelled,
        "winner_fighter_slug": bout.winner_fighter_slug,
        "result_round": bout.result_round,
        "result_time_seconds": _clock_to_wire(bout.result_time_seconds),
        "method": bout.method,
        "method_details": bout.method_details,
        "fighters": [_fighter_ref_to_storable(fighter) for fighter in bout.fighters],
    }


def _stats_to_storable(stats: CitoEventStats) -> dict[str, object]:
    """Serializa o ``CitoEventStats`` inteiro (os quatro blocos) na forma wire cacheável.

    ``event`` e ``bouts`` entram porque o DTO os exige (ADR 0005) e porque é deles que vem o
    ``corner`` -- gravar só as stats faria um backfill retomado devolver números sem o card.
    """
    return {
        "event": _event_to_storable(stats.event),
        "bouts": [_bout_to_storable(bout) for bout in stats.bouts],
        "bout_stats": [_line_to_storable(line) for line in stats.bout_stats],
        "round_stats": [_line_to_storable(line) for line in stats.round_stats],
    }


class EventStatsCache:
    """Cache get-or-fetch em disco das stats de evento da Cito, por slug (resumível)."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def _path(self, event_slug: str) -> Path:
        return self._cache_dir / f"event_stats_{event_slug}.json"

    def get_or_fetch(
        self, event_slug: str, fetch: Callable[[str], CitoEventStats]
    ) -> tuple[CitoEventStats, bool]:
        """Devolve ``(stats, cache_hit)``: hit lê do disco sem chamar ``fetch`` (0 quota).

        Miss: chama ``fetch`` (que cobra o ``CallBudget`` no cliente), grava a resposta na forma
        wire e devolve ``cache_hit=False``. Hit: desserializa o JSON e revalida via
        ``CitoEventStats.model_validate``, sem invocar ``fetch`` nem cobrar quota.
        """
        path = self._path(event_slug)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            logger.info("Cache hit do evento %r (lido do disco, 0 quota)", event_slug)
            return CitoEventStats.model_validate(payload), True

        stats = fetch(event_slug)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_stats_to_storable(stats)), encoding="utf-8")
        logger.info("Cache miss do evento %r; resposta gravada em %s", event_slug, path)
        return stats, False


class CatalogPageCache:
    """Cache get-or-fetch em disco das páginas **cruas** do catálogo Cito (resumível).

    O catálogo custa ~17 chamadas (811 eventos com ``limit=50``) e é relido pelas Slices 05 e
    06. Cada página é gravada como o JSON **cru** que a Cito devolveu -- diferente do
    ``EventStatsCache``, aqui não há reconstrução de forma wire: o payload do disco é o mesmo
    que o da rede, então a validação em ``CitoCatalogEnvelope`` acontece pelo caminho de sempre,
    na borda, sem ``Any`` propagando.

    Um cache hit **não** chama o cliente e, portanto, **não cobra o ``CallBudget``** -- a mesma
    regra do cache do M5. A chave é responsabilidade de quem chama e precisa embutir o recorte
    (``limit``/``from``/``to``/página): páginas de recortes diferentes não são intercambiáveis.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def _path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.json"

    def get_or_fetch(self, key: str, fetch: Callable[[], object]) -> tuple[object, bool]:
        """Devolve ``(payload_cru, cache_hit)``: hit lê do disco sem chamar ``fetch`` (0 quota).

        Miss: chama ``fetch`` (que cobra o ``CallBudget`` no cliente), grava a resposta crua e
        devolve ``cache_hit=False``.
        """
        path = self._path(key)
        if path.is_file():
            logger.info("Cache hit da página de catálogo %r (lida do disco, 0 quota)", key)
            return json.loads(path.read_text(encoding="utf-8")), True

        payload = fetch()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("Cache miss da página de catálogo %r; resposta gravada em %s", key, path)
        return payload, False
