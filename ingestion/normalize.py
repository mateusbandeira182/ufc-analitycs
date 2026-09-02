"""Normalização determinística de nome -- chave de dedup do seed e do casamento de evento.

A entity resolution (``ingestion.entity_resolution``) usa o nome normalizado como
chave e a data de nascimento como desempate. A normalização é pura e determinística:
remove acentos (NFKD), baixa a caixa, colapsa espaços e descarta sufixos de linhagem
(``Jr``, ``Sr``, ``II``, ``III``, ``IV``) que variam entre fontes sem mudar a identidade.

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


# Pontuação e qualquer caractere não-alfanumérico remanescente viram separador de token.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_event_name(name: str) -> str:
    """'UFC 319: Du Plessis vs. Chimaev' -> 'ufc 319 du plessis vs chimaev'.

    Aplica ``normalize_name`` PRIMEIRO (NFKD -> ASCII, caixa, espaços) e só então troca a
    pontuação restante por espaço. A ordem importa: remover não-alfanuméricos antes do NFKD
    comeria letras acentuadas ('Šarić' -> 'ari').
    """
    return " ".join(_NON_ALNUM.sub(" ", normalize_name(name)).split())
