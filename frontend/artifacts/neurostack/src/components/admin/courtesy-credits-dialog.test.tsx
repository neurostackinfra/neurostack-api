import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "@/context/i18n";
import { CourtesyCreditsDialog } from "./courtesy-credits-dialog";

// DESIGN creditos-cortesia, Success Criteria "Diálogo 'Créditos'", and the
// idempotency rule of decision 10 (a key per attempt, reused on a retry).

const LIST_URL = "/v1/admin/credits/courtesy-grants?builder_id=b-1";
const GRANT_URL = "/v1/admin/credits/courtesy-grants";
const REASON = "Conta de teste do piloto";

const activeLot = {
  lot_id: "lot-1",
  kind: "courtesy",
  status: "active",
  granted_mc: 10_000_000,
  available_mc: 4_999_400,
  consumed_mc: 5_000_600,
  removed_mc: 0,
  expired_mc: 0,
  paid_at: "2026-11-09",
  expires_at: "2026-12-09T03:00:00Z",
};

const exhaustedLot = {
  ...activeLot,
  lot_id: "lot-2",
  status: "exhausted",
  available_mc: 0,
  consumed_mc: 4_000_000,
  removed_mc: 6_000_000,
};

type Reply = { ok: boolean; status: number; body: unknown } | "network" | "hang";

function response(reply: Exclude<Reply, "network" | "hang">) {
  return Promise.resolve({ ok: reply.ok, status: reply.status, json: () => Promise.resolve(reply.body) });
}

const created = { ok: true, status: 201, body: { lot_id: "lot-new" } } as const;

let postReplies: Reply[] = [];
/** Answers a `"hang"` POST (a request in flight) with a success. */
let releasePost: (() => void) | null = null;
/** When set, the next lots GET hangs until `releaseList` is called. */
let holdNextList = false;
let releaseList: (() => void) | null = null;
const fetchMock = vi.fn();

const ACTIVE_LOT_BUTTON = "Tirar créditos do lote que expira em 09/12/2026";

interface PostCall {
  url: string;
  key: string;
  body: Record<string, unknown>;
}

function postCalls(): PostCall[] {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")
    .map(([url, init]) => {
      const request = init as RequestInit;
      const headers = request.headers as Record<string, string>;
      return { url: String(url), key: headers["Idempotency-Key"], body: JSON.parse(String(request.body)) };
    });
}

function renderDialog(lots: unknown[] = [activeLot, exhaustedLot]) {
  fetchMock.mockImplementation((input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const reply = postReplies.shift() ?? created;
      if (reply === "network") return Promise.reject(new TypeError("Failed to fetch"));
      if (reply === "hang") {
        return new Promise((resolve) => {
          releasePost = () => resolve(response(created));
        });
      }
      return response(reply);
    }
    if (input === LIST_URL) {
      const reply = response({ ok: true, status: 200, body: { as_of: "2026-11-09T12:00:00Z", lots, events: [] } });
      if (!holdNextList) return reply;
      holdNextList = false;
      return new Promise((resolve) => {
        releaseList = () => resolve(reply);
      });
    }
    throw new Error(`Unexpected fetch: ${input}`);
  });
  const onChanged = vi.fn();
  const onOpenChange = vi.fn();
  render(
    <I18nProvider>
      <CourtesyCreditsDialog
        open
        onOpenChange={onOpenChange}
        builderId="b-1"
        builderName="Aurora Digital"
        token="admin-token"
        onChanged={onChanged}
      />
    </I18nProvider>,
  );
  return { onChanged, onOpenChange };
}

/** Waits for the first lots load, so no state update lands after the test. */
async function lotsLoaded() {
  const lots = within(await screen.findByRole("region", { name: "Lotes de cortesia" }));
  await lots.findByRole("table");
}

function grantSection() {
  return within(screen.getByRole("region", { name: "Dar cortesia" }));
}

function removalSection() {
  return within(screen.getByRole("region", { name: "Tirar créditos" }));
}

function fillGrant(credits: string, reason = REASON) {
  const grant = grantSection();
  fireEvent.change(grant.getByLabelText("Quantidade de créditos"), { target: { value: credits } });
  fireEvent.change(grant.getByLabelText("Motivo (obrigatório)"), { target: { value: reason } });
}

async function confirmGrant() {
  fireEvent.click(grantSection().getByRole("button", { name: "Continuar" }));
  fireEvent.click(await grantSection().findByRole("button", { name: /Confirmar|Tentar de novo/ }));
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("ns_locale", "pt");
  postReplies = [];
  releasePost = null;
  holdNextList = false;
  releaseList = null;
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CourtesyCreditsDialog: lots", () => {
  it("lists the builder's courtesy lots, with removal only on an active lot", async () => {
    renderDialog();
    const lots = within(await screen.findByRole("region", { name: "Lotes de cortesia" }));
    const rows = await lots.findAllByRole("row");
    expect(rows).toHaveLength(3);
    const active = within(rows[1]);
    expect(active.getByText("09/12/2026")).toBeInTheDocument();
    expect(active.getByText("10.000")).toBeInTheDocument();
    expect(active.getByText("4.999,4")).toBeInTheDocument();
    expect(active.getByRole("button", { name: ACTIVE_LOT_BUTTON })).toBeInTheDocument();
    const exhausted = within(rows[2]);
    expect(exhausted.getByText("Esgotado")).toBeInTheDocument();
    expect(exhausted.getByText("6.000")).toBeInTheDocument();
    expect(exhausted.queryByRole("button", { name: /Tirar créditos/ })).not.toBeInTheDocument();
  });

  it("shows a readable text, not the body, when the lots cannot be read", async () => {
    fetchMock.mockImplementation(() =>
      response({ ok: false, status: 503, body: { code: "credit_unavailable" } }),
    );
    render(
      <I18nProvider>
        <CourtesyCreditsDialog open onOpenChange={() => undefined} builderId="b-1" builderName="Aurora" token="t" />
      </I18nProvider>,
    );
    expect(await screen.findByText(/Os créditos estão indisponíveis no servidor agora/)).toBeInTheDocument();
    expect(screen.queryByText(/credit_unavailable/)).not.toBeInTheDocument();
  });
});

describe("CourtesyCreditsDialog: Dar cortesia", () => {
  it("fills the 5.000 / 15.000 / 35.000 shortcuts and shows a read-only 30 dias", async () => {
    renderDialog();
    await lotsLoaded();
    const grant = grantSection();
    const amount = grant.getByLabelText("Quantidade de créditos") as HTMLInputElement;
    for (const [label, value] of [["5.000", "5000"], ["15.000", "15000"], ["35.000", "35000"]]) {
      fireEvent.click(grant.getByRole("button", { name: label }));
      expect(amount.value).toBe(value);
    }
    const validity = grant.getByLabelText("Validade") as HTMLInputElement;
    expect(validity.value).toBe("30 dias");
    expect(validity).toHaveAttribute("readonly");
  });

  it("accepts 1 to 100.000 whole credits only, and never sends without a reason", async () => {
    renderDialog();
    await lotsLoaded();
    const grant = grantSection();
    const proceed = () => grant.getByRole("button", { name: "Continuar" });
    for (const invalid of ["0", "-5", "100001", "1.5", "abc"]) {
      fillGrant(invalid);
      expect(proceed()).toBeDisabled();
    }
    fillGrant("100000", "   ");
    expect(proceed()).toBeDisabled();
    fillGrant("100.000");
    expect(proceed()).toBeEnabled();
    fillGrant("1");
    expect(proceed()).toBeEnabled();
    expect(postCalls()).toHaveLength(0);
  });

  it("confirms by repeating the amount, then sends the body with an Idempotency-Key", async () => {
    const { onChanged } = renderDialog();
    fillGrant("15000");
    fireEvent.click(grantSection().getByRole("button", { name: "Continuar" }));
    expect(
      grantSection().getByText("Dar 15.000 créditos de cortesia a Aurora Digital, válidos por 30 dias?"),
    ).toBeInTheDocument();
    expect(postCalls()).toHaveLength(0);
    fireEvent.click(grantSection().getByRole("button", { name: "Confirmar" }));
    expect(await grantSection().findByText("Cortesia de 15.000 créditos concedida.")).toBeInTheDocument();
    const [call] = postCalls();
    expect(call.url).toBe(GRANT_URL);
    expect(call.body).toEqual({ builder_id: "b-1", credits: 15000, reason: REASON });
    expect(call.key).toMatch(/^.{1,200}$/);
    expect(onChanged).toHaveBeenCalledTimes(1);
    // The form is cleared after a success: the reason is not kept.
    expect((grantSection().getByLabelText("Motivo (obrigatório)") as HTMLTextAreaElement).value).toBe("");
  });

  it("reuses the same key on a retry after a network failure, and a new key for a new attempt", async () => {
    renderDialog();
    postReplies = ["network", created, created];
    fillGrant("5000");
    await confirmGrant();
    expect(await grantSection().findByText(/Falha de rede/)).toBeInTheDocument();
    // Same attempt: the retry sends the same key and the same body.
    fireEvent.click(grantSection().getByRole("button", { name: "Tentar de novo" }));
    expect(await grantSection().findByText("Cortesia de 5.000 créditos concedida.")).toBeInTheDocument();
    // A new attempt after the success.
    fillGrant("5000");
    await confirmGrant();
    await waitFor(() => expect(postCalls()).toHaveLength(3));
    const [first, retry, next] = postCalls();
    expect(retry.key).toBe(first.key);
    expect(retry.body).toEqual(first.body);
    expect(next.key).not.toBe(first.key);
  });

  it("gets a new key when the form is edited after a failure", async () => {
    renderDialog();
    postReplies = ["network", created];
    fillGrant("5000");
    await confirmGrant();
    expect(await grantSection().findByText(/Falha de rede/)).toBeInTheDocument();
    fireEvent.click(grantSection().getByRole("button", { name: "Voltar" }));
    fillGrant("6000");
    await confirmGrant();
    await waitFor(() => expect(postCalls()).toHaveLength(2));
    const [first, edited] = postCalls();
    expect(edited.body.credits).toBe(6000);
    expect(edited.key).not.toBe(first.key);
  });

  it("reuses the key when the same body is confirmed again after a failure and Voltar", async () => {
    // The server may have committed the first attempt: the same grant sent
    // again must carry the same key, or it would be a second courtesy.
    renderDialog();
    postReplies = ["network", created];
    const grant = grantSection();
    fireEvent.change(grant.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    fireEvent.click(grant.getByRole("button", { name: "5.000" }));
    await confirmGrant();
    expect(await grantSection().findByText(/Falha de rede/)).toBeInTheDocument();
    fireEvent.click(grantSection().getByRole("button", { name: "Voltar" }));
    fireEvent.click(grantSection().getByRole("button", { name: "5.000" }));
    await confirmGrant();
    expect(await grantSection().findByText("Cortesia de 5.000 créditos concedida.")).toBeInTheDocument();
    const [first, again] = postCalls();
    expect(again.body).toEqual(first.body);
    expect(again.key).toBe(first.key);
  });

  it("gets a new key on the next confirm after idempotency_key_reused", async () => {
    renderDialog();
    postReplies = [{ ok: false, status: 409, body: { code: "idempotency_key_reused" } }, created];
    fillGrant("5000");
    await confirmGrant();
    expect(await grantSection().findByRole("alert")).toHaveTextContent(/Esta tentativa já foi usada/);
    fireEvent.click(grantSection().getByRole("button", { name: "Confirmar" }));
    expect(await grantSection().findByText("Cortesia de 5.000 créditos concedida.")).toBeInTheDocument();
    const [first, next] = postCalls();
    expect(next.body).toEqual(first.body);
    expect(next.key).not.toBe(first.key);
  });

  it("treats a 5xx without a code as an unknown outcome, retried under the same key", async () => {
    renderDialog();
    postReplies = [{ ok: false, status: 504, body: null }, created];
    fillGrant("5000");
    await confirmGrant();
    expect(await grantSection().findByRole("alert")).toHaveTextContent(/não foi possível confirmar se o pedido chegou/);
    fireEvent.click(grantSection().getByRole("button", { name: "Tentar de novo" }));
    expect(await grantSection().findByText("Cortesia de 5.000 créditos concedida.")).toBeInTheDocument();
    const [first, retry] = postCalls();
    expect(retry.key).toBe(first.key);
  });

  it("shows the unknown-outcome text in English too", async () => {
    localStorage.setItem("ns_locale", "en");
    renderDialog();
    postReplies = [{ ok: false, status: 502, body: { detail: "Bad Gateway" } }];
    const grant = within(screen.getByRole("region", { name: "Grant courtesy" }));
    fireEvent.change(grant.getByLabelText("Credits"), { target: { value: "5000" } });
    fireEvent.change(grant.getByLabelText("Reason (required)"), { target: { value: REASON } });
    fireEvent.click(grant.getByRole("button", { name: "Continue" }));
    fireEvent.click(grant.getByRole("button", { name: "Confirm" }));
    expect(await grant.findByRole("alert")).toHaveTextContent("Network failure: the request could not be confirmed.");
    expect(grant.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("builds a v4 key from getRandomValues when randomUUID is missing (no secure context)", async () => {
    Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true, writable: true });
    try {
      renderDialog();
      fillGrant("5000");
      await confirmGrant();
      expect(await grantSection().findByText("Cortesia de 5.000 créditos concedida.")).toBeInTheDocument();
      const [call] = postCalls();
      expect(call.key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    } finally {
      delete (crypto as { randomUUID?: unknown }).randomUUID;
    }
    expect(typeof crypto.randomUUID).toBe("function");
  });

  it.each([
    [409, { code: "builder_inactive" }, "Este builder está inativo e não pode receber cortesia."],
    [409, { code: "idempotency_key_reused" }, /Esta tentativa já foi usada para outro pedido/],
    [404, { detail: { code: "builder_not_found" } }, "Builder não encontrado."],
    [503, { code: "credit_unavailable" }, /Os créditos estão indisponíveis no servidor agora/],
    [503, { code: "credit_clock_ahead" }, /O servidor não pôde gravar agora/],
  ])("shows the %i %j refusal as text", async (status, body, text) => {
    renderDialog();
    postReplies = [{ ok: false, status, body }];
    fillGrant("5000");
    await confirmGrant();
    expect(await grantSection().findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText(/"code"|builder_inactive|credit_unavailable/)).not.toBeInTheDocument();
  });

  it("never writes the reason to the console", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method).mockImplementation(() => undefined),
    );
    renderDialog();
    postReplies = ["network", { ok: false, status: 409, body: { code: "builder_inactive" } }];
    fillGrant("5000");
    await confirmGrant();
    await grantSection().findByText(/Falha de rede/);
    fireEvent.click(grantSection().getByRole("button", { name: "Tentar de novo" }));
    await grantSection().findByText(/inativo/);
    for (const spy of spies) {
      for (const args of spy.mock.calls) expect(JSON.stringify(args)).not.toContain(REASON);
    }
  });
});

describe("CourtesyCreditsDialog: Tirar créditos", () => {
  async function openRemoval(name: string = ACTIVE_LOT_BUTTON) {
    const lots = within(await screen.findByRole("region", { name: "Lotes de cortesia" }));
    fireEvent.click(await lots.findByRole("button", { name }));
    return removalSection();
  }

  it("removes at most floor(spendable / 1000) whole credits: 4.999.600 mc is 4.999", async () => {
    renderDialog([{ ...activeLot, available_mc: 4_999_600 }]);
    const removal = await openRemoval();
    expect(removal.getByText(/\(até 4\.999 créditos inteiros\)/)).toBeInTheDocument();
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    const amount = removal.getByLabelText("Quantidade de créditos");
    fireEvent.change(amount, { target: { value: "5000" } });
    expect(removal.getByRole("button", { name: "Continuar" })).toBeDisabled();
    fireEvent.change(amount, { target: { value: "4999" } });
    expect(removal.getByText("Sobram no lote: 0,6 créditos.")).toBeInTheDocument();
    expect(removal.getByRole("button", { name: "Continuar" })).toBeEnabled();
  });

  it("a lot under 1 whole credit only allows Tirar tudo", async () => {
    renderDialog([{ ...activeLot, available_mc: 400 }]);
    const removal = await openRemoval();
    expect(removal.getByLabelText("Quantidade de créditos")).toBeDisabled();
    expect(removal.getByText("O lote tem menos de 1 crédito inteiro: só é possível Tirar tudo.")).toBeInTheDocument();
    expect(removal.queryByText(/Informe um número inteiro/)).not.toBeInTheDocument();
    fireEvent.click(removal.getByRole("checkbox", { name: "Tirar tudo" }));
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    expect(removal.getByRole("button", { name: "Continuar" })).toBeEnabled();
  });

  it("a lot under 1 whole credit says so in English", async () => {
    localStorage.setItem("ns_locale", "en");
    renderDialog([{ ...activeLot, available_mc: 400 }]);
    const lots = within(await screen.findByRole("region", { name: "Courtesy lots" }));
    fireEvent.click(await lots.findByRole("button", { name: "Remove credits from the lot expiring on 12/09/2026" }));
    const removal = within(screen.getByRole("region", { name: "Remove credits" }));
    expect(removal.getByLabelText("Credits")).toBeDisabled();
    expect(removal.getByText("The lot has less than 1 whole credit: only Remove all is possible.")).toBeInTheDocument();
  });

  it("while a removal is in flight, no other lot can be opened and the dialog cannot be closed", async () => {
    const secondLot = { ...activeLot, lot_id: "lot-3", expires_at: "2026-12-20T03:00:00Z" };
    const { onOpenChange } = renderDialog([activeLot, secondLot]);
    postReplies = ["hang"];
    const removal = await openRemoval();
    fireEvent.change(removal.getByLabelText("Quantidade de créditos"), { target: { value: "1000" } });
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    fireEvent.click(removal.getByRole("button", { name: "Continuar" }));
    fireEvent.click(removal.getByRole("button", { name: "Confirmar" }));
    await waitFor(() => expect(postCalls()).toHaveLength(1));
    const lots = within(screen.getByRole("region", { name: "Lotes de cortesia" }));
    expect(lots.getByRole("button", { name: ACTIVE_LOT_BUTTON })).toBeDisabled();
    expect(lots.getByRole("button", { name: "Tirar créditos do lote que expira em 20/12/2026" })).toBeDisabled();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    releasePost?.();
    expect(await screen.findByText("Créditos retirados do lote.")).toBeInTheDocument();
    await waitFor(() =>
      expect(lots.getByRole("button", { name: "Tirar créditos do lote que expira em 20/12/2026" })).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps an open removal form when the lots reload after a grant", async () => {
    const { onChanged } = renderDialog();
    const removal = await openRemoval();
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: "remoção em andamento" } });
    holdNextList = true;
    fillGrant("5000");
    await confirmGrant();
    expect(await grantSection().findByText("Cortesia de 5.000 créditos concedida.")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalledTimes(1);
    // The reload is in flight: a non-blocking indicator, the table and the
    // open removal form (with what was typed) stay on screen.
    expect(await screen.findByText("Atualizando…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: ACTIVE_LOT_BUTTON })).toBeInTheDocument();
    expect(
      (removalSection().getByLabelText("Motivo (obrigatório)") as HTMLTextAreaElement).value,
    ).toBe("remoção em andamento");
    releaseList?.();
    await waitFor(() => expect(screen.queryByText("Atualizando…")).not.toBeInTheDocument());
    expect(
      (removalSection().getByLabelText("Motivo (obrigatório)") as HTMLTextAreaElement).value,
    ).toBe("remoção em andamento");
  });

  it("shows the lot's whole balance and the balance left, refusing more than the whole credits", async () => {
    renderDialog();
    const removal = await openRemoval();
    expect(removal.getByText(/Saldo do lote: 4\.999,4 créditos \(até 4\.999 créditos inteiros\)/)).toBeInTheDocument();
    const amount = removal.getByLabelText("Quantidade de créditos");
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    fireEvent.change(amount, { target: { value: "5000" } });
    expect(removal.getByRole("button", { name: "Continuar" })).toBeDisabled();
    fireEvent.change(amount, { target: { value: "4999" } });
    expect(removal.getByText("Sobram no lote: 0,4 créditos.")).toBeInTheDocument();
    expect(removal.getByRole("button", { name: "Continuar" })).toBeEnabled();
  });

  it("does not send without a reason", async () => {
    renderDialog();
    const removal = await openRemoval();
    fireEvent.change(removal.getByLabelText("Quantidade de créditos"), { target: { value: "1000" } });
    expect(removal.getByRole("button", { name: "Continuar" })).toBeDisabled();
    expect(postCalls()).toHaveLength(0);
  });

  it("sends credits N to the lot's removals route after confirming the balance left", async () => {
    const { onChanged } = renderDialog();
    const removal = await openRemoval();
    fireEvent.change(removal.getByLabelText("Quantidade de créditos"), { target: { value: "2000" } });
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    fireEvent.click(removal.getByRole("button", { name: "Continuar" }));
    expect(removal.getByText("Tirar 2.000 créditos deste lote? Sobram 2.999,4 créditos.")).toBeInTheDocument();
    fireEvent.click(removal.getByRole("button", { name: "Confirmar" }));
    expect(await screen.findByText("Créditos retirados do lote.")).toBeInTheDocument();
    const [call] = postCalls();
    expect(call.url).toBe(`${GRANT_URL}/lot-1/removals`);
    expect(call.body).toEqual({ credits: 2000, reason: REASON });
    expect(call.key.length).toBeGreaterThan(0);
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("Tirar tudo sends remove_all and leaves 0", async () => {
    renderDialog();
    const removal = await openRemoval();
    fireEvent.click(removal.getByRole("checkbox", { name: "Tirar tudo" }));
    expect(removal.getByLabelText("Quantidade de créditos")).toBeDisabled();
    expect(removal.getByText("Sobram no lote: 0 créditos.")).toBeInTheDocument();
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    fireEvent.click(removal.getByRole("button", { name: "Continuar" }));
    expect(removal.getByText("Tirar 4.999,4 créditos deste lote? Sobram 0 créditos.")).toBeInTheDocument();
    fireEvent.click(removal.getByRole("button", { name: "Confirmar" }));
    await waitFor(() => expect(postCalls()).toHaveLength(1));
    expect(postCalls()[0].body).toEqual({ remove_all: true, reason: REASON });
  });

  it("reuses the key on a retry and shows 409 / 404 refusals as text", async () => {
    renderDialog();
    postReplies = [
      "network",
      { ok: false, status: 409, body: { code: "courtesy_removal_exceeds_balance" } },
      { ok: false, status: 404, body: { code: "courtesy_lot_not_found" } },
    ];
    const removal = await openRemoval();
    fireEvent.change(removal.getByLabelText("Quantidade de créditos"), { target: { value: "1000" } });
    fireEvent.change(removal.getByLabelText("Motivo (obrigatório)"), { target: { value: REASON } });
    fireEvent.click(removal.getByRole("button", { name: "Continuar" }));
    fireEvent.click(removal.getByRole("button", { name: "Confirmar" }));
    expect(await removal.findByText(/Falha de rede/)).toBeInTheDocument();
    fireEvent.click(removal.getByRole("button", { name: "Tentar de novo" }));
    expect(await removal.findByRole("alert")).toHaveTextContent(
      "A quantidade passa do saldo que sobra no lote. Nada foi retirado.",
    );
    fireEvent.click(removal.getByRole("button", { name: "Confirmar" }));
    await waitFor(() => expect(removal.getByRole("alert")).toHaveTextContent("Lote de cortesia não encontrado."));
    const [first, retry, again] = postCalls();
    expect(retry.key).toBe(first.key);
    expect(again.key).toBe(first.key);
  });
});
