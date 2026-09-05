import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import {
  BusinessHoursEditor,
  formatBusinessHoursSummary,
  type BusinessHoursMap,
} from "./business-hours-editor";

afterEach(() => {
  cleanup();
});

describe("<BusinessHoursEditor />", () => {
  const expandEditor = () => {
    fireEvent.click(screen.getByTestId("business-hours-editor-toggle"));
  };

  it("starts compact and reveals 7 single-line day controls on demand", () => {
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

    expect(screen.getByTestId("business-hours-editor-summary")).toHaveTextContent(
      "Seg: 09:00–18:00 | Ter–Dom: fechado",
    );
    expect(screen.queryByTestId("business-hours-row-mon")).toBeNull();

    expandEditor();

    for (const day of ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]) {
      expect(screen.getByTestId(`business-hours-row-${day}`)).toBeInTheDocument();
      expect(screen.getByTestId(`business-hours-switch-${day}`)).toBeInTheDocument();
    }

    expect(screen.getByTestId("business-hours-open-mon")).toHaveValue("09:00");
    expect(screen.getByTestId("business-hours-open-mon")).not.toBeDisabled();
    expect(screen.getByTestId("business-hours-close-mon")).toHaveValue("18:00");
    expect(screen.getByTestId("business-hours-close-mon")).not.toBeDisabled();

    expect(screen.queryByTestId("business-hours-open-tue")).toBeNull();
    expect(screen.queryByTestId("business-hours-close-tue")).toBeNull();
    expect(screen.getByTestId("business-hours-row-tue")).toHaveTextContent("Fechado");
  });

  it("groups consecutive days with equal hours in the compact summary", () => {
    const value: BusinessHoursMap = {
      mon: { open: "09:00", close: "18:00" },
      tue: { open: "09:00", close: "18:00" },
      wed: { open: "09:00", close: "18:00" },
      thu: { open: "09:00", close: "18:00" },
      fri: { open: "09:00", close: "18:00" },
      sat: { open: "09:00", close: "13:00" },
    };

    expect(formatBusinessHoursSummary(value)).toBe(
      "Seg–Sex: 09:00–18:00 | Sáb: 09:00–13:00 | Dom: fechado",
    );
    expect(formatBusinessHoursSummary(null)).toBe("Nenhum horário configurado");
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

    expandEditor();
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

    expandEditor();
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

    // Validation errors force the editor open so the invalid fields are visible.
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

    expandEditor();
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
    expect(screen.queryByTestId("business-hours-metric-note")).toBeNull();
    expandEditor();
    expect(screen.getByTestId("business-hours-metric-note")).toBeInTheDocument();
  });
});
