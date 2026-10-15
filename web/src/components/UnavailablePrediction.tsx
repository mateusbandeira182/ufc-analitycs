import { AlertTriangle, UserX, Users } from "lucide-react";
import type { ReactNode } from "react";

import type { UnavailableCode } from "@/api/schema";

interface UnavailablePredictionProps {
  /** Enum legível por máquina: é ELE que escolhe o aviso (RF-28 / D-08). */
  code: UnavailableCode | null;
  /** Prosa pronta do backend, com ids embutidos. EXIBIDA verbatim, JAMAIS parseada. */
  reason: string | null;
  /**
   * `"section"` é o bloco centrado da página de confronto; `"inline"` é a linha
   * dentro do card de uma luta — treze blocos de `py-12` num card de evento
   * seriam ilegíveis. Só a **moldura** muda: o `switch` sobre os três códigos e a
   * prosa são os mesmos nos dois, porque dois componentes com a mesma redação
   * divergiriam no primeiro ajuste de texto.
   */
  variant?: "section" | "inline";
}

/** Aviso de cada causa: título e orientação, escolhidos pelo código. */
interface Aviso {
  icon: ReactNode;
  title: string;
  hint: string;
}

/**
 * Palpite que o modelo não produziu, com o aviso escolhido pelo
 * `unavailable_code` — **nunca** por parse de `unavailable_reason` (D-08). A
 * prosa é texto pronto para gente, com ids embutidos: a tela a exibe e não a
 * interpreta. Parseá-la é o anti-padrão que a própria D-04 nomeia ao rejeitar o
 * 422.
 *
 * Nenhum caminho daqui renderiza barra ou porcentagem, e muito menos 50/50:
 * 50/50 se lê como opinião do modelo quando é ausência dela (RF-21), e
 * `clampProbability(null)` renderizaria "0%" — uma afirmação sobre uma luta que
 * ninguém mediu.
 */
export function UnavailablePrediction({
  code,
  reason,
  variant = "section",
}: UnavailablePredictionProps) {
  const { icon, title, hint } = avisoDe(code);
  const inline = variant === "inline";

  return (
    <div
      role="status"
      className={
        inline
          ? "flex items-start gap-2.5 border-t border-dashed border-border bg-card/40 px-4 py-3"
          : "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
      }
    >
      {icon}
      {/* `contents` deixa o agrupador fora do layout na variante de seção, que
          empilha os três textos no centro da própria coluna flex. */}
      <div className={inline ? "min-w-0" : "contents"}>
        <h3
          className={
            inline
              ? "font-display text-xs font-semibold uppercase tracking-[0.15em]"
              : "font-display text-lg font-semibold uppercase tracking-wide"
          }
        >
          {title}
        </h3>
        <p
          className={
            inline
              ? "mt-1 text-xs text-muted-foreground"
              : "max-w-md text-sm text-muted-foreground"
          }
        >
          {hint}
        </p>
        {reason ? (
          <p
            className={
              inline
                ? "mt-1 text-[0.7rem] leading-relaxed text-muted-foreground/80"
                : "max-w-md text-xs text-muted-foreground/80"
            }
          >
            {reason}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Aviso por causa. O `switch` fecha sobre os três códigos do contrato; qualquer
 * outro valor (código ausente, ou um que um backend mais novo passe a emitir)
 * cai no genérico — a tela degrada em vez de estourar.
 */
function avisoDe(code: UnavailableCode | null): Aviso {
  switch (code) {
    case "missing_history":
      return {
        icon: <UserX aria-hidden="true" className="size-6 text-primary" />,
        title: "Sem histórico para prever",
        hint: "Um dos atletas não tem luta anterior no acervo, e o modelo só aprende com o que já aconteceu. Na estreia não há o que medir.",
      };
    case "missing_corners":
      return {
        icon: <Users aria-hidden="true" className="size-6 text-primary" />,
        title: "Luta sem os dois cantos",
        hint: "O card ainda não traz um atleta no canto vermelho e um no azul para esta luta.",
      };
    case "repeated_fighter":
      return {
        icon: <Users aria-hidden="true" className="size-6 text-primary" />,
        title: "Atleta em mais de uma luta do card",
        hint: "Um dos atletas aparece em mais de uma luta deste card, e a previsão exige um atleta por luta.",
      };
    default:
      return {
        icon: (
          <AlertTriangle aria-hidden="true" className="size-6 text-primary" />
        ),
        title: "Sem previsão para este confronto",
        hint: "O modelo não produziu uma probabilidade para esta dupla.",
      };
  }
}
