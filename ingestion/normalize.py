"""Normalização determinística de nome -- chave de dedup do seed e do casamento de evento.

A entity resolution (``ingestion.entity_resolution``) usa o nome normalizado como
chave e a data de nascimento como desempate. A normalização é pura e determinística:
baixa a caixa, **translitera letra atômica** (``ł``, ``ø``, ``đ``, ``ß``...), remove acentos
decomponíveis (NFKD), colapsa espaços e descarta sufixos de linhagem (``Jr``, ``Sr``, ``II``,
``III``, ``IV``) que variam entre fontes sem mudar a identidade.

``normalize_event_name`` mora aqui, e não no pacote de uma fonte, porque o casamento de
evento por nome precisa ser **idêntico** em todas as fontes: a Cito
(``ingestion.cito.matching``/``gap_sync``) e a API oficial da UFC
(``ingestion.ufc_official.matching``) comparam a mesma chave. Uma divergência de
normalização entre elas quebraria o casamento em silêncio -- o evento simplesmente não
casaria, sem erro nenhum.
"""

from __future__ import annotations

import re
import unicodedata

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
_WHITESPACE = re.compile(r"\s+")

# Letras ATÔMICAS (M9, SPEC 011, RF-09): o NFKD não as decompõe -- elas não são
# 'letra + diacrítico' --, então o ``encode("ascii", "ignore")`` as APAGAVA em vez de
# transliterá-las. 'Michał Oleksiejczuk' virava 'micha oleksiejczuk', que não casa com o
# 'michal oleksiejczuk' já persistido: a entity resolution criava um lutador novo e a
# contagem de estreantes inflava, sem erro nenhum.
#
# A tabela entra ANTES do NFKD, e as chaves são minúsculas porque o ``casefold`` passou a
# preceder a tradução (``"Ł".casefold() == "ł"``). O ``ß`` já vira ``"ss"`` no próprio
# ``casefold`` e fica aqui como registro da regra -- a RF-09 o nomeia, e a tabela é a
# documentação dela.
_ATOMIC_LETTERS = str.maketrans(
    {"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe", "þ": "th", "ð": "d"}
)


def normalize_name(name: str) -> str:
    """Devolve a chave de dedup determinística de ``name``.

    Em minúsculas, com letra atômica **transliterada** (``ł`` -> ``l``), sem acentos
    decomponíveis (NFKD -> ASCII), espaços colapsados e sufixos de linhagem removidos. Nomes
    que só diferem em caixa/acento/espaço/sufixo colapsam para a mesma chave.

    A ordem é load-bearing: ``casefold`` -> transliteração -> NFKD -> ASCII. Transliterar
    depois do NFKD seria tarde (a letra atômica já teria sido apagada pelo ``ignore``), e
    transliterar antes do ``casefold`` exigiria duplicar cada entrada da tabela em caixa alta.
    """
    folded = name.casefold().translate(_ATOMIC_LETTERS)
    decomposed = unicodedata.normalize("NFKD", folded)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    tokens = [token for token in _WHITESPACE.split(ascii_name.strip()) if token]
    kept = [token for token in tokens if token.strip(".") not in _SUFFIXES]
    return " ".join(kept)


# Pontuação e qualquer caractere não-alfanumérico remanescente viram separador de token.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_event_name(name: str) -> str:
    """'UFC 319: Du Plessis vs. Chimaev' -> 'ufc 319 du plessis vs chimaev'.

    Aplica ``normalize_name`` PRIMEIRO (caixa, transliteração, NFKD -> ASCII, espaços) e só
    então troca a pontuação restante por espaço. A ordem importa: remover não-alfanuméricos
    antes do NFKD comeria letras acentuadas ('Šarić' -> 'ari').
    """
    return " ".join(_NON_ALNUM.sub(" ", normalize_name(name)).split())
