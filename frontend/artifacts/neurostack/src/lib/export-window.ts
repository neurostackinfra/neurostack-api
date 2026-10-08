import type { TranslationKey } from "@/locales/en";

/** Max inclusive civil dates per export file (the server enforces the same cap). */
export const EXPORT_MAX_DAYS = 60;

function civilDayNumber(value: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  const day = new Date(0);
  day.setUTCFullYear(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return Math.round(day.getTime() / 86_400_000);
}

/**
 * Inclusive civil days between two `yyyy-MM-dd` or `yyyy-MM-ddTHH:mm` values.
 * Only the date part counts, like the server does for naive datetimes, so a
 * 23:30 end time never adds a day. Null when either value is missing.
 */
export function inclusiveCivilDays(start: string, end: string): number | null {
  const first = civilDayNumber(start);
  const last = civilDayNumber(end);
  if (first === null || last === null) return null;
  return last - first + 1;
}

export function exceedsExportWindow(start: string, end: string): boolean {
  const days = inclusiveCivilDays(start, end);
  return days !== null && days > EXPORT_MAX_DAYS;
}

/** Translation key for a 400 `detail` from the audit export window policy. */
export function exportWindowErrorKey(detail: unknown): TranslationKey | null {
  if (detail === "export_window_60_days") return "audit_export_error_window_60_days";
  if (detail === "export_lookback_6_months") return "audit_export_error_lookback_6_months";
  return null;
}
