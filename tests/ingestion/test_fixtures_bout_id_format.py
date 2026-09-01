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
