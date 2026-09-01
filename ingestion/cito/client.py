"""Cliente HTTP tipado da Cito API (``mmaapi.dev``).

Busca um evento (``CitoEvent``), o perfil de um lutador (``CitoFighter``), as stats
granulares por canto de uma luta (``CitoBoutStats``), as stats do endpoint real de evento
-- totais + round-a-round -- (``CitoEventStats`` via ``fetch_event_stats``) e o **catálogo
paginado** de eventos (``list[CitoCatalogItem]`` via ``fetch_event_catalog``, a fonte do
identificador de um evento na Cito). Dois caminhos:

- **Modo fixture** (``fixture_dir`` definido): lê um JSON local (``event_{id}.json`` /
  ``fighter_{slug}.json`` / ``bout_stats_{bout_id}.json`` / ``events_catalog_page_{n}.json``)
  em vez de tocar a rede -- é o caminho de teste e da execução de demonstração, e **não**
  consome a quota do free tier (500 req/mês).
- **Modo HTTP**: ``GET {base_url}/api/v1/ufc/events`` (evento por id e catálogo paginado),
  ``GET {base_url}/api/v1/ufc/events/{slug}/stats``,
  ``GET {base_url}/api/v1/ufc/fighters/{slug}`` e
  ``GET {base_url}/api/v1/ufc/bouts/{boutId}/stats`` autenticados por token, com o erro e o
  rate-limit convertidos em exceções tipadas (``CitoRateLimitError`` para 429, ``CitoError``
  para os demais), sem vazar a exceção crua do ``httpx``. Todos os caminhos são **versionados**;
  o ``/events/{slug}/stats`` sem prefixo, usado até o M5, nunca existiu na API real (ADR 0005).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.dto import (
    CitoBoutStats,
    CitoCatalogEnvelope,
    CitoCatalogItem,
    CitoEvent,
    CitoEventStats,
    CitoFighter,
    CitoFighterEnvelope,
    CitoStatsEnvelope,
)
from mma_analytics.settings import settings

_EVENTS_PATH = "/api/v1/ufc/events"
_FIGHTERS_PATH = "/api/v1/ufc/fighters"
_BOUTS_PATH = "/api/v1/ufc/bouts"
_HTTP_TOO_MANY_REQUESTS = 429

# Teto default de chamadas à Cito por execução, alinhado ao free tier (500 req/mês).
DEFAULT_CALL_BUDGET = 500

# Limite por página do catálogo. A Cito **clampa** o valor em 100: uma sondagem autorizada de
# uma chamada com ``limit=200`` (2026-08-31) devolveu ``meta.limit=100``, 100 itens e
# ``totalPages=9`` -- sem erro e sem aviso. Usar 100 custa 9 páginas para os 811 eventos, contra
# as 17 de ``limit=50`` (a captura da fixture versionada). A paginação nunca depende deste valor:
# ela segue ``meta.hasNextPage``/``meta.nextPage``, então um clamp da API não a quebra.
CATALOG_PAGE_LIMIT = 100


class CitoError(Exception):
    """Falha ao consumir a Cito API (erro HTTP, rede ou payload inválido)."""


class CitoRateLimitError(CitoError):
    """Rate-limit da Cito (HTTP 429): a quota do free tier foi excedida."""


class QuotaExceededError(RuntimeError):
    """Uma chamada à Cito excederia o teto configurado; interrompe antes de gastar."""


@dataclass
class CallBudget:
    """Contador do consumo de chamadas à Cito por execução, com teto configurável.

    ``charge`` é cobrado antes de **cada** fetch (inclusive em modo fixture, que evita a rede
    mas não o custo: o contador modela o consumo real do free tier). Ao atingir ``limit``,
    ``charge`` levanta ``QuotaExceededError`` **sem** incrementar além do teto -- a execução
    para antes de estourar a quota.
    """

    limit: int
    used: int = 0

    def charge(self) -> None:
        """Contabiliza uma chamada; levanta ``QuotaExceededError`` se estourar o teto."""
        if self.used >= self.limit:
            raise QuotaExceededError(
                f"Teto de {self.limit} chamadas à Cito atingido; execução interrompida."
            )
        self.used += 1


class CitoClient:
    """Cliente da Cito API para buscar um evento como DTO tipado.

    Em modo fixture (``fixture_dir``), lê o payload de um JSON local sem tocar a rede.
    Caso contrário, usa ``httpx`` com autenticação por token. ``transport`` permite
    injetar um ``httpx.MockTransport`` nos testes do caminho HTTP.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str,
        fixture_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        budget: CallBudget | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url
        self._fixture_dir = fixture_dir
        self._transport = transport
        self._budget = budget

    def _charge(self) -> None:
        """Cobra uma unidade do orçamento antes de um fetch, se um ``CallBudget`` foi injetado.

        Roda também em modo fixture (o custo modela o free tier). Levanta ``QuotaExceededError``
        antes de servir o payload quando o teto seria estourado. Sem orçamento, é no-op.
        """
        if self._budget is not None:
            self._budget.charge()

    def fetch_event(self, event_id: str) -> CitoEvent:
        """Busca o evento ``event_id`` e devolve um ``CitoEvent`` tipado.

        Em modo fixture lê ``{fixture_dir}/event_{event_id}.json``; caso contrário faz
        a chamada HTTP autenticada. Erros HTTP viram ``CitoError``/``CitoRateLimitError``.
        Cobra uma unidade do orçamento (``CallBudget``) antes do fetch.
        """
        self._charge()
        payload = (
            self._read_fixture(f"event_{event_id}.json")
            if self._fixture_dir is not None
            else self._get_json(_EVENTS_PATH, f"o evento {event_id!r}", params={"id": event_id})
        )
        return CitoEvent.model_validate(payload)

    def get_fighter(self, slug: str) -> CitoFighter:
        """Busca o perfil do lutador ``slug`` e devolve um ``CitoFighter`` tipado.

        O payload real é o envelope ``{success, data, meta}`` em camelCase, desembrulhado e
        traduzido ao domínio por ``CitoFighterDetail.to_fighter`` -- a forma foi medida na
        sondagem de 2026-09-01, quando a primeira execução real revelou que o objeto cru em
        snake_case que o M1 supunha nunca existiu na API (mesma classe de divergência que a
        ADR 0005 corrigiu para o endpoint de stats).

        Em modo fixture lê ``{fixture_dir}/fighter_{slug}.json``; caso contrário faz a
        chamada HTTP autenticada ``GET {base_url}/api/v1/ufc/fighters/{slug}``. Um envelope
        com ``success=false`` vira ``CitoError``; erros HTTP viram
        ``CitoError``/``CitoRateLimitError``, sem vazar a exceção crua do ``httpx``. Cobra uma
        unidade do orçamento (``CallBudget``) antes do fetch.
        """
        self._charge()
        payload = (
            self._read_fixture(f"fighter_{slug}.json")
            if self._fixture_dir is not None
            else self._get_json(f"{_FIGHTERS_PATH}/{slug}", f"o lutador {slug!r}")
        )
        envelope = CitoFighterEnvelope.model_validate(payload)
        if not envelope.success:
            raise CitoError(f"Cito retornou success=false para o perfil do lutador {slug!r}.")
        return envelope.data.to_fighter()

    def fetch_bout_stats(self, bout_id: str) -> CitoBoutStats:
        """Busca as stats granulares por canto da luta ``bout_id`` e devolve ``CitoBoutStats``.

        Em modo fixture lê ``{fixture_dir}/bout_stats_{bout_id}.json``; caso contrário faz a
        chamada HTTP autenticada ``GET {base_url}/api/v1/ufc/bouts/{bout_id}/stats``. Erros HTTP
        viram ``CitoError``/``CitoRateLimitError``, sem vazar a exceção crua do ``httpx``.
        Cobra uma unidade do orçamento (``CallBudget``) antes do fetch.
        """
        self._charge()
        payload = (
            self._read_fixture(f"bout_stats_{bout_id}.json")
            if self._fixture_dir is not None
            else self._get_json(f"{_BOUTS_PATH}/{bout_id}/stats", f"as stats da luta {bout_id!r}")
        )
        return CitoBoutStats.model_validate(payload)

    def fetch_event_stats(self, slug: str) -> CitoEventStats:
        """Busca os quatro blocos de stats do evento ``slug`` e devolve o DTO desembrulhado.

        Endpoint real da Cito (``GET {base_url}/api/v1/ufc/events/{slug}/stats`` -- o caminho
        **versionado**, o mesmo de ``fetch_event``): o payload é o envelope ``{success, data,
        meta}`` em camelCase, e ``data`` traz ``event`` (metadados + data local), ``bouts`` (o
        card, com o ``corner`` de cada lutador e o resultado), ``boutStats`` (totais) e
        ``roundStats`` (round-a-round), com os golpes como ``"L of A"`` e o tempo como ``"m:ss"``
        -- convertidos na borda pelos DTOs. Em modo fixture lê
        ``{fixture_dir}/event_stats_{slug}.json``. Um envelope com ``success=false`` vira
        ``CitoError`` (payload inválido, não silencioso); erros HTTP viram
        ``CitoError``/``CitoRateLimitError``. Cobra uma unidade do orçamento antes do fetch.
        """
        self._charge()
        payload = (
            self._read_fixture(f"event_stats_{slug}.json")
            if self._fixture_dir is not None
            else self._get_json(f"{_EVENTS_PATH}/{slug}/stats", f"as stats do evento {slug!r}")
        )
        envelope = CitoStatsEnvelope.model_validate(payload)
        if not envelope.success:
            raise CitoError(f"Cito retornou success=false para as stats do evento {slug!r}.")
        return envelope.data

    def fetch_event_catalog(
        self,
        *,
        limit: int = CATALOG_PAGE_LIMIT,
        date_from: date | None = None,
        date_to: date | None = None,
        cache: CatalogPageCache | None = None,
    ) -> list[CitoCatalogItem]:
        """Percorre TODAS as páginas do catálogo e devolve os itens na ordem em que vieram.

        O catálogo (``GET {base_url}/api/v1/ufc/events``) é a **fonte do identificador** de um
        evento na Cito: o ``slug`` e o ``id`` vêm de lá, nunca de uma regra sobre o nome
        persistido (SPEC 007, RF-04). Cobra uma unidade de ``CallBudget`` **por página** (~17
        páginas para os 811 eventos com ``limit=50``); ``date_from``/``date_to`` reduzem esse
        custo quando só uma janela interessa.

        Com um ``cache`` (``CatalogPageCache``), a paginação é **resumível**: uma página já
        baixada é servida do disco, sem tocar a rede e **sem cobrar o ``CallBudget``**. Em modo
        fixture lê ``{fixture_dir}/events_catalog_page_{page}.json``, sem tocar a rede.

        A paginação encerra em ``meta.hasNextPage`` falso; um ``meta.nextPage`` que não avança
        também encerra -- guarda contra laço infinito (e queima de quota) diante de um ``meta``
        inconsistente da API.
        """
        items: list[CitoCatalogItem] = []
        page = 1
        while True:
            envelope = self._fetch_catalog_page(page, limit, date_from, date_to, cache)
            items.extend(envelope.data)
            meta = envelope.meta
            if not meta.has_next_page or meta.next_page is None or meta.next_page <= page:
                return items
            page = meta.next_page

    @staticmethod
    def _catalog_cache_key(
        page: int, limit: int, date_from: date | None, date_to: date | None
    ) -> str:
        """Chave de cache de uma página, embutindo o recorte que a produziu.

        Páginas de recortes diferentes não são intercambiáveis (a página 2 de ``limit=50`` não
        tem os mesmos eventos que a de ``limit=200``), então ``limit``/``from``/``to`` entram na
        chave -- reusar o arquivo entre recortes serviria dado errado.
        """
        inicio = date_from.isoformat() if date_from is not None else "all"
        fim = date_to.isoformat() if date_to is not None else "all"
        return f"catalog_l{limit}_f{inicio}_t{fim}_p{page}"

    def _fetch_catalog_page(
        self,
        page: int,
        limit: int,
        date_from: date | None,
        date_to: date | None,
        cache: CatalogPageCache | None = None,
    ) -> CitoCatalogEnvelope:
        """Uma página do catálogo; cobra o orçamento no miss e valida o envelope.

        Com cache, o ``_charge`` só acontece no **miss** (é ``_fetch_catalog_payload`` que
        cobra) -- um hit não custa quota. Um envelope com ``success=false`` vira ``CitoError``
        (payload inválido, nunca silencioso), como em ``fetch_event_stats``.
        """
        if cache is not None:
            key = self._catalog_cache_key(page, limit, date_from, date_to)
            payload, _hit = cache.get_or_fetch(
                key, lambda: self._fetch_catalog_payload(page, limit, date_from, date_to)
            )
        else:
            payload = self._fetch_catalog_payload(page, limit, date_from, date_to)
        envelope = CitoCatalogEnvelope.model_validate(payload)
        if not envelope.success:
            raise CitoError(f"Cito retornou success=false para o catálogo (página {page}).")
        return envelope

    def _fetch_catalog_payload(
        self, page: int, limit: int, date_from: date | None, date_to: date | None
    ) -> object:
        """Busca o payload **cru** de uma página; cobra uma unidade do orçamento antes do fetch."""
        self._charge()
        if self._fixture_dir is not None:
            return self._read_fixture(f"events_catalog_page_{page}.json")
        params = {"page": str(page), "limit": str(limit)}
        if date_from is not None:
            params["from"] = date_from.isoformat()
        if date_to is not None:
            params["to"] = date_to.isoformat()
        return self._get_json(_EVENTS_PATH, f"o catálogo de eventos (página {page})", params=params)

    def _read_fixture(self, filename: str) -> object:
        """Lê e desserializa um JSON de fixture local; ausência vira ``CitoError`` explícito."""
        fixture_dir = self._fixture_dir
        if fixture_dir is None:  # pragma: no cover - guarda de tipo; o chamador já checa
            raise CitoError("Modo fixture sem diretório configurado.")
        path = fixture_dir / filename
        if not path.is_file():
            raise CitoError(f"Fixture não encontrada: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _get_json(self, path: str, target: str, *, params: dict[str, str] | None = None) -> object:
        """Faz o ``GET`` autenticado (``x-api-key``) e devolve o JSON; erros viram ``CitoError``.

        Rate-limit (429) vira ``CitoRateLimitError``; demais erros HTTP e falha de rede viram
        ``CitoError``, sem vazar a exceção crua do ``httpx``.
        """
        headers = {"x-api-key": self._token}
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers=headers,
                transport=self._transport,
            ) as client:
                response = client.get(path, params=params)
        except httpx.RequestError as exc:
            raise CitoError(f"Falha de rede ao consultar a Cito: {exc}") from exc

        self._raise_for_status(response, target)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response, target: str) -> None:
        if response.status_code == _HTTP_TOO_MANY_REQUESTS:
            raise CitoRateLimitError(f"Rate-limit da Cito (429) ao buscar {target}.")
        if response.is_error:
            raise CitoError(f"Erro HTTP {response.status_code} da Cito ao buscar {target}.")


def build_cito_client(*, fixture: bool, fixture_dir: Path, budget: CallBudget) -> CitoClient:
    """Constrói o ``CitoClient`` de um comando de ingestão: modo fixture ou HTTP autenticado.

    Extraído no M6 (Slice 06) quando a **quinta** cópia idêntica ia nascer -- ``incremental``,
    ``matching``, ``sync_catalog``, ``backfill_rounds`` e ``gap_sync`` construíam o cliente com
    exatamente o mesmo corpo. Movido como está, sem generalização: o modo fixture lê JSON local
    (0 quota real) e o modo HTTP usa o token e a base URL das ``settings``. O orçamento é
    cobrado a cada fetch inclusive em modo fixture (o custo modela o free tier).
    """
    if fixture:
        return CitoClient(
            token=settings.cito_api_token,
            base_url=settings.cito_base_url,
            fixture_dir=fixture_dir,
            budget=budget,
        )
    return CitoClient(token=settings.cito_api_token, base_url=settings.cito_base_url, budget=budget)
