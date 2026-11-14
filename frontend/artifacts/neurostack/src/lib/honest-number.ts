// -- Honest-number rule, shared by Consultoria and the Dashboard ---
//
// A share is always computed from counts, never read ready-made from a payload.
// Below MIN_SAMPLE_FOR_PERCENT the percentage is withheld and only "X de N" is
// shown, so one message out of one never reads as "100%".

export const MIN_SAMPLE_FOR_PERCENT = 20;

export interface Share {
  /** Whole-number percentage ("30%", "<1%", ">99%"), or null when N < MIN_SAMPLE_FOR_PERCENT. */
  percent: string | null;
  /** "X de N" (pt) / "X of N" (en), with pt-BR / en-US digit grouping. */
  fraction: string;
}

export function formatShare(x: number, n: number, locale: string): Share | null {
  if (n <= 0) return null;
  const numberLocale = locale === "en" ? "en-US" : "pt-BR";
  const fraction = `${x.toLocaleString(numberLocale)} ${locale === "en" ? "of" : "de"} ${n.toLocaleString(numberLocale)}`;
  if (n < MIN_SAMPLE_FOR_PERCENT) return { percent: null, fraction };

  // Multiply before dividing: (x / n) * 100 loses exact halves (29 of 200 -> 14).
  const rounded = Math.round((x * 100) / n);
  const partial = x > 0 && x < n;
  let percent = `${rounded}%`;
  if (partial && rounded === 0) percent = "<1%";
  else if (partial && rounded === 100) percent = ">99%";
  return { percent, fraction };
}
