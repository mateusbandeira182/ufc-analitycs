"""Teste ponta a ponta dos blocos D3+D4 -- SPEC 009, Slice 06 (CA-11, CA-13, CA-15).

Contra o Postgres de teste transacional, pelo caminho real (``run_materialize``/``run_check``),
numa sequência só -- que é como a slice é demonstrada:

1. o cache defasado (sem as 6 chaves novas) faz ``run_check`` falhar **nomeando** cada uma;
2. ``run_materialize`` as persiste e o mesmo ``run_check`` volta a passar;
3. as 6 chegam ao payload JSONB como número ou ``null`` -- jamais string, jamais ``NaN``
   (que não é JSON válido) -- com os valores conferidos à mão;
4. nenhuma delas é descartada em silêncio por ``build_dataset`` -- o modo de falha que
   escondeu 21 features do M5 por meses;
5. o granular fica intocado: a re-materialização escreve só no cache derivado.

A fixture monta **três níveis de histórico** de propósito: os finalistas A e B, os quatro
adversários anteriores deles (cada um com o próprio estilo as-of) e os enchimentos que dão a
esses adversários um cartel anterior. É o mínimo para que o D3 e o D4 nasçam com valor: o D3
precisa do estilo do adversário **daquela** luta, e o D4 do cartel dele **naquela** data --
com uma fixture de dois níveis as duas colunas nasceriam ``NaN`` e o teste falharia dizendo
"feature descartada" onde o defeito real seria "fixture sem dado".

Os estilos são os dois extremos do quadrado unitário (grappler puro ``(1, 0)`` e volumoso
puro ``(0, 1)``), o que torna os pesos do núcleo gaussiano exatamente ``1`` e ``exp(-2)`` e
os valores esperados conferíveis à mão.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from analysis.dataset import build_dataset, read_bout_features
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.features.cli import _enriched_long_frame, run_check, run_materialize
from ingestion.features.freshness import StaleFeatureCacheError, check_cache_freshness
from ingestion.features.matchup import MatchupMatrix, build_matchup_matrix
from ingestion.features.rolling import (
    OPPONENT_WIN_RATE_PRIOR_AVG,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
)
from ingestion.normalize import normalize_name

# As 6 colunas que a slice acrescenta ao payload: duas bases as-of por lutador, cada uma
# virando o trio de canto. Montadas a partir das constantes, nunca redigitadas.
_BASES_AS_OF: tuple[str, ...] = (SIMILAR_STYLE_WIN_RATE_PRIOR, OPPONENT_WIN_RATE_PRIOR_AVG)
_COLUNAS_DA_SLICE: tuple[str, ...] = tuple(
    f"{base}{sufixo}" for base in _BASES_AS_OF for sufixo in ("_a", "_b", "_diff")
)

# Box-score do grappler puro: vetor de estilo ``(1, 0)``. Os três componentes do eixo de
# grappling saturam (3 quedas, 5 minutos de controle, 100% do golpe conectado no solo) e os
# dois do eixo de volume ficam em zero.
_GRAPPLER: dict[str, int] = {
    "sig_strikes_landed": 0,
    "sig_strikes_attempted": 0,
    "takedowns_landed": 3,
    "takedowns_attempted": 3,
    "control_time_seconds": 300,
    "distance_landed": 0,
    "clinch_landed": 0,
    "ground_landed": 10,
}
# Box-score do volumoso puro: vetor de estilo ``(0, 1)``. Seis golpes significativos por
# minuto (90 em 15 minutos), todos na distância; nenhuma queda, nenhum controle.
_VOLUMOSO: dict[str, int] = {
    "sig_strikes_landed": 90,
    "sig_strikes_attempted": 180,
    "takedowns_landed": 0,
    "takedowns_attempted": 0,
    "control_time_seconds": 0,
    "distance_landed": 10,
    "clinch_landed": 0,
    "ground_landed": 0,
}
# O enchimento existe para dar ao adversário um cartel e um estilo anteriores; o box-score
# dele não entra em conta nenhuma conferida aqui.
_SEM_STATS: dict[str, int] = {}

# Peso do adversário mais dissemelhante possível (``d**2 = 2`` com ``2*sigma**2 = 1``).
_PESO_DO_EXTREMO = math.exp(-2.0)

# Valores esperados na luta final, conferidos à mão (ver ``_seed_tres_niveis_de_historico``).
#
# A é grappler e enfrentou um volumoso (venceu) e um grappler (perdeu); o adversário atual, B,
# é volumoso -- então a vitória pesa 1 e a derrota pesa ``exp(-2)``.
_D3_A = 1.0 / (1.0 + _PESO_DO_EXTREMO)
# B é volumoso e venceu dois grapplers; o adversário atual, A, é grappler -- os dois pesam 1.
_D3_B = 1.0
# Os adversários anteriores de A chegaram com cartel 1,0 e 0,0; os de B, com 1,0 e 1,0.
_D4_A = 0.5
_D4_B = 1.0

_GRANULAR = (Bout, BoutFighter, BoutFighterRound, Fighter, Event)


def _seed_fighter(db_session: Session, name: str) -> Fighter:
    """Lutador com bio completa -- a base é irrelevante aqui, mas a coluna é lida a jusante."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=date(1990, 1, 1),
        height_cm=180,
        reach_cm=180,
        stance=Stance.ORTHODOX,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    db_session.add(fighter)
    db_session.flush()
    return fighter


def _corner(bout_id: int, fighter: Fighter, corner: Corner, stats: dict[str, int]) -> BoutFighter:
    """Um canto da luta, com o box-score dado (o que estiver fora de ``stats`` fica nulo)."""
    return BoutFighter(
        bout_id=bout_id,
        fighter_id=fighter.id,
        corner=corner,
        knockdowns=0,
        submission_attempts=0,
        total_strikes_landed=stats.get("sig_strikes_landed"),
        source="kaggle",
        **stats,
    )


def _seed_bout(
    db_session: Session,
    *,
    dia: int,
    vencedor: Fighter,
    perdedor: Fighter,
    stats_do_vencedor: dict[str, int],
    stats_do_perdedor: dict[str, int],
) -> int:
    """Semeia uma luta decidida de 3 rounds cheios; o vencedor ocupa o canto vermelho.

    ``dia`` é o deslocamento em dias a partir de 2023-01-01 e é o que impõe a cronologia da
    fixture -- o estilo e o cartel as-of de cada adversário só existem se a luta anterior dele
    vier antes. Devolve o ``bout_id``.
    """
    event = Event(
        name=f"UFC Test {dia}",
        date=date(2023, 1, 1) + timedelta(days=dia),
        location=None,
        source="kaggle",
    )
    db_session.add(event)
    db_session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=vencedor.id,
        method=BoutMethod.DECISION,
        round=3,
        ending_time_seconds=300,
        weight_class="Lightweight",
        title_bout=False,
        scheduled_rounds=3,
        source="kaggle",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            _corner(int(bout.id), vencedor, Corner.RED, stats_do_vencedor),
            _corner(int(bout.id), perdedor, Corner.BLUE, stats_do_perdedor),
        ]
    )
    db_session.flush()
    return int(bout.id)


def _seed_tres_niveis_de_historico(db_session: Session) -> int:
    """Semeia a fixture de três níveis e devolve o ``bout_id`` da luta final A vs B.

    Nível 1 (dias 0-30) -- cada adversário anterior ganha um cartel e um estilo as-of contra
    um enchimento: ``o_volumoso`` **vence** lutando volume, ``o_grappler`` **perde** lutando
    grappling, e os dois adversários de B (``b_um``/``b_dois``) **vencem** lutando grappling.

    Nível 2 (dias 40-70) -- as duas lutas anteriores de cada finalista: A (grappler) vence o
    volumoso e perde para o grappler; B (volumoso) vence os dois grapplers.

    Nível 3 (dia 90) -- a final A vs B, onde as seis colunas da slice são conferidas.
    """
    a = _seed_fighter(db_session, "Fighter A")
    b = _seed_fighter(db_session, "Fighter B")
    o_volumoso = _seed_fighter(db_session, "Opponent Volumoso")
    o_grappler = _seed_fighter(db_session, "Opponent Grappler")
    b_um = _seed_fighter(db_session, "Opponent B Um")
    b_dois = _seed_fighter(db_session, "Opponent B Dois")
    enchimentos = [_seed_fighter(db_session, f"Enchimento {indice}") for indice in range(4)]

    # Nível 1: o cartel e o estilo as-of de cada adversário anterior.
    _seed_bout(
        db_session,
        dia=0,
        vencedor=o_volumoso,
        perdedor=enchimentos[0],
        stats_do_vencedor=_VOLUMOSO,
        stats_do_perdedor=_SEM_STATS,
    )
    _seed_bout(
        db_session,
        dia=10,
        vencedor=enchimentos[1],
        perdedor=o_grappler,
        stats_do_vencedor=_SEM_STATS,
        stats_do_perdedor=_GRAPPLER,
    )
    for dia, adversario, enchimento in ((20, b_um, enchimentos[2]), (30, b_dois, enchimentos[3])):
        _seed_bout(
            db_session,
            dia=dia,
            vencedor=adversario,
            perdedor=enchimento,
            stats_do_vencedor=_GRAPPLER,
            stats_do_perdedor=_SEM_STATS,
        )

    # Nível 2: as duas lutas anteriores de cada finalista.
    _seed_bout(
        db_session,
        dia=40,
        vencedor=a,
        perdedor=o_volumoso,
        stats_do_vencedor=_GRAPPLER,
        stats_do_perdedor=_VOLUMOSO,
    )
    _seed_bout(
        db_session,
        dia=50,
        vencedor=o_grappler,
        perdedor=a,
        stats_do_vencedor=_GRAPPLER,
        stats_do_perdedor=_GRAPPLER,
    )
    for dia, adversario in ((60, b_um), (70, b_dois)):
        _seed_bout(
            db_session,
            dia=dia,
            vencedor=b,
            perdedor=adversario,
            stats_do_vencedor=_VOLUMOSO,
            stats_do_perdedor=_GRAPPLER,
        )

    # Nível 3: a final.
    return _seed_bout(
        db_session,
        dia=90,
        vencedor=a,
        perdedor=b,
        stats_do_vencedor=_GRAPPLER,
        stats_do_perdedor=_VOLUMOSO,
    )


def _matriz_recomputada(db_session: Session) -> MatchupMatrix:
    """Recomputa a matriz contra o granular vivo, pelo mesmo caminho de ``run_materialize``."""
    return build_matchup_matrix(_enriched_long_frame(db_session))


def _remover_do_cache(db_session: Session, colunas: tuple[str, ...]) -> None:
    """Remove as chaves dos payloads persistidos, simulando o cache anterior à slice."""
    for coluna in colunas:
        db_session.execute(
            text("UPDATE bout_features SET features = features - :chave"), {"chave": coluna}
        )
    db_session.flush()


def test_check_falha_nomeando_as_seis_colunas_e_volta_a_passar_apos_materializar(
    db_session: Session,
) -> None:
    """CA-11 (RF-08): a guarda acusa as 6 colunas novas e some após a re-materialização.

    Reproduz o estado que esta slice produz no banco real: o cache foi materializado antes de
    a pipeline ganhar os blocos D3 e D4, então as 6 chaves existem só no recomputo. Se a
    ``StaleFeatureCacheError`` subisse **sem nomear** as colunas, a guarda estaria cega -- e
    isso seria defeito a corrigir, não a contornar.
    """
    _seed_tres_niveis_de_historico(db_session)
    run_materialize(db_session)
    assert len(_COLUNAS_DA_SLICE) == 6
    _remover_do_cache(db_session, _COLUNAS_DA_SLICE)

    relatorio = check_cache_freshness(db_session, _matriz_recomputada(db_session))
    assert set(_COLUNAS_DA_SLICE) <= set(relatorio.column_diff)

    with pytest.raises(StaleFeatureCacheError) as excinfo:
        run_check(db_session)
    mensagem = str(excinfo.value)
    for coluna in _COLUNAS_DA_SLICE:
        assert coluna in mensagem, coluna
    assert "materialize" in mensagem

    run_materialize(db_session)
    assert run_check(db_session).is_stale is False


def test_payload_das_seis_colunas_e_numerico_ou_nulo_e_sobrevive_ao_dataset(
    db_session: Session,
) -> None:
    """CA-13/CA-15: as 6 colunas vão do granular ao treino sem perder nada pelo caminho.

    Os valores são os conferidos à mão no cabeçalho: A venceu um volumoso e perdeu para um
    grappler, e o adversário desta luta é volumoso -- a vitória pesa ``1`` e a derrota pesa
    ``exp(-2)``, então ``similar_style_win_rate_prior_a`` vale ``1 / (1 + exp(-2))``. B venceu
    dois grapplers e enfrenta um grappler: os dois pesam ``1`` e a feature dele vale ``1,0``.
    A força de calendário sai dos cartéis as-of dos adversários (1,0 e 0,0 para A; 1,0 e 1,0
    para B).

    E o granular não é tocado -- a re-materialização escreve só no cache derivado (CA-15).
    """
    bout_final = _seed_tres_niveis_de_historico(db_session)
    contagens_antes = {
        modelo: db_session.scalar(select(func.count()).select_from(modelo)) for modelo in _GRANULAR
    }

    run_materialize(db_session)

    row = db_session.get(BoutFeatures, bout_final)
    assert row is not None
    for coluna in _COLUNAS_DA_SLICE:
        assert coluna in row.features, coluna
        valor = row.features[coluna]
        assert valor is None or isinstance(valor, int | float), coluna
        assert not (isinstance(valor, float) and math.isnan(valor)), coluna
        assert valor != float("inf"), coluna

    assert row.features[f"{SIMILAR_STYLE_WIN_RATE_PRIOR}_a"] == pytest.approx(_D3_A)
    assert row.features[f"{SIMILAR_STYLE_WIN_RATE_PRIOR}_b"] == pytest.approx(_D3_B)
    assert row.features[f"{SIMILAR_STYLE_WIN_RATE_PRIOR}_diff"] == pytest.approx(_D3_A - _D3_B)
    assert row.features[f"{OPPONENT_WIN_RATE_PRIOR_AVG}_a"] == pytest.approx(_D4_A)
    assert row.features[f"{OPPONENT_WIN_RATE_PRIOR_AVG}_b"] == pytest.approx(_D4_B)
    assert row.features[f"{OPPONENT_WIN_RATE_PRIOR_AVG}_diff"] == pytest.approx(_D4_A - _D4_B)

    # As duas são features **do lutador**: nenhuma vira coluna única bout-level.
    for base in _BASES_AS_OF:
        assert base not in row.features, base

    # Nenhuma das 6 é descartada em silêncio a caminho do treino.
    dataset = build_dataset(read_bout_features(db_session))
    assert set(_COLUNAS_DA_SLICE) <= set(dataset.feature_names)

    # CA-15: o granular é fonte da verdade e não é escrito por nenhuma slice desta SPEC.
    for modelo, antes in contagens_antes.items():
        assert db_session.scalar(select(func.count()).select_from(modelo)) == antes, modelo


def test_rematerializar_duas_vezes_nao_muda_o_cache_nem_o_granular(db_session: Session) -> None:
    """CA-15: a re-materialização é idempotente -- rodar de novo não duplica nem altera nada.

    ``materialize_features`` faz ``INSERT ... ON CONFLICT (bout_id) DO UPDATE``; a segunda
    passada tem de deixar contagem e conteúdo idênticos. É a invariante de ingestão do projeto
    aplicada ao cache derivado.
    """
    bout_final = _seed_tres_niveis_de_historico(db_session)
    run_materialize(db_session)
    primeira = dict(db_session.get(BoutFeatures, bout_final).features)  # type: ignore[union-attr]
    contagem = db_session.scalar(select(func.count()).select_from(BoutFeatures))

    run_materialize(db_session)

    segunda = db_session.get(BoutFeatures, bout_final)
    assert segunda is not None
    assert segunda.features == primeira
    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == contagem
