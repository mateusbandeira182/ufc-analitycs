"""Fronteira dinâmica tipada da API oficial da UFC: o JSON cru vira DTO Pydantic aqui.

**Nenhum alias deste módulo foi suposto.** Todos vêm de capturas reais de 2026-09-01, guardadas
verbatim em ``tests/ingestion/fixtures/ufc_official/`` (procedência completa no ``README.md``
de lá) -- a regra vale porque 3 de 3 DTOs escritos sem sondar a API real, na SPEC 007, estavam
errados, e sempre em silêncio: um alias que não casa degrada para ``None``, não levanta.

Endpoints e envelopes
---------------------
==================================== ============================ ==========================
Endpoint                             Chave de topo                DTO
==================================== ============================ ==========================
``/api/v3/event/live/{eventId}.json`` ``LiveEventDetail``          ``UfcOfficialEvent``
``/api/v3/fight/live/{fightId}.json`` ``LiveFightDetail``          ``UfcOfficialFight``
==================================== ============================ ==========================

Sem autenticação, sem chave, sem rate limit observado. Não é a Cito e não consome quota.

Campos estruturais (ausência **falha alto**)
--------------------------------------------
- Evento: ``EventId``, ``Name``, ``StartTime``, ``TimeZone``, ``Status``, ``FightCard``.
- Luta: ``FightId``, ``Fighters``.
- Lutador: ``FighterId``, ``Name`` (objeto ``FirstName``/``LastName``/``NickName``), ``Corner``
  e ``Outcome`` (o objeto; os campos **dentro** dele são opcionais).

Campos opcionais (ausência permanece **nula**, nunca zero nem sentinela)
------------------------------------------------------------------------
``MMAId``, ``DOB``, ``Stance``, ``Height``, ``Reach``, ``Weight``, ``NickName``, e os dois
campos de dentro de ``Outcome``. Medido em 450 lutadores: ``Reach`` falta em 50, ``MMAId`` em
69, ``DOB`` em 8, ``Stance`` em 2.

Unidades: **a unidade da borda está no nome do campo**
------------------------------------------------------
``height_inches`` e ``reach_inches`` em **polegadas**, ``weight_lbs`` em **libras**. Nenhuma
conversão acontece aqui -- a conversão para cm/kg (com tolerância de ±2 cm na comparação com o
nosso dado) é da Sprint 008-05. O alcance admite meia polegada (Darren Till: ``74.5``), então
arredondar na borda perderia informação real. Converter aqui esconderia a unidade da fonte, que
foi exatamente como a Cito devolveu polegadas onde o DTO esperava centímetros (SPEC 007).

``corner`` e ``outcome`` são campos DISTINTOS
----------------------------------------------
``Corner`` (``"Red"``/``"Blue"``, 225 x 225 nos 450 lutadores medidos) é o canto; ``Outcome`` é
o desfecho (``"Win"``, ``"Loss"``, ``"Draw"``, ``"No Contest"``, ou ``null``). Um **não** é
derivado do outro, e nada neste módulo os colapsa num "vencedor". É a razão de ser da SPEC 008:
o ``corner`` da Cito é o desfecho renomeado (99,7% de vitórias do vermelho) e vazava o rótulo
que o modelo tenta prever. A prova mais direta está na captura de evento ``Upcoming``, onde o
canto vem preenchido e o desfecho vem nulo.

A data do evento é a LOCAL, não a UTC
--------------------------------------
``StartTime`` é um instante UTC; a data de calendário sai dele aplicando ``TimeZone``
(``GMT±HH:MM``) e é exposta por ``UfcOfficialEvent.local_date``. UFC 184 começou em
``2015-03-01T00:00Z`` sob ``GMT-08:00`` e é o evento de **2015-02-28**. É sobre a data local que
a janela da RF-03 (2010-03-21 em diante) é decidida.

O que falha alto (RF-10)
------------------------
``UfcOfficialContractError`` -- subtipo de ``UfcOfficialError`` -- para campo estrutural
ausente, tipo trocado (a validação é **estrita**: ``"74.5"`` onde se espera número levanta),
rótulo de canto fora de ``Red``/``Blue`` e ``TimeZone`` fora de ``GMT±HH:MM``. Nunca ``None``,
nunca DTO meio preenchido, nunca canto ou fuso default. A mensagem nomeia o endpoint com o
identificador.

Deliberadamente fora desta slice
--------------------------------
``FightStats`` e ``RoundStats`` (presentes no endpoint de luta) **não** são declarados: são o
portão condicional da Slice 07 e declará-los agora criaria campo sem consumidor. Ficam
preservados na captura por ``extra="ignore"``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from apps.bouts.enums import Corner

# Caminhos dos dois endpoints. Vivem aqui, junto do contrato que descrevem, e são importados
# pelo cliente -- uma definição só, usada tanto na requisição quanto na mensagem de erro.
EVENT_PATH = "/api/v3/event/live/{event_id}.json"
FIGHT_PATH = "/api/v3/fight/live/{fight_id}.json"

# ``strict=True`` é uma decisão MEDIDA, não uma preferência: sem ela a validação lax do Pydantic
# aceitaria ``"74.5"`` onde se espera número e uma mudança de contrato passaria em silêncio --
# a classe de erro que ficou meses invisível no M5. Medido no Pydantic 2.13: em modo estrito o
# dicionário aninhado ainda vira modelo e ``int`` ainda é aceito onde se espera ``float``
# (``185`` e ``185.0`` são o mesmo dado, artefato do serializador), enquanto ``str``->número,
# número->``str`` e ``dict``->``list`` passam a levantar. Data e instante ficam explicitamente
# fora do modo estrito (``strict=False`` no campo), porque a fonte os devolve como string ISO.
_CONFIG = ConfigDict(extra="ignore", populate_by_name=True, strict=True)


class UfcOfficialError(Exception):
    """Falha ao consumir a API oficial da UFC (erro HTTP, rede ou payload inválido)."""


class UfcOfficialContractError(UfcOfficialError):
    """O payload não tem a forma esperada: campo estrutural ausente ou tipo trocado.

    Distinta da base de propósito: um erro de rede/HTTP é transiente e o chamador pode pular o
    item; uma quebra de contrato significa que a fonte mudou e a ingestão inteira precisa parar
    até alguém olhar. RF-10.
    """


# ``TimeZone`` veio como ``GMT±HH:MM`` em 24 de 24 eventos sondados. Formato desconhecido
# **falha alto** em vez de degradar para UTC: a data do evento sai deste deslocamento, e errá-la
# desloca o evento em um dia (ver ``UfcOfficialEvent.local_date``).
_TIME_ZONE_PATTERN = re.compile(r"^GMT(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})$")

# Rótulos de canto do payload oficial, medidos em 450 lutadores (225 ``Red`` x 225 ``Blue``).
_CORNER_BY_LABEL = {"Red": Corner.RED, "Blue": Corner.BLUE}

_EnvelopeT = TypeVar("_EnvelopeT", bound=BaseModel)


class UfcOfficialFighterName(BaseModel):
    """Nome do lutador na fonte oficial, que é um **objeto**, não uma string."""

    model_config = _CONFIG

    first_name: str = Field(alias="FirstName")
    last_name: str = Field(alias="LastName")
    nick_name: str | None = Field(default=None, alias="NickName")


class UfcOfficialOutcome(BaseModel):
    """Desfecho de um lutador numa luta -- distinto do canto, nunca derivado dele.

    O objeto é **estrutural** (presente em 450 de 450 lutadores sondados), mas os seus campos
    são nulos enquanto a luta não aconteceu: um evento ``Upcoming`` devolve
    ``{"OutcomeId": null, "Outcome": null}`` com o ``Corner`` já preenchido. Ausência
    permanece nula -- nunca sentinela, nunca zero.
    """

    model_config = _CONFIG

    outcome_id: int | None = Field(default=None, alias="OutcomeId")
    label: str | None = Field(default=None, alias="Outcome")


class UfcOfficialFighter(BaseModel):
    """Um canto de uma luta na fonte oficial.

    ``corner`` e ``outcome`` são campos **DISTINTOS** do payload -- nunca derive um do outro,
    nem colapse os dois num "vencedor". A separação é a razão de ser da SPEC 008: o ``corner``
    da Cito é o desfecho renomeado (99,7% de vitórias do vermelho) e vazava o rótulo que o
    modelo tenta prever.
    """

    model_config = _CONFIG

    fighter_id: int = Field(alias="FighterId")
    mma_id: int | None = Field(default=None, alias="MMAId")
    name: UfcOfficialFighterName = Field(alias="Name")
    corner: Corner = Field(alias="Corner")
    outcome: UfcOfficialOutcome = Field(alias="Outcome")
    # Antropometria: UNIDADE DA BORDA NO NOME. Nenhuma conversão acontece aqui -- a conversão
    # para cm/kg é da Sprint 008-05. Converter na borda esconderia a unidade da fonte, que foi
    # exatamente como a Cito devolveu polegadas onde o DTO esperava centímetros (SPEC 007).
    height_inches: float | None = Field(default=None, alias="Height")
    reach_inches: float | None = Field(default=None, alias="Reach")
    weight_lbs: float | None = Field(default=None, alias="Weight")
    # ``DOB`` chega como string ISO (``AAAA-MM-DD``): fora do modo estrito, como o instante.
    date_of_birth: date | None = Field(default=None, alias="DOB", strict=False)
    stance: str | None = Field(default=None, alias="Stance")

    @field_validator("corner", mode="before")
    @classmethod
    def _map_corner(cls, value: object) -> Corner:
        """Traduz o rótulo do payload (``"Red"``/``"Blue"``) para o domínio.

        Valor desconhecido levanta ``ValueError`` -- nunca degrada para um canto default: um
        canto errado grava dado falso na coluna que é o **alvo** do modelo.
        """
        corner = _CORNER_BY_LABEL.get(value) if isinstance(value, str) else None
        if corner is None:
            raise ValueError(
                f"Canto desconhecido na fonte oficial: {value!r}; esperado 'Red' ou 'Blue'."
            )
        return corner


class UfcOfficialFight(BaseModel):
    """Uma luta: o identificador e os dois cantos. Ambos estruturais."""

    model_config = _CONFIG

    fight_id: int = Field(alias="FightId")
    fighters: list[UfcOfficialFighter] = Field(alias="Fighters")


class UfcOfficialEvent(BaseModel):
    """Um evento e o seu card. ``FightCard`` é estrutural -- ausência falha alto."""

    model_config = _CONFIG

    event_id: int = Field(alias="EventId")
    name: str = Field(alias="Name")
    # ``StartTime`` chega como string ISO com ``Z``: fora do modo estrito (ver ``_CONFIG``).
    start_time: datetime = Field(alias="StartTime", strict=False)
    time_zone: str = Field(alias="TimeZone")
    status: str = Field(alias="Status")
    fight_card: list[UfcOfficialFight] = Field(alias="FightCard")

    @field_validator("time_zone")
    @classmethod
    def _valida_formato_do_fuso(cls, value: str) -> str:
        """Exige ``GMT±HH:MM``; formato desconhecido falha alto em vez de virar UTC."""
        if _TIME_ZONE_PATTERN.match(value) is None:
            raise ValueError(
                f"Fuso em formato inesperado na fonte oficial: {value!r}; esperado 'GMT±HH:MM'."
            )
        return value

    @property
    def local_date(self) -> date:
        """Data de calendário do evento, derivada do instante UTC com o fuso **local**.

        ``StartTime`` é um instante UTC e a sua data **não** é a data do evento: UFC 184 começou
        em ``2015-03-01T00:00Z`` sob ``GMT-08:00`` e a nossa base o registra corretamente em
        2015-02-28. É a mesma divergência já medida na Cito (``startsAt`` UTC contra
        ``eventDate`` local, ADR 0005), e é sobre esta data que a janela da RF-03 é decidida.
        """
        match = _TIME_ZONE_PATTERN.match(self.time_zone)
        if match is None:  # pragma: no cover - o validador de campo já rejeitou o formato
            raise ValueError(f"Fuso em formato inesperado: {self.time_zone!r}.")
        offset = timedelta(
            hours=int(match.group("hours")),
            minutes=int(match.group("minutes")),
        )
        if match.group("sign") == "-":
            offset = -offset
        return self.start_time.astimezone(timezone(offset)).date()


class UfcOfficialEventEnvelope(BaseModel):
    """Envelope de topo do endpoint de evento: ``{"LiveEventDetail": {...}}``."""

    model_config = _CONFIG

    event: UfcOfficialEvent = Field(alias="LiveEventDetail")


class UfcOfficialFightEnvelope(BaseModel):
    """Envelope de topo do endpoint de luta: ``{"LiveFightDetail": {...}}``.

    O objeto de luta deste endpoint **não** é o mesmo do card do evento: ele acrescenta
    ``Event``, ``OfficialStats``, ``FightStats`` e ``RoundStats``, e não traz
    ``FightNightTracking``. Só a parte estrutural (``FightId`` + ``Fighters``, com forma de
    lutador idêntica nos dois caminhos, medida) é compartilhada com ``UfcOfficialFight``; o
    resto fica de fora por ``extra="ignore"``, sem consumidor nesta slice. ``FightStats`` e
    ``RoundStats`` são o portão condicional da Slice 07 e não são declarados aqui.

    O bloco ``Event`` deste endpoint é **reduzido** (sem ``Organization`` nem ``FightCard``),
    então não é modelado por ``UfcOfficialEvent`` -- forçar um molde único sobre duas formas
    diferentes é como o alias ``sigStrikes`` sobreviveu ao M5 inteiro.
    """

    model_config = _CONFIG

    fight: UfcOfficialFight = Field(alias="LiveFightDetail")


def parse_event(payload: object, *, event_id: int) -> UfcOfficialEvent:
    """Valida o payload cru do endpoint de evento; forma inesperada vira erro tipado.

    Campo estrutural ausente ou de tipo trocado levanta ``UfcOfficialContractError`` -- nunca
    ``None``, nunca DTO meio preenchido (RF-10).
    """
    return _validado(UfcOfficialEventEnvelope, payload, EVENT_PATH.format(event_id=event_id)).event


def parse_fight(payload: object, *, fight_id: int) -> UfcOfficialFight:
    """Valida o payload cru do endpoint de luta; forma inesperada vira erro tipado."""
    return _validado(UfcOfficialFightEnvelope, payload, FIGHT_PATH.format(fight_id=fight_id)).fight


def _validado(modelo: type[_EnvelopeT], payload: object, endpoint: str) -> _EnvelopeT:
    """Valida o payload no envelope, traduzindo a falha do Pydantic em erro tipado do módulo.

    A mensagem **nomeia o endpoint com o identificador**: a Slice 02 varre centenas de ids e um
    erro de contrato sem o id vira caça ao tesouro no log. A ``ValidationError`` original vira
    causa (``raise ... from``), então o detalhe campo a campo não se perde.
    """
    try:
        return modelo.model_validate(payload)
    except ValidationError as exc:
        raise UfcOfficialContractError(
            f"Payload da fonte oficial fora do contrato em {endpoint}: {exc}"
        ) from exc
