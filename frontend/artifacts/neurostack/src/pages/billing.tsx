import { useState, useEffect, useCallback } from "react";
import {
  CreditCard,
  CheckCircle2,
  Wallet,
  Zap,
  FileText,
  Download,
  Plus,
  Star,
  Loader2,
  Copy,
  ArrowRight,
  Tag,
  X,
  Clock,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/context/i18n";

// ── Types ────────────────────────────────────────────────────────────────────

interface BillingSummary {
  plan: string | null;
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

interface CheckoutResult {
  ok: boolean;
  method: string;
  qr_code?: string;
  qr_code_url?: string;
  payment_url?: string;
  subscription_id?: string;
}

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

function formatBRL(value: number) {
  return new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", minimumFractionDigits: 0 }).format(value);
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
  const txt = (p: string, e: string) => (locale === "en" ? e : p);
  const token = useToken();

  // State
  const [billingCycle, setBillingCycle] = useState<"monthly" | "annual">("monthly");
  const [summary, setSummary] = useState<BillingSummary | null>(null);
  const [loading, setLoading] = useState(true);

  // Checkout state
  const [showCheckout, setShowCheckout] = useState(false);
  const [paymentMethod, setPaymentMethod] = useState<"pix" | "card">("pix");
  const [installments, setInstallments] = useState(12);
  const [couponCode, setCouponCode] = useState("");
  const [couponDiscount, setCouponDiscount] = useState<number | null>(null);
  const [couponError, setCouponError] = useState("");
  const [couponLoading, setCouponLoading] = useState(false);
  const [checkoutLoading, setCheckoutLoading] = useState(false);
  const [checkoutResult, setCheckoutResult] = useState<CheckoutResult | null>(null);
  const [qrCopied, setQrCopied] = useState(false);

  // Fetch summary
  const fetchSummary = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/v1/billing/summary", { headers: authHeaders(token) });
      if (res.ok) {
        const data = await res.json();
        setSummary(data);
        setBillingCycle(data.billing_cycle ?? "monthly");
      }
    } catch {
      // fallback handled by defaults
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void fetchSummary(); }, [fetchSummary]);

  // Pricing
  const activeClinics = summary?.active_clinics ?? 0;
  const freeTier = summary?.free_tier ?? 2;
  const extraClinics = summary?.extra_clinics ?? 0;
  const totalMonthly = summary?.total_monthly ?? 2000;
  const totalAnnual = summary?.total_annual ?? 20400;
  const subscriptionStatus = summary?.subscription_status ?? "trial";

  const displayPrice = billingCycle === "monthly" ? totalMonthly : totalAnnual;
  const annualSavings = totalMonthly * 12 - totalAnnual;

  // Price with coupon
  const priceAfterCoupon = couponDiscount
    ? Math.round(displayPrice * (1 - couponDiscount / 100))
    : displayPrice;

  // Validate coupon
  const handleApplyCoupon = async () => {
    if (!couponCode.trim()) return;
    setCouponLoading(true);
    setCouponError("");
    setCouponDiscount(null);
    try {
      const res = await fetch(`/v1/billing/coupon/${encodeURIComponent(couponCode.trim())}`, {
        headers: authHeaders(token),
      });
      const data = await res.json();
      if (data.valid) {
        setCouponDiscount(data.discount_percent);
        setCouponError("");
      } else {
        setCouponError(data.message || txt("Cupom inválido", "Invalid coupon"));
        setCouponDiscount(null);
      }
    } catch {
      setCouponError(txt("Erro ao validar cupom", "Error validating coupon"));
    } finally {
      setCouponLoading(false);
    }
  };

  // Checkout
  const handleCheckout = async () => {
    setCheckoutLoading(true);
    setCheckoutResult(null);
    try {
      const res = await fetch("/v1/billing/checkout", {
        method: "POST",
        headers: authHeaders(token),
        body: JSON.stringify({
          plan: "start",
          cycle: billingCycle,
          method: paymentMethod,
          coupon_code: couponDiscount ? couponCode.trim() : undefined,
          installments: paymentMethod === "card" && billingCycle === "annual" ? installments : undefined,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        setCheckoutResult(data);
        if (data.payment_url && data.method === "card") {
          window.open(data.payment_url, "_blank");
        }
      }
    } catch {
      // network error
    } finally {
      setCheckoutLoading(false);
    }
  };

  const copyQR = () => {
    if (checkoutResult?.qr_code) {
      void navigator.clipboard.writeText(checkoutResult.qr_code);
      setQrCopied(true);
      setTimeout(() => setQrCopied(false), 2000);
    }
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
                <span className="text-foreground/60 font-normal text-lg ml-2">(Billing)</span>
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

      {/* ── Section 01: Plano Atual ──────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Plano Atual", "Current Plan")} />

        {/* Toggle mensal / anual */}
        <div className="flex items-center justify-center gap-2">
          <button
            onClick={() => setBillingCycle("monthly")}
            className={`px-4 py-2 rounded-l-lg text-sm font-semibold transition-colors ${
              billingCycle === "monthly"
                ? "bg-emerald-500 text-white"
                : "bg-muted text-muted-foreground hover:bg-muted/80"
            }`}
          >
            {txt("Mensal", "Monthly")}
          </button>
          <button
            onClick={() => setBillingCycle("annual")}
            className={`px-4 py-2 rounded-r-lg text-sm font-semibold transition-colors flex items-center gap-1.5 ${
              billingCycle === "annual"
                ? "bg-emerald-500 text-white"
                : "bg-muted text-muted-foreground hover:bg-muted/80"
            }`}
          >
            {txt("Anual", "Annual")}
            <Badge className="bg-amber-500/20 text-amber-400 border-amber-500/30 text-[9px] px-1.5">
              {txt("Economize 15%", "Save 15%")}
            </Badge>
          </button>
        </div>

        {loading ? (
          <Card className="border-border/60 bg-card">
            <CardContent className="pt-5 flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-emerald-500" />
            </CardContent>
          </Card>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {/* NeuroStack Start — active */}
            <Card className="border-emerald-500/30 bg-gradient-to-b from-emerald-500/5 to-transparent relative">
              <div className="absolute top-3 right-3">
                <Badge className="bg-emerald-500/15 text-emerald-400 border-emerald-500/30 text-[9px]">
                  {txt("Disponível", "Available")}
                </Badge>
              </div>
              <CardContent className="pt-6 space-y-4">
                <div className="flex items-center gap-2">
                  <Star className="h-5 w-5 text-emerald-500" />
                  <p className="text-lg font-bold text-foreground">NeuroStack Start</p>
                </div>
                <div>
                  <p className="text-3xl font-bold text-foreground">
                    {formatBRL(billingCycle === "monthly" ? totalMonthly : totalAnnual)}
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    {billingCycle === "monthly"
                      ? txt("/mês", "/month")
                      : txt("/ano", "/year")}
                  </p>
                  {billingCycle === "annual" && (
                    <p className="text-xs text-emerald-400 mt-1">
                      {txt(`Economia de ${formatBRL(annualSavings)}/ano`, `Save ${formatBRL(annualSavings)}/year`)}
                    </p>
                  )}
                </div>
                <div className="text-xs text-muted-foreground space-y-1.5 border-t border-border/40 pt-3">
                  <p>+ {formatBRL(350)}{txt("/clínica acima de 2 inclusas", "/clinic above 2 included")}</p>
                  <p className="text-foreground/80 font-medium">
                    {txt(
                      `Você tem ${activeClinics} clínica(s) ativa(s) — ${Math.min(activeClinics, freeTier)} inclusa(s), ${extraClinics} cobrada(s)`,
                      `You have ${activeClinics} active clinic(s) — ${Math.min(activeClinics, freeTier)} included, ${extraClinics} charged`
                    )}
                  </p>
                </div>
                <Button
                  className="w-full gap-2"
                  onClick={() => { setShowCheckout(true); setCheckoutResult(null); }}
                >
                  <Zap className="h-4 w-4" />
                  {subscriptionStatus === "active"
                    ? txt("Alterar Plano", "Change Plan")
                    : txt("Assinar agora", "Subscribe now")}
                </Button>
              </CardContent>
            </Card>

            {/* Coming Soon cards */}
            {["Growth", "Scale", "API SRM"].map((plan) => (
              <Card key={plan} className="border-border/40 bg-card opacity-60 cursor-not-allowed">
                <CardContent className="pt-6 space-y-4">
                  <div className="flex items-center justify-between">
                    <p className="text-lg font-bold text-muted-foreground">NeuroStack {plan}</p>
                    <Badge variant="outline" className="text-[9px] text-muted-foreground border-border/50">
                      {txt("Em breve", "Coming soon")}
                    </Badge>
                  </div>
                  <div>
                    <p className="text-2xl font-bold text-muted-foreground/50">—</p>
                  </div>
                  <Button disabled variant="outline" className="w-full opacity-50 cursor-not-allowed">
                    {txt("Em breve", "Coming soon")}
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* ── Section 02: Credits (skeleton) ────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Recarga de Créditos (Add-ons)", "Credit Top-Up (Add-ons)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Wallet className="h-4 w-4 text-emerald-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Saldo de Créditos Extras", "Extra Credits Balance")}
              </CardTitle>
              <Badge variant="outline" className="text-[9px] text-muted-foreground ml-auto">Roadmap</Badge>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <Button disabled size="sm" variant="outline" className="gap-2 opacity-50 cursor-not-allowed border-border/60 w-full">
              <Plus className="h-3.5 w-3.5" />
              {txt("Adicionar Créditos (Pacotes Avulsos)", "Add Credits (Individual Packages)")}
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* ── Section 03: Checkout ──────────────────────────────────────────── */}
      {showCheckout && (
        <div className="space-y-3">
          <SectionDivider num="03" label={txt("Checkout / Pagamento", "Checkout / Payment")} />
          <Card className="border-emerald-500/20 bg-card">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <CreditCard className="h-4 w-4 text-emerald-500" />
                  <CardTitle className="text-sm font-semibold text-foreground">
                    {txt("Finalizar Assinatura", "Complete Subscription")}
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
                <label className="text-xs font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1.5">
                  <Tag className="h-3 w-3" />
                  {txt("Cupom de Desconto", "Discount Coupon")}
                </label>
                <div className="flex gap-2">
                  <Input
                    value={couponCode}
                    onChange={(e) => setCouponCode(e.target.value.toUpperCase())}
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
                {couponDiscount && (
                  <div className="flex items-center gap-2 text-xs">
                    <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                    <span className="text-emerald-400 font-semibold">
                      {txt(`Cupom aplicado: ${couponDiscount}% de desconto`, `Coupon applied: ${couponDiscount}% off`)}
                    </span>
                    <button onClick={() => { setCouponDiscount(null); setCouponCode(""); }} className="text-muted-foreground hover:text-foreground">
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                )}
              </div>

              {/* Price summary */}
              <div className="rounded-lg border border-border/50 bg-muted/20 p-4 space-y-2">
                <div className="flex justify-between text-sm">
                  <span className="text-muted-foreground">
                    NeuroStack Start — {billingCycle === "monthly" ? txt("Mensal", "Monthly") : txt("Anual", "Annual")}
                  </span>
                  <span className={`font-semibold ${couponDiscount ? "line-through text-muted-foreground" : "text-foreground"}`}>
                    {formatBRL(displayPrice)}
                  </span>
                </div>
                {couponDiscount && (
                  <div className="flex justify-between text-sm">
                    <span className="text-emerald-400">{txt("Com desconto", "With discount")} ({couponDiscount}%)</span>
                    <span className="font-bold text-emerald-400">{formatBRL(priceAfterCoupon)}</span>
                  </div>
                )}
              </div>

              {/* Payment method selector */}
              <div className="space-y-2">
                <label className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                  {txt("Método de Pagamento", "Payment Method")}
                </label>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    onClick={() => setPaymentMethod("pix")}
                    className={`flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-semibold transition-colors ${
                      paymentMethod === "pix"
                        ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-400"
                        : "border-border/50 text-muted-foreground hover:bg-muted/30"
                    }`}
                  >
                    <Zap className="h-4 w-4" /> PIX
                  </button>
                  <button
                    onClick={() => setPaymentMethod("card")}
                    className={`flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-semibold transition-colors ${
                      paymentMethod === "card"
                        ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-400"
                        : "border-border/50 text-muted-foreground hover:bg-muted/30"
                    }`}
                  >
                    <CreditCard className="h-4 w-4" /> {txt("Cartão de Crédito", "Credit Card")}
                  </button>
                </div>
              </div>

              {/* Installments — only annual + card */}
              {paymentMethod === "card" && billingCycle === "annual" && (
                <div className="space-y-2">
                  <label className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                    {txt("Parcelas (sem juros)", "Installments (interest-free)")}
                  </label>
                  <select
                    value={installments}
                    onChange={(e) => setInstallments(Number(e.target.value))}
                    className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-emerald-500"
                  >
                    {Array.from({ length: 12 }, (_, i) => i + 1).map((n) => (
                      <option key={n} value={n}>
                        {n}x {txt("de", "of")} {formatBRL(Math.round(priceAfterCoupon / n))} {txt("sem juros", "interest-free")}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {/* Recurrence info — monthly + card */}
              {paymentMethod === "card" && billingCycle === "monthly" && (
                <p className="text-xs text-muted-foreground bg-muted/30 rounded-lg px-3 py-2">
                  {txt(
                    "AbacatePay debita automaticamente todo mês na mesma data — Subscription MONTHLY",
                    "AbacatePay charges automatically every month on the same date — MONTHLY Subscription"
                  )}
                </p>
              )}
              {paymentMethod === "pix" && billingCycle === "monthly" && (
                <p className="text-xs text-muted-foreground bg-muted/30 rounded-lg px-3 py-2">
                  {txt(
                    "AbacatePay envia PIX a cada ciclo mensal — Subscription MONTHLY",
                    "AbacatePay sends PIX each monthly cycle — MONTHLY Subscription"
                  )}
                </p>
              )}
              {paymentMethod === "pix" && billingCycle === "annual" && (
                <p className="text-xs text-muted-foreground bg-muted/30 rounded-lg px-3 py-2">
                  {txt(
                    "PIX único anual — Subscription YEARLY",
                    "Single annual PIX — YEARLY Subscription"
                  )}
                </p>
              )}

              {/* Pay button */}
              {!checkoutResult && (
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
                  {txt("Pagar", "Pay")} {formatBRL(priceAfterCoupon)}
                </Button>
              )}

              {/* PIX QR result */}
              {checkoutResult && checkoutResult.qr_code && (
                <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-5 space-y-4 text-center">
                  <p className="text-sm font-semibold text-emerald-400">
                    {txt("Escaneie o QR Code ou copie o código PIX", "Scan the QR Code or copy the PIX code")}
                  </p>
                  {checkoutResult.qr_code_url && (
                    <div className="flex justify-center">
                      <img src={checkoutResult.qr_code_url} alt="QR Code PIX" className="h-48 w-48 rounded-lg border border-border" />
                    </div>
                  )}
                  <div className="flex gap-2 items-center">
                    <Input
                      readOnly
                      value={checkoutResult.qr_code}
                      className="font-mono text-[10px] text-muted-foreground"
                    />
                    <Button variant="outline" size="sm" onClick={copyQR} className="shrink-0 gap-1.5">
                      <Copy className="h-3.5 w-3.5" />
                      {qrCopied ? txt("Copiado!", "Copied!") : txt("Copiar", "Copy")}
                    </Button>
                  </div>
                </div>
              )}

              {/* Card redirect result */}
              {checkoutResult && checkoutResult.method === "card" && checkoutResult.payment_url && (
                <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-5 space-y-3 text-center">
                  <p className="text-sm font-semibold text-emerald-400">
                    {txt("Redirecionando para o pagamento...", "Redirecting to payment...")}
                  </p>
                  <Button variant="outline" size="sm" onClick={() => window.open(checkoutResult.payment_url!, "_blank")} className="gap-1.5">
                    <ArrowRight className="h-3.5 w-3.5" />
                    {txt("Abrir link de pagamento", "Open payment link")}
                  </Button>
                </div>
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
