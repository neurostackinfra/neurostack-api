export interface ProviderCredentialCardProps {
  id: string;
  label: string;
  sub: string;
  comingSoonLabel: string;
}

/**
 * Display-only provider card used in the API-provider settings tab.
 *
 * All providers are unavailable for new configuration in this release. This
 * intentionally renders neither an interactive role nor form children: a card
 * cannot acquire focus, select a provider, or contribute credentials to a
 * PATCH body. Existing credentials are therefore left exclusively to the
 * legacy backend state until a provider receives direct validation.
 */
export function ProviderCredentialCard({
  id,
  label,
  sub,
  comingSoonLabel,
}: ProviderCredentialCardProps) {
  return (
    <article
      data-testid={`provider-card-${id}`}
      data-provider-id={id}
      data-coming-soon="true"
      className="rounded-lg border border-border bg-background px-3 py-2.5 text-muted-foreground opacity-70"
    >
      <div
        className="flex w-full items-start justify-between gap-2"
      >
        <div className="min-w-0 flex-1">
          <span className="text-xs font-bold truncate">{label}</span>
          <span className="block text-[10px] opacity-70 mt-0.5">{sub}</span>
        </div>
        <span
          data-testid={`provider-badge-coming-soon-${id}`}
          className="shrink-0 rounded border border-orange-500/30 bg-orange-500/10 px-1.5 py-0.5 text-[10px] font-bold uppercase text-orange-400"
        >
          {comingSoonLabel}
        </span>
      </div>
    </article>
  );
}

export default ProviderCredentialCard;
