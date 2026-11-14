import type { ComponentProps } from "react";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OutsideHoursClinicMetric } from "./builder";

type Clinic = ComponentProps<typeof OutsideHoursClinicMetric>["clinic"];

const baseClinic: Clinic = {
  id: "clinic-1",
  name: "Clinic One",
  slug: null,
  status: "active",
  plan_type: "clinic",
  is_active: true,
  created_at: null,
  last_trace_at: null,
  health_status: "healthy",
  trace_count_7d: 0,
  safety_flags_7d: 0,
  handoff_count_7d: 0,
  outside_hours_count_7d: 0,
  outside_hours_pct_7d: null,
  conversational_trace_count_7d: 0,
  business_hours_configured: true,
  config_completeness: 0,
  contact_phone: null,
  admin_email: null,
};

function renderMetric(overrides: Partial<Clinic>, locale: string) {
  render(<OutsideHoursClinicMetric clinic={{ ...baseClinic, ...overrides }} locale={locale} />);
  return screen.getByTestId(`clinic-outside-hours-${overrides.id ?? baseClinic.id}`);
}

describe("OutsideHoursClinicMetric", () => {
  it.each([
    ["pt", "3 de 10"],
    ["en", "3 of 10"],
  ])("shows a fraction below the minimum sample (%s)", (locale, big) => {
    const el = renderMetric({ outside_hours_count_7d: 3, conversational_trace_count_7d: 10 }, locale);
    expect(within(el).getByText(big)).toBeTruthy();
    expect(within(el).getByText("3 / 10")).toBeTruthy();
  });

  it.each(["pt", "en"])("shows a whole percentage from counts (%s)", (locale) => {
    const el = renderMetric({ outside_hours_count_7d: 30, conversational_trace_count_7d: 100 }, locale);
    expect(within(el).getByText("30%")).toBeTruthy();
    expect(within(el).getByText("30 / 100")).toBeTruthy();
  });

  it("rounds exact halves up (29 of 200 is 15%)", () => {
    const el = renderMetric({ outside_hours_count_7d: 29, conversational_trace_count_7d: 200, outside_hours_pct_7d: 14.5 }, "pt");
    expect(within(el).getByText("15%")).toBeTruthy();
    expect(within(el).queryByText("14%")).toBeNull();
  });

  it.each([
    ["pt", "Não configurado"],
    ["en", "Not configured"],
  ])("shows not configured regardless of counts (%s)", (locale, label) => {
    const el = renderMetric(
      { business_hours_configured: false, outside_hours_count_7d: 30, conversational_trace_count_7d: 100, outside_hours_pct_7d: 30 },
      locale,
    );
    expect(within(el).getByText(label)).toBeTruthy();
    expect(within(el).queryByText("30 / 100")).toBeNull();
    expect(within(el).queryByText("30%")).toBeNull();
  });

  it.each([
    ["pt", "Sem dados"],
    ["en", "No data"],
  ])("shows no data when the total is zero (%s)", (locale, label) => {
    const el = renderMetric({ conversational_trace_count_7d: 0 }, locale);
    expect(within(el).getByText(label)).toBeTruthy();
  });

  it("ignores the server percentage and computes from counts", () => {
    const el = renderMetric({ outside_hours_count_7d: 30, conversational_trace_count_7d: 100, outside_hours_pct_7d: 99 }, "en");
    expect(within(el).getByText("30%")).toBeTruthy();
    expect(within(el).queryByText("99%")).toBeNull();
  });

  it("keeps the per-clinic test id", () => {
    renderMetric({ id: "abc-42", outside_hours_count_7d: 1, conversational_trace_count_7d: 2 }, "pt");
    expect(screen.getByTestId("clinic-outside-hours-abc-42")).toBeTruthy();
  });
});
