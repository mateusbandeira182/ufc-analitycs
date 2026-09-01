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

from ingestion.cito.dto import CitoBoutStatLine, CitoEventStats, CitoRoundStatLine

logger = logging.getLogger(__name__)

# Campos de golpe do DTO expressos como ``"landed of attempted"`` na forma wire da Cito.
_SPLIT_FIELD_NAMES = (
    "sig_strikes",
    "head",
    "body",
    "leg",
    "distance",
    "clinch",
    "ground",
    "takedowns",
)


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
    os oito splits e o tempo de controle voltam à string que os ``field_validator`` reparseiam.
    """
    storable: dict[str, object] = {
        "bout_id": line.bout_id,
        "corner": line.corner.value,
        "fighter_slug": line.fighter_slug,
        "knockdowns": line.knockdowns,
        "submission_attempts": line.submission_attempts,
        "reversals": line.reversals,
        "sig_strikes": _stat_to_wire(line.sig_strikes),
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


def _stats_to_storable(stats: CitoEventStats) -> dict[str, object]:
    """Serializa o ``CitoEventStats`` inteiro (totais + round-a-round) na forma wire cacheável."""
    return {
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
