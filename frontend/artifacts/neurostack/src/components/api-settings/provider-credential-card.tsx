import type { KeyboardEvent, ReactNode } from "react";

export interface ProviderCredentialCardProps {
  id: string;
  label: string;
  sub: string;
  activeCls: string;
  isSelected: boolean;
  onSelect: (id: string) => void;
  isConfigOnly?: boolean;
  isComingSoon?: boolean;
  configOnlyLabel?: string;
  comingSoonLabel?: string;
  statusDot?: ReactNode;
  disabled?: boolean;
  children?: ReactNode;
}

/**
 * Provider credential card used in the API-provider settings tab.
 *
 * Closed state renders label + subtitle + optional status dot / badges.
 * Expanded state (parent-controlled via `isSelected`) reveals `children`.
 *
 * - `isComingSoon`: the card is fully non-interactive (pointer-events-none,
 *   opacity-50). Clicks and keyboard activation are ignored so the provider
 *   is impossible to select from the UI.
 * - `isConfigOnly`: the card is interactive but shows a "Config only" badge
 *   so operators know saved credentials will not be exercised at runtime.
 * - Enter/Space toggle selection when the card is interactive.
 * - `aria-expanded` mirrors `isSelected` so screen readers announce the
 *   expanded/collapsed state consistently.
 */
export function ProviderCredentialCard({
  id,
  label,
  sub,
  activeCls,
  isSelected,
  onSelect,
  isConfigOnly = false,
  isComingSoon = false,
  configOnlyLabel = "Configuração apenas",
  comingSoonLabel = "Em breve",
  statusDot,
  disabled = false,
  children,
}: ProviderCredentialCardProps) {
  const inert = isComingSoon || disabled;

  const activate = () => {
    if (inert) return;
    onSelect(id);
  };

  const handleKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (inert) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect(id);
    }
  };

  const containerClass = [
    "rounded-lg border transition-all",
    isSelected && !isComingSoon
      ? `${activeCls} border-2`
      : "border-border text-muted-foreground hover:border-muted-foreground/50",
    isComingSoon ? "pointer-events-none opacity-50" : "",
    disabled && !isComingSoon ? "opacity-60 cursor-not-allowed" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      data-testid={`provider-card-${id}`}
      data-provider-id={id}
      className={containerClass}
    >
      <div
        role="button"
        tabIndex={inert ? -1 : 0}
        aria-expanded={isSelected}
        aria-disabled={inert || undefined}
        onClick={activate}
        onKeyDown={handleKey}
        className="flex flex-col items-start px-3 py-2.5 text-left cursor-pointer focus:outline-none focus:ring-1 focus:ring-accent rounded-lg"
      >
        <div className="flex w-full items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-bold truncate">{label}</span>
              {statusDot}
            </div>
            <span className="block text-[10px] opacity-70 mt-0.5">{sub}</span>
          </div>
          <div className="flex flex-col items-end gap-1 shrink-0">
            {isComingSoon && (
              <span
                data-testid={`provider-badge-coming-soon-${id}`}
                className="text-[9px] font-bold px-1.5 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/30"
              >
                {comingSoonLabel}
              </span>
            )}
            {isConfigOnly && !isComingSoon && (
              <span
                data-testid={`provider-badge-config-only-${id}`}
                className="text-[9px] font-bold px-1.5 py-0.5 rounded border bg-slate-500/10 text-slate-400 border-slate-500/30"
              >
                {configOnlyLabel}
              </span>
            )}
          </div>
        </div>
      </div>
      {isSelected && !isComingSoon && children && (
        <div
          data-testid={`provider-card-expanded-${id}`}
          className="border-t border-border/50 px-3 py-3"
        >
          {children}
        </div>
      )}
    </div>
  );
}

export default ProviderCredentialCard;
