"""Casamento evento persistido <-> item do catálogo da fonte oficial (SPEC 008, Slice 02).

Funções **puras**: não tocam o banco, não escrevem, não conhecem a forma do payload (a projeção
do DTO em ``OfficialCatalogItem`` é da descoberta, ``ingestion.ufc_official.discovery``).

Política mais restritiva que a da Cito, de propósito
----------------------------------------------------
O casamento da Cito (``ingestion.cito.matching.resolve_event_match``) aceitava um candidato
único na janela de data **sem** concordância de nome, marcando-o ``matched_by_name=False`` para
inspeção humana posterior. Aqui isso **não** acontece: sem corroboração, nada é escrito.

São duas as corroborações aceitas, em ordem de confiança (Sprint 008-08): o **nome
normalizado** e, só quando ele não resolve, o **roster** -- a interseção entre os lutadores do
card persistido e os do card oficial. Nunca a **forma** do rótulo: ``UFC 32`` é prefixo de
``UFC 320``, e prefixo é adivinhação sobre o nome, enquanto o roster é evidência independente
vinda do dado.

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

# Nomes normalizados em comum exigidos para o roster corroborar um candidato (Sprint 008-08).
# Quatro é o piso de um card real (o menor card da janela tem oito lutadores) e alto o bastante
# para que uma coincidência de dois ou três lutadores repetidos entre eventos vizinhos não case
# nada.
#
# NÃO é parâmetro: não vira argumento de função, variável de ambiente nem flag de linha de
# comando. Um limiar que se afrouxa por conveniência -- em especial depois de ver qual evento
# deixou de casar -- deixa de ser garantia e vira racionalização.
_MIN_ROSTER_OVERLAP = 4


class OfficialEventMatchError(Exception):
    """Falha ao resolver o item de catálogo oficial de um evento persistido."""


class AmbiguousOfficialEventMatchError(OfficialEventMatchError):
    """Mais de um candidato corrobora o evento -- nunca casar em silêncio.

    Espelha ``AmbiguousEventMatchError`` (M6) e a entity resolution do M1: a ambiguidade
    **falha alto**; quem chama conta o evento, não escreve nada para ele e segue (RF-02).

    Vale para os dois tiers: dois candidatos concordando por nome, e dois candidatos acima do
    limiar de roster.
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
    # Nomes NORMALIZADOS dos lutadores do card oficial -- a mesma chave de
    # ``fighters.name_normalized``, para a interseção do tier 2 ser comparável. Uma normalização
    # divergente entre os dois lados daria interseção zero e desligaria o tier em silêncio.
    #
    # Sai do ``FightCard`` do payload que o cache já guarda em disco: o roster **não custa
    # requisição nenhuma**, nem endpoint novo, nem campo novo do DTO.
    fighter_names: frozenset[str]


@dataclass(frozen=True)
class OfficialEventMatch:
    """Item de catálogo casado a um evento persistido, com o critério que o resolveu.

    ``matched_by`` existe para o relatório distinguir os graus de confiança, e é por isso que
    ele nunca foi uma flag booleana: ``"name"`` é o tier de alta confiança e ``"roster"`` é
    corroboração indireta, que o relatório nomeia um a um para inspeção.
    """

    item: OfficialCatalogItem
    matched_by: Literal["name", "roster"]


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
    event: Event, candidates: Sequence[OfficialCatalogItem], *, roster: frozenset[str]
) -> OfficialEventMatch | None:
    """Tier 1: nome normalizado. Tier 2 (só se o 1 não resolveu): roster corroborado.

    **Tier 1** desempata os candidatos por ``normalize_event_name``. Exatamente um concordante
    -> casa. Mais de um -> ``AmbiguousOfficialEventMatchError``. Nenhum -> cai para o tier 2,
    mesmo que houvesse um único candidato na janela (a divergência deliberada em relação à Cito
    -- ver a docstring do módulo).

    **Tier 2** aceita o candidato com ``>= _MIN_ROSTER_OVERLAP`` nomes normalizados em comum
    entre o ``roster`` persistido do evento e o ``fighter_names`` do item, e **exatamente um**
    candidato acima do limiar. Dois acima -> ambíguo, mesmo com interseções de tamanhos
    diferentes: escolher o maior seria decidir no escuro com aparência de critério. Nenhum
    acima -> ``None``, e o evento permanece em revisão.

    Nunca há heurística sobre a **forma** do nome (prefixo, subcadeia, distância de edição):
    ``UFC 32`` é prefixo de ``UFC 320`` e o consumidor a jusante é a correção do canto, que é o
    alvo do modelo. A corroboração do tier 2 é o roster porque quem lutou naquele card é fato
    dos dois lados -- evidência vinda do dado, não adivinhação sobre o rótulo.

    A ambiguidade só é erro quando existiria escrita a decidir: dois candidatos que não
    corroboram por critério nenhum não levantam, porque nenhum deles seria escrito.
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

    by_roster = [
        item
        for item in candidates
        if len(item.fighter_names & roster) >= _MIN_ROSTER_OVERLAP  # roster vazio nunca alcança
    ]
    if len(by_roster) > 1:
        raise AmbiguousOfficialEventMatchError(
            f"O evento {event.name!r} ({event.date}) é corroborado pelo roster por "
            f"{len(by_roster)} eventos da fonte oficial "
            f"({', '.join(item.event_id for item in by_roster)}); nunca casar em silêncio -- "
            "o de maior interseção NÃO vence, porque tamanho de interseção não é evidência de "
            "identidade."
        )
    if len(by_roster) == 1:
        return OfficialEventMatch(item=by_roster[0], matched_by="roster")
    return None
