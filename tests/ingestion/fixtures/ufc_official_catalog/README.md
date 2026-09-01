# Catálogo derivado da fonte oficial (SPEC 008, Sprint 008-02)

Dez payloads de evento usados pelos testes da **varredura** e do **mapeamento**
(`tests/ingestion/test_ufc_official_discovery_cli.py`), nomeados exatamente como o cache em
disco os nomeia (`event_live_{eventId}.json`) -- o diretório **é** um diretório de cache
válido, e é assim que o modo offline do comando o consome.

## Derivação: recorte, nunca reconstrução

Cada arquivo é a captura real de `https://d29dxerjsp82wz.cloudfront.net/api/v3/event/live/{id}.json`
(2026-09-01) com **uma única** transformação: `FightCard` foi cortado nas duas primeiras lutas,
para o conjunto caber no repositório. Nenhum valor foi editado, nenhum campo foi removido de
dentro de um objeto e nenhuma forma foi remontada a partir de um DTO -- é a mesma disciplina do
conjunto `catalog_paginado_derivado/` da Cito.

O **contrato de verdade** continua sendo `../ufc_official/`, cujas capturas são verbatim, sem
nem esse recorte. Se um teste precisa provar a forma da fonte, ele lê de lá; estes dez servem
à lógica de varredura, filtro e casamento.

## Os dez ids, e por que cada um está aqui

| id | Evento | Data local | `OrganizationId` | Papel no teste |
|---|---|---|---|---|
| 1 | UFC 92: The Ultimate 2008 | 2008-12-27 | 1 (UFC) | Bloco denso de ids baixos: a varredura fria começa aqui |
| 2 | The Ultimate Fighter: Team Nogueira vs. Team Mir Finale | 2008-12-13 | 1 (UFC) | Idem |
| 3 | UFC Fight Night - Fight for the Troops | 2008-12-10 | 1 (UFC) | Idem; fecha o bloco que a varredura fria enxerga |
| 4 | UFC 91: Couture vs Lesnar | 2008-11-15 | 1 (UFC) | O **id novo acima da fronteira**: só é servido no teste de descoberta incremental |
| 200 | WEC 29: Condit vs. Larson | 2007-08-05 | 3 (WEC) | Promoção fora do escopo: precisa ser descartada pelo filtro |
| 380 | Gladiator FC - Day 2 | 2004-06-27 | 33 (Gladiator FC) | **`TimeZone` nulo**: prova que o filtro de promoção precisa vir antes do cálculo da data local |
| 700 | UFC 184: Rousey vs Zingano | **2015-02-28** | 1 (UFC) | Data local != data UTC (`StartTime` é `2015-03-01T00:00Z` sob `GMT-08:00`) |
| 900 | DWCS Brazil 1.3 | 2018-08-13 | 67 (DWCS) | Fase de promoção posterior: descartada como o WEC |
| 1069 | UFC 271: Adesanya vs. Whittaker 2 | -- | 1 (UFC) | **`TimeZone` nulo num evento do UFC**: fica fora do catálogo, contado e nomeado, nunca datado por palpite |
| 1124 | UFC 282: Blachowicz vs. Ankalaev | 2022-12-10 | 1 (UFC) | Evento da janela que casa por nome com a nossa base (`events.id = 117`) |

Os ids **não** são contíguos de propósito: 1-4 formam o bloco denso que os testes de fronteira
percorrem, e 200/380/700/900/1069/1124 são ids reais esparsos, que só o modo offline (que lê o diretório
inteiro como cache) alcança. É a mesma assimetria da fonte real, onde a varredura fria depende
da densidade do intervalo.

## Como recapturar

```bash
BASE=https://d29dxerjsp82wz.cloudfront.net
for ID in 1 2 3 4 200 380 700 900 1069 1124; do
  curl -sS "$BASE/api/v3/event/live/$ID.json" \
    | python -c 'import json,sys; p=json.load(sys.stdin); p["LiveEventDetail"]["FightCard"]=p["LiveEventDetail"]["FightCard"][:2]; print(json.dumps(p,separators=(",",":")))' \
    > "tests/ingestion/fixtures/ufc_official_catalog/event_live_$ID.json"
done
```
