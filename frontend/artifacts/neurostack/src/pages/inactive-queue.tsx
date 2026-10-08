import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Bot, Check, ChevronLeft, ChevronRight, Copy, RefreshCw, UserX } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { useToast } from "@/hooks/use-toast";
import { getBucketLabel, getIntentBucket } from "@/lib/intent-presentation";

const PAGE_SIZE = 20;
const POLL_INTERVAL_MS = 30_000;
const SUCCESS_TRANSITION_MS = 600;
const COPY_FEEDBACK_MS = 2_000;
const DEFAULT_MASTER_REACTIVATION_PHRASE = "Precisando de algo mais, estamos a disposição.";
const DEFAULT_REACTIVATION_DELAY_HOURS = 1;

export interface InactiveSessionItem {
  session_id: string;
  contact_identifier: string | null;
  channel: string;
  last_intent: string | null;
  funnel_stage: string | null;
  last_activity_at: string;
}

interface InactiveSessionsResponse {
  items: InactiveSessionItem[];
  total: number;
  page: number;
  page_size: number;
  has_next: boolean;
  has_previous: boolean;
}

interface QueueState {
  contextKey: string;
  items: InactiveSessionItem[];
  total: number;
  hasNext: boolean;
  hasPrevious: boolean;
  loading: boolean;
  error: boolean;
  pendingIds: Set<string>;
  successfulIds: Set<string>;
}

interface ContextIdentity {
  key: string;
  generation: number;
  mounted: boolean;
}

interface MasterPhraseState {
  tenantContextKey: string;
  status: "loading" | "ready" | "error";
  phrase: string;
  reactivationDelayHours: number;
}

interface ProtectedItem {
  contextKey: string;
  generation: number;
  item: InactiveSessionItem;
  phase: "pending" | "successful";
}

function initialQueueState(contextKey: string, loading: boolean): QueueState {
  return {
    contextKey,
    items: [],
    total: 0,
    hasNext: false,
    hasPrevious: false,
    loading,
    error: false,
    pendingIds: new Set(),
    successfulIds: new Set(),
  };
}

function demoQueueState(contextKey: string, items: InactiveSessionItem[]): QueueState {
  return {
    ...initialQueueState(contextKey, false),
    items,
    total: items.length,
  };
}

function buildDemoInactiveSessions(): InactiveSessionItem[] {
  const mountedAt = Date.now();
  const minutesAgo = (minutes: number) => new Date(mountedAt - minutes * 60_000).toISOString();

  return [
    {
      session_id: "d3e0a001-7a11-4b01-8a01-000000000001",
      contact_identifier: "Contato demonstrativo 01",
      channel: "whatsapp",
      last_intent: "agendar",
      funnel_stage: "decisao",
      last_activity_at: minutesAgo(45),
    },
    {
      session_id: "d3e0a002-7a11-4b02-8a02-000000000002",
      contact_identifier: "Contato demonstrativo 02",
      channel: "webchat",
      last_intent: "pedido_preco",
      funnel_stage: "consideracao",
      last_activity_at: minutesAgo(180),
    },
    {
      session_id: "d3e0a003-7a11-4b03-8a03-000000000003",
      contact_identifier: "Contato demonstrativo 03",
      channel: "whatsapp",
      last_intent: "interesse_tratamento",
      funnel_stage: "descoberta",
      last_activity_at: minutesAgo(1_560),
    },
  ];
}

function initialMasterPhraseState(tenantContextKey: string): MasterPhraseState {
  return {
    tenantContextKey,
    status: "loading",
    phrase: DEFAULT_MASTER_REACTIVATION_PHRASE,
    reactivationDelayHours: DEFAULT_REACTIVATION_DELAY_HOURS,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isInactiveSessionItem(value: unknown): value is InactiveSessionItem {
  if (!isRecord(value)) return false;
  return typeof value.session_id === "string"
    && (typeof value.contact_identifier === "string" || value.contact_identifier === null)
    && typeof value.channel === "string"
    && (typeof value.last_intent === "string" || value.last_intent === null)
    && (typeof value.funnel_stage === "string" || value.funnel_stage === null)
    && typeof value.last_activity_at === "string";
}

function isInactiveSessionsResponse(value: unknown): value is InactiveSessionsResponse {
  if (!isRecord(value)) return false;
  return Array.isArray(value.items)
    && value.items.every(isInactiveSessionItem)
    && typeof value.total === "number"
    && Number.isFinite(value.total)
    && value.total >= 0
    && typeof value.page === "number"
    && Number.isInteger(value.page)
    && value.page > 0
    && typeof value.page_size === "number"
    && Number.isInteger(value.page_size)
    && value.page_size > 0
    && typeof value.has_next === "boolean"
    && typeof value.has_previous === "boolean";
}

function getReactivationDelayHours(value: unknown): number {
  if (typeof value === "number") {
    return Number.isSafeInteger(value) && value > 0
      ? value
      : DEFAULT_REACTIVATION_DELAY_HOURS;
  }
  if (typeof value !== "string" || !/^[+-]?\d+$/.test(value.trim())) {
    return DEFAULT_REACTIVATION_DELAY_HOURS;
  }
  const parsed = Number(value.trim());
  return Number.isSafeInteger(parsed) && parsed > 0
    ? parsed
    : DEFAULT_REACTIVATION_DELAY_HOURS;
}

function getMasterPhraseSettings(value: unknown) {
  if (!isRecord(value)) throw new Error("inactive_master_phrase_invalid_response");
  const configuredPhrase = value.effective_trigger_message ?? value.trigger_message;
  const configuredDelay = value.effective_reactivation_delay_hours
    ?? value.reactivation_delay_hours;
  return {
    phrase: typeof configuredPhrase === "string" && configuredPhrase.trim()
      ? configuredPhrase
      : DEFAULT_MASTER_REACTIVATION_PHRASE,
    reactivationDelayHours: getReactivationDelayHours(configuredDelay),
  };
}

function isReactivationResponse(
  value: unknown,
  sessionId: string,
): value is { session_id: string; status: "reactivated" | "already_active" } {
  return isRecord(value)
    && value.session_id === sessionId
    && (value.status === "reactivated" || value.status === "already_active");
}

function getToken(): string {
  try {
    return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? "";
  } catch {
    return "";
  }
}

function shortSessionCode(sessionId: string) {
  return `#${sessionId.replaceAll("-", "").slice(0, 8).toUpperCase()}`;
}

export default function InactiveQueuePage() {
  const { tenantId, isBuilder, isInvestor, isSuperAdmin, viewingClinicId } = useAuth();
  const { locale, t } = useI18n();
  const { toast } = useToast();
  const selectedTenantId = isBuilder || isSuperAdmin ? viewingClinicId : null;
  const hasRequiredClinic = isInvestor || !(isBuilder || isSuperAdmin) || Boolean(selectedTenantId);
  const tenantContextKey = isInvestor
    ? "investor-demo"
    : hasRequiredClinic
    ? `${isBuilder || isSuperAdmin ? "override" : "own"}:${selectedTenantId ?? tenantId ?? ""}`
    : "no-clinic";
  const [demoItems] = useState(buildDemoInactiveSessions);

  const [pagination, setPagination] = useState({ tenantContextKey, page: 1 });
  const page = pagination.tenantContextKey === tenantContextKey ? pagination.page : 1;
  const contextKey = `${tenantContextKey}:page:${page}`;
  const [queueState, setQueueState] = useState<QueueState>(() => (
    isInvestor ? demoQueueState(contextKey, demoItems) : initialQueueState(contextKey, hasRequiredClinic)
  ));
  const [masterPhraseCopied, setMasterPhraseCopied] = useState(false);
  const [masterPhraseState, setMasterPhraseState] = useState<MasterPhraseState>(
    () => isInvestor
      ? { ...initialMasterPhraseState(tenantContextKey), status: "ready" }
      : initialMasterPhraseState(tenantContextKey),
  );
  const [masterPhraseRequestVersion, setMasterPhraseRequestVersion] = useState(0);
  const visibleState = queueState.contextKey === contextKey
    ? queueState
    : isInvestor
      ? demoQueueState(contextKey, demoItems)
      : initialQueueState(contextKey, hasRequiredClinic);
  const { items, total, hasNext, hasPrevious, loading, error, pendingIds, successfulIds } = visibleState;

  const requestSequenceRef = useRef(0);
  const activeRequestRef = useRef<AbortController | null>(null);
  const mutationControllersRef = useRef<Map<string, AbortController>>(new Map());
  const protectedItemsRef = useRef<Map<string, ProtectedItem>>(new Map());
  const removalTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const generationRef = useRef(0);
  const contextIdentityRef = useRef<ContextIdentity>({ key: "", generation: 0, mounted: false });
  const copyFeedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const masterPhraseRequestRef = useRef<AbortController | null>(null);
  const masterPhraseGenerationRef = useRef(0);
  const masterPhraseContextKeyRef = useRef(tenantContextKey);
  masterPhraseContextKeyRef.current = tenantContextKey;

  const visibleMasterPhraseState = masterPhraseState.tenantContextKey === tenantContextKey
    ? masterPhraseState
    : isInvestor
      ? { ...initialMasterPhraseState(tenantContextKey), status: "ready" }
      : initialMasterPhraseState(tenantContextKey);

  useEffect(() => {
    setPagination({ tenantContextKey, page: 1 });
  }, [tenantContextKey]);

  const setPage = (nextPage: number | ((current: number) => number)) => {
    setPagination((current) => {
      const currentPage = current.tenantContextKey === tenantContextKey ? current.page : 1;
      return {
        tenantContextKey,
        page: typeof nextPage === "function" ? nextPage(currentPage) : nextPage,
      };
    });
  };

  const isCurrentContext = useCallback((identity: ContextIdentity) => (
    identity.mounted
    && contextIdentityRef.current.mounted
    && contextIdentityRef.current.key === identity.key
    && contextIdentityRef.current.generation === identity.generation
  ), []);

  const clearAsyncWork = useCallback(() => {
    activeRequestRef.current?.abort();
    activeRequestRef.current = null;
    mutationControllersRef.current.forEach((controller) => controller.abort());
    mutationControllersRef.current.clear();
    removalTimersRef.current.forEach((timer) => clearTimeout(timer));
    removalTimersRef.current.clear();
    protectedItemsRef.current.clear();
    requestSequenceRef.current += 1;
  }, []);

  useLayoutEffect(() => {
    clearAsyncWork();
    const identity = {
      key: contextKey,
      generation: ++generationRef.current,
      mounted: true,
    };
    contextIdentityRef.current = identity;
    setQueueState(isInvestor
      ? demoQueueState(contextKey, demoItems)
      : initialQueueState(contextKey, hasRequiredClinic));

    return () => {
      if (contextIdentityRef.current.generation === identity.generation) {
        contextIdentityRef.current = { ...identity, mounted: false };
      }
      clearAsyncWork();
    };
  }, [clearAsyncWork, contextKey, demoItems, hasRequiredClinic, isInvestor]);

  const fetchQueue = useCallback(async (identity: ContextIdentity, showLoading = false) => {
    if (!isCurrentContext(identity)) return;
    if (isInvestor) return;
    const requestSequence = ++requestSequenceRef.current;
    activeRequestRef.current?.abort();
    const controller = new AbortController();
    activeRequestRef.current = controller;

    if (!hasRequiredClinic) {
      setQueueState(initialQueueState(identity.key, false));
      return;
    }

    setQueueState((current) => current.contextKey === identity.key
      ? { ...current, loading: showLoading || current.loading, error: false }
      : current);

    const params = new URLSearchParams({
      page: String(page),
      page_size: String(PAGE_SIZE),
    });
    if (selectedTenantId) params.set("tenant_id", selectedTenantId);

    try {
      const response = await fetch(`/v1/sessions/inactive?${params.toString()}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("inactive_queue_request_failed");
      const data: unknown = await response.json();
      if (!isInactiveSessionsResponse(data)) throw new Error("inactive_queue_invalid_response");
      if (
        requestSequence !== requestSequenceRef.current
        || controller.signal.aborted
        || !isCurrentContext(identity)
      ) return;

      const lastPage = Math.max(1, Math.ceil(data.total / data.page_size));
      const protectedItems = [...protectedItemsRef.current.values()]
        .filter((entry) => entry.contextKey === identity.key && entry.generation === identity.generation);
      if (page > lastPage && protectedItems.length === 0) {
        setQueueState((current) => current.contextKey === identity.key
          ? { ...current, total: data.total, hasNext: false, hasPrevious: lastPage > 1 }
          : current);
        setPage(lastPage);
        return;
      }

      const protectedIds = new Set(protectedItems.map((entry) => entry.item.session_id));
      const reconciledItems = [
        ...protectedItems.map((entry) => entry.item),
        ...data.items.filter((item) => !protectedIds.has(item.session_id)),
      ];
      setQueueState((current) => current.contextKey === identity.key ? {
        ...current,
        items: reconciledItems,
        ...(protectedItems.length === 0 ? {
          total: data.total,
          hasNext: data.has_next,
          hasPrevious: data.has_previous,
        } : {}),
      } : current);
    } catch (requestError) {
      if (
        controller.signal.aborted
        || requestSequence !== requestSequenceRef.current
        || !isCurrentContext(identity)
      ) return;
      setQueueState((current) => current.contextKey === identity.key ? { ...current, error: true } : current);
    } finally {
      if (requestSequence === requestSequenceRef.current && !controller.signal.aborted && isCurrentContext(identity)) {
        setQueueState((current) => current.contextKey === identity.key ? { ...current, loading: false } : current);
      }
    }
  }, [hasRequiredClinic, isCurrentContext, isInvestor, page, selectedTenantId]);

  useEffect(() => {
    if (isInvestor) return;
    const identity = contextIdentityRef.current;
    void fetchQueue(identity, true);
    const pollId = window.setInterval(() => void fetchQueue(identity, false), POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(pollId);
      activeRequestRef.current?.abort();
    };
  }, [contextKey, fetchQueue, isInvestor]);

  useEffect(() => {
    masterPhraseRequestRef.current?.abort();
    const controller = new AbortController();
    const generation = ++masterPhraseGenerationRef.current;
    masterPhraseRequestRef.current = controller;
    if (copyFeedbackTimerRef.current) {
      clearTimeout(copyFeedbackTimerRef.current);
      copyFeedbackTimerRef.current = null;
    }
    setMasterPhraseCopied(false);
    setMasterPhraseState(initialMasterPhraseState(tenantContextKey));

    if (isInvestor) {
      masterPhraseRequestRef.current = null;
      setMasterPhraseState({
        ...initialMasterPhraseState(tenantContextKey),
        status: "ready",
      });
      return () => controller.abort();
    }

    if (!hasRequiredClinic) {
      masterPhraseRequestRef.current = null;
      return () => controller.abort();
    }

    const configUrl = selectedTenantId
      ? `/v1/config?tenant_id=${encodeURIComponent(selectedTenantId)}`
      : "/v1/config";

    void (async () => {
      try {
        const response = await fetch(configUrl, {
          headers: { Authorization: `Bearer ${getToken()}` },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("inactive_master_phrase_request_failed");
        const data: unknown = await response.json();
        const settings = getMasterPhraseSettings(data);
        if (controller.signal.aborted || generation !== masterPhraseGenerationRef.current) return;
        setMasterPhraseState({
          tenantContextKey,
          status: "ready",
          ...settings,
        });
      } catch {
        if (controller.signal.aborted || generation !== masterPhraseGenerationRef.current) return;
        setMasterPhraseState({
          ...initialMasterPhraseState(tenantContextKey),
          status: "error",
        });
      } finally {
        if (masterPhraseRequestRef.current === controller) {
          masterPhraseRequestRef.current = null;
        }
      }
    })();

    return () => controller.abort();
  }, [hasRequiredClinic, isInvestor, masterPhraseRequestVersion, selectedTenantId, tenantContextKey]);

  useEffect(() => () => {
    if (copyFeedbackTimerRef.current) clearTimeout(copyFeedbackTimerRef.current);
    masterPhraseRequestRef.current?.abort();
  }, []);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const formattedActivity = useMemo(() => new Intl.DateTimeFormat(
    locale === "pt" ? "pt-BR" : "en-US",
    { dateStyle: "short", timeStyle: "short" },
  ), [locale]);

  const copyMasterPhrase = async () => {
    const tenantKey = tenantContextKey;
    const generation = masterPhraseGenerationRef.current;
    const phrase = visibleMasterPhraseState.phrase;
    const isCurrentMasterPhrase = () => (
      contextIdentityRef.current.mounted
      && masterPhraseContextKeyRef.current === tenantKey
      && masterPhraseGenerationRef.current === generation
    );
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard_unavailable");
      await navigator.clipboard.writeText(phrase);
      if (!isCurrentMasterPhrase()) return;
      setMasterPhraseCopied(true);
      if (copyFeedbackTimerRef.current) clearTimeout(copyFeedbackTimerRef.current);
      copyFeedbackTimerRef.current = setTimeout(() => {
        if (isCurrentMasterPhrase()) setMasterPhraseCopied(false);
        copyFeedbackTimerRef.current = null;
      }, COPY_FEEDBACK_MS);
    } catch {
      if (!isCurrentMasterPhrase()) return;
      toast({
        title: t("inactive_master_phrase_copy_error_title"),
        description: t("inactive_master_phrase_copy_error_description"),
        variant: "destructive",
      });
    }
  };

  const reactivate = async (item: InactiveSessionItem) => {
    const sessionId = item.session_id;
    if (
      pendingIds.has(sessionId)
      || successfulIds.has(sessionId)
      || protectedItemsRef.current.has(sessionId)
    ) return;
    const identity = contextIdentityRef.current;
    if (!isCurrentContext(identity) || identity.key !== contextKey) return;
    protectedItemsRef.current.set(sessionId, {
      contextKey: identity.key,
      generation: identity.generation,
      item,
      phase: "pending",
    });
    activeRequestRef.current?.abort();
    requestSequenceRef.current += 1;
    setQueueState((current) => current.contextKey === identity.key ? {
      ...current,
      pendingIds: new Set(current.pendingIds).add(sessionId),
    } : current);

    const params = new URLSearchParams();
    if (selectedTenantId) params.set("tenant_id", selectedTenantId);
    const query = params.size > 0 ? `?${params.toString()}` : "";
    const mutationKey = `${identity.generation}:${sessionId}`;
    const controller = new AbortController();
    mutationControllersRef.current.set(mutationKey, controller);
    let succeeded = false;

    try {
      if (!isInvestor) {
        const response = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}/reactivate${query}`, {
          method: "POST",
          headers: { Authorization: `Bearer ${getToken()}` },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("inactive_reactivation_failed");
        const result: unknown = await response.json();
        if (!isReactivationResponse(result, sessionId)) throw new Error("inactive_reactivation_invalid_response");
      }
      if (controller.signal.aborted || !isCurrentContext(identity)) return;

      succeeded = true;
      protectedItemsRef.current.set(sessionId, {
        contextKey: identity.key,
        generation: identity.generation,
        item,
        phase: "successful",
      });
      setQueueState((current) => {
        if (current.contextKey !== identity.key) return current;
        const nextPending = new Set(current.pendingIds);
        nextPending.delete(sessionId);
        return {
          ...current,
          items: current.items.some((currentItem) => currentItem.session_id === sessionId)
            ? current.items
            : [item, ...current.items],
          pendingIds: nextPending,
          successfulIds: new Set(current.successfulIds).add(sessionId),
        };
      });
      toast({
        title: t("inactive_reactivate_success_title"),
        description: t("inactive_reactivate_success_description"),
      });
      const timer = setTimeout(() => {
        if (!isCurrentContext(identity)) return;
        activeRequestRef.current?.abort();
        requestSequenceRef.current += 1;
        protectedItemsRef.current.delete(sessionId);
        setQueueState((current) => {
          if (current.contextKey !== identity.key) return current;
          const next = new Set(current.successfulIds);
          next.delete(sessionId);
          return {
            ...current,
            items: current.items.filter((currentItem) => currentItem.session_id !== sessionId),
            ...(isInvestor ? { total: Math.max(0, current.total - 1) } : {}),
            successfulIds: next,
          };
        });
        removalTimersRef.current.delete(sessionId);
        if (!isInvestor) void fetchQueue(identity, false);
      }, SUCCESS_TRANSITION_MS);
      removalTimersRef.current.set(sessionId, timer);
    } catch {
      if (controller.signal.aborted || !isCurrentContext(identity)) return;
      toast({
        title: t("inactive_reactivate_error_title"),
        description: t("inactive_reactivate_error_description"),
        variant: "destructive",
      });
    } finally {
      if (mutationControllersRef.current.get(mutationKey) === controller) {
        mutationControllersRef.current.delete(mutationKey);
      }
      if (controller.signal.aborted || !isCurrentContext(identity) || succeeded) return;
      protectedItemsRef.current.delete(sessionId);
      setQueueState((current) => {
        if (current.contextKey !== identity.key) return current;
        const next = new Set(current.pendingIds);
        next.delete(sessionId);
        return { ...current, pendingIds: next };
      });
    }
  };

  if (!hasRequiredClinic) {
    return (
      <div className="flex min-h-64 items-center justify-center rounded-xl border border-dashed bg-card p-6">
        <p className="text-center text-sm text-muted-foreground">{t("inactive_no_clinic")}</p>
      </div>
    );
  }

  return (
    <section className="space-y-6" aria-labelledby="inactive-queue-title">
      <header>
        <div className="flex items-center gap-2">
          <UserX className="h-6 w-6 text-primary" aria-hidden="true" />
          <h1 id="inactive-queue-title" className="text-2xl font-bold">{t("inactive_queue_title")}</h1>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">{t("inactive_queue_subtitle")}</p>
      </header>

      <aside
        className="rounded-xl border border-primary/20 bg-card p-5 shadow-sm"
        aria-labelledby="inactive-master-phrase-title"
      >
        <h2 id="inactive-master-phrase-title" className="text-base font-semibold text-foreground">
          {t("inactive_master_phrase_title")}
        </h2>
        {visibleMasterPhraseState.status === "loading" ? (
          <p className="mt-3 text-sm text-muted-foreground" aria-live="polite">
            <RefreshCw className="mr-2 inline h-4 w-4 animate-spin" aria-hidden="true" />
            {t("inactive_master_phrase_loading")}
          </p>
        ) : visibleMasterPhraseState.status === "error" ? (
          <div className="mt-3 flex flex-col items-start gap-3" role="alert">
            <p className="text-sm text-destructive">{t("inactive_master_phrase_load_error")}</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setMasterPhraseRequestVersion((current) => current + 1)}
            >
              {t("inactive_master_phrase_retry")}
            </Button>
          </div>
        ) : (
          <>
            <p className="mt-1 max-w-4xl text-sm leading-relaxed text-muted-foreground">
              {visibleMasterPhraseState.reactivationDelayHours === 1
                ? t("inactive_master_phrase_description")
                : t("inactive_master_phrase_description_multiple").replace(
                  "{hours}",
                  String(visibleMasterPhraseState.reactivationDelayHours),
                )}
            </p>
            <div className="mt-4 rounded-lg border bg-muted/30 p-4">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t("inactive_master_phrase_label")}
              </p>
              <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="break-words text-sm font-medium text-foreground">
                  {visibleMasterPhraseState.phrase}
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="shrink-0 gap-2"
                  onClick={() => void copyMasterPhrase()}
                  aria-label={t(
                    masterPhraseCopied
                      ? "inactive_master_phrase_copied_aria"
                      : "inactive_master_phrase_copy_aria",
                  )}
                >
                  {masterPhraseCopied ? (
                    <Check className="h-4 w-4 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                  ) : (
                    <Copy className="h-4 w-4" aria-hidden="true" />
                  )}
                  <span aria-live="polite">
                    {t(masterPhraseCopied ? "inactive_master_phrase_copied" : "inactive_master_phrase_copy")}
                  </span>
                </Button>
              </div>
            </div>
          </>
        )}
      </aside>

      {loading ? (
        <div className="flex min-h-56 items-center justify-center rounded-xl border bg-card" role="status">
          <RefreshCw className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
          <span>{t("inactive_loading")}</span>
        </div>
      ) : error ? (
        <div className="flex min-h-56 flex-col items-center justify-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-6" role="alert">
          <p className="text-sm text-destructive">{t("inactive_error")}</p>
          <Button
            variant="outline"
            onClick={() => void fetchQueue(contextIdentityRef.current, true)}
          >
            {t("inactive_retry")}
          </Button>
        </div>
      ) : items.length === 0 ? (
        <div className="flex min-h-56 flex-col items-center justify-center rounded-xl border border-dashed bg-card p-6 text-center">
          <Bot className="mb-3 h-8 w-8 text-muted-foreground" aria-hidden="true" />
          <p className="text-sm text-muted-foreground">{t("inactive_empty")}</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border bg-card shadow-sm">
          <div className="hidden grid-cols-[1.2fr_.8fr_.65fr_1fr_1fr_1.25fr] gap-4 border-b bg-muted/40 px-5 py-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground md:grid">
            <span>{t("inactive_contact")}</span>
            <span>{t("inactive_session_code")}</span>
            <span>{t("inactive_channel")}</span>
            <span>{t("inactive_last_intent")}</span>
            <span>{t("inactive_last_activity")}</span>
            <span>{t("inactive_action")}</span>
          </div>
          <div role="list">
            {items.map((item) => {
              const pending = pendingIds.has(item.session_id);
              const successful = successfulIds.has(item.session_id);
              const switchId = `reactivate-${item.session_id}`;
              return (
                <article
                  key={item.session_id}
                  role="listitem"
                  data-testid={`inactive-row-${item.session_id}`}
                  className={`grid grid-cols-1 gap-3 border-b px-5 py-4 transition-all duration-500 last:border-b-0 md:grid-cols-[1.2fr_.8fr_.65fr_1fr_1fr_1.25fr] md:items-center md:gap-4 ${successful ? "bg-emerald-500/5 opacity-70" : ""}`}
                >
                  <QueueField label={t("inactive_contact")} value={item.contact_identifier || "—"} prominent />
                  <QueueField label={t("inactive_session_code")} value={shortSessionCode(item.session_id)} mono />
                  <QueueField label={t("inactive_channel")} value={item.channel} />
                  <QueueField
                    label={t("inactive_last_intent")}
                    value={item.last_intent ? getBucketLabel(getIntentBucket(item.last_intent), t) : "—"}
                  />
                  <QueueField label={t("inactive_last_activity")} value={formattedActivity.format(new Date(item.last_activity_at))} />
                  <div className="flex min-w-0 items-center justify-between gap-3 md:justify-start">
                    <label
                      htmlFor={switchId}
                      className="min-w-0 text-sm font-semibold text-foreground"
                    >
                      <span className="block break-words">{t("inactive_reactivate")}</span>
                      {successful && (
                        <span className="block text-emerald-600 dark:text-emerald-400">
                          {t("inactive_ai_in_control")}
                        </span>
                      )}
                    </label>
                    <Switch
                      id={switchId}
                      checked={successful}
                      disabled={pending || successful}
                      onCheckedChange={(checked) => {
                        if (checked) void reactivate(item);
                      }}
                      onKeyDown={(event) => {
                        if ((event.key === " " || event.key === "Enter") && !pending && !successful) {
                          event.preventDefault();
                          void reactivate(item);
                        }
                      }}
                      aria-label={t("inactive_reactivate")}
                      className="shrink-0 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 data-[state=checked]:bg-emerald-600"
                    />
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      )}

      {!loading && !error && total > 0 && (
        <footer className="flex flex-col gap-3 text-sm sm:flex-row sm:items-center sm:justify-between" aria-label={t("inactive_action")}>
          <p className="text-muted-foreground">{total} {t("inactive_total")}</p>
          <div className="flex items-center justify-between gap-3 sm:justify-end">
            <Button
              variant="outline"
              size="sm"
              disabled={!hasPrevious}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
            >
              <ChevronLeft className="mr-1 h-4 w-4" aria-hidden="true" />
              {t("inactive_previous")}
            </Button>
            <span aria-live="polite">{t("inactive_page")} {page} {t("inactive_of")} {totalPages}</span>
            <Button
              variant="outline"
              size="sm"
              disabled={!hasNext}
              onClick={() => setPage((current) => current + 1)}
            >
              {t("inactive_next")}
              <ChevronRight className="ml-1 h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </footer>
      )}
    </section>
  );
}

function QueueField({
  label,
  value,
  prominent = false,
  mono = false,
}: {
  label: string;
  value: string;
  prominent?: boolean;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <span className="mb-0.5 block text-[11px] font-semibold uppercase tracking-wide text-muted-foreground md:hidden">{label}</span>
      <span className={`block break-words text-sm ${prominent ? "font-semibold" : ""} ${mono ? "font-mono text-xs" : ""}`}>{value}</span>
    </div>
  );
}
