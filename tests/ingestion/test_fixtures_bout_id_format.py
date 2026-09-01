"""Trava de formato: todo ``bout_id`` das fixtures da Cito tem a forma que a API real produz.

Existe por causa de um defeito concreto: até a correção do alvo pré-2010, quatro fixtures deste
diretório eram autorais, não capturas -- traziam ``bout_id`` do tipo ``"ufc-319-bout-1"``, que a
Cito nunca devolveu, e uma delas ainda invertia o canto da luta principal de UFC 319. A suíte
ficou verde durante todo o M5 e o M6 sobre payloads que a API não produz, e **nenhuma** das duas
auditorias pegou. O formato do identificador é o critério mais barato que teria pegado: um id
inventado não se parece com um id real.

Formatos aceitos, medidos sobre as 157 capturas de evento em ``.cache/cito`` (1.933 ids
distintos): 16 caracteres hexadecimais minúsculos (1.481 ocorrências, ex.: ``12cedec11b37ddc0``)
ou um id numérico de 5 dígitos (452 ocorrências, ex.: ``11963``, usado em eventos sem
enriquecimento do ufcstats). Nenhuma outra forma aparece.

A trava é deliberadamente **sem lista de exceção**: uma fixture que precise de um id fora do
formato está afirmando algo sobre a API que não foi medido, e é isso que se quer barrar.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).parent / "fixtures"

# Chaves que carregam o identificador de luta da Cito, em qualquer bloco do payload.
_CHAVES_DE_BOUT_ID = frozenset({"bout_id", "boutId"})

# O ``id`` de um item dentro de uma lista ``bouts`` também é o identificador da luta -- foi
# exatamente ali que o ``"ufc-319-bout-1"`` morava no bloco ``bouts[]`` da fixture antiga.
_LISTAS_DE_LUTA = frozenset({"bouts"})

_FORMATO_REAL = re.compile(r"^(?:[0-9a-f]{16}|[0-9]{5})$")


def _identificadores(node: object, *, dentro_de_bouts: bool = False) -> list[str]:
    """Coleta recursivamente todo identificador de luta do payload dado."""
    achados: list[str] = []
    if isinstance(node, dict):
        for chave, valor in node.items():
            if chave in _CHAVES_DE_BOUT_ID and isinstance(valor, str):
                achados.append(valor)
            if dentro_de_bouts and chave == "id" and isinstance(valor, str):
                achados.append(valor)
            achados.extend(_identificadores(valor, dentro_de_bouts=chave in _LISTAS_DE_LUTA))
    elif isinstance(node, list):
        for item in node:
            achados.extend(_identificadores(item, dentro_de_bouts=dentro_de_bouts))
    return achados


def _fixtures_json() -> list[Path]:
    """Todas as fixtures JSON do diretório, inclusive as dos subdiretórios."""
    return sorted(_FIXTURES.rglob("*.json"))


@pytest.mark.parametrize("fixture", _fixtures_json(), ids=lambda caminho: caminho.name)
def test_bout_id_das_fixtures_tem_o_formato_da_api_real(fixture: Path) -> None:
    """Nenhuma fixture da Cito carrega identificador de luta inventado."""
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    fora_do_formato = sorted(
        {
            identificador
            for identificador in _identificadores(payload)
            if not _FORMATO_REAL.match(identificador)
        }
    )

    assert not fora_do_formato, (
        f"{fixture.relative_to(_FIXTURES)} traz identificador(es) de luta fora do formato da Cito: "
        f"{fora_do_formato}. Um id assim indica payload escrito à mão, não captura -- recorte a "
        f"fixture de uma resposta real (ver .cache/cito)."
    )


def test_a_trava_enxerga_os_identificadores_das_fixtures_de_payload() -> None:
    """Guarda da guarda: o varredor de fato encontra ids -- uma trava cega passaria sempre.

    Sem esta asserção, quebrar o caminho de coleta (renomear uma chave, errar a recursão)
    transformaria o teste acima num no-op verde, que é a mesma classe de falha silenciosa que
    ele existe para impedir.
    """
    coletados = {
        fixture.name: _identificadores(json.loads(fixture.read_text(encoding="utf-8")))
        for fixture in _fixtures_json()
    }
    com_ids = {nome: ids for nome, ids in coletados.items() if ids}

    assert len(com_ids) >= 8
    assert "12cedec11b37ddc0" in com_ids["event_stats_ufc-319.json"]
    assert "12cedec11b37ddc0" in com_ids["event_ufc-319.json"]


# --------------------------------------------------------------------------- #
# Trava de coerência interna (revisão da SPEC 007, R-04).
#
# O formato do identificador é barato mas cego a uma segunda classe de fixture inventada: a
# **quimera** -- blocos verdadeiros de eventos diferentes costurados no mesmo payload. Foi o que
# aconteceu com ``gap_sync/event_stats_ufc-freedom-250.json``, que trazia o bloco ``event`` de um
# evento e uma luta cujo ``eventSlug`` (e o ``meta`` inteiro) era de outro. Todos os ids eram
# bem-formados, então a trava de formato passou.
#
# A invariante aqui é a que a API real cumpre em toda captura conferida: um payload de evento fala
# de **um** evento, e cada bloco pendurado numa luta fala **daquela** luta.
# --------------------------------------------------------------------------- #

# Blocos internos de uma luta que repetem o identificador dela em ``boutId``.
_BLOCOS_DA_LUTA = ("fighters", "boutStats", "roundStats")

# Blocos do payload que espelham, no topo, as linhas penduradas em cada luta do card.
_BLOCOS_DO_TOPO = ("boutStats", "roundStats")

_PREFIXO_EVENT_STATS = "event_stats_"


def _objeto(node: object) -> dict[str, object] | None:
    """O nó como objeto JSON, ou ``None`` quando ele não é um objeto."""
    return node if isinstance(node, dict) else None


def _lista(node: object) -> list[object]:
    """O nó como lista JSON; qualquer outra coisa vira lista vazia."""
    return node if isinstance(node, list) else []


def _valor(node: object, chave: str) -> object:
    """O valor de ``chave`` quando o nó é um objeto JSON; ``None`` caso contrário."""
    objeto = _objeto(node)
    return objeto.get(chave) if objeto is not None else None


def _texto(node: object, chave: str) -> str | None:
    """O valor de ``chave`` no nó quando ele é uma string; ``None`` caso contrário."""
    valor = _valor(node, chave)
    return valor if isinstance(valor, str) else None


def _payload_de_evento(payload: object) -> dict[str, object] | None:
    """O bloco ``data`` de uma fixture de stats de evento; ``None`` se a fixture é de outro tipo."""
    envelope = _objeto(payload)
    if envelope is None:
        return None
    data = _objeto(envelope.get("data"))
    if data is None or "event" not in data:
        return None
    return data


def _incoerencias(fixture: Path, payload: object) -> list[str]:
    """Lista, em pt-BR, tudo que no payload afirma pertencer a outro evento ou a outra luta."""
    data = _payload_de_evento(payload)
    if data is None:
        return []

    achados: list[str] = []
    evento = data["event"]
    slug = _texto(evento, "slug")
    event_id = _texto(evento, "id")

    nome = fixture.name.removesuffix(".json")
    if nome.startswith(_PREFIXO_EVENT_STATS):
        slug_do_arquivo = nome.removeprefix(_PREFIXO_EVENT_STATS)
        if slug_do_arquivo != slug:
            achados.append(
                f"o nome do arquivo diz {slug_do_arquivo!r} e o bloco event diz {slug!r}"
            )

    ids_das_lutas: set[str] = set()
    for luta in _lista(data.get("bouts")):
        bout_id = _texto(luta, "id")
        if bout_id is not None:
            ids_das_lutas.add(bout_id)
        event_slug = _texto(luta, "eventSlug")
        if event_slug is not None and event_slug != slug:
            achados.append(f"a luta {bout_id} tem eventSlug {event_slug!r}, e o evento é {slug!r}")
        for bloco in _BLOCOS_DA_LUTA:
            for linha in _lista(_valor(luta, bloco)):
                referencia = _texto(linha, "boutId")
                if referencia is not None and referencia != bout_id:
                    achados.append(f"{bloco} da luta {bout_id} referencia a luta {referencia}")

    for bloco in _BLOCOS_DO_TOPO:
        for linha in _lista(data.get(bloco)):
            referencia = _texto(linha, "boutId")
            if referencia is not None and referencia not in ids_das_lutas:
                achados.append(f"{bloco} do topo referencia a luta {referencia}, ausente do card")

    meta = _valor(payload, "meta")
    pedido = _texto(meta, "requestedIdentifier")
    if pedido is not None and pedido not in {slug, event_id}:
        achados.append(f"meta.requestedIdentifier é {pedido!r}, e o evento é {slug!r}")
    resolvido = _valor(meta, "resolved")
    if _texto(resolvido, "slug") not in {None, slug}:
        achados.append(f"meta.resolved.slug é {_texto(resolvido, 'slug')!r}, e o evento é {slug!r}")
    if _texto(resolvido, "id") not in {None, event_id}:
        achados.append(f"meta.resolved.id é {_texto(resolvido, 'id')!r}, e o evento é {event_id!r}")

    return achados


@pytest.mark.parametrize("fixture", _fixtures_json(), ids=lambda caminho: caminho.name)
def test_payload_de_evento_das_fixtures_fala_de_um_evento_so(fixture: Path) -> None:
    """Nenhuma fixture costura blocos de eventos ou de lutas diferentes no mesmo payload."""
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    achados = _incoerencias(fixture, payload)

    assert not achados, (
        f"{fixture.relative_to(_FIXTURES)} é um payload internamente incoerente: "
        f"{achados}. A API real nunca devolveu isso -- recorte a fixture de uma resposta real "
        f"(ver .cache/cito) em vez de costurar blocos de eventos diferentes."
    )


def test_a_trava_de_coerencia_enxerga_os_payloads_de_evento() -> None:
    """Guarda da guarda: a trava de fato inspeciona payloads de evento e reprova uma quimera.

    Sem isto, um erro no reconhecimento do payload (uma chave renomeada, o ``data`` mudando de
    lugar) transformaria a asserção acima num verde vazio -- a mesma falha silenciosa que ela
    existe para impedir.
    """
    inspecionadas = [
        fixture
        for fixture in _fixtures_json()
        if _payload_de_evento(json.loads(fixture.read_text(encoding="utf-8"))) is not None
    ]

    assert len(inspecionadas) >= 6

    quimera = json.loads(
        (_FIXTURES / "gap_sync" / "event_stats_ufc-fight-night-august-22-2026.json").read_text(
            encoding="utf-8"
        )
    )
    quimera["data"]["bouts"][0]["eventSlug"] = "ufc-freedom-250"

    assert _incoerencias(Path("event_stats_ufc-fight-night-august-22-2026.json"), quimera)
