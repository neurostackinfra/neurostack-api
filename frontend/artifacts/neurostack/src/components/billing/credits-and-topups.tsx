import { useId, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  Coins,
  Download,
  Hourglass,
  Loader2,
  MinusCircle,
  PauseCircle,
  Plus,
  RefreshCw,
  Wallet,
  X,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import type { CreditLot, CreditStateInfo, CreditSummary } from "@/pages/billing-contract";
import { creditLedgerStanding } from "@/pages/billing-view";
import {
  formatCivilDate,
  formatCount,
  formatCredits,
  formatInstant,
  formatMoney,
  type Locale,
  type Txt,
} from "./format";

// Credits and top-ups of the payment screen. Presentational: no fetch, no
// storage, no commercial decision. Every figure is the credit ledger's
// (`GET /v1/credits/*`), every offer is the server's, and whether a top-up
// may be bought arrives already decided (`TopupState`). A checkout return, a
// PIX QR code or a pending payment changes nothing here until the page re-reads
// the ledger.

/**
 * The credit summary read: each outcome renders its own content. The
 * `503 credit_unavailable` answer (credit switch off, a feature not launched)
 * has no state here: the page does not render the credits area at all.
 */
export type CreditsPanelState =
  | { status: "loading" }
  /** 401/403: nothing financial is shown. */
  | { status: "forbidden" }
  /** Another 5xx, a network failure or a malformed body: announced, retryable. */
  | { status: "error" }
  | { status: "ready"; summary: CreditSummary };

/** The lots read (`GET /v1/credits/lots`): the only history the contract has. */
export type CreditHistoryState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "ready"; lots: CreditLot[] };

export type TopupMethod = "credit_card" | "pix";

/** One package of `GET /v1/credits/catalog` the server lists as available now. */
export interface TopupPackageItem {
  offerId: string;
  credits: number;
  amountCents: number;
  currency: string;
  validityMonths: number;
  methods: TopupMethod[];
}

/** Decided by the page from server readbacks (`deriveTopupState`). */
export type TopupState =
  | { status: "hidden" }
  | { status: "blocked"; reason: string; announce?: boolean }
  | { status: "offered"; packages: TopupPackageItem[] };

export interface ExportAction {
  onExport: () => void;
  disabled: boolean;
  loading: boolean;
  message?: string | null;
}

/**
 * How the ledger's figures may be presented for a credit state; decided only
 * by `creditLedgerStanding` (`billing-view.ts`), the same rule that blocks the
 * top-up:
 * - `not_applicable`: a standalone tenant has no credit account; the API
 *   answers zeros that are not data, so nothing is shown as a balance.
 * - `pending` (on hold): the `pending` state, or any reason other than
 *   `standalone` under any lifecycle state (quarantine, reconciliation or a
 *   reason this UI does not know): no figure is presented as usable. The
 *   lifecycle's deadline and export information stays visible.
 * - `standing`: active, grace, suspended, deletion due without a hold: the
 *   balance is shown.
 */
export function creditStanding(state: CreditStateInfo): "not_applicable" | "pending" | "standing" {
  return creditLedgerStanding(state);
}

function pendingReasonLines(reasons: string[], txt: Txt): string[] {
  const lines: string[] = [];
  if (reasons.includes("quarantine")) {
    lines.push(
      txt(
        "Pendência financeira: um pagamento está em verificação.",
        "Financial pending: a payment is under verification.",
      ),
    );
  }
  if (reasons.includes("reconciliation")) {
    lines.push(
      txt(
        "Pendência financeira: o vínculo entre um pagamento e os seus créditos está em conciliação pelo servidor.",
        "Financial pending: the link between a payment and your credits is being reconciled by the server.",
      ),
    );
  }
  // An empty list or only reasons this UI does not know: generic, never
  // another reason's specific copy.
  if (lines.length === 0) {
    lines.push(
      txt(
        "Pendência financeira em verificação pelo servidor.",
        "Financial pending under server verification.",
      ),
    );
  }
  return lines;
}

function CreditMetric({
  label,
  value,
  tone = "text-foreground",
  hint,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}) {
  return (
    <div role="group" aria-label={label} className="min-w-0 rounded-lg border border-border/60 bg-muted/15 p-3">
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={`mt-0.5 text-sm font-bold tabular-nums ${tone}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[10px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** Unknown or missing date, the same wording everywhere. */
function unknownDate(txt: Txt) {
  return txt("data não informada", "date not provided");
}

/**
 * The lifecycle lines of grace, suspended and deletion due: the deadline and
 * the export date, which are always true. The "Recarregue ..." call to action
 * is written only when a `Recarregar` action really exists (`topupOffered`);
 * on hold, with the commercial flag off, a live checkout or unreadable
 * packages, the page does not tell the user to top up.
 */
function lifecycleLines(
  state: CreditStateInfo,
  locale: Locale,
  txt: Txt,
  topupOffered: boolean,
): { message: string; exportLine: string; severe: boolean } | null {
  const when = (value?: string | null) => formatInstant(value, locale) ?? unknownDate(txt);
  const exportLine = state.export_deadline_at
    ? txt(
        `Exporte seus dados até ${when(state.export_deadline_at)}.`,
        `Export your data by ${when(state.export_deadline_at)}.`,
      )
    : "";
  switch (state.state) {
    case "grace": {
      const lead = txt(
        `Créditos esgotados: o serviço continua como dívida até ${when(state.suspension_due_at)}.`,
        `Credits exhausted: service continues as debt until ${when(state.suspension_due_at)}.`,
      );
      return {
        message: !topupOffered ? lead : `${lead} ${txt("Recarregue para evitar a suspensão.", "Top up to avoid suspension.")}`,
        exportLine,
        severe: false,
      };
    }
    case "suspended": {
      const lead = txt("Serviço suspenso por falta de créditos.", "Service suspended for lack of credits.");
      return {
        message: !topupOffered ? lead : `${lead} ${txt("Recarregue para reativar.", "Top up to reactivate.")}`,
        exportLine,
        severe: true,
      };
    }
    case "deletion_due": {
      const lead = txt(
        "Prazo de retenção encerrado: o conteúdo pode ser removido.",
        "Retention deadline reached: content may be removed.",
      );
      return {
        message: !topupOffered ? lead : `${lead} ${txt("Recarregue para reativar.", "Top up to reactivate.")}`,
        exportLine,
        severe: true,
      };
    }
    default:
      return null;
  }
}

/** The state notice of a standing account (grace, suspended, deletion due). */
function StandingNotice({
  state,
  locale,
  txt,
  topupOffered,
}: {
  state: CreditStateInfo;
  locale: Locale;
  txt: Txt;
  topupOffered: boolean;
}) {
  const lines = lifecycleLines(state, locale, txt, topupOffered);
  if (!lines) return null;
  const { message, exportLine } = lines;
  const tone = lines.severe
    ? "border-red-500/40 bg-red-500/10 text-red-800 dark:text-red-300"
    : "border-amber-500/30 bg-amber-500/5 text-amber-800 dark:text-amber-300";
  return (
    <div
      data-testid="credit-state"
      data-state={state.state}
      className={`flex items-start gap-2 rounded-md border px-3 py-2 text-xs ${tone}`}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>
        {message} {exportLine}
      </span>
    </div>
  );
}

const CONSUMPTION_LABELS: Record<string, [string, string]> = {
  trace: ["Decisões", "Decisions"],
  rag: ["Base de conhecimento", "Knowledge base"],
  media: ["Mídias", "Media"],
  egress: ["Downloads", "Downloads"],
  maintenance: ["Manutenção", "Maintenance"],
};

function BalanceDetails({ summary, locale, txt }: { summary: CreditSummary; locale: Locale; txt: Txt }) {
  const credits = (mc: number) => `${formatCredits(mc, locale)} ${txt("créditos", "credits")}`;
  const { balance, debt, reservations, thresholds, consumption } = summary;
  const byCategory = new Map(reservations.reservations.map((item) => [item.category, item]));
  const consumptionItems = consumption.mode === "enforced" ? consumption.charged : consumption.observed;
  const windowStart = formatCivilDate(reservations.window.start, locale);
  const windowEnd = formatCivilDate(reservations.window.end, locale);
  return (
    <>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        <CreditMetric label={txt("Franquia mensal", "Monthly allowance")} value={credits(balance.monthly_available_mc)} />
        <CreditMetric label={txt("Recargas", "Top-ups")} value={credits(balance.topup_available_mc)} />
        <CreditMetric
          label={txt("Dívida", "Debt")}
          value={credits(balance.open_debt_mc)}
          tone={balance.open_debt_mc > 0 ? "text-red-700 dark:text-red-400" : "text-foreground"}
        />
        <CreditMetric
          label={txt("Reservas do ciclo", "Cycle reservations")}
          value={credits(balance.reserved_mc)}
          hint={txt("Base de conhecimento e mídias já debitadas", "Knowledge base and media already debited")}
        />
        <CreditMetric
          label={txt("Disponível líquido", "Net available")}
          value={credits(balance.net_available_mc)}
          // Success green only for a positive amount; zero (or less) is neutral.
          tone={balance.net_available_mc > 0 ? "text-emerald-700 dark:text-emerald-400" : "text-foreground"}
        />
        <CreditMetric
          label={txt("Franquia do próximo ciclo", "Next cycle allowance")}
          value={credits(balance.monthly_not_started_mc)}
          hint={txt("Liberada no início do ciclo", "Usable when the cycle starts")}
        />
      </div>

      {summary.expiries.length > 0 && (
        <div data-testid="credit-expiries" className="space-y-1">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            {txt("Vencimentos", "Expiries")}
          </p>
          {summary.expiries.slice(0, 4).map((expiry) => (
            <div key={expiry.lot_id} className="flex flex-wrap items-center justify-between gap-x-2 text-xs">
              <span className="flex items-center gap-1.5 text-muted-foreground">
                <Clock className="h-3 w-3" aria-hidden="true" />
                {expiry.kind === "monthly" ? txt("Franquia mensal", "Monthly allowance") : txt("Recarga", "Top-up")} ·{" "}
                {formatInstant(expiry.expires_at, locale) ?? unknownDate(txt)}
              </span>
              <span className="font-semibold tabular-nums">{credits(expiry.available_mc)}</span>
            </div>
          ))}
        </div>
      )}

      {debt.debts.length > 0 && (
        <div data-testid="credit-debts" className="space-y-1">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            {txt("Dívida em aberto", "Open debt")}
          </p>
          {debt.debts.map((item) => (
            <div key={item.debt_id} className="flex flex-wrap items-center justify-between gap-x-2 text-xs">
              <span className="text-muted-foreground">
                {item.source_kind === "reversal"
                  ? txt("Estorno de pagamento", "Payment refund")
                  : txt("Consumo acima do saldo", "Consumption beyond balance")}{" "}
                · {formatInstant(item.created_at, locale) ?? unknownDate(txt)}
              </span>
              <span className="font-semibold tabular-nums text-red-700 dark:text-red-400">{credits(item.open_mc)}</span>
            </div>
          ))}
        </div>
      )}

      <div className="space-y-1" data-testid="credit-reservations">
        <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
          {txt("Alocações do ciclo", "Cycle allocations")}
          {windowStart && windowEnd ? ` · ${windowStart} ${txt("a", "to")} ${windowEnd}` : ""}
        </p>
        {(["rag", "media", "egress"] as const).map((category) => {
          const item = byCategory.get(category);
          const label =
            category === "rag"
              ? txt("Base de conhecimento", "Knowledge base")
              : category === "media"
                ? txt("Mídias", "Media")
                : txt("Downloads excedentes", "Download overage");
          return (
            <div key={category} className="flex items-center justify-between gap-2 text-xs">
              <span className="text-muted-foreground">{label}</span>
              {/* The API always lists the three categories; a missing one is not zero. */}
              <span className="tabular-nums">{item ? credits(item.coverage_mc) : txt("Indisponível", "Unavailable")}</span>
            </div>
          );
        })}
      </div>

      <div className="space-y-1.5" data-testid="credit-thresholds">
        <div className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">{txt("Capacidade consumida", "Capacity used")}</span>
          <span className="font-semibold tabular-nums">{thresholds.used_percent}%</span>
        </div>
        <Progress
          value={Math.min(100, Math.max(0, thresholds.used_percent))}
          aria-label={txt("Capacidade consumida", "Capacity used")}
          className={`h-2 ${thresholds.reached.includes(90) ? "[&>div]:bg-red-500" : ""}`}
        />
        {thresholds.reached.length > 0 && (
          <p className="text-[11px] text-amber-700 dark:text-amber-400">
            {txt("Alertas atingidos", "Thresholds reached")}: {thresholds.reached.map((value) => `${value}%`).join(", ")}
          </p>
        )}
      </div>

      <div className="space-y-1" data-testid="credit-consumption">
        <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
          {consumption.mode === "enforced"
            ? txt("Consumo cobrado no ciclo", "Consumption charged this cycle")
            : txt("Consumo estimado no ciclo (não cobrado)", "Estimated consumption this cycle (not charged)")}
        </p>
        {consumptionItems.length === 0 ? (
          <p className="text-xs text-muted-foreground">{txt("Nenhum consumo no ciclo.", "No consumption this cycle.")}</p>
        ) : (
          consumptionItems.map((item) => {
            const label = CONSUMPTION_LABELS[item.kind];
            return (
              <div key={item.kind} className="flex items-center justify-between gap-2 text-xs">
                <span className="text-muted-foreground">{label ? txt(label[0], label[1]) : item.kind}</span>
                <span className="tabular-nums">{credits(item.amount_mc)}</span>
              </div>
            );
          })
        )}
      </div>
    </>
  );
}

// ── History (lots) ───────────────────────────────────────────────────────────

const LOT_STATUS: Record<CreditLot["status"], { pt: string; en: string; icon: typeof CheckCircle2; tone: string }> = {
  active: { pt: "Em vigor", en: "In force", icon: CheckCircle2, tone: "text-emerald-700 dark:text-emerald-400" },
  not_started: { pt: "Ainda não iniciado", en: "Not started yet", icon: Hourglass, tone: "text-sky-700 dark:text-sky-400" },
  exhausted: { pt: "Esgotado", en: "Used up", icon: MinusCircle, tone: "text-muted-foreground" },
  expired: { pt: "Vencido", en: "Expired", icon: XCircle, tone: "text-muted-foreground" },
};

/**
 * While the ledger is on hold a lot that would be spendable (`active`,
 * `not_started`) is not presented as such: its status reads "on hold", with
 * an icon and text, never by colour alone. Used-up and expired lots keep
 * their own labels.
 */
const LOT_ON_HOLD = { pt: "Retido", en: "On hold", icon: PauseCircle, tone: "text-amber-800 dark:text-amber-300" };

function lotStatus(status: CreditLot["status"], onHold: boolean) {
  return onHold && (status === "active" || status === "not_started") ? LOT_ON_HOLD : LOT_STATUS[status];
}

/**
 * Same technique as the plan comparison: one native table whose rows become
 * one card each when its container is narrower than 36rem, so the desktop
 * table and the mobile cards are the same collection in the same order.
 */
export const HISTORY_TABLE_CLASSES = {
  container: "@container",
  wrapper: "@min-[36rem]:overflow-x-auto @min-[36rem]:rounded-lg @min-[36rem]:border @min-[36rem]:border-border/60",
  table: "block w-full @min-[36rem]:table @min-[36rem]:border-collapse",
  head: "hidden @min-[36rem]:table-header-group",
  headCell:
    "bg-muted/30 px-2 py-2 text-left align-bottom text-[10px] font-bold uppercase tracking-wider text-muted-foreground first:pl-3 last:pr-3",
  body: "block space-y-2 @min-[36rem]:table-row-group @min-[36rem]:space-y-0",
  row: "grid grid-cols-2 gap-2 rounded-lg border border-border/60 bg-muted/10 p-3 @min-[36rem]:table-row @min-[36rem]:rounded-none @min-[36rem]:border-0 @min-[36rem]:bg-transparent @min-[36rem]:p-0",
  bodyCell:
    "block min-w-0 @min-[36rem]:table-cell @min-[36rem]:border-t @min-[36rem]:border-border/60 @min-[36rem]:px-2 @min-[36rem]:py-2.5 @min-[36rem]:align-middle @min-[36rem]:first:pl-3 @min-[36rem]:last:pr-3",
  cellLabel: "block text-[10px] uppercase tracking-wide text-muted-foreground @min-[36rem]:hidden",
} as const;

function CreditHistory({
  history,
  locale,
  txt,
  onRetry,
  onHold,
}: {
  history: CreditHistoryState;
  locale: Locale;
  txt: Txt;
  onRetry?: () => void;
  /** The ledger is on hold (`creditLedgerStanding`): no lot reads as spendable. */
  onHold: boolean;
}) {
  const title = txt("Histórico de créditos", "Credit history");
  let body;
  if (history.status === "loading") {
    body = (
      <p role="status" aria-live="polite" className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        {txt("Carregando o histórico de créditos…", "Loading the credit history…")}
      </p>
    );
  } else if (history.status === "error") {
    body = (
      <div role="alert" className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs">
        <p className="flex items-center gap-2 text-destructive">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {txt(
            "Não foi possível ler o histórico de créditos. Nenhum movimento foi presumido.",
            "The credit history could not be read. No entry was assumed.",
          )}
        </p>
        {onRetry && (
          <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={onRetry}>
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            {txt("Tentar novamente", "Try again")}
          </Button>
        )}
      </div>
    );
  } else if (history.lots.length === 0) {
    body = (
      <p data-testid="credit-history-empty" className="text-xs text-muted-foreground">
        {txt("Nenhum crédito foi concedido a esta conta até agora.", "No credit has been granted to this account so far.")}
      </p>
    );
  } else {
    const c = HISTORY_TABLE_CLASSES;
    const columns = [
      txt("Data", "Date"),
      txt("Origem", "Origin"),
      txt("Créditos", "Credits"),
      txt("Situação", "Status"),
      // `expires_at` is an instant at 00:00 of that day, not an inclusive last day.
      txt("Expira em", "Expires on"),
    ];
    // Newest grant first; the collection itself is the server's, unpaginated
    // (the lots route has no cursor and no `has_more`).
    const lots = [...history.lots].sort((a, b) => (a.paid_at < b.paid_at ? 1 : a.paid_at > b.paid_at ? -1 : 0));
    const noDate = unknownDate(txt);
    body = (
      <div className={c.container}>
        <div className={c.wrapper}>
          <table role="table" aria-label={title} className={c.table}>
            <thead role="rowgroup" className={c.head}>
              <tr role="row">
                {columns.map((label) => (
                  <th key={label} role="columnheader" scope="col" className={c.headCell}>
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody role="rowgroup" className={c.body}>
              {lots.map((lot) => {
                const status = lotStatus(lot.status, onHold);
                const StatusIcon = status.icon;
                return (
                  // `lot_id` keys the row; it is internal and never rendered.
                  <tr key={lot.lot_id} role="row" data-testid="credit-history-row" className={c.row}>
                    <td role="cell" className={c.bodyCell}>
                      <span className={c.cellLabel}>{columns[0]}</span>
                      <span className="block text-xs tabular-nums text-foreground">
                        {formatCivilDate(lot.paid_at, locale) ?? noDate}
                      </span>
                    </td>
                    <td role="cell" className={c.bodyCell}>
                      <span className={c.cellLabel}>{columns[1]}</span>
                      <span className="block text-xs text-foreground">
                        {lot.kind === "monthly" ? txt("Franquia mensal", "Monthly allowance") : txt("Recarga", "Top-up")}
                      </span>
                    </td>
                    <td role="cell" className={c.bodyCell}>
                      <span className={c.cellLabel}>{columns[2]}</span>
                      <span className="block text-xs font-semibold tabular-nums text-foreground">
                        {formatCredits(lot.granted_mc, locale)} {txt("créditos", "credits")}
                      </span>
                    </td>
                    <td role="cell" className={c.bodyCell}>
                      <span className={c.cellLabel}>{columns[3]}</span>
                      <span data-testid="credit-lot-status" className={`flex items-center gap-1 text-xs font-medium ${status.tone}`}>
                        <StatusIcon className="h-3 w-3 shrink-0" aria-hidden="true" />
                        {txt(status.pt, status.en)}
                      </span>
                    </td>
                    <td role="cell" className={c.bodyCell}>
                      <span className={c.cellLabel}>{columns[4]}</span>
                      <span className="block text-xs tabular-nums text-foreground">
                        {formatInstant(lot.expires_at, locale, { withTime: false }) ?? noDate}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  }
  return (
    <Card className="border-border/60 bg-card" data-testid="credit-history" data-state={history.status}>
      <CardHeader className="pb-3">
        <h3 className="text-sm font-semibold leading-none tracking-tight text-foreground">{title}</h3>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

// ── Top-ups ──────────────────────────────────────────────────────────────────

function TopupSection({
  topup,
  locale,
  txt,
  onBuy,
  buying,
  buyError,
}: {
  topup: TopupState;
  locale: Locale;
  txt: Txt;
  onBuy?: (offerId: string, method: TopupMethod) => void;
  buying: boolean;
  buyError: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [offerId, setOfferId] = useState<string | null>(null);
  const [method, setMethod] = useState<TopupMethod | null>(null);
  const openRef = useRef<HTMLButtonElement>(null);
  const panelHeadingRef = useRef<HTMLHeadingElement>(null);
  const hintId = useId();
  const panelId = useId();

  if (topup.status === "hidden") return null;
  if (topup.status === "blocked") {
    // A temporary block exposes no action: only its reason, as text.
    return (
      <p
        data-testid="topup-blocked"
        // Only the outcome of the user's own click is announced; a block that
        // was already there on load stays a quiet, static notice.
        role={topup.announce ? "alert" : undefined}
        className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-300"
      >
        <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>{topup.reason}</span>
      </p>
    );
  }

  const selected = topup.packages.find((pkg) => pkg.offerId === offerId) ?? null;
  const methodOk = selected !== null && method !== null && selected.methods.includes(method);
  const close = () => {
    setOpen(false);
    setTimeout(() => openRef.current?.focus(), 0);
  };

  return (
    <div className="space-y-3" data-testid="topup-offer">
      {!open && (
        <Button
          ref={openRef}
          type="button"
          className="gap-2"
          // Rendered only while the panel is closed: `aria-expanded` is false
          // and nothing is controlled yet, so no `aria-controls`.
          aria-expanded={false}
          onClick={() => {
            setOpen(true);
            setTimeout(() => panelHeadingRef.current?.focus(), 0);
          }}
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          {txt("Recarregar", "Top up")}
        </Button>
      )}
      {open && (
        <Card
          id={panelId}
          className="border-emerald-500/20 bg-card"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              close();
            }
          }}
        >
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Wallet className="h-4 w-4 text-emerald-500" aria-hidden="true" />
                <h3
                  ref={panelHeadingRef}
                  tabIndex={-1}
                  className="text-sm font-semibold text-foreground focus:outline-none"
                >
                  {txt("Recarregar créditos", "Top up credits")}
                </h3>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={txt("Fechar recarga", "Close top-up")}
                onClick={close}
                className="h-7 w-7 p-0"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </Button>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-xs text-muted-foreground">
              {txt(
                "Recarga avulsa paga no checkout hospedado do ASAAS. Os créditos entram no saldo somente depois que o pagamento for confirmado e registrado.",
                "One-off top-up paid on the ASAAS-hosted checkout. Credits reach the balance only after the payment is confirmed and recorded.",
              )}
            </p>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" role="group" aria-label={txt("Pacote", "Package")}>
              {topup.packages.map((pkg) => {
                const price = formatMoney(pkg.amountCents, pkg.currency, locale);
                const pressed = pkg.offerId === offerId;
                return (
                  <Button
                    key={pkg.offerId}
                    type="button"
                    variant={pressed ? "default" : "outline"}
                    aria-pressed={pressed}
                    className="h-auto flex-col items-start gap-0.5 whitespace-normal py-2 text-left"
                    onClick={() => {
                      setOfferId(pkg.offerId);
                      if (method && !pkg.methods.includes(method)) setMethod(null);
                    }}
                  >
                    <span className="text-sm font-semibold">
                      {formatCount(pkg.credits, locale)} {txt("créditos", "credits")}
                    </span>
                    <span className="text-[11px] font-normal opacity-80">
                      {price ?? txt("Valor indisponível", "Amount unavailable")} ·{" "}
                      {txt(`válida por ${pkg.validityMonths} meses`, `valid for ${pkg.validityMonths} months`)}
                    </span>
                  </Button>
                );
              })}
            </div>
            {selected && (
              <div className="flex flex-wrap gap-2" role="group" aria-label={txt("Forma de pagamento", "Payment method")}>
                {selected.methods.map((item) => (
                  <Button
                    key={item}
                    type="button"
                    size="sm"
                    variant={method === item ? "default" : "outline"}
                    aria-pressed={method === item}
                    onClick={() => setMethod(item)}
                  >
                    {item === "pix" ? "PIX" : txt("Cartão de crédito", "Credit card")}
                  </Button>
                ))}
              </div>
            )}
            {!methodOk && (
              <p id={hintId} className="text-xs text-muted-foreground">
                {txt(
                  "Escolha um pacote e a forma de pagamento para continuar.",
                  "Choose a package and the payment method to continue.",
                )}
              </p>
            )}
            <Button
              type="button"
              className="w-full gap-2"
              disabled={!methodOk || buying}
              aria-describedby={methodOk ? undefined : hintId}
              onClick={() => {
                if (selected && method && methodOk && !buying) onBuy?.(selected.offerId, method);
              }}
            >
              {buying ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              ) : (
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              )}
              {txt("Ir para o pagamento da recarga", "Continue to top-up payment")}
            </Button>
            {buyError && (
              <p role="alert" className="text-sm text-destructive">
                {buyError}
              </p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// ── Composition ──────────────────────────────────────────────────────────────

export function CreditsAndTopups({
  state,
  history,
  topup,
  locale,
  onRetry,
  onRetryHistory,
  exportAction,
  onBuy,
  buying = false,
  buyError = null,
}: {
  state: CreditsPanelState;
  history: CreditHistoryState;
  topup: TopupState;
  locale: Locale;
  onRetry?: () => void;
  onRetryHistory?: () => void;
  exportAction?: ExportAction;
  onBuy?: (offerId: string, method: TopupMethod) => void;
  buying?: boolean;
  buyError?: string | null;
}) {
  const txt: Txt = (pt, en) => (locale === "en" ? en : pt);

  if (state.status === "loading") {
    return (
      <p
        data-testid="credits-and-topups"
        data-state="loading"
        role="status"
        aria-live="polite"
        className="flex items-center gap-2 text-xs text-muted-foreground"
      >
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        {txt("Carregando os créditos…", "Loading credits…")}
      </p>
    );
  }
  if (state.status === "forbidden") {
    return (
      <p data-testid="credits-and-topups" data-state="forbidden" className="text-xs text-muted-foreground">
        {txt("Sua conta não tem acesso aos créditos.", "Your account has no access to credits.")}
      </p>
    );
  }
  if (state.status === "error") {
    return (
      <div
        data-testid="credits-and-topups"
        data-state="error"
        role="alert"
        className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs"
      >
        <p className="flex items-center gap-2 text-destructive">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          {txt(
            "Não foi possível ler os créditos. Nenhum saldo foi presumido.",
            "Credits could not be read. No balance was assumed.",
          )}
        </p>
        {onRetry && (
          <Button type="button" variant="outline" size="sm" className="gap-1.5" onClick={onRetry}>
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            {txt("Tentar novamente", "Try again")}
          </Button>
        )}
      </div>
    );
  }

  const { summary } = state;
  const standing = creditStanding(summary.state);
  const updatedAt = formatInstant(summary.as_of, locale);
  const holdLifecycle = standing === "pending" ? lifecycleLines(summary.state, locale, txt, false) : null;
  return (
    <div className="space-y-3" data-testid="credits-and-topups" data-state={standing}>
      <Card className="border-emerald-500/25 bg-card" data-testid="credit-panel">
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Coins className="h-4 w-4 text-emerald-500" aria-hidden="true" />
            <h3 className="text-sm font-semibold leading-none tracking-tight text-foreground">
              {txt("Saldo de créditos", "Credit balance")}
            </h3>
            {updatedAt && (
              <span className="ml-auto text-[10px] text-muted-foreground">
                {txt("Atualizado em", "Updated at")} {updatedAt}
              </span>
            )}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {standing !== "not_applicable" && summary.state.mode === "observational" && (
            <p
              data-testid="credit-mode"
              className="rounded-md border border-sky-500/30 bg-sky-500/5 px-3 py-2 text-xs text-sky-800 dark:text-sky-300"
            >
              {txt(
                "Modo observacional: o consumo é medido, mas ainda não é cobrado dos seus créditos.",
                "Observational mode: consumption is measured but not yet charged to your credits.",
              )}
            </p>
          )}
          {/* Persistent polite region: mounted with the ready panel, so a
              notice that changes after "Atualizar estado" is announced. */}
          <div role="status" aria-live="polite" data-testid="credit-notices" className="empty:mb-0">
            {standing === "not_applicable" && (
              <p data-testid="credit-state" data-state="not_applicable" className="text-xs text-muted-foreground">
                {txt(
                  "Não se aplica: esta clínica não pertence a um builder com conta de créditos.",
                  "Not applicable: this clinic does not belong to a builder with a credit account.",
                )}
              </p>
            )}
            {standing === "pending" && (
              <div
                data-testid="credit-state"
                data-state="pending"
                data-lifecycle={summary.state.state}
                className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs text-amber-800 dark:text-amber-300"
              >
                <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <div className="space-y-1">
                  <p className="font-semibold">{txt("Pendente", "Pending")}</p>
                  {pendingReasonLines(summary.state.pending_reasons, txt).map((line) => (
                    <p key={line}>{line}</p>
                  ))}
                  {holdLifecycle && (
                    <p data-testid="credit-hold-lifecycle">
                      {holdLifecycle.message} {holdLifecycle.exportLine}
                    </p>
                  )}
                  <p>
                    {txt(
                      "O saldo fica retido e não é exibido como disponível até o servidor concluir a verificação. Os lotes do histórico abaixo são registros do servidor, não saldo disponível.",
                      "The balance stays on hold and is not shown as available until the server completes the verification. The lots in the history below are server records, not available balance.",
                    )}
                  </p>
                </div>
              </div>
            )}
            {standing === "standing" && (
              <StandingNotice state={summary.state} locale={locale} txt={txt} topupOffered={topup.status === "offered"} />
            )}
          </div>
          {standing === "standing" && <BalanceDetails summary={summary} locale={locale} txt={txt} />}
          {exportAction && (
            <div className="space-y-1.5 border-t border-border/40 pt-3">
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="gap-1.5"
                disabled={exportAction.disabled || exportAction.loading}
                aria-describedby={exportAction.message ? "credit-export-message" : undefined}
                onClick={exportAction.onExport}
              >
                <Download className="h-3.5 w-3.5" aria-hidden="true" />
                {txt("Exportar dados (ZIP)", "Export data (ZIP)")}
              </Button>
              {exportAction.message && (
                <p id="credit-export-message" role="alert" className="text-xs text-muted-foreground">
                  {exportAction.message}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <TopupSection
        topup={topup}
        locale={locale}
        txt={txt}
        onBuy={onBuy}
        buying={buying}
        buyError={buyError}
      />

      {standing !== "not_applicable" && (
        <CreditHistory
          history={history}
          locale={locale}
          txt={txt}
          onRetry={onRetryHistory}
          onHold={standing === "pending"}
        />
      )}
    </div>
  );
}
