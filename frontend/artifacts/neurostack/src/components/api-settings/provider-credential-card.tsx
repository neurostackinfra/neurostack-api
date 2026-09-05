export type ProviderCardState =
  | "configured"
  | "incomplete"
  | "available"
  | "coming_soon";

export interface ProviderCredentialCardProps {
  id: string;
  label: string;
  sub: string;
  state: ProviderCardState;
  badgeLabel: string;
  isExpanded?: boolean;
  onToggle?: () => void;
}

const BADGE_CLASSES: Record<ProviderCardState, string> = {
  configured: "border-emerald-500/30 bg-emerald-500/10 text-emerald-400",
  incomplete: "border-amber-500/30 bg-amber-500/10 text-amber-400",
  available: "border-sky-500/30 bg-sky-500/10 text-sky-400",
  coming_soon: "border-orange-500/30 bg-orange-500/10 text-orange-400",
};

/**
 * Provider card in the API-provider settings tab.
 *
 * A `coming_soon` card is inert by construction: it renders no interactive
 * role, so it cannot acquire focus, be selected, or contribute credentials to
 * a PATCH body. That is what keeps a provider with no runtime path from being
 * adopted through this surface; every other state opens an editor.
 */
export function ProviderCredentialCard({
  id,
  label,
  sub,
  state,
  badgeLabel,
  isExpanded = false,
  onToggle,
}: ProviderCredentialCardProps) {
  const body = (
    <div className="flex w-full items-start justify-between gap-2">
      <div className="min-w-0 flex-1">
        <span className="text-xs font-bold truncate">{label}</span>
        <span className="block text-[10px] opacity-70 mt-0.5">{sub}</span>
      </div>
      <span
        data-testid={`provider-badge-${state}-${id}`}
        className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase ${BADGE_CLASSES[state]}`}
      >
        {badgeLabel}
      </span>
    </div>
  );

  if (state === "coming_soon") {
    return (
      <article
        data-testid={`provider-card-${id}`}
        data-provider-id={id}
        data-card-state={state}
        className="rounded-lg border border-border bg-background px-3 py-2.5 text-muted-foreground opacity-70"
      >
        {body}
      </article>
    );
  }

  return (
    <button
      type="button"
      data-testid={`provider-card-${id}`}
      data-provider-id={id}
      data-card-state={state}
      aria-expanded={isExpanded}
      onClick={onToggle}
      className={`rounded-lg border px-3 py-2.5 text-left transition-colors ${
        isExpanded
          ? "border-accent bg-accent/10 text-foreground"
          : "border-border bg-background text-foreground hover:border-muted-foreground/50"
      }`}
    >
      {body}
    </button>
  );
}

export default ProviderCredentialCard;
