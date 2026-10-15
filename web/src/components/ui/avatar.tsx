import { cn } from "@/lib/utils";

interface AvatarProps {
  /** Nome do lutador: é o conteúdo acessível e a origem das iniciais do fallback. */
  name: string;
  /** `headshot_url` do lutador; `null`/ausente cai no fallback por iniciais. */
  src?: string | null;
  /** Tamanho e ajustes ficam por conta do consumidor (ex.: `size-8` na tabela). */
  className?: string;
}

/**
 * Retrato do lutador, recortado no octógono — a moldura da arena é a moldura de
 * quem entra nela. A forma é a mesma com ou sem retrato: só 794 dos 2.754
 * lutadores têm `headshot_url` e lutador histórico nunca terá, então o fallback
 * é o caso comum e não pode parecer um buraco na tela.
 *
 * Presentacional puro: sem server-state, sem variante de tamanho (o tamanho
 * entra por `className`, como no `Skeleton`) e sem fallback por erro de rede —
 * a decisão "tem retrato ou não" é o valor de `src`, não um evento de carga.
 *
 * A imagem leva `alt={name}` (e não `alt=""` decorativo). O fallback expõe as
 * iniciais como texto: repetir o nome dentro dele duplicaria o texto em toda
 * tela que hoje mostra o avatar ao lado do nome. Quando uma tela exibir o
 * avatar sozinho (Sprints 07 e 08), quem rotula é o consumidor — aqui não há
 * uso concreto que justifique a prop.
 */
export function Avatar({ name, src, className }: AvatarProps) {
  const base = cn(
    "octagon-clip flex size-10 shrink-0 items-center justify-center overflow-hidden bg-secondary",
    className,
  );

  if (src) {
    return <img src={src} alt={name} className={cn(base, "object-cover")} />;
  }

  return (
    <span className={base}>
      <span className="font-display text-[0.7em] font-semibold uppercase leading-none tracking-wide text-muted-foreground">
        {initialsFrom(name)}
      </span>
    </span>
  );
}

/**
 * Iniciais do lutador: primeira letra da primeira e da última palavra do nome.
 * `Array.from` porque índice de string quebra par substituto, e sem normalizar
 * — a inicial exibida preserva `Ł`, `Ø` e afins. Nome vazio vira interrogação:
 * um espaço em branco na tela não se explica.
 */
function initialsFrom(name: string): string {
  const palavras = name.trim().split(/\s+/).filter(Boolean);
  if (palavras.length === 0) {
    return "?";
  }
  const primeira = Array.from(palavras[0] ?? "")[0] ?? "";
  const ultima =
    palavras.length > 1
      ? (Array.from(palavras[palavras.length - 1] ?? "")[0] ?? "")
      : "";
  return `${primeira}${ultima}`.toLocaleUpperCase("pt-BR");
}
