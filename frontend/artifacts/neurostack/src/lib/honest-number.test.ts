import { describe, expect, it } from "vitest";

import { MIN_SAMPLE_FOR_PERCENT, formatShare } from "./honest-number";

describe("formatShare", () => {
  it("sets the minimum sample for a percentage at 20", () => {
    expect(MIN_SAMPLE_FOR_PERCENT).toBe(20);
  });

  it.each(["pt", "en"])("rounds exact halves up in integer arithmetic (%s)", (locale) => {
    expect(formatShare(29, 200, locale)?.percent).toBe("15%");
    expect(formatShare(57, 200, locale)?.percent).toBe("29%");
    expect(formatShare(87, 200, locale)?.percent).toBe("44%");
  });

  it.each(["pt", "en"])("returns null when N is 0 (%s)", (locale) => {
    expect(formatShare(0, 0, locale)).toBeNull();
    expect(formatShare(3, 0, locale)).toBeNull();
  });

  it("gives only the fraction for N from 1 to 19", () => {
    expect(formatShare(3, 10, "pt")).toEqual({ percent: null, fraction: "3 de 10" });
    expect(formatShare(3, 10, "en")).toEqual({ percent: null, fraction: "3 of 10" });
    expect(formatShare(1, 1, "pt")).toEqual({ percent: null, fraction: "1 de 1" });
    expect(formatShare(19, 19, "en")).toEqual({ percent: null, fraction: "19 of 19" });
  });

  it("gives a whole percentage and the fraction from N = 20", () => {
    expect(formatShare(30, 100, "pt")).toEqual({ percent: "30%", fraction: "30 de 100" });
    expect(formatShare(30, 100, "en")).toEqual({ percent: "30%", fraction: "30 of 100" });
    expect(formatShare(6, 20, "pt")).toEqual({ percent: "30%", fraction: "6 de 20" });
    expect(formatShare(1, 3 * 7, "en")?.percent).toBe("5%");
  });

  it.each(["pt", "en"])("never rounds a partial share to 0%% or 100%% (%s)", (locale) => {
    expect(formatShare(1, 300, locale)?.percent).toBe("<1%");
    expect(formatShare(299, 300, locale)?.percent).toBe(">99%");
  });

  it.each(["pt", "en"])("keeps exact 0%% and 100%% (%s)", (locale) => {
    expect(formatShare(0, 25, locale)?.percent).toBe("0%");
    expect(formatShare(25, 25, locale)?.percent).toBe("100%");
  });

  it("groups digits per locale", () => {
    expect(formatShare(1240, 2000, "pt")?.fraction).toBe("1.240 de 2.000");
    expect(formatShare(1240, 2000, "en")?.fraction).toBe("1,240 of 2,000");
    expect(formatShare(1240, 2000, "pt")?.percent).toBe("62%");
  });
});
