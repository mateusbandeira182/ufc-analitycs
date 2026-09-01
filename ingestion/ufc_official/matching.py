"""Casamento evento persistido <-> item do catálogo da fonte oficial (SPEC 008, Slice 02).

Funções **puras**: não tocam o banco, não escrevem, não conhecem a forma do payload (a projeção
do DTO em ``OfficialCatalogItem`` é da descoberta, ``ingestion.ufc_official.discovery``).

Política mais restritiva que a da Cito, de propósito
----------------------------------------------------
O casamento da Cito (``ingestion.cito.matching.resolve_event_match``) aceitava um candidato
único na janela de data **sem** concordância de nome, marcando-o ``matched_by_name=False`` para
inspeção humana posterior. Aqui isso **não** acontece: sem corroboração de nome, nada é escrito.

A razão é o consumidor a jusante. Lá, o slug errado custava uma chamada de quota desperdiçada;
aqui, a Slice 04 corrige o **canto autoritativo** a partir de ``events.ufc_event_id``, e um id
errado corrigiria o canto com o card do evento errado -- gravando dado falso exatamente na
coluna que é o alvo do modelo. **Id errado é pior que id ausente**, então o evento sem
corroboração fica em revisão, contado e nomeado no relatório.

Por que não parametrizar a função da Cito
------------------------------------------
Os tipos de item são diferentes e os critérios já divergem (o parágrafo acima). Generalizar
exigiria um ``Protocol`` estrutural para um segundo uso, acoplando a política das duas fontes
num ponto em que elas deliberadamente discordam. Se uma terceira fonte aparecer, a extração se
justifica então. O que **é** compartilhado -- ``normalize_event_name`` -- mora em
``ingestion.normalize``, porque uma normalização divergente entre as fontes quebraria o
casamento em silêncio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from apps.events.models import Event
from ingestion.normalize import normalize_event_name

# A data persistida em ``events.date`` é a data LOCAL do evento (seed do Kaggle/ufcstats) e a da
# fonte oficial é ``UfcOfficialEvent.local_date`` (o instante UTC de ``StartTime`` sob o fuso de
# ``TimeZone``). Uma janela simétrica de um dia absorve a divergência residual entre as fontes.
# NÃO é heurística de nome: a data é dado real das duas pontas. Mesma tolerância do casamento da
# Cito, pelo mesmo motivo.
_EVENT_DATE_TOLERANCE = timedelta(days=1)


class OfficialEventMatchError(Exception):
    """Falha ao resolver o item de catálogo oficial de um evento persistido."""


class AmbiguousOfficialEventMatchError(OfficialEventMatchError):
    """Mais de um candidato concorda por nome -- nunca casar em silêncio.

    Espelha ``AmbiguousEventMatchError`` (M6) e a entity resolution do M1: a ambiguidade
    **falha alto**; quem chama conta o evento, não escreve nada para ele e segue (RF-02).
    """


@dataclass(frozen=True)
class OfficialCatalogItem:
    """Projeção mínima do payload de ``event/live/{eventId}`` usada no casamento.

    ``event_id`` é texto porque é assim que ``events.ufc_event_id`` o guarda: identificador
    externo é opaco, nunca aritmética. ``local_date`` já é a data de calendário **local** (nunca
    a UTC de ``StartTime``): decidir a janela ou o casamento sobre a UTC erraria o dia em todo
    evento noturno das Américas.

    Não guarda a promoção: o catálogo que chega aqui já é só do UFC, porque a descoberta filtra
    na projeção (é lá que o payload é conhecido, e é lá que o filtro precisa acontecer antes do
    cálculo da data local -- há evento de outra promoção sem fuso na fonte).
    """

    event_id: str
    name: str
    local_date: date


@dataclass(frozen=True)
class OfficialEventMatch:
    """Item de catálogo casado a um evento persistido, com o critério que o resolveu.

    ``matched_by`` existe para o relatório distinguir os graus de confiança. Hoje há um critério
    só (``"name"``); um segundo tier entraria aqui, não numa flag booleana.
    """

    item: OfficialCatalogItem
    matched_by: Literal["name"]


def official_event_candidates(
    event: Event, catalog: Sequence[OfficialCatalogItem]
) -> tuple[OfficialCatalogItem, ...]:
    """Itens cuja data local está a no máximo um dia da ``event.date`` persistida.

    Separado do desempate porque quem chama precisa distinguir **não casado** (nenhum candidato
    na janela) de **em revisão** (havia candidato, mas nenhum corroborou por nome): são
    diagnósticos diferentes no relatório de cobertura -- ausência na fonte contra divergência de
    grafia.
    """
    return tuple(
        item for item in catalog if abs(item.local_date - event.date) <= _EVENT_DATE_TOLERANCE
    )


def resolve_official_event_match(
    event: Event, candidates: Sequence[OfficialCatalogItem]
) -> OfficialEventMatch | None:
    """Desempata os candidatos por ``normalize_event_name``; sem corroboração, não casa.

    Exatamente um concordante -> casa. Mais de um -> ``AmbiguousOfficialEventMatchError``.
    Nenhum concordante -> ``None``, mesmo que houvesse um único candidato na janela (a
    divergência deliberada em relação à Cito -- ver a docstring do módulo).

    A ambiguidade só é erro quando existiria escrita a decidir: dois candidatos que **não**
    concordam por nome não levantam, porque nenhum deles seria escrito de qualquer forma.
    """
    event_key = normalize_event_name(event.name)
    by_name = [item for item in candidates if normalize_event_name(item.name) == event_key]
    if len(by_name) > 1:
        raise AmbiguousOfficialEventMatchError(
            f"O evento {event.name!r} ({event.date}) casa por nome com {len(by_name)} eventos da "
            f"fonte oficial ({', '.join(item.event_id for item in by_name)}); "
            "nunca casar em silêncio."
        )
    if len(by_name) == 1:
        return OfficialEventMatch(item=by_name[0], matched_by="name")
    return None
