"""Consumo da API JSON oficial da UFC (fonte autoritativa de canto e antropometria).

Ver ``ingestion.ufc_official.dto`` para o contrato medido dos endpoints,
``ingestion.ufc_official.client`` para o cliente HTTP e
``ingestion.ufc_official.discovery`` para a varredura do catálogo e o mapeamento de eventos.
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
# NÃO é a mesma constante que ``analysis.dataset.FIRST_RELIABLE_CORNER_DATE`` (2010-01-01):
# aquela é o filtro do **treino** e esta é a janela de **autoridade da fonte**. Valores e
# propósitos diferentes -- não unificar, não importar uma no lugar da outra.
#
# Definida aqui, no pacote, porque todas as slices seguintes do M7 (03 a 07) precisam do mesmo
# corte: cinco definições locais da mesma data divergiriam em silêncio na primeira correção.
OFFICIAL_WINDOW_START: Final[date] = date(2010, 3, 21)
