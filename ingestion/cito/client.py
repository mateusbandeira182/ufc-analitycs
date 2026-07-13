"""Cliente HTTP tipado da Cito API (``mmaapi.dev``).

Busca um evento (``CitoEvent``), o perfil de um lutador (``CitoFighter``), as stats
granulares por canto de uma luta (``CitoBoutStats``) e as stats do endpoint real de evento
-- totais + round-a-round -- (``CitoEventStats`` via ``fetch_event_stats``). Dois caminhos:

- **Modo fixture** (``fixture_dir`` definido): lê um JSON local (``event_{id}.json`` /
  ``fighter_{slug}.json`` / ``bout_stats_{bout_id}.json``) em vez de tocar a rede -- é o
  caminho de teste e da execução de demonstração, e **não** consome a quota do free tier
  (500 req/mês).
- **Modo HTTP**: ``GET {base_url}/api/v1/ufc/events``,
  ``GET {base_url}/api/v1/ufc/fighters/{slug}`` e
  ``GET {base_url}/api/v1/ufc/bouts/{boutId}/stats`` autenticados por token, com o erro e o
  rate-limit convertidos em exceções tipadas (``CitoRateLimitError`` para 429, ``CitoError``
  para os demais), sem vazar a exceção crua do ``httpx``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from ingestion.cito.dto import (
    CitoBoutStats,
    CitoEvent,
    CitoEventStats,
    CitoFighter,
    CitoStatsEnvelope,
)

_EVENTS_PATH = "/api/v1/ufc/events"
_FIGHTERS_PATH = "/api/v1/ufc/fighters"
_BOUTS_PATH = "/api/v1/ufc/bouts"
_EVENT_STATS_PATH = "/events"
_HTTP_TOO_MANY_REQUESTS = 429

# Teto default de chamadas à Cito por execução, alinhado ao free tier (500 req/mês).
DEFAULT_CALL_BUDGET = 500


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

        Em modo fixture lê ``{fixture_dir}/fighter_{slug}.json``; caso contrário faz a
        chamada HTTP autenticada ``GET {base_url}/api/v1/ufc/fighters/{slug}``. Erros HTTP
        viram ``CitoError``/``CitoRateLimitError``, sem vazar a exceção crua do ``httpx``.
        Cobra uma unidade do orçamento (``CallBudget``) antes do fetch.
        """
        self._charge()
        payload = (
            self._read_fixture(f"fighter_{slug}.json")
            if self._fixture_dir is not None
            else self._get_json(f"{_FIGHTERS_PATH}/{slug}", f"o lutador {slug!r}")
        )
        return CitoFighter.model_validate(payload)

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
        """Busca ``boutStats``/``roundStats`` do evento ``slug`` e devolve o DTO desembrulhado.

        Endpoint real da Cito (``GET {base_url}/events/{slug}/stats``): o payload é o envelope
        ``{success, data, meta}`` em camelCase, com os golpes como ``"L of A"`` e o tempo como
        ``"m:ss"`` -- convertidos na borda pelos DTOs. Em modo fixture lê
        ``{fixture_dir}/event_stats_{slug}.json``. Um envelope com ``success=false`` vira
        ``CitoError`` (payload inválido, não silencioso); erros HTTP viram
        ``CitoError``/``CitoRateLimitError``. Cobra uma unidade do orçamento antes do fetch.
        """
        self._charge()
        payload = (
            self._read_fixture(f"event_stats_{slug}.json")
            if self._fixture_dir is not None
            else self._get_json(f"{_EVENT_STATS_PATH}/{slug}/stats", f"as stats do evento {slug!r}")
        )
        envelope = CitoStatsEnvelope.model_validate(payload)
        if not envelope.success:
            raise CitoError(f"Cito retornou success=false para as stats do evento {slug!r}.")
        return envelope.data

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
