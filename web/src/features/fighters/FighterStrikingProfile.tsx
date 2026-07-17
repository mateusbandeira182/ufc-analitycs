import type { StrikingProfileOut } from "@/api/schema";
import { Skeleton } from "@/components/ui/skeleton";
import { formatShare } from "@/features/fighters/statsFormat";
import { useFighterStats } from "@/features/fighters/useFighterStats";

/*
  "Perfil de striking": os shares de golpe significativo conectado por alvo
  (cabeça/corpo/perna) e por posição (distância/clinch/solo), agregados on demand
  pelo backend em `/fighters/:id/stats`. É um retrato de carreira — independe do
  recorte (últimas N / período) das médias, que segue derivado do histórico. Cada
  share vem como fração (0..1) ou nulo; nulo vira "sem dado", nunca NaN/Infinity.
*/

interface Share {
  label: string;
  value: number | null;
}

/**
 * Seção do perfil de striking na página de estatísticas. Consome o
 * `striking_profile` do resumo do lutador e trata os três estados do server-state
 * (carregando, erro, sucesso). O id inválido nunca chega aqui — a página o barra
 * antes.
 */
export function FighterStrikingProfile({ fighterId }: { fighterId: number }) {
  const { data, isPending, isError } = useFighterStats(fighterId);

  return (
    <section>
      <h2 className="mb-1 font-display text-xl font-bold uppercase tracking-wide">
        Perfil de striking
      </h2>
      <p className="mb-5 text-[0.7rem] uppercase tracking-widest text-muted-foreground">
        Golpes significativos conectados, por alvo e por posição
      </p>

      {isPending ? (
        <div
          aria-busy="true"
          aria-label="Carregando perfil de striking"
          className="grid gap-6 sm:grid-cols-2"
        >
          <Skeleton className="h-32 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      ) : isError ? (
        <p role="status" className="text-sm text-muted-foreground">
          Não foi possível carregar o perfil de striking deste lutador.
        </p>
      ) : (
        <div className="grid gap-8 sm:grid-cols-2">
          <ShareGroup
            title="Por alvo"
            shares={targetShares(data.striking_profile)}
          />
          <ShareGroup
            title="Por posição"
            shares={positionShares(data.striking_profile)}
          />
        </div>
      )}
    </section>
  );
}

/** Shares por alvo do golpe (onde acertou): cabeça, corpo, perna. */
function targetShares(profile: StrikingProfileOut): Share[] {
  return [
    { label: "Cabeça", value: profile.share_head },
    { label: "Corpo", value: profile.share_body },
    { label: "Perna", value: profile.share_leg },
  ];
}

/** Shares por posição da troca (de onde acertou): distância, clinch, solo. */
function positionShares(profile: StrikingProfileOut): Share[] {
  return [
    { label: "Distância", value: profile.share_distance },
    { label: "Clinch", value: profile.share_clinch },
    { label: "Solo", value: profile.share_ground },
  ];
}

/** Um agrupamento de shares (alvo ou posição) com seu rótulo. */
function ShareGroup({ title, shares }: { title: string; shares: Share[] }) {
  return (
    <div>
      <p className="font-display text-sm font-bold uppercase tracking-[0.2em] text-primary">
        {title}
      </p>
      <dl className="mt-4 grid gap-4">
        {shares.map((share) => (
          <ShareBar key={share.label} label={share.label} value={share.value} />
        ))}
      </dl>
    </div>
  );
}

/**
 * Uma linha do perfil: rótulo do alvo/posição, a barra proporcional (belt-gold) e
 * o percentual. Sem dado — share nulo/não finito — deixa a barra vazia e o texto
 * "sem dado"; o percentual (texto) carrega a informação, a barra só a reforça.
 */
function ShareBar({ label, value }: Share) {
  const width =
    value !== null && Number.isFinite(value)
      ? Math.min(100, Math.max(0, value * 100))
      : 0;

  return (
    <div className="grid grid-cols-[5.5rem_1fr_3.5rem] items-center gap-3">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <div
        aria-hidden="true"
        className="h-2 w-full overflow-hidden rounded-full border border-border bg-card"
      >
        <span
          className="block h-full bg-belt-gold"
          style={{ width: `${String(width)}%` }}
        />
      </div>
      <dd className="text-right font-mono text-sm font-bold tabular-nums text-belt-gold">
        {formatShare(value)}
      </dd>
    </div>
  );
}
