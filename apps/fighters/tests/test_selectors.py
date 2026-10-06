"""Testes de Selector de fighters contra o Postgres de teste transacional.

Cobrem ``list_fighters`` (sem filtro, busca por nome **ou** apelido case-insensitive
por substring, curinga do ``LIKE`` valendo como texto literal nos dois campos,
paginação por limit/offset com ordem estável), ``get_fighter_by_id`` (encontrado
e inexistente), ``get_fighter_history`` (série cronológica, stats do canto
consultado, lutador sem lutas) e ``get_fighter_stats`` (médias e vitórias por
método computadas on demand, ignorando NULL, sem persistir agregado). Semeiam via
factories e exercitam o selector direto, sem subir a API.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.fighters.models import Fighter
from apps.fighters.selectors import (
    get_fighter_by_id,
    get_fighter_history,
    get_fighter_stats,
    list_fighters,
)
from apps.fighters.tests.factories import FighterFactory
from mma_analytics.db import Base


def _add(session: Session, name: str, *, nickname: str | None = None) -> Fighter:
    """Semeia um fighter com o ``name``/``nickname`` dados e devolve o model persistido.

    ``nickname`` é explícito (e nulo por padrão) para que os testes de curinga controlem
    o acervo inteiro: um apelido gerado ao acaso pela factory poderia introduzir o próprio
    caractere sob prova e tornar a asserção não-determinística.
    """
    fighter = FighterFactory.build(name=name, nickname=nickname)
    session.add(fighter)
    session.flush()
    return fighter


def _add_history_bout(
    session: Session,
    fighter: Fighter,
    *,
    event_name: str,
    event_date: date,
    won: bool,
    status: BoutStatus = BoutStatus.COMPLETED,
) -> Bout:
    """Semeia um evento, uma luta e o canto do ``fighter`` naquela luta.

    ``status`` é explícito para os testes da luta agendada (M9, RF-07): ela tem linha em
    ``bout_fighters`` como qualquer outra, e é por ela que vaza para histórico e agregados.
    """
    event = EventFactory.build(name=event_name, date=event_date)
    session.add(event)
    session.flush()
    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=fighter.id if won else None,
        method=BoutMethod.DECISION if status is BoutStatus.COMPLETED else None,
        status=status,
    )
    session.add(bout)
    session.flush()
    session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=fighter.id, corner=Corner.RED))
    session.flush()
    return bout


def test_list_fighters_sem_filtro_devolve_todos(db_session: Session) -> None:
    """Sem filtro, devolve todos os fighters e o ``total`` reflete o conjunto."""
    for name in ("Jon Jones", "Alex Pereira", "Israel Adesanya"):
        _add(db_session, name)

    rows, total = list_fighters(db_session, q=None, limit=50, offset=0)

    assert total == 3
    assert {row.name for row in rows} == {"Jon Jones", "Alex Pereira", "Israel Adesanya"}


def test_list_fighters_filtra_por_nome_case_insensitive(db_session: Session) -> None:
    """``q`` filtra o nome por substring ignorando caixa; ``total`` reflete o filtro."""
    _add(db_session, "Alexander Volkanovski")
    _add(db_session, "Alex Pereira")
    _add(db_session, "Jon Jones")

    rows, total = list_fighters(db_session, q="alex", limit=50, offset=0)

    assert total == 2
    assert {row.name for row in rows} == {"Alexander Volkanovski", "Alex Pereira"}


def test_list_fighters_encontra_pelo_apelido(db_session: Session) -> None:
    """``q`` também casa o apelido: "bones" devolve Jon Jones, cujo nome não contém o termo."""
    _add(db_session, "Jon Jones", nickname="Bones")
    _add(db_session, "Alex Pereira", nickname="Poatan")

    rows, total = list_fighters(db_session, q="bones", limit=50, offset=0)

    assert total == 1
    assert [row.name for row in rows] == ["Jon Jones"]


def test_list_fighters_percentual_nao_e_curinga(db_session: Session) -> None:
    """CA-26a: num acervo sem ``%``, buscar ``%`` devolve zero -- não o acervo inteiro."""
    _add(db_session, "Jon Jones", nickname="Bones")
    _add(db_session, "Alex Pereira", nickname="Poatan")

    rows, total = list_fighters(db_session, q="%", limit=50, offset=0)

    assert rows == []
    assert total == 0


def test_list_fighters_encontra_apelido_com_percentual_literal(db_session: Session) -> None:
    """CA-26b: o ``%`` digitado é texto, então o apelido que o contém é encontrado."""
    alvo = _add(db_session, "Jon Jones", nickname="100%")
    _add(db_session, "Alex Pereira", nickname="Poatan")

    rows, total = list_fighters(db_session, q="100%", limit=50, offset=0)

    assert total == 1
    assert [row.id for row in rows] == [alvo.id]


def test_list_fighters_underline_no_apelido_nao_e_curinga(db_session: Session) -> None:
    """CA-26c: ``_`` no apelido é literal -- ``The_Rock`` não casa ``TheXRock``."""
    alvo = _add(db_session, "Dwayne Johnson", nickname="The_Rock")
    _add(db_session, "Brock Lesnar", nickname="TheXRock")

    rows, total = list_fighters(db_session, q="The_Rock", limit=50, offset=0)

    assert total == 1
    assert [row.id for row in rows] == [alvo.id]


def test_list_fighters_underline_no_nome_nao_e_curinga(db_session: Session) -> None:
    """CA-26c no campo **nome**: o mesmo padrão escapado alimenta os dois ``ilike``.

    Espelha o caso do apelido porque um escape aplicado só ao ``ilike`` do apelido
    passaria despercebido pelos casos da SPEC, que usam apelido nos dois lados.
    """
    alvo = _add(db_session, "The_Rock")
    _add(db_session, "TheXRock")

    rows, total = list_fighters(db_session, q="The_Rock", limit=50, offset=0)

    assert total == 1
    assert [row.id for row in rows] == [alvo.id]


def test_list_fighters_pagina_com_ordem_estavel(db_session: Session) -> None:
    """``limit``/``offset`` paginam sobre uma ordem determinística por ``name``."""
    for name in ("Charlie", "Alpha", "Bravo", "Delta"):
        _add(db_session, name)

    primeira, total = list_fighters(db_session, q=None, limit=2, offset=0)
    segunda, _ = list_fighters(db_session, q=None, limit=2, offset=2)

    assert total == 4
    assert [row.name for row in primeira] == ["Alpha", "Bravo"]
    assert [row.name for row in segunda] == ["Charlie", "Delta"]


def test_list_fighters_nome_sem_match_devolve_vazio(db_session: Session) -> None:
    """Filtro sem correspondência devolve lista vazia e ``total`` zero."""
    _add(db_session, "Jon Jones")

    rows, total = list_fighters(db_session, q="inexistente", limit=50, offset=0)

    assert rows == []
    assert total == 0


def test_list_fighters_offset_alem_do_total_devolve_vazio(db_session: Session) -> None:
    """``offset`` além do total devolve página vazia, mas ``total`` cheio."""
    _add(db_session, "Jon Jones")
    _add(db_session, "Alex Pereira")

    rows, total = list_fighters(db_session, q=None, limit=50, offset=10)

    assert rows == []
    assert total == 2


def test_get_fighter_by_id_encontrado_e_inexistente(db_session: Session) -> None:
    """Devolve o fighter pelo id e ``None`` quando o id não existe."""
    fighter = _add(db_session, "Jon Jones")

    assert get_fighter_by_id(db_session, fighter.id) is fighter
    assert get_fighter_by_id(db_session, 999_999) is None


def test_get_fighter_history_ordem_cronologica_ascendente(db_session: Session) -> None:
    """Lutas semeadas fora de ordem saem por ``events.date`` ascendente."""
    fighter = _add(db_session, "Max Holloway")
    _add_history_bout(
        db_session, fighter, event_name="UFC 300", event_date=date(2024, 4, 13), won=True
    )
    _add_history_bout(
        db_session, fighter, event_name="UFC 200", event_date=date(2016, 7, 9), won=False
    )
    _add_history_bout(
        db_session, fighter, event_name="UFC 250", event_date=date(2020, 6, 6), won=True
    )

    history = get_fighter_history(db_session, fighter.id)

    assert [row.event.date for row in history] == [
        date(2016, 7, 9),
        date(2020, 6, 6),
        date(2024, 4, 13),
    ]
    assert [row.event.name for row in history] == ["UFC 200", "UFC 250", "UFC 300"]


def test_get_fighter_history_traz_stats_do_canto_consultado(db_session: Session) -> None:
    """Numa luta de dois cantos, devolve as stats do lutador consultado, não do oponente."""
    fighter = _add(db_session, "Charles Oliveira")
    opponent = _add(db_session, "Justin Gaethje")
    event = EventFactory.build(name="UFC 274", date=date(2022, 5, 7))
    db_session.add(event)
    db_session.flush()
    bout = BoutFactory.build(event_id=event.id, winner_id=fighter.id, method=BoutMethod.SUBMISSION)
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=fighter.id,
                corner=Corner.RED,
                sig_strikes_landed=30,
                control_time_seconds=120,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=opponent.id,
                corner=Corner.BLUE,
                sig_strikes_landed=8,
                control_time_seconds=10,
            ),
        ]
    )
    db_session.flush()

    history = get_fighter_history(db_session, fighter.id)

    assert len(history) == 1
    row = history[0]
    assert row.stats.fighter_id == fighter.id
    assert row.stats.corner == Corner.RED
    assert row.stats.sig_strikes_landed == 30
    assert row.stats.control_time_seconds == 120
    assert row.bout.id == bout.id
    # Identidade do próprio lutador e do adversário (o outro canto) -- enrich SPA.
    assert row.stats.fighter.name == "Charles Oliveira"
    assert row.opponent is not None
    assert row.opponent.fighter_id == opponent.id
    assert row.opponent.fighter.name == "Justin Gaethje"


def test_get_fighter_history_sem_adversario_devolve_opponent_none(db_session: Session) -> None:
    """Luta com um único canto (dados sujos) devolve ``opponent`` nulo, sem quebrar."""
    fighter = _add(db_session, "Jon Jones")
    _add_history_bout(
        db_session, fighter, event_name="UFC 285", event_date=date(2023, 3, 4), won=True
    )

    history = get_fighter_history(db_session, fighter.id)

    assert len(history) == 1
    assert history[0].opponent is None


def test_get_fighter_history_lutador_sem_lutas_devolve_vazio(db_session: Session) -> None:
    """Lutador existente sem lutas devolve sequência vazia (distinto de not-found)."""
    fighter = _add(db_session, "Ilia Topuria")

    assert get_fighter_history(db_session, fighter.id) == []


def _add_stats_bout(
    session: Session,
    fighter: Fighter,
    *,
    method: BoutMethod,
    winner: Fighter | None,
    sig_strikes_landed: int | None = None,
    takedowns_landed: int | None = None,
    control_time_seconds: int | None = None,
) -> Bout:
    """Semeia um evento, uma luta e o canto do ``fighter`` com stats conhecidas.

    ``winner`` é o lutador vencedor (``None`` em empate/no contest -> ``winner_id``
    nulo). As stats são as do canto do ``fighter`` consultado naquela luta.
    """
    event = EventFactory.build()
    session.add(event)
    session.flush()
    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=winner.id if winner is not None else None,
        method=method,
    )
    session.add(bout)
    session.flush()
    session.add(
        BoutFighterFactory.build(
            bout_id=bout.id,
            fighter_id=fighter.id,
            corner=Corner.RED,
            sig_strikes_landed=sig_strikes_landed,
            takedowns_landed=takedowns_landed,
            control_time_seconds=control_time_seconds,
        )
    )
    session.flush()
    return bout


def _table_counts(session: Session) -> dict[str, int]:
    """Contagem de linhas das tabelas do domínio, para provar leitura sem escrita."""
    return {
        "fighters": session.scalar(select(func.count()).select_from(Fighter)) or 0,
        "bouts": session.scalar(select(func.count()).select_from(Bout)) or 0,
        "bout_fighters": session.scalar(select(func.count()).select_from(BoutFighter)) or 0,
    }


def test_get_fighter_stats_computes_averages_and_wins(db_session: Session) -> None:
    """Médias e vitórias por método computadas on demand a partir de ``bout_fighters``."""
    target = _add(db_session, "Alexander Volkanovski")
    _add_stats_bout(
        db_session,
        target,
        method=BoutMethod.KO_TKO,
        winner=target,
        sig_strikes_landed=10,
        takedowns_landed=2,
        control_time_seconds=60,
    )
    _add_stats_bout(
        db_session,
        target,
        method=BoutMethod.DECISION,
        winner=target,
        sig_strikes_landed=20,
        takedowns_landed=4,
        control_time_seconds=120,
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.fighter_id == target.id
    assert stats.bouts_counted == 2
    assert stats.avg_sig_strikes_landed == 15.0
    assert stats.avg_takedowns_landed == 3.0
    assert stats.avg_control_time_seconds == 90.0
    assert stats.wins_by_method == {"ko_tko": 1, "decision": 1}


def test_get_fighter_stats_avg_ignora_null(db_session: Session) -> None:
    """``func.avg`` ignora NULL; a luta com stat nula ainda conta em ``bouts_counted``."""
    target = _add(db_session, "Islam Makhachev")
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=10
    )
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=None
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.bouts_counted == 2  # ambas as lutas contam
    assert stats.avg_sig_strikes_landed == 10.0  # média só sobre o valor não-nulo


def test_get_fighter_stats_vitorias_excluem_derrota_e_empate(db_session: Session) -> None:
    """Só ``winner_id == fighter_id`` conta como vitória; derrota e empate não entram."""
    target = _add(db_session, "Max Holloway")
    opponent = _add(db_session, "Dustin Poirier")
    _add_stats_bout(db_session, target, method=BoutMethod.KO_TKO, winner=target)
    _add_stats_bout(db_session, target, method=BoutMethod.DECISION, winner=opponent)
    _add_stats_bout(db_session, target, method=BoutMethod.DECISION, winner=None)

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.bouts_counted == 3
    assert stats.wins_by_method == {"ko_tko": 1}


def _add_split_bout(
    session: Session,
    fighter: Fighter,
    *,
    head_landed: int | None,
    body_landed: int | None,
    leg_landed: int | None,
    distance_landed: int | None,
    clinch_landed: int | None,
    ground_landed: int | None,
) -> None:
    """Semeia uma luta do ``fighter`` com os splits de golpe conectados dados."""
    evento = EventFactory.build()
    session.add(evento)
    session.flush()
    bout = BoutFactory.build(event_id=evento.id, method=BoutMethod.DECISION)
    session.add(bout)
    session.flush()
    session.add(
        BoutFighterFactory.build(
            bout_id=bout.id,
            fighter_id=fighter.id,
            corner=Corner.RED,
            head_landed=head_landed,
            body_landed=body_landed,
            leg_landed=leg_landed,
            distance_landed=distance_landed,
            clinch_landed=clinch_landed,
            ground_landed=ground_landed,
        )
    )
    session.flush()


def test_get_fighter_stats_striking_profile_soma_sobre_soma(db_session: Session) -> None:
    """Shares são razão de somas na carreira (não média de razões por luta)."""
    target = _add(db_session, "Max Holloway")
    _add_split_bout(
        db_session,
        target,
        head_landed=20,
        body_landed=10,
        leg_landed=0,
        distance_landed=25,
        clinch_landed=5,
        ground_landed=0,
    )
    _add_split_bout(
        db_session,
        target,
        head_landed=10,
        body_landed=0,
        leg_landed=10,
        distance_landed=10,
        clinch_landed=0,
        ground_landed=10,
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    perfil = stats.striking_profile
    # Alvo: head=30, body=10, leg=10 -> total 50.
    assert perfil.share_head == 0.6
    assert perfil.share_body == 0.2
    assert perfil.share_leg == 0.2
    # Posição: distância=35, clinch=5, solo=10 -> total 50.
    assert perfil.share_distance == 0.7
    assert perfil.share_clinch == 0.1
    assert perfil.share_ground == 0.2


def test_get_fighter_stats_striking_profile_denominador_zero_da_none(
    db_session: Session,
) -> None:
    """Sem golpes conectados no grupo, os shares são ``None`` (nunca inf/NaN)."""
    target = _add(db_session, "Khabib Nurmagomedov")
    _add_split_bout(
        db_session,
        target,
        head_landed=None,
        body_landed=None,
        leg_landed=None,
        distance_landed=None,
        clinch_landed=None,
        ground_landed=None,
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    perfil = stats.striking_profile
    assert perfil.share_head is None
    assert perfil.share_body is None
    assert perfil.share_leg is None
    assert perfil.share_distance is None
    assert perfil.share_clinch is None
    assert perfil.share_ground is None


def test_get_fighter_stats_striking_profile_componente_nulo_conta_como_zero(
    db_session: Session,
) -> None:
    """Componente ``None`` no grupo conta como zero conectado; o grupo ainda soma."""
    target = _add(db_session, "Israel Adesanya")
    _add_split_bout(
        db_session,
        target,
        head_landed=30,
        body_landed=None,
        leg_landed=10,
        distance_landed=40,
        clinch_landed=None,
        ground_landed=None,
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    perfil = stats.striking_profile
    # Alvo: head=30, body=0 (None), leg=10 -> total 40.
    assert perfil.share_head == 0.75
    assert perfil.share_body == 0.0
    assert perfil.share_leg == 0.25
    # Posição: distância=40, clinch=0, solo=0 -> total 40.
    assert perfil.share_distance == 1.0


def test_get_fighter_stats_lutador_inexistente_devolve_none(db_session: Session) -> None:
    """Lutador inexistente devolve ``None`` (habilita o 404, distinto de sem lutas)."""
    assert get_fighter_stats(db_session, 999_999) is None


def test_get_fighter_stats_lutador_sem_lutas(db_session: Session) -> None:
    """Lutador existente sem lutas: contagem zero, médias ``None`` e vitórias vazias."""
    target = _add(db_session, "Ilia Topuria")

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.fighter_id == target.id
    assert stats.bouts_counted == 0
    assert stats.avg_sig_strikes_landed is None
    assert stats.avg_takedowns_landed is None
    assert stats.avg_control_time_seconds is None
    assert stats.wins_by_method == {}
    assert stats.striking_profile.share_head is None
    assert stats.striking_profile.share_distance is None


def test_get_fighter_stats_on_demand_sem_persistencia_de_agregado(db_session: Session) -> None:
    """Invariante (RF-08): agregação recalculada ao vivo, sem escrita nem tabela de agregado."""
    target = _add(db_session, "Jon Jones")
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=10
    )

    # (1) Recálculo ao vivo: mutar o dado granular muda a média.
    antes = get_fighter_stats(db_session, target.id)
    assert antes is not None
    assert antes.avg_sig_strikes_landed == 10.0
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=90
    )
    depois = get_fighter_stats(db_session, target.id)
    assert depois is not None
    assert depois.avg_sig_strikes_landed == 50.0
    assert depois.avg_sig_strikes_landed != antes.avg_sig_strikes_landed

    # (2) Sem efeito de escrita: consultar o selector não altera a contagem de linhas.
    contagens_antes = _table_counts(db_session)
    get_fighter_stats(db_session, target.id)
    assert _table_counts(db_session) == contagens_antes

    # (3) Sem tabela/coluna de agregado no schema.
    assert not any("stat" in table for table in Base.metadata.tables)
    fighter_columns = {column.key for column in Base.metadata.tables["fighters"].columns}
    assert not any(column.startswith(("avg_", "mean_", "total_")) for column in fighter_columns)


def test_get_fighter_history_ignora_a_luta_agendada(db_session: Session) -> None:
    """Ponto 3 da varredura (M9, RF-07): histórico é **passado**.

    A luta já marcada do lutador chega à tela pelo card do evento futuro. Incluí-la aqui
    somaria ao cartel uma luta que ainda não aconteceu, com desfecho nulo.
    """
    fighter = _add(db_session, "Alexander Volkanovski")
    realizada = _add_history_bout(
        db_session, fighter, event_name="UFC 245", event_date=date(2019, 12, 14), won=True
    )
    agendada = _add_history_bout(
        db_session,
        fighter,
        event_name="UFC Card Futuro",
        event_date=date(2026, 12, 12),
        won=False,
        status=BoutStatus.SCHEDULED,
    )

    historico = get_fighter_history(db_session, fighter.id)

    assert [linha.bout.id for linha in historico] == [realizada.id]
    # Guarda de não-vacuidade: a agendada está persistida, com o canto do lutador.
    persistida = db_session.get(Bout, agendada.id)
    assert persistida is not None
    assert persistida.status is BoutStatus.SCHEDULED
    assert len(persistida.bout_fighters) == 1


def _add_scheduled_bout_com_cantos_nulos(session: Session, fighter: Fighter) -> Bout:
    """Semeia uma luta **agendada** do lutador: sem desfecho e com stats nulas no canto.

    Stats nulas de propósito -- é o estado real de um card por vir, e é exatamente o que
    torna o defeito do ponto 9 silencioso: ``func.avg`` do Postgres ignora ``NULL``, então
    as médias sobrevivem intactas enquanto ``func.count`` conta a linha.
    """
    evento = EventFactory.build(name="UFC Card Futuro", date=date(2026, 12, 12))
    session.add(evento)
    session.flush()
    bout = BoutFactory.build(
        event_id=evento.id, winner_id=None, method=None, status=BoutStatus.SCHEDULED
    )
    session.add(bout)
    session.flush()
    session.add(
        BoutFighterFactory.build(
            bout_id=bout.id,
            fighter_id=fighter.id,
            corner=Corner.RED,
            sig_strikes_landed=None,
            takedowns_landed=None,
            control_time_seconds=None,
            head_landed=None,
            body_landed=None,
            leg_landed=None,
            distance_landed=None,
            clinch_landed=None,
            ground_landed=None,
        )
    )
    session.flush()
    return bout


def test_get_fighter_stats_ignora_agendada(db_session: Session) -> None:
    """Ponto 9 da varredura (M9, RF-07): a **contagem** é quem denuncia o defeito.

    Sem o filtro, ``bouts_counted`` e o denominador do ``striking_profile`` inflam com uma
    luta que ainda não aconteceu, enquanto as médias permanecem corretas -- ``func.avg`` do
    Postgres ignora ``NULL``. Asserir só a média deixaria o defeito passar; por isso o
    assert obrigatório é sobre ``bouts_counted``.

    ``wins_by_method`` é conferido junto como caracterização: a query de vitórias filtra
    ``winner_id``, e luta agendada tem vencedor nulo -- ela já era imune, e isso fica fixado
    em vez de virar suposição.
    """
    target = _add(db_session, "Max Holloway")
    for numero in (245, 251):
        _add_history_bout(
            db_session,
            target,
            event_name=f"UFC {numero}",
            event_date=date(2019, 12, 14),
            won=True,
        )
    antes = get_fighter_stats(db_session, target.id)
    assert antes is not None

    _add_scheduled_bout_com_cantos_nulos(db_session, target)
    depois = get_fighter_stats(db_session, target.id)

    assert depois is not None
    assert depois.bouts_counted == 2
    assert depois.bouts_counted == antes.bouts_counted
    assert depois.striking_profile == antes.striking_profile
    assert depois.wins_by_method == antes.wins_by_method


def test_get_fighter_stats_agendada_nao_move_as_medias(db_session: Session) -> None:
    """A assimetria do ponto 9, fixada: as médias passam mesmo com o defeito presente.

    Existe para documentar por que o teste acima olha a contagem. Se algum dia a média
    também passar a se mover, é sinal de que o canto agendado ganhou stats -- e aí o recorte
    precisa ser revisto, não este assert.
    """
    target = _add(db_session, "Max Holloway")
    _add_split_bout(
        db_session,
        target,
        head_landed=20,
        body_landed=10,
        leg_landed=0,
        distance_landed=25,
        clinch_landed=5,
        ground_landed=0,
    )
    antes = get_fighter_stats(db_session, target.id)
    assert antes is not None

    _add_scheduled_bout_com_cantos_nulos(db_session, target)
    depois = get_fighter_stats(db_session, target.id)

    assert depois is not None
    assert depois.avg_sig_strikes_landed == antes.avg_sig_strikes_landed
    assert depois.avg_takedowns_landed == antes.avg_takedowns_landed
    assert depois.avg_control_time_seconds == antes.avg_control_time_seconds
