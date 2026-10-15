import { AlertTriangle, UserX, Users } from "lucide-react";
import type { ReactNode } from "react";

import type { UnavailableCode } from "@/api/schema";

interface UnavailablePredictionProps {
  /** Enum legível por máquina: é ELE que escolhe o aviso (RF-28 / D-08). */
  code: UnavailableCode | null;
  /** Prosa pronta do backend, com ids embutidos. EXIBIDA verbatim, JAMAIS parseada. */
  reason: string | null;
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
}: UnavailablePredictionProps) {
  const { icon, title, hint } = avisoDe(code);

  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border bg-card/50 px-6 py-12 text-center"
    >
      {icon}
      <h3 className="font-display text-lg font-semibold uppercase tracking-wide">
        {title}
      </h3>
      <p className="max-w-md text-sm text-muted-foreground">{hint}</p>
      {reason ? (
        <p className="max-w-md text-xs text-muted-foreground/80">{reason}</p>
      ) : null}
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
