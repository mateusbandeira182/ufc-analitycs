# Homônimos da fonte oficial (SPEC 008, Sprint 008-03)

Dezesseis payloads de evento -- **dois por nome normalizado** -- que cobrem os oito nomes que
mapeiam para mais de um `FighterId` na API oficial da UFC. São a base do teste parametrizado
`tests/ingestion/test_ufc_official_fighter_ids_homonimos.py::test_nome_com_multiplos_fighter_ids_resolve_pelo_contexto_da_luta`
(CA-03). Nomeados como o cache em disco os nomeia (`event_live_{eventId}.json`), então o
diretório **é** um diretório de cache válido e o modo offline o consome direto.

## Derivação: recorte, nunca reconstrução

Cada arquivo é a captura real de `https://d29dxerjsp82wz.cloudfront.net/api/v3/event/live/{id}.json`
(varredura de 2026-09-01, materializada em `.cache/ufc_official/`) com **uma única**
transformação: `FightCard` foi reduzido à **luta do homônimo**. Nenhum valor foi editado,
nenhum campo foi removido de dentro de um objeto e nenhuma forma foi remontada a partir de um
DTO -- é a mesma disciplina de `../ufc_official_catalog/`.

O **contrato de verdade** continua sendo `../ufc_official/`, cujas capturas são verbatim, sem
nem esse recorte.

## Como a lista dos oito nomes foi obtida

Agrupando os 1.341 payloads do cache por `normalize_name(FirstName + LastName)` e contando
`FighterId` distintos. **Oito** nomes têm mais de um -- o número que a SPEC 008 afirma sem
enumerar. O recorte importa e está medido:

| Recorte | Nomes com mais de um `FighterId` |
|---|---|
| Todas as promoções do cache (1.341 eventos) | **8** |
| Só eventos do UFC (`OrganizationId == 1`) | 2 (`bruno silva`, `lance gibson`) |
| Só UFC e dentro da janela (>= 2010-03-21) | 1 (`bruno silva`) |

Ou seja: os oito são reais, mas seis deles só existem porque a fonte cobre PRIDE, K-1,
Strikeforce, Bellator e DWCS. Dentro do escopo do projeto (UFC) e da janela da RF-03, o único
homônimo que a ingestão de fato encontra é o `bruno silva` -- e é por isso que a coleta contra a
base de desenvolvimento inteira (629 eventos mapeados, 14.710 observações) devolve **zero**
nomes com mais de um id: o segundo `Bruno Silva` persistido não tem luta nenhuma na base.

## Os dezesseis arquivos

| Nome normalizado | `FighterId` | Evento | Data local | Promoção | Na janela? |
|---|---|---|---|---|---|
| `bruno silva` | 3283 | UFC 243: Whittaker vs. Adesanya (946) | 2019-10-06 | UFC | sim |
| `bruno silva` | 3314 | UFC Fight Night: Jung vs. Ige (1035) | 2021-06-19 | UFC | sim |
| `jean silva` | 835 | PRIDE Bushido 8 (139) | 2005-07-17 | PRIDE | **não** |
| `jean silva` | 4064 | UFC Fight Night: Ankalaev vs. Walker 2 (1185) | 2024-01-13 | UFC | sim |
| `joey gomez` | 2738 | UFC Fight Night: Dillashaw vs Cruz (759) | 2016-01-17 | UFC | sim |
| `joey gomez` | 3540 | DWCS 2.4 (883) | 2018-07-10 | DWCS | sim |
| `lance gibson` | 131 | UFC 29: Defense of the Belts (90) | 2000-12-16 | UFC | **não** |
| `lance gibson` | 4499 | UFC Fight Night: Royval vs. Kape (1292) | 2025-12-13 | UFC | sim |
| `michael mcdonald` | 1007 | K-1 Beast 2004 (317) | 2004-03-14 | K-1 | **não** |
| `michael mcdonald` | 1650 | UFC Fight Night: Nogueira vs Davis (533) | 2011-03-26 | UFC | sim |
| `mike davis` | 1318 | Strikeforce - Young Guns 3 (248) | 2008-09-12 | Strikeforce | **não** |
| `mike davis` | 3135 | UFC Fight Night: Jacare vs. Hermansson (916) | 2019-04-27 | UFC | sim |
| `tony johnson` | 1359 | DWCS 3.2 (928) | 2019-06-25 | DWCS | sim |
| `tony johnson` | 2493 | Bellator 136: Brooks vs. Jansen (715) | 2015-04-10 | Bellator | sim |
| `victor valenzuela` | 1259 | Strikeforce - Shamrock vs. Baroni (255) | 2007-06-21 | Strikeforce | **não** |
| `victor valenzuela` | 4489 | UFC Fight Night: Sterling vs. Zalal (1307) | 2026-04-25 | UFC | sim |

Quando o mesmo `FighterId` aparece em vários eventos, o escolhido é o **mais antigo dentro da
janela**, preferindo o UFC; só quando o id não tem nenhuma ocorrência na janela é que o evento
anterior a 2010-03-21 entra -- e aí ele serve de prova da guarda da RF-03, não de cobertura.

`lance gibson` merece nota: as duas grafias da fonte são `Lance Gibson` e `Lance Gibson Jr.`, e
elas colidem porque `normalize_name` descarta sufixo de linhagem. É colisão real de
normalização, não artefato do recorte.

## Como recapturar

```bash
BASE=https://d29dxerjsp82wz.cloudfront.net
DEST=tests/ingestion/fixtures/ufc_official_homonimos
# O índice da luta a preservar sai da própria captura: é a única em que o homônimo aparece.
for ID in 946 1035 139 1185 759 883 90 1292 317 533 248 916 928 715 255 1307; do
  curl -sS "$BASE/api/v3/event/live/$ID.json" > "$DEST/event_live_$ID.json"
done
# ... e então recortar FightCard na luta do homônimo, sem tocar em mais nada.
```
