"""Gate humano de gasto de quota da Cito -- regra de segurança compartilhada.

Nenhum comando de ingestão toca a Cito real sem confirmação humana explícita
(``--confirmar-gasto-de-quota``): o free tier é de 500 requisições por mês, e uma execução
descuidada consome o ciclo inteiro. O gate aborta **antes** da primeira unidade de quota --
antes mesmo de instanciar o cliente ou abrir a sessão.

Promovido de ``ingestion.cito.backfill_rounds`` (M5, Slice 05) para módulo próprio no M6
(Slice 03), quando nasceu o segundo callsite (``ingestion.cito.sync_catalog``). A função foi
movida como está, sem generalização: as alternativas eram duplicar a regra em dois módulos
ou o comando novo importar um símbolo privado de um módulo de backfill sem relação com ele.
"""

from __future__ import annotations


class HumanGateNotConfirmedError(RuntimeError):
    """Execução contra a rede real exigida sem a confirmação humana explícita do gasto de quota."""


def enforce_human_gate(*, fixture: bool, confirmed: bool) -> None:
    """Modo rede real sem ``--confirmar-gasto-de-quota`` aborta ANTES de qualquer fetch.

    O modo fixture (JSON local, 0 quota) não exige gate. Contra a Cito real, a confirmação
    explícita é obrigatória: sem ela, levanta ``HumanGateNotConfirmedError`` antes de
    instanciar o cliente ou tocar a rede.
    """
    if not fixture and not confirmed:
        raise HumanGateNotConfirmedError(
            "A execução contra a Cito real exige --confirmar-gasto-de-quota; "
            "nenhuma chamada foi disparada."
        )
