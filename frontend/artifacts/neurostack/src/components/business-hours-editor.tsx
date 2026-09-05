import { useEffect, useState, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ChevronDown, ChevronUp, Pencil } from "lucide-react";

/**
 * Weekly business-hours schedule shared with the backend contract at
 * `src/models/clinic_config.py::BusinessHoursWindow`.
 *
 * Keys are lower-case day codes; days not present in the map are treated
 * as closed. Times are `HH:MM` strings in the clinic's local timezone.
 */
export type BusinessDayCode = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

export interface BusinessHoursWindow {
  open: string;
  close: string;
}

export type BusinessHoursMap = Partial<Record<BusinessDayCode, BusinessHoursWindow>>;

export const BUSINESS_DAY_ORDER: BusinessDayCode[] = [
  "mon",
  "tue",
  "wed",
  "thu",
  "fri",
  "sat",
  "sun",
];

export interface BusinessHoursEditorProps {
  value: BusinessHoursMap | null;
  onChange: (next: BusinessHoursMap | null) => void;
  errors: Record<string, string>;
  disabled: boolean;
  locale?: "pt" | "en";
}

interface DayLabel {
  pt: string;
  en: string;
}

const DAY_LABELS: Record<BusinessDayCode, DayLabel> = {
  mon: { pt: "Seg", en: "Mon" },
  tue: { pt: "Ter", en: "Tue" },
  wed: { pt: "Qua", en: "Wed" },
  thu: { pt: "Qui", en: "Thu" },
  fri: { pt: "Sex", en: "Fri" },
  sat: { pt: "Sáb", en: "Sat" },
  sun: { pt: "Dom", en: "Sun" },
};

const DAY_FULL: Record<BusinessDayCode, DayLabel> = {
  mon: { pt: "Segunda-feira", en: "Monday" },
  tue: { pt: "Terça-feira", en: "Tuesday" },
  wed: { pt: "Quarta-feira", en: "Wednesday" },
  thu: { pt: "Quinta-feira", en: "Thursday" },
  fri: { pt: "Sexta-feira", en: "Friday" },
  sat: { pt: "Sábado", en: "Saturday" },
  sun: { pt: "Domingo", en: "Sunday" },
};

interface CompactHoursCardProps {
  summary: string;
  children: ReactNode;
  editLabel: string;
  closeLabel: string;
  testId: string;
  forceOpen?: boolean;
}

export function CompactHoursCard({
  summary,
  children,
  editLabel,
  closeLabel,
  testId,
  forceOpen = false,
}: CompactHoursCardProps) {
  const [open, setOpen] = useState(forceOpen);

  useEffect(() => {
    if (forceOpen) setOpen(true);
  }, [forceOpen]);

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <div className="overflow-hidden rounded-md border border-border bg-muted/10" data-testid={testId}>
        <div className="flex min-h-11 items-center gap-3 px-3 py-2">
          <p
            className="min-w-0 flex-1 truncate text-sm text-foreground"
            title={summary}
            data-testid={`${testId}-summary`}
          >
            {summary}
          </p>
          <CollapsibleTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8 shrink-0 gap-1.5 px-2 text-xs"
              aria-label={open ? closeLabel : editLabel}
              data-testid={`${testId}-toggle`}
            >
              {open ? <ChevronUp className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
              {open ? closeLabel : editLabel}
              {!open && <ChevronDown className="h-3.5 w-3.5" />}
            </Button>
          </CollapsibleTrigger>
        </div>
        <CollapsibleContent className="border-t border-border p-3">
          {children}
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

export function formatBusinessHoursSummary(
  value: BusinessHoursMap | null,
  locale: "pt" | "en" = "pt",
): string {
  const current = value ?? {};
  if (Object.keys(current).length === 0) {
    return locale === "en" ? "No hours configured" : "Nenhum horário configurado";
  }

  const groups: Array<{
    days: BusinessDayCode[];
    window: BusinessHoursWindow | null;
  }> = [];

  for (const day of BUSINESS_DAY_ORDER) {
    const window = current[day] ?? null;
    const previous = groups.at(-1);
    const sameWindow = previous
      && previous.window?.open === window?.open
      && previous.window?.close === window?.close;

    if (sameWindow) {
      previous.days.push(day);
    } else {
      groups.push({ days: [day], window });
    }
  }

  return groups.map(({ days, window }) => {
    const first = DAY_LABELS[days[0]][locale];
    const last = DAY_LABELS[days[days.length - 1]][locale];
    const label = days.length === 1 ? first : `${first}–${last}`;
    const hours = window
      ? `${window.open}–${window.close}`
      : locale === "en" ? "closed" : "fechado";
    return `${label}: ${hours}`;
  }).join(" | ");
}

function emitOrNull(next: BusinessHoursMap): BusinessHoursMap | null {
  return Object.keys(next).length === 0 ? null : next;
}

export function BusinessHoursEditor({
  value,
  onChange,
  errors,
  disabled,
  locale = "pt",
}: BusinessHoursEditorProps) {
  const current: BusinessHoursMap = value ?? {};
  const txt = (pt: string, en: string) => (locale === "en" ? en : pt);
  const hasErrors = Object.keys(errors).length > 0;

  const setDay = (day: BusinessDayCode, window: BusinessHoursWindow) => {
    const next: BusinessHoursMap = { ...current, [day]: window };
    onChange(emitOrNull(next));
  };

  const closeDay = (day: BusinessDayCode) => {
    const next: BusinessHoursMap = { ...current };
    delete next[day];
    onChange(emitOrNull(next));
  };

  const toggleDay = (day: BusinessDayCode, open: boolean) => {
    if (open) {
      setDay(day, { open: "09:00", close: "18:00" });
    } else {
      closeDay(day);
    }
  };

  return (
    <CompactHoursCard
      summary={formatBusinessHoursSummary(value, locale)}
      editLabel={txt("Editar", "Edit")}
      closeLabel={txt("Concluir", "Done")}
      testId="business-hours-editor"
      forceOpen={hasErrors}
    >
      <div className="divide-y divide-border/70">
        {BUSINESS_DAY_ORDER.map((day) => {
          const window = current[day];
          const isOpen = Boolean(window);
          const openError = errors[`${day}.open`];
          const closeError = errors[`${day}.close`];
          const rowError = errors[day];
          const errorMessage = rowError ?? openError ?? closeError;
          const rowInvalid = Boolean(errorMessage);
          const errorId = rowInvalid ? `business-hours-${day}-error` : undefined;
          const dayLabel = DAY_LABELS[day][locale];
          const dayFull = DAY_FULL[day][locale];

          return (
            <div
              key={day}
              className="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-2 py-2 first:pt-0 last:pb-0"
              data-testid={`business-hours-row-${day}`}
            >
              <div className="flex w-28 shrink-0 items-center gap-2.5">
                <Switch
                  aria-label={txt(
                    `Abrir ${dayFull}`,
                    `Open on ${dayFull}`,
                  )}
                  checked={isOpen}
                  onCheckedChange={(checked) => toggleDay(day, checked)}
                  disabled={disabled}
                  data-testid={`business-hours-switch-${day}`}
                />
                <Label className="w-8 text-sm font-medium">{dayLabel}</Label>
              </div>
              {isOpen ? (
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  <Input
                    id={`business-hours-${day}-open`}
                    type="time"
                    aria-label={txt(
                      `Hora de abertura em ${dayFull}`,
                      `Opening time on ${dayFull}`,
                    )}
                    aria-invalid={rowInvalid || undefined}
                    aria-describedby={errorId}
                    value={window?.open ?? ""}
                    onChange={(e) =>
                      isOpen &&
                      setDay(day, {
                        open: e.target.value,
                        close: window?.close ?? "",
                      })
                    }
                    disabled={disabled || !isOpen}
                    className={`h-8 min-w-0 max-w-32 ${rowInvalid ? "border-red-500" : ""}`}
                    data-testid={`business-hours-open-${day}`}
                  />
                  <span className="text-xs text-muted-foreground" aria-hidden="true">–</span>
                  <Input
                    id={`business-hours-${day}-close`}
                    type="time"
                    aria-label={txt(
                      `Hora de fechamento em ${dayFull}`,
                      `Closing time on ${dayFull}`,
                    )}
                    aria-invalid={rowInvalid || undefined}
                    aria-describedby={errorId}
                    value={window?.close ?? ""}
                    onChange={(e) =>
                      isOpen &&
                      setDay(day, {
                        open: window?.open ?? "",
                        close: e.target.value,
                      })
                    }
                    disabled={disabled || !isOpen}
                    className={`h-8 min-w-0 max-w-32 ${rowInvalid ? "border-red-500" : ""}`}
                    data-testid={`business-hours-close-${day}`}
                  />
                </div>
              ) : (
                <span className="flex-1 text-sm text-muted-foreground">
                  {txt("Fechado", "Closed")}
                </span>
              )}
              {rowInvalid && (
                <p
                  id={errorId}
                  role="alert"
                  className="text-xs text-red-400"
                  data-testid={`business-hours-error-${day}`}
                >
                  {errorMessage}
                </p>
              )}
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-xs text-muted-foreground" data-testid="business-hours-metric-note">
        {txt(
          "A métrica de atendimentos fora do horário é derivada automaticamente deste calendário e do fuso horário da clínica.",
          "The outside-hours metric is derived automatically from this schedule and the clinic's timezone.",
        )}
      </p>
    </CompactHoursCard>
  );
}
