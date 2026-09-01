"""Cliente HTTP da API JSON oficial da UFC -- o feed que o próprio ``ufc.com`` consome.

Ver a docstring de ``ingestion.ufc_official.dto`` para o **contrato medido** (endpoints, chaves
de topo, campos obrigatórios e opcionais, unidades, o que falha alto). Este módulo cuida só do
transporte.

Endpoints
---------
- ``GET {base_url}/api/v3/event/live/{eventId}.json`` -> ``UfcOfficialEvent`` (evento + card).
- ``GET {base_url}/api/v3/fight/live/{fightId}.json`` -> ``UfcOfficialFight`` (uma luta), ou
  ``UfcOfficialFightGranular`` por ``fetch_fight_granular`` quando o granular
  (``FightStats``/``RoundStats``) também for necessário.

``base_url`` default é ``settings.ufc_official_base_url``.

O que este cliente **não** tem, e por quê
-----------------------------------------
- **Sem autenticação.** A fonte é pública: sem chave, sem header, sem token no ``.env``.
- **Sem ``CallBudget`` e sem gate de quota.** Não há quota a proteger -- ao contrário da Cito
  (free tier de 500 req/mês, com gate humano). Nada aqui consome quota da Cito.
- **Sem modo fixture.** O ``fixture_dir`` do ``CitoClient`` existe para não gastar a quota; aqui
  não há o que economizar, então os testes usam ``httpx.MockTransport`` servindo a captura
  verbatim -- exercitam o caminho HTTP real em vez de um desvio.
- **Sem cache em disco.** O cache da Cito torna o backfill resumível sem re-gastar quota. A
  Slice 02 trouxe o seu (``ingestion.ufc_official.cache.OfficialEventCache``) e é dela que ele
  é responsabilidade; daqui sai apenas ``fetch_event_payload``, o JSON **cru** que o cache
  grava -- nunca a forma desserializada (a lição do M5).

Erros
-----
- ``UfcOfficialError`` -- transiente: HTTP não-200, falha de rede, corpo que não é JSON. O
  chamador pode pular o item e seguir.
- ``UfcOfficialContractError`` -- a fonte mudou de forma: campo estrutural ausente ou tipo
  trocado. A ingestão precisa **parar** até alguém olhar (RF-10).

Id inexistente **não** chega aqui como erro de transporte: a fonte responde **HTTP 200** com o
envelope vazio ``{"LiveEventDetail": {}}``, nunca 404 (medido em 2026-09-01 nos ids 0, 1344,
1345, 1350, 1400, 2000, 5000 e 99999). Este módulo não distingue ausência de conteúdo, então
``fetch_event`` trata o envelope vazio como quebra de contrato; quem varre ids deve pegar o cru
com ``fetch_event_payload`` e consultar ``dto.is_absent_event_payload`` **antes** de validar --
é o que a varredura da Slice 02 faz.

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
    UfcOfficialFightGranular,
    parse_event,
    parse_fight,
    parse_fight_granular,
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

    def fetch_event_payload(self, event_id: int) -> object:
        """Busca o evento ``event_id`` e devolve o JSON **cru**, sem validar a forma.

        Existe para a varredura da Slice 02 gravar em disco exatamente o que a fonte devolveu.
        Validar antes de cachear faria o cache guardar a forma que o DTO de hoje entende -- o
        defeito que escondeu uma quebra de contrato da Cito por meses (SPEC 007).
        """
        return self._get_json(EVENT_PATH.format(event_id=event_id))

    def fetch_event(self, event_id: int) -> UfcOfficialEvent:
        """Busca o evento ``event_id`` (com o card completo) e devolve o DTO tipado."""
        return parse_event(self.fetch_event_payload(event_id), event_id=event_id)

    def fetch_fight(self, fight_id: int) -> UfcOfficialFight:
        """Busca a luta ``fight_id`` e devolve o DTO tipado."""
        path = FIGHT_PATH.format(fight_id=fight_id)
        return parse_fight(self._get_json(path), fight_id=fight_id)

    def fetch_fight_granular(self, fight_id: int) -> UfcOfficialFightGranular:
        """Busca a luta ``fight_id`` lendo também ``FightStats``/``RoundStats``.

        Mesmo endpoint de ``fetch_fight``, DTO diferente: quem só precisa de canto e desfecho
        não paga a validação de 22 campos por linha de estatística nem depende dela.
        """
        path = FIGHT_PATH.format(fight_id=fight_id)
        return parse_fight_granular(self._get_json(path), fight_id=fight_id)

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
