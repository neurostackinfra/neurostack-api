/**
 * Formatting shared by the payment screen's presentational components. Every
 * function either renders the contract's value (zero included) or returns
 * `null`, so the caller shows its explicit "unavailable" copy instead of a
 * raw, guessed or empty value.
 */

export type Locale = "pt" | "en";
export type Txt = (pt: string, en: string) => string;

function intlLocale(locale: Locale): string {
  return locale === "en" ? "en-US" : "pt-BR";
}

/** A number in the UI locale; zero renders as `0`. */
export function formatNumber(value: number, locale: Locale, maximumFractionDigits = 0): string {
  return value.toLocaleString(intlLocale(locale), { maximumFractionDigits });
}

/** An integer count (credits, clinics) in the UI locale. */
export function formatCount(value: number, locale: Locale): string {
  return formatNumber(value, locale, 0);
}

/**
 * Money from integer cents and the contract's ISO currency. `null` when the
 * amount is not a finite number or the currency is not a valid ISO code: the
 * caller shows "unavailable" instead of a guessed amount or currency.
 */
export function formatMoney(cents: number, currency: string, locale: Locale): string | null {
  if (!Number.isFinite(cents)) return null;
  try {
    return new Intl.NumberFormat(intlLocale(locale), { style: "currency", currency }).format(cents / 100);
  } catch {
    return null;
  }
}

/**
 * Millicredits per credit. The credit API returns every amount as integer
 * millicredits (`*_mc`) and fixes 1 credit = 1,000 mc (`src/api/schemas/credits.py`
 * module docstring, `CreditCatalogResponse.millicredits_per_credit`,
 * `docs/billing/credits.md` "Units"); the UI divides only to display.
 */
export const MILLICREDITS_PER_CREDIT = 1000;

/** Integer millicredits shown as credits (up to three decimals); zero is `0`. */
export function formatCredits(mc: number, locale: Locale): string {
  return (mc / MILLICREDITS_PER_CREDIT).toLocaleString(intlLocale(locale), {
    maximumFractionDigits: 3,
  });
}

/**
 * A UTC instant of the credit API shown in America/Sao_Paulo (the accounting
 * zone of `docs/billing/credits.md`). `null` for an absent or unparsable value.
 */
export function formatInstant(
  value: string | null | undefined,
  locale: Locale,
  { withTime = true }: { withTime?: boolean } = {},
): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(intlLocale(locale), {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
    timeZone: "America/Sao_Paulo",
  }).format(date);
}

const CIVIL_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * A `YYYY-MM-DD` calendar date shown as such. It is parsed with a regular
 * expression, never with `new Date("YYYY-MM-DD")` (UTC midnight, which shifts
 * a day in America/Sao_Paulo). `null` for an absent or malformed value.
 */
export function formatCivilDate(value: string | null | undefined, locale: Locale): string | null {
  const match = typeof value === "string" ? CIVIL_DATE.exec(value) : null;
  if (!match) return null;
  const [, year, month, day] = match;
  if (Number(month) < 1 || Number(month) > 12 || Number(day) < 1 || Number(day) > 31) return null;
  return locale === "en" ? `${month}/${day}/${year}` : `${day}/${month}/${year}`;
}
