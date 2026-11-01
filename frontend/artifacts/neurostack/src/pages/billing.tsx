import { useState, useEffect, useCallback, useId, useRef, forwardRef } from "react";
import {
  AlertTriangle,
  CreditCard,
  Loader2,
  ArrowRight,
  X,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { useI18n } from "@/context/i18n";
import { PlanUsageSummary } from "@/components/plan-usage-summary";
import {
  CreditsAndTopups,
  type CreditHistoryState,
  type CreditsPanelState,
  type TopupMethod,
} from "@/components/billing/credits-and-topups";
import { PlanComparison } from "@/components/billing/plan-comparison";
import { SubscriptionOverview } from "@/components/billing/subscription-overview";
import type { Txt } from "@/components/billing/format";
import { navigateToHostedCheckout } from "./billing-navigation";
import {
  UUID_PATTERN,
  authHeaders,
  isBillingCatalog,
  isBillingSummary,
  isCheckoutSession,
  isCreditCatalog,
  isCreditLots,
  isCreditSummary,
  readBilling,
  readCredits,
  type BillingCatalog,
  type BillingLoad,
  type BillingSummary,
  type CheckoutSession,
  type CreditCatalog,
  type CreditLots,
  type CreditSummary,
} from "./billing-contract";
import { deriveBillingView, deriveTopupState } from "./billing-view";

// ── Types ────────────────────────────────────────────────────────────────────

interface SubscriptionPlan {
  offerId: string;
  name: string;
}

// `_LIVE_STATES` in `src/services/billing_checkout.py`: the session is still open.
const LIVE_CHECKOUT_STATES = new Set([
  "creating",
  "active",
  "paid_pending_subscription",
  "reconciliation_required",
  "terminal_pending_readback",
]);

// Only the opaque session UUID returned by the API is remembered, so the page
// can re-read that session from the server after the hosted checkout returns.
const CHECKOUT_SESSION_STORAGE_KEY = "ns_billing_checkout_session";

// ── Helpers ──────────────────────────────────────────────────────────────────

function useToken() {
  const raw = localStorage.getItem("ns_auth");
  try {
    return (JSON.parse(raw ?? "{}") as { token?: string }).token ?? "";
  } catch {
    return "";
  }
}

function readStoredSessionId(): string | null {
  try {
    const value = window.sessionStorage.getItem(CHECKOUT_SESSION_STORAGE_KEY);
    return value && UUID_PATTERN.test(value) ? value : null;
  } catch {
    return null;
  }
}

function storeSessionId(value: string | null) {
  try {
    if (value) window.sessionStorage.setItem(CHECKOUT_SESSION_STORAGE_KEY, value);
    else window.sessionStorage.removeItem(CHECKOUT_SESSION_STORAGE_KEY);
  } catch {
    // Without storage the page still works; it just cannot re-read the session.
  }
}

/** The backend-built hosted URL, followed only for an `active` HTTPS session. */
function hostedCheckoutUrl(session: CheckoutSession | null): string | null {
  if (!session || session.state !== "active" || !session.checkout_url) return null;
  try {
    return new URL(session.checkout_url).protocol === "https:" ? session.checkout_url : null;
  } catch {
    return null;
  }
}

function hasReturnMarker(): boolean {
  return new URLSearchParams(window.location.search).has("checkout");
}

/** Drop the navigation-only `?checkout=` marker so a reload does not repeat it. */
function removeReturnMarker() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("checkout")) return;
  params.delete("checkout");
  const query = params.toString();
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`,
  );
}

function checkoutStateLabel(state: string, txt: Txt) {
  const labels: Record<string, [string, string]> = {
    creating: ["Criando o checkout no provedor", "Creating the checkout with the provider"],
    active: ["Checkout aberto, aguardando pagamento", "Checkout open, awaiting payment"],
    paid_pending_subscription: [
      "Pagamento recebido pelo provedor; aguardando confirmação da assinatura",
      "Payment received by the provider; awaiting subscription confirmation",
    ],
    reconciliation_required: ["Em verificação pelo servidor", "Under server verification"],
    terminal_pending_readback: [
      "Aguardando a leitura final do provedor",
      "Awaiting the provider's final readback",
    ],
    completed: ["Checkout concluído", "Checkout completed"],
    abandoned: ["Checkout encerrado sem conclusão", "Checkout closed without completion"],
    cancelled: ["Checkout cancelado", "Checkout cancelled"],
    expired: ["Checkout expirado", "Checkout expired"],
    failed: ["Checkout não criado", "Checkout not created"],
  };
  const known = labels[state];
  return known
    ? txt(known[0], known[1])
    : txt(`Estado informado pelo servidor: ${state}`, `Server-reported state: ${state}`);
}

/** Top-up outcomes that differ from the subscription copy. */
function topupErrorMessage(status: number, txt: Txt) {
  if (status === 409 || status === 503) {
    return txt(
      "Recargas estão indisponíveis no momento. Nenhuma cobrança foi criada.",
      "Top-ups are unavailable right now. No charge was created.",
    );
  }
  if (status === 202) {
    return txt(
      "O servidor não conseguiu confirmar se o checkout foi criado no provedor. Não pague novamente; aguarde alguns minutos e atualize a página.",
      "The server could not confirm whether the checkout was created with the provider. Do not pay again; wait a few minutes and refresh the page.",
    );
  }
  return checkoutErrorMessage(status, txt);
}

/** `attachment; filename="..."` of the export, or a fixed fallback. */
function exportFilename(disposition: string | null): string {
  const match = disposition?.match(/filename="([^"]+)"/);
  const name = match?.[1] ?? "";
  return /^[A-Za-z0-9._-]+$/.test(name) ? name : "neurostack-export.zip";
}

/** Simple translated copy per route outcome; backend codes are never shown. */
function checkoutErrorMessage(status: number, txt: Txt) {
  switch (status) {
    case 202:
      return txt(
        "O servidor não conseguiu confirmar se o checkout foi criado no provedor. Não pague novamente. Para retomar o checkout existente, clique em assinar de novo no mesmo plano: o servidor devolve a sessão já aberta. Ou aguarde alguns minutos e atualize a página.",
        "The server could not confirm whether the checkout was created with the provider. Do not pay again. To resume the existing checkout, click subscribe again on the same plan: the server returns the session already open. Or wait a few minutes and refresh the page.",
      );
    case 401:
      return txt(
        "Sua sessão expirou. Entre novamente para continuar.",
        "Your session expired. Sign in again to continue.",
      );
    case 403:
      return txt(
        "Sua conta não tem permissão para gerenciar o faturamento. Nenhuma cobrança foi criada.",
        "Your account is not allowed to manage billing. No charge was created.",
      );
    case 409:
      return txt(
        "Sua conta não pode iniciar uma nova assinatura agora. Se já existe uma assinatura ou um checkout em andamento, aguarde a confirmação ou fale com o suporte.",
        "Your account cannot start a new subscription right now. If a subscription or checkout is already in progress, wait for confirmation or contact support.",
      );
    case 422:
      return txt(
        "O servidor rejeitou os dados do checkout. Nenhuma cobrança foi criada.",
        "The server rejected the checkout request. No charge was created.",
      );
    case 502:
      return txt(
        "O provedor de pagamento recusou a criação do checkout. Nenhuma cobrança foi criada. Tente novamente mais tarde.",
        "The payment provider refused to create the checkout. No charge was created. Try again later.",
      );
    case 503:
      return txt(
        "Novas assinaturas estão indisponíveis no momento. Nenhuma cobrança foi criada.",
        "New subscriptions are unavailable right now. No charge was created.",
      );
    default:
      return txt(
        "Não foi possível iniciar o checkout. Nenhum estado financeiro foi ativado.",
        "The checkout could not be started. No financial state was activated.",
      );
  }
}

/** Area heading; the number is decorative, the heading names the area. */
// `tabIndex={-1}`: focus lands here after a retry removes the retry button.
const AreaHeading = forwardRef<HTMLHeadingElement, { id: string; num: string; label: string }>(function AreaHeading(
  { id, num, label },
  ref,
) {
  return (
    <div className="flex items-center gap-3">
      <div
        aria-hidden="true"
        className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/20 shrink-0"
      >
        <span className="text-[9px] font-bold text-emerald-800 dark:text-emerald-500">{num}</span>
      </div>
      <h2
        ref={ref}
        id={id}
        tabIndex={-1}
        className="text-[10px] font-bold uppercase tracking-widest text-emerald-700 focus:outline-none dark:text-emerald-500"
      >
        {label}
      </h2>
      <div aria-hidden="true" className="flex-1 h-px bg-emerald-500/20" />
    </div>
  );
});

/** Marks the header's persistent polite live region (payment notices). */
const BILLING_STATUS_REGION = "billing-status";

// ── Main Component ───────────────────────────────────────────────────────────

export default function Billing() {
  const { locale } = useI18n();
  const txt = useCallback<Txt>((p, e) => (locale === "en" ? e : p), [locale]);
  const token = useToken();

  // State: one load per authoritative resource, owned by this page only.
  const [summary, setSummary] = useState<BillingLoad<BillingSummary>>({ status: "loading" });
  const [catalog, setCatalog] = useState<BillingLoad<BillingCatalog>>({ status: "loading" });
  const [returnedFromCheckout] = useState(hasReturnMarker);

  // Checkout state
  const [showCheckout, setShowCheckout] = useState(false);
  const [selectedPlan, setSelectedPlan] = useState<SubscriptionPlan | null>(null);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const [trackedSession, setTrackedSession] = useState<CheckoutSession | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  // A stored session id the server has not yet answered for: subscribing stays
  // blocked until it is read (or the server says it does not exist).
  const [unreadSessionId, setUnreadSessionId] = useState<string | null>(readStoredSessionId);
  const checkoutRef = useRef<HTMLDivElement>(null);
  // A retry button unmounts while its read is loading; focus moves to the
  // heading of what is being re-read instead of falling to <body>.
  const titleRef = useRef<HTMLHeadingElement>(null);
  const plansHeadingRef = useRef<HTMLHeadingElement>(null);
  const creditsHeadingRef = useRef<HTMLHeadingElement>(null);
  // A checkout POST answered 200 without a provider URL unmounts the button
  // that sent it; focus then moves to the checkout status card (not <body>).
  const checkoutStatusRef = useRef<HTMLDivElement>(null);
  const checkoutStatusLabelId = useId();
  const focusCheckoutStatus = useRef(false);

  // Credits: read from the ledger only. `unavailable` is the credit switch
  // being off (feature absent); every other failure stays visible.
  const [credit, setCredit] = useState<BillingLoad<CreditSummary>>({ status: "loading" });
  const [lots, setLots] = useState<BillingLoad<CreditLots>>({ status: "loading" });
  const [creditCatalog, setCreditCatalog] = useState<BillingLoad<CreditCatalog>>({ status: "loading" });
  const [topupLoading, setTopupLoading] = useState(false);
  const [topupError, setTopupError] = useState<string | null>(null);
  const [exportState, setExportState] = useState<"idle" | "loading" | "unavailable" | "error">(
    "idle",
  );

  // Fetch summary: a failed or refused read stays a failure, never a default.
  // A re-read keeps the last good answer on screen (no area flickers away);
  // a retry after a failure shows loading.
  const fetchSummary = useCallback(async () => {
    setSummary((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    setSummary(await readBilling("/v1/billing/summary", token, isBillingSummary));
  }, [token]);

  // The backend-owned catalog: every price, credit and clinic shown for a plan.
  const fetchCatalog = useCallback(async () => {
    setCatalog({ status: "loading" });
    setCatalog(await readBilling("/v1/billing/catalog", token, isBillingCatalog));
  }, [token]);

  // The lots: the only credit history of the contract (no cursor, no paging).
  const fetchLots = useCallback(async () => {
    setLots((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    setLots(await readCredits("/v1/credits/lots", token, isCreditLots));
  }, [token]);

  // The credit summary: the ledger's figures, never anticipated by the page.
  // A re-read keeps the last ledger answer on screen until the new one
  // arrives; a retry after a failure shows loading. The lots and the
  // packages are read only once the ledger answered: with the switch off
  // there is nothing more to ask.
  const fetchCredit = useCallback(async () => {
    setCredit((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    const result = await readCredits("/v1/credits/summary", token, isCreditSummary);
    setCredit(result);
    if (result.status !== "ok") return;
    await Promise.all([
      fetchLots(),
      (async () => {
        setCreditCatalog(await readCredits("/v1/credits/catalog", token, isCreditCatalog));
      })(),
    ]);
  }, [token, fetchLots]);

  // Read (or ask the server to reconcile) one checkout session by its UUID.
  const fetchSession = useCallback(
    async (sessionId: string, reconcile = false) => {
      setSessionLoading(true);
      setSessionError(null);
      try {
        const path = `/v1/billing/checkout/${encodeURIComponent(sessionId)}`;
        const res = await fetch(
          reconcile ? `${path}/reconcile` : path,
          reconcile
            ? { method: "POST", headers: authHeaders(token) }
            : { headers: authHeaders(token) },
        );
        if (res.status === 404) {
          // Unknown and foreign IDs are indistinguishable 404s: forget it.
          storeSessionId(null);
          setTrackedSession(null);
          setUnreadSessionId(null);
          return;
        }
        if (reconcile && res.status === 202) {
          // The provider read failed; the session stays under verification.
          setSessionError(
            txt(
              "O provedor não pôde ser consultado agora. O checkout continua em verificação pelo servidor; tente verificar novamente mais tarde.",
              "The provider could not be reached right now. The checkout remains under server verification; try verifying again later.",
            ),
          );
          return;
        }
        const data: unknown = res.status === 200 ? await res.json() : null;
        if (!isCheckoutSession(data)) {
          setSessionError(
            txt(
              "Não foi possível ler o estado do checkout no servidor. Tente novamente mais tarde.",
              "The checkout state could not be read from the server. Try again later.",
            ),
          );
          return;
        }
        setTrackedSession(data);
        setUnreadSessionId(null);
        // Terminal sessions are shown once and then forgotten.
        storeSessionId(LIVE_CHECKOUT_STATES.has(data.state) ? data.checkout_session_id : null);
      } catch {
        setSessionError(
          txt(
            "Não foi possível ler o estado do checkout no servidor. Tente novamente mais tarde.",
            "The checkout state could not be read from the server. Try again later.",
          ),
        );
      } finally {
        setSessionLoading(false);
      }
    },
    [token, txt],
  );

  // One read of each authoritative resource per visit. The `?checkout=`
  // return marker is navigation only: it activates nothing and starts no
  // polling; status comes from the summary and the stored session's readback.
  const initialReadStarted = useRef(false);
  useEffect(() => {
    removeReturnMarker();
    if (initialReadStarted.current) return;
    initialReadStarted.current = true;
    void fetchSummary();
    void fetchCatalog();
    // After a checkout return or a PIX QR code the ledger is re-read like
    // the order: a grant shows only once the server applied it.
    void fetchCredit();
    const storedSessionId = readStoredSessionId();
    if (storedSessionId) void fetchSession(storedSessionId);
  }, [fetchSummary, fetchCatalog, fetchSession, fetchCredit]);

  useEffect(() => {
    if (focusCheckoutStatus.current && checkoutStatusRef.current) {
      focusCheckoutStatus.current = false;
      checkoutStatusRef.current.focus();
    }
  });

  // ── Derived state: the commercial view is read only in `billing-view` ────
  const {
    summaryData,
    subscription,
    accessDenied,
    overviewState,
    usageState,
    plans,
    comparisonState,
    plansNotice,
    contextAlert,
  } = deriveBillingView({ summary, catalog, txt });

  // Pending signals, all read from the server: this tab's tracked session
  // (or a stored id not yet read back) and the summary's `live_session`, the
  // builder's newest live attempt, which survives a redirect, a refresh and
  // another tab. Any of them blocks subscribing and top-ups until the server
  // reports otherwise; none of them ever confirms a subscription or credits.
  const liveSession = summaryData?.checkout.live_session ?? null;
  const sessionInProgress =
    trackedSession !== null && LIVE_CHECKOUT_STATES.has(trackedSession.state);
  const checkoutLive = sessionInProgress || unreadSessionId !== null || liveSession !== null;
  const checkoutBlocked = summaryData === null || checkoutLive;
  const checkoutBlockedReason = checkoutLive
    ? txt(
        "Há um checkout em andamento nesta conta. Novas assinaturas ficam bloqueadas até o servidor confirmar o estado dele.",
        "A checkout is in progress on this account. New subscriptions stay blocked until the server confirms its state.",
      )
    : null;
  // The status card shows the tracked session, or else the summary's live
  // attempt (which carries no provider URL, so it offers no hosted link).
  const shownSession: CheckoutSession | null =
    trackedSession &&
    (sessionInProgress || liveSession === null || liveSession.checkout_session_id === trackedSession.checkout_session_id)
      ? trackedSession
      : liveSession
        ? {
            checkout_session_id: liveSession.checkout_session_id,
            kind: liveSession.kind,
            state: liveSession.state,
            checkout_url: null,
          }
        : trackedSession;
  const trackedCheckoutUrl = hostedCheckoutUrl(shownSession);
  const shownIsLive =
    shownSession !== null &&
    (LIVE_CHECKOUT_STATES.has(shownSession.state) ||
      shownSession.checkout_session_id === liveSession?.checkout_session_id);
  const topup = deriveTopupState({ summary, credit, creditCatalog, checkoutLive, txt });
  // Areas 02 and 03 appear only once the summary was read, or failed with an
  // explicit state: never while it may still answer 401/403.
  const summarySettled =
    summary.status === "ok" || summary.status === "unavailable" || summary.status === "error";
  // `503 {"code": "credit_unavailable"}` (and only that answer) is the credit
  // switch being off: a feature not launched, so area 03 does not exist at
  // all. Every other credit failure keeps the area with its own error.
  const creditsLaunched = credit.status !== "unavailable";
  const creditsState: CreditsPanelState =
    credit.status === "ok"
      ? { status: "ready", summary: credit.data }
      : credit.status === "loading"
        ? { status: "loading" }
        : credit.status === "forbidden" || credit.status === "unauthorized"
          ? { status: "forbidden" }
          : { status: "error" };
  const historyState: CreditHistoryState =
    lots.status === "ok"
      ? { status: "ready", lots: lots.data.lots }
      : lots.status === "loading"
        ? { status: "loading" }
        : { status: "error" };

  // Checkout: exactly one POST; the browser never activates anything.
  const handleCheckout = async () => {
    if (!selectedPlan || checkoutBlocked) return;
    setCheckoutLoading(true);
    setCheckoutError(null);
    try {
      const res = await fetch("/v1/billing/checkout", {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({ offer_id: selectedPlan.offerId, method: "credit_card" }),
      });
      // 202 is an `ok` status but carries only a reconciliation code.
      if (res.status !== 200) {
        setCheckoutError(checkoutErrorMessage(res.status, txt));
        return;
      }
      const data: unknown = await res.json();
      if (!isCheckoutSession(data)) {
        setCheckoutError(checkoutErrorMessage(0, txt));
        return;
      }
      setTrackedSession(data);
      storeSessionId(LIVE_CHECKOUT_STATES.has(data.state) ? data.checkout_session_id : null);
      const url = hostedCheckoutUrl(data);
      if (url) navigateToHostedCheckout(url);
      else focusCheckoutStatus.current = true;
    } catch {
      setCheckoutError(checkoutErrorMessage(0, txt));
    } finally {
      setCheckoutLoading(false);
    }
  };

  // Top-up: exactly one POST of `offer_id` and `method`; nothing else. The
  // offer must be one the server offered in this very render.
  const handleTopup = async (offerId: string, method: TopupMethod) => {
    if (topup.status !== "offered" || topupLoading) return;
    const offered = topup.packages.find((pkg) => pkg.offerId === offerId);
    if (!offered || !offered.methods.includes(method)) return;
    setTopupLoading(true);
    setTopupError(null);
    try {
      const res = await fetch("/v1/billing/checkout", {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({ offer_id: offered.offerId, method }),
      });
      if (res.status !== 200) {
        setTopupError(topupErrorMessage(res.status, txt));
        return;
      }
      const data: unknown = await res.json();
      if (!isCheckoutSession(data)) {
        setTopupError(checkoutErrorMessage(0, txt));
        return;
      }
      setTrackedSession(data);
      storeSessionId(LIVE_CHECKOUT_STATES.has(data.state) ? data.checkout_session_id : null);
      const url = hostedCheckoutUrl(data);
      if (url) navigateToHostedCheckout(url);
      else focusCheckoutStatus.current = true;
    } catch {
      setTopupError(checkoutErrorMessage(0, txt));
    } finally {
      setTopupLoading(false);
    }
  };

  // Export: the builder session downloads `GET /v1/credits/export`.
  const handleExport = async () => {
    if (exportState === "loading" || exportState === "unavailable") return;
    setExportState("loading");
    try {
      const res = await fetch("/v1/credits/export", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 503) {
        setExportState("unavailable");
        return;
      }
      if (res.status !== 200) {
        setExportState("error");
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = exportFilename(res.headers.get("Content-Disposition"));
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setExportState("idle");
    } catch {
      setExportState("error");
    }
  };

  const openCheckout = (offerId: string) => {
    const plan = plans.find((item) => item.offerId === offerId);
    if (checkoutBlocked || !plan?.canSubscribe) return;
    setSelectedPlan({ offerId: plan.offerId, name: plan.name });
    setShowCheckout(true);
    setCheckoutError(null);
    setTimeout(() => checkoutRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };

  // ── Render ───────────────────────────────────────────────────────────────

  const errorAlert = contextAlert?.tone === "error" ? contextAlert : null;
  const statusNotice = contextAlert?.tone === "status" ? contextAlert.message : null;

  const header = (
    <header className="space-y-2">
      <Breadcrumb aria-label={txt("Trilha de navegação", "Breadcrumb")}>
        <BreadcrumbList className="text-xs">
          <BreadcrumbItem>NeuroStack</BreadcrumbItem>
          <BreadcrumbSeparator />
          <BreadcrumbItem>
            <BreadcrumbPage>{txt("Financeiro", "Finance")}</BreadcrumbPage>
          </BreadcrumbItem>
        </BreadcrumbList>
      </Breadcrumb>
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-500/10 border border-emerald-500/20">
          <CreditCard className="h-5 w-5 text-emerald-500" aria-hidden="true" />
        </div>
        <div>
          <h1 ref={titleRef} tabIndex={-1} className="text-2xl font-bold tracking-tight text-foreground focus:outline-none">
            {txt("Pagamento", "Payment")}
          </h1>
          <p className="text-sm font-medium text-foreground/70 mt-0.5">
            {txt(
              "Acompanhe sua assinatura, o consumo do plano e seus créditos.",
              "Track your subscription, plan usage and credits.",
            )}
          </p>
        </div>
      </div>
      {errorAlert && (
        <div
          data-testid="billing-context-alert"
          role="alert"
          className="flex flex-wrap items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive"
        >
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span className="flex-1">{errorAlert.message}</span>
          {errorAlert.retry && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => {
                titleRef.current?.focus();
                void fetchSummary();
              }}
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              {txt("Tentar novamente", "Try again")}
            </Button>
          )}
        </div>
      )}
      {/* Mounted empty from the first render so a notice written into it is
          announced; it takes the status role and the visible box only while
          it holds a message (empty, `-mt-2` cancels the header spacing). */}
      <div
        data-live-region={BILLING_STATUS_REGION}
        aria-live="polite"
        aria-atomic="true"
        {...(statusNotice ? { role: "status", "data-testid": "billing-context-alert" } : {})}
        className={
          statusNotice
            ? "flex flex-wrap items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm text-amber-800 dark:text-amber-300"
            : "-mt-2"
        }
      >
        {statusNotice && (
          <>
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            <span className="flex-1">{statusNotice}</span>
          </>
        )}
      </div>
    </header>
  );

  // Denied access shows nothing financial: no plan, catalog, usage or credit.
  if (accessDenied) {
    return <div className="space-y-8 max-w-4xl">{header}</div>;
  }

  return (
    <div className="space-y-8 max-w-4xl">
      {header}

      {/* ── Hosted checkout return / tracked session (server state only) ── */}
      {returnedFromCheckout && (
        <Card className="border-amber-500/30 bg-amber-500/5">
          <CardContent className="pt-5 flex items-start gap-3 text-sm">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400" />
            <span>
              {txt(
                "Você voltou do checkout hospedado. O retorno não confirma pagamento: o estado exibido é relido do servidor e só muda depois que o provedor confirmar.",
                "You returned from the hosted checkout. The return does not confirm payment: the state shown is re-read from the server and changes only after the provider confirms.",
              )}
            </span>
          </CardContent>
        </Card>
      )}
      {sessionError && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="pt-5 space-y-3 text-sm">
            <p role="alert" className="text-destructive">{sessionError}</p>
            {unreadSessionId && !trackedSession && (
              <>
                <p className="text-xs text-muted-foreground">
                  {txt(
                    "Um checkout iniciado nesta sessão ainda não foi lido do servidor. Novas assinaturas ficam bloqueadas até essa leitura.",
                    "A checkout started in this session has not been read from the server yet. New subscriptions stay blocked until it is read.",
                  )}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  disabled={sessionLoading}
                  onClick={() => void fetchSession(unreadSessionId)}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  {txt("Tentar ler novamente", "Try reading again")}
                </Button>
              </>
            )}
          </CardContent>
        </Card>
      )}
      {shownSession && (
        <Card
          ref={checkoutStatusRef}
          tabIndex={-1}
          aria-labelledby={checkoutStatusLabelId}
          className="border-amber-500/30 bg-amber-500/5 focus:outline-none"
          role="status"
          data-testid="checkout-status"
        >
          <CardContent className="pt-5 space-y-3 text-sm">
            <p id={checkoutStatusLabelId} className="font-semibold text-foreground">
              {checkoutStateLabel(shownSession.state, txt)}
            </p>
            <p className="text-xs text-muted-foreground">
              {shownSession.kind === "topup"
                ? txt(
                    "Este estado vem do servidor. Os créditos só entram no saldo depois que o pagamento for confirmado e registrado; o retorno do checkout ou o QR do PIX não adicionam créditos.",
                    "This state comes from the server. Credits only reach the balance once the payment is confirmed and recorded; returning from checkout or a PIX QR code adds no credits.",
                  )
                : txt(
                    "Este estado vem do servidor. A assinatura só aparece como ativa depois da confirmação do provedor.",
                    "This state comes from the server. The subscription only shows as active after the provider confirms.",
                  )}
            </p>
            {shownSession.state === "reconciliation_required" && (
              <p className="text-xs font-medium text-amber-700 dark:text-amber-400">
                {txt(
                  "Não inicie outro checkout enquanto esta verificação estiver aberta.",
                  "Do not start another checkout while this verification is open.",
                )}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {trackedCheckoutUrl && (
                <Button size="sm" className="gap-1.5" onClick={() => navigateToHostedCheckout(trackedCheckoutUrl)}>
                  <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                  {txt("Abrir checkout hospedado", "Open hosted checkout")}
                </Button>
              )}
              {shownIsLive && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  disabled={sessionLoading}
                  onClick={() => {
                    void fetchSession(shownSession.checkout_session_id);
                    void fetchSummary();
                    void fetchCredit();
                  }}
                >
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  {txt("Atualizar estado", "Refresh state")}
                </Button>
              )}
              {(shownSession.state === "creating" || shownSession.state === "reconciliation_required") && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  disabled={sessionLoading}
                  onClick={() => void fetchSession(shownSession.checkout_session_id, true)}
                >
                  <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
                  {txt("Verificar com o provedor", "Verify with the provider")}
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Area 01: Assinatura e uso ───────────────────────────────────── */}
      <section aria-labelledby="billing-area-subscription" className="space-y-3" data-testid="billing-area">
        <AreaHeading id="billing-area-subscription" num="01" label={txt("Assinatura e uso", "Subscription and usage")} />
        <SubscriptionOverview state={overviewState} locale={locale} />
        <PlanUsageSummary
          state={usageState}
          locale={locale}
          variant="billing"
        />
      </section>

      {summarySettled && (
        <>
          {/* ── Area 02: Planos ─────────────────────────────────────────────── */}
          <section aria-labelledby="billing-area-plans" className="space-y-3" data-testid="billing-area">
            <AreaHeading ref={plansHeadingRef} id="billing-area-plans" num="02" label={txt("Planos", "Plans")} />
            <PlanComparison
              state={comparisonState}
              locale={locale}
              onSubscribe={openCheckout}
              onRetry={() => {
                plansHeadingRef.current?.focus();
                void fetchCatalog();
              }}
              actionBlockedReason={checkoutBlockedReason}
              notice={plansNotice}
            />

            {showCheckout && selectedPlan && !checkoutBlocked && (
              <div ref={checkoutRef}>
                <Card className="border-emerald-500/20 bg-card">
                  <CardHeader className="pb-3">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <CreditCard className="h-4 w-4 text-emerald-500" aria-hidden="true" />
                        <CardTitle className="text-sm font-semibold text-foreground">
                          {txt(`Finalizar Assinatura NeuroStack ${selectedPlan.name}`, `Complete NeuroStack ${selectedPlan.name} Subscription`)}
                        </CardTitle>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={txt("Fechar", "Close")}
                        onClick={() => setShowCheckout(false)}
                        className="h-7 w-7 p-0"
                      >
                        <X className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-5">
                    {/* Monthly card only, on the provider-hosted checkout */}
                    <p className="text-xs text-muted-foreground bg-muted/30 rounded-lg px-3 py-2">
                      {txt(
                        "Assinatura mensal no cartão de crédito. Os dados do cartão são informados somente no checkout hospedado do ASAAS, que exibe o valor final calculado pelo servidor.",
                        "Monthly credit card subscription. Card details are entered only on the ASAAS-hosted checkout, which shows the final amount computed by the server.",
                      )}
                    </p>

                    {/* Pay button */}
                    <Button
                      className="w-full gap-2"
                      onClick={() => void handleCheckout()}
                      disabled={checkoutLoading}
                    >
                      {checkoutLoading ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <ArrowRight className="h-4 w-4" />
                      )}
                      {txt("Continuar para o checkout seguro", "Continue to secure checkout")}
                    </Button>
                    {checkoutError && (
                      <p role="alert" className="text-sm text-destructive mt-2">{checkoutError}</p>
                    )}
                  </CardContent>
                </Card>
              </div>
            )}
          </section>

          {/* ── Area 03: Créditos e recargas (absent while credits are not launched) ── */}
          {creditsLaunched && (
            <section aria-labelledby="billing-area-credits" className="space-y-3" data-testid="billing-area">
              <AreaHeading
                ref={creditsHeadingRef}
                id="billing-area-credits"
                num="03"
                label={txt("Créditos e recargas", "Credits and top-ups")}
              />
              <CreditsAndTopups
                state={creditsState}
                history={historyState}
                topup={topup}
                locale={locale}
                onRetry={() => {
                  creditsHeadingRef.current?.focus();
                  void fetchCredit();
                }}
                onRetryHistory={() => {
                  creditsHeadingRef.current?.focus();
                  void fetchLots();
                }}
                exportAction={{
                  onExport: () => void handleExport(),
                  disabled: exportState === "unavailable",
                  loading: exportState === "loading",
                  message:
                    exportState === "unavailable"
                      ? txt("Exportação indisponível no momento.", "Export is unavailable right now.")
                      : exportState === "error"
                        ? txt("Não foi possível exportar agora. Tente novamente.", "The export failed. Try again.")
                        : null,
                }}
                onBuy={(offerId, method) => void handleTopup(offerId, method)}
                buying={topupLoading}
                buyError={topupError}
              />
            </section>
          )}
        </>
      )}
    </div>
  );
}
