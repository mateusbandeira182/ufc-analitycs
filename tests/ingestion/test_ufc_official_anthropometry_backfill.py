"""Testes do backfill de antropometria da fonte oficial -- CA-04 a CA-08 (Sprint 008-05).

Rodam contra o Postgres de teste ``ufc_bum_test`` na sessão transacional (rollback ao final) e
**offline**: o diretório de capturas verbatim é lido como cache, então nenhuma requisição sai.
Zero chamadas à Cito -- a fonte desta slice é gratuita.

O cenário sai das capturas verbatim de UFC 282: Blachowicz vs. Ankalaev
(``event_live_1124.json``, com Darren Till e Dricus Du Plessis) e de UFC 140: Jones vs Machida
(``event_live_566.json``, com o rótulo de base ``Open Stance`` que o nosso enum não representa).

Valores de origem, medidos na captura:

===================  =========  ========  =========  ============  ============
Lutador              ``Height`` ``Reach`` ``Weight`` ``DOB``       ``Stance``
===================  =========  ========  =========  ============  ============
Darren Till          72.0 in    74.5 in   185.0 lb   1992-12-24    Southpaw
Dricus Du Plessis    73.0 in    76.0 in   185.0 lb   1994-01-14    Switch
Krzysztof Soszynski  73.0 in    77.5 in   205.0 lb   1977-08-02    Open Stance
Igor Pokrajac        72.0 in    74.0 in   205.0 lb   1979-01-02    Orthodox
===================  =========  ========  =========  ============  ============
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official.anthropometry import (
    AnthropometryBackfillResult,
    OfficialAnthropometry,
    _parse_args,
    backfill_anthropometry,
    collect_anthropometry,
)
from ingestion.ufc_official.cache import OfficialEventCache

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

_UFC_282 = ("UFC 282: Blachowicz vs. Ankalaev", date(2022, 12, 10), "1124")
_UFC_140 = ("UFC 140: Jones vs Machida", date(2011, 12, 10), "566")
# Um evento anterior a 2010-03-21: nada dele pode ser tocado (RF-03, CA-08).
_PRE_JANELA = ("UFC 100", date(2009, 7, 11), "263")

_TILL = OfficialAnthropometry(
    ufc_fighter_id="2544",
    name="Darren Till",
    height_cm=183,  # 72,0 in x 2,54 = 182,88
    reach_cm=189,  # 74,5 in x 2,54 = 189,23
    weight_kg=83.91,  # 185 lb x 0,45359237 = 83,914...
    date_of_birth=date(1992, 12, 24),
    stance=Stance.SOUTHPAW,
)
_DU_PLESSIS = OfficialAnthropometry(
    ufc_fighter_id="3599",
    name="Dricus Du Plessis",
    height_cm=185,  # 73,0 in x 2,54 = 185,42
    reach_cm=193,  # 76,0 in x 2,54 = 193,04
    weight_kg=83.91,
    date_of_birth=date(1994, 1, 14),
    stance=Stance.SWITCH,
)


def _semeia_lutador(
    session: Session,
    nome: str,
    *,
    ufc_fighter_id: str | None,
    dob: date | None = None,
    height_cm: int | None = None,
    reach_cm: int | None = None,
    weight_kg: float | None = None,
    stance: Stance | None = None,
) -> Fighter:
    """Um lutador persistido; por padrão com os cinco atributos **nulos**."""
    lutador = Fighter(
        name=nome,
        name_normalized=normalize_name(nome),
        nickname=None,
        date_of_birth=dob,
        height_cm=height_cm,
        reach_cm=reach_cm,
        stance=stance,
        weight_kg=weight_kg,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
        ufc_fighter_id=ufc_fighter_id,
    )
    session.add(lutador)
    session.flush()
    return lutador


def _semeia_luta(session: Session, evento: tuple[str, date, str], *lutadores: Fighter) -> None:
    """Um evento com uma luta entre os lutadores dados (o canto aqui é só para semear)."""
    nome, quando, ufc_event_id = evento
    persistido = session.scalars(select(Event).where(Event.name == nome)).one_or_none()
    if persistido is None:
        persistido = Event(
            name=nome, date=quando, location=None, source="kaggle", ufc_event_id=ufc_event_id
        )
        session.add(persistido)
        session.flush()
    luta = Bout(
        event_id=persistido.id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    session.add(luta)
    session.flush()
    for canto, lutador in zip((Corner.RED, Corner.BLUE), lutadores, strict=True):
        session.add(
            BoutFighter(bout_id=luta.id, fighter_id=lutador.id, corner=canto, source="kaggle")
        )
    session.flush()


def _cenario_ufc_282(session: Session) -> tuple[Fighter, Fighter]:
    """Till x Du Plessis persistidos com os cinco atributos nulos, no evento da janela."""
    till = _semeia_lutador(session, "Darren Till", ufc_fighter_id="2544")
    du_plessis = _semeia_lutador(session, "Dricus Du Plessis", ufc_fighter_id="3599")
    _semeia_luta(session, _UFC_282, till, du_plessis)
    return till, du_plessis


def _coleta(session: Session) -> tuple[OfficialAnthropometry, ...]:
    """Execução offline da coleta: lê o diretório de fixtures como cache, sem tocar a rede."""
    return collect_anthropometry(session, None, OfficialEventCache(_FIXTURES)).profiles


# --------------------------------------------------------------------------- #
# CA-04 -- preenche só onde o nosso está nulo
# --------------------------------------------------------------------------- #


def test_backfill_preenche_os_cinco_campos_onde_o_nosso_esta_nulo(db_session: Session) -> None:
    """CA-04: os cinco atributos nulos recebem o valor da fonte, já convertido."""
    till, du_plessis = _cenario_ufc_282(db_session)

    resultado = backfill_anthropometry(db_session, [_TILL, _DU_PLESSIS])

    assert (till.height_cm, till.reach_cm, till.weight_kg) == (183, 189, 83.91)
    assert (till.date_of_birth, till.stance) == (date(1992, 12, 24), Stance.SOUTHPAW)
    assert (du_plessis.height_cm, du_plessis.reach_cm) == (185, 193)
    assert du_plessis.stance is Stance.SWITCH
    assert resultado.fighters_touched == 2
    assert resultado.updated_fields == 10  # cinco campos x dois lutadores


def test_backfill_nao_sobrescreve_valor_ja_preenchido(db_session: Session) -> None:
    """CA-04/RF-04: valor já gravado permanece, mesmo quando a fonte discorda."""
    till, _ = _cenario_ufc_282(db_session)
    till.height_cm = 175
    till.stance = Stance.ORTHODOX
    db_session.flush()

    backfill_anthropometry(db_session, [_TILL])

    assert till.height_cm == 175
    assert till.stance is Stance.ORTHODOX
    # O que estava nulo ao lado continua sendo preenchido: não sobrescrever não é não escrever.
    assert till.reach_cm == 189


def test_divergencia_fora_da_tolerancia_e_contada_e_nao_escrita(db_session: Session) -> None:
    """CA-02/CA-04: 175 cm contra 183 cm é divergência -- reportada, nomeada e não escrita."""
    till, _ = _cenario_ufc_282(db_session)
    till.height_cm = 175
    db_session.flush()

    resultado = backfill_anthropometry(db_session, [_TILL])

    assert till.height_cm == 175
    assert resultado.divergences == 1
    (divergencia,) = resultado.divergent
    assert divergencia.name == "Darren Till"
    assert divergencia.field == "height_cm"
    assert divergencia.existing == "175"
    assert divergencia.from_source == "183"


def test_diferenca_dentro_da_tolerancia_nao_conta_como_divergencia(db_session: Session) -> None:
    """CA-02: 2 cm de diferença é arredondamento de polegada, não discordância."""
    till, _ = _cenario_ufc_282(db_session)
    till.height_cm = 181
    db_session.flush()

    resultado = backfill_anthropometry(db_session, [_TILL])

    assert till.height_cm == 181
    assert resultado.divergent == ()


def test_campo_ausente_na_fonte_deixa_o_nosso_nulo(db_session: Session) -> None:
    """CA-05: alcance ausente na fonte permanece nulo -- nunca zero, nunca sentinela."""
    till, _ = _cenario_ufc_282(db_session)

    resultado = backfill_anthropometry(
        db_session,
        [
            OfficialAnthropometry(
                ufc_fighter_id="2544",
                name="Darren Till",
                height_cm=183,
                reach_cm=None,
                weight_kg=83.91,
                date_of_birth=date(1992, 12, 24),
                stance=Stance.SOUTHPAW,
            )
        ],
    )

    assert till.reach_cm is None
    assert resultado.absent_in_source == 1
    assert resultado.updated_fields == 4


def test_backfill_nunca_insere_linha_nova(db_session: Session) -> None:
    """CA-04: perfil da fonte sem lutador persistido é contado, nunca criado."""
    _cenario_ufc_282(db_session)
    antes = db_session.scalar(select(func.count()).select_from(Fighter))

    resultado = backfill_anthropometry(
        db_session,
        [
            OfficialAnthropometry(
                ufc_fighter_id="999999",
                name="Ninguem Persistido",
                height_cm=180,
                reach_cm=180,
                weight_kg=70.0,
                date_of_birth=date(1990, 1, 1),
                stance=Stance.ORTHODOX,
            )
        ],
    )

    assert db_session.scalar(select(func.count()).select_from(Fighter)) == antes
    assert resultado.profiles_without_fighter == 1
    assert resultado.updated_fields == 0


def test_lutador_sem_ufc_fighter_id_e_pulado_e_contado(db_session: Session) -> None:
    """CA-04/RF-07: sem id não há casamento -- nunca cair para o nome, que é ambíguo."""
    sem_id = _semeia_lutador(db_session, "Darren Till", ufc_fighter_id=None)
    outro = _semeia_lutador(db_session, "Dricus Du Plessis", ufc_fighter_id="3599")
    _semeia_luta(db_session, _UFC_282, sem_id, outro)

    resultado = backfill_anthropometry(db_session, [_TILL, _DU_PLESSIS])

    assert sem_id.height_cm is None
    assert resultado.skipped_without_id == 1
    assert resultado.profiles_without_fighter == 1


def test_backfill_preserva_o_source_da_linha(db_session: Session) -> None:
    """``source`` é a origem da LINHA, não do campo: a linha do Kaggle continua ``kaggle``."""
    till, _ = _cenario_ufc_282(db_session)

    backfill_anthropometry(db_session, [_TILL])

    assert till.source == "kaggle"


# --------------------------------------------------------------------------- #
# CA-06 -- idempotência
# --------------------------------------------------------------------------- #


def test_segunda_execucao_nao_escreve_nada(db_session: Session) -> None:
    """CA-06: reexecutar devolve ``updated_fields=0`` e não muda a contagem de preenchidos."""
    _cenario_ufc_282(db_session)

    primeira = backfill_anthropometry(db_session, [_TILL, _DU_PLESSIS])
    preenchidos = _contagem_com_altura(db_session)
    segunda = backfill_anthropometry(db_session, [_TILL, _DU_PLESSIS])

    assert primeira.updated_fields == 10
    assert segunda.updated_fields == 0
    assert segunda.fighters_touched == 0
    assert segunda.divergent == ()
    assert _contagem_com_altura(db_session) == preenchidos


def _contagem_com_altura(session: Session) -> int | None:
    """Quantos lutadores têm ``height_cm`` não-nulo neste instante."""
    return session.scalar(
        select(func.count()).select_from(Fighter).where(Fighter.height_cm.is_not(None))
    )


# --------------------------------------------------------------------------- #
# CA-07 -- a janela de 2010-03-21
# --------------------------------------------------------------------------- #


def test_lutador_so_ativo_antes_da_janela_nao_recebe_escrita(db_session: Session) -> None:
    """CA-07/RF-03: quem só lutou antes de 2010-03-21 é contado e não recebe nada.

    O lutador tem ``ufc_fighter_id`` e há um perfil da fonte com esse id -- o único motivo de
    ele não ser escrito é a janela.
    """
    antigo = _semeia_lutador(db_session, "Darren Till", ufc_fighter_id="2544")
    adversario = _semeia_lutador(db_session, "Dricus Du Plessis", ufc_fighter_id="3599")
    _semeia_luta(db_session, _PRE_JANELA, antigo, adversario)

    resultado = backfill_anthropometry(db_session, [_TILL, _DU_PLESSIS])

    assert (antigo.height_cm, antigo.date_of_birth, antigo.stance) == (None, None, None)
    assert resultado.updated_fields == 0
    assert resultado.out_of_window == 2


# --------------------------------------------------------------------------- #
# CA-03 -- coleta persisted-driven e o rótulo de base desconhecido
# --------------------------------------------------------------------------- #


def test_coleta_projeta_os_cantos_dos_eventos_da_janela(db_session: Session) -> None:
    """A coleta lê o card do evento mapeado e devolve a antropometria já no domínio."""
    _cenario_ufc_282(db_session)

    perfis = {perfil.ufc_fighter_id: perfil for perfil in _coleta(db_session)}

    assert perfis["2544"] == _TILL
    assert perfis["3599"] == _DU_PLESSIS


def test_rotulo_de_base_desconhecido_e_contado_e_nao_derruba_a_coleta(
    db_session: Session,
) -> None:
    """CA-03/RF-10: ``Open Stance`` é contado e nomeado; o adversário continua sendo coletado.

    O rótulo é real -- Krzysztof Soszynski em UFC 140, na captura verbatim. Falhar alto é o
    comportamento certo do parser; derrubar a execução inteira por causa de um lutador não é.
    """
    soszynski = _semeia_lutador(db_session, "Krzysztof Soszynski", ufc_fighter_id="309")
    pokrajac = _semeia_lutador(db_session, "Igor Pokrajac", ufc_fighter_id="966")
    _semeia_luta(db_session, _UFC_140, soszynski, pokrajac)

    coleta = collect_anthropometry(db_session, None, OfficialEventCache(_FIXTURES))

    assert sorted(perfil.ufc_fighter_id for perfil in coleta.profiles) == ["309", "966"]
    assert coleta.unknown_stance == (("309", "Krzysztof Soszynski", "Open Stance"),)


def test_rotulo_de_base_desconhecido_custa_a_base_e_nada_mais(db_session: Session) -> None:
    """CA-03/CA-05: o rótulo não mapeado zera só a base -- os outros quatro campos continuam.

    Soszynski tem ``Height`` 73.0 in, ``Reach`` 77.5 in, ``Weight`` 205.0 lb e ``DOB``
    1977-08-02 na captura verbatim. Descartar o lutador inteiro por causa da base custaria
    quatro atributos bons por causa de um rótulo ruim, e a slice existe justamente para fechar
    buracos. A base fica nula, contada e nomeada -- nunca adivinhada.
    """
    soszynski = _semeia_lutador(db_session, "Krzysztof Soszynski", ufc_fighter_id="309")
    pokrajac = _semeia_lutador(db_session, "Igor Pokrajac", ufc_fighter_id="966")
    _semeia_luta(db_session, _UFC_140, soszynski, pokrajac)

    backfill_anthropometry(db_session, _coleta(db_session))

    assert soszynski.height_cm == 185  # 73,0 in x 2,54 = 185,42
    assert soszynski.reach_cm == 197  # 77,5 in x 2,54 = 196,85
    assert soszynski.weight_kg == 92.99  # 205 lb x 0,45359237 = 92,986...
    assert soszynski.date_of_birth == date(1977, 8, 2)
    assert soszynski.stance is None


# --------------------------------------------------------------------------- #
# CA-08 -- a exposição pela API, sem tocar em ``apps/``
# --------------------------------------------------------------------------- #


def test_endpoint_de_lutador_devolve_a_antropometria_apos_o_backfill(
    client: TestClient, db_session: Session
) -> None:
    """CA-08: o campo que vinha nulo em ``GET /api/v1/fighters/{id}`` agora vem preenchido."""
    till, _ = _cenario_ufc_282(db_session)
    assert client.get(f"/api/v1/fighters/{till.id}").json()["reach_cm"] is None

    backfill_anthropometry(db_session, _coleta(db_session))

    corpo = client.get(f"/api/v1/fighters/{till.id}").json()
    assert corpo["height_cm"] == 183
    assert corpo["reach_cm"] == 189
    assert corpo["weight_kg"] == 83.91
    assert corpo["date_of_birth"] == "1992-12-24"
    assert corpo["stance"] == "southpaw"


# --------------------------------------------------------------------------- #
# Camada de argumentos do comando
# --------------------------------------------------------------------------- #


def test_parse_args_le_o_diretorio_de_cache() -> None:
    """``--cache-dir`` é parseado como ``Path``."""
    assert _parse_args(["--cache-dir", "/data/ufc"]).cache_dir == Path("/data/ufc")


def test_parse_args_sem_fixture_dir_opera_online() -> None:
    """Sem ``--fixture-dir`` o valor é ``None`` (a execução consulta a fonte)."""
    assert _parse_args([]).fixture_dir is None


def test_resultado_do_backfill_e_imutavel() -> None:
    """O resumo é um valor, não um acumulador mutável passado adiante."""
    resultado = AnthropometryBackfillResult(
        updated_fields=0,
        fighters_touched=0,
        divergent=(),
        absent_in_source=0,
        skipped_without_id=0,
        out_of_window=0,
        profiles_without_fighter=0,
    )
    assert resultado.divergences == 0
