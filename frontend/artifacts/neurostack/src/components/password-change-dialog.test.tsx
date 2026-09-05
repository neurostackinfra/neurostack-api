import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PasswordChangeDialog } from "./password-change-dialog";

function renderDialog(onSubmit = vi.fn().mockResolvedValue(true)) {
  const onOpenChange = vi.fn();
  render(
    <PasswordChangeDialog
      open
      onOpenChange={onOpenChange}
      title="Change password"
      description="Choose a new password"
      newPasswordLabel="New password"
      confirmPasswordLabel="Confirm new password"
      submitLabel="Save"
      cancelLabel="Cancel"
      loadingLabel="Saving"
      mismatchMessage="Passwords do not match"
      errorMessage="Request failed"
      onSubmit={onSubmit}
    />,
  );
  return { onSubmit, onOpenChange };
}

describe("PasswordChangeDialog", () => {
  it("uses accessible password fields and blocks a mismatch without a request", () => {
    const { onSubmit } = renderDialog();
    const fields = [
      screen.getByLabelText("New password"),
      screen.getByLabelText("Confirm new password"),
    ];
    expect(fields).toHaveLength(2);
    fields.forEach((field) =>
      expect(field).toHaveAttribute("autocomplete", "new-password"),
    );
    fireEvent.change(fields[0], { target: { value: "secret-1" } });
    fireEvent.change(fields[1], { target: { value: "secret-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Passwords do not match",
    );
  });

  it("sends only password fields, prevents duplicate submit, and clears after success", async () => {
    let resolve!: (value: boolean) => void;
    const onSubmit = vi.fn(
      () =>
        new Promise<boolean>((done) => {
          resolve = done;
        }),
    );
    const { onOpenChange } = renderDialog(onSubmit);
    const fields = [
      screen.getByLabelText("New password"),
      screen.getByLabelText("Confirm new password"),
    ];
    fireEvent.change(fields[0], { target: { value: "secret" } });
    fireEvent.change(fields[1], { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(screen.getByRole("button", { name: "Saving" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      new_password: "secret",
      confirm_password: "secret",
    });
    resolve(true);
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(fields[0]).toHaveValue("");
    expect(fields[1]).toHaveValue("");
  });

  it("keeps the dialog open and announces an HTTP failure", async () => {
    const { onOpenChange } = renderDialog(vi.fn().mockResolvedValue(false));
    const fields = [
      screen.getByLabelText("New password"),
      screen.getByLabelText("Confirm new password"),
    ];
    fireEvent.change(fields[0], { target: { value: "secret" } });
    fireEvent.change(fields[1], { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Request failed",
    );
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
