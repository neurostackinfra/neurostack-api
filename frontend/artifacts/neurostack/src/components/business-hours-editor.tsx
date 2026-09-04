import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

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
    <div
      className="space-y-3 rounded-md border border-border bg-muted/10 p-3"
      data-testid="business-hours-editor"
    >
      <div className="space-y-2">
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
              className="flex flex-col gap-2 rounded-md border border-border bg-background p-3 sm:flex-row sm:items-center"
              data-testid={`business-hours-row-${day}`}
            >
              <div className="flex items-center gap-3 sm:w-40">
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
                <Label className="text-sm font-medium">{dayLabel}</Label>
                <span className="text-xs text-muted-foreground">
                  {isOpen ? txt("Aberto", "Open") : txt("Fechado", "Closed")}
                </span>
              </div>
              <div className="grid flex-1 grid-cols-2 gap-2">
                <div>
                  <Label
                    className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground"
                    htmlFor={`business-hours-${day}-open`}
                  >
                    {txt("Abertura", "Opens")}
                  </Label>
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
                    className={rowInvalid ? "border-red-500" : ""}
                    data-testid={`business-hours-open-${day}`}
                  />
                </div>
                <div>
                  <Label
                    className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground"
                    htmlFor={`business-hours-${day}-close`}
                  >
                    {txt("Fechamento", "Closes")}
                  </Label>
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
                    className={rowInvalid ? "border-red-500" : ""}
                    data-testid={`business-hours-close-${day}`}
                  />
                </div>
              </div>
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
      <p className="text-xs text-muted-foreground" data-testid="business-hours-metric-note">
        {txt(
          "A métrica de atendimentos fora do horário é derivada automaticamente deste calendário e do fuso horário da clínica — sem opção de desligar.",
          "The outside-hours metric is derived automatically from this schedule and the clinic's timezone — there is no opt-in.",
        )}
      </p>
    </div>
  );
}
