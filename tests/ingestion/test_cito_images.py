"""Extração das duas URLs de imagem do lutador a partir do card da Cito (M7, Slice 06).

Cobre o CA-04 do plano 008-06: a função é **pura** (sem banco, sem rede) e lê exclusivamente
``bouts[].fighters[].profile.headshot_url``/``.body_image_url``. Os dois testes-guarda do fim
do arquivo são o coração desta slice: a arte do card
(``event_fight_card_upper_body_of_standing_athlete``) **não** é atributo de lutador, e o
``athlete_bio_full_body`` carrega sufixo ``_L_`` nos **dois** cantos da mesma luta -- apontar a
recuperação de canto do M6 para ele corromperia o alvo do modelo em silêncio.

Nenhum teste toca a rede: os itens são montados em memória e a fixture de contrato é lida do
disco.
"""

from __future__ import annotations

import json
from pathlib import Path

from ingestion.cito.dto import CitoCatalogItem, CitoEventStats, CitoStatsEnvelope
from ingestion.cito.gap_sync import art_side
from ingestion.cito.images import FighterImages, extract_fighter_images

_FIXTURES = Path(__file__).parent / "fixtures"
_REAL_SLUG = "ufc-fight-night-august-22-2026"

# Prefixos de estilo reais da Cito, medidos na captura de 2026-08-31.
_ESTILO_HEADSHOT = "https://ufc.com/images/styles/event_results_athlete_headshot/s3"
_ESTILO_BODY = "https://ufc.com/images/styles/athlete_bio_full_body/s3"
_ESTILO_ARTE_DE_CARD = (
    "https://ufc.com/images/styles/event_fight_card_upper_body_of_standing_athlete/s3"
)


def _stats_reais() -> CitoEventStats:
    """Payload integral da sondagem de 2026-08-31, como a Cito devolveu (sem poda)."""
    raw = (_FIXTURES / f"event_stats_{_REAL_SLUG}.json").read_text(encoding="utf-8")
    return CitoStatsEnvelope.model_validate(json.loads(raw)).data


def _canto(
    slug: str,
    *,
    headshot: str | None = None,
    body: str | None = None,
    arte: str | None = None,
    com_profile: bool = True,
) -> dict[str, object]:
    """Um canto do card na forma wire da Cito; ``com_profile=False`` omite o ``profile``."""
    canto: dict[str, object] = {
        "fighterSlug": slug,
        "fighterName": slug.replace("-", " ").title(),
        "corner": "red",
        "imageUrl": arte,
    }
    if com_profile:
        perfil: dict[str, object] = {"slug": slug, "name": slug.replace("-", " ").title()}
        if headshot is not None:
            perfil["headshotUrl"] = headshot
        if body is not None:
            perfil["bodyImageUrl"] = body
        canto["profile"] = perfil
    return canto


def _item(*cantos: dict[str, object]) -> CitoCatalogItem:
    """Item de catálogo (``includeBouts``) com uma luta contendo os cantos dados."""
    return CitoCatalogItem.model_validate(
        {
            "id": "item-1",
            "slug": "ufc-fight-night-august-29-2026",
            "title": "UFC Fight Night: Teste",
            "status": "completed",
            "startsAt": "2026-08-29T10:00:00.000Z",
            "eventDate": "2026-08-29",
            "bouts": [{"id": "bout-1", "fighters": list(cantos)}],
        }
    )


# --------------------------------------------------------------------------- #
# Extração: as duas variantes, e a ausência que permanece nula
# --------------------------------------------------------------------------- #


def test_extrai_as_duas_variantes_de_imagem_do_canto() -> None:
    """CA-04: cada variante cai na sua chave -- retrato em ``headshot``, corpo em ``body``."""
    item = _item(
        _canto(
            "umar-nurmagomedov",
            headshot=f"{_ESTILO_HEADSHOT}/2026-01/NURMAGOMEDOV_UMAR_01-24.png?itok=a",
            body=f"{_ESTILO_BODY}/2026-01/NURMAGOMEDOV_UMAR_L_01-24.png?itok=b",
        )
    )

    imagens = extract_fighter_images(item)

    assert set(imagens) == {"umar-nurmagomedov"}
    retrato = imagens["umar-nurmagomedov"]
    assert retrato.headshot_url == f"{_ESTILO_HEADSHOT}/2026-01/NURMAGOMEDOV_UMAR_01-24.png?itok=a"
    assert retrato.body_image_url == f"{_ESTILO_BODY}/2026-01/NURMAGOMEDOV_UMAR_L_01-24.png?itok=b"


def test_extrai_os_dois_cantos_de_uma_luta() -> None:
    """CA-04: a saída é chaveada por ``fighter_slug`` e cobre os dois cantos."""
    item = _item(
        _canto("umar-nurmagomedov", headshot=f"{_ESTILO_HEADSHOT}/a.png"),
        _canto("arman-tsarukyan", headshot=f"{_ESTILO_HEADSHOT}/b.png"),
    )

    imagens = extract_fighter_images(item)

    assert set(imagens) == {"umar-nurmagomedov", "arman-tsarukyan"}


def test_canto_sem_profile_nao_entra_na_saida() -> None:
    """CA-04: canto sem ``profile`` não produz entrada -- ausência, nunca chave com nulos.

    Uma entrada vazia induziria o backfill a "atualizar" o lutador com dois ``None``, que é
    exatamente o que a regra de nunca apagar URL persistida existe para impedir.
    """
    item = _item(_canto("sem-perfil", com_profile=False))

    assert extract_fighter_images(item) == {}


def test_profile_sem_nenhuma_imagem_nao_entra_na_saida() -> None:
    """CA-04: ``profile`` presente mas sem imagem nenhuma também não gera entrada."""
    item = _item(_canto("sem-imagem"))

    assert extract_fighter_images(item) == {}


def test_canto_com_so_uma_variante_mantem_a_outra_nula() -> None:
    """CA-04: só o retrato presente -> ``body_image_url`` permanece ``None``, nunca ``""``."""
    item = _item(_canto("so-headshot", headshot=f"{_ESTILO_HEADSHOT}/c.png"))

    imagens = extract_fighter_images(item)

    assert imagens["so-headshot"] == FighterImages(
        headshot_url=f"{_ESTILO_HEADSHOT}/c.png", body_image_url=None
    )


def test_canto_com_so_o_corpo_inteiro_mantem_o_retrato_nulo() -> None:
    """CA-04: o simétrico do anterior -- só o corpo presente, retrato ``None``."""
    item = _item(_canto("so-body", body=f"{_ESTILO_BODY}/d.png"))

    imagens = extract_fighter_images(item)

    assert imagens["so-body"] == FighterImages(
        headshot_url=None, body_image_url=f"{_ESTILO_BODY}/d.png"
    )


def test_slug_repetido_no_item_mantem_a_primeira_ocorrencia_nao_vazia() -> None:
    """CA-04: o mesmo lutador em duas lutas do item -- a primeira ocorrência vence, nada é composto.

    Não há mescla de variantes entre ocorrências: compor um headshot de uma luta com um corpo de
    outra criaria um registro que nunca existiu em nenhum payload.
    """
    item = CitoCatalogItem.model_validate(
        {
            "id": "item-1",
            "slug": "ufc-fight-night-august-29-2026",
            "title": "UFC Fight Night: Teste",
            "status": "completed",
            "startsAt": "2026-08-29T10:00:00.000Z",
            "bouts": [
                {
                    "id": "bout-1",
                    "fighters": [_canto("repetido", headshot=f"{_ESTILO_HEADSHOT}/primeira.png")],
                },
                {
                    "id": "bout-2",
                    "fighters": [
                        _canto(
                            "repetido",
                            headshot=f"{_ESTILO_HEADSHOT}/segunda.png",
                            body=f"{_ESTILO_BODY}/segunda.png",
                        )
                    ],
                },
            ],
        }
    )

    imagens = extract_fighter_images(item)

    assert imagens["repetido"] == FighterImages(
        headshot_url=f"{_ESTILO_HEADSHOT}/primeira.png", body_image_url=None
    )


def test_is_empty_marca_o_par_sem_nenhuma_url() -> None:
    """CA-04: ``is_empty`` é o predicado de "nada a gravar" usado pelo backfill."""
    assert FighterImages(headshot_url=None, body_image_url=None).is_empty is True
    assert FighterImages(headshot_url="x", body_image_url=None).is_empty is False
    assert FighterImages(headshot_url=None, body_image_url="y").is_empty is False


def test_extracao_funciona_sobre_o_card_do_payload_real() -> None:
    """CA-04: o mesmo extrator roda sobre o card real -- 26 lutadores, todos com as duas URLs.

    O card de ``CitoEventStats`` e o de ``CitoCatalogItem`` são o mesmo ``CitoBoutBlock``
    (medido em 2026-09-01), então a fixture de contrato exercita a extração sem custo de quota.
    """
    reais = _stats_reais()
    item = CitoCatalogItem.model_validate(
        {
            "id": reais.event.id,
            "slug": reais.event.slug,
            "title": reais.event.title,
            "status": reais.event.status,
            "startsAt": "2026-08-23T00:00:00.000Z",
            "eventDate": reais.event.event_date.isoformat(),
            "bouts": [bout.model_dump(by_alias=True) for bout in reais.bouts],
        }
    )

    imagens = extract_fighter_images(item)

    assert len(imagens) == 26
    for imagem in imagens.values():
        assert imagem.headshot_url is not None
        assert imagem.body_image_url is not None


# --------------------------------------------------------------------------- #
# CA-04 -- testes-guarda: a arte do card NUNCA é atributo de lutador
# --------------------------------------------------------------------------- #


def test_arte_do_card_nunca_vira_headshot_nem_body() -> None:
    """CA-04: com ``profile`` ausente e só a arte no canto, a extração devolve **nada**.

    A arte (``event_fight_card_upper_body_of_standing_athlete``) é por luta, muda a cada card e
    é dela que o M6 tira o canto REAL. Aproveitá-la "para ganhar cobertura" gravaria em
    ``fighters`` uma imagem que não é atributo do atleta.
    """
    item = _item(
        _canto(
            "so-arte",
            arte=f"{_ESTILO_ARTE_DE_CARD}/2026-08/HERNANDEZ_ANTHONY_L_08-22.png?itok=x",
            com_profile=False,
        )
    )

    assert extract_fighter_images(item) == {}


def test_arte_do_card_nao_contamina_a_extracao_do_payload_real() -> None:
    """CA-04: sobre o card real, nenhuma das 52 URLs extraídas é arte de card."""
    reais = _stats_reais()
    item = CitoCatalogItem.model_validate(
        {
            "id": reais.event.id,
            "slug": reais.event.slug,
            "title": reais.event.title,
            "status": reais.event.status,
            "startsAt": "2026-08-23T00:00:00.000Z",
            "bouts": [bout.model_dump(by_alias=True) for bout in reais.bouts],
        }
    )

    imagens = extract_fighter_images(item)

    for imagem in imagens.values():
        assert "event_fight_card_upper_body_of_standing_athlete" not in (imagem.headshot_url or "")
        assert "event_fight_card_upper_body_of_standing_athlete" not in (
            imagem.body_image_url or ""
        )
        assert "event_results_athlete_headshot" in (imagem.headshot_url or "")
        assert "athlete_bio_full_body" in (imagem.body_image_url or "")


def test_body_image_tem_sufixo_de_lado_nos_dois_cantos_e_por_isso_nao_da_canto() -> None:
    """CA-04: o guarda que justifica o teste existir -- ``athlete_bio_full_body`` engana.

    Medido no payload real: a **arte do card** dá ``L`` e ``R`` aos dois cantos da mesma luta
    (é o canto de verdade), enquanto o ``bodyImageUrl`` dá ``L`` para os **dois**. Apontar
    ``art_side`` para a imagem de corpo inteiro "para ganhar cobertura" não ganharia nada e
    corromperia o canto -- que é o alvo do modelo -- sem que contagem nenhuma acusasse.
    """
    bout = _stats_reais().bouts[0]

    lados_da_arte = {canto.fighter_slug: art_side(canto.image_url) for canto in bout.fighters}
    lados_do_corpo = {
        canto.fighter_slug: art_side(canto.profile.body_image_url if canto.profile else None)
        for canto in bout.fighters
    }

    assert set(lados_da_arte.values()) == {"L", "R"}  # a arte distingue os cantos
    assert set(lados_do_corpo.values()) == {"L"}  # o corpo inteiro dá o MESMO lado aos dois


def test_recuperacao_de_canto_do_m6_continua_lendo_so_a_arte_do_card() -> None:
    """CA-04: ``art_side`` é alimentado por ``fighters[].image_url``, nunca pelas URLs de perfil.

    Teste-guarda contra a regressão descrita acima: se um dia alguém trocar a fonte do canto
    pela imagem de perfil, este teste quebra antes de o viés chegar ao modelo.
    """
    bout = _stats_reais().bouts[0]
    canto_azul = next(canto for canto in bout.fighters if canto.fighter_slug == "anthony-hernandez")
    canto_vermelho = next(
        canto for canto in bout.fighters if canto.fighter_slug == "gregory-rodrigues"
    )

    assert art_side(canto_azul.image_url) == "L"
    assert art_side(canto_vermelho.image_url) == "R"
    # As URLs que ESTA slice persiste não são a fonte do canto e não são consultadas por ela.
    imagens = extract_fighter_images(
        CitoCatalogItem.model_validate(
            {
                "id": "item-1",
                "slug": "ufc-fight-night-august-22-2026",
                "title": "UFC Fight Night: Hernandez vs Rodrigues",
                "status": "completed",
                "startsAt": "2026-08-23T00:00:00.000Z",
                "bouts": [bout.model_dump(by_alias=True)],
            }
        )
    )
    assert imagens["anthony-hernandez"].headshot_url != canto_azul.image_url
    assert imagens["anthony-hernandez"].body_image_url != canto_azul.image_url


def test_arte_de_card_no_campo_de_perfil_e_rejeitada() -> None:
    """CA-04: a Cito às vezes põe a arte do card **dentro do perfil** -- e ela é rejeitada ali.

    Medido na passada 1 do backfill (2026-09-01, custo zero): de 600 lutadores com imagem, **18**
    trazem no ``profile.bodyImageUrl`` uma URL do estilo
    ``event_fight_card_upper_body_of_standing_athlete``, não do ``athlete_bio_full_body`` que o
    campo promete. Ou seja: ler o campo certo não basta -- o campo certo às vezes carrega o
    conteúdo errado.

    Isso é o CA-04 no caso real: a arte do card não é atributo de lutador (a SPEC a coloca
    explicitamente fora de escopo), então ela não entra na coluna, venha de onde vier. Ausência
    explícita: a variante rejeitada permanece ``None``, e a outra é preservada.
    """
    item = _item(
        _canto(
            "yizha",
            headshot=f"{_ESTILO_HEADSHOT}/2026-02/YIZHA_02-01.png",
            body=f"{_ESTILO_ARTE_DE_CARD}/2026-02/YIZHA_L_01-31.png?itok=x",
        )
    )

    imagens = extract_fighter_images(item)

    assert imagens["yizha"] == FighterImages(
        headshot_url=f"{_ESTILO_HEADSHOT}/2026-02/YIZHA_02-01.png", body_image_url=None
    )


def test_arte_de_card_no_headshot_do_perfil_tambem_e_rejeitada() -> None:
    """CA-04: o mesmo filtro vale para o retrato -- a rejeição é por estilo, não por campo."""
    item = _item(
        _canto("so-arte-no-perfil", headshot=f"{_ESTILO_ARTE_DE_CARD}/2026-02/NOME_R_01-31.png")
    )

    assert extract_fighter_images(item) == {}


def test_variante_de_estilo_legitimo_diferente_e_preservada() -> None:
    """CA-04: o filtro rejeita a arte de card, não "todo estilo inesperado".

    Também medido na passada 1: um lutador traz o retrato no estilo ``teaser``. É imagem **do
    atleta**, não arte de luta -- descartá-la perderia dado legítimo, então ela passa.
    """
    teaser = "https://ufc.com/images/styles/teaser/s3/2026-04/MARTINETTI_ADRIAN_04-25.png?itok=y"
    item = _item(_canto("adrian-luna-martinetti", headshot=teaser))

    imagens = extract_fighter_images(item)

    assert imagens["adrian-luna-martinetti"].headshot_url == teaser
