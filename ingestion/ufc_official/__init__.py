"""Consumo da API JSON oficial da UFC (fonte autoritativa de canto e antropometria).

Ver ``ingestion.ufc_official.dto`` para o contrato medido dos endpoints,
``ingestion.ufc_official.client`` para o cliente HTTP e
``ingestion.ufc_official.discovery`` para a varredura do catálogo e o mapeamento de eventos, e
``ingestion.ufc_official.fighter_ids`` para o backfill do identificador de lutador.
"""

from __future__ import annotations

from datetime import date
from typing import Final

# Primeira data em que o canto foi REALMENTE registrado: 2010-03-21, UFC Live: Vera vs Jones.
# Antes dela as três fontes (Kaggle, Cito e a API oficial) trazem o mesmo canto **fabricado** --
# 100,0% de vitórias do vermelho até 2010-03-06 --, o que prova que descendem da mesma operação
# de estatística da UFC. Nada anterior a esta data é tocado por nenhuma slice do M7 (RF-03).
#
# É **limitação da fonte**, medida (SPEC 008, ADR 0006), nunca parâmetro de tuning: não vira
# argumento de função, não vira variável de ambiente e não vira flag de linha de comando. Um
# corte que se pode afrouxar por conveniência deixa de ser garantia.
#
# NÃO é a mesma constante que ``analysis.dataset.FIRST_RELIABLE_CORNER_DATE``. Desde a Slice
# 00B da SPEC 009 as duas **coincidem em valor** (2010-03-21) -- por **medição convergente**,
# não por unificação: esta é a janela de **autoridade da fonte** (SPEC 008) e aquela é o filtro
# do **treino** (ADR 0006, Emenda 1). Propósitos, módulos e consumidores diferentes; uma pode
# mudar sem a outra. Continuam **proibidas** de virar uma constante só ou de ser importada uma
# no lugar da outra -- a coincidência de hoje é resultado, não definição. A guarda executável é
# ``test_constante_da_janela_nao_e_a_do_filtro_de_treino``, que afirma identidade, módulo e
# valor.
#
# Definida aqui, no pacote, porque todas as slices seguintes do M7 (03 a 07) precisam do mesmo
# corte: cinco definições locais da mesma data divergiriam em silêncio na primeira correção.
OFFICIAL_WINDOW_START: Final[date] = date(2010, 3, 21)
