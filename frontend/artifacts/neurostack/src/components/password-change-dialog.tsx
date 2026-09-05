import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface PasswordChangePayload {
  new_password: string;
  confirm_password: string;
}

interface PasswordChangeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  target?: string;
  newPasswordLabel: string;
  confirmPasswordLabel: string;
  submitLabel: string;
  cancelLabel: string;
  mismatchMessage: string;
  errorMessage: string;
  loadingLabel: string;
  onSubmit: (payload: PasswordChangePayload) => Promise<boolean>;
}

export function PasswordChangeDialog({
  open,
  onOpenChange,
  title,
  description,
  target,
  newPasswordLabel,
  confirmPasswordLabel,
  submitLabel,
  cancelLabel,
  mismatchMessage,
  errorMessage,
  loadingLabel,
  onSubmit,
}: PasswordChangeDialogProps) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function clear() {
    setNewPassword("");
    setConfirmPassword("");
    setError("");
    setSubmitting(false);
  }

  useEffect(() => {
    if (!open) clear();
  }, [open]);

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) clear();
    onOpenChange(nextOpen);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    if (newPassword !== confirmPassword) {
      setError(mismatchMessage);
      return;
    }

    setSubmitting(true);
    setError("");
    try {
      const succeeded = await onSubmit({ new_password: newPassword, confirm_password: confirmPassword });
      if (succeeded) {
        clear();
        onOpenChange(false);
      } else {
        setError(errorMessage);
      }
    } catch {
      setError(errorMessage);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
          {target && <p className="text-sm font-medium" data-testid="password-change-target">{target}</p>}
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="new-password">{newPasswordLabel}</Label>
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              aria-describedby={error ? "password-change-error" : undefined}
              disabled={submitting}
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="confirm-password">{confirmPasswordLabel}</Label>
            <Input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              aria-describedby={error ? "password-change-error" : undefined}
              disabled={submitting}
              required
            />
          </div>
          {error && <p id="password-change-error" role="alert" className="text-sm text-destructive">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => handleOpenChange(false)} disabled={submitting}>{cancelLabel}</Button>
            <Button type="submit" disabled={submitting}>{submitting ? loadingLabel : submitLabel}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
