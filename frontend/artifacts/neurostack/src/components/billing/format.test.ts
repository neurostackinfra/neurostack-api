import { describe, expect, it } from "vitest";

import { formatCivilDate, formatCount, formatCredits, formatInstant, formatMoney, formatNumber } from "./format";

const nbsp = (value: string | null) => value?.replace(/ /g, " ") ?? null;

describe("billing formatters", () => {
  it("formats money from integer cents in each locale, zero included", () => {
    expect(nbsp(formatMoney(123_456, "BRL", "pt"))).toBe("R$ 1.234,56");
    expect(nbsp(formatMoney(123_456, "BRL", "en"))).toBe("R$1,234.56");
    expect(nbsp(formatMoney(0, "BRL", "pt"))).toBe("R$ 0,00");
  });

  it("returns null for an invalid currency or a non-finite amount instead of guessing", () => {
    expect(formatMoney(1_000, "not-a-currency", "pt")).toBeNull();
    expect(formatMoney(Number.NaN, "BRL", "pt")).toBeNull();
    expect(formatMoney(Number.POSITIVE_INFINITY, "BRL", "en")).toBeNull();
  });

  it("formats counts and numbers per locale and keeps zero as a value", () => {
    expect(formatCount(70_000, "pt")).toBe("70.000");
    expect(formatCount(70_000, "en")).toBe("70,000");
    expect(formatCount(0, "pt")).toBe("0");
    expect(formatNumber(12.345, "pt", 2)).toBe("12,35");
    expect(formatNumber(12.345, "en", 1)).toBe("12.3");
    expect(formatNumber(0, "en", 2)).toBe("0");
    expect(formatNumber(4.6, "pt")).toBe("5");
  });

  it("shows a civil date as written, without a time-zone shift", () => {
    // `new Date("2026-12-01")` is UTC midnight, the 30th in São Paulo; the
    // regex parse keeps the calendar day whatever the runtime zone.
    expect(formatCivilDate("2026-12-01", "pt")).toBe("01/12/2026");
    expect(formatCivilDate("2026-12-01", "en")).toBe("12/01/2026");
    expect(formatCivilDate("2027-01-01", "pt")).toBe("01/01/2027");
  });

  it("formats integer millicredits as credits, zero included", () => {
    expect(formatCredits(1_500, "pt")).toBe("1,5");
    expect(formatCredits(496_000, "en")).toBe("496");
    expect(formatCredits(1, "pt")).toBe("0,001");
    expect(formatCredits(0, "pt")).toBe("0");
    expect(formatCredits(2_000_000, "pt")).toBe("2.000");
  });

  it("shows a UTC instant in America/Sao_Paulo, with or without the time", () => {
    // 02:30Z on Nov 1st is still Oct 31st in São Paulo (UTC-3).
    expect(formatInstant("2026-11-01T02:30:00Z", "pt")).toBe("31/10/2026, 23:30");
    expect(formatInstant("2026-11-01T02:30:00Z", "pt", { withTime: false })).toBe("31/10/2026");
    expect(formatInstant("2026-11-01T02:30:00Z", "en", { withTime: false })).toBe("10/31/2026");
  });

  it.each([null, undefined, "", "soon"])("returns null for an absent or unparsable instant (%s)", (value) => {
    expect(formatInstant(value, "pt")).toBeNull();
  });

  it.each([null, undefined, "", "soon", "2026-12-01T00:00:00Z", "2026-13-01", "2026-00-10", "2026-12-32", "01/12/2026"])(
    "returns null for an absent or malformed civil date (%s), never the raw value",
    (value) => {
      expect(formatCivilDate(value, "pt")).toBeNull();
      expect(formatCivilDate(value, "en")).toBeNull();
    },
  );
});
