/**
 * Evento classificado como `upcoming` cuja data já passou. O backend particiona
 * a lista pelo `status` da luta, não pela data, e o `gap_sync` é **manual**:
 * então um evento de data passada com o card inteiro ainda `scheduled` é sinal
 * de catálogo atrasado, não de erro. A linha leva o marcador "aguardando
 * sincronização" para que uma tabela de "Próximos" com datas passadas não seja
 * uma contradição muda.
 *
 * `today` entra por parâmetro para o teste não depender do relógio, e a
 * comparação é de string `YYYY-MM-DD`: `new Date(iso)` é meia-noite UTC e
 * exibiria o dia anterior em fuso negativo — foi assim que o `formatIsoDate`
 * chegou a renderizar `"01T00:00:00Z/09/2026"` sem erro nenhum.
 *
 * O dia do próprio evento é `false`: ele ainda não aconteceu.
 */
export function isAwaitingSync(eventDate: string, today: string): boolean {
  return eventDate < today;
}

/**
 * Data de hoje como `YYYY-MM-DD`, para injetar em `isAwaitingSync`. Montada dos
 * componentes **locais** da data: `toISOString()` converte para UTC e devolveria
 * o dia anterior em fuso negativo — o mesmo deslize que a comparação de string
 * existe para evitar.
 */
export function todayIsoDate(now: Date = new Date()): string {
  const year = String(now.getFullYear()).padStart(4, "0");
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
