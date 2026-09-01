"""Round-trip do ``EventStatsCache``: o cache hit devolve o DTO **inteiro** -- CA-06.

O cache em disco existe para tornar o backfill resumível (uma interrupção não re-gasta quota).
Isso só vale se o que volta do disco for igual ao que veio da rede: um bloco que a serialização
esquece some na retomada, e o backfill retomado gravaria stats sem o card.

Desde a ADR 0005 o DTO tem quatro blocos (``event``, ``bouts``, ``bout_stats``, ``round_stats``),
e os dois primeiros são obrigatórios -- um cache gravado sem eles nem revalida. Este arquivo
guarda a fidelidade dos blocos de card; a fidelidade dos splits round-a-round e a mecânica de
hit/miss ficam em ``test_cito_backfill_rounds.py``, junto do consumidor.

Nenhum teste toca a rede: o ``fetch`` lê a fixture local.
"""

from __future__ import annotations

from pathlib import Path

from ingestion.cito.cache import EventStatsCache
from ingestion.cito.client import CitoClient
from ingestion.cito.dto import CitoEventStats

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_event_stats(slug: str) -> CitoEventStats:
    """Carrega a fixture de stats de um evento como DTO tipado, sem tocar rede/quota."""
    client = CitoClient(token="", base_url="https://api.citoapi.com", fixture_dir=_FIXTURES)
    return client.fetch_event_stats(slug)


def test_cache_hit_devolve_dto_identico_ao_do_miss(tmp_path: Path) -> None:
    """CA-06: o DTO do hit é igual ao do miss -- inclusive ``event`` e ``bouts``."""
    cache = EventStatsCache(tmp_path)

    stats_miss, hit_miss = cache.get_or_fetch("ufc-319", _fixture_event_stats)
    stats_hit, hit_hit = cache.get_or_fetch("ufc-319", _fixture_event_stats)

    assert (hit_miss, hit_hit) == (False, True)
    assert stats_hit == stats_miss


def test_cache_hit_preserva_metadados_do_evento(tmp_path: Path) -> None:
    """CA-06: a data local, o slug e o status do evento sobrevivem ao round-trip do disco."""
    cache = EventStatsCache(tmp_path)
    cache.get_or_fetch("ufc-319", _fixture_event_stats)

    stats, hit = cache.get_or_fetch("ufc-319", _fixture_event_stats)

    assert hit is True
    assert stats.event == _fixture_event_stats("ufc-319").event


def test_cache_hit_preserva_o_card_com_cantos_e_resultado(tmp_path: Path) -> None:
    """CA-06: o card volta íntegro -- peso, método, vencedor, tempo e o ``corner`` de cada canto.

    O ``corner`` é o caso crítico: ele não existe mais nas linhas de stat, então perdê-lo aqui
    tiraria do backfill retomado a única fonte do canto.
    """
    cache = EventStatsCache(tmp_path)
    cache.get_or_fetch("ufc-319", _fixture_event_stats)

    stats, hit = cache.get_or_fetch("ufc-319", _fixture_event_stats)

    assert hit is True
    assert stats.bouts == _fixture_event_stats("ufc-319").bouts


def test_cache_hit_preserva_total_strikes(tmp_path: Path) -> None:
    """CA-02/CA-06: ``total_strikes`` volta como tupla, não como ``(None, None)``.

    O split é reconstruído na forma wire (``"L of A"``) na gravação; um campo esquecido ali
    degradaria em silêncio na revalidação, que é a falha que a ADR 0005 documenta.
    """
    cache = EventStatsCache(tmp_path)
    cache.get_or_fetch("ufc-319", _fixture_event_stats)

    stats, _hit = cache.get_or_fetch("ufc-319", _fixture_event_stats)

    red = next(line for line in stats.bout_stats if line.fighter_slug == "khamzat-chimaev")
    assert red.sig_strikes == (37, 47)
    assert red.total_strikes == (529, 567)


def test_cache_hit_preserva_o_profile_embutido_no_canto(tmp_path: Path) -> None:
    """O ``profile`` de cada canto sobrevive ao round-trip -- é dele que nasce o lutador novo.

    O fechamento do gap (Slice 06) cria os lutadores inéditos a partir do ``profile`` embutido,
    sem gastar chamada de perfil (RF-13). Se a serialização o esquecesse, retomar o gap depois
    de uma interrupção de quota gravaria os lutadores sem apelido e com cartel zerado -- dado
    falso, com aparência de dado.
    """
    cache = EventStatsCache(tmp_path)
    slug = "ufc-fight-night-august-22-2026"

    stats_miss, _ = cache.get_or_fetch(slug, _fixture_event_stats)
    stats_hit, hit = cache.get_or_fetch(slug, _fixture_event_stats)

    assert hit is True
    assert stats_hit == stats_miss
    (canto,) = [f for f in stats_hit.bouts[0].fighters if f.fighter_slug == "anthony-hernandez"]
    assert canto.profile is not None
    assert canto.profile.nickname == "Fluffy"
    assert (canto.profile.record.wins, canto.profile.record.losses) == (15, 4)


def test_cache_hit_preserva_a_arte_do_card(tmp_path: Path) -> None:
    """A URL da arte sobrevive ao round-trip -- é dela que sai o canto REAL da luta.

    Desde a correção de 2026-09-01 o canto persistido vem do sufixo ``_L_``/``_R_`` do nome do
    arquivo da arte (``ingestion.cito.gap_sync.art_side``). Se a serialização esquecesse a URL,
    um gap retomado do cache degradaria silenciosamente para o canto atribuído -- e a diferença
    não apareceria em contagem nenhuma, só no viés do modelo meses depois.
    """
    slug = "ufc-fight-night-august-22-2026"
    cache = EventStatsCache(tmp_path)
    cache.get_or_fetch(slug, _fixture_event_stats)

    stats, hit = cache.get_or_fetch(slug, _fixture_event_stats)

    assert hit is True
    artes = [canto.image_url for bout in stats.bouts for canto in bout.fighters]
    assert artes == [
        canto.image_url for bout in _fixture_event_stats(slug).bouts for canto in bout.fighters
    ]
    assert all(arte is not None for arte in artes)
