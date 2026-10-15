"""Testes da renormalização das chaves de dedup já persistidas (SPEC 011, CA-10a / CA-10b).

A correção do ``normalize_name`` (RF-09) muda a chave de linhas que **já estão** em
``fighters.name_normalized`` -- coluna persistida e indexada. Corrigir só a função deixaria a
chave nova sem casar com a linha antiga: o 'Michał Oleksiejczuk' de um card agendado **não**
reconheceria o 'michal oleksiejczuk' do seed e duplicaria o lutador, sem erro nenhum. Daí o
comando de renormalização, e daí ele **medir antes de aplicar**.

O ponto delicado é a distinção entre três classes de linha, e os testes existem para fixá-la:

- chave muda e não colide com ninguém -> renormalizável;
- par que **passa a** compartilhar a chave e colapsaria (mesma DOB, ou DOB nula de um lado,
  que é o caso em que ``match_fighter_id`` levanta ``AmbiguousFighterMatchError``) -> **colisão
  nova**, que PARA a sprint;
- homônimo real que **já hoje** compartilha a chave, separado por DOBs conhecidas e distintas
  (os dois ``Bruno Silva`` do seed) -> **não** é colisão nova. Contá-lo transformaria o gate
  num falso positivo permanente, travando num estado pré-existente e legítimo.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.features.models import FighterAsOfFeatures
from apps.fighters.models import Fighter
from apps.predictions.models import BoutPrediction
from ingestion import renormalize_names
from ingestion.renormalize_names import (
    FighterMergeError,
    NewCollisionError,
    PersistedName,
    apply_renormalization,
    load_persisted_names,
    main,
    merge_fighters,
    plan_renormalization,
)

_DOB_A = date(1983, 2, 24)
_DOB_B = date(1990, 7, 1)


@pytest.fixture
def cli_isolado(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Aponta o ``SessionLocal`` do comando para a sessão transacional do teste.

    Sem isto o CLI abriria conexão própria, fora da transação do teste, e o ``--aplicar``
    escaparia do rollback -- escrevendo de verdade no banco de teste.
    """

    @contextmanager
    def _sessao() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(renormalize_names, "SessionLocal", _sessao)
    yield


def _row(
    fighter_id: int, name: str, name_normalized: str, dob: date | None = None
) -> PersistedName:
    """Linha persistida de ``fighters`` como o planejador a enxerga."""
    return PersistedName(
        id=fighter_id, name=name, name_normalized=name_normalized, date_of_birth=dob
    )


def _fighter(session: Session, fighter_id: int, name: str, key: str, dob: date | None) -> Fighter:
    """Semeia um lutador com a chave normalizada **crua** (o estado pré-correção)."""
    fighter = Fighter(
        id=fighter_id,
        name=name,
        name_normalized=key,
        nickname=None,
        date_of_birth=dob,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    session.add(fighter)
    session.flush()
    return fighter


# --- plan_renormalization: o que muda ----------------------------------------------------


def test_linha_cuja_chave_muda_sem_colidir_e_renormalizavel() -> None:
    """'Jan Błachowicz' persistido como 'jan bachowicz' passa a 'jan blachowicz'."""
    plano = plan_renormalization([_row(1, "Jan Błachowicz", "jan bachowicz")])

    assert plano.changed == ((1, "jan bachowicz", "jan blachowicz"),)
    assert plano.new_collisions == ()
    assert plano.is_safe_to_apply


def test_linha_cuja_chave_nao_muda_fica_fora_do_plano() -> None:
    """Nome sem letra atômica já está na chave correta; renormalizar seria UPDATE inútil."""
    plano = plan_renormalization([_row(1, "Alexander Volkanovski", "alexander volkanovski")])

    assert plano.changed == ()
    assert plano.is_safe_to_apply


def test_plano_e_ordenado_por_id_para_relatorio_estavel() -> None:
    """A ordem de ``changed`` é determinística: o relatório não muda de forma entre execuções."""
    plano = plan_renormalization(
        [
            _row(9, "Jan Błachowicz", "jan bachowicz"),
            _row(2, "Michał Oleksiejczuk", "micha oleksiejczuk"),
        ]
    )

    assert [fighter_id for fighter_id, _antiga, _nova in plano.changed] == [2, 9]


# --- plan_renormalization: o que é colisão NOVA (CA-10b) ---------------------------------


def test_par_que_passa_a_colidir_com_a_mesma_dob_e_colisao_nova() -> None:
    """Mesma DOB: a entity resolution COLAPSARIA os dois -- achado, não efeito colateral."""
    plano = plan_renormalization(
        [
            _row(1, "Jan Błachowicz", "jan bachowicz", _DOB_A),
            _row(2, "Jan Blachowicz", "jan blachowicz", _DOB_A),
        ]
    )

    assert plano.new_collisions == ((1, 2, "jan blachowicz"),)
    assert not plano.is_safe_to_apply


def test_par_que_passa_a_colidir_com_dob_nula_de_um_lado_e_colisao_nova() -> None:
    """DOB nula de um lado: ``match_fighter_id`` levanta ambiguidade -- também bloqueia."""
    plano = plan_renormalization(
        [
            _row(1, "Jan Błachowicz", "jan bachowicz", None),
            _row(2, "Jan Blachowicz", "jan blachowicz", _DOB_A),
        ]
    )

    assert plano.new_collisions == ((1, 2, "jan blachowicz"),)
    assert not plano.is_safe_to_apply


def test_homonimo_real_que_ja_colide_hoje_nao_e_colisao_nova() -> None:
    """Os dois ``Bruno Silva`` já compartilham a chave e a DOB os separa: não bloqueia nada."""
    plano = plan_renormalization(
        [
            _row(1, "Bruno Silva", "bruno silva", _DOB_A),
            _row(2, "Bruno Silva", "bruno silva", _DOB_B),
        ]
    )

    assert plano.changed == ()
    assert plano.new_collisions == ()
    assert plano.is_safe_to_apply


def test_par_que_passa_a_colidir_com_dobs_distintas_e_homonimo_nao_colisao() -> None:
    """Chave nova compartilhada mas DOBs conhecidas e distintas: a DOB segue desempatando."""
    plano = plan_renormalization(
        [
            _row(1, "Jan Błachowicz", "jan bachowicz", _DOB_A),
            _row(2, "Jan Blachowicz", "jan blachowicz", _DOB_B),
        ]
    )

    assert plano.changed == ((1, "jan bachowicz", "jan blachowicz"),)
    assert plano.new_collisions == ()
    assert plano.is_safe_to_apply


def test_colisao_nova_e_reportada_com_os_ids_em_ordem_crescente() -> None:
    """O par vem ordenado (menor id primeiro): o relatório precisa ser estável."""
    plano = plan_renormalization(
        [
            _row(7, "Jan Błachowicz", "jan bachowicz", None),
            _row(3, "Jan Blachowicz", "jan blachowicz", None),
        ]
    )

    assert plano.new_collisions == ((3, 7, "jan blachowicz"),)


# --- apply_renormalization: a escrita (CA-10a) -------------------------------------------


def test_apply_atualiza_so_a_linha_que_muda_de_chave(db_session: Session) -> None:
    """A linha com chave correta não recebe UPDATE; a defasada recebe a chave nova."""
    defasada = _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)
    intocada = _fighter(db_session, 9002, "Alexander Volkanovski", "alexander volkanovski", _DOB_B)

    plano = plan_renormalization(load_persisted_names(db_session))
    assert apply_renormalization(db_session, plano) == 1

    db_session.flush()
    assert defasada.name_normalized == "jan blachowicz"
    assert intocada.name_normalized == "alexander volkanovski"


def test_apply_nao_sobrescreve_o_source_da_linha(db_session: Session) -> None:
    """``source`` é a origem da LINHA, não do campo: recalcular a chave o mantém."""
    fighter = _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)

    apply_renormalization(db_session, plan_renormalization(load_persisted_names(db_session)))

    db_session.flush()
    assert fighter.source == "kaggle"


def test_apply_e_idempotente_a_segunda_execucao_nao_muda_nada(db_session: Session) -> None:
    """Depois de aplicada, a renormalização vira no-op: ``changed`` zera."""
    _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)

    apply_renormalization(db_session, plan_renormalization(load_persisted_names(db_session)))
    db_session.flush()

    segundo = plan_renormalization(load_persisted_names(db_session))
    assert segundo.changed == ()
    assert apply_renormalization(db_session, segundo) == 0


def test_apply_recusa_plano_com_colisao_nova_sem_escrever_nada(db_session: Session) -> None:
    """Colisão nova não se resolve mesclando nem escolhendo: nada é escrito e a função levanta."""
    cito = _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)
    kaggle = _fighter(db_session, 9002, "Jan Blachowicz", "jan blachowicz", _DOB_A)

    plano = plan_renormalization(load_persisted_names(db_session))
    with pytest.raises(NewCollisionError) as excinfo:
        apply_renormalization(db_session, plano)

    db_session.flush()
    assert cito.name_normalized == "jan bachowicz"
    assert kaggle.name_normalized == "jan blachowicz"
    assert "9001" in str(excinfo.value) and "9002" in str(excinfo.value)


# --- CLI: o default é MEDIR ---------------------------------------------------------------


def test_cli_em_modo_medicao_nao_escreve_nada(
    cli_isolado: None, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Sem ``--aplicar``, o comando mede e reporta -- zero escrita (medir antes de aplicar)."""
    fighter = _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)

    with caplog.at_level(logging.INFO, logger="ingestion.renormalize_names"):
        main([])

    db_session.flush()
    assert fighter.name_normalized == "jan bachowicz"
    assert "1 mudam de chave" in caplog.text
    assert "modo medição" in caplog.text


def test_cli_com_aplicar_grava_as_chaves_novas(cli_isolado: None, db_session: Session) -> None:
    """``--aplicar`` escreve; é o passo que só acontece depois da medição limpa."""
    fighter = _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)

    main(["--aplicar"])

    db_session.flush()
    assert fighter.name_normalized == "jan blachowicz"


def test_cli_com_aplicar_aborta_diante_de_colisao_nova(
    cli_isolado: None, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Colisão nova aborta com ``SystemExit(1)`` e deixa as chaves como estavam (CA-10b)."""
    cito = _fighter(db_session, 9001, "Jan Błachowicz", "jan bachowicz", None)
    _fighter(db_session, 9002, "Jan Blachowicz", "jan blachowicz", _DOB_A)

    with (
        caplog.at_level(logging.WARNING, logger="ingestion.renormalize_names"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--aplicar"])

    assert excinfo.value.code == 1
    db_session.flush()
    assert cito.name_normalized == "jan bachowicz"
    assert "COLISÃO NOVA" in caplog.text


# --- merge_fighters: a mesclagem da duplicata (decisão humana de 2026-10-14) -------------
#
# A colisão que o gate detectou era um lutador em DUAS linhas (Jan Błachowicz: 596 semeado do
# Kaggle, 2668 criado pela Cito). A resolução mora aqui, e não num script à parte, porque a
# ferramenta que **detecta** a colisão é o lugar coeso para **resolvê-la** -- e porque reparo
# único de banco existente não é migration: depois da correção do ``normalize_name``, um
# seed + incremental do zero nunca recria o par.


def _bout_com_cantos(
    session: Session,
    event: Event,
    red_id: int,
    blue_id: int,
    *,
    winner_id: int | None,
    method: BoutMethod | None = BoutMethod.DECISION,
) -> Bout:
    """Semeia uma luta realizada com os dois cantos apontando para os ids dados."""
    bout = Bout(
        event_id=event.id,
        winner_id=winner_id,
        method=method,
        round=3,
        status=BoutStatus.COMPLETED,
        source="cito",
    )
    session.add(bout)
    session.flush()
    for fighter_id, corner in ((red_id, Corner.RED), (blue_id, Corner.BLUE)):
        session.add(
            BoutFighter(bout_id=bout.id, fighter_id=fighter_id, corner=corner, source="cito")
        )
    session.flush()
    return bout


def _event(session: Session, name: str) -> Event:
    """Semeia um evento mínimo para pendurar as lutas da mesclagem."""
    event = Event(name=name, date=date(2025, 12, 6), location=None, source="cito")
    session.add(event)
    session.flush()
    return event


def test_merge_reaponta_os_cantos_da_duplicata_para_o_sobrevivente(db_session: Session) -> None:
    """Os ``bout_fighters`` da duplicata passam a apontar o sobrevivente (CA da mesclagem)."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    adversario = _fighter(db_session, 9003, "Bogdan Guskov", "bogdan guskov", _DOB_B)
    evento = _event(db_session, "UFC 323")
    luta = _bout_com_cantos(db_session, evento, dup.id, adversario.id, winner_id=None)

    resultado = merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    cantos = db_session.scalars(select(BoutFighter).where(BoutFighter.bout_id == luta.id)).all()
    assert sorted(canto.fighter_id for canto in cantos) == sorted([alvo.id, adversario.id])
    assert resultado.bout_fighters_repointed == 1


def test_merge_reaponta_winner_id_e_predicao(db_session: Session) -> None:
    """``bouts.winner_id`` e ``bout_predictions.predicted_winner_id`` seguem a duplicata."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    adversario = _fighter(db_session, 9003, "Bogdan Guskov", "bogdan guskov", _DOB_B)
    evento = _event(db_session, "UFC 323")
    luta = _bout_com_cantos(db_session, evento, dup.id, adversario.id, winner_id=dup.id)
    predicao = BoutPrediction(
        bout_id=luta.id,
        predicted_winner_id=dup.id,
        prob_predicted_winner=0.6,
        model_version="teste",
        n_features=1,
        predicted_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="walk_forward",
    )
    db_session.add(predicao)
    db_session.flush()

    resultado = merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    repontada = db_session.get(Bout, luta.id)
    assert repontada is not None
    assert repontada.winner_id == alvo.id
    assert predicao.predicted_winner_id == alvo.id
    assert resultado.winners_repointed == 1
    assert resultado.predictions_repointed == 1


def test_merge_adota_a_grafia_correta_da_duplicata(db_session: Session) -> None:
    """O sobrevivente herda ``Jan Błachowicz`` -- é o nome real da pessoa."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)

    merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    assert alvo.name == "Jan Błachowicz"
    assert alvo.name_normalized == "jan blachowicz"


def test_merge_mantem_o_source_do_sobrevivente(db_session: Session) -> None:
    """``source`` é a origem da LINHA: a linha sobrevivente continua ``kaggle``."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)

    merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    assert alvo.source == "kaggle"


def test_merge_recalcula_o_cartel_somando_o_desfecho_das_lutas_movidas(
    db_session: Session,
) -> None:
    """Cartel = snapshot do sobrevivente + desfecho DERIVADO das lutas repontadas.

    Nunca o valor da duplicata copiado à mão, e nunca um recálculo total do granular: o
    snapshot é de carreira inteira (inclui luta fora do UFC) e o granular só tem UFC -- um
    recálculo total trocaria 29-11-1 por um parcial de 12-8-1.
    """
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    alvo.wins, alvo.losses, alvo.draws = 29, 11, 1
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    adversario = _fighter(db_session, 9003, "Bogdan Guskov", "bogdan guskov", _DOB_B)
    outro = _fighter(db_session, 9004, "Navajo Stirling", "navajo stirling", None)
    evento = _event(db_session, "UFC 323")
    # Empate (vencedor nulo com método de decisão) e derrota: o delta real do caso Błachowicz.
    _bout_com_cantos(db_session, evento, dup.id, adversario.id, winner_id=None)
    _bout_com_cantos(
        db_session, evento, dup.id, outro.id, winner_id=outro.id, method=BoutMethod.KO_TKO
    )
    db_session.flush()

    resultado = merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    assert (alvo.wins, alvo.losses, alvo.draws) == (29, 12, 2)
    assert resultado.record_delta == (0, 1, 1)


def test_merge_apaga_o_cache_asof_da_duplicata(db_session: Session) -> None:
    """``fighter_asof_features`` é cache reconstrutível: a linha da duplicata é apagada."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    db_session.add(
        FighterAsOfFeatures(
            fighter_id=dup.id,
            features={},
            style_history={},
            n_bouts_source=2,
            source="derived",
            materialized_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db_session.flush()

    resultado = merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    assert db_session.get(FighterAsOfFeatures, dup.id) is None
    assert resultado.asof_cache_deleted == 1


def test_merge_remove_a_linha_da_duplicata(db_session: Session) -> None:
    """Terminada a mesclagem, a duplicata não existe mais."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)

    merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    assert db_session.get(Fighter, 9002) is None
    assert db_session.get(Fighter, 9001) is not None


def test_merge_e_idempotente_a_segunda_execucao_e_no_op(db_session: Session) -> None:
    """Duplicata ausente não é erro: a segunda execução reporta zero e não muda nada."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    adversario = _fighter(db_session, 9003, "Bogdan Guskov", "bogdan guskov", _DOB_B)
    evento = _event(db_session, "UFC 323")
    _bout_com_cantos(db_session, evento, dup.id, adversario.id, winner_id=None)

    primeiro = merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)
    db_session.flush()
    nome_depois = alvo.name
    cartel_depois = (alvo.wins, alvo.losses, alvo.draws)

    segundo = merge_fighters(db_session, duplicate_id=9002, target_id=alvo.id)

    db_session.flush()
    assert primeiro.bout_fighters_repointed == 1
    assert not segundo.applied
    assert segundo.bout_fighters_repointed == 0
    assert (alvo.name, (alvo.wins, alvo.losses, alvo.draws)) == (nome_depois, cartel_depois)


def test_merge_recusa_quando_uma_luta_contem_os_dois(db_session: Session) -> None:
    """Luta com os dois lados apontando o mesmo lutador seria auto-confronto: recusa antes."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    evento = _event(db_session, "UFC 323")
    luta = _bout_com_cantos(db_session, evento, dup.id, alvo.id, winner_id=None)

    with pytest.raises(FighterMergeError) as excinfo:
        merge_fighters(db_session, duplicate_id=dup.id, target_id=alvo.id)

    db_session.flush()
    cantos = db_session.scalars(select(BoutFighter).where(BoutFighter.bout_id == luta.id)).all()
    assert sorted(canto.fighter_id for canto in cantos) == [alvo.id, dup.id]
    assert db_session.get(Fighter, dup.id) is not None
    assert str(luta.id) in str(excinfo.value)


def test_merge_recusa_quando_as_chaves_normalizadas_divergem(db_session: Session) -> None:
    """Mesclar dois nomes que não colapsam na mesma chave é outra operação: recusa."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    outra_pessoa = _fighter(db_session, 9002, "Michal Oleksiejczuk", "michal oleksiejczuk", None)

    with pytest.raises(FighterMergeError):
        merge_fighters(db_session, duplicate_id=outra_pessoa.id, target_id=alvo.id)

    db_session.flush()
    assert db_session.get(Fighter, 9002) is not None
    assert alvo.name == "Jan Blachowicz"


def test_merge_recusa_mesclar_a_linha_consigo_mesma(db_session: Session) -> None:
    """``--mesclar 596 596`` é erro de operador, não no-op silencioso."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)

    with pytest.raises(FighterMergeError):
        merge_fighters(db_session, duplicate_id=alvo.id, target_id=alvo.id)


def test_merge_recusa_alvo_inexistente(db_session: Session) -> None:
    """Alvo ausente falha alto: nunca mesclar para um id que não existe."""
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)

    with pytest.raises(FighterMergeError):
        merge_fighters(db_session, duplicate_id=dup.id, target_id=424242)

    db_session.flush()
    assert db_session.get(Fighter, 9002) is not None


# --- CLI da mesclagem: mede por default, aplica sob flag ---------------------------------


def test_cli_mesclar_mede_por_default_sem_escrever(
    cli_isolado: None, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """``--mesclar`` sem ``--aplicar`` reporta o que faria e não toca nada."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    adversario = _fighter(db_session, 9003, "Bogdan Guskov", "bogdan guskov", _DOB_B)
    evento = _event(db_session, "UFC 323")
    _bout_com_cantos(db_session, evento, dup.id, adversario.id, winner_id=None)

    with caplog.at_level(logging.INFO, logger="ingestion.renormalize_names"):
        main(["--mesclar", "9002", "9001"])

    db_session.flush()
    assert db_session.get(Fighter, 9002) is not None
    assert alvo.name == "Jan Blachowicz"
    assert "modo medição" in caplog.text


def test_cli_mesclar_com_aplicar_executa_a_mesclagem(
    cli_isolado: None, db_session: Session
) -> None:
    """``--mesclar <dup> <alvo> --aplicar`` repontará, recalculará e removerá a duplicata."""
    alvo = _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    dup = _fighter(db_session, 9002, "Jan Błachowicz", "jan bachowicz", None)
    adversario = _fighter(db_session, 9003, "Bogdan Guskov", "bogdan guskov", _DOB_B)
    evento = _event(db_session, "UFC 323")
    _bout_com_cantos(db_session, evento, dup.id, adversario.id, winner_id=None)

    main(["--mesclar", "9002", "9001", "--aplicar"])

    db_session.flush()
    assert db_session.get(Fighter, 9002) is None
    assert alvo.name == "Jan Błachowicz"


def test_cli_mesclar_aborta_com_codigo_1_quando_a_mesclagem_e_recusada(
    cli_isolado: None, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Recusa da mesclagem vira ``SystemExit(1)``, sem escrita."""
    _fighter(db_session, 9001, "Jan Blachowicz", "jan blachowicz", _DOB_A)
    _fighter(db_session, 9002, "Michal Oleksiejczuk", "michal oleksiejczuk", None)

    with (
        caplog.at_level(logging.ERROR, logger="ingestion.renormalize_names"),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["--mesclar", "9002", "9001", "--aplicar"])

    assert excinfo.value.code == 1
    db_session.flush()
    assert db_session.get(Fighter, 9002) is not None
