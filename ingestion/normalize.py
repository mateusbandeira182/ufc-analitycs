"""Normalização determinística de nome de lutador -- chave de dedup do seed.

A entity resolution (``ingestion.entity_resolution``) usa o nome normalizado como
chave e a data de nascimento como desempate. A normalização é pura e determinística:
remove acentos (NFKD), baixa a caixa, colapsa espaços e descarta sufixos de linhagem
(``Jr``, ``Sr``, ``II``, ``III``, ``IV``) que variam entre fontes sem mudar a identidade.
"""

from __future__ import annotations

import re
import unicodedata

_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
_WHITESPACE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Devolve a chave de dedup determinística de ``name``.

    Sem acentos (NFKD -> ASCII), em minúsculas, espaços colapsados e sufixos de
    linhagem removidos. Nomes que só diferem em caixa/acento/espaço/sufixo colapsam
    para a mesma chave.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    tokens = [token for token in _WHITESPACE.split(ascii_name.casefold().strip()) if token]
    kept = [token for token in tokens if token.strip(".") not in _SUFFIXES]
    return " ".join(kept)
