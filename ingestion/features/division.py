"""Conhecimento de domínio da divisão: categoria física e taxa base point-in-time.

Bloco B1 da SPEC 009 (M8), Slice 02. Duas famílias de features **bout-level** -- valem para
a luta inteira, não para um canto, e por isso entram na matriz de confronto como coluna
única, sem sufixo ``_a``/``_b`` e sem ``_diff`` (que seria identicamente zero):

- **B1a, categoria** (``weight_class_lbs``, ``is_womens_division``): traduz o rótulo cru de
  ``bouts.weight_class`` no **limite de peso real da divisão, em libras**, mais o indicador
  de divisão feminina. O limite real (e não um índice arbitrário) é o eixo físico que a
  árvore pode cortar com significado; o indicador é obrigatório porque as divisões femininas
  **colidem em peso** com as masculinas (125, 135 e 145) e têm taxa base muito diferente.
  Sem one-hot: dez colunas de diluição para a informação que duas carregam.
- **B1b, taxa base** (``division_finish_rate_prior``): a fração de lutas daquela divisão
  decididas por KO/TKO ou finalização, calculada **só sobre lutas de data estritamente
  anterior**. É propriedade do estilo da divisão -- pesados finalizam quase o dobro do palha
  feminino. **Não** é target encoding: a taxa de vitória do canto vermelho por divisão seria
  armadilha de vazamento e a SPEC a proíbe explicitamente.

Este módulo fica **fora** de ``matchup.py`` de propósito: aquele módulo é column-agnóstico
por contrato (não conhece feature por feature), e enfiar o mapa de divisões nele quebraria
a propriedade que o mantém estável a cada slice nova. ``matchup`` pivota e classifica;
``division`` conhece divisão.

O DataFrame do Pandas é fronteira dinâmica (``pyproject.toml`` marca ``pandas.*`` como
``follow_imports=skip``): a conversão do rótulo cru para o domínio acontece na borda, valor
a valor, e nenhum ``Any`` propaga. Ausência permanece **nula** em toda saída (RF-03): nunca
zero, nunca ``inf``, nunca imputação.
"""

from __future__ import annotations

import logging

import pandas as pd

from apps.bouts.enums import BoutMethod
from ingestion.features.rolling import safe_ratio

logger = logging.getLogger(__name__)

# Nomes das features emitidas. Fonte única: o registro de blocos de ``analysis.ablation``
# importa daqui em vez de redigitar as strings -- um nome desatualizado faria a coluna sumir
# do bloco em silêncio e o veredito passaria a valer para um bloco menor do que o medido diz.
WEIGHT_CLASS_LBS = "weight_class_lbs"
IS_WOMENS_DIVISION = "is_womens_division"
DIVISION_FINISH_RATE_PRIOR = "division_finish_rate_prior"

# Os dois sub-blocos são medidos como vereditos **independentes** (RF-06/RF-07): a categoria
# e a taxa base respondem perguntas diferentes e podem pagar uma sem a outra.
DIVISION_CATEGORY_FEATURES: list[str] = [WEIGHT_CLASS_LBS, IS_WOMENS_DIVISION]
DIVISION_BASE_RATE_FEATURES: list[str] = [DIVISION_FINISH_RATE_PRIOR]

# Limite de peso REAL da divisão, em libras -- eixo físico com significado, não índice.
# Chaves na forma canônica devolvida por ``normalize_division_keys``.
WEIGHT_CLASS_LIMIT_LBS: dict[str, int] = {
    "flyweight": 125,
    "bantamweight": 135,
    "featherweight": 145,
    "lightweight": 155,
    "welterweight": 170,
    "middleweight": 185,
    "light heavyweight": 205,
    "heavyweight": 265,
    "women's strawweight": 115,
    "women's flyweight": 125,
    "women's bantamweight": 135,
    "women's featherweight": 145,
}

# ``is_womens_division`` é derivada do prefixo da chave canônica, não de um segundo
# dicionário: dois mapas para o mesmo fato divergiriam na primeira divisão nova.
_WOMENS_PREFIX = "women's"

# Sufixo de disputa de cinturão. A Cito grava ``"Middleweight Title"`` para a MESMA divisão
# (ver ``tests/ingestion/test_incremental.py``); tratá-lo como rótulo desconhecido jogaria
# fora justamente as lutas de título e fragmentaria a taxa base nas lutas mais importantes
# do card. Que a luta valha cinturão é informação do bloco B2, não do B1.
_TITLE_SUFFIX = " title"

# Apóstrofo tipográfico -> apóstrofo reto, para que ``Women’s`` e ``Women's`` sejam a mesma
# divisão. Rótulo copiado de página web traz o primeiro.
_TYPOGRAPHIC_APOSTROPHE = "’"

# Finalização = KO/TKO ou submissão, a mesma convenção de ``rolling.FINISH_RATE_PRIOR``.
# Duas ocorrências não fecham a rule of three -- extrair um helper compartilhado agora seria
# abstração prematura sobre um conjunto que nunca mudou.
_FINISH_METHODS: frozenset[str] = frozenset({BoutMethod.KO_TKO.value, BoutMethod.SUBMISSION.value})

# Colunas internas da agregação por (divisão, data). Nomes locais: nada disto sai do módulo.
_COL_KEY = "division_key"
_COL_DATE = "event_date"
_COL_FINISHES = "finishes"
_COL_BOUTS = "bouts"


def _canonical_division(raw: object) -> str | None:
    """Rótulo cru -> chave canônica do mapa, ou ``None`` quando fora dele.

    Normaliza na borda: apóstrofo tipográfico, caixa, e espaços em excesso (o banco real tem
    ``"road to  1 ..."`` com espaço duplo); depois remove o sufixo de disputa de cinturão.
    Um valor que não é texto (nulo do Pandas) e um rótulo fora do mapa (catch weight, open
    weight, torneio do TUF, grafia nova) devolvem ``None`` -- nunca um chute.
    """
    if not isinstance(raw, str):
        return None
    texto = " ".join(raw.replace(_TYPOGRAPHIC_APOSTROPHE, "'").lower().split())
    if texto.endswith(_TITLE_SUFFIX):
        texto = texto[: -len(_TITLE_SUFFIX)].strip()
    return texto if texto in WEIGHT_CLASS_LIMIT_LBS else None


def _warn_unknown_labels(values: pd.Series, keys: pd.Series) -> None:
    """Avisa **uma vez por rótulo distinto** fora do mapa, com o número de lutas afetadas.

    Avisar por linha afogaria o log: o banco real tem 126 rótulos distintos de
    ``weight_class``, dezenas deles torneios do TUF com uma única luta cada. O que a RF-09
    quer visível é *quais* rótulos ficaram de fora e *quanto* cada um custa em cobertura.
    """
    desconhecidos = values[keys.isna() & values.notna()]
    if desconhecidos.empty:
        return
    contagem = desconhecidos.value_counts()
    for rotulo in sorted(str(item) for item in contagem.index):
        logger.warning(
            "Rótulo de divisão desconhecido: %r em %d luta(s). As features de divisão ficam "
            "nulas nessas linhas (nunca zero, nunca chute) -- acrescente o rótulo a "
            "WEIGHT_CLASS_LIMIT_LBS se ele for uma divisão de verdade.",
            rotulo,
            int(contagem[rotulo]),
        )


def normalize_division_keys(values: pd.Series) -> pd.Series:
    """Rótulo cru de ``bouts.weight_class`` -> chave canônica; desconhecido -> nulo.

    Devolve uma série de dtype ``string`` alinhada à entrada. É o ponto único onde a grafia
    do rótulo importa: tudo a jusante (categoria e taxa base) fala apenas a chave canônica,
    o que impede que ``Lightweight`` e ``lightweight`` virem duas divisões e fragmentem o
    denominador da taxa base.
    """
    keys = values.map(_canonical_division).astype("string")
    _warn_unknown_labels(values, keys)
    return keys


def division_category_features(keys: pd.Series) -> pd.DataFrame:
    """Duas colunas ``float64``: limite de peso em libras e indicador de divisão feminina.

    ``float64`` (e não ``Int64``) porque a ausência precisa atravessar a borda já existente
    de ``materialize._to_json_value``, que mapeia ``NaN`` -> ``None`` no JSONB. Chave nula
    (rótulo desconhecido ou ausente) produz nulo nas **duas** colunas: zero em
    ``weight_class_lbs`` seria um peso impossível, e zero em ``is_womens_division`` afirmaria
    "masculino" sobre uma luta cuja divisão ninguém conhece (RF-03).
    """
    canonicas = [None if pd.isna(chave) else str(chave) for chave in keys]
    libras = [
        None if chave is None else float(WEIGHT_CLASS_LIMIT_LBS[chave]) for chave in canonicas
    ]
    feminina = [
        None if chave is None else float(chave.startswith(_WOMENS_PREFIX)) for chave in canonicas
    ]
    return pd.DataFrame(
        {
            WEIGHT_CLASS_LBS: pd.Series(libras, index=keys.index, dtype="float64"),
            IS_WOMENS_DIVISION: pd.Series(feminina, index=keys.index, dtype="float64"),
        }
    )


def division_finish_rate_prior(
    keys: pd.Series, event_date: pd.Series, method: pd.Series
) -> pd.Series:
    """Fração de finalizações da divisão em lutas de data ESTRITAMENTE anterior.

    O ponto de risco declarado do bloco. Um ``shift(1)`` posicional sobre a série ordenada
    **não basta**: deixaria uma luta enxergar outra do MESMO evento, cujo desfecho não é
    conhecido quando se prevê o card. A forma que garante o corte certo é agregar por
    ``(divisão, data)`` e **subtrair o grupo da própria data** do acumulado por divisão --
    assim todas as lutas de um mesmo evento compartilham exatamente o mesmo passado e
    nenhuma entra no denominador da outra (CA-10).

    Sem nenhuma luta anterior (primeira data da divisão) o valor é nulo, nunca 0 (RF-03) --
    ``safe_ratio`` é quem mapeia o denominador zero. Chave nula (rótulo desconhecido) não
    forma divisão fantasma: fica de fora da agregação e a luta recebe nulo. NC e empate
    contam no denominador como não-finalização, mesma convenção de
    ``rolling.FINISH_RATE_PRIOR``.

    A série devolvida está alinhada à entrada (mesmo índice, mesma ordem): a matriz de
    confronto não chega ordenada por data, e um desalinhamento silencioso atribuiria a taxa
    base de uma luta a outra.
    """
    bouts = pd.DataFrame(
        {
            _COL_KEY: keys,
            _COL_DATE: event_date,
            _COL_FINISHES: method.isin(_FINISH_METHODS).astype("float64"),
        }
    )
    per_date = (
        bouts.dropna(subset=[_COL_KEY])
        .groupby([_COL_KEY, _COL_DATE], as_index=False)
        .agg(**{_COL_FINISHES: (_COL_FINISHES, "sum"), _COL_BOUTS: (_COL_FINISHES, "size")})
        .sort_values(by=[_COL_KEY, _COL_DATE], kind="stable")
    )
    por_divisao = per_date.groupby(_COL_KEY)
    prior_finishes = por_divisao[_COL_FINISHES].cumsum() - per_date[_COL_FINISHES]
    prior_bouts = por_divisao[_COL_BOUTS].cumsum() - per_date[_COL_BOUTS]
    per_date[DIVISION_FINISH_RATE_PRIOR] = safe_ratio(prior_finishes, prior_bouts)

    # ``how="left"`` preserva a ordem das linhas da esquerda; ``validate="many_to_one"``
    # torna explícito que ``per_date`` tem uma linha por (divisão, data). Linha de chave nula
    # não casa com nada (a chave foi dropada da direita) e recebe nulo.
    merged = bouts.merge(
        per_date[[_COL_KEY, _COL_DATE, DIVISION_FINISH_RATE_PRIOR]],
        on=[_COL_KEY, _COL_DATE],
        how="left",
        validate="many_to_one",
    )
    return pd.Series(
        merged[DIVISION_FINISH_RATE_PRIOR].to_numpy(), index=keys.index, dtype="float64"
    )
