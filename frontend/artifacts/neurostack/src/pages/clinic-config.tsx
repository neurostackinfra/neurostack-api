import { useEffect, useState, useCallback } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";

interface ClinicProfessional {
  name: string;
  description: string;
  specialty: string;
}

interface ClinicService {
  name: string;
  description: string;
  price?: string;
  authority_notes?: string;
}
import { useToast } from "@/hooks/use-toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sliders, Plus, Trash2, Save, Loader2, Plug, User, Building2,
  Shield, PhoneForwarded, Boxes,
  Copy, Check, KeyRound, AlertTriangle, CheckCircle2, XCircle, Link2,
  Cpu, MessageSquare, Lock, ChevronDown, ExternalLink, FileText,
} from "lucide-react";

// ── API Credentials (Tab 6) ───────────────────────────────────────────────────

const API_BASE  = import.meta.env.BASE_URL.replace(/\/$/, "");

function getToken(): string {
  try { return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? ""; }
  catch { return ""; }
}

interface CredentialStatus {
  // Z-API
  zapi_instance_id:         string | null;
  zapi_token_set:           boolean;
  zapi_token_masked:        string | null;
  // Evolution API
  whatsapp_provider:        string | null;
  evolution_api_url:        string | null;
  evolution_instance_name:  string | null;
  evolution_api_key_set:    boolean;
  evolution_api_key_masked: string | null;
  // LLM
  llm_api_key_set:          boolean;
  llm_api_key_masked:       string | null;
  llm_base_url:             string | null;
  llm_model_name:           string | null;
  apis_configured:          boolean;
}
interface ClinicSlim { clinic_id: number; clinic_name: string; }

function StatusDot({ configured }: { configured: boolean }) {
  return configured
    ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
    : <XCircle      className="h-3.5 w-3.5 text-slate-500 shrink-0" />;
}

type LlmProvider = "openai" | "dropout" | "slm_cortex" | "outros";
type WhatsappProvider = "zapi" | "evolution";

const WA_PROVIDERS: { id: WhatsappProvider; label: string; sub: string; activeCls: string }[] = [
  { id: "zapi",      label: "Z-API",        sub: "by Z-API.io",   activeCls: "border-[#25D366]/50 text-[#25D366] bg-[#25D366]/10" },
  { id: "evolution", label: "Evolution API", sub: "Open Source",   activeCls: "border-purple-500/50 text-purple-400 bg-purple-500/10" },
];

const PROVIDERS: { id: LlmProvider; label: string; sub: string; activeCls: string }[] = [
  { id: "slm_cortex", label: "SLM Cortex", sub: "by NeuroStack", activeCls: "border-violet-500/50 text-violet-400 bg-violet-500/10"  },
  { id: "dropout",    label: "Dropout",    sub: "by Neuralake",  activeCls: "border-blue-500/50 text-blue-400 bg-blue-500/10"         },
  { id: "openai",     label: "GPT",        sub: "by OpenAI",     activeCls: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10"},
  { id: "outros",     label: "Outros",     sub: "Custom / OSS",  activeCls: "border-slate-500/50 text-slate-300 bg-slate-500/10"     },
];

function inferProvider(url: string | null): LlmProvider {
  if (!url) return "openai";
  if (url.includes("neuralake")) return "dropout";
  if (url.includes("neurostack") || url.includes("cortex")) return "slm_cortex";
  if (url.includes("openai.com")) return "openai";
  return "outros";
}

const PROVIDER_DEFAULT_URL: Record<LlmProvider, string> = {
  openai:     "https://api.openai.com/v1",
  dropout:    "https://api.neuralake.cloud/v1",
  slm_cortex: "",
  outros:     "",
};

function ApiInput({
  label, value, onChange, type = "text", placeholder, disabled, configured, mono,
}: {
  label: string; value: string; onChange?: (v: string) => void; type?: string;
  placeholder?: string; disabled?: boolean; configured?: boolean; mono?: boolean;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-muted-foreground">
        {label}
        {configured && <span className="text-emerald-400 ml-1.5">● configurado</span>}
      </label>
      <input
        type={type}
        value={value}
        onChange={e => onChange?.(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={`w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50 disabled:cursor-not-allowed transition-opacity ${mono ? "font-mono" : ""}`}
      />
    </div>
  );
}

function ApiCredentialsForm({ clinicId, isReadOnly, isSuperAdmin }: {
  clinicId: string; isReadOnly: boolean; isSuperAdmin: boolean;
}) {
  const { toast } = useToast();
  const [clinics, setClinics]               = useState<ClinicSlim[]>([]);
  const [selectedClinicId, setSelectedClinicId] = useState<string>(clinicId);
  const [status, setStatus]                 = useState<CredentialStatus | null>(null);
  const [loading, setLoading]               = useState(false);
  const [saving, setSaving]                 = useState(false);
  const [error, setError]                   = useState<string | null>(null);
  const [copiedWebhook, setCopiedWebhook]   = useState(false);

  // WhatsApp provider selector
  const [waProvider, setWaProvider]         = useState<WhatsappProvider>("zapi");

  // Z-API fields
  const [zapiInstanceId, setZapiInstanceId] = useState("");
  const [zapiToken, setZapiToken]           = useState("");
  const [zapiClientToken, setZapiClientToken] = useState("");

  // Evolution API fields
  const [evolApiUrl, setEvolApiUrl]           = useState("");
  const [evolInstanceName, setEvolInstanceName] = useState("");
  const [evolApiKey, setEvolApiKey]           = useState("");

  // LLM
  const [llmProvider, setLlmProvider]       = useState<LlmProvider>("openai");
  const [llmApiKey, setLlmApiKey]           = useState("");
  const [llmApiUrl, setLlmApiUrl]           = useState("");
  const [llmModelName, setLlmModelName]     = useState("");
  const [llmFallback, setLlmFallback]       = useState<"openai" | "dropout">("dropout");

  useEffect(() => {
    if (!isSuperAdmin) return;
    void fetch(`/v1/admin/clinics`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then(r => r.json()).then((d: ClinicSlim[]) => setClinics(d)).catch(() => {});
  }, [isSuperAdmin]);

  const targetId   = isSuperAdmin ? selectedClinicId : clinicId;
  const webhookUrl = waProvider ? `${window.location.origin}/v1/webhook/${waProvider}` : "";

  const fetchStatus = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const res = await fetch(`/v1/config/api-credentials?clinic_id=${targetId}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) { setError("Erro ao carregar configurações."); return; }
      const data = await res.json() as CredentialStatus;
      setStatus(data);
      // WhatsApp provider + fields
      const savedWaProvider = (data.whatsapp_provider ?? "zapi") as WhatsappProvider;
      setWaProvider(savedWaProvider);
      setZapiInstanceId(data.zapi_instance_id ?? "");
      setEvolApiUrl(data.evolution_api_url ?? "");
      setEvolInstanceName(data.evolution_instance_name ?? "");
      // LLM
      const inferredProvider = inferProvider(data.llm_base_url);
      setLlmModelName(inferredProvider === "dropout" ? "text" : (data.llm_model_name ?? ""));
      setLlmProvider(inferredProvider);
      setLlmApiUrl(data.llm_base_url ?? PROVIDER_DEFAULT_URL[inferredProvider]);
      // WhatsApp secret fields: left empty so the placeholder "••••••••• (manter atual)"
      // appears when a token is already set. On save, empty = not sent → COALESCE
      // in the UPDATE preserves the existing encrypted value in the DB.
      setZapiToken("");
      setEvolApiKey("");
      // LLM API key: send partial mask (sk-pro****9xyz) so the user can visually
      // confirm which key is configured. Backend detects "****" and skips update.
      setLlmApiKey(data.llm_api_key_masked ?? "");
    } catch { setError("Erro de rede."); }
    finally { setLoading(false); }
  }, [targetId]);

  useEffect(() => { void fetchStatus(); }, [fetchStatus]);

  const handleSave = async () => {
    setSaving(true); setError(null);
    try {
      const body: Record<string, string | number> = { clinic_id: targetId };
      // WhatsApp provider selection — always send to persist the chosen provider
      body["whatsapp_provider"] = waProvider;
      if (waProvider === "zapi") {
        if (zapiInstanceId)  body["zapi_instance_id"]  = zapiInstanceId;
        if (zapiToken)       body["zapi_token"]        = zapiToken;
        if (zapiClientToken) body["zapi_client_token"] = zapiClientToken;
      } else {
        if (evolApiUrl)      body["evolution_api_url"]      = evolApiUrl;
        if (evolInstanceName) body["evolution_instance_name"] = evolInstanceName;
        if (evolApiKey)      body["evolution_api_key"]      = evolApiKey;
      }
      // Only send LLM api key if the user actually typed a new one (non-empty)
      if (llmApiKey)      body["llm_api_key"]       = llmApiKey;
      // Always persist the current URL and provider (even if unchanged)
      if (llmProvider !== "slm_cortex") {
        body["llm_api_url"]  = llmApiUrl || PROVIDER_DEFAULT_URL[llmProvider];
        body["llm_provider"] = llmProvider;
      }
      if (llmProvider === "dropout") body["llm_model_name"] = "text";
      else if (llmModelName && (llmProvider === "openai" || llmProvider === "outros"))  body["llm_model_name"] = llmModelName;
      const saveUrl = targetId
        ? `/v1/config/api-credentials?clinic_id=${targetId}`
        : `/v1/config/api-credentials`;
      const res = await fetch(saveUrl, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
        body: JSON.stringify(body),
      });
      const data = await res.json() as { ok?: boolean; apis_configured?: boolean; error?: string };
      if (!res.ok) { setError(data.error ?? "Erro ao salvar."); return; }
      toast({ title: "Configurações salvas!", description: data.apis_configured ? "APIs totalmente configuradas ✓" : "Parcialmente configurado." });
      await fetchStatus();
    } catch { setError("Erro de rede."); }
    finally { setSaving(false); }
  };

  function copyWebhook() {
    navigator.clipboard.writeText(webhookUrl).catch(() => {});
    setCopiedWebhook(true);
    setTimeout(() => setCopiedWebhook(false), 2000);
  }

  const isCortex     = llmProvider === "slm_cortex";
  const isOutros     = llmProvider === "outros";
  const isComingSoon = isCortex;

  return (
    <div className="space-y-5">

      {/* ── Super admin clinic picker ── */}
      {isSuperAdmin && clinics.length > 0 && (
        <div className="space-y-1.5">
          <label className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Ver clínica</label>
          <select
            value={selectedClinicId}
            onChange={e => setSelectedClinicId(Number(e.target.value))}
            className="w-full rounded-md border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-1 focus:ring-accent"
          >
            {clinics.map(c => (
              <option key={c.clinic_id} value={c.clinic_id}>{c.clinic_name} (ID {c.clinic_id})</option>
            ))}
          </select>
        </div>
      )}

      {/* ── Webhook URL (only when provider is selected) ── */}
      {waProvider && webhookUrl && (
        <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50/60 dark:bg-blue-950/30 p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Link2 className="h-4 w-4 text-blue-500 shrink-0" />
            <p className="text-[11px] font-semibold uppercase tracking-wider text-blue-700 dark:text-blue-400">
              Webhook URL — {waProvider === "zapi" ? "Z-API" : "Evolution API"}
            </p>
            {status && (
              <span className={`ml-auto text-[10px] font-bold px-2 py-0.5 rounded-full border ${status.apis_configured ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30" : "bg-amber-500/15 text-amber-500 border-amber-500/30"}`}>
                {status.apis_configured ? "Configurado" : "Pendente"}
              </span>
            )}
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">
              Cole na configuração de webhooks do {waProvider === "zapi" ? "Z-API" : "Evolution API"}
            </label>
            <div className="flex gap-2">
              <input
                readOnly value={webhookUrl}
                onClick={e => (e.target as HTMLInputElement).select()}
                className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-mono text-foreground cursor-text focus:outline-none focus:ring-1 focus:ring-accent"
              />
              <button type="button" onClick={copyWebhook}
                className="inline-flex items-center gap-1.5 rounded-md border border-blue-300 dark:border-blue-700 bg-white dark:bg-blue-900/20 px-3 py-1.5 text-xs font-medium text-blue-700 dark:text-blue-300 hover:bg-blue-50 dark:hover:bg-blue-900/40 transition-colors shrink-0"
              >
                {copiedWebhook
                  ? <><Check className="h-3.5 w-3.5 text-emerald-500" /> Copiado!</>
                  : <><Copy className="h-3.5 w-3.5" /> Copiar URL</>}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading && <div className="flex items-center gap-2 text-muted-foreground text-sm"><Loader2 className="h-4 w-4 animate-spin" /> Carregando...</div>}

      {/* ── Status dots ── */}
      {status && !loading && (
        <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
          {waProvider === "zapi" ? (
            <>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.zapi_instance_id} /> Z-API Instance</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={status.zapi_token_set} /> Z-API Token</span>
            </>
          ) : (
            <>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.evolution_api_url} /> Evol. API URL</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.evolution_instance_name} /> Instance Name</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={status.evolution_api_key_set} /> Evol. API Key</span>
            </>
          )}
          <span className="flex items-center gap-1.5"><StatusDot configured={status.llm_api_key_set} /> LLM API Key</span>
          <span className="flex items-center gap-1.5"><StatusDot configured={!!status.llm_base_url} /> LLM URL</span>
          {llmProvider === "openai" && (
            <span className="flex items-center gap-1.5"><StatusDot configured={!!status.llm_model_name} /> Model Name</span>
          )}
        </div>
      )}

      {/* ── WhatsApp Provider (Unofficial APIs) ── */}
      <div className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4">

        {/* Section header */}
        <div className="flex items-center gap-2">
          <MessageSquare className="h-3.5 w-3.5 text-[#25D366]" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">WhatsApp — Provedor Ativo</p>
          <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded border bg-[#25D366]/10 text-[#25D366] border-[#25D366]/30">Ativo</span>
        </div>

        {/* Provider selector cards */}
        <div className="grid grid-cols-2 gap-2">
          {WA_PROVIDERS.map(wp => (
            <button
              key={wp.id}
              type="button"
              disabled={isReadOnly}
              onClick={() => {
                if (waProvider === wp.id) return;
                setWaProvider(wp.id);
                setZapiInstanceId(""); setZapiToken("");
                setEvolApiUrl(""); setEvolInstanceName(""); setEvolApiKey("");
              }}
              className={`flex flex-col items-start rounded-lg border px-3 py-2.5 text-left transition-all disabled:opacity-60 disabled:cursor-not-allowed ${
                waProvider === wp.id
                  ? wp.activeCls + " border-2"
                  : "border-border text-muted-foreground hover:border-muted-foreground/50"
              }`}
            >
              <span className="text-xs font-bold">{wp.label}</span>
              <span className="text-[10px] opacity-70 mt-0.5">{wp.sub}</span>
            </button>
          ))}
        </div>

        {/* Z-API fields */}
        {waProvider === "zapi" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <ApiInput label="ID da Instância" value={zapiInstanceId} onChange={setZapiInstanceId}
              placeholder="3F13596C8ABCA2F740884AC8C214C6FF" disabled={isReadOnly} mono />
            <ApiInput label="Token da Instância" value={zapiToken} onChange={setZapiToken} type="password"
              placeholder={status?.zapi_token_set ? "••••••••• (manter atual)" : "Cole o token da instância"}
              disabled={isReadOnly} configured={status?.zapi_token_set} />
            <div className="sm:col-span-2">
              <ApiInput label="Client-Token (Segurança)" value={zapiClientToken} onChange={setZapiClientToken} type="password"
                placeholder="Cole o Client-Token da aba Segurança do Z-API"
                disabled={isReadOnly} />
            </div>
          </div>
        )}

        {/* Evolution API fields */}
        {waProvider === "evolution" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <ApiInput label="Global API URL" value={evolApiUrl} onChange={setEvolApiUrl}
                placeholder="https://api.seudominio.com" disabled={isReadOnly} mono />
            </div>
            <ApiInput label="Instance Name" value={evolInstanceName} onChange={setEvolInstanceName}
              placeholder="minha-clinica-prod" disabled={isReadOnly} />
            <ApiInput label="API Key" value={evolApiKey} onChange={setEvolApiKey} type="password"
              placeholder={status?.evolution_api_key_set ? "••••••••• (manter atual)" : "Cole a API Key"}
              disabled={isReadOnly} configured={status?.evolution_api_key_set} />
          </div>
        )}
      </div>

      {/* ── WABA / Official API — Enterprise Em breve ── */}
      <div className="rounded-lg border border-dashed border-slate-600/60 bg-slate-900/20 p-4 space-y-3 select-none pointer-events-none">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-3.5 w-3.5 text-slate-400" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">WABA / WhatsApp Business API (Oficial)</p>
          <span className="ml-auto flex items-center gap-1.5 text-[10px] font-bold px-1.5 py-0.5 rounded border bg-muted text-foreground border-border">
            <Lock className="h-2.5 w-2.5" /> Enterprise — Em breve
          </span>
        </div>
        <p className="text-xs text-slate-400">Conexão direta via Meta Business Platform. Ideal para grandes redes — conformidade total com LGPD/HIPAA.</p>
        <div className="grid gap-3 sm:grid-cols-3">
          {["Phone Number ID", "WABA ID", "Permanent Token"].map(f => (
            <div key={f} className="space-y-1">
              <label className="text-xs font-medium text-slate-500">{f}</label>
              <div className="h-8 rounded-md bg-muted/40 border border-slate-700/50" />
            </div>
          ))}
        </div>
      </div>

      {/* ── LLM Provider selector ── */}
      <div className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4">
        <div className="flex items-center gap-2">
          <Cpu className="h-3.5 w-3.5 text-accent" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">AI Provider — LLM</p>
          {isComingSoon && (
            <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/30">Em breve</span>
          )}
        </div>

        {/* Provider toggle group */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {PROVIDERS.map(p => {
            const active = llmProvider === p.id;
            return (
              <button
                key={p.id}
                type="button"
                disabled={isReadOnly}
                onClick={() => {
                  if (llmProvider === p.id) return; // no-op if same tab
                  setLlmProvider(p.id);
                  setLlmApiUrl(PROVIDER_DEFAULT_URL[p.id]);
                  setLlmApiKey(""); // clear key — different providers use different keys
                  setLlmModelName(p.id === "dropout" ? "text" : "");
                }}
                className={`relative flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
                  active
                    ? p.activeCls
                    : "border-border bg-background text-muted-foreground hover:border-slate-500"
                }`}
              >
                <span className="text-xs font-bold">{p.label}</span>
                <span className="text-[10px] opacity-70">{p.sub}</span>
                {active && <span className="absolute top-2 right-2 h-1.5 w-1.5 rounded-full bg-current opacity-80" />}
              </button>
            );
          })}
        </div>

        {/* ── Provider: OpenAI ── */}
        {llmProvider === "openai" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <ApiInput label="API URL" value={llmApiUrl} onChange={setLlmApiUrl}
                placeholder="https://api.openai.com/v1" disabled={isReadOnly} mono />
            </div>
            <ApiInput label="API Key" value={llmApiKey} onChange={setLlmApiKey} type="password"
              placeholder={status?.llm_api_key_set ? "••••••••• (manter atual)" : "sk-..."}
              disabled={isReadOnly} configured={status?.llm_api_key_set} />
            <ApiInput label="Model Name" value={llmModelName} onChange={setLlmModelName}
              placeholder="gpt-4o-mini" disabled={isReadOnly} mono />
          </div>
        )}

        {/* ── Provider: Dropout / Neuralake ── */}
        {llmProvider === "dropout" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <ApiInput label="API URL" value={llmApiUrl} onChange={setLlmApiUrl}
                placeholder="https://api.neuralake.cloud/v1" disabled={isReadOnly} mono />
            </div>
            <ApiInput label="API Key" value={llmApiKey} onChange={setLlmApiKey} type="password"
              placeholder={status?.llm_api_key_set ? "••••••••• (manter atual)" : "ns-live-..."}
              disabled={isReadOnly} configured={status?.llm_api_key_set} />
            <ApiInput label="Model Name" value="text" onChange={() => {}}
              disabled mono />
            <p className="text-[10px] font-mono text-foreground tracking-wide sm:col-span-2">OpenAI SDK Compatible</p>
          </div>
        )}

        {/* ── Provider: SLM Cortex (coming soon) ── */}
        {llmProvider === "slm_cortex" && (
          <div className="space-y-3 opacity-60 pointer-events-none">
            <div className="flex items-start gap-2 rounded-md border border-slate-600 bg-slate-900 px-3 py-2.5 text-xs text-white">
              <Cpu className="h-3.5 w-3.5 mt-0.5 shrink-0 text-violet-400" />
              <p>O SLM Cortex é o modelo sovereign da NeuroStack — treinado com dados clínicos anonimizados em ambiente isolado. A URL de inferência é gerenciada internamente.</p>
            </div>
            <ApiInput label="API Key (NeuroStack)" value={llmApiKey} onChange={setLlmApiKey} type="password"
              placeholder="ns-cortex-..." configured={status?.llm_api_key_set} />
            <div className="space-y-1">
              <label className="text-xs font-medium text-muted-foreground flex items-center gap-2">
                Sistema de Fallback
                <ChevronDown className="h-3 w-3 opacity-50" />
              </label>
              <select
                value={llmFallback}
                onChange={e => setLlmFallback(e.target.value as "openai" | "dropout")}
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:outline-none"
              >
                <option value="dropout">Dropout by Neuralake</option>
                <option value="openai">GPT by OpenAI</option>
              </select>
            </div>
          </div>
        )}

        {/* ── Provider: Outros (Custom / OSS) ── */}
        {isOutros && (
          <div className="space-y-3">
            <ApiInput label="API URL" value={llmApiUrl} onChange={setLlmApiUrl}
              placeholder="https://openrouter.ai/api/v1" disabled={isReadOnly} mono />
            <ApiInput label="API Key" value={llmApiKey} onChange={setLlmApiKey} type="password"
              placeholder={status?.llm_api_key_set ? "••••••••• (manter atual)" : "sk-or-..."}
              disabled={isReadOnly} configured={status?.llm_api_key_set} />
            <ApiInput label="Nome do Modelo" value={llmModelName} onChange={setLlmModelName}
              placeholder="ex: qwen/qwen3-235b-a22b" disabled={isReadOnly} mono />
            <div className="rounded-md border border-border bg-muted/30 px-3 py-2.5 space-y-1.5">
              <p className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">Recomendados no momento:</p>
              <ol className="space-y-0.5 list-none">
                {[
                  "Qwen 3.5-35B-A3B by Alibaba",
                  "Mistral Small 4 by Mistral",
                  "Qwen 3-235B-A22B by Alibaba",
                  "Llama 4 Maverick by Meta",
                  "DeepSeek V3.2 by DeepSeek",
                ].map((model, i) => (
                  <li key={i} className="text-[11px] text-muted-foreground">
                    {i + 1}) {model}
                  </li>
                ))}
              </ol>
            </div>
          </div>
        )}
      </div>

      {error && <p className="flex items-center gap-1.5 text-sm text-destructive"><AlertTriangle className="h-3.5 w-3.5" />{error}</p>}

      <div className="flex items-center justify-between pt-1">
        {isReadOnly && <span className="text-xs text-amber-400 flex items-center gap-1.5"><AlertTriangle className="h-3 w-3" /> Modo somente leitura</span>}
        <button
          type="button"
          onClick={handleSave}
          disabled={saving || isReadOnly || isComingSoon}
          className="ml-auto inline-flex items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-medium text-accent-foreground hover:bg-accent/90 disabled:opacity-40 transition-colors"
        >
          {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
          {isComingSoon ? "Em breve" : "Salvar Configurações de API"}
        </button>
      </div>
    </div>
  );
}

// ── Small layout helpers ──────────────────────────────────────────────────────

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground pb-1 border-b border-border mb-3">
      {children}
    </p>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <Label className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function SwitchRow({
  label,
  hint,
  checked,
  onCheckedChange,
  disabled,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-4 rounded-md border border-border bg-muted/20 px-3 py-3">
      <div className="min-w-0">
        <p className="text-sm font-medium text-foreground">{label}</p>
        {hint && <p className="text-xs text-muted-foreground mt-0.5">{hint}</p>}
      </div>
      <Switch checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} className="shrink-0" />
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

// ── Investor mock config data ──
const INVESTOR_MOCK_CONFIG = {
  clinic_name: "Clínica Estética Lumina",
  main_domain: "estetica",
  agent_name: "Lumina",
  agent_presentation: "name_attendant",
  ai_disclosure: "affirm_ai",
  use_emojis: "yes",
  clinic_history: "Fundada em 2018, a Clínica Estética Lumina nasceu da missão de democratizar tratamentos estéticos com segurança e resultados naturais. Com atendimento humanizado e tecnologia de ponta, já realizamos mais de 15.000 procedimentos.",
  clinic_specialties: "Dermatologia, Medicina Estética, Nutrição Clínica, Fisioterapia Dermatofuncional",
  professionals: [
    { name: "Dra. Ana Lima", specialty: "Dermatologista", description: "Especialista em procedimentos estéticos com 12 anos de experiência, CRM 12345. Mais de 500 aplicações de toxina botulínica." },
    { name: "Dr. Carlos Mendes", specialty: "Cirurgião Plástico", description: "Cirurgião plástico com 8 anos de experiência, membro da SBCP. Especialista em harmonização facial." },
  ],
  full_address: "Rua das Flores, 123 — Sala 45 — Jardim Paulista, São Paulo/SP — CEP 01234-000",
  parking_info: "Estacionamento conveniado no Shopping Paulista, entrada pela Av. Brasil",
  working_hours: "Seg–Sex: 9h–19h | Sáb: 9h–14h | Dom: fechado",
  scheduling_hours: "Seg–Sex: 8h–18h",
  service_format: "both",
  online_services_desc: "Consulta de avaliação, acompanhamento pós-procedimento, orientações nutricionais",
  presential_services_desc: "Aplicação de toxina botulínica, preenchimento labial, limpeza de pele profunda, harmonização facial",
  differentials: "Especialistas com mais de 10 anos de experiência, atendimento humanizado e foco em resultados naturais. Equipamentos de última geração importados da Alemanha.",
  accepts_insurance: "yes",
  insurance_list: "Unimed, Bradesco Saúde, SulAmérica, Amil",
  initial_consultation: "free",
  provide_prices: "yes",
  lgpd_active: "yes",
  lgpd_enabled: "yes",
  language_policy: "neurostack_default",
  handoff_active: "yes",
  standby_mode: "active",
  handoff_period: "permanent",
  services: [
    { name: "Toxina Botulínica (Botox)", description: "Aplicação para rugas dinâmicas na testa, glabela e região dos olhos", price: "R$ 1.200", authority_notes: "Procedimento realizado por Dra. Ana Lima, dermatologista com mais de 500 aplicações bem-sucedidas" },
    { name: "Preenchimento Labial", description: "Ácido hialurônico para volume e contorno labial", price: "R$ 2.500", authority_notes: "Técnica avançada com produto Juvederm, referência mundial em preenchimento" },
    { name: "Limpeza de Pele Profunda", description: "Extração, peeling enzimático, máscara calmante e proteção solar", price: "R$ 350", authority_notes: "Protocolo exclusivo desenvolvido pela equipe de estética da clínica" },
    { name: "Harmonização Facial", description: "Protocolo completo: mandíbula, mento, bigode chinês, olheiras", price: "R$ 4.800", authority_notes: "Realizado por Dr. Carlos Mendes, membro da SBCP com mais de 300 harmonizações" },
  ],
};

export default function ClinicConfig() {
  const { clinicId, isReadOnly, isClinicAdmin, role, isBuilder, isInvestor, viewingClinicId } = useAuth();
  const { t, locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  const { toast } = useToast();

  const [config, setConfig] = useState<any>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    if (isInvestor) {
      setConfig(INVESTOR_MOCK_CONFIG);
      setIsLoading(false);
      return;
    }
    async function loadConfig() {
      setIsLoading(true);
      try {
        const token = getToken();
        const configUrl = isBuilder && viewingClinicId
          ? `/v1/config?tenant_id=${viewingClinicId}`
          : "/v1/config";
        const res = await fetch(configUrl, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!res.ok) throw new Error(`Config fetch failed: ${res.status}`);
        const data = await res.json();
        setConfig(data);
      } catch {
        toast({ title: "Error", description: "Failed to load config", variant: "destructive" });
      } finally {
        setIsLoading(false);
      }
    }
    void loadConfig();
  }, [clinicId, viewingClinicId, isInvestor]);

  // Tab 1 — Identidade
  const [clinicName, setClinicName] = useState("");
  const [mainDomain, setMainDomain] = useState("");
  const [agentName, setAgentName] = useState("");
  const [agentPresentation, setAgentPresentation] = useState("");
  const [aiDisclosure, setAiDisclosure] = useState("");
  const [useEmojis, setUseEmojis] = useState("no");
  const [clinicHistory, setClinicHistory] = useState("");
  const [clinicSpecialties, setClinicSpecialties] = useState("");
  const [professionals, setProfessionals] = useState<ClinicProfessional[]>([]);

  // Tab 2 — Operação
  const [fullAddress, setFullAddress] = useState("");
  const [parkingInfo, setParkingInfo] = useState("");
  const [workingHours, setWorkingHours] = useState("");
  const [schedulingHours, setSchedulingHours] = useState("");
  const [serviceFormat, setServiceFormat] = useState("");
  const [onlineServicesDesc, setOnlineServicesDesc] = useState("");
  const [presentialServicesDesc, setPresentialServicesDesc] = useState("");

  // Tab 3 — Regras de Negócio
  const [differentials, setDifferentials] = useState("");
  const [acceptsInsurance, setAcceptsInsurance] = useState("no");
  const [insuranceList, setInsuranceList] = useState("");
  const [initialConsultation, setInitialConsultation] = useState("");
  const [providePrices, setProvidePrices] = useState("no");
  const [lgpdActive, setLgpdActive] = useState("yes");
  const [privacyLink, setPrivacyLink] = useState("");
  const [lgpdEnabled, setLgpdEnabled] = useState(false);
  const DEFAULT_LGPD_MESSAGE = "Olá! Bem-vindo(a) ao atendimento da [Nome da Clínica]. 🏥 Para garantir o melhor cuidado com a sua saúde e segurança, nossa assistente virtual processa seus dados de acordo com a Lei Geral de Proteção de Dados (LGPD). Ao continuar esta conversa, você concorda com o uso dos seus dados para fins de agendamento e orientações sobre serviços. 📄 Leia nossa Política de Privacidade completa aqui: {{lgpd_link}} Como posso te ajudar hoje?";
  const [lgpdMessage, setLgpdMessage] = useState(DEFAULT_LGPD_MESSAGE);
  const [lgpdMessageEn, setLgpdMessageEn] = useState("");
  const [lgpdLinkMode, setLgpdLinkMode] = useState<"neurostack" | "custom">("neurostack");
  const [lgpdCustomLink, setLgpdCustomLink] = useState("");
  const [clinicLegalName, setClinicLegalName] = useState("");
  const [clinicCnpj, setClinicCnpj] = useState("");
  const [clinicContact, setClinicContact] = useState("");
  const [dpoName, setDpoName] = useState("");
  const [dpoContact, setDpoContact] = useState("");
  const [languagePolicy, setLanguagePolicy] = useState("neurostack_default");
  const [languagePolicyCustom, setLanguagePolicyCustom] = useState("");
  const [agentLanguage, setAgentLanguage] = useState("pt-BR");

  // Tab 4 — Handoff
  const [handoffActive, setHandoffActive] = useState("yes");
  const [standbyMode, setStandbyMode] = useState("active");
  const [handoffPeriod, setHandoffPeriod] = useState("permanent");
  const [handoffCycleDays, setHandoffCycleDays] = useState("");
  const [hitlActive, setHitlActive] = useState(false);
  const [hitlCooldown, setHitlCooldown] = useState("15");

  // Tab 5 — Catálogo de Serviços
  const [services, setServices] = useState<ClinicService[]>([]);


  useEffect(() => {
    if (!config) return;
    setClinicName(config.clinic_name ?? "");
    setMainDomain(config.main_domain ?? "");
    setAgentName(config.agent_name ?? "");
    setAgentPresentation(config.agent_presentation ?? "");
    setAiDisclosure(config.ai_disclosure ?? "");
    setUseEmojis(config.use_emojis ?? "no");
    setClinicHistory(config.clinic_history ?? "");
    const rawSpecialties = config.clinic_specialties;
    setClinicSpecialties(Array.isArray(rawSpecialties) ? rawSpecialties.join(", ") : (rawSpecialties ?? ""));
    setProfessionals(config.professionals ?? []);
    setFullAddress(config.full_address ?? "");
    setParkingInfo(config.parking_info ?? "");
    setWorkingHours(config.working_hours ?? "");
    setSchedulingHours(config.scheduling_hours ?? "");
    setServiceFormat(config.service_format ?? "");
    setDifferentials(config.differentials ?? "");
    setAcceptsInsurance(config.accepts_insurance ?? "no");
    setInsuranceList(config.insurance_list ?? "");
    setInitialConsultation(config.initial_consultation ?? "");
    setProvidePrices(config.provide_prices ?? "no");
    setLgpdActive(config.lgpd_active ?? "yes");
    setPrivacyLink(config.privacy_link ?? "");
    setLgpdEnabled(config.lgpd_enabled === "yes");
    setLgpdMessage(config.lgpd_message || DEFAULT_LGPD_MESSAGE);
    setLgpdMessageEn(config.lgpd_message_en ?? "");
    setLgpdLinkMode(config.lgpd_custom_link ? "custom" : "neurostack");
    setLgpdCustomLink(config.lgpd_custom_link ?? "");
    setClinicLegalName(config.clinic_legal_name ?? "");
    setClinicCnpj(config.clinic_cnpj ?? "");
    setClinicContact(config.clinic_contact ?? "");
    setDpoName(config.dpo_name ?? "");
    setDpoContact(config.dpo_contact ?? "");
    setLanguagePolicy(config.language_policy ?? "neurostack_default");
    setLanguagePolicyCustom(config.language_policy_custom ?? "");
    setAgentLanguage(config.agent_language ?? "pt-BR");
    setHandoffActive(config.handoff_active ?? "yes");
    setStandbyMode(config.standby_mode ?? "active");
    setHandoffPeriod(config.handoff_period ?? "permanent");
    setHandoffCycleDays(config.handoff_cycle_days ?? "");
    setOnlineServicesDesc(config.online_services_desc ?? "");
    setPresentialServicesDesc(config.presential_services_desc ?? "");
    setServices(config.services ?? []);
  }, [config]);

  const addProfessional = () =>
    setProfessionals((prev) => [...prev, { name: "", description: "", specialty: "" }]);
  const removeProfessional = (idx: number) =>
    setProfessionals((prev) => prev.filter((_, i) => i !== idx));
  const updateProfessional = (idx: number, field: keyof ClinicProfessional, value: string) =>
    setProfessionals((prev) => prev.map((p, i) => (i === idx ? { ...p, [field]: value } : p)));

  const addService = () => {
    setServices((prev) => [...prev, { name: "", description: "", price: "", authority_notes: "" }]);
  };
  const removeService = (idx: number) => setServices((prev) => prev.filter((_, i) => i !== idx));
  const updateService = (idx: number, field: keyof ClinicService, value: string) =>
    setServices((prev) => prev.map((s, i) => (i === idx ? { ...s, [field]: value } : s)));

  const handleSave = async () => {
    setIsSaving(true);
    const payload: Record<string, any> = {
      clinic_name: clinicName.trim(),
      agent_name: agentName.trim(),
      differentials: differentials.trim(),
      services: services.filter((s) => s.name.trim()),
      pricing_mode: "consult_only",
      main_domain: mainDomain || null,
      agent_presentation: agentPresentation || null,
      ai_disclosure: aiDisclosure || null,
      use_emojis: useEmojis || null,
      clinic_history: clinicHistory.trim() || null,
      professionals_info: null,
      clinic_specialties: clinicSpecialties.trim() || null,
      professionals: professionals.filter((p) => p.name.trim()),
      full_address: fullAddress.trim() || null,
      parking_info: parkingInfo.trim() || null,
      working_hours: workingHours.trim() || null,
      scheduling_hours: schedulingHours.trim() || null,
      service_format: serviceFormat || null,
      accepts_insurance: acceptsInsurance || null,
      insurance_list: insuranceList.trim() || null,
      initial_consultation: initialConsultation || null,
      provide_prices: providePrices || null,
      lgpd_active: lgpdActive || null,
      privacy_link: privacyLink.trim() || null,
      lgpd_enabled: lgpdEnabled ? "yes" : "no",
      lgpd_message: lgpdMessage.trim() || null,
      lgpd_message_en: lgpdMessageEn.trim() || null,
      lgpd_custom_link: lgpdLinkMode === "custom" ? lgpdCustomLink.trim() || null : null,
      clinic_legal_name: clinicLegalName.trim() || null,
      clinic_cnpj: clinicCnpj.trim() || null,
      clinic_contact: clinicContact.trim() || null,
      dpo_name: dpoName.trim() || null,
      dpo_contact: dpoContact.trim() || null,
      language_policy: languagePolicy || null,
      language_policy_custom: languagePolicyCustom.trim() || null,
      agent_language: agentLanguage || "pt-BR",
      handoff_active: "yes",
      standby_mode: standbyMode || null,
      handoff_period: handoffPeriod || null,
      handoff_cycle_days: handoffCycleDays.trim() || null,
      online_services_desc: onlineServicesDesc.trim() || null,
      presential_services_desc: presentialServicesDesc.trim() || null,
    };
    try {
      const token = getToken();
      const patchUrl = isBuilder && viewingClinicId
        ? `/v1/config?tenant_id=${viewingClinicId}`
        : "/v1/config";
      const res = await fetch(patchUrl, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`Save failed: ${res.status}`);
      const updated = await res.json();
      setConfig(updated);
      toast({ title: t("config_saved"), description: t("config_saved_desc") });
    } catch {
      toast({ title: t("config_save_fail"), description: t("config_save_fail_desc"), variant: "destructive" });
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  const cardShell = "border-border shadow-sm";
  const cardHead = "pb-3 bg-slate-50/50 dark:bg-slate-900/50 border-b border-border";

  return (
    <div className="max-w-2xl mx-auto space-y-5">
      {/* Page header */}
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-accent/10 border border-accent/20">
          <Sliders className="h-5 w-5 text-accent" />
        </div>
        <div>
          <h1 className="text-xl font-semibold text-foreground">{t("config_title")}</h1>
          <p className="text-sm text-muted-foreground">{t("config_subtitle")}</p>
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="identidade" className="w-full">
        <TabsList className={`w-full grid h-auto p-1 bg-muted/50 border border-border rounded-lg ${isClinicAdmin ? "grid-cols-4" : "grid-cols-6"}`}>
          {[
            { value: "identidade", icon: User,          label: t("config_tab_identidade"), clinicHidden: false },
            { value: "operacao",   icon: Building2,      label: t("config_tab_operacao"),   clinicHidden: false },
            { value: "negocio",    icon: Shield,         label: t("config_tab_negocio"),    clinicHidden: false },
            { value: "handoff",    icon: PhoneForwarded, label: t("config_tab_handoff"),    clinicHidden: true },
            { value: "servicos",   icon: Boxes,          label: t("config_tab_servicos"),   clinicHidden: false },
            { value: "integracoes",icon: Plug,           label: t("config_tab_apis"),       clinicHidden: true },
          ].filter(tab => !(isClinicAdmin && tab.clinicHidden)).map(({ value, icon: Icon, label }) => (
            <TabsTrigger
              key={value}
              value={value}
              className="flex flex-col items-center gap-0.5 py-2 text-[10px] font-medium data-[state=active]:bg-background data-[state=active]:shadow-sm"
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </TabsTrigger>
          ))}
        </TabsList>

        {/* ── Tab 1: Identidade ─────────────────────────────────────────────── */}
        <TabsContent value="identidade" className="mt-4">
          <Card className={cardShell}>
            <CardHeader className={cardHead}>
              <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
                {t("config_tab_identidade_header")}
              </CardTitle>
              <CardDescription className="text-xs">
                {t("config_tab_identidade_desc")}
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-5 space-y-4">
              <SectionLabel>{txt("Clínica", "Clinic")}</SectionLabel>
              <div className="grid grid-cols-2 gap-4">
                <Field label={txt("Nome da Clínica", "Clinic Name")}>
                  <Input
                    value={clinicName}
                    onChange={(e) => setClinicName(e.target.value)}
                    placeholder={txt("Ex: Clínica Estética Lumina", "e.g. Lumina Aesthetic Clinic")}
                    disabled={isReadOnly}
                    data-testid="input-clinic-name"
                  />
                </Field>
                <Field label={txt("Domínio Principal", "Main Domain")}>
                  <Select value={mainDomain} onValueChange={setMainDomain} disabled={isReadOnly}>
                    <SelectTrigger className="h-9 text-sm">
                      <SelectValue placeholder={txt("Selecione...", "Select...")} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="medicina">{txt("Medicina", "Medicine")}</SelectItem>
                      <SelectItem value="estetica">{txt("Estética", "Aesthetics")}</SelectItem>
                      <SelectItem value="odonto">{txt("Odontologia", "Dentistry")}</SelectItem>
                      <SelectItem value="bem_estar">{txt("Bem-estar", "Wellness")}</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
              </div>

              <SectionLabel>{txt("Agente", "Agent")}</SectionLabel>
              <Field label={txt("Nome do Agente", "Agent Name")}>
                <Input
                  value={agentName}
                  onChange={(e) => setAgentName(e.target.value)}
                  placeholder={txt("Ex: Lumina, Sofia, Ana", "e.g. Lumina, Sofia, Ana")}
                  disabled={isReadOnly}
                  data-testid="input-agent-name"
                />
              </Field>

              <Field label={txt("Apresentação", "Presentation")}>
                <Select value={agentPresentation} onValueChange={setAgentPresentation} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue placeholder={txt("Selecione o formato...", "Select format...")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="name_only">{txt('Nome — "Olá, sou a Lumina"', 'Name — "Hi, I\'m Lumina"')}</SelectItem>
                    <SelectItem value="name_attendant">{txt("Nome — Atendente Virtual", "Name — Virtual Attendant")}</SelectItem>
                    <SelectItem value="name_ai">{txt("Nome — Assistente IA", "Name — AI Assistant")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>

              <Field label={txt("Resposta sobre ser IA", "Response about being AI")}>
                <Select value={aiDisclosure} onValueChange={setAiDisclosure} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue placeholder={txt("Selecione...", "Select...")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="forward_human">{txt("Encaminha para humano", "Redirects to human")}</SelectItem>
                    <SelectItem value="affirm_ai">{txt("Afirma ser IA — foco em agilidade", "Confirms being AI — speed-focused")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>

              <SwitchRow
                label={txt("Uso de Emojis", "Emoji Usage")}
                hint={txt("Permite uso de emojis nas respostas do agente", "Allows emoji use in agent responses")}
                checked={useEmojis === "yes"}
                onCheckedChange={(v) => setUseEmojis(v ? "yes" : "no")}
                disabled={isReadOnly}
              />

              <SectionLabel>{txt("História & Especialidades", "History & Specialties")}</SectionLabel>
              <Field label={txt("História da Clínica", "Clinic History")} hint={txt("Usada como contexto quando o paciente pergunta sobre a clínica.", "Used as context when the patient asks about the clinic.")}>
                <Textarea
                  value={clinicHistory}
                  onChange={(e) => setClinicHistory(e.target.value)}
                  placeholder={txt("Ex: Fundada em 2015, a Clínica Lumina nasceu da missão de...", "e.g. Founded in 2015, Lumina Clinic was born from the mission of...")}
                  rows={3}
                  className="resize-none text-sm"
                  disabled={isReadOnly}
                />
              </Field>
              <Field label={txt("Especialidades da Clínica", "Clinic Specialties")} hint={txt("Liste as especialidades separadas por vírgula.", "List specialties separated by commas.")}>
                <Textarea
                  value={clinicSpecialties}
                  onChange={(e) => setClinicSpecialties(e.target.value)}
                  placeholder={txt("Ex: Dermatologia, Medicina Estética, Nutrição Clínica, Fisioterapia", "e.g. Dermatology, Aesthetic Medicine, Clinical Nutrition, Physiotherapy")}
                  rows={2}
                  className="resize-none text-sm"
                  disabled={isReadOnly}
                  data-testid="textarea-clinic-specialties"
                />
              </Field>

              <div className="flex items-center justify-between pt-1">
                <SectionLabel>{txt("Equipe", "Team")}</SectionLabel>
                {!isReadOnly && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={addProfessional}
                  className="gap-1 h-6 text-xs mb-3"
                  data-testid="btn-add-professional"
                >
                  <Plus className="h-3.5 w-3.5" />
                  {t("config_add")}
                </Button>
                )}
              </div>

              {professionals.length === 0 && (
                <p className="text-xs text-muted-foreground italic text-center py-3 border border-dashed border-border rounded-md">
                  {txt("Nenhum profissional cadastrado ainda.", "No professionals registered yet.")}
                </p>
              )}

              {professionals.map((pro, idx) => (
                <div
                  key={idx}
                  className="group relative rounded-md border border-border bg-muted/20 p-4 space-y-3"
                  data-testid={`professional-item-${idx}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-mono font-bold uppercase tracking-wider text-muted-foreground">
                      {txt("Profissional", "Professional")} {idx + 1}
                    </span>
                    {!isReadOnly && (
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => removeProfessional(idx)}
                      className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                      data-testid={`btn-remove-professional-${idx}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                    )}
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <Field label={t("config_name_label")}>
                      <Input
                        value={pro.name}
                        onChange={(e) => updateProfessional(idx, "name", e.target.value)}
                        placeholder={txt("Ex: Dra. Ana Lima", "e.g. Dr. Ana Lima")}
                        className="h-8 text-sm"
                        disabled={isReadOnly}
                        data-testid={`input-professional-name-${idx}`}
                      />
                    </Field>
                    <Field label={txt("Especialidade", "Specialty")}>
                      <Input
                        value={pro.specialty}
                        onChange={(e) => updateProfessional(idx, "specialty", e.target.value)}
                        placeholder={txt("Ex: Dermatologista", "e.g. Dermatologist")}
                        className="h-8 text-sm"
                        disabled={isReadOnly}
                        data-testid={`input-professional-specialty-${idx}`}
                      />
                    </Field>
                  </div>

                  <Field label={t("config_desc_label")} hint={txt("Formação, anos de experiência, diferenciais.", "Education, years of experience, differentials.")}>
                    <Textarea
                      value={pro.description}
                      onChange={(e) => updateProfessional(idx, "description", e.target.value)}
                      placeholder={txt("Ex: Especialista em procedimentos estéticos com 12 anos de experiência, CRM 12345", "e.g. Specialist in aesthetic procedures with 12 years of experience, CRM 12345")}
                      rows={2}
                      className="resize-none text-sm"
                      disabled={isReadOnly}
                      data-testid={`input-professional-description-${idx}`}
                    />
                  </Field>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── Tab 2: Operação ───────────────────────────────────────────────── */}
        <TabsContent value="operacao" className="mt-4">
          <Card className={cardShell}>
            <CardHeader className={cardHead}>
              <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
                {t("config_tab_operacao_header")}
              </CardTitle>
              <CardDescription className="text-xs">
                {t("config_tab_operacao_desc")}
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-5 space-y-4">
              <SectionLabel>{txt("Localização", "Location")}</SectionLabel>
              <Field label={txt("Endereço Completo", "Full Address")}>
                <Textarea
                  value={fullAddress}
                  onChange={(e) => setFullAddress(e.target.value)}
                  placeholder={txt("Ex: Rua das Flores, 123 — Sala 45 — Jardim Paulista, São Paulo/SP — CEP 01234-000", "e.g. 123 Flower St, Suite 45, Downtown, São Paulo/SP — ZIP 01234-000")}
                  rows={2}
                  className="resize-none text-sm"
                  disabled={isReadOnly}
                  data-testid="input-full-address"
                />
              </Field>
              <Field label={txt("Estacionamento / Acesso / Referência", "Parking / Access / Reference")}>
                <Input
                  value={parkingInfo}
                  onChange={(e) => setParkingInfo(e.target.value)}
                  placeholder={txt("Ex: Estacionamento conveniado no Shopping X, entrada pela Av. Brasil", "e.g. Partner parking at Shopping X, entrance on Brasil Ave.")}
                  disabled={isReadOnly}
                />
              </Field>

              <SectionLabel>{txt("Horários", "Hours")}</SectionLabel>
              <Field label={txt("Horário de Funcionamento", "Business Hours")} hint={txt("Exibido ao paciente ao perguntar sobre horários.", "Shown to the patient when asking about hours.")}>
                <Input
                  value={workingHours}
                  onChange={(e) => setWorkingHours(e.target.value)}
                  placeholder={txt("Ex: Seg–Sex: 9h–19h | Sáb: 9h–14h | Dom: fechado", "e.g. Mon–Fri: 9am–7pm | Sat: 9am–2pm | Sun: closed")}
                  disabled={isReadOnly}
                  data-testid="input-working-hours"
                />
              </Field>
              <Field label={txt("Horário do Setor de Agendamento", "Scheduling Department Hours")} hint={txt("Quando difere do horário geral de atendimento.", "When it differs from the general working hours.")}>
                <Input
                  value={schedulingHours}
                  onChange={(e) => setSchedulingHours(e.target.value)}
                  placeholder={txt("Ex: Seg–Sex: 8h–18h (agendamentos apenas por WhatsApp)", "e.g. Mon–Fri: 8am–6pm (scheduling via WhatsApp only)")}
                  disabled={isReadOnly}
                />
              </Field>

              <SectionLabel>{txt("Formato de Atendimento", "Service Format")}</SectionLabel>
              <Field label={txt("Modalidade", "Modality")}>
                <Select value={serviceFormat} onValueChange={setServiceFormat} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue placeholder={txt("Selecione...", "Select...")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="both">{txt("Online e Presencial", "Online and In-Person")}</SelectItem>
                    <SelectItem value="online">{txt("Apenas Online", "Online Only")}</SelectItem>
                    <SelectItem value="presential">{txt("Apenas Presencial", "In-Person Only")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>

              {(serviceFormat === "online" || serviceFormat === "both") && (
                <Field
                  label={txt("Serviços realizados Online", "Online Services")}
                  hint={txt("Quais procedimentos ou consultas são oferecidos de forma remota.", "Which procedures or consultations are offered remotely.")}
                >
                  <Textarea
                    value={onlineServicesDesc}
                    onChange={(e) => setOnlineServicesDesc(e.target.value)}
                    placeholder={txt("Ex: Consulta de avaliação, acompanhamento pós-procedimento, orientações nutricionais", "e.g. Assessment consultation, post-procedure follow-up, nutritional guidance")}
                    rows={3}
                    className="resize-none text-sm"
                    disabled={isReadOnly}
                    data-testid="textarea-online-services"
                  />
                </Field>
              )}

              {(serviceFormat === "presential" || serviceFormat === "both") && (
                <Field
                  label={txt("Serviços realizados Presencialmente", "In-Person Services")}
                  hint={txt("Quais procedimentos exigem presença física na clínica.", "Which procedures require physical presence at the clinic.")}
                >
                  <Textarea
                    value={presentialServicesDesc}
                    onChange={(e) => setPresentialServicesDesc(e.target.value)}
                    placeholder={txt("Ex: Aplicação de toxina botulínica, preenchimento labial, limpeza de pele profunda", "e.g. Botulinum toxin application, lip filler, deep skin cleansing")}
                    rows={3}
                    className="resize-none text-sm"
                    disabled={isReadOnly}
                    data-testid="textarea-presential-services"
                  />
                </Field>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── Tab 3: Regras de Negócio ──────────────────────────────────────── */}
        <TabsContent value="negocio" className="mt-4">
          <Card className={cardShell}>
            <CardHeader className={cardHead}>
              <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
                {t("config_tab_negocio_header")}
              </CardTitle>
              <CardDescription className="text-xs">
                {t("config_tab_negocio_desc")}
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-5 space-y-4">
              <SectionLabel>{txt("Convênios", "Insurance")}</SectionLabel>
              <SwitchRow
                label={txt("Atende Convênios", "Accepts Insurance")}
                checked={acceptsInsurance === "yes"}
                onCheckedChange={(v) => setAcceptsInsurance(v ? "yes" : "no")}
                disabled={isReadOnly}
              />
              {acceptsInsurance === "yes" && (
                <Field label={txt("Lista de Convênios Aceitos", "Accepted Insurance Plans")}>
                  <Textarea
                    value={insuranceList}
                    onChange={(e) => setInsuranceList(e.target.value)}
                    placeholder={txt("Ex: Unimed, Bradesco Saúde, SulAmérica, Amil", "e.g. Aetna, Cigna, UnitedHealth, BlueCross")}
                    rows={2}
                    className="resize-none text-sm"
                    disabled={isReadOnly}
                  />
                </Field>
              )}

              <SectionLabel>{txt("Consulta Inicial", "Initial Consultation")}</SectionLabel>
              <Field label={txt("Tipo de Consulta Inicial", "Initial Consultation Type")}>
                <Select value={initialConsultation} onValueChange={setInitialConsultation} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue placeholder={txt("Selecione...", "Select...")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="free">{txt("Gratuita / Cortesia", "Free / Complimentary")}</SelectItem>
                    <SelectItem value="paid">{txt("Paga", "Paid")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>

              <SectionLabel>{txt("Política de Preços", "Pricing Policy")}</SectionLabel>
              <SwitchRow
                label={txt("Fornecer Preços ao Paciente", "Provide Prices to Patient")}
                hint={txt("Se desativado, o agente não cita valores e direciona para avaliação", "If disabled, the agent does not quote prices and redirects to evaluation")}
                checked={providePrices === "yes"}
                onCheckedChange={(v) => setProvidePrices(v ? "yes" : "no")}
                disabled={isReadOnly}
              />

              <SectionLabel>{t("config_service_authority")}</SectionLabel>
              <Field
                label={txt("Contexto de Autoridade", "Authority Context")}
                hint={txt("Injetado como contexto quando o SRM detecta pedido de preço — antes de qualquer valor.", "Injected as context when SRM detects a price request — before any value.")}
              >
                <Textarea
                  value={differentials}
                  onChange={(e) => setDifferentials(e.target.value)}
                  placeholder={txt("Ex: Especialistas com mais de 10 anos de experiência, atendimento humanizado e foco em resultados naturais.", "e.g. Specialists with over 10 years of experience, humanized care and a focus on natural results.")}
                  rows={4}
                  className="resize-none text-sm leading-relaxed"
                  disabled={isReadOnly}
                  data-testid="textarea-differentials"
                />
              </Field>

              {/* ── Compliance & LGPD ──────────────────────────────────── */}
              <div className="flex items-center gap-2 pt-2 pb-1">
                <Shield className="h-4 w-4 text-violet-400" />
                <span className="text-sm font-semibold text-foreground">Compliance & LGPD</span>
                <Badge className="bg-violet-500/15 text-violet-400 border-violet-500/30 text-[10px]">🛡️ {txt("Novo", "New")}</Badge>
              </div>

              <SwitchRow
                label={txt("Ativar LGPD no Atendimento", "Enable LGPD in Attendance")}
                hint={txt("Envia mensagem de abertura com aviso de privacidade", "Sends an opening message with a privacy notice")}
                checked={lgpdEnabled}
                onCheckedChange={setLgpdEnabled}
                disabled={isReadOnly}
              />

              {lgpdEnabled && (<>
                <Field
                  label={txt("Mensagem de Abertura", "Opening Message")}
                  hint={txt("Use {{lgpd_link}} onde o link de privacidade será inserido automaticamente", "Use {{lgpd_link}} where the privacy link will be inserted automatically")}
                >
                  <Textarea
                    value={lgpdMessage}
                    onChange={(e) => setLgpdMessage(e.target.value)}
                    placeholder={`Olá! Bem-vindo(a) ao atendimento da ${clinicName || "[Nome da Clínica]"}. 🏥 Para garantir o melhor cuidado com a sua saúde e segurança, nossa assistente virtual processa seus dados de acordo com a LGPD. Ao continuar, você concorda com o uso dos seus dados para fins de agendamento. 📄 Leia nossa Política de Privacidade: {{lgpd_link}} Como posso te ajudar?`}
                    rows={5}
                    className="text-sm"
                    disabled={isReadOnly}
                  />
                  {!lgpdMessage && !isReadOnly && (
                    <button
                      type="button"
                      className="text-[11px] text-violet-400 hover:text-violet-300 transition-colors mt-1"
                      onClick={() => setLgpdMessage(`Olá! Bem-vindo(a) ao atendimento da ${clinicName || "[Nome da Clínica]"}. 🏥 Para garantir o melhor cuidado com a sua saúde e segurança, nossa assistente virtual processa seus dados de acordo com a Lei Geral de Proteção de Dados (LGPD). Ao continuar esta conversa, você concorda com o uso dos seus dados para fins de agendamento e orientações sobre serviços. 📄 Leia nossa Política de Privacidade completa aqui: {{lgpd_link}} Como posso te ajudar hoje?`)}
                    >
                      ✦ {txt("Usar modelo padrão NeuroStack", "Use NeuroStack default template")}
                    </button>
                  )}
                </Field>

                <Field
                  label={txt("Mensagem de Abertura (English)", "Opening Message (English)")}
                  hint={txt(
                    "Usado quando o Idioma do Agente estiver configurado como Inglês.",
                    "Used when Agent Language is set to English."
                  )}
                >
                  <Textarea
                    value={lgpdMessageEn}
                    onChange={(e) => setLgpdMessageEn(e.target.value)}
                    placeholder="Hello! Welcome to {{clinic_name}}. 🏥 ..."
                    rows={5}
                    className="resize-none text-sm"
                    disabled={isReadOnly}
                  />
                </Field>

                <Field label={txt("Link de Privacidade", "Privacy Link")} hint="">
                  <div className="flex flex-col gap-2">
                    <div className="flex gap-4">
                      {(["neurostack", "custom"] as const).map((mode) => (
                        <label key={mode} className="flex items-center gap-2 cursor-pointer select-none">
                          <div
                            className={`h-4 w-4 rounded-full border-2 flex items-center justify-center transition-colors ${lgpdLinkMode === mode ? "border-violet-500 bg-violet-500" : "border-border"}`}
                            onClick={() => !isReadOnly && setLgpdLinkMode(mode)}
                          >
                            {lgpdLinkMode === mode && <div className="h-1.5 w-1.5 rounded-full bg-white" />}
                          </div>
                          <span className="text-sm text-foreground/80">
                            {mode === "neurostack" ? txt("Usar página padrão NeuroStack", "Use NeuroStack default page") : txt("Usar link próprio", "Use own link")}
                          </span>
                        </label>
                      ))}
                    </div>

                    {lgpdLinkMode === "custom" && (
                      <Input
                        value={lgpdCustomLink}
                        onChange={(e) => setLgpdCustomLink(e.target.value)}
                        placeholder="https://suaclinica.com.br/privacidade"
                        type="url"
                        className="text-sm font-mono"
                        disabled={isReadOnly}
                      />
                    )}
                  </div>
                </Field>

                {lgpdLinkMode === "neurostack" && (<>
                  <div className="rounded-md border border-violet-500/20 bg-violet-500/5 p-3 space-y-2">
                    <p className="text-[11px] font-medium text-violet-400">
                      {txt("Dados da página pública de privacidade", "Public privacy page data")}
                    </p>
                    <div className="grid grid-cols-2 gap-2">
                      <Field label={txt("Nome Legal da Clínica", "Clinic Legal Name")} hint="">
                        <Input value={clinicLegalName} onChange={(e) => setClinicLegalName(e.target.value)} placeholder={txt("Clínica Saúde Ltda.", "Health Clinic Ltd.")} className="text-sm h-8" disabled={isReadOnly} />
                      </Field>
                      <Field label={txt("CNPJ", "Tax ID / CNPJ")} hint="">
                        <Input value={clinicCnpj} onChange={(e) => setClinicCnpj(e.target.value)} placeholder="00.000.000/0001-00" className="text-sm h-8 font-mono" disabled={isReadOnly} />
                      </Field>
                    </div>
                    <Field label={txt("E-mail / Telefone de Contato da Clínica", "Clinic Contact Email / Phone")} hint="">
                      <Input value={clinicContact} onChange={(e) => setClinicContact(e.target.value)} placeholder={txt("contato@suaclinica.com.br / (11) 3000-0000", "contact@yourclinic.com / +1 (555) 000-0000")} className="text-sm h-8" disabled={isReadOnly} />
                    </Field>
                    <div className="grid grid-cols-2 gap-2">
                      <Field label={txt("Nome do Encarregado (DPO)", "DPO Name")} hint="">
                        <Input value={dpoName} onChange={(e) => setDpoName(e.target.value)} placeholder={txt("João Silva", "John Smith")} className="text-sm h-8" disabled={isReadOnly} />
                      </Field>
                      <Field label={txt("E-mail / Contato do DPO", "DPO Email / Contact")} hint="">
                        <Input value={dpoContact} onChange={(e) => setDpoContact(e.target.value)} placeholder={txt("dpo@suaclinica.com.br", "dpo@yourclinic.com")} className="text-sm h-8" disabled={isReadOnly} />
                      </Field>
                    </div>
                  </div>

                  <Field label={txt("Sua URL única de Política de Privacidade", "Your unique Privacy Policy URL")} hint={txt("Compartilhe este link com seus pacientes", "Share this link with your patients")}>
                    <div className="flex items-center gap-2">
                      <Input
                        readOnly
                        value={`${window.location.origin}${API_BASE}/privacidade/${clinicId}`}
                        className="text-sm font-mono bg-muted/40 text-muted-foreground"
                      />
                      <a
                        href={`${API_BASE}/privacidade/${clinicId}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-border hover:bg-accent transition-colors shrink-0"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                    </div>
                  </Field>
                </>)}
              </>)}

              <SectionLabel>{txt("Idioma do Agente", "Agent Language")}</SectionLabel>
              <Field
                label={txt("Idioma do Agente", "Agent Language")}
                hint={txt(
                  "Define o idioma das respostas do agente para pacientes em todos os canais (chat, WhatsApp).",
                  "Sets the language the agent uses when responding to patients across all channels (chat, WhatsApp)."
                )}
              >
                <Select value={agentLanguage} onValueChange={setAgentLanguage} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="pt-BR">{txt("Português (BR)", "Portuguese (BR)")}</SelectItem>
                    <SelectItem value="en">{txt("Inglês", "English")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <SectionLabel>{txt("Estilo de Resposta", "Response Style")}</SectionLabel>
              <Field label={txt("Estilo de Resposta", "Response Style")}>
                <Select value={languagePolicy} onValueChange={setLanguagePolicy} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="neurostack_default">{txt("Padrão NeuroStack", "NeuroStack Default")}</SelectItem>
                    <SelectItem value="custom">{txt("Personalizado", "Custom")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              {languagePolicy === "custom" && (
                <Field label={txt("Diretriz Personalizada", "Custom Directive")} hint={txt("Descreva o tom e estilo que o agente deve seguir.", "Describe the tone and style the agent should follow.")}>
                  <Textarea
                    value={languagePolicyCustom}
                    onChange={(e) => setLanguagePolicyCustom(e.target.value)}
                    placeholder={txt("Ex: Responda de forma formal, sem abreviações, usando sempre 'você' e nunca 'vc'.", "e.g. Respond formally, without abbreviations, always using 'you' and never 'u'.")}
                    rows={3}
                    className="resize-none text-sm"
                    disabled={isReadOnly}
                  />
                </Field>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── Tab 4: Handoff ────────────────────────────────────────────────── */}
        <TabsContent value="handoff" className="mt-4">
          <Card className={cardShell}>
            <CardHeader className={cardHead}>
              <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
                {t("config_tab_handoff_header")}
              </CardTitle>
              <CardDescription className="text-xs">
                {t("config_tab_handoff_desc")}
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-5 space-y-4">
              <div className="rounded-md border border-emerald-200 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/30 px-4 py-3 text-sm text-emerald-700 dark:text-emerald-300 flex items-center gap-2">
                <span className="inline-flex items-center rounded-full bg-emerald-500/20 px-2 py-0.5 text-[10px] font-bold tracking-wider text-emerald-600 dark:text-emerald-400">ON</span>
                {txt("Handoff sempre ativo — o agente transfere a conversa para um humano quando necessário.", "Handoff always active — the agent transfers the conversation to a human when needed.")}
              </div>

              {/* ── HITL Block ─────────────────────────────────────── */}
                  <div className="rounded-lg border border-border/60 bg-muted/20 px-4 py-4 space-y-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="space-y-1 flex-1">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm font-semibold text-foreground">
                            {txt("Ativar Human-in-the-Loop (Modo Co-Piloto)", "Enable Human-in-the-Loop (Co-Pilot Mode)")}
                          </span>
                          <span className="inline-flex items-center rounded-full border border-indigo-500/30 bg-indigo-500/10 px-2 py-0.5 text-[10px] font-bold tracking-wider text-indigo-500">
                            {txt("Recomendado · HITL", "Recommended · HITL")}
                          </span>
                          <span className="inline-flex items-center rounded-full border border-purple-500/40 bg-purple-500/10 px-2 py-0.5 text-[10px] font-bold tracking-wider text-purple-400">
                            ROADMAP · V2
                          </span>
                        </div>
                        <p className="text-xs text-muted-foreground leading-relaxed">
                          {txt("Em vez de desligar a IA permanentemente, ela pausará e lerá as mensagens da sua equipe no WhatsApp Web, mantendo o contexto para continuar a venda depois.", "Instead of permanently disabling AI, it will pause and read your team's messages on WhatsApp Web, keeping context to continue the conversation later.")}
                        </p>
                      </div>
                      <Switch
                        checked={hitlActive}
                        disabled={isReadOnly}
                        onCheckedChange={() => {
                          toast({
                            title: txt("Funcionalidade em desenvolvimento", "Feature in development"),
                            description: txt("O Modo Co-Piloto estará disponível na V2.", "Co-Pilot Mode will be available in V2."),
                          });
                        }}
                        className="mt-0.5 shrink-0"
                      />
                    </div>

                    {hitlActive && (
                      <div className="pt-1 border-t border-border/40 space-y-1.5">
                        <label className="text-xs font-semibold text-foreground/80">
                          {txt("Tempo de Pausa Ativa (Cooldown)", "Active Pause Time (Cooldown)")}
                        </label>
                        <Select value={hitlCooldown} onValueChange={setHitlCooldown} disabled={isReadOnly}>
                          <SelectTrigger className="h-9 text-sm">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="5">5 {txt("Minutos", "Minutes")}</SelectItem>
                            <SelectItem value="15">15 {txt("Minutos", "Minutes")}</SelectItem>
                            <SelectItem value="30">30 {txt("Minutos", "Minutes")}</SelectItem>
                          </SelectContent>
                        </Select>
                        <p className="text-[11px] text-muted-foreground">
                          {txt("A IA ficará em silêncio por este período após cada mensagem enviada por um humano.", "AI will stay silent for this period after each message sent by a human.")}
                        </p>
                      </div>
                    )}
                  </div>

                  <SwitchRow
                    label={txt("Modo Stand-by pós-Handoff", "Post-Handoff Stand-by Mode")}
                    hint={txt("A IA fica inativa após acionar handoff até o ciclo se encerrar", "AI goes inactive after triggering handoff until the cycle ends")}
                    checked={standbyMode === "active"}
                    onCheckedChange={(v) => setStandbyMode(v ? "active" : "inactive")}
                    disabled={isReadOnly}
                  />

                  <Field label={txt("Período de Handoff", "Handoff Period")}>
                    <Select value={handoffPeriod} onValueChange={setHandoffPeriod} disabled={isReadOnly}>
                      <SelectTrigger className="h-9 text-sm">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="permanent">{txt("Permanente (até reativação manual)", "Permanent (until manual reactivation)")}</SelectItem>
                        <SelectItem value="cycle">{txt("Ciclo de X dias", "X-day Cycle")}</SelectItem>
                      </SelectContent>
                    </Select>
                  </Field>

                  {handoffPeriod === "cycle" && (
                    <Field label={txt("Duração do Ciclo (dias)", "Cycle Duration (days)")}>
                      <Input
                        type="number"
                        min="1"
                        max="365"
                        value={handoffCycleDays}
                        onChange={(e) => setHandoffCycleDays(e.target.value)}
                        placeholder={txt("Ex: 7", "e.g. 7")}
                        className="w-32 text-sm"
                        disabled={isReadOnly}
                      />
                    </Field>
                  )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── Tab 5: Catálogo de Serviços ──────────────────────────────────── */}
        <TabsContent value="servicos" className="mt-4">
          <Card className={cardShell}>
            <CardHeader className={cardHead}>
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
                    {t("config_services")}
                  </CardTitle>
                  <CardDescription className="text-xs mt-0.5">
                    {txt("Cada serviço é injetado como contexto literal. Os Diferenciais de Autoridade reforçam credibilidade antes de qualquer preço.", "Each service is injected as literal context. Authority Differentials reinforce credibility before any price.")}
                  </CardDescription>
                </div>
                {!isReadOnly && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={addService}
                  className="gap-1 h-7 text-xs shrink-0"
                  data-testid="btn-add-service"
                >
                  <Plus className="h-3.5 w-3.5" />
                  {t("config_add")}
                </Button>
                )}
              </div>
            </CardHeader>
            <CardContent className="pt-5 space-y-4">
              {services.length === 0 && (
                <p className="text-sm text-muted-foreground italic text-center py-6">
                  {t("config_services_empty")}
                </p>
              )}
              {services.map((service, idx) => (
                <div
                  key={idx}
                  className="group relative rounded-md border border-border bg-muted/20 p-4 space-y-3"
                  data-testid={`service-item-${idx}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-mono text-muted-foreground uppercase tracking-wider">
                      {t("config_service_label")} {idx + 1}
                    </span>
                    {!isReadOnly && (
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => removeService(idx)}
                      className="h-6 w-6 opacity-0 group-hover:opacity-100 transition-opacity text-muted-foreground hover:text-destructive"
                      data-testid={`btn-remove-service-${idx}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                    )}
                  </div>

                  <div className="grid grid-cols-[1fr_auto] gap-3">
                    <Field label={t("config_name_label")}>
                      <Input
                        value={service.name}
                        onChange={(e) => updateService(idx, "name", e.target.value)}
                        placeholder={t("config_service_name_ph")}
                        className="h-8 text-sm"
                        disabled={isReadOnly}
                        data-testid={`input-service-name-${idx}`}
                      />
                    </Field>
                    <Field label={t("config_service_price")}>
                      <Input
                        type="text"
                        value={service.price ?? ""}
                        onChange={(e) => updateService(idx, "price", e.target.value)}
                        placeholder="Ex: $150, €100, R$500"
                        className="h-8 text-sm w-36"
                        disabled={isReadOnly}
                      />
                    </Field>
                  </div>

                  <Field label={t("config_desc_label")}>
                    <Textarea
                      value={service.description}
                      onChange={(e) => updateService(idx, "description", e.target.value)}
                      placeholder={t("config_service_desc_ph")}
                      rows={2}
                      className="resize-none text-sm"
                      disabled={isReadOnly}
                      data-testid={`input-service-description-${idx}`}
                    />
                  </Field>

                  <Field
                    label={t("config_service_authority")}
                    hint={t("config_service_authority_hint")}
                  >
                    <Textarea
                      value={service.authority_notes ?? ""}
                      onChange={(e) => updateService(idx, "authority_notes", e.target.value)}
                      placeholder={txt("Ex: Procedimento realizado por especialista certificado pela SBCD com mais de 500 casos de sucesso.", "e.g. Procedure performed by a specialist certified by SBCD with over 500 successful cases.")}
                      rows={2}
                      className="resize-none text-sm"
                      disabled={isReadOnly}
                    />
                  </Field>
                </div>
              ))}
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── Tab 6: APIs ───────────────────────────────────────────────────── */}
        <TabsContent value="integracoes" className="mt-4">
          <Card className={cardShell}>
            <CardHeader className={cardHead}>
              <div className="flex items-center gap-2">
                <Plug className="h-4 w-4 text-accent" />
                <CardTitle className="text-sm font-semibold uppercase tracking-wider text-foreground">
                  {t("config_integrations")}
                </CardTitle>
              </div>
              <CardDescription className="text-xs">{t("config_integrations_desc")}</CardDescription>
            </CardHeader>
            <CardContent className="pt-5">
              <ApiCredentialsForm
                clinicId={isBuilder && viewingClinicId ? viewingClinicId : clinicId}
                isReadOnly={isReadOnly}
                isSuperAdmin={role === "super_admin"}
              />
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      {/* Sticky Save */}
      <div className="flex justify-end pb-8 gap-3 items-center">
        {isReadOnly && (
          <span className="text-xs text-amber-400">
            {t("config_readonly_note")}
          </span>
        )}
        <Button
          onClick={handleSave}
          disabled={isSaving || isReadOnly}
          className="gap-2 bg-accent hover:bg-accent/90 text-accent-foreground font-medium px-6 disabled:opacity-40"
          data-testid="btn-save-config"
        >
          {isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          {t("config_save")}
        </Button>
      </div>
    </div>
  );
}
