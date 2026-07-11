"""Testes da entity resolution de fighters -- CA-03.

Cobrem a tipagem da borda (linha crua do CSV -> ``ResolvedFighter``), o parsing
dos campos (DOB, medidas em cm, stance mapeada ao enum, nickname) e a dedup por
``(name_normalized, date_of_birth)``: variações do mesmo lutador colapsam num
único registro, homônimos com DOB distinta permanecem separados e DOB ausente
colapsa na chave normalizada.
"""

from __future__ import annotations

from datetime import date

import pytest

from apps.fighters.enums import Stance
from ingestion.entity_resolution import (
    AmbiguousFighterMatchError,
    ExistingFighter,
    FighterCandidate,
    FighterRow,
    match_fighter_id,
    resolve_fighters,
)


def _row(name: str, **overrides: str) -> FighterRow:
    """Constrói uma linha crua com valores plausíveis, sobrescrevendo o necessário."""
    base: FighterRow = {
        "name": name,
        "nick_name": "",
        "dob": "May 08, 1982",
        "height": "180.0",
        "reach": "185.0",
        "stance": "Orthodox",
        "wins": "10",
        "losses": "2",
        "draws": "0",
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


def test_variacoes_do_mesmo_lutador_colapsam_para_um_registro() -> None:
    """Caixa/acento/espaço/sufixo com a mesma DOB resolvem para um único lutador."""
    rows = [
        _row("Alexander Volkanovski", dob="May 08, 1982"),
        _row("alexander  vólkanovski Jr", dob="May 08, 1982"),
    ]
    resolved = resolve_fighters(rows)
    assert len(resolved) == 1
    assert resolved[0].name_normalized == "alexander volkanovski"


def test_homonimos_com_dob_distinta_permanecem_separados() -> None:
    """Mesmo nome normalizado com DOB diferente = dois lutadores (desempate por DOB)."""
    rows = [
        _row("Bruno Silva", dob="Jul 13, 1989"),
        _row("Bruno Silva", dob="Mar 16, 1990"),
    ]
    resolved = resolve_fighters(rows)
    assert len(resolved) == 2
    assert {r.date_of_birth for r in resolved} == {date(1989, 7, 13), date(1990, 3, 16)}


def test_dob_ausente_colapsa_na_chave_normalizada() -> None:
    """Sem DOB o desempate degrada para o nome normalizado apenas."""
    rows = [_row("Ghost Prospect", dob=""), _row("ghost  prospect", dob="")]
    resolved = resolve_fighters(rows)
    assert len(resolved) == 1
    assert resolved[0].date_of_birth is None


def test_primeira_ocorrencia_vence_na_colisao() -> None:
    """Na dedup, os atributos do primeiro registro da chave são preservados."""
    rows = [
        _row("Jon Jones", nick_name="Bones", dob="Jul 19, 1987"),
        _row("jon jones", nick_name="Outro", dob="Jul 19, 1987"),
    ]
    resolved = resolve_fighters(rows)
    assert len(resolved) == 1
    assert resolved[0].nickname == "Bones"


def test_parsing_de_campos_para_o_dominio() -> None:
    """DOB, medidas (cm inteiro), stance (enum), nickname e cartel são tipados na borda."""
    (fighter,) = resolve_fighters(
        [_row("Jose Aldo", nick_name="Junior", height="167.64", reach="177.8")]
    )
    assert fighter.date_of_birth == date(1982, 5, 8)
    assert fighter.height_cm == 168
    assert fighter.reach_cm == 178
    assert fighter.stance is Stance.ORTHODOX
    assert fighter.nickname == "Junior"
    assert (fighter.wins, fighter.losses, fighter.draws) == (10, 2, 0)


def test_stance_fora_do_enum_vira_none() -> None:
    """Stances fora de orthodox/southpaw/switch (Open Stance, Sideways, vazio) -> NULL."""
    rows = [
        _row("Fighter A", stance="Open Stance"),
        _row("Fighter B", stance="Sideways"),
        _row("Fighter C", stance=""),
    ]
    resolved = resolve_fighters(rows)
    assert all(f.stance is None for f in resolved)


def test_medidas_e_nickname_ausentes_viram_none() -> None:
    """Height/reach/nickname vazios no CSV resolvem para NULL no domínio."""
    (fighter,) = resolve_fighters([_row("No Measures", height="", reach="", nick_name="")])
    assert fighter.height_cm is None
    assert fighter.reach_cm is None
    assert fighter.nickname is None


# --- Matching cross-source (Kaggle x Cito) -- CA-01, CA-04, CA-05, CA-06 --------------


def test_cross_source_nome_e_dob_iguais_reusa_id_existente() -> None:
    """CA-01: nome normalizado + DOB casam com um existente -> reusa o id (mesma pessoa)."""
    existing = [
        ExistingFighter(
            id=7, name_normalized="alexander volkanovski", date_of_birth=date(1988, 9, 29)
        ),
    ]
    candidate = FighterCandidate(name="Alexander Volkanovski", date_of_birth=date(1988, 9, 29))
    assert match_fighter_id(candidate, existing) == 7


def test_cross_source_nome_sem_correspondencia_retorna_none() -> None:
    """CA-01: nome que não casa com nenhum existente -> ``None`` (lutador novo)."""
    existing = [ExistingFighter(id=1, name_normalized="jon jones", date_of_birth=date(1987, 7, 19))]
    candidate = FighterCandidate(name="Ilia Topuria", date_of_birth=date(1997, 1, 21))
    assert match_fighter_id(candidate, existing) is None


def test_cross_source_homonimo_com_dob_divergente_retorna_none() -> None:
    """CA-04: mesmo nome mas DOB conhecida diverge de todos os existentes -> novo (não funde)."""
    existing = [
        ExistingFighter(id=1, name_normalized="bruno silva", date_of_birth=date(1989, 7, 13))
    ]
    candidate = FighterCandidate(name="Bruno Silva", date_of_birth=date(1990, 3, 16))
    assert match_fighter_id(candidate, existing) is None


def test_cross_source_dob_ausente_com_um_unico_existente_reusa_id() -> None:
    """CA-05: candidato sem DOB casa por nome só quando há exatamente um existente daquele nome."""
    existing = [ExistingFighter(id=5, name_normalized="jon jones", date_of_birth=date(1987, 7, 19))]
    candidate = FighterCandidate(name="Jon Jones", date_of_birth=None)
    assert match_fighter_id(candidate, existing) == 5


def test_cross_source_dob_ausente_com_multiplos_existentes_e_ambiguo() -> None:
    """CA-06: candidato sem DOB e >1 existente daquele nome -> erro (nunca funde em silêncio)."""
    existing = [
        ExistingFighter(id=1, name_normalized="bruno silva", date_of_birth=date(1989, 7, 13)),
        ExistingFighter(id=2, name_normalized="bruno silva", date_of_birth=date(1990, 3, 16)),
    ]
    candidate = FighterCandidate(name="Bruno Silva", date_of_birth=None)
    with pytest.raises(AmbiguousFighterMatchError):
        match_fighter_id(candidate, existing)


def test_cross_source_dob_conhecida_com_existente_sem_dob_e_ambiguo() -> None:
    """CA-06: DOB conhecida sem match exato, mas há existente sem DOB indescartável -> erro."""
    existing = [ExistingFighter(id=3, name_normalized="conor mcgregor", date_of_birth=None)]
    candidate = FighterCandidate(name="Conor McGregor", date_of_birth=date(1988, 7, 14))
    with pytest.raises(AmbiguousFighterMatchError):
        match_fighter_id(candidate, existing)
