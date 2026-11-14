import { useState } from "react";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DateRangePicker } from "./date-range-picker";

// Local 2026-05-01: "today" sits after 2026-04-01, so min(today, start + 90)
// cannot mask the 90-day boundary.
const TODAY = "2026-05-01";

beforeEach(() => {
  vi.setSystemTime(new Date(2026, 4, 1));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function inputs(container: HTMLElement) {
  const [start, end] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="date"]'));
  return { start, end };
}

function renderPicker(startDate: string, endDate: string, maxDays?: number) {
  return render(
    <DateRangePicker
      startDate={startDate}
      endDate={endDate}
      onStartChange={vi.fn()}
      onEndChange={vi.fn()}
      maxDays={maxDays}
    />,
  );
}

function ControlledPicker({ maxDays }: { maxDays?: number }) {
  const [startDate, setStartDate] = useState("2026-01-01");
  const [endDate, setEndDate] = useState("2026-03-01");
  return (
    <DateRangePicker
      startDate={startDate}
      endDate={endDate}
      onStartChange={setStartDate}
      onEndChange={setEndDate}
      maxDays={maxDays}
    />
  );
}

describe("<DateRangePicker /> maxDays", () => {
  it("caps the end at start + 90 calendar days", () => {
    const { container } = renderPicker("2026-01-01", "2026-02-01", 90);
    expect(inputs(container).end).toHaveAttribute("max", "2026-04-01");
    expect(inputs(container).end).toHaveAttribute("min", "2026-01-01");
  });

  it("floors the start at end - 90 calendar days", () => {
    const { container } = renderPicker("2026-02-01", "2026-04-01", 90);
    expect(inputs(container).start).toHaveAttribute("min", "2026-01-01");
    expect(inputs(container).start).toHaveAttribute("max", "2026-04-01");
  });

  it("keeps today as the end cap when start + 90 is later", () => {
    const { container } = renderPicker("2026-04-20", TODAY, 90);
    expect(inputs(container).end).toHaveAttribute("max", TODAY);
  });

  it("keeps today's attributes without the prop", () => {
    const { container } = renderPicker("2026-01-01", "2026-04-01");
    const { start, end } = inputs(container);
    expect(start).not.toHaveAttribute("min");
    expect(start).toHaveAttribute("max", "2026-04-01");
    expect(end).toHaveAttribute("min", "2026-01-01");
    expect(end).toHaveAttribute("max", TODAY);
  });

  it("survives clearing the start: the end cap falls back to today", () => {
    const { container } = render(<ControlledPicker maxDays={90} />);
    expect(() => fireEvent.change(inputs(container).start, { target: { value: "" } })).not.toThrow();
    const { start, end } = inputs(container);
    expect(start).toHaveValue("");
    expect(end).toHaveAttribute("max", TODAY);
    expect(start).toHaveAttribute("min", "2025-12-01");
  });

  it("survives clearing the end: the start has no min", () => {
    const { container } = render(<ControlledPicker maxDays={90} />);
    expect(() => fireEvent.change(inputs(container).end, { target: { value: "" } })).not.toThrow();
    const { start, end } = inputs(container);
    expect(end).toHaveValue("");
    expect(start).not.toHaveAttribute("min");
    expect(end).toHaveAttribute("max", "2026-04-01");
  });
});
