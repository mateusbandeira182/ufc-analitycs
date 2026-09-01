"""Cliente HTTP da API JSON oficial da UFC -- o feed que o próprio ``ufc.com`` consome.

Ver a docstring de ``ingestion.ufc_official.dto`` para o **contrato medido** (endpoints, chaves
de topo, campos obrigatórios e opcionais, unidades, o que falha alto). Este módulo cuida só do
transporte.

Endpoints
---------
- ``GET {base_url}/api/v3/event/live/{eventId}.json`` -> ``UfcOfficialEvent`` (evento + card).
- ``GET {base_url}/api/v3/fight/live/{fightId}.json`` -> ``UfcOfficialFight`` (uma luta).

``base_url`` default é ``settings.ufc_official_base_url``.

O que este cliente **não** tem, e por quê
-----------------------------------------
- **Sem autenticação.** A fonte é pública: sem chave, sem header, sem token no ``.env``.
- **Sem ``CallBudget`` e sem gate de quota.** Não há quota a proteger -- ao contrário da Cito
  (free tier de 500 req/mês, com gate humano). Nada aqui consome quota da Cito.
- **Sem modo fixture.** O ``fixture_dir`` do ``CitoClient`` existe para não gastar a quota; aqui
  não há o que economizar, então os testes usam ``httpx.MockTransport`` servindo a captura
  verbatim -- exercitam o caminho HTTP real em vez de um desvio.
- **Sem cache em disco.** O cache da Cito torna o backfill resumível sem re-gastar quota. Se a
  Slice 02 precisar de retomada na varredura de ids, ela decide o formato -- e o que gravar
  precisa ser o **payload cru**, nunca a forma desserializada (a lição do M5).

Erros
-----
- ``UfcOfficialError`` -- transiente: HTTP não-200 (inclusive 404 de id inexistente), falha de
  rede, corpo que não é JSON. O chamador pode pular o item e seguir.
- ``UfcOfficialContractError`` -- a fonte mudou de forma: campo estrutural ausente ou tipo
  trocado. A ingestão precisa **parar** até alguém olhar (RF-10).

Nenhum caminho devolve ``None`` nem DTO meio preenchido, e nenhuma exceção crua do ``httpx``
escapa. A mensagem sempre nomeia o endpoint com o identificador -- a varredura da Slice 02 passa
por centenas de ids e um erro sem id vira caça ao tesouro no log.
"""

from __future__ import annotations

import httpx

from ingestion.ufc_official.dto import (
    EVENT_PATH,
    FIGHT_PATH,
    UfcOfficialError,
    UfcOfficialEvent,
    UfcOfficialFight,
    parse_event,
    parse_fight,
)
from mma_analytics.settings import settings


class UfcOfficialClient:
    """Cliente da API oficial da UFC. Sem autenticação e sem quota.

    ``transport`` existe para os testes injetarem um ``httpx.MockTransport``; em produção fica
    ``None`` e o ``httpx`` usa o transporte padrão.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url if base_url is not None else settings.ufc_official_base_url
        self._transport = transport

    def fetch_event(self, event_id: int) -> UfcOfficialEvent:
        """Busca o evento ``event_id`` (com o card completo) e devolve o DTO tipado."""
        path = EVENT_PATH.format(event_id=event_id)
        return parse_event(self._get_json(path), event_id=event_id)

    def fetch_fight(self, fight_id: int) -> UfcOfficialFight:
        """Busca a luta ``fight_id`` e devolve o DTO tipado."""
        path = FIGHT_PATH.format(fight_id=fight_id)
        return parse_fight(self._get_json(path), fight_id=fight_id)

    def _get_json(self, path: str) -> object:
        """Faz o ``GET`` e devolve o JSON cru; qualquer falha de transporte vira erro tipado.

        HTTP não-200, falha de rede e corpo que não é JSON viram ``UfcOfficialError``, sem vazar
        a exceção crua do ``httpx``. A validação de forma é do ``dto``, não daqui.
        """
        try:
            with httpx.Client(base_url=self._base_url, transport=self._transport) as client:
                response = client.get(path)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise UfcOfficialError(
                f"A fonte oficial respondeu {exc.response.status_code} em {path}."
            ) from exc
        except httpx.RequestError as exc:
            raise UfcOfficialError(
                f"Falha de rede ao consultar a fonte oficial em {path}: {exc}"
            ) from exc
        except ValueError as exc:
            raise UfcOfficialError(
                f"A fonte oficial devolveu um corpo que não é JSON em {path}: {exc}"
            ) from exc
