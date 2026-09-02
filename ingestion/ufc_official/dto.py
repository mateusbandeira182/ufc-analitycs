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
- Evento: ``EventId``, ``Name``, ``StartTime``, ``Status``, ``Organization`` (com
  ``OrganizationId``), ``FightCard``. **``TimeZone`` não é estrutural**: vem nulo em eventos
  antigos de promoções fora do escopo (medido no id 380, ``Gladiator FC - Day 2``, de 2004), e
  sem ele ``local_date`` levanta em vez de cair para a data UTC.
- Luta: ``FightId``, ``Fighters``.
- Lutador: ``FighterId``, ``Name`` (objeto ``FirstName``/``LastName``/``NickName``), ``Corner``
  e ``Outcome`` (o objeto; os campos **dentro** dele são opcionais).

Campos opcionais (ausência permanece **nula**, nunca zero nem sentinela)
------------------------------------------------------------------------
``TimeZone``, ``MMAId``, ``DOB``, ``Stance``, ``Height``, ``Reach``, ``Weight``,
``NickName``, e os dois campos de dentro de ``Outcome``. Medido em 450 lutadores: ``Reach``
falta em 50, ``MMAId`` em 69, ``DOB`` em 8, ``Stance`` em 2.

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

Ausência de id: HTTP 200 com envelope vazio, nunca 404
------------------------------------------------------
Um ``eventId`` que não existe devolve **200** com ``{"LiveEventDetail": {}}``. Não é quebra de
contrato -- é a forma desta fonte dizer "não existe" --, e é reconhecido por
``is_absent_event_payload`` **antes** da validação. Sem essa distinção a varredura da Slice 02
abortaria no primeiro id acima da fronteira.

O que falha alto (RF-10)
------------------------
``UfcOfficialContractError`` -- subtipo de ``UfcOfficialError`` -- para campo estrutural
ausente, tipo trocado (a validação é **estrita**: ``"74.5"`` onde se espera número levanta),
rótulo de canto fora de ``Red``/``Blue`` e ``TimeZone`` fora de ``GMT±HH:MM``. Nunca ``None``,
nunca DTO meio preenchido, nunca canto ou fuso default. A mensagem nomeia o endpoint com o
identificador.

Granular (``FightStats``/``RoundStats``) -- acrescentado pela Slice 07
----------------------------------------------------------------------
O endpoint de luta traz também a estatística granular, lida por ``UfcOfficialFightGranular`` e
``parse_fight_granular``. Ela fica num DTO **separado** de ``UfcOfficialFight`` porque só a
medição do portão a consome: quem precisa apenas de canto e desfecho não paga a validação de 22
campos por linha. As duas listas são estruturais (rename estoura), mas vazias em luta que ainda
não aconteceu -- vazio é ausência de conteúdo, ausente seria mudança de forma. O tempo de
controle chega como ``"m:ss"`` e é convertido a segundos **na borda**, como toda unidade aqui.
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


class UfcOfficialOrganization(BaseModel):
    """A promoção dona do evento -- campo de primeira classe do payload, nunca inferido.

    A fonte cobre PRIDE (2), WEC (3), Strikeforce (4), DREAM (8), K-1 (9), DWCS (67) e Road to
    UFC (68) além do UFC (1); o escopo do projeto é **só UFC** (e as fases de promoção,
    travadas em 2026-08-31, mantêm DWCS e Road to UFC para depois). Estrutural: presente em 85
    de 85 eventos sondados em 2026-09-01, com ``OrganizationId`` inteiro em todos.
    """

    model_config = _CONFIG

    organization_id: int = Field(alias="OrganizationId")
    name: str = Field(alias="Name")


class UfcOfficialEvent(BaseModel):
    """Um evento e o seu card. ``FightCard`` é estrutural -- ausência falha alto."""

    model_config = _CONFIG

    event_id: int = Field(alias="EventId")
    name: str = Field(alias="Name")
    # ``StartTime`` chega como string ISO com ``Z``: fora do modo estrito (ver ``_CONFIG``).
    start_time: datetime = Field(alias="StartTime", strict=False)
    status: str = Field(alias="Status")
    # ``TimeZone`` pode vir NULO (medido: id 380, ``Gladiator FC - Day 2``, de 2004). Sem ele não
    # há data de calendário confiável -- e a ausência NÃO degrada para UTC (ver ``local_date``).
    time_zone: str | None = Field(default=None, alias="TimeZone")
    organization: UfcOfficialOrganization = Field(alias="Organization")
    fight_card: list[UfcOfficialFight] = Field(alias="FightCard")

    @field_validator("time_zone")
    @classmethod
    def _valida_formato_do_fuso(cls, value: str | None) -> str | None:
        """Exige ``GMT±HH:MM`` quando presente; formato desconhecido falha alto.

        Ausência (``None``) é tolerada porque é real na fonte; **formato estranho não é**. Um
        fuso que mudou de forma erraria a data em silêncio se fosse aceito, enquanto a ausência
        é visível: ``local_date`` levanta e a varredura conta o evento.
        """
        if value is not None and _TIME_ZONE_PATTERN.match(value) is None:
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

        Sem ``TimeZone`` **levanta**, nunca cai para a data UTC: a ausência do fuso é conhecida
        na fonte (id 380) e um palpite de fuso erra o dia inteiro em card noturno das Américas.
        """
        if self.time_zone is None:
            raise UfcOfficialContractError(
                f"O evento {self.event_id} da fonte oficial não tem 'TimeZone', então não tem "
                "data de calendário confiável; assumir UTC deslocaria o evento em um dia."
            )
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
    ``RoundStats`` são declarados por ``UfcOfficialFightGranular``, que é quem os consome --
    quem só precisa de canto e desfecho não paga a validação de 22 campos por linha.

    O bloco ``Event`` deste endpoint é **reduzido** (sem ``Organization`` nem ``FightCard``),
    então não é modelado por ``UfcOfficialEvent`` -- forçar um molde único sobre duas formas
    diferentes é como o alias ``sigStrikes`` sobreviveu ao M5 inteiro.
    """

    model_config = _CONFIG

    fight: UfcOfficialFight = Field(alias="LiveFightDetail")


# ``ControlTime`` e os demais relógios da fonte chegam como ``"m:ss"`` (medido em 78 linhas de
# estatística de 2026-09-01). É a **mesma forma** que a Cito devolve, o que torna a conversão
# simétrica -- mas ela acontece na borda, aqui, e não no meio da comparação.
_CLOCK_PATTERN = re.compile(r"^(?P<minutes>\d+):(?P<seconds>[0-5]\d)$")


class UfcOfficialStatLine(BaseModel):
    """Estatística de um canto -- **a mesma forma** no total da luta e em cada round.

    Os 22 campos abaixo são **obrigatórios**, e essa é a decisão de projeto que mais importa
    neste módulo. Medido em 78 linhas do evento de referência: nenhum deles veio ausente ou
    nulo. Se algum fosse declarado opcional, um rename na fonte degradaria para ``None``, a
    linha sairia do denominador da medição pela regra "ausência não é divergência", e o
    relatório informaria concordância alta sobre um campo que deixou de existir. Falhar alto
    (RF-10) é o que impede esse resultado.

    A fonte devolve ~65 campos por linha (tempo por posição, acurácias, controle por posição em
    sete recortes); só os 22 que ``bout_fighter_rounds`` guarda são declarados. O resto fica
    preservado na captura por ``extra="ignore"``.

    Nomes de alvo e posição descrevem os golpes **significativos** (``SigHeadStrikes...``,
    ``SigDistanceStrikes...``), que é o vocabulário do ``ufcstats`` do qual as três linhagens
    (Kaggle, Cito e fonte oficial) descendem. ``TotalStrikes...`` é a contagem separada e
    mapeia para ``total_strikes_*``.
    """

    model_config = _CONFIG

    knockdowns: int = Field(alias="Knockdowns")
    sig_strikes_landed: int = Field(alias="SigStrikesLanded")
    sig_strikes_attempted: int = Field(alias="SigStrikesAttempted")
    takedowns_landed: int = Field(alias="TakedownsLanded")
    takedowns_attempted: int = Field(alias="TakedownsAttempted")
    submission_attempts: int = Field(alias="SubmissionsAttempted")
    reversals: int = Field(alias="Reversals")
    # UNIDADE NA BORDA: ``"m:ss"`` na fonte, segundos aqui (ver ``_para_segundos``).
    control_time_seconds: int = Field(alias="ControlTime")
    total_strikes_landed: int = Field(alias="TotalStrikesLanded")
    total_strikes_attempted: int = Field(alias="TotalStrikesAttempted")
    head_landed: int = Field(alias="SigHeadStrikesLanded")
    head_attempted: int = Field(alias="SigHeadStrikesAttempted")
    body_landed: int = Field(alias="SigBodyStrikesLanded")
    body_attempted: int = Field(alias="SigBodyStrikesAttempted")
    leg_landed: int = Field(alias="SigLegStrikesLanded")
    leg_attempted: int = Field(alias="SigLegStrikesAttempted")
    distance_landed: int = Field(alias="SigDistanceStrikesLanded")
    distance_attempted: int = Field(alias="SigDistanceStrikesAttempted")
    clinch_landed: int = Field(alias="SigClinchStrikesLanded")
    clinch_attempted: int = Field(alias="SigClinchStrikesAttempted")
    ground_landed: int = Field(alias="SigGroundStrikesLanded")
    ground_attempted: int = Field(alias="SigGroundStrikesAttempted")

    @field_validator("control_time_seconds", mode="before")
    @classmethod
    def _para_segundos(cls, value: object) -> int:
        """Converte ``"1:20"`` em ``80``; formato desconhecido levanta.

        Nunca degrada para zero nem para nulo: um tempo de controle zerado por engano entra
        como valor plausível na comparação numérica e inventa divergência (ou concordância)
        onde não há dado. Valor que não é string também levanta -- a fonte publica relógio, e
        um número cru aqui significaria que a forma mudou.
        """
        if not isinstance(value, str):
            raise ValueError(f"Tempo da fonte oficial deveria ser string 'm:ss': {value!r}.")
        match = _CLOCK_PATTERN.match(value.strip())
        if match is None:
            raise ValueError(
                f"Tempo da fonte oficial em formato inesperado: {value!r} (esperado 'm:ss')."
            )
        return int(match.group("minutes")) * 60 + int(match.group("seconds"))


class UfcOfficialFightStatLine(UfcOfficialStatLine):
    """Uma linha de ``FightStats``: os totais de um canto na luta inteira.

    O canto é identificado por ``FighterId`` -- ``FightStats`` **não** traz ``Corner``, que vive
    só em ``Fighters[]``. O nome sai do card da própria luta.
    """

    model_config = _CONFIG

    fighter_id: int = Field(alias="FighterId")


class UfcOfficialRoundStatLine(UfcOfficialStatLine):
    """Uma linha de ``RoundStats[].Rounds[]``: a forma do total, acrescida do número do round."""

    model_config = _CONFIG

    round_number: int = Field(alias="RoundNumber")


class UfcOfficialFighterRoundStats(BaseModel):
    """Um canto em ``RoundStats``: o ``FighterId`` e a lista de rounds dele.

    A fonte aninha por lutador (``[{FighterId, Rounds: [...]}]``), ao contrário da Cito, que
    devolve ``roundStats`` já achatado. O achatamento acontece na medição, não aqui.
    """

    model_config = _CONFIG

    fighter_id: int = Field(alias="FighterId")
    rounds: list[UfcOfficialRoundStatLine] = Field(alias="Rounds")


class UfcOfficialFightGranular(UfcOfficialFight):
    """A luta com o granular: o card (canto e desfecho) mais ``FightStats``/``RoundStats``.

    As duas listas são **estruturais** -- ausência da chave falha alto (RF-10) --, mas a lista
    **vazia** é ausência legítima de conteúdo: luta que ainda não aconteceu devolve
    ``"FightStats": []`` e ``"RoundStats": []`` (medido no ``FightId`` 13017, do UFC 331, em
    2026-09-01). Vazio e ausente não são a mesma coisa: o primeiro é a fonte dizendo "ainda não
    há estatística", o segundo seria a fonte tendo mudado de forma.
    """

    model_config = _CONFIG

    fight_stats: list[UfcOfficialFightStatLine] = Field(alias="FightStats")
    round_stats: list[UfcOfficialFighterRoundStats] = Field(alias="RoundStats")


class UfcOfficialFightGranularEnvelope(BaseModel):
    """Envelope de topo do endpoint de luta, lido com o granular declarado."""

    model_config = _CONFIG

    fight: UfcOfficialFightGranular = Field(alias="LiveFightDetail")


# Chave de topo do endpoint de evento. Isolada porque a **ausência** é reconhecida por ela
# antes de qualquer validação (ver ``is_absent_event_payload``).
_EVENT_ENVELOPE_KEY = "LiveEventDetail"


def is_absent_event_payload(payload: object) -> bool:
    """``{"LiveEventDetail": {}}`` é como esta fonte diz "este ``eventId`` não existe".

    A fonte **não** responde 404 para id inexistente: devolve **HTTP 200** com o envelope
    vazio. Medido em 2026-09-01 nos ids 0, 1344, 1345, 1350, 1400, 2000, 5000 e 99999 (o id
    1343 ainda era um evento real). Sem esta verificação, a varredura da Slice 02 trataria o
    primeiro id acima da fronteira como quebra de contrato e abortaria a execução inteira.

    A condição é **estrita**: só o objeto rigorosamente vazio conta como ausência. Um
    ``LiveEventDetail`` parcialmente preenchido continua sendo quebra de contrato e falha alto
    (RF-10) -- tratá-lo como ausência esconderia a quebra atrás de um número de cobertura.
    """
    if not isinstance(payload, dict):
        return False
    detail = payload.get(_EVENT_ENVELOPE_KEY)
    return isinstance(detail, dict) and not detail


def parse_event(payload: object, *, event_id: int) -> UfcOfficialEvent:
    """Valida o payload cru do endpoint de evento; forma inesperada vira erro tipado.

    Campo estrutural ausente ou de tipo trocado levanta ``UfcOfficialContractError`` -- nunca
    ``None``, nunca DTO meio preenchido (RF-10).
    """
    return _validado(UfcOfficialEventEnvelope, payload, EVENT_PATH.format(event_id=event_id)).event


def parse_fight(payload: object, *, fight_id: int) -> UfcOfficialFight:
    """Valida o payload cru do endpoint de luta; forma inesperada vira erro tipado."""
    return _validado(UfcOfficialFightEnvelope, payload, FIGHT_PATH.format(fight_id=fight_id)).fight


def parse_fight_granular(payload: object, *, fight_id: int) -> UfcOfficialFightGranular:
    """Valida o payload cru do endpoint de luta **com** ``FightStats``/``RoundStats``.

    Distinto de ``parse_fight`` de propósito: quem só precisa de canto e desfecho (as Slices 03
    a 06) não paga a validação de 22 campos por linha nem quebra numa luta cujo granular a
    fonte tenha deixado de publicar.
    """
    return _validado(
        UfcOfficialFightGranularEnvelope, payload, FIGHT_PATH.format(fight_id=fight_id)
    ).fight


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
