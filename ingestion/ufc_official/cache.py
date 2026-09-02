"""Cache em disco do payload **cru** de um evento da fonte oficial, por ``eventId``.

É o que torna a descoberta incremental possível: a varredura completa do intervalo de ids
acontece **uma vez**, e as execuções seguintes leem do disco em vez de rebaixar ~1.300 eventos.

O que está gravado é o JSON **como veio**
-----------------------------------------
Um arquivo por id com **todas** as chaves que a fonte devolveu, na ordem em que vieram -- sem
reconstrução de forma e sem passar por DTO nenhum (o que a reserialização não preserva é
espaçamento, não conteúdo). A leitura passa pelo **mesmo** ``parse_event`` da rede, então uma
quebra de contrato aparece igual vindo do disco ou vindo do cabo, e um campo que a fonte manda e
o DTO ignora continua no arquivo, disponível para a próxima sondagem.

A alternativa é conhecida e custou caro: o ``EventStatsCache`` da Cito guardava a forma já
desserializada (reconstruída a partir do DTO validado) e, com isso, escondeu por meses um campo
que a API devolvia e o DTO não declarava. Um cache que só sabe devolver o que o DTO de hoje
entende não serve para descobrir que o DTO de hoje está errado.

Ausência não é cacheada
-----------------------
Só sucessos vão para o disco. Um id que hoje não existe pode existir amanhã (a fonte responde
**200 com envelope vazio**, não 404, para id inexistente), e gravar a ausência congelaria o
buraco para sempre. Re-sondar buracos é irrelevante em custo: a fonte é gratuita, sem
autenticação e sem gate de quota.

Por que um cache novo e não ``CatalogPageCache``
-------------------------------------------------
As semânticas de chave e de invalidação diferem -- id de evento contra recorte de página
(``limit``/``from``/``to``), horizonte de um evento futuro contra o de uma página -- e o corpo
compartilhado seriam ~15 linhas de leitura e escrita de JSON. Extrair um cache genérico agora
seria abstração sobre duas formas diferentes na segunda ocorrência; a extração espera a
terceira.

Para revarrer o catálogo inteiro, apague o diretório (``rm -rf .cache/ufc_official``). Não há
flag para isso de propósito: uma revarredura completa é decisão consciente, não default.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Um arquivo por evento, nomeado pelo id -- o mesmo nome das capturas versionadas, para que um
# diretório de fixtures possa ser lido como cache no modo offline.
_FILENAME = "event_live_{event_id}.json"
_FILENAME_PATTERN = re.compile(r"^event_live_(?P<event_id>\d+)\.json$")


class OfficialEventCache:
    """Cache get-or-fetch em disco do payload cru de um evento, por ``eventId``."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def _path(self, event_id: int) -> Path:
        return self._cache_dir / _FILENAME.format(event_id=event_id)

    def known_ids(self) -> set[int]:
        """Ids já materializados em disco -- é deles que sai a fronteira da próxima execução.

        Arquivo com nome fora do padrão é ignorado (o diretório de fixtures também guarda um
        ``README.md`` e capturas do endpoint de luta).
        """
        if not self._cache_dir.is_dir():
            return set()
        encontrados = (
            _FILENAME_PATTERN.match(caminho.name) for caminho in self._cache_dir.iterdir()
        )
        return {int(match.group("event_id")) for match in encontrados if match is not None}

    def get_or_fetch(
        self, event_id: int, fetch: Callable[[int], object | None]
    ) -> tuple[object | None, bool]:
        """Devolve ``(payload_cru, cache_hit)``; ``None`` no payload significa **ausente**.

        Hit: lê o JSON do disco sem chamar ``fetch``. Miss: chama ``fetch`` e, **só se ele
        devolver um payload**, grava-o cru. Ausência (``None``) não é gravada.
        """
        path = self._path(event_id)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), True

        payload = fetch(event_id)
        if payload is None:
            return None, False

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        logger.debug("Evento %d gravado no cache em %s", event_id, path)
        return payload, False
