import { CalendarRange } from "lucide-react";
import { format } from "date-fns";

// -- Date Range Picker ---

export function DateRangePicker({
  startDate, endDate,
  onStartChange, onEndChange,
}: {
  startDate: string; endDate: string;
  onStartChange: (v: string) => void; onEndChange: (v: string) => void;
}) {
  const today = format(new Date(), "yyyy-MM-dd");
  return (
    <div className="flex flex-col sm:flex-row items-end gap-2 shrink-0">
      <div className="space-y-1">
        <label className="text-[11px] font-medium text-muted-foreground flex items-center gap-1">
          <CalendarRange className="h-3 w-3" /> Data Inicial
        </label>
        <input
          type="date"
          value={startDate}
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
          max={today}
          onChange={e => onEndChange(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-accent h-9 w-36"
        />
      </div>
    </div>
  );
}
