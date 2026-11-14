import { CalendarRange } from "lucide-react";
import { addDays, format, isValid, parseISO, subDays } from "date-fns";

// -- Date Range Picker ---

const DATE_FORMAT = "yyyy-MM-dd";

/**
 * Optional `maxDays` caps the span at that many calendar days: the end gets
 * `max = min(today, start + maxDays)` and the start gets `min = end - maxDays`.
 * Each cap is derived only from a valid date (a cleared input is ""), and the
 * dates are parsed in local time with `parseISO`, never `new Date("yyyy-MM-dd")`.
 */
export function DateRangePicker({
  startDate, endDate,
  onStartChange, onEndChange,
  maxDays,
}: {
  startDate: string; endDate: string;
  onStartChange: (v: string) => void; onEndChange: (v: string) => void;
  maxDays?: number;
}) {
  const today = format(new Date(), DATE_FORMAT);
  let endMax = today;
  let startMin: string | undefined;
  if (maxDays !== undefined) {
    const start = parseISO(startDate);
    const end = parseISO(endDate);
    if (isValid(start)) {
      const cap = format(addDays(start, maxDays), DATE_FORMAT);
      if (cap < endMax) endMax = cap;
    }
    if (isValid(end)) startMin = format(subDays(end, maxDays), DATE_FORMAT);
  }
  return (
    <div className="flex flex-col sm:flex-row items-end gap-2 shrink-0">
      <div className="space-y-1">
        <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
          <CalendarRange className="h-3 w-3" /> Data Inicial
        </label>
        <input
          type="date"
          value={startDate}
          min={startMin}
          max={endDate}
          onChange={e => onStartChange(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent h-9 w-36"
        />
      </div>
      <div className="space-y-1">
        <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
          <CalendarRange className="h-3 w-3" /> Data Final
        </label>
        <input
          type="date"
          value={endDate}
          min={startDate}
          max={endMax}
          onChange={e => onEndChange(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent h-9 w-36"
        />
      </div>
    </div>
  );
}
