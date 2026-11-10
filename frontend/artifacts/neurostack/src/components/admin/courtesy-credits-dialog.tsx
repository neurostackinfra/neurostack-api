import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { useI18n } from "@/context/i18n";
import {
  MILLICREDITS_PER_CREDIT,
  formatCount,
  formatCredits,
  formatInstant,
  type Locale,
  type Txt,
} from "@/components/billing/format";

// The super admin's "Créditos" dialog of the "Assinaturas por Builder" table
// (creditos-cortesia, DESIGN "Diálogo 'Créditos'"). It lists the builder's
// courtesy lots, grants a courtesy ("Dar cortesia") and removes credits from
// an active courtesy lot ("Tirar créditos").
//
// The reason the admin types lives only in this dialog's form state: it is
// sent in the grant/removal body and nowhere else (never logged, never kept
// after the dialog closes -- the content unmounts with the dialog). The
// courtesy events the GET also returns (actor, reason) are never read.
//
// Idempotency (DESIGN Decision 10): one `Idempotency-Key` per attempt, and an
// attempt is the exact request body. The key follows the body, not the form:
// a resend of the same body (a retry after a network failure or an unknown
// outcome, a `503 credit_clock_ahead` -- DESIGN Decision 16 --, or "Voltar"
// and the same values confirmed again) reuses the key, so a grant the server
// committed is never sent twice under two keys. A different body, a success
// or an `idempotency_key_reused` answer starts a new attempt with a new key.

/** DESIGN Decision 3: whole credits, 1 to 100,000 per grant. */
const MAX_GRANT_CREDITS = 100_000;
/** DESIGN Decision 3: the shortcuts are the credits of Plus / Pro / Max. */
const GRANT_SHORTCUTS = [5_000, 15_000, 35_000] as const;
/** DESIGN IN: the reason has 1 to 500 characters. */
const MAX_REASON_LENGTH = 500;

const COURTESY_GRANTS_PATH = "/v1/admin/credits/courtesy-grants";

/** One courtesy lot of `GET /v1/admin/credits/courtesy-grants?builder_id=`. */
interface CourtesyLot {
  lot_id: string;
  status: string;
  granted_mc: number;
  available_mc: number;
  consumed_mc: number;
  removed_mc: number;
  expired_mc: number;
  expires_at: string;
}

const LOT_AMOUNT_KEYS = ["granted_mc", "available_mc", "consumed_mc", "removed_mc", "expired_mc"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCourtesyLot(value: unknown): value is CourtesyLot {
  return (
    isRecord(value) &&
    typeof value.lot_id === "string" &&
    typeof value.status === "string" &&
    typeof value.expires_at === "string" &&
    LOT_AMOUNT_KEYS.every((key) => Number.isInteger(value[key]) && (value[key] as number) >= 0)
  );
}

/** The lots of the GET answer, or `null` for a body this dialog does not know. */
function readLots(body: unknown): CourtesyLot[] | null {
  if (!isRecord(body) || !Array.isArray(body.lots)) return null;
  return body.lots.every(isCourtesyLot) ? body.lots : null;
}

/** The refusal code of an error body: `{code}` or FastAPI's `{detail: {code}}`. */
function refusalCode(body: unknown): string | null {
  if (!isRecord(body)) return null;
  if (typeof body.code === "string") return body.code;
  if (isRecord(body.detail) && typeof body.detail.code === "string") return body.detail.code;
  return null;
}

/** A readable, translated text for a refused request (never the raw body). */
function refusalText(status: number, code: string | null, txt: Txt): string {
  switch (code) {
    case "credit_unavailable":
      return txt(
        "Os créditos estão indisponíveis no servidor agora. Nada foi gravado; tente mais tarde.",
        "Credits are unavailable on the server right now. Nothing was saved; try again later.",
      );
    case "credit_clock_ahead":
      return txt(
        "O servidor não pôde gravar agora. Nada foi gravado; tente de novo em instantes.",
        "The server could not save right now. Nothing was saved; try again in a moment.",
      );
    case "builder_inactive":
      return txt(
        "Este builder está inativo e não pode receber cortesia.",
        "This builder is inactive and cannot receive a courtesy.",
      );
    case "builder_not_found":
      return txt("Builder não encontrado.", "Builder not found.");
    case "courtesy_lot_not_found":
      return txt("Lote de cortesia não encontrado.", "Courtesy lot not found.");
    case "courtesy_removal_exceeds_balance":
      return txt(
        "A quantidade passa do saldo que sobra no lote. Nada foi retirado.",
        "The amount exceeds the lot's remaining balance. Nothing was removed.",
      );
    case "idempotency_key_reused":
      return txt(
        "Esta tentativa já foi usada para outro pedido. Nada foi gravado; confirme de novo para enviar como nova tentativa.",
        "This attempt was already used for another request. Nothing was saved; confirm again to send it as a new attempt.",
      );
    default:
      break;
  }
  switch (status) {
    case 400:
    case 422:
      return txt("O servidor recusou os dados enviados.", "The server rejected the data sent.");
    case 401:
    case 403:
      return txt(
        "Sua sessão não tem permissão para esta ação.",
        "Your session is not allowed to do this.",
      );
    case 404:
      return txt("Builder ou lote não encontrado.", "Builder or lot not found.");
    case 409:
      return txt(
        "O servidor recusou o pedido por conflito. Nada foi gravado.",
        "The server refused the request due to a conflict. Nothing was saved.",
      );
    case 503:
      return txt(
        "Os créditos estão indisponíveis no servidor agora. Nada foi gravado; tente mais tarde.",
        "Credits are unavailable on the server right now. Nothing was saved; try again later.",
      );
    default:
      return txt(
        `O servidor respondeu com erro (${status}).`,
        `The server answered with an error (${status}).`,
      );
  }
}

const GROUPED_DIGITS = /^\d{1,3}(?:[.,\s]\d{3})+$/;
const PLAIN_DIGITS = /^\d+$/;

/** Whole credits typed by the admin ("15000" or "15.000"), or `null`. */
function parseCredits(value: string): number | null {
  const text = value.trim();
  if (PLAIN_DIGITS.test(text)) return Number(text);
  if (GROUPED_DIGITS.test(text)) return Number(text.replace(/[.,\s]/g, ""));
  return null;
}

/**
 * A v4 UUID. `crypto.randomUUID` exists only in a secure context, so outside
 * one (plain HTTP) the UUID is built from `crypto.getRandomValues`.
 */
function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The request of one attempt and its key: the key follows the body. */
interface Attempt {
  body: string;
  key: string;
}

/**
 * The key for `body`: the current attempt's key when the body is the same,
 * else a new attempt. `null` when no key can be made (no Web Crypto).
 */
function attemptFor(ref: { current: Attempt | null }, body: string): Attempt | null {
  if (ref.current?.body === body) return ref.current;
  try {
    ref.current = { body, key: newIdempotencyKey() };
  } catch {
    ref.current = null;
  }
  return ref.current;
}

const NETWORK_FAILURE: [string, string] = [
  "Falha de rede: não foi possível confirmar se o pedido chegou. Tente de novo: a mesma tentativa é reenviada e não duplica.",
  "Network failure: the request could not be confirmed. Try again: the same attempt is resent and is not duplicated.",
];

type SendResult = { ok: true } | { ok: false; message: string; network: boolean; code: string | null };

async function sendCourtesyRequest(
  path: string,
  token: string,
  key: string,
  body: string,
  txt: Txt,
): Promise<SendResult> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        "Idempotency-Key": key,
      },
      body,
    });
  } catch {
    return { ok: false, network: true, code: null, message: txt(...NETWORK_FAILURE) };
  }
  if (res.ok) return { ok: true };
  const errorBody: unknown = await res.json().catch(() => null);
  const code = refusalCode(errorBody);
  // A 5xx without a refusal code (a gateway timeout, a proxy error) may come
  // after the server committed: the outcome is unknown, as on a network
  // failure. A 503 is the server's own "nothing was saved".
  if (res.status >= 500 && res.status !== 503 && code === null) {
    return { ok: false, network: true, code: null, message: txt(...NETWORK_FAILURE) };
  }
  return { ok: false, network: false, code, message: refusalText(res.status, code, txt) };
}

const NO_KEY: [string, string] = [
  "Este navegador não gera a chave do pedido. Nada foi enviado.",
  "This browser cannot create the request key. Nothing was sent.",
];

function lotStatusLabel(status: string, txt: Txt): string {
  switch (status) {
    case "active":
      return txt("Ativo", "Active");
    case "exhausted":
      return txt("Esgotado", "Exhausted");
    case "expired":
      return txt("Expirado", "Expired");
    case "not_started":
      return txt("Não iniciado", "Not started");
    default:
      return status;
  }
}

// ── "Dar cortesia" ────────────────────────────────────────────────────────────

function GrantForm({
  builderId,
  builderName,
  token,
  sending,
  setSending,
  onDone,
}: {
  builderId: string;
  builderName: string;
  token: string;
  /** A grant or a removal is in flight (lifted to the dialog). */
  sending: boolean;
  setSending: (sending: boolean) => void;
  onDone: () => void;
}) {
  const { locale } = useI18n();
  const txt = useCallback<Txt>((p, e) => (locale === "en" ? e : p), [locale]);
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);
  const attemptRef = useRef<Attempt | null>(null);

  const credits = parseCredits(amount);
  const amountValid = credits !== null && credits >= 1 && credits <= MAX_GRANT_CREDITS;
  const trimmedReason = reason.trim();
  const reasonValid = trimmedReason.length >= 1 && trimmedReason.length <= MAX_REASON_LENGTH;

  // An edit clears the messages only: the key follows the body sent (`send`).
  const edit = () => {
    setError(null);
    setRetry(false);
    setSuccess(null);
  };

  const send = async () => {
    if (sending || !amountValid || !reasonValid || credits === null) return;
    const body = JSON.stringify({ builder_id: builderId, credits, reason: trimmedReason });
    const attempt = attemptFor(attemptRef, body);
    if (!attempt) {
      setError(txt(...NO_KEY));
      return;
    }
    setSending(true);
    setError(null);
    const result = await sendCourtesyRequest(COURTESY_GRANTS_PATH, token, attempt.key, body, txt);
    setSending(false);
    if (result.ok) {
      attemptRef.current = null;
      setAmount("");
      setReason("");
      setConfirming(false);
      setRetry(false);
      setSuccess(
        txt(
          `Cortesia de ${formatCount(credits, locale as Locale)} créditos concedida.`,
          `Courtesy of ${formatCount(credits, locale as Locale)} credits granted.`,
        ),
      );
      onDone();
      return;
    }
    if (result.code === "idempotency_key_reused") attemptRef.current = null;
    setError(result.message);
    setRetry(result.network);
  };

  return (
    <section className="space-y-3 rounded-md border border-border/60 p-3" aria-label={txt("Dar cortesia", "Grant courtesy")}>
      <p className="text-sm font-semibold">{txt("Dar cortesia", "Grant courtesy")}</p>
      {!confirming ? (
        <>
          <div className="space-y-1.5">
            <Label htmlFor="courtesy-grant-credits" className="text-xs">
              {txt("Quantidade de créditos", "Credits")}
            </Label>
            <Input
              id="courtesy-grant-credits"
              inputMode="numeric"
              value={amount}
              onChange={(event) => { setAmount(event.target.value); edit(); }}
              placeholder={txt("1 a 100.000", "1 to 100,000")}
            />
            <div className="flex flex-wrap gap-1.5">
              {GRANT_SHORTCUTS.map((value) => (
                <Button
                  key={value}
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => { setAmount(String(value)); edit(); }}
                >
                  {formatCount(value, locale as Locale)}
                </Button>
              ))}
            </div>
            {amount.trim() !== "" && !amountValid && (
              <p className="text-xs text-rose-400">
                {txt(
                  "Informe um número inteiro de 1 a 100.000 créditos.",
                  "Enter a whole number from 1 to 100,000 credits.",
                )}
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="courtesy-grant-validity" className="text-xs">
              {txt("Validade", "Validity")}
            </Label>
            <Input
              id="courtesy-grant-validity"
              readOnly
              value={txt("30 dias", "30 days")}
              className="bg-muted/40"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="courtesy-grant-reason" className="text-xs">
              {txt("Motivo (obrigatório)", "Reason (required)")}
            </Label>
            <Textarea
              id="courtesy-grant-reason"
              value={reason}
              maxLength={MAX_REASON_LENGTH}
              onChange={(event) => { setReason(event.target.value); edit(); }}
            />
          </div>
          <Button
            type="button"
            size="sm"
            disabled={!amountValid || !reasonValid}
            onClick={() => { setError(null); setSuccess(null); setConfirming(true); }}
          >
            {txt("Continuar", "Continue")}
          </Button>
        </>
      ) : (
        <div className="space-y-3">
          <p className="text-sm">
            {txt(
              `Dar ${formatCount(credits ?? 0, locale as Locale)} créditos de cortesia a ${builderName}, válidos por 30 dias?`,
              `Grant ${formatCount(credits ?? 0, locale as Locale)} courtesy credits to ${builderName}, valid for 30 days?`,
            )}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={sending}
              onClick={() => { setConfirming(false); setError(null); setRetry(false); }}
            >
              {txt("Voltar", "Back")}
            </Button>
            <Button type="button" size="sm" disabled={sending} onClick={() => void send()}>
              {sending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {retry ? txt("Tentar de novo", "Try again") : txt("Confirmar", "Confirm")}
            </Button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="text-xs text-rose-400">{error}</p>}
      {success && <p role="status" className="text-xs text-emerald-400">{success}</p>}
    </section>
  );
}

// ── "Tirar créditos" ──────────────────────────────────────────────────────────

function RemovalForm({
  lot,
  token,
  sending,
  setSending,
  onCancel,
  onDone,
}: {
  lot: CourtesyLot;
  token: string;
  /** A grant or a removal is in flight (lifted to the dialog). */
  sending: boolean;
  setSending: (sending: boolean) => void;
  onCancel: () => void;
  onDone: (message: string) => void;
}) {
  const { locale } = useI18n();
  const loc = locale as Locale;
  const txt = useCallback<Txt>((p, e) => (locale === "en" ? e : p), [locale]);
  const [amount, setAmount] = useState("");
  const [removeAll, setRemoveAll] = useState(false);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(false);
  // Keyed per lot (the form remounts per lot); the key follows the body.
  const attemptRef = useRef<Attempt | null>(null);

  // DESIGN Decision 5: 1 <= N <= floor(spendable mc / 1000); `remove_all`
  // takes the whole spendable balance, the millicredit fraction included.
  const wholeCredits = Math.floor(lot.available_mc / MILLICREDITS_PER_CREDIT);
  const credits = parseCredits(amount);
  const amountValid = removeAll
    ? lot.available_mc > 0
    : credits !== null && credits >= 1 && credits <= wholeCredits;
  const remainingMc = removeAll
    ? 0
    : amountValid && credits !== null
      ? lot.available_mc - credits * MILLICREDITS_PER_CREDIT
      : null;
  const trimmedReason = reason.trim();
  const reasonValid = trimmedReason.length >= 1 && trimmedReason.length <= MAX_REASON_LENGTH;

  const edit = () => {
    setError(null);
    setRetry(false);
  };

  const send = async () => {
    if (sending || !amountValid || !reasonValid) return;
    const body = JSON.stringify(
      removeAll ? { remove_all: true, reason: trimmedReason } : { credits, reason: trimmedReason },
    );
    const attempt = attemptFor(attemptRef, body);
    if (!attempt) {
      setError(txt(...NO_KEY));
      return;
    }
    setSending(true);
    setError(null);
    const result = await sendCourtesyRequest(
      `${COURTESY_GRANTS_PATH}/${encodeURIComponent(lot.lot_id)}/removals`,
      token,
      attempt.key,
      body,
      txt,
    );
    setSending(false);
    if (result.ok) {
      attemptRef.current = null;
      onDone(txt("Créditos retirados do lote.", "Credits removed from the lot."));
      return;
    }
    if (result.code === "idempotency_key_reused") attemptRef.current = null;
    setError(result.message);
    setRetry(result.network);
  };

  const removedLabel = removeAll
    ? formatCredits(lot.available_mc, loc)
    : formatCount(credits ?? 0, loc);

  return (
    <section className="space-y-3 rounded-md border border-border/60 p-3" aria-label={txt("Tirar créditos", "Remove credits")}>
      <p className="text-sm font-semibold">{txt("Tirar créditos", "Remove credits")}</p>
      <p className="text-xs text-muted-foreground">
        {txt(
          `Saldo do lote: ${formatCredits(lot.available_mc, loc)} créditos (até ${formatCount(wholeCredits, loc)} créditos inteiros).`,
          `Lot balance: ${formatCredits(lot.available_mc, loc)} credits (up to ${formatCount(wholeCredits, loc)} whole credits).`,
        )}
      </p>
      {!confirming ? (
        <>
          <div className="space-y-1.5">
            <Label htmlFor="courtesy-removal-credits" className="text-xs">
              {txt("Quantidade de créditos", "Credits")}
            </Label>
            <Input
              id="courtesy-removal-credits"
              inputMode="numeric"
              value={removeAll ? "" : amount}
              disabled={removeAll || wholeCredits === 0}
              onChange={(event) => { setAmount(event.target.value); edit(); }}
            />
            {wholeCredits === 0 && (
              <p className="text-xs text-muted-foreground">
                {txt(
                  "O lote tem menos de 1 crédito inteiro: só é possível Tirar tudo.",
                  "The lot has less than 1 whole credit: only Remove all is possible.",
                )}
              </p>
            )}
            {wholeCredits > 0 && !removeAll && amount.trim() !== "" && !amountValid && (
              <p className="text-xs text-rose-400">
                {txt(
                  `Informe um número inteiro de 1 a ${formatCount(wholeCredits, loc)} créditos.`,
                  `Enter a whole number from 1 to ${formatCount(wholeCredits, loc)} credits.`,
                )}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="courtesy-removal-all"
              checked={removeAll}
              onCheckedChange={(checked) => { setRemoveAll(checked === true); edit(); }}
            />
            <Label htmlFor="courtesy-removal-all" className="text-xs">{txt("Tirar tudo", "Remove all")}</Label>
          </div>
          {remainingMc !== null && (
            <p className="text-xs">
              {txt(
                `Sobram no lote: ${formatCredits(remainingMc, loc)} créditos.`,
                `Left in the lot: ${formatCredits(remainingMc, loc)} credits.`,
              )}
            </p>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="courtesy-removal-reason" className="text-xs">
              {txt("Motivo (obrigatório)", "Reason (required)")}
            </Label>
            <Textarea
              id="courtesy-removal-reason"
              value={reason}
              maxLength={MAX_REASON_LENGTH}
              onChange={(event) => { setReason(event.target.value); edit(); }}
            />
          </div>
          <div className="flex gap-2">
            <Button type="button" variant="outline" size="sm" onClick={onCancel}>
              {txt("Cancelar", "Cancel")}
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!amountValid || !reasonValid}
              onClick={() => { setError(null); setConfirming(true); }}
            >
              {txt("Continuar", "Continue")}
            </Button>
          </div>
        </>
      ) : (
        <div className="space-y-3">
          <p className="text-sm">
            {txt(
              `Tirar ${removedLabel} créditos deste lote? Sobram ${formatCredits(remainingMc ?? 0, loc)} créditos.`,
              `Remove ${removedLabel} credits from this lot? ${formatCredits(remainingMc ?? 0, loc)} credits are left.`,
            )}
          </p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={sending}
              onClick={() => { setConfirming(false); setError(null); setRetry(false); }}
            >
              {txt("Voltar", "Back")}
            </Button>
            <Button type="button" size="sm" disabled={sending} onClick={() => void send()}>
              {sending && <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" />}
              {retry ? txt("Tentar de novo", "Try again") : txt("Confirmar", "Confirm")}
            </Button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="text-xs text-rose-400">{error}</p>}
    </section>
  );
}

// ── Dialog ────────────────────────────────────────────────────────────────────

type LotsState =
  | { status: "loading" }
  | { status: "ready"; lots: CourtesyLot[] }
  | { status: "error"; message: string };

function DialogBody({
  builderId,
  builderName,
  token,
  sending,
  setSending,
  onChanged,
}: {
  builderId: string;
  builderName: string;
  token: string;
  /** A grant or a removal is in flight: one request at a time. */
  sending: boolean;
  setSending: (sending: boolean) => void;
  onChanged?: () => void;
}) {
  const { locale } = useI18n();
  const loc = locale as Locale;
  const txt = useCallback<Txt>((p, e) => (locale === "en" ? e : p), [locale]);
  const [lotsState, setLotsState] = useState<LotsState>({ status: "loading" });
  const [removingLotId, setRemovingLotId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);

  // Only the first load blanks the table: a reload (after a grant or a
  // removal) keeps the lots, and an open removal form, on screen.
  const loadLots = useCallback(async () => {
    setLotsState((current) => (current.status === "ready" ? current : { status: "loading" }));
    setReloading(true);
    try {
      const res = await fetch(
        `${COURTESY_GRANTS_PATH}?builder_id=${encodeURIComponent(builderId)}`,
        { headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` } },
      );
      if (!res.ok) {
        const errorBody: unknown = await res.json().catch(() => null);
        setLotsState({ status: "error", message: refusalText(res.status, refusalCode(errorBody), txt) });
        return;
      }
      const lots = readLots(await res.json().catch(() => null));
      setLotsState(
        lots
          ? { status: "ready", lots }
          : {
              status: "error",
              message: txt(
                "O servidor respondeu num formato inesperado. Nenhum lote foi presumido.",
                "The server answered in an unexpected format. No lot was assumed.",
              ),
            },
      );
    } catch {
      setLotsState({
        status: "error",
        message: txt(
          "Falha de rede ao carregar os lotes de cortesia.",
          "Network failure while loading the courtesy lots.",
        ),
      });
    } finally {
      setReloading(false);
    }
  }, [builderId, token, txt]);

  useEffect(() => {
    void loadLots();
  }, [loadLots]);

  const changed = () => {
    void loadLots();
    onChanged?.();
  };

  const removingLot =
    lotsState.status === "ready" ? lotsState.lots.find((lot) => lot.lot_id === removingLotId) ?? null : null;

  return (
    <div className="space-y-4">
      <section className="space-y-2" aria-label={txt("Lotes de cortesia", "Courtesy lots")}>
        <p className="text-sm font-semibold">
          {txt("Lotes de cortesia", "Courtesy lots")}
          {reloading && lotsState.status === "ready" && (
            <span role="status" className="ml-2 text-xs font-normal text-muted-foreground">
              <Loader2 className="inline h-3 w-3 mr-1 animate-spin" />
              {txt("Atualizando…", "Updating…")}
            </span>
          )}
        </p>
        {lotsState.status === "loading" && (
          <p className="text-xs text-muted-foreground">{txt("Carregando…", "Loading…")}</p>
        )}
        {lotsState.status === "error" && (
          <p role="alert" className="text-xs text-rose-400">{lotsState.message}</p>
        )}
        {lotsState.status === "ready" && lotsState.lots.length === 0 && (
          <p className="text-xs text-muted-foreground">
            {txt("Nenhum lote de cortesia.", "No courtesy lot.")}
          </p>
        )}
        {lotsState.status === "ready" && lotsState.lots.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border/50">
                  <th className="text-left pb-2 pr-3 font-medium text-muted-foreground">{txt("Expira em", "Expires on")}</th>
                  <th className="text-left pb-2 pr-3 font-medium text-muted-foreground">{txt("Status", "Status")}</th>
                  <th className="text-right pb-2 pr-3 font-medium text-muted-foreground">{txt("Concedido", "Granted")}</th>
                  <th className="text-right pb-2 pr-3 font-medium text-muted-foreground">{txt("Disponível", "Available")}</th>
                  <th className="text-right pb-2 pr-3 font-medium text-muted-foreground">{txt("Consumido", "Consumed")}</th>
                  <th className="text-right pb-2 pr-3 font-medium text-muted-foreground">{txt("Retirado", "Removed")}</th>
                  <th className="text-right pb-2 pr-3 font-medium text-muted-foreground">{txt("Expirado", "Expired")}</th>
                  <th className="pb-2"><span className="sr-only">{txt("Ações", "Actions")}</span></th>
                </tr>
              </thead>
              <tbody>
                {lotsState.lots.map((lot) => {
                  const expiresOn = formatInstant(lot.expires_at, loc, { withTime: false }) ?? "-";
                  return (
                    <tr key={lot.lot_id} className="border-b border-border/30 last:border-0">
                      <td className="py-2 pr-3 tabular-nums">{expiresOn}</td>
                      <td className="py-2 pr-3">
                        <Badge variant="outline" className="text-[10px]">{lotStatusLabel(lot.status, txt)}</Badge>
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">{formatCredits(lot.granted_mc, loc)}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{formatCredits(lot.available_mc, loc)}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{formatCredits(lot.consumed_mc, loc)}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{formatCredits(lot.removed_mc, loc)}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{formatCredits(lot.expired_mc, loc)}</td>
                      <td className="py-2 text-right">
                        {lot.status === "active" && lot.available_mc > 0 && (
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="h-7 text-xs"
                            disabled={sending}
                            aria-label={txt(
                              `Tirar créditos do lote que expira em ${expiresOn}`,
                              `Remove credits from the lot expiring on ${expiresOn}`,
                            )}
                            onClick={() => { setNotice(null); setRemovingLotId(lot.lot_id); }}
                          >
                            {txt("Tirar créditos", "Remove credits")}
                          </Button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {notice && <p role="status" className="text-xs text-emerald-400">{notice}</p>}
      </section>

      {removingLot && (
        <RemovalForm
          key={removingLot.lot_id}
          lot={removingLot}
          token={token}
          sending={sending}
          setSending={setSending}
          onCancel={() => setRemovingLotId(null)}
          onDone={(message) => {
            setRemovingLotId(null);
            setNotice(message);
            changed();
          }}
        />
      )}

      <GrantForm
        builderId={builderId}
        builderName={builderName}
        token={token}
        sending={sending}
        setSending={setSending}
        onDone={changed}
      />
    </div>
  );
}

export interface CourtesyCreditsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  builderId: string;
  builderName: string;
  token: string;
  /** Called after a grant or a removal succeeded (e.g. to reload the table). */
  onChanged?: () => void;
}

export function CourtesyCreditsDialog({
  open,
  onOpenChange,
  builderId,
  builderName,
  token,
  onChanged,
}: CourtesyCreditsDialogProps) {
  const { locale } = useI18n();
  const txt: Txt = (p, e) => (locale === "en" ? e : p);
  // A grant or a removal in flight: the dialog cannot be closed (Esc, the
  // close button, a click outside) until its outcome is shown.
  const [sending, setSending] = useState(false);
  return (
    <Dialog open={open} onOpenChange={(next) => { if (next || !sending) onOpenChange(next); }}>
      <DialogContent className="sm:max-w-2xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{txt(`Créditos de ${builderName}`, `${builderName} credits`)}</DialogTitle>
          <DialogDescription>
            {txt(
              "Créditos de cortesia: validade de 30 dias, sem assinatura nem pagamento.",
              "Courtesy credits: valid for 30 days, with no subscription or payment.",
            )}
          </DialogDescription>
        </DialogHeader>
        {/* Mounted only while open: the form state (the reason included) is
            dropped when the dialog closes. */}
        {open && (
          <DialogBody
            builderId={builderId}
            builderName={builderName}
            token={token}
            sending={sending}
            setSending={setSending}
            onChanged={onChanged}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}
