import { useState, useEffect, useCallback, useRef } from "react";
import {
  CreditCard,
  CheckCircle2,
  Wallet,
  Zap,
  FileText,
  Plus,
  Star,
  Loader2,
  ArrowRight,
  Tag,
  X,
  Clock,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/context/i18n";
import {
  PlanUsageSummary,
  type BillingUsage,
} from "@/components/plan-usage-summary";
import { navigateToHostedCheckout } from "./billing-navigation";

// ── Types ────────────────────────────────────────────────────────────────────

interface BillingSummary {
  plan: string | null;
  usage: BillingUsage | null;
  active_clinics: number;
  free_tier: number;
  extra_clinics: number;
  base_price: number;
  extra_price: number;
  total_monthly: number;
  total_annual: number;
  discount_pct: number;
  subscription_status: string;
  billing_cycle: string;
}

// `CheckoutResponse` in `src/api/schemas/billing.py`.
interface CheckoutSession {
  ok?: boolean;
  checkout_session_id: string;
  kind: "subscription" | "topup";
  state: string;
  checkout_url?: string | null;
}

interface SubscriptionPlan {
  offerId: string;
  name: "Plus" | "Pro" | "Max";
}

// Monthly offers of the immutable catalog in `src/services/billing_catalog.py`.
// Only `offer_id` is sent: price, cycle, clinics and coupon terms are resolved
// by the server, and the amount is shown on the hosted checkout.
const SUBSCRIPTION_PLANS: readonly SubscriptionPlan[] = [
  { offerId: "plan_plus_monthly_v1", name: "Plus" },
  { offerId: "plan_pro_monthly_v1", name: "Pro" },
  { offerId: "plan_max_monthly_v1", name: "Max" },
];

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
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// ── Helpers ──────────────────────────────────────────────────────────────────

function useToken() {
  const raw = localStorage.getItem("ns_auth");
  try {
    return (JSON.parse(raw ?? "{}") as { token?: string }).token ?? "";
  } catch {
    return "";
  }
}

function authHeaders(token: string) {
  return { "Content-Type": "application/json", Authorization: `Bearer ${token}` };
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

function isCheckoutSession(value: unknown): value is CheckoutSession {
  if (!value || typeof value !== "object") return false;
  const data = value as Record<string, unknown>;
  return (
    typeof data.checkout_session_id === "string" &&
    UUID_PATTERN.test(data.checkout_session_id) &&
    (data.kind === "subscription" || data.kind === "topup") &&
    typeof data.state === "string" &&
    (data.checkout_url === undefined ||
      data.checkout_url === null ||
      typeof data.checkout_url === "string")
  );
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

async function readDetail(res: Response): Promise<string | null> {
  try {
    const data = (await res.json()) as { detail?: unknown };
    return typeof data.detail === "string" ? data.detail : null;
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

type Txt = (p: string, e: string) => string;

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

/** Simple translated copy per route outcome; backend codes are never shown. */
function checkoutErrorMessage(status: number, detail: string | null, txt: Txt) {
  switch (status) {
    case 202:
      return txt(
        "O servidor não conseguiu confirmar se o checkout foi criado no provedor. Não pague novamente. Para retomar o checkout existente, clique em assinar de novo no mesmo plano: o servidor devolve a sessão já aberta. Ou aguarde alguns minutos e atualize a página.",
        "The server could not confirm whether the checkout was created with the provider. Do not pay again. To resume the existing checkout, click subscribe again on the same plan: the server returns the session already open. Or wait a few minutes and refresh the page.",
      );
    case 400:
      return detail === "billing_checkout_coupon_not_eligible"
        ? txt(
            "Este cupom não pode ser usado nesta assinatura. Nenhuma cobrança foi criada.",
            "This coupon cannot be used for this subscription. No charge was created.",
          )
        : txt(
            "Cupom inválido, expirado ou esgotado. Nenhuma cobrança foi criada.",
            "Invalid, expired or exhausted coupon. No charge was created.",
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

function SectionDivider({ num, label }: { num: string; label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/20 shrink-0">
        <span className="text-[9px] font-bold text-emerald-500">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-emerald-500">{label}</span>
      <div className="flex-1 h-px bg-emerald-500/20" />
    </div>
  );
}

// ── Main Component ───────────────────────────────────────────────────────────

export default function Billing() {
  const { locale } = useI18n();
  const txt = useCallback<Txt>((p, e) => (locale === "en" ? e : p), [locale]);
  const token = useToken();

  // State
  const [summary, setSummary] = useState<BillingSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [returnedFromCheckout] = useState(hasReturnMarker);

  // Checkout state
  const [showCheckout, setShowCheckout] = useState(false);
  const [selectedPlan, setSelectedPlan] = useState<SubscriptionPlan | null>(null);
  const [couponCode, setCouponCode] = useState("");
  const [validatedCoupon, setValidatedCoupon] = useState<{ code: string; percent: number } | null>(null);
  const [couponError, setCouponError] = useState("");
  const [couponLoading, setCouponLoading] = useState(false);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);
  const [trackedSession, setTrackedSession] = useState<CheckoutSession | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  // A stored session id the server has not yet answered for: subscribing stays
  // blocked until it is read (or the server says it does not exist).
  const [unreadSessionId, setUnreadSessionId] = useState<string | null>(readStoredSessionId);
  const checkoutRef = useRef<HTMLDivElement>(null);

  // Fetch summary
  const fetchSummary = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/v1/billing/summary", { headers: authHeaders(token) });
      if (res.ok) {
        setSummary(await res.json());
      }
    } catch {
      // Checkout stays unavailable until the server state is read.
    } finally {
      setLoading(false);
    }
  }, [token]);

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
    const storedSessionId = readStoredSessionId();
    if (storedSessionId) void fetchSession(storedSessionId);
  }, [fetchSummary, fetchSession]);

  // Plan values are intentionally not displayed until pricing is finalized;
  // the backend computes the checkout amount, shown on the hosted checkout.
  const subscriptionStatus = summary?.subscription_status ?? "trial";
  const sessionInProgress =
    trackedSession !== null && LIVE_CHECKOUT_STATES.has(trackedSession.state);
  const checkoutBlocked =
    summary === null ||
    summary.subscription_status === "active" ||
    sessionInProgress ||
    unreadSessionId !== null;
  const trackedCheckoutUrl = hostedCheckoutUrl(trackedSession);

  // Validate coupon
  const handleApplyCoupon = async () => {
    const code = couponCode.trim();
    if (!code) return;
    setCouponLoading(true);
    setCouponError("");
    setValidatedCoupon(null);
    try {
      const res = await fetch(`/v1/billing/coupon/${encodeURIComponent(code)}`, {
        headers: authHeaders(token),
      });
      const data = await res.json();
      if (res.ok && data.valid && typeof data.discount_percent === "number") {
        setValidatedCoupon({ code: data.code ?? code, percent: data.discount_percent });
        setCouponError("");
      } else {
        setCouponError(data.message || txt("Cupom inválido", "Invalid coupon"));
      }
    } catch {
      setCouponError(txt("Erro ao validar cupom", "Error validating coupon"));
    } finally {
      setCouponLoading(false);
    }
  };

  // Checkout: exactly one POST; the browser never activates anything.
  const handleCheckout = async () => {
    if (!selectedPlan || checkoutBlocked) return;
    setCheckoutLoading(true);
    setCheckoutError(null);
    try {
      const res = await fetch("/v1/billing/checkout", {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({
          offer_id: selectedPlan.offerId,
          method: "credit_card",
          ...(validatedCoupon ? { coupon_code: validatedCoupon.code } : {}),
        }),
      });
      // 202 is an `ok` status but carries only a reconciliation code.
      if (res.status !== 200) {
        const detail = await readDetail(res);
        if (res.status === 400) setValidatedCoupon(null);
        setCheckoutError(checkoutErrorMessage(res.status, detail, txt));
        return;
      }
      const data: unknown = await res.json();
      if (!isCheckoutSession(data)) {
        setCheckoutError(checkoutErrorMessage(0, null, txt));
        return;
      }
      setTrackedSession(data);
      storeSessionId(LIVE_CHECKOUT_STATES.has(data.state) ? data.checkout_session_id : null);
      const url = hostedCheckoutUrl(data);
      if (url) navigateToHostedCheckout(url);
    } catch {
      setCheckoutError(checkoutErrorMessage(0, null, txt));
    } finally {
      setCheckoutLoading(false);
    }
  };

  const openCheckout = (plan: SubscriptionPlan) => {
    if (checkoutBlocked) return;
    setSelectedPlan(plan);
    setShowCheckout(true);
    setCheckoutError(null);
    setTimeout(() => checkoutRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8 max-w-4xl">
      {/* ── Page header ──────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-emerald-500/10 border border-emerald-500/20">
              <CreditCard className="h-5 w-5 text-emerald-500" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                {txt("Faturamento e Gestão de Assinatura", "Billing & Subscription Management")}
              </h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Gerencie seu plano, pagamentos e assinatura NeuroStack",
                  "Manage your NeuroStack plan, payments and subscription"
                )}
              </p>
            </div>
          </div>
          {subscriptionStatus === "active" && (
            <Badge className="shrink-0 mt-1 bg-emerald-500/15 border-emerald-500/30 text-emerald-400 text-[10px] uppercase tracking-wider px-3 py-1.5">
              <CheckCircle2 className="h-3 w-3 mr-1.5" />
              {txt("Assinatura Ativa", "Active Subscription")}
            </Badge>
          )}
          {subscriptionStatus === "trial" && (
            <Badge variant="outline" className="shrink-0 mt-1 bg-amber-500/10 border-amber-500/30 text-amber-400 text-[10px] uppercase tracking-wider px-3 py-1.5">
              <Clock className="h-3 w-3 mr-1.5" />
              Trial
            </Badge>
          )}
        </div>
      </div>

      {/* ── Hosted checkout return / tracked session (server state only) ── */}
      {returnedFromCheckout && (
        <Card className="border-amber-500/30 bg-amber-500/5">
          <CardContent className="pt-5 flex items-start gap-3 text-sm">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
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
      {trackedSession && (
        <Card className="border-amber-500/30 bg-amber-500/5" role="status">
          <CardContent className="pt-5 space-y-3 text-sm">
            <p className="font-semibold text-foreground">{checkoutStateLabel(trackedSession.state, txt)}</p>
            <p className="text-xs text-muted-foreground">
              {txt(
                "Este estado vem do servidor. A assinatura só aparece como ativa depois da confirmação do provedor.",
                "This state comes from the server. The subscription only shows as active after the provider confirms.",
              )}
            </p>
            {trackedSession.state === "reconciliation_required" && (
              <p className="text-xs font-medium text-amber-400">
                {txt(
                  "Não inicie outro checkout enquanto esta verificação estiver aberta.",
                  "Do not start another checkout while this verification is open.",
                )}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {trackedCheckoutUrl && (
                <Button size="sm" className="gap-1.5" onClick={() => navigateToHostedCheckout(trackedCheckoutUrl)}>
                  <ArrowRight className="h-3.5 w-3.5" />
                  {txt("Abrir checkout hospedado", "Open hosted checkout")}
                </Button>
              )}
              {LIVE_CHECKOUT_STATES.has(trackedSession.state) && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  disabled={sessionLoading}
                  onClick={() => {
                    void fetchSession(trackedSession.checkout_session_id);
                    void fetchSummary();
                  }}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  {txt("Atualizar estado", "Refresh state")}
                </Button>
              )}
              {(trackedSession.state === "creating" || trackedSession.state === "reconciliation_required") && (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-1.5"
                  disabled={sessionLoading}
                  onClick={() => void fetchSession(trackedSession.checkout_session_id, true)}
                >
                  <ShieldCheck className="h-3.5 w-3.5" />
                  {txt("Verificar com o provedor", "Verify with the provider")}
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* ── Section 01: Plano Atual ──────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Plano Atual", "Current Plan")} />


        {loading ? (
          <Card className="border-border/60 bg-card">
            <CardContent className="pt-5 flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-emerald-500" />
            </CardContent>
          </Card>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {/* Monthly card subscriptions (catalog offers) */}
            {SUBSCRIPTION_PLANS.map((plan) => (
              <Card key={plan.offerId} className="border-emerald-500/30 bg-gradient-to-b from-emerald-500/5 to-transparent relative">
                <CardContent className="pt-6 space-y-4">
                  <div className="flex items-center gap-2">
                    <Star className="h-5 w-5 text-emerald-500" />
                    <p className="text-lg font-bold text-foreground">NeuroStack {plan.name}</p>
                  </div>
                  <div className="text-xs text-muted-foreground space-y-1.5 border-t border-border/40 pt-3">
                    <p className="text-foreground/80 font-medium">
                      {txt("Assinatura mensal no cartão de crédito", "Monthly credit card subscription")}
                    </p>
                  </div>
                  {subscriptionStatus === "active" ? (
                    <Button disabled variant="outline" className="w-full opacity-60 cursor-not-allowed">
                      {txt("Gerenciar Assinatura", "Manage Subscription")}
                    </Button>
                  ) : (
                    <Button
                      className="w-full gap-2"
                      disabled={checkoutBlocked}
                      onClick={() => openCheckout(plan)}
                    >
                      <Zap className="h-4 w-4" />
                      {txt(`Assinar ${plan.name}`, `Subscribe to ${plan.name}`)}
                    </Button>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>

      {summary?.usage && <PlanUsageSummary usage={summary.usage} locale={locale} />}

      {/* ── Section 02: Credits (skeleton) ────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Recarga de Créditos (Add-ons)", "Credit Top-Up (Add-ons)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Wallet className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Recargas de créditos", "Credit top-ups")}
              </CardTitle>
              <Badge variant="outline" className="text-[9px] text-muted-foreground ml-auto">
                {txt("Indisponível", "Unavailable")}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <Button disabled size="sm" variant="outline" className="gap-2 opacity-50 cursor-not-allowed border-border/60 w-full">
              <Plus className="h-3.5 w-3.5" />
              {txt("Recargas indisponíveis", "Top-ups unavailable")}
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* ── Section 03: Checkout ──────────────────────────────────────────── */}
      {showCheckout && selectedPlan && !checkoutBlocked && (
        <div ref={checkoutRef} className="space-y-3">
          <SectionDivider num="03" label={txt("Checkout / Pagamento", "Checkout / Payment")} />
          <Card className="border-emerald-500/20 bg-card">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <CreditCard className="h-4 w-4 text-emerald-500" />
                  <CardTitle className="text-sm font-semibold text-foreground">
                    {txt(`Finalizar Assinatura NeuroStack ${selectedPlan.name}`, `Complete NeuroStack ${selectedPlan.name} Subscription`)}
                  </CardTitle>
                </div>
                <Button variant="ghost" size="sm" onClick={() => setShowCheckout(false)} className="h-7 w-7 p-0">
                  <X className="h-4 w-4" />
                </Button>
              </div>
            </CardHeader>
            <CardContent className="space-y-5">
              {/* Coupon input */}
              <div className="space-y-2">
                <label htmlFor="billing-coupon" className="text-xs font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1.5">
                  <Tag className="h-3 w-3" />
                  {txt("Cupom de Desconto", "Discount Coupon")}
                </label>
                <div className="flex gap-2">
                  <Input
                    id="billing-coupon"
                    value={couponCode}
                    maxLength={50}
                    onChange={(e) => {
                      setCouponCode(e.target.value.toUpperCase());
                      setValidatedCoupon(null);
                    }}
                    placeholder={txt("Digite o código do cupom", "Enter coupon code")}
                    className="font-mono uppercase text-sm"
                  />
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void handleApplyCoupon()}
                    disabled={couponLoading || !couponCode.trim()}
                    className="shrink-0 px-4"
                  >
                    {couponLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : txt("Aplicar", "Apply")}
                  </Button>
                </div>
                {couponError && <p className="text-xs text-red-500 font-medium">{couponError}</p>}
                {validatedCoupon && (
                  <div className="flex items-center gap-2 text-xs">
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                    <span className="text-emerald-400 font-semibold">
                      {txt(`Cupom aplicado: ${validatedCoupon.percent}% de desconto`, `Coupon applied: ${validatedCoupon.percent}% off`)}
                    </span>
                    <button type="button" aria-label={txt("Remover cupom", "Remove coupon")} onClick={() => { setValidatedCoupon(null); setCouponCode(""); }} className="text-muted-foreground hover:text-foreground">
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                )}
              </div>

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

      {/* ── Section 04: Invoice History (skeleton) ────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="04" label={txt("Histórico de Faturas", "Invoice History")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Transações Recentes", "Recent Transactions")}
              </CardTitle>
              <Badge variant="outline" className="text-[9px] text-muted-foreground ml-auto">Roadmap</Badge>
            </div>
          </CardHeader>
          <CardContent>
            <div className="py-6 text-center">
              <FileText className="h-8 w-8 text-muted-foreground/30 mx-auto mb-2" />
              <p className="text-xs text-muted-foreground">
                {txt("Histórico de faturas estará disponível em breve", "Invoice history will be available soon")}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
