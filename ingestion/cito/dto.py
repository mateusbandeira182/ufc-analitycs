"""DTOs que tipam o payload da Cito API na borda (Pydantic v2).

O JSON da Cito é uma fronteira dinâmica: é tipado aqui, na entrada, antes de qualquer
uso no domínio -- nenhum ``Any`` propaga para dentro da ingestão. O evento
(``CitoEvent``) traz os metadados, os cantos das lutas e o **resultado** de cada luta
(método/round/tempo/vencedor); as stats granulares por canto vêm à parte, de
``GET /bouts/{boutId}/stats`` (``CitoBoutStats``).

Convenção de canto: em ``CitoBout.corners`` o índice 0 é o canto **vermelho** (red) e o
índice 1 é o **azul** (blue) -- é a fonte da verdade da atribuição de canto, com a qual
os rótulos ``corner`` das stats devem concordar.
"""

from __future__ import annotations

from datetime import UTC, date

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from apps.bouts.enums import Corner
from apps.fighters.enums import Stance
from ingestion.cito.parsers import parse_clock, parse_stat


class CitoCorner(BaseModel):
    """Um canto de uma luta: o lutador identificado por ``slug`` e nome de exibição."""

    model_config = ConfigDict(extra="ignore")

    slug: str
    name: str


class CitoBout(BaseModel):
    """Uma luta do card: id externo da Cito, o resultado e os dois cantos.

    O resultado (``method``/``finish_round``/``finish_time_seconds``/``weight_class``/
    ``winner_slug``) vem do payload do evento (``GET /events``), não das stats. Todos são
    opcionais: uma luta sem resultado (cartão futuro) degrada para vencedor/round nulos e
    método ``NO_CONTEST`` no mapeamento (ver ``ingestion.incremental.map_bout_core``). O
    ``winner_slug`` referencia o ``slug`` de um dos cantos.
    """

    model_config = ConfigDict(extra="ignore")

    bout_id: str
    corners: tuple[CitoCorner, CitoCorner]
    method: str | None = None
    finish_round: int | None = None
    finish_time_seconds: int | None = None
    weight_class: str | None = None
    winner_slug: str | None = None


class CitoFighterStats(BaseModel):
    """Box-score granular de um canto numa luta (``GET /bouts/{boutId}/stats``).

    Uma linha por lutador-por-luta (long): o ``corner`` liga ao ``fighter_id`` já resolvido
    e as métricas são as daquela luta (nunca médias). Métrica ausente no payload vira
    ``None`` -- não se inventa zero.
    """

    model_config = ConfigDict(extra="ignore")

    corner: Corner
    fighter_slug: str
    knockdowns: int | None = None
    sig_strikes_landed: int | None = None
    sig_strikes_attempted: int | None = None
    takedowns_landed: int | None = None
    takedowns_attempted: int | None = None
    submission_attempts: int | None = None
    control_time_seconds: int | None = None


class CitoBoutStats(BaseModel):
    """As stats de uma luta: o id externo da Cito e a linha de cada canto (red/blue)."""

    model_config = ConfigDict(extra="ignore")

    bout_id: str
    fighters: list[CitoFighterStats]


class CitoEvent(BaseModel):
    """Um evento do UFC vindo da Cito: metadados + a lista de lutas com os dois cantos."""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    name: str
    date: date  # data de calendário do evento (sem instante/timezone)
    bouts: list[CitoBout]


class CitoFighter(BaseModel):
    """Perfil de um lutador da Cito (``GET /fighters/{slug}``).

    A ``date_of_birth`` é a base do desempate da entity resolution cross-source; pode
    vir ausente (``None``), caso em que a política de matching degrada para o nome
    normalizado (ver ``ingestion.entity_resolution``). O cartel (``wins``/``losses``/
    ``draws``) alimenta os campos NOT NULL de ``Fighter`` na criação de um lutador novo;
    ausente no payload, cada um degrada para ``0``.
    """

    model_config = ConfigDict(extra="ignore")

    slug: str
    name: str
    date_of_birth: date | None = None
    nickname: str | None = None
    height_cm: int | None = None
    reach_cm: int | None = None
    stance: Stance | None = None
    wins: int = 0
    losses: int = 0
    draws: int = 0

    @field_validator("stance", mode="before")
    @classmethod
    def _coerce_stance(cls, value: object) -> Stance | None:
        """Normaliza o rótulo de stance da Cito ao enum; fora do enum (ou vazio) -> ``None``.

        A grafia da Cito pode variar em caixa (``"Orthodox"``); o rótulo é baixado à caixa
        antes de casar com o enum. Rótulos não previstos degradam para ``None`` em vez de
        estourar a validação -- consistente com o tratamento de stance do seed (ADR 0002).
        """
        if value is None or isinstance(value, Stance):
            return value
        try:
            return Stance(str(value).strip().casefold())
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# DTOs do endpoint real ``GET /api/v1/ufc/events/{slug}/stats`` (envelope camelCase).
#
# Aditivos: os DTOs acima (``CitoEvent``/``CitoBout``/``CitoBoutStats``/...) são consumidos
# pelo M1 (``ingestion.incremental``) e permanecem intactos. Aqui, a Cito real embrulha as
# stats granulares num envelope ``{success, data, meta}``, usa camelCase e expressa golpes como
# ``"L of A"`` e tempo como ``"m:ss"`` -- convertidos na borda pelos parsers, sem propagar ``Any``.
#
# ``data`` traz **quatro** blocos: ``event`` (metadados + data local), ``bouts`` (o card, com o
# ``corner`` de cada lutador e o resultado), ``boutStats`` (totais por lutador-por-luta) e
# ``roundStats`` (round-a-round). A forma foi confirmada contra a API real em 2026-08-31 e está
# versionada em ``tests/ingestion/fixtures/event_stats_ufc-fight-night-august-22-2026.json``;
# ver ADR 0005 para as três divergências de contrato que o M5 carregava.
# ---------------------------------------------------------------------------

_CAMEL_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

# Campos de golpe expressos como ``"landed of attempted"`` no payload real da Cito.
_SPLIT_FIELDS = (
    "sig_strikes",
    "total_strikes",
    "head",
    "body",
    "leg",
    "distance",
    "clinch",
    "ground",
    "takedowns",
)


class CitoEventBlock(BaseModel):
    """Metadados do evento no bloco ``data.event`` do endpoint real de stats.

    A ``event_date`` é a data **local** do evento -- a mesma grandeza que o seed persistiu em
    ``events.date`` e a mesma que o slug usa. Difere da data UTC de ``starts_at`` sempre que o
    card noturno nos EUA atravessa a meia-noite UTC (o caso da maioria dos Fight Nights), o que
    torna insegura qualquer derivação de slug por regra sobre o nome (ver ADR 0005).
    """

    model_config = _CAMEL_CONFIG

    id: str
    slug: str
    title: str
    status: str  # "scheduled" | "completed"
    event_date: date
    starts_at: AwareDatetime | None = None


class CitoBoutFighterRef(BaseModel):
    """Um canto do card (``bouts[].fighters[]``) -- é daqui que o ``corner`` vem.

    As linhas de ``boutStats``/``roundStats`` da API real **não** trazem ``corner``; o rótulo de
    canto só existe aqui, no card. Ver ADR 0005.
    """

    model_config = _CAMEL_CONFIG

    fighter_slug: str
    fighter_name: str | None = None
    corner: Corner
    outcome: str | None = None  # "win" | "loss" | "draw" | ...


class CitoBoutBlock(BaseModel):
    """Uma luta do card (``bouts[]``): contexto + resultado, como a Cito devolve.

    Só os campos com consumidor concreto entram: ``venue``/``odds``/``dataAvailability`` e demais
    blocos do payload seguem tolerados por ``extra="ignore"`` e serão adicionados quando (e se)
    alguém os consumir.
    """

    model_config = _CAMEL_CONFIG

    id: str
    card_section: str | None = None  # "Main Card" | "Prelims"
    card_section_order: int | None = None
    bout_order: int | None = None
    weight_class: str | None = None
    title_bout: bool | None = None
    status: str | None = None
    is_cancelled: bool = False
    winner_fighter_slug: str | None = None
    result_round: int | None = None
    result_time_seconds: int | None = Field(default=None, validation_alias="resultTime")
    method: str | None = None  # "Decision - Unanimous" | "KO/TKO" | "Submission"
    method_details: str | None = None
    fighters: list[CitoBoutFighterRef]

    @field_validator("result_time_seconds", mode="before")
    @classmethod
    def _clock(cls, value: object) -> int | None:
        """Converte ``"5:00"`` -> segundos na borda; ausência -> ``None``."""
        return parse_clock(value if value is None or isinstance(value, str) else str(value))


class CitoBoutStatLine(BaseModel):
    """Totais de um canto numa luta do endpoint real (``boutStats``).

    Uma linha por lutador-por-luta (long): o lutador é identificado por ``fighter_slug`` (a API
    real não traz ``corner`` aqui -- o canto vem de ``bouts[].fighters[]``) e os splits de golpe
    chegam como tuplas ``(landed, attempted)`` após o parse na borda. Split ausente degrada para
    ``(None, None)`` (não se inventa zero); string mal-formada levanta ``CitoParseError``.
    """

    model_config = _CAMEL_CONFIG

    bout_id: str
    fighter_slug: str
    fighter_name: str | None = None
    knockdowns: int | None = None
    submission_attempts: int | None = None
    reversals: int | None = None
    sig_strikes: tuple[int | None, int | None] = Field(
        default=(None, None), validation_alias="significantStrikes"
    )
    total_strikes: tuple[int | None, int | None] = (None, None)
    head: tuple[int | None, int | None] = (None, None)
    body: tuple[int | None, int | None] = (None, None)
    leg: tuple[int | None, int | None] = (None, None)
    distance: tuple[int | None, int | None] = (None, None)
    clinch: tuple[int | None, int | None] = (None, None)
    ground: tuple[int | None, int | None] = (None, None)
    takedowns: tuple[int | None, int | None] = (None, None)
    control_time_seconds: int | None = Field(default=None, validation_alias="controlTime")

    @field_validator(*_SPLIT_FIELDS, mode="before")
    @classmethod
    def _split(cls, value: object) -> tuple[int | None, int | None]:
        """Converte ``"L of A"`` -> ``(L, A)`` na borda; ausência -> ``(None, None)``."""
        return parse_stat(value if value is None or isinstance(value, str) else str(value))

    @field_validator("control_time_seconds", mode="before")
    @classmethod
    def _clock(cls, value: object) -> int | None:
        """Converte ``"m:ss"`` -> segundos na borda; ausência -> ``None``."""
        return parse_clock(value if value is None or isinstance(value, str) else str(value))


class CitoRoundStatLine(CitoBoutStatLine):
    """Stats de um canto num round específico (``roundStats``): a forma do total + ``round``."""

    round: int


class CitoEventStats(BaseModel):
    """Os quatro blocos que o endpoint real devolve numa única chamada.

    ``event`` (metadados, com a data local) e ``bouts`` (o card com resultado e o ``corner`` de
    cada lutador) são **obrigatórios**: são estruturais no envelope, então a ausência precisa
    falhar alto, não degradar. ``round_stats``, ao contrário, degrada para lista vazia -- um
    evento sem round-a-round é ausência legítima de dado, não payload quebrado.
    """

    model_config = _CAMEL_CONFIG

    event: CitoEventBlock
    bouts: list[CitoBoutBlock]
    bout_stats: list[CitoBoutStatLine]
    round_stats: list[CitoRoundStatLine] = []


class CitoStatsEnvelope(BaseModel):
    """Envelope ``{success, data, meta}`` do endpoint real; ``data`` é o ``CitoEventStats``."""

    model_config = _CAMEL_CONFIG

    success: bool
    data: CitoEventStats


# ---------------------------------------------------------------------------
# DTOs do catálogo paginado ``GET /api/v1/ufc/events`` (M6, SPEC 007, Slice 03).
#
# Aditivos: os DTOs acima permanecem intactos. O catálogo é a **fonte do identificador** do
# evento na Cito -- o slug nunca é derivado por regra a partir do nome persistido (RF-04).
# ---------------------------------------------------------------------------


class CitoCatalogItem(BaseModel):
    """Item do catálogo ``GET /api/v1/ufc/events`` -- a fonte do identificador do evento.

    ``starts_at`` é o instante UTC do início; ``event_date`` é a **data local** do evento --
    a mesma que o slug usa ('ufc-fight-night-august-22-2026' para um ``starts_at`` de
    2026-08-23 UTC) e a mesma grandeza que o seed persistiu em ``events.date``. As duas
    divergem em um dia sempre que o card noturno nos EUA atravessa a meia-noite UTC, o que
    é o caso da maioria dos Fight Nights. Por isso o casamento usa ``local_date`` e uma
    janela de tolerância -- e o slug **nunca** é derivado (ver
    ``ingestion.cito.matching.resolve_event_match``).
    """

    model_config = _CAMEL_CONFIG

    id: str  # uuid textual -> events.cito_event_id
    slug: str  # -> events.cito_slug
    title: str
    short_title: str | None = None
    status: str  # "scheduled" | "completed"
    starts_at: AwareDatetime
    event_date: date | None = None  # data local do evento, quando o catálogo a expõe
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    location_text: str | None = None

    @property
    def local_date(self) -> date:
        """Data de calendário **local** do evento -- a base da janela de casamento.

        Usa ``event_date`` quando o catálogo a expõe (o caso de todos os itens da captura
        real de 2026-08-31); na ausência, degrada para a data UTC de ``starts_at``, que a
        janela de tolerância cobre. Nunca inventa data.
        """
        return (
            self.event_date
            if self.event_date is not None
            else self.starts_at.astimezone(UTC).date()
        )


class CitoCatalogMeta(BaseModel):
    """Bloco ``meta`` da página do catálogo; só o necessário para paginar."""

    model_config = _CAMEL_CONFIG

    page: int
    limit: int
    total: int
    total_pages: int
    has_next_page: bool
    next_page: int | None = None


class CitoCatalogEnvelope(BaseModel):
    """Envelope ``{success, data, meta}`` de uma página do catálogo paginado."""

    model_config = _CAMEL_CONFIG

    success: bool
    data: list[CitoCatalogItem]
    meta: CitoCatalogMeta
