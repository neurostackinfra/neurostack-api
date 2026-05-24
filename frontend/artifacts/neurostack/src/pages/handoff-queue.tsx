import { useEffect, useState, useCallback } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Clock, CheckCircle, ExternalLink, Download } from "lucide-react";

interface HandoffQueueItem {
  id: string;
  numero_whatsapp: string | null;
  session_id: string;
  intencao_detectada: string | null;
  estagio_atual: string | null;
  primeira_linha_mensagem: string | null;
  created_at: string;
  data_hora_saida: string | null;
  status: string;
}

interface QueueStats {
  avg_wait_seconds: number;
  pct_within_sla: number;
}

function getToken(): string {
  try {
    return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? "";
  } catch {
    return "";
  }
}

function getSlaColor(seconds: number): string {
  if (seconds < 300) return "border-green-500 bg-green-50 dark:bg-green-950/30";
  if (seconds < 600) return "border-yellow-500 bg-yellow-50 dark:bg-yellow-950/30";
  return "border-red-500 bg-red-50 dark:bg-red-950/30";
}

function cleanNumber(s: string): string {
  return s.replace(/\D/g, "");
}

function formatWait(s: number): string {
  const mins = Math.floor(s / 60);
  const secs = s % 60;
  return `${mins}m ${secs}s`;
}

export default function HandoffQueuePage() {
  const { tenantId, isBuilder, isSuperAdmin, viewingClinicId } = useAuth();
  const { t } = useI18n();
  const { toast } = useToast();

  const effectiveTenantId = (isBuilder || isSuperAdmin) ? viewingClinicId : tenantId;

  const [handoffs, setHandoffs] = useState<HandoffQueueItem[]>([]);
  const [stats, setStats] = useState<QueueStats>({ avg_wait_seconds: 0, pct_within_sla: 0 });
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState<Date>(new Date());
  const [fadingIds, setFadingIds] = useState<Set<string>>(new Set());

  const fetchQueue = useCallback(async () => {
    if (!effectiveTenantId) return;
    const token = getToken();
    if (!token) return;
    try {
      setLoading(true);
      const res = await fetch(`/v1/handoff/queue?tenant_id=${effectiveTenantId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data = await res.json();
      setHandoffs(data.items ?? []);
      setStats({ avg_wait_seconds: data.avg_wait_seconds ?? 0, pct_within_sla: data.pct_within_sla ?? 0 });
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, [effectiveTenantId]);

  // 1-second global tick
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  // 30-second polling
  useEffect(() => {
    fetchQueue();
    const id = setInterval(fetchQueue, 30000);
    return () => clearInterval(id);
  }, [fetchQueue]);

  const handleCheckout = async (id: string) => {
    const token = getToken();
    if (!token) return;
    try {
      const res = await fetch(`/v1/handoff/queue/${id}/checkout`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 409) {
        toast({ title: "Already attended", variant: "destructive" });
        return;
      }
      if (!res.ok) return;
      setFadingIds((prev) => new Set(prev).add(id));
      setTimeout(() => {
        setHandoffs((prev) => prev.filter((h) => h.id !== id));
        setFadingIds((prev) => { const n = new Set(prev); n.delete(id); return n; });
      }, 300);
    } catch {
      // silent
    }
  };

  const handleAtendido = (id: string) => handleCheckout(id);

  const handleWhatsApp = (id: string, numero: string | null) => {
    if (numero) {
      window.open(`https://wa.me/${cleanNumber(numero)}`, "_blank");
    }
    handleCheckout(id);
  };

  const handleExport = async () => {
    if (!effectiveTenantId) return;
    const token = getToken();
    if (!token) return;
    try {
      const res = await fetch(`/v1/handoff/queue/export?tenant_id=${effectiveTenantId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "handoffs.csv";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      // silent
    }
  };

  if (!effectiveTenantId) {
    return (
      <div className="flex items-center justify-center h-64">
        <p className="text-muted-foreground">{t("handoff_no_clinic")}</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("handoff_queue_title")}</h1>
        <Button variant="outline" size="sm" onClick={handleExport} className="gap-1.5">
          <Download className="h-4 w-4" />
          {t("handoff_export_csv")}
        </Button>
      </div>

      {/* Tab nav */}
      <div className="flex gap-4 border-b">
        <button className="pb-2 text-sm font-semibold border-b-2 border-primary">
          {t("handoff_queue_tab_geral")}
        </button>
        <button className="pb-2 text-sm opacity-50 cursor-not-allowed pointer-events-none">
          {t("handoff_queue_tab_setor")}
        </button>
      </div>

      {/* Analytics strip */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="flex items-center gap-3 rounded-lg border p-4">
          <Clock className="h-5 w-5 text-muted-foreground" />
          <div>
            <p className="text-xs text-muted-foreground">{t("handoff_avg_wait")}</p>
            <p className="text-lg font-semibold">{formatWait(Math.round(stats.avg_wait_seconds))}</p>
          </div>
        </div>
        <div className="flex items-center gap-3 rounded-lg border p-4">
          <CheckCircle className="h-5 w-5 text-muted-foreground" />
          <div>
            <p className="text-xs text-muted-foreground">{t("handoff_pct_sla")}</p>
            <p className="text-lg font-semibold">{stats.pct_within_sla}%</p>
          </div>
        </div>
      </div>

      {/* Color legend */}
      <div className="flex flex-wrap gap-3 text-xs">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-3 rounded-full bg-green-500" />
          {t("handoff_legend_green")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-3 rounded-full bg-yellow-500" />
          {t("handoff_legend_yellow")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-3 w-3 rounded-full bg-red-500" />
          {t("handoff_legend_red")}
        </span>
      </div>

      {/* Card grid */}
      {handoffs.length === 0 && !loading && (
        <p className="text-center text-muted-foreground py-12">{t("handoff_empty")}</p>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {handoffs.map((item) => {
          const secondsWaiting = Math.max(0, Math.floor((now.getTime() - new Date(item.created_at).getTime()) / 1000));
          const isFading = fadingIds.has(item.id);

          return (
            <div
              key={item.id}
              className={`border-2 rounded-xl p-4 transition-all duration-300 ${getSlaColor(secondsWaiting)} ${isFading ? "opacity-0" : "opacity-100"}`}
            >
              {/* WhatsApp number */}
              <p className="text-2xl font-bold tracking-wide">{item.numero_whatsapp || "—"}</p>

              {/* Session code + status */}
              <div className="flex items-center gap-2 mt-1">
                <span className="text-xs text-muted-foreground">
                  {t("handoff_session_code")}: {item.session_id.slice(0, 8)}
                </span>
                <span className="text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded bg-muted">
                  {item.status}
                </span>
              </div>

              {/* Details */}
              <div className="mt-3 space-y-1 text-sm">
                {item.intencao_detectada && (
                  <p><span className="text-muted-foreground">{t("handoff_intent")}:</span> {item.intencao_detectada}</p>
                )}
                {item.estagio_atual && (
                  <p><span className="text-muted-foreground">{t("handoff_stage")}:</span> {item.estagio_atual}</p>
                )}
                <p><span className="text-muted-foreground">{t("handoff_entry_time")}:</span> {new Date(item.created_at).toLocaleString()}</p>
              </div>

              {/* Countdown */}
              <p className="text-sm font-mono mt-2">{t("handoff_waiting_since")} {formatWait(secondsWaiting)}</p>

              {/* First message */}
              {item.primeira_linha_mensagem && (
                <p className="text-xs text-muted-foreground truncate mt-2">{item.primeira_linha_mensagem}</p>
              )}

              {/* Buttons */}
              <div className="flex gap-2 mt-4">
                <Button size="sm" onClick={() => handleAtendido(item.id)}>
                  {t("handoff_btn_attended")}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => handleWhatsApp(item.id, item.numero_whatsapp)}
                >
                  <ExternalLink className="h-4 w-4 mr-1" />
                  WhatsApp
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
