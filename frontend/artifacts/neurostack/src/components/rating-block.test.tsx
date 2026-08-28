import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

// The component uses the workspace API client for the actual POST and the
// shared toast hook for error surfacing. These tests focus on the *gating*
// behavior (when the reason box shows, when submit enables), not on the
// network call, so both dependencies are stubbed with inert implementations.
vi.mock("@workspace/api-client-react", () => ({
  customFetch: vi.fn(async () => ({ ok: true })),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

import { RatingBlock } from "./rating-block";

const MSG_ID = "msg-under-test";

afterEach(() => {
  cleanup();
});

function renderBlock() {
  return render(
    <RatingBlock
      msgId={MSG_ID}
      sessionId="session-1"
      agentResponse="ok"
      userMessage="pergunta"
    />,
  );
}

describe("<RatingBlock /> reason gating", () => {
  it("opens the reason textarea when rating 3 is selected", () => {
    renderBlock();

    fireEvent.click(screen.getByTestId(`rating-btn-3-${MSG_ID}`));

    expect(screen.getByTestId(`rating-reason-${MSG_ID}`)).toBeInTheDocument();
  });

  it("does not open the reason textarea when rating 4 is selected", () => {
    renderBlock();

    fireEvent.click(screen.getByTestId(`rating-btn-4-${MSG_ID}`));

    expect(
      screen.queryByTestId(`rating-reason-${MSG_ID}`),
    ).not.toBeInTheDocument();
  });

  it("keeps submit disabled with a 7-character reason and enables it at 8", () => {
    renderBlock();

    fireEvent.click(screen.getByTestId(`rating-btn-3-${MSG_ID}`));
    const reason = screen.getByTestId(`rating-reason-${MSG_ID}`);
    const submit = screen.getByTestId(`rating-submit-${MSG_ID}`);

    // Below the 8-char floor: submit stays disabled.
    fireEvent.change(reason, { target: { value: "1234567" } });
    expect(submit).toBeDisabled();

    // At the 8-char floor: submit enables.
    fireEvent.change(reason, { target: { value: "12345678" } });
    expect(submit).not.toBeDisabled();

    // Whitespace does not count toward the floor: 7 non-space chars + padding
    // still fails the threshold after trimming.
    fireEvent.change(reason, { target: { value: "   abcd   " } });
    expect(submit).toBeDisabled();
  });
});
