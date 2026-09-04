import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import {
  BusinessHoursEditor,
  type BusinessHoursMap,
} from "./business-hours-editor";

afterEach(() => {
  cleanup();
});

describe("<BusinessHoursEditor />", () => {
  it("renders 7 rows with open/closed switches and disabled times on closed days", () => {
    const value: BusinessHoursMap = {
      mon: { open: "09:00", close: "18:00" },
    };
    render(
      <BusinessHoursEditor
        value={value}
        onChange={() => undefined}
        errors={{}}
        disabled={false}
      />,
    );

    for (const day of ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) {
      expect(screen.getByTestId(`business-hours-row-${day}`)).toBeInTheDocument();
      expect(screen.getByTestId(`business-hours-switch-${day}`)).toBeInTheDocument();
      expect(screen.getByTestId(`business-hours-open-${day}`)).toBeInTheDocument();
      expect(screen.getByTestId(`business-hours-close-${day}`)).toBeInTheDocument();
    }

    // Monday is open — inputs enabled and prefilled
    expect(screen.getByTestId("business-hours-open-mon")).toHaveValue("09:00");
    expect(screen.getByTestId("business-hours-open-mon")).not.toBeDisabled();
    expect(screen.getByTestId("business-hours-close-mon")).toHaveValue("18:00");
    expect(screen.getByTestId("business-hours-close-mon")).not.toBeDisabled();

    // Tuesday is closed — inputs disabled and empty
    expect(screen.getByTestId("business-hours-open-tue")).toBeDisabled();
    expect(screen.getByTestId("business-hours-open-tue")).toHaveValue("");
    expect(screen.getByTestId("business-hours-close-tue")).toBeDisabled();
  });

  it("closing a day removes its window from the emitted value", () => {
    const onChange = vi.fn();
    const value: BusinessHoursMap = {
      mon: { open: "09:00", close: "18:00" },
      tue: { open: "10:00", close: "17:00" },
    };
    render(
      <BusinessHoursEditor
        value={value}
        onChange={onChange}
        errors={{}}
        disabled={false}
      />,
    );

    fireEvent.click(screen.getByTestId("business-hours-switch-tue"));

    expect(onChange).toHaveBeenCalledTimes(1);
    const emitted = onChange.mock.calls[0][0];
    expect(emitted).toEqual({ mon: { open: "09:00", close: "18:00" } });
    expect(emitted).not.toHaveProperty("tue");
  });

  it("closing the last remaining open day emits null", () => {
    const onChange = vi.fn();
    const value: BusinessHoursMap = {
      mon: { open: "09:00", close: "18:00" },
    };
    render(
      <BusinessHoursEditor
        value={value}
        onChange={onChange}
        errors={{}}
        disabled={false}
      />,
    );

    fireEvent.click(screen.getByTestId("business-hours-switch-mon"));

    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("surfaces a per-day error with aria-invalid on both time inputs", () => {
    const value: BusinessHoursMap = {
      mon: { open: "18:00", close: "09:00" },
    };
    render(
      <BusinessHoursEditor
        value={value}
        onChange={() => undefined}
        errors={{ mon: "Fechamento deve ser posterior à abertura." }}
        disabled={false}
      />,
    );

    const row = screen.getByTestId("business-hours-row-mon");
    const errorNode = within(row).getByTestId("business-hours-error-mon");
    expect(errorNode).toHaveAttribute("role", "alert");
    expect(errorNode.textContent).toMatch(/Fechamento/);

    const openInput = within(row).getByTestId("business-hours-open-mon");
    const closeInput = within(row).getByTestId("business-hours-close-mon");
    expect(openInput).toHaveAttribute("aria-invalid", "true");
    expect(closeInput).toHaveAttribute("aria-invalid", "true");
    expect(openInput.getAttribute("aria-describedby")).toBe(errorNode.id);
    expect(closeInput.getAttribute("aria-describedby")).toBe(errorNode.id);
  });

  it("changing one day's time does not touch other days in the emitted value", () => {
    const onChange = vi.fn();
    const value: BusinessHoursMap = {
      mon: { open: "09:00", close: "18:00" },
      wed: { open: "10:00", close: "17:00" },
      fri: { open: "08:00", close: "12:00" },
    };
    render(
      <BusinessHoursEditor
        value={value}
        onChange={onChange}
        errors={{}}
        disabled={false}
      />,
    );

    const monOpen = screen.getByTestId("business-hours-open-mon");
    fireEvent.change(monOpen, { target: { value: "10:30" } });

    expect(onChange).toHaveBeenCalledTimes(1);
    const emitted = onChange.mock.calls[0][0];
    expect(emitted).toEqual({
      mon: { open: "10:30", close: "18:00" },
      wed: { open: "10:00", close: "17:00" },
      fri: { open: "08:00", close: "12:00" },
    });
  });

  it("displays the static outside-hours metric note", () => {
    render(
      <BusinessHoursEditor
        value={null}
        onChange={() => undefined}
        errors={{}}
        disabled={false}
      />,
    );
    expect(screen.getByTestId("business-hours-metric-note")).toBeInTheDocument();
  });
});
