import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Bot, ChevronLeft, ChevronRight, RefreshCw, UserX } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { useToast } from "@/hooks/use-toast";
import { getBucketLabel, getIntentBucket } from "@/lib/intent-presentation";

const PAGE_SIZE = 20;
const POLL_INTERVAL_MS = 30_000;
const SUCCESS_TRANSITION_MS = 600;

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
  const { tenantId, isBuilder, isSuperAdmin, viewingClinicId } = useAuth();
  const { locale, t } = useI18n();
  const { toast } = useToast();
  const selectedTenantId = isBuilder || isSuperAdmin ? viewingClinicId : null;
  const hasRequiredClinic = !(isBuilder || isSuperAdmin) || Boolean(selectedTenantId);
  const tenantContextKey = hasRequiredClinic
    ? `${isBuilder || isSuperAdmin ? "override" : "own"}:${selectedTenantId ?? tenantId ?? ""}`
    : "no-clinic";

  const [pagination, setPagination] = useState({ tenantContextKey, page: 1 });
  const page = pagination.tenantContextKey === tenantContextKey ? pagination.page : 1;
  const contextKey = `${tenantContextKey}:page:${page}`;
  const [queueState, setQueueState] = useState<QueueState>(() => initialQueueState(contextKey, hasRequiredClinic));
  const visibleState = queueState.contextKey === contextKey
    ? queueState
    : initialQueueState(contextKey, hasRequiredClinic);
  const { items, total, hasNext, hasPrevious, loading, error, pendingIds, successfulIds } = visibleState;

  const requestSequenceRef = useRef(0);
  const activeRequestRef = useRef<AbortController | null>(null);
  const mutationControllersRef = useRef<Map<string, AbortController>>(new Map());
  const protectedItemsRef = useRef<Map<string, ProtectedItem>>(new Map());
  const removalTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const generationRef = useRef(0);
  const contextIdentityRef = useRef<ContextIdentity>({ key: "", generation: 0, mounted: false });

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
    setQueueState(initialQueueState(contextKey, hasRequiredClinic));

    return () => {
      if (contextIdentityRef.current.generation === identity.generation) {
        contextIdentityRef.current = { ...identity, mounted: false };
      }
      clearAsyncWork();
    };
  }, [clearAsyncWork, contextKey, hasRequiredClinic]);

  const fetchQueue = useCallback(async (identity: ContextIdentity, showLoading = false) => {
    if (!isCurrentContext(identity)) return;
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
  }, [hasRequiredClinic, isCurrentContext, page, selectedTenantId]);

  useEffect(() => {
    const identity = contextIdentityRef.current;
    void fetchQueue(identity, true);
    const pollId = window.setInterval(() => void fetchQueue(identity, false), POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(pollId);
      activeRequestRef.current?.abort();
    };
  }, [contextKey, fetchQueue]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const formattedActivity = useMemo(() => new Intl.DateTimeFormat(
    locale === "pt" ? "pt-BR" : "en-US",
    { dateStyle: "short", timeStyle: "short" },
  ), [locale]);

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
      const response = await fetch(`/v1/sessions/${encodeURIComponent(sessionId)}/reactivate${query}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("inactive_reactivation_failed");
      const result: unknown = await response.json();
      if (!isReactivationResponse(result, sessionId)) throw new Error("inactive_reactivation_invalid_response");
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
            successfulIds: next,
          };
        });
        removalTimersRef.current.delete(sessionId);
        void fetchQueue(identity, false);
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
          <div className="hidden grid-cols-[1.2fr_.8fr_.65fr_1fr_1fr_1fr_1.25fr] gap-4 border-b bg-muted/40 px-5 py-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground md:grid">
            <span>{t("inactive_contact")}</span>
            <span>{t("inactive_session_code")}</span>
            <span>{t("inactive_channel")}</span>
            <span>{t("inactive_last_intent")}</span>
            <span>{t("inactive_funnel_stage")}</span>
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
                  className={`grid grid-cols-1 gap-3 border-b px-5 py-4 transition-all duration-500 last:border-b-0 md:grid-cols-[1.2fr_.8fr_.65fr_1fr_1fr_1fr_1.25fr] md:items-center md:gap-4 ${successful ? "bg-emerald-500/5 opacity-70" : ""}`}
                >
                  <QueueField label={t("inactive_contact")} value={item.contact_identifier || "—"} prominent />
                  <QueueField label={t("inactive_session_code")} value={shortSessionCode(item.session_id)} mono />
                  <QueueField label={t("inactive_channel")} value={item.channel} />
                  <QueueField
                    label={t("inactive_last_intent")}
                    value={item.last_intent ? getBucketLabel(getIntentBucket(item.last_intent), t) : "—"}
                  />
                  <QueueField label={t("inactive_funnel_stage")} value={item.funnel_stage || "—"} />
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
