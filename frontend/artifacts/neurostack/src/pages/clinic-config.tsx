import { useEffect, useState, useCallback } from "react";
import { useSearch, useLocation } from "wouter";
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

type BusinessDay = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

const BUSINESS_DAYS: BusinessDay[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
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
  Cpu, MessageSquare, Lock, ChevronDown, ExternalLink, FileText, Mic2, Volume2,
  AudioLines,
} from "lucide-react";
import { ProviderCredentialCard } from "@/components/api-settings/provider-credential-card";

// ── API Credentials (Tab 6) ───────────────────────────────────────────────────

const API_BASE  = import.meta.env.BASE_URL.replace(/\/$/, "");

function getToken(): string {
  try { return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? ""; }
  catch { return ""; }
}

type ConfigSaveErrorLocale = "en" | "pt";

const CONFIG_SAVE_FIELD_LABELS: Record<string, Record<ConfigSaveErrorLocale, string>> = {
  clinic_name: { pt: "nome da clínica", en: "clinic name" },
  agent_name: { pt: "nome do agente", en: "agent name" },
  clinic_legal_name: { pt: "razão social", en: "legal clinic name" },
  clinic_cnpj: { pt: "CNPJ", en: "tax ID (CNPJ)" },
  clinic_contact: { pt: "contato da clínica", en: "clinic contact" },
  dpo_name: { pt: "nome do encarregado de dados (DPO)", en: "data protection officer (DPO) name" },
  dpo_contact: { pt: "contato do encarregado de dados (DPO)", en: "data protection officer (DPO) contact" },
  builder_lgpd_consent: { pt: "confirmação do contrato LGPD", en: "LGPD contract confirmation" },
  lgpd_custom_link: { pt: "link personalizado de privacidade", en: "custom privacy link" },
  scheduling_link_url: { pt: "link de agendamento", en: "scheduling link" },
  scheduling_link_message: { pt: "mensagem do link de agendamento", en: "scheduling link message" },
  timezone: { pt: "fuso horário", en: "timezone" },
  business_hours_open: { pt: "horário de abertura", en: "opening time" },
  business_hours_close: { pt: "horário de fechamento", en: "closing time" },
  business_hours_days: { pt: "dias de atendimento", en: "business days" },
  handoff_cycle_days: { pt: "dias do ciclo de handoff", en: "handoff cycle days" },
  handoff_inactive_default_hours: { pt: "horas de inatividade do handoff", en: "handoff inactivity hours" },
  meta_minutos_atendimento: { pt: "meta de minutos para atendimento", en: "service-time target in minutes" },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeConfigFieldLabel(field: unknown, locale: ConfigSaveErrorLocale): string | null {
  if (typeof field !== "string" || !/^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(field)) return null;
  return CONFIG_SAVE_FIELD_LABELS[field]?.[locale] ?? field.replaceAll("_", " ");
}

function configSaveErrorFields(detail: unknown, locale: ConfigSaveErrorLocale): string[] {
  const issues = Array.isArray(detail)
    ? detail
    : isRecord(detail) && Array.isArray(detail.fields)
      ? detail.fields
      : [];
  const fields = issues.flatMap((issue) => {
    if (typeof issue === "string") return [issue];
    if (!isRecord(issue)) return [];
    if (typeof issue.field === "string") return [issue.field];
    if (Array.isArray(issue.loc)) return [issue.loc.at(-1)];
    return [];
  });
  return [...new Set(fields.map((field) => safeConfigFieldLabel(field, locale)).filter((field): field is string => Boolean(field)))];
}

/**
 * Convert structured API failures into actionable copy without ever echoing
 * submitted values or arbitrary backend messages into the UI.
 */
export function describeConfigSaveFailure(
  payload: unknown,
  status: number,
  requestedLocale: string,
): string {
  const locale: ConfigSaveErrorLocale = requestedLocale === "en" ? "en" : "pt";
  const detail = isRecord(payload) && "detail" in payload ? payload.detail : payload;
  const detailRecord = isRecord(detail) ? detail : null;
  const errorCode = typeof detailRecord?.error === "string" ? detailRecord.error : null;

  const knownErrors: Record<string, Record<ConfigSaveErrorLocale, string>> = {
    credential_endpoint_required: {
      pt: "Credenciais de API devem ser salvas na aba Integrações.",
      en: "API credentials must be saved in the Integrations tab.",
    },
    invalid_credentials_payload: {
      pt: "Os dados de credenciais são inválidos. Revise os campos da aba Integrações.",
      en: "The credential data is invalid. Review the fields in the Integrations tab.",
    },
    unsafe_api_url: {
      pt: "A URL da API informada não é permitida. Use um endereço HTTPS público.",
      en: "The API URL is not allowed. Use a public HTTPS address.",
    },
    target_tenant_forbidden: {
      pt: "Você não tem permissão para alterar a configuração desta clínica.",
      en: "You do not have permission to change this clinic's configuration.",
    },
    invalid_target_tenant: {
      pt: "A clínica selecionada é inválida. Selecione novamente e tente salvar.",
      en: "The selected clinic is invalid. Select it again and retry saving.",
    },
    target_tenant_not_found: {
      pt: "A clínica selecionada não foi encontrada.",
      en: "The selected clinic was not found.",
    },
  };
  if (errorCode && knownErrors[errorCode]) return knownErrors[errorCode][locale];

  const fields = configSaveErrorFields(detail, locale);
  if (fields.length > 0) {
    return locale === "en"
      ? `Review the following fields: ${fields.join(", ")}.`
      : `Revise os seguintes campos: ${fields.join(", ")}.`;
  }

  if (status === 401) return locale === "en"
    ? "Your session has expired. Sign in again before saving."
    : "Sua sessão expirou. Entre novamente antes de salvar.";
  if (status === 403) return locale === "en"
    ? "You do not have permission to change this configuration."
    : "Você não tem permissão para alterar esta configuração.";
  if (status === 404) return locale === "en"
    ? "The clinic configuration was not found."
    : "A configuração da clínica não foi encontrada.";
  if (status === 409) return locale === "en"
    ? "The configuration was changed elsewhere. Reload the page and try again."
    : "A configuração foi alterada em outro local. Atualize a página e tente novamente.";
  if (status === 422) return locale === "en"
    ? "Some configuration data is invalid. Review the highlighted fields."
    : "Alguns dados da configuração são inválidos. Revise os campos destacados.";
  if (status >= 500) return locale === "en"
    ? "The server could not save the configuration. Try again in a few moments."
    : "O servidor não conseguiu salvar a configuração. Tente novamente em alguns instantes.";
  return locale === "en"
    ? `The configuration could not be saved (error ${status}).`
    : `Não foi possível salvar a configuração (erro ${status}).`;
}

interface CredentialStatus {
  // Z-API
  zapi_instance_id:         string | null;
  zapi_token_set:           boolean;
  zapi_token_masked:        string | null;
  // Z-API inbound capability token (one-time URL). The backend stores a
  // salted SHA-256 digest so the raw token is never returned by the API.
  zapi_inbound_token_digest_set?: boolean;
  // Evolution API
  whatsapp_provider:        string | null;
  evolution_api_url:        string | null;
  evolution_instance_name:  string | null;
  evolution_api_key_set:    boolean;
  evolution_api_key_masked: string | null;
  // Meta Cloud API (WhatsApp Business — official)
  meta_phone_number_id?:       string | null;
  meta_waba_id?:               string | null;
  meta_permanent_token_set?:   boolean;
  meta_permanent_token_masked?: string | null;
  meta_app_secret_set?:        boolean;
  meta_app_secret_masked?:     string | null;
  // Verify token is not a secret — the user must copy it into the Meta dashboard.
  meta_verify_token?:          string | null;
  // Uazapi (wuzapi-based, WhatsApp Web multi-device)
  uazapi_api_url?:             string | null;
  uazapi_instance_name?:       string | null;
  uazapi_token_set?:           boolean;
  uazapi_token_masked?:        string | null;
  // LLM
  llm_api_key_set:          boolean;
  llm_api_key_masked:       string | null;
  llm_base_url:             string | null;
  llm_model_name:           string | null;
  // Whisper (audio transcription)
  whisper_api_key_set?:     boolean;
  whisper_api_key_masked?:  string | null;
  whisper_model?:           string | null;
  // STT provider
  stt_provider?:            string | null;
  stt_api_url?:             string | null;
  audio_max_seconds?:       number | null;
  // Multimodal transcription (chat model that accepts audio directly)
  multimodal_provider?:     string | null;
  multimodal_api_url?:      string | null;
  multimodal_model_name?:   string | null;
  multimodal_api_key_set?:  boolean;
  multimodal_api_key_masked?: string | null;
  multimodal_readiness?:    string;
  // TTS provider (operational for agent replies)
  tts_provider?:            string | null;
  tts_api_url?:             string | null;
  tts_api_key_set?:         boolean;
  tts_api_key_masked?:      string | null;
  tts_model_name?:          string | null;
  tts_voice_id?:            string | null;
  tts_enabled?:             boolean | null;
  tts_readiness?:           string;
  apis_configured:          boolean;
}
interface ClinicSlim { clinic_id: number; clinic_name: string; }

function StatusDot({ configured }: { configured: boolean }) {
  return configured
    ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
    : <XCircle      className="h-3.5 w-3.5 text-slate-500 shrink-0" />;
}

type LlmProvider = "openai" | "dropout" | "slm_cortex" | "outros";
type WhatsappProvider =
  | "meta_cloud"
  | "dialog360"
  | "twilio"
  | "zapi"
  | "evolution"
  | "uazapi";
type WhatsappProviderGroup = "oficial" | "whatsapp_web";
type SttProvider =
  | "openai_whisper"
  | "elevenlabs_scribe"
  | "google_stt"
  | "deepgram_stt"
  | "microsoft_stt"
  | "outros";
type TtsProvider = "elevenlabs" | "google";
type MultimodalProvider = "openrouter" | "groq" | "openai";

// Providers with operational runtime support today. Others are stored as
// configuration only and surface a "Configuração apenas" badge in the UI.
export const OPERATIONAL_STT_PROVIDERS: ReadonlySet<SttProvider> = new Set([
  "outros",
  "openai_whisper",
]);

// Mirror of `OPERATIONAL_MULTIMODAL_PROVIDERS` in
// `src/services/api_credentials.py`. Only OpenRouter was measured against the
// live API accepting the OGG/Opus WhatsApp delivers; the other two are stored
// as configuration only, so only OpenRouter carries the "recommended" badge.
export const OPERATIONAL_MULTIMODAL_PROVIDERS: ReadonlySet<MultimodalProvider> = new Set([
  "openrouter",
]);

// Every canonical multimodal provider id declared in the backend allowlist
// (`PROVIDER_ALLOWLISTS["multimodal"]` in `src/services/api_credentials.py`).
export const MULTIMODAL_PROVIDERS: {
  id: MultimodalProvider; label: string; sub: string; activeCls: string;
}[] = [
  { id: "openrouter", label: "OpenRouter", sub: "Gemini / GPT-4o audio", activeCls: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10" },
  { id: "groq",       label: "Groq",       sub: "LPU inference",         activeCls: "border-orange-500/50 text-orange-400 bg-orange-500/10"   },
  { id: "openai",     label: "OpenAI",     sub: "GPT-4o audio",          activeCls: "border-slate-500/50 text-slate-300 bg-slate-500/10"     },
];

// The multimodal API URL is DERIVED from the provider, never typed. Egress is
// pinned to exactly these three hosts (`MULTIMODAL_*_HOSTS` in
// `src/services/safe_egress.py`), so a free-text field could only ever hold one
// of these values — and any other value would be accepted by PATCH and then
// fail at runtime with `unsafe_api_url`. Deriving it removes that trap.
export const MULTIMODAL_PROVIDER_API_URL: Record<MultimodalProvider, string> = {
  openrouter: "https://openrouter.ai/api/v1",
  groq:       "https://api.groq.com/openai/v1",
  openai:     "https://api.openai.com/v1",
};

// Capability labels for the backend's stable `fresh_api_key_required` error,
// which names the capability but never a human-readable message.
const CAPABILITY_LABELS: Record<string, { pt: string; en: string }> = {
  llm:        { pt: "do LLM",                     en: "for the LLM"                     },
  multimodal: { pt: "do provedor multimodal",     en: "for the multimodal provider"     },
  stt:        { pt: "do provedor de transcrição", en: "for the transcription provider"  },
  tts:        { pt: "do provedor de voz",         en: "for the voice provider"          },
};

const MULTIMODAL_PROVIDER_MODEL_PLACEHOLDER: Record<MultimodalProvider, string> = {
  openrouter: "google/gemini-2.5-flash",
  groq:       "whisper-large-v3",
  openai:     "gpt-4o-audio-preview",
};

// Providers with a functional backend integration. Selecting any provider
// outside this set still surfaces the credential form (per product design),
// but saving returns a clear "not implemented" error from the backend.
const IMPLEMENTED_WA_PROVIDERS: ReadonlySet<WhatsappProvider> = new Set(["meta_cloud", "zapi", "evolution", "uazapi"]);

// Maps the frontend provider id to the webhook URL segment exposed by the
// backend (e.g. Meta Cloud API is served at `/v1/webhook/meta`, not `/meta_cloud`).
const WEBHOOK_SEGMENT: Partial<Record<WhatsappProvider, string>> = {
  meta_cloud: "meta",
  zapi:       "zapi",
  evolution:  "evolution",
  uazapi:     "uazapi",
};

const WA_PROVIDERS: {
  id: WhatsappProvider;
  group: WhatsappProviderGroup;
  label: string;
  sub: string;
  activeCls: string;
}[] = [
  // Official WhatsApp Business API providers
  { id: "meta_cloud", group: "oficial", label: "Meta Cloud API", sub: "by Meta (direct)",       activeCls: "border-[#25D366]/50 text-[#25D366] bg-[#25D366]/10" },
  { id: "dialog360",  group: "oficial", label: "360dialog",      sub: "BSP oficial",            activeCls: "border-cyan-500/50 text-cyan-400 bg-cyan-500/10" },
  { id: "twilio",     group: "oficial", label: "Twilio",         sub: "BSP oficial",            activeCls: "border-rose-500/50 text-rose-400 bg-rose-500/10" },
  // WhatsApp Web / unofficial providers
  { id: "zapi",       group: "whatsapp_web", label: "Z-API",         sub: "by Z-API.io",  activeCls: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10" },
  { id: "evolution",  group: "whatsapp_web", label: "Evolution API", sub: "Open Source",  activeCls: "border-purple-500/50 text-purple-400 bg-purple-500/10" },
  { id: "uazapi",     group: "whatsapp_web", label: "Uazapi",        sub: "WhatsApp Web", activeCls: "border-orange-500/50 text-orange-400 bg-orange-500/10" },
];

const WA_PROVIDER_LABEL: Record<WhatsappProvider, string> = WA_PROVIDERS.reduce(
  (acc, wp) => ({ ...acc, [wp.id]: wp.label }),
  {} as Record<WhatsappProvider, string>,
);

// Every canonical STT provider id declared in the backend allowlist
// (`PROVIDER_ALLOWLISTS["stt"]` in `src/services/api_credentials.py`) must be
// represented here. Missing an id would break credential save/load parity.
export const STT_PROVIDERS: { id: SttProvider; label: string; sub: string; activeCls: string }[] = [
  { id: "openai_whisper",    label: "Whisper",    sub: "by OpenAI",         activeCls: "border-amber-500/50 text-amber-400 bg-amber-500/10"    },
  { id: "elevenlabs_scribe", label: "Scribe",     sub: "by ElevenLabs",     activeCls: "border-orange-500/50 text-orange-400 bg-orange-500/10" },
  { id: "google_stt",        label: "Cloud STT",  sub: "by Google",         activeCls: "border-sky-500/50 text-sky-400 bg-sky-500/10"          },
  { id: "deepgram_stt",      label: "Deepgram",   sub: "by Deepgram",       activeCls: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10" },
  { id: "microsoft_stt",     label: "Azure STT",  sub: "by Microsoft",      activeCls: "border-blue-500/50 text-blue-400 bg-blue-500/10"       },
  { id: "outros",            label: "Outros",     sub: "Groq / OpenRouter", activeCls: "border-slate-500/50 text-slate-300 bg-slate-500/10"    },
];

export const TTS_PROVIDERS: { id: TtsProvider; label: string; sub: string; activeCls: string }[] = [
  { id: "elevenlabs", label: "ElevenLabs",      sub: "Voz natural e humana", activeCls: "border-violet-500/50 text-violet-400 bg-violet-500/10" },
  { id: "google",     label: "Google Cloud TTS", sub: "Escala e bom custo",   activeCls: "border-sky-500/50 text-sky-400 bg-sky-500/10"       },
];

const TTS_PROVIDER_DEFAULTS: Record<TtsProvider, { apiUrl: string; modelName: string; voiceId: string }> = {
  elevenlabs: {
    apiUrl: "https://api.elevenlabs.io/v1",
    modelName: "eleven_multilingual_v2",
    voiceId: "JBFqnCBsd6RMkjVDRZzb",
  },
  google: {
    apiUrl: "https://texttospeech.googleapis.com/v1",
    modelName: "pt-BR-Neural2-A",
    voiceId: "",
  },
};

const PROVIDER_DEFAULT_STT_URL: Partial<Record<SttProvider, string>> = {
  openai_whisper:    "https://api.openai.com/v1",
  elevenlabs_scribe: "https://api.elevenlabs.io/v1",
  // google_stt: incompatible API format — no default URL
  // outros: user-provided — no default
};

// `comingSoon` providers are display-only: they render a "Em breve" badge,
// cannot be selected and are never included in a PATCH body. The backend
// rejects `slm_cortex` writes today (see `_llm_operational` in
// `src/services/api_credentials.py`), so the UI must not send it either.
export const PROVIDERS: {
  id: LlmProvider;
  label: string;
  sub: string;
  activeCls: string;
  comingSoon?: boolean;
}[] = [
  { id: "slm_cortex", label: "SLM Cortex", sub: "by NeuroStack", activeCls: "border-violet-500/50 text-violet-400 bg-violet-500/10",  comingSoon: true },
  { id: "dropout",    label: "Dropout",    sub: "by Neuralake",  activeCls: "border-blue-500/50 text-blue-400 bg-blue-500/10"         },
  { id: "openai",     label: "GPT",        sub: "by OpenAI",     activeCls: "border-emerald-500/50 text-emerald-400 bg-emerald-500/10"},
  { id: "outros",     label: "Outros",     sub: "Custom / OSS",  activeCls: "border-slate-500/50 text-slate-300 bg-slate-500/10"     },
];

// Set of LLM provider ids that can be selected AND sent in a PATCH body.
// Anything outside this set (currently `slm_cortex`) is treated as unset and
// silently dropped from the PATCH payload.
export const SELECTABLE_LLM_PROVIDERS: ReadonlySet<LlmProvider> = new Set(
  PROVIDERS.filter(p => !p.comingSoon).map(p => p.id),
);

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

function ProviderCard({
  wp, active, disabled, onClick,
}: {
  wp: { id: WhatsappProvider; label: string; sub: string; activeCls: string };
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={`whatsapp-card-${wp.id}`}
      data-provider-id={wp.id}
      disabled={disabled}
      onClick={onClick}
      className={`flex flex-col items-start rounded-lg border px-3 py-2.5 text-left transition-all disabled:opacity-60 disabled:cursor-not-allowed ${
        active
          ? wp.activeCls + " border-2"
          : "border-border text-muted-foreground hover:border-muted-foreground/50"
      }`}
    >
      <span className="text-xs font-bold">{wp.label}</span>
      <span className="text-[10px] opacity-70 mt-0.5">{wp.sub}</span>
    </button>
  );
}

function UnimplementedProviderNotice({ provider }: { provider: WhatsappProvider }) {
  const label = WA_PROVIDER_LABEL[provider] ?? provider;
  return (
    <div className="rounded-md border border-dashed border-amber-500/40 bg-amber-500/5 px-3 py-3 space-y-1.5">
      <div className="flex items-center gap-2 text-amber-500">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        <p className="text-[11px] font-semibold uppercase tracking-wider">
          {label} — provedor ainda não implementado
        </p>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Este provedor aparece aqui como opção, mas o backend ainda não implementa a integração.
        Tentar salvar retornará um erro claro. Use <span className="font-semibold">Meta Cloud API</span>,
        <span className="font-semibold"> Z-API</span> ou <span className="font-semibold">Evolution API</span>
        {" "}enquanto isso.
      </p>
    </div>
  );
}

/**
 * Generate a URL-safe base64 token and its SHA-256 hex digest using the Web
 * Crypto API. The raw token is intended to be displayed to the operator
 * exactly once — the digest is what we PATCH to the backend so the plaintext
 * value never leaves the browser.
 */
async function generateZapiCapabilityToken(): Promise<{ rawToken: string; digest: string }> {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  // Convert to a URL-safe base64 string without padding.
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const rawToken = btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=/g, "");
  const hashBuf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(rawToken));
  const digest = Array.from(new Uint8Array(hashBuf))
    .map(b => b.toString(16).padStart(2, "0"))
    .join("");
  return { rawToken, digest };
}

export function ApiCredentialsForm({ clinicId, isReadOnly, isSuperAdmin }: {
  clinicId: string; isReadOnly: boolean; isSuperAdmin: boolean;
}) {
  const { toast } = useToast();
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  const [clinics, setClinics]               = useState<ClinicSlim[]>([]);
  const [selectedClinicId, setSelectedClinicId] = useState<string>(clinicId);
  const [status, setStatus]                 = useState<CredentialStatus | null>(null);
  const [loading, setLoading]               = useState(false);
  const [saving, setSaving]                 = useState(false);
  const [error, setError]                   = useState<string | null>(null);
  const [copiedWebhook, setCopiedWebhook]   = useState(false);
  const [copiedVerifyToken, setCopiedVerifyToken] = useState(false);

  // WhatsApp provider selector
  const [waProvider, setWaProvider]         = useState<WhatsappProvider>("zapi");

  // Z-API fields
  const [zapiInstanceId, setZapiInstanceId] = useState("");
  const [zapiToken, setZapiToken]           = useState("");
  const [zapiClientToken, setZapiClientToken] = useState("");
  // One-time capability URL for inbound Z-API webhooks. The raw token is
  // generated in the browser, hashed to SHA-256, and only the hex digest is
  // sent to the backend. The raw value is displayed exactly once and then
  // cleared from memory. We deliberately do NOT persist it to
  // localStorage/sessionStorage/URL/history so the token cannot be recovered.
  const [zapiInboundRawToken, setZapiInboundRawToken] = useState<string>("");

  // Evolution API fields
  const [evolApiUrl, setEvolApiUrl]           = useState("");
  const [evolInstanceName, setEvolInstanceName] = useState("");
  const [evolApiKey, setEvolApiKey]           = useState("");

  // Meta Cloud API fields
  const [metaPhoneNumberId, setMetaPhoneNumberId] = useState("");
  const [metaWabaId, setMetaWabaId]               = useState("");
  const [metaPermanentToken, setMetaPermanentToken] = useState("");
  const [metaAppSecret, setMetaAppSecret]         = useState("");

  // Uazapi fields
  const [uazapiApiUrl, setUazapiApiUrl]               = useState("");
  const [uazapiToken, setUazapiToken]                 = useState("");
  const [uazapiInstanceName, setUazapiInstanceName]   = useState("");

  // LLM
  const [llmProvider, setLlmProvider]       = useState<LlmProvider>("openai");
  const [llmApiKey, setLlmApiKey]           = useState("");
  const [llmApiUrl, setLlmApiUrl]           = useState("");
  const [llmModelName, setLlmModelName]     = useState("");
  const [llmFallback, setLlmFallback]       = useState<"openai" | "dropout">("dropout");

  // STT (audio transcription)
  const [sttProvider, setSttProvider]       = useState<SttProvider>("outros");
  const [sttApiKey, setSttApiKey]           = useState("");
  const [sttApiUrl, setSttApiUrl]           = useState("");
  const [sttModelName, setSttModelName]     = useState("");
  const [audioMaxSeconds, setAudioMaxSeconds] = useState(60);

  // Multimodal transcription. The API URL is derived from the provider and
  // never edited, so there is no `multimodalApiUrl` state to keep in sync.
  const [multimodalProvider, setMultimodalProvider] = useState<MultimodalProvider>("openrouter");
  const [multimodalApiKey, setMultimodalApiKey]     = useState("");
  const [multimodalModelName, setMultimodalModelName] = useState("");
  const [multimodalDirty, setMultimodalDirty]      = useState(false);
  // True once the user switched providers on an already-saved bundle: the
  // backend then demands a fresh key (`fresh_api_key_required`), so the UI has
  // to say so before the save is attempted.
  const [multimodalNeedsFreshKey, setMultimodalNeedsFreshKey] = useState(false);

  // TTS for agent replies. Disabled by default so saving credentials alone
  // never changes the patient experience.
  const [ttsProvider, setTtsProvider]         = useState<TtsProvider>("elevenlabs");
  const [ttsApiKey, setTtsApiKey]             = useState("");
  const [ttsApiUrl, setTtsApiUrl]             = useState("");
  const [ttsModelName, setTtsModelName]       = useState("");
  const [ttsVoiceId, setTtsVoiceId]           = useState("");
  const [ttsEnabled, setTtsEnabled]           = useState(false);
  const [ttsDirty, setTtsDirty]               = useState(false);

  useEffect(() => {
    if (!isSuperAdmin) return;
    void fetch(`/v1/admin/clinics`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then(r => r.json()).then((d: ClinicSlim[]) => setClinics(d)).catch(() => {});
  }, [isSuperAdmin]);

  const targetId   = isSuperAdmin ? selectedClinicId : clinicId;
  const webhookSegment = WEBHOOK_SEGMENT[waProvider];
  const webhookUrl = webhookSegment ? `${window.location.origin}/v1/webhook/${webhookSegment}` : "";
  const providerImplemented = IMPLEMENTED_WA_PROVIDERS.has(waProvider);

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
      // Meta Cloud API — non-secret identifiers pre-fill; secret fields stay
      // empty so the "manter atual" placeholder shows when already saved.
      setMetaPhoneNumberId(data.meta_phone_number_id ?? "");
      setMetaWabaId(data.meta_waba_id ?? "");
      setMetaPermanentToken("");
      setMetaAppSecret("");
      // Uazapi — non-secret identifiers pre-fill; token stays empty so
      // "manter atual" placeholder shows when already saved.
      setUazapiApiUrl(data.uazapi_api_url ?? "");
      setUazapiInstanceName(data.uazapi_instance_name ?? "");
      setUazapiToken("");
      // LLM — coerce any non-selectable provider (e.g. slm_cortex, coming
      // soon) to the safe default so the UI never enters an unreachable state
      // and the PATCH body never carries a rejected provider id.
      const inferredProvider = inferProvider(data.llm_base_url);
      const effectiveLlmProvider: LlmProvider = SELECTABLE_LLM_PROVIDERS.has(inferredProvider)
        ? inferredProvider
        : "openai";
      setLlmModelName(effectiveLlmProvider === "dropout" ? "text" : (data.llm_model_name ?? ""));
      setLlmProvider(effectiveLlmProvider);
      setLlmApiUrl(data.llm_base_url ?? PROVIDER_DEFAULT_URL[effectiveLlmProvider]);
      // WhatsApp secret fields: left empty so the placeholder "••••••••• (manter atual)"
      // appears when a token is already set. On save, empty = not sent → COALESCE
      // in the UPDATE preserves the existing encrypted value in the DB.
      setZapiToken("");
      setEvolApiKey("");
      // LLM API key: send partial mask (sk-pro****9xyz) so the user can visually
      // confirm which key is configured. Backend detects "****" and skips update.
      setLlmApiKey(data.llm_api_key_masked ?? "");
      // STT (audio transcription)
      const savedSttProvider = (data.stt_provider as SttProvider) ?? "outros";
      setSttProvider(savedSttProvider);
      setSttApiKey(data.whisper_api_key_masked ?? "");
      setSttApiUrl(data.stt_api_url || PROVIDER_DEFAULT_STT_URL[savedSttProvider] || "");
      setSttModelName(data.whisper_model ?? "");
      setAudioMaxSeconds(Number(data.audio_max_seconds) || 60);
      // Multimodal — an unknown/absent provider falls back to the recommended
      // one without implying a saved configuration (see `multimodalDirty`).
      // The masked key is echoed back so the operator can see a key is stored;
      // the mask is stripped from the PATCH body on save.
      const hasSavedMultimodalProvider = MULTIMODAL_PROVIDERS.some(p => p.id === data.multimodal_provider);
      setMultimodalProvider(hasSavedMultimodalProvider
        ? data.multimodal_provider as MultimodalProvider
        : "openrouter");
      setMultimodalApiKey(data.multimodal_api_key_masked ?? "");
      setMultimodalModelName(hasSavedMultimodalProvider ? (data.multimodal_model_name ?? "") : "");
      setMultimodalDirty(false);
      setMultimodalNeedsFreshKey(false);
      // TTS secrets never pre-fill. Unknown/retired provider ids fall back to
      // ElevenLabs without silently submitting a new TTS configuration.
      const hasSavedTtsProvider = TTS_PROVIDERS.some(p => p.id === data.tts_provider);
      const savedTtsProvider = hasSavedTtsProvider
        ? data.tts_provider as TtsProvider
        : "elevenlabs";
      setTtsProvider(savedTtsProvider);
      setTtsApiKey("");
      setTtsApiUrl(hasSavedTtsProvider ? (data.tts_api_url ?? TTS_PROVIDER_DEFAULTS[savedTtsProvider].apiUrl) : "");
      setTtsModelName(hasSavedTtsProvider ? (data.tts_model_name ?? "") : "");
      setTtsVoiceId(data.tts_voice_id ?? TTS_PROVIDER_DEFAULTS[savedTtsProvider].voiceId);
      setTtsEnabled(data.tts_enabled === true);
      setTtsDirty(false);
    } catch { setError("Erro de rede."); }
    finally { setLoading(false); }
  }, [targetId]);

  useEffect(() => { void fetchStatus(); }, [fetchStatus]);

  const handleSave = async () => {
    setSaving(true); setError(null);
    // Pre-generated Z-API capability token (raw + digest). Kept in the local
    // scope of this save call so the raw token is only ever surfaced through
    // the "one-time URL" panel below on success and never leaks otherwise.
    let generatedZapiCapability: { rawToken: string; digest: string } | null = null;
    try {
      // Clinic scope belongs exclusively to the query string. Sending
      // clinic_id in this strict credential payload is rejected as an extra
      // field by the backend.
      // `null` is a meaningful value here, not an absence: `exclude_unset` on
      // the backend keeps an explicit null in the patch, so it clears a stored
      // field, whereas an omitted key preserves it.
      const body: Record<string, string | number | boolean | null> = {};
      // WhatsApp provider selection — always send to persist the chosen provider
      body["whatsapp_provider"] = waProvider;
      if (waProvider === "zapi") {
        if (zapiInstanceId)  body["zapi_instance_id"]  = zapiInstanceId;
        if (zapiToken)       body["zapi_token"]        = zapiToken;
        if (zapiClientToken) body["zapi_client_token"] = zapiClientToken;
        // Rotate the inbound capability token whenever the user saves a new
        // Z-API instance token. The raw value is only kept in the closure so
        // the "one-time URL" panel can render it on success — nothing is ever
        // written to localStorage/sessionStorage/URL/history.
        if (zapiToken) {
          generatedZapiCapability = await generateZapiCapabilityToken();
          body["zapi_inbound_token_digest"] = generatedZapiCapability.digest;
        }
      } else if (waProvider === "evolution") {
        if (evolApiUrl)      body["evolution_api_url"]      = evolApiUrl;
        if (evolInstanceName) body["evolution_instance_name"] = evolInstanceName;
        if (evolApiKey)      body["evolution_api_key"]      = evolApiKey;
      } else if (waProvider === "meta_cloud") {
        if (metaPhoneNumberId)  body["meta_phone_number_id"]  = metaPhoneNumberId;
        if (metaWabaId)         body["meta_waba_id"]          = metaWabaId;
        if (metaPermanentToken) body["meta_permanent_token"]  = metaPermanentToken;
        if (metaAppSecret)      body["meta_app_secret"]       = metaAppSecret;
      } else if (waProvider === "uazapi") {
        if (uazapiApiUrl)       body["uazapi_api_url"]        = uazapiApiUrl;
        if (uazapiInstanceName) body["uazapi_instance_name"]  = uazapiInstanceName;
        // Skip masked tokens (e.g. "abcd****wxyz") — send only real values
        // typed by the user so the backend can preserve the existing secret
        // via COALESCE when the field is omitted.
        if (uazapiToken && !uazapiToken.includes("*") && !uazapiToken.includes("•"))
          body["uazapi_token"] = uazapiToken;
      }
      // dialog360 / twilio: no per-provider fields yet; the backend
      // will reject the save with a "provider_not_implemented" error handled
      // in the error branch below (never a silent success).
      // Only send LLM api key if the user actually typed a new one (non-empty)
      if (llmApiKey)      body["llm_api_key"]       = llmApiKey;
      // Never send non-selectable providers (slm_cortex is display-only and
      // the backend rejects the write). Silently skip the LLM block entirely
      // when the current selection is not operational.
      if (SELECTABLE_LLM_PROVIDERS.has(llmProvider)) {
        body["llm_api_url"]  = llmApiUrl || PROVIDER_DEFAULT_URL[llmProvider];
        body["llm_provider"] = llmProvider;
        if (llmProvider === "dropout") body["llm_model_name"] = "text";
        else if (llmModelName && (llmProvider === "openai" || llmProvider === "outros")) body["llm_model_name"] = llmModelName;
      }
      // STT (audio transcription)
      body["stt_provider"]     = sttProvider;
      // Canonical field, never the legacy `whisper_model` alias: the backend
      // only clears the stale alias when the patch carries `stt_model_name`
      // (`api_credentials.py:277-278`), so sending the alias here would let an
      // old canonical value silently keep winning. `null`, not "", because the
      // field is `Identifier` with min_length=1 and a brand-new clinic that
      // never touched STT has this state empty — that empty string 422s on
      // every save, including saves unrelated to STT.
      body["stt_model_name"]   = sttModelName || null;
      body["audio_max_seconds"] = audioMaxSeconds;
      if (sttApiKey && !sttApiKey.includes("•") && !sttApiKey.includes("*"))
        body["whisper_api_key"] = sttApiKey;
      // Always send stt_api_url so the backend can clear a stale value.
      // `null`, not "", for the same min_length=1 reason as stt_model_name
      // above — both the empty and the google_stt (no URL) case need `null`.
      body["stt_api_url"] = sttProvider !== "google_stt" ? (sttApiUrl || null) : null;
      // Multimodal transcription. Submitted only after the user edits this
      // capability so an unrelated API save never creates an implicit provider
      // selection. The URL is derived from the provider, never typed.
      if (multimodalDirty) {
        const freshMultimodalKey = multimodalApiKey
          && !multimodalApiKey.includes("•")
          && !multimodalApiKey.includes("*");
        // Changing the provider changes the derived URL too, so the backend
        // requires fresh key material (`fresh_api_key_required`). Say it here
        // in plain language instead of letting the operator hit a 422.
        if (multimodalNeedsFreshKey && !freshMultimodalKey) {
          const msg = txt(
            "Informe a API Key do novo provedor multimodal — trocar de provedor exige uma chave nova.",
            "Enter the API key for the new multimodal provider — switching providers requires a new key.",
          );
          setError(msg);
          toast({ title: txt("Erro ao salvar", "Could not save"), description: msg, variant: "destructive" });
          return;
        }
        body["multimodal_provider"] = multimodalProvider;
        body["multimodal_api_url"] = MULTIMODAL_PROVIDER_API_URL[multimodalProvider];
        if (freshMultimodalKey) body["multimodal_api_key"] = multimodalApiKey;
        // Always send the model name — like `stt_api_url` above — so the
        // backend can clear a stale value. Switching provider resets this
        // field to "", and omitting it would let the previous provider's model
        // survive the merge, producing a bundle that looks `active` but fails
        // on the first real voice note. `null` (never "") is what clears it:
        // the field is `Identifier` with min_length=1, so "" would 422 while
        // None merges through and drops readiness to an honest `incomplete`.
        body["multimodal_model_name"] = multimodalModelName || null;
      }
      // Submit TTS only after an explicit edit so unrelated saves preserve the
      // existing bundle. `tts_enabled` makes voice replies opt-in.
      if (ttsDirty) {
        const hasTtsKey = Boolean(ttsApiKey) || Boolean(status?.tts_api_key_set);
        const effectiveModel = ttsModelName || TTS_PROVIDER_DEFAULTS[ttsProvider].modelName;
        const effectiveVoiceId = ttsVoiceId || TTS_PROVIDER_DEFAULTS[ttsProvider].voiceId;
        if (ttsEnabled && (!hasTtsKey || !effectiveModel || (ttsProvider === "elevenlabs" && !effectiveVoiceId))) {
          const msg = txt(
            "Complete a API Key, o modelo e a voz antes de ativar respostas em áudio.",
            "Complete the API key, model and voice before enabling audio replies.",
          );
          setError(msg);
          toast({ title: txt("Erro ao salvar", "Could not save"), description: msg, variant: "destructive" });
          return;
        }
        body["tts_provider"] = ttsProvider;
        body["tts_api_url"] = ttsApiUrl || TTS_PROVIDER_DEFAULTS[ttsProvider].apiUrl;
        if (ttsApiKey && !ttsApiKey.includes("â€¢") && !ttsApiKey.includes("*"))
          body["tts_api_key"] = ttsApiKey;
        body["tts_model_name"] = effectiveModel;
        body["tts_voice_id"] = ttsProvider === "elevenlabs" ? effectiveVoiceId : null;
        body["tts_enabled"] = ttsEnabled;
      }
      const saveUrl = targetId
        ? `/v1/config/api-credentials?clinic_id=${targetId}`
        : `/v1/config/api-credentials`;
      const res = await fetch(saveUrl, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
        body: JSON.stringify(body),
      });
      const data = await res.json() as {
        ok?: boolean;
        apis_configured?: boolean;
        error?: string;
        // FastAPI wraps HTTPException payloads under `detail` — this covers the
        // structured errors returned by the backend (provider_not_implemented,
        // meta_phone_number_id_conflict, etc.).
        detail?: string | { error?: string; message?: string; provider?: string; capability?: string };
      };
      if (!res.ok) {
        let errMsg = data.error ?? "Erro ao salvar.";
        const detail = data.detail;
        if (typeof detail === "string") {
          errMsg = detail;
        } else if (detail && typeof detail === "object") {
          if (detail.error === "provider_not_implemented") {
            const provLabel = detail.provider
              ? (WA_PROVIDER_LABEL[detail.provider as WhatsappProvider] ?? detail.provider)
              : "esse provedor";
            errMsg = `${provLabel}: provedor ainda não implementado. Use Meta Cloud API, Z-API ou Evolution API.`;
          } else if (detail.error === "fresh_api_key_required") {
            // The backend refuses to carry a key across a provider/URL change.
            // Translate the stable code so the operator never sees it raw.
            const capability = CAPABILITY_LABELS[detail.capability ?? ""];
            const capabilityLabel = capability
              ? txt(capability.pt, capability.en)
              : txt("desta integração", "of this integration");
            errMsg = txt(
              `Informe uma nova API Key ${capabilityLabel}: trocar de provedor ou de endpoint exige uma chave nova.`,
              `Enter a new API key ${capabilityLabel}: switching provider or endpoint requires a new key.`,
            );
          } else {
            errMsg = detail.message ?? detail.error ?? errMsg;
          }
        }
        setError(errMsg);
        toast({ title: "Erro ao salvar", description: errMsg, variant: "destructive" });
        return;
      }
      toast({ title: "Configurações salvas!", description: data.apis_configured ? "APIs totalmente configuradas ✓" : "Parcialmente configurado." });
      // Surface the raw capability token ONCE, only after the PATCH succeeded.
      // The digest was already persisted server-side, so the raw value is
      // useless to anyone but the operator viewing this exact response.
      if (generatedZapiCapability) {
        setZapiInboundRawToken(generatedZapiCapability.rawToken);
      }
      await fetchStatus();
    } catch { setError("Erro de rede."); }
    finally { setSaving(false); }
  };

  /**
   * Explicit dismiss of the one-time Z-API capability URL panel. Clears the
   * raw token from React state so it cannot be re-rendered or read from a
   * DOM inspector after the panel unmounts.
   */
  const dismissZapiInboundRawToken = () => setZapiInboundRawToken("");

  function copyWebhook() {
    navigator.clipboard.writeText(webhookUrl).catch(() => {});
    setCopiedWebhook(true);
    setTimeout(() => setCopiedWebhook(false), 2000);
  }

  function copyVerifyToken() {
    const tok = status?.meta_verify_token;
    if (!tok) return;
    navigator.clipboard.writeText(tok).catch(() => {});
    setCopiedVerifyToken(true);
    setTimeout(() => setCopiedVerifyToken(false), 2000);
  }

  // `slm_cortex` is display-only and forcibly coerced to a real provider at
  // load-time; the two flags below remain as `false` sentinels because a
  // handful of downstream branches used to depend on them.
  const isCortex     = false;
  const isOutros     = llmProvider === "outros";
  const isComingSoon = false;

  return (
    <div className="space-y-5" data-testid="api-settings-section">

      {/* ── Super admin clinic picker ── */}
      {isSuperAdmin && clinics.length > 0 && (
        <div className="space-y-1.5">
          <label className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">Ver clínica</label>
          <select
            value={selectedClinicId}
            onChange={e => setSelectedClinicId(e.target.value)}
            className="w-full rounded-md border border-border bg-background text-sm px-3 py-2 focus:outline-none focus:ring-1 focus:ring-accent"
          >
            {clinics.map(c => (
              <option key={c.clinic_id} value={c.clinic_id}>{c.clinic_name} (ID {c.clinic_id})</option>
            ))}
          </select>
        </div>
      )}

      {/* ── Webhook URL (only when provider has a functional backend) ── */}
      {webhookUrl && (
        <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50/60 dark:bg-blue-950/30 p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Link2 className="h-4 w-4 text-blue-500 shrink-0" />
            <p className="text-[11px] font-semibold uppercase tracking-wider text-blue-700 dark:text-blue-400">
              Webhook URL - {WA_PROVIDER_LABEL[waProvider]}
            </p>
            {status && (
              <span className={`ml-auto text-[10px] font-bold px-2 py-0.5 rounded-full border ${status.apis_configured ? "bg-emerald-500/15 text-emerald-500 border-emerald-500/30" : "bg-amber-500/15 text-amber-500 border-amber-500/30"}`}>
                {status.apis_configured ? "Configurado" : "Pendente"}
              </span>
            )}
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">
              Cole na configuração de webhooks do {WA_PROVIDER_LABEL[waProvider]}
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
          {waProvider === "zapi" && (
            <>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.zapi_instance_id} /> Z-API Instance</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={status.zapi_token_set} /> Z-API Token</span>
            </>
          )}
          {waProvider === "evolution" && (
            <>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.evolution_api_url} /> Evol. API URL</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.evolution_instance_name} /> Instance Name</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={status.evolution_api_key_set} /> Evol. API Key</span>
            </>
          )}
          {waProvider === "meta_cloud" && (
            <>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.meta_phone_number_id} /> Phone Number ID</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.meta_waba_id} /> WABA ID</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.meta_permanent_token_set} /> Permanent Token</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.meta_verify_token} /> Verify Token</span>
            </>
          )}
          {waProvider === "uazapi" && (
            <>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.uazapi_api_url} /> Uazapi API URL</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.uazapi_instance_name} /> Instance Name</span>
              <span className="flex items-center gap-1.5"><StatusDot configured={!!status.uazapi_token_set} /> Uazapi Token</span>
            </>
          )}
          {!providerImplemented && (
            <span className="flex items-center gap-1.5 text-amber-500">
              <AlertTriangle className="h-3.5 w-3.5" /> Provedor ainda não implementado
            </span>
          )}
          <span className="flex items-center gap-1.5"><StatusDot configured={status.llm_api_key_set} /> LLM API Key</span>
          <span className="flex items-center gap-1.5"><StatusDot configured={!!status.llm_base_url} /> LLM URL</span>
          {llmProvider === "openai" && (
            <span className="flex items-center gap-1.5"><StatusDot configured={!!status.llm_model_name} /> Model Name</span>
          )}
        </div>
      )}

      {/* ── WhatsApp Providers (6 canais agrupados) ── */}
      <div className="space-y-5 rounded-lg border border-slate-700/50 bg-muted/20 p-4">

        {/* Section header */}
        <div className="flex items-center gap-2">
          <MessageSquare className="h-3.5 w-3.5 text-[#25D366]" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">WhatsApp - Provedor Ativo</p>
          <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded border bg-[#25D366]/10 text-[#25D366] border-[#25D366]/30">Ativo</span>
        </div>

        {/* Group: Official WhatsApp Business API */}
        <div className="space-y-2">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground/80">
            WhatsApp Business API (Oficial)
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {WA_PROVIDERS.filter(wp => wp.group === "oficial").map(wp => (
              <ProviderCard
                key={wp.id}
                wp={wp}
                active={waProvider === wp.id}
                disabled={isReadOnly}
                onClick={() => {
                  if (waProvider === wp.id) return;
                  setWaProvider(wp.id);
                  // Clear per-provider inputs so we don't accidentally send
                  // stale fields when switching providers.
                  setZapiInstanceId(""); setZapiToken(""); setZapiClientToken("");
                  setEvolApiUrl(""); setEvolInstanceName(""); setEvolApiKey("");
                  setMetaPermanentToken(""); setMetaAppSecret("");
                  setUazapiApiUrl(""); setUazapiInstanceName(""); setUazapiToken("");
                }}
              />
            ))}
          </div>
        </div>

        {/* Group: Unofficial / WhatsApp Web providers */}
        <div className="space-y-2">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground/80">
            Outros Provedores WhatsApp Web
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {WA_PROVIDERS.filter(wp => wp.group === "whatsapp_web").map(wp => (
              <ProviderCard
                key={wp.id}
                wp={wp}
                active={waProvider === wp.id}
                disabled={isReadOnly}
                onClick={() => {
                  if (waProvider === wp.id) return;
                  setWaProvider(wp.id);
                  setZapiInstanceId(""); setZapiToken(""); setZapiClientToken("");
                  setEvolApiUrl(""); setEvolInstanceName(""); setEvolApiKey("");
                  setMetaPermanentToken(""); setMetaAppSecret("");
                  setUazapiApiUrl(""); setUazapiInstanceName(""); setUazapiToken("");
                }}
              />
            ))}
          </div>
        </div>

        {/* Meta Cloud API fields */}
        {waProvider === "meta_cloud" && (
          <div className="grid gap-3 sm:grid-cols-2">
            {/* signature_pending banner — identity is configured but the App Secret
                is missing. Without HMAC we cannot authenticate inbound webhooks. */}
            {(() => {
              const identityReady =
                !!status?.meta_phone_number_id && !!status?.meta_permanent_token_set;
              const signaturePending = identityReady && status?.meta_app_secret_set === false;
              if (!signaturePending) return null;
              return (
                <div
                  data-testid="meta-signature-pending-banner"
                  className="sm:col-span-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2.5 flex items-start gap-2"
                >
                  <AlertTriangle className="h-4 w-4 shrink-0 text-amber-500 mt-0.5" />
                  <div className="space-y-1">
                    <div className="flex items-center gap-2">
                      <p className="text-[11px] font-semibold uppercase tracking-wider text-amber-500">
                        {txt(
                          "HMAC pendente — mensagens não podem ser autenticadas",
                          "HMAC pending — messages cannot be authenticated",
                        )}
                      </p>
                      <span
                        data-testid="meta-signature-pending-badge"
                        className="text-[9px] font-bold px-1.5 py-0.5 rounded border bg-amber-500/15 text-amber-500 border-amber-500/40"
                      >
                        {txt("Assinatura pendente", "Signature pending")}
                      </span>
                    </div>
                    <p className="text-[11px] text-muted-foreground">
                      {txt(
                        "Informe o App Secret abaixo e salve para começar a verificar as assinaturas X-Hub-Signature-256 do Meta.",
                        "Enter the App Secret below and save to start verifying Meta's X-Hub-Signature-256 signatures.",
                      )}
                    </p>
                  </div>
                </div>
              );
            })()}
            <ApiInput label="Phone Number ID" value={metaPhoneNumberId} onChange={setMetaPhoneNumberId}
              placeholder="123456789012345" disabled={isReadOnly} mono
              configured={!!status?.meta_phone_number_id} />
            <ApiInput label="WABA ID" value={metaWabaId} onChange={setMetaWabaId}
              placeholder="987654321098765" disabled={isReadOnly} mono
              configured={!!status?.meta_waba_id} />
            <div className="sm:col-span-2">
              <ApiInput label="Permanent Token" value={metaPermanentToken} onChange={setMetaPermanentToken} type="password"
                placeholder={status?.meta_permanent_token_set ? "••••••••• (manter atual)" : "EAAG..."}
                disabled={isReadOnly} configured={status?.meta_permanent_token_set} />
            </div>
            <div className="sm:col-span-2" data-testid="meta-app-secret-field">
              <ApiInput
                label={
                  status?.meta_phone_number_id && status?.meta_permanent_token_set && !status?.meta_app_secret_set
                    ? txt("App Secret *", "App Secret *")
                    : "App Secret"
                }
                value={metaAppSecret}
                onChange={setMetaAppSecret}
                type="password"
                placeholder={
                  status?.meta_app_secret_set
                    ? "••••••••• (manter atual)"
                    : txt(
                        "Obrigatório — assina webhooks (X-Hub-Signature-256)",
                        "Required — signs webhooks (X-Hub-Signature-256)",
                      )
                }
                disabled={isReadOnly}
                configured={status?.meta_app_secret_set}
              />
            </div>

            {/* Verify Token — only shown after Phone Number ID + Permanent Token
                have been saved at least once (backend auto-generates on first save). */}
            {status?.meta_verify_token ? (
              <div className="sm:col-span-2 space-y-1">
                <label className="text-xs font-medium text-muted-foreground">
                  Verify Token <span className="text-emerald-400 ml-1.5">● gerado automaticamente</span>
                </label>
                <div className="flex gap-2">
                  <input
                    readOnly value={status.meta_verify_token}
                    onClick={e => (e.target as HTMLInputElement).select()}
                    className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-mono text-foreground cursor-text focus:outline-none focus:ring-1 focus:ring-accent"
                  />
                  <button type="button" onClick={copyVerifyToken}
                    className="inline-flex items-center gap-1.5 rounded-md border border-blue-300 dark:border-blue-700 bg-white dark:bg-blue-900/20 px-3 py-1.5 text-xs font-medium text-blue-700 dark:text-blue-300 hover:bg-blue-50 dark:hover:bg-blue-900/40 transition-colors shrink-0"
                  >
                    {copiedVerifyToken
                      ? <><Check className="h-3.5 w-3.5 text-emerald-500" /> Copiado!</>
                      : <><Copy className="h-3.5 w-3.5" /> Copiar</>}
                  </button>
                </div>
                <p className="text-[10px] text-muted-foreground">
                  Cole este valor em "Verify Token" ao configurar o webhook no Meta App Dashboard.
                </p>
              </div>
            ) : (
              <div className="sm:col-span-2 rounded-md border border-dashed border-slate-600/60 bg-muted/20 px-3 py-2.5 text-[11px] text-muted-foreground">
                O <span className="font-mono">Verify Token</span> será gerado automaticamente após salvar
                <span className="font-semibold"> Phone Number ID</span> e
                <span className="font-semibold"> Permanent Token</span> pela primeira vez.
              </div>
            )}
          </div>
        )}

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

            {/* Inbound capability URL — one-time-only display.
                A new URL is generated locally each time the user saves the
                Z-API token. Once dismissed, the raw value is cleared from
                React state and can never be recovered. */}
            {zapiInboundRawToken ? (
              <div
                data-testid="zapi-inbound-token-panel"
                className="sm:col-span-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-3 space-y-2"
              >
                <div className="flex items-center gap-2">
                  <Lock className="h-4 w-4 shrink-0 text-amber-500" />
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-amber-500">
                    {txt("Webhook URL (exibida uma única vez)", "Webhook URL (shown only once)")}
                  </p>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  {txt(
                    "Copie esta URL agora. Ela não será exibida novamente.",
                    "Copy this URL now. It will not be shown again.",
                  )}
                </p>
                <pre
                  data-testid="zapi-inbound-token-url"
                  className="text-[11px] font-mono text-foreground bg-background border border-border rounded px-3 py-2 whitespace-pre-wrap break-all select-all"
                >{`${window.location.origin}/v1/webhook/zapi/${zapiInboundRawToken}`}</pre>
                <div className="flex justify-end">
                  <button
                    type="button"
                    data-testid="zapi-inbound-token-dismiss"
                    onClick={dismissZapiInboundRawToken}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground hover:bg-muted/40 transition-colors"
                  >
                    {txt("Já copiei — descartar", "I copied it — dismiss")}
                  </button>
                </div>
              </div>
            ) : status?.zapi_inbound_token_digest_set ? (
              <div
                data-testid="zapi-inbound-token-configured"
                className="sm:col-span-2 rounded-md border border-dashed border-emerald-500/40 bg-emerald-500/5 px-3 py-2.5 text-[11px] text-muted-foreground flex items-center gap-2"
              >
                <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
                <span>
                  {txt(
                    "Token configurado. URL exibida apenas uma vez.",
                    "Token configured. URL was shown only once.",
                  )}
                </span>
              </div>
            ) : null}
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

        {/* Uazapi fields (wuzapi-based, WhatsApp Web multi-device, self-hosted) */}
        {waProvider === "uazapi" && (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <ApiInput label="API URL" value={uazapiApiUrl} onChange={setUazapiApiUrl}
                placeholder="https://uazapi.seudominio.com" disabled={isReadOnly} mono
                configured={!!status?.uazapi_api_url} />
            </div>
            <ApiInput label="Instance Name" value={uazapiInstanceName} onChange={setUazapiInstanceName}
              placeholder="minha-clinica-prod" disabled={isReadOnly}
              configured={!!status?.uazapi_instance_name} />
            <ApiInput label="Token" value={uazapiToken} onChange={setUazapiToken} type="password"
              placeholder={status?.uazapi_token_set ? "••••••••• (manter atual)" : "Cole o Token da instância"}
              disabled={isReadOnly} configured={status?.uazapi_token_set} />
          </div>
        )}

        {/* dialog360 / twilio — visible form + backend rejects on save.
            No functional fields yet; each provider will get its own wish with
            the real credential schema. Presenting an empty state keeps the UI
            visually consistent with the implemented providers. */}
        {!providerImplemented && (
          <UnimplementedProviderNotice provider={waProvider} />
        )}
      </div>

      {/* ── LLM Provider selector ── */}
      <div className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4">
        <div className="flex items-center gap-2">
          <Cpu className="h-3.5 w-3.5 text-accent" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">AI Provider - LLM</p>
          {isComingSoon && (
            <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/30">Em breve</span>
          )}
        </div>

        {/* Provider toggle group */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2" data-testid="llm-provider-list">
          {PROVIDERS.map(p => {
            // `comingSoon` providers can never be the active selection —
            // treat any stored value that matches as unset so the card is
            // rendered as disabled instead of "active".
            const active     = llmProvider === p.id && !p.comingSoon;
            const comingSoon = !!p.comingSoon;
            return (
              <button
                key={p.id}
                type="button"
                data-testid={`llm-provider-${p.id}`}
                data-provider-id={p.id}
                data-coming-soon={comingSoon ? "true" : undefined}
                disabled={isReadOnly || comingSoon}
                aria-disabled={comingSoon || undefined}
                onClick={() => {
                  if (comingSoon) return; // never let the user select coming-soon providers
                  if (llmProvider === p.id) return; // no-op if same tab
                  setLlmProvider(p.id);
                  setLlmApiUrl(PROVIDER_DEFAULT_URL[p.id]);
                  setLlmApiKey(""); // clear key — different providers use different keys
                  setLlmModelName(p.id === "dropout" ? "text" : "");
                }}
                className={`relative flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-all disabled:cursor-not-allowed ${
                  comingSoon ? "pointer-events-none opacity-50" : ""
                } ${
                  active
                    ? p.activeCls
                    : "border-border bg-background text-muted-foreground hover:border-slate-500"
                } disabled:opacity-50`}
              >
                <div className="flex w-full items-center justify-between gap-1">
                  <span className="text-xs font-bold">{p.label}</span>
                  {comingSoon && (
                    <span
                      data-testid={`llm-coming-soon-${p.id}`}
                      className="text-[9px] font-bold px-1 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/30"
                    >
                      {txt("Em breve", "Coming soon")}
                    </span>
                  )}
                </div>
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
              <p>O SLM Cortex é o modelo sovereign da NeuroStack, treinado com dados clínicos anonimizados em ambiente isolado. A URL de inferência é gerenciada internamente.</p>
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

      {/* ── Provider Speech to Text ── */}
      <div className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4">
        <div className="flex items-center gap-2">
          <Mic2 className="h-3.5 w-3.5 text-accent" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Provider Speech to Text</p>
        </div>

        {/* Provider toggle group */}
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2" data-testid="stt-provider-list">
          {STT_PROVIDERS.map(p => {
            const active     = sttProvider === p.id;
            const configOnly = !OPERATIONAL_STT_PROVIDERS.has(p.id);
            return (
              <button
                key={p.id}
                type="button"
                data-testid={`stt-provider-${p.id}`}
                data-provider-id={p.id}
                disabled={isReadOnly}
                onClick={() => {
                  if (sttProvider === p.id) return;
                  setSttProvider(p.id);
                  setSttApiKey("");
                  setSttModelName("");
                  setSttApiUrl(PROVIDER_DEFAULT_STT_URL[p.id] ?? "");
                }}
                className={`relative flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
                  active
                    ? p.activeCls
                    : "border-border bg-background text-muted-foreground hover:border-slate-500"
                }`}
              >
                <div className="flex w-full items-center justify-between gap-1">
                  <span className="text-xs font-bold">{p.label}</span>
                  {configOnly && (
                    <span
                      data-testid={`stt-config-only-${p.id}`}
                      className="text-[9px] font-bold px-1 py-0.5 rounded border bg-slate-500/10 text-slate-400 border-slate-500/30"
                    >
                      {txt("Configuração apenas", "Config only")}
                    </span>
                  )}
                </div>
                <span className="text-[10px] opacity-70">{p.sub}</span>
                {active && <span className="absolute top-2 right-2 h-1.5 w-1.5 rounded-full bg-current opacity-80" />}
              </button>
            );
          })}
        </div>

        {/* Conditional fields based on sttProvider */}
        <div className="grid gap-3 sm:grid-cols-2">
          {sttProvider !== "google_stt" && (
            <div className="sm:col-span-2">
              <ApiInput label="API URL" value={sttApiUrl} onChange={setSttApiUrl}
                placeholder={
                  sttProvider === "openai_whisper"    ? "https://api.openai.com/v1" :
                  sttProvider === "elevenlabs_scribe" ? "https://api.elevenlabs.io/v1" :
                  sttProvider === "deepgram_stt"      ? "https://api.deepgram.com/v1" :
                  sttProvider === "microsoft_stt"     ? "https://<region>.stt.speech.microsoft.com" :
                  "https://api.groq.com/openai/v1"
                }
                disabled={isReadOnly} mono />
            </div>
          )}
          <ApiInput label="API Key" value={sttApiKey} onChange={setSttApiKey} type="password"
            placeholder={
              status?.whisper_api_key_set ? "••••••••• (manter atual)" :
              sttProvider === "openai_whisper" ? "sk-..." :
              sttProvider === "elevenlabs_scribe" ? "sk_..." :
              sttProvider === "google_stt" ? "AIza..." :
              sttProvider === "deepgram_stt" ? "Deepgram API key" :
              sttProvider === "microsoft_stt" ? "Azure Speech key" :
              "gsk_..."
            }
            disabled={isReadOnly} configured={status?.whisper_api_key_set} />
          <ApiInput label="Model Name" value={sttModelName} onChange={setSttModelName}
            placeholder={
              sttProvider === "openai_whisper" ? "whisper-1" :
              sttProvider === "elevenlabs_scribe" ? "scribe_v1" :
              sttProvider === "google_stt" ? "latest_long" :
              sttProvider === "deepgram_stt" ? "nova-2" :
              sttProvider === "microsoft_stt" ? "phi-4-multimodal" :
              "distil-whisper-large-v3-en"
            }
            disabled={isReadOnly} mono />
          <div className="space-y-1">
            <label className="text-xs font-medium text-muted-foreground">
              {txt("Duração Máxima (segundos)", "Max Duration (seconds)")}
            </label>
            <input
              type="number"
              min={10}
              max={300}
              value={audioMaxSeconds}
              onChange={(e) => setAudioMaxSeconds(Number(e.target.value))}
              disabled={isReadOnly}
              className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            />
          </div>
        </div>
      </div>

      {/* ── Multimodal transcription (chat model that accepts audio) ── */}
      <div
        className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4"
        data-testid="multimodal-settings-section"
      >
        <div className="flex flex-wrap items-center gap-2">
          <AudioLines className="h-3.5 w-3.5 text-accent" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            {txt("Transcrição Multimodal", "Multimodal Transcription")}
          </p>
          <span
            data-testid="multimodal-recommended-badge"
            className="ml-auto text-[9px] font-bold px-1.5 py-0.5 rounded border bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
          >
            {txt("RECOMENDÁVEL", "RECOMMENDED")}
          </span>
        </div>

        <p className="text-[11px] text-muted-foreground">
          {txt(
            "Modelo de chat que recebe o áudio direto do WhatsApp, sem transcrição separada. Usado como alternativa quando o STT falha.",
            "Chat model that takes WhatsApp audio directly, with no separate transcription step. Used as a fallback when STT fails.",
          )}
        </p>

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2" data-testid="multimodal-provider-list">
          {MULTIMODAL_PROVIDERS.map(p => {
            const active     = multimodalProvider === p.id;
            const configOnly = !OPERATIONAL_MULTIMODAL_PROVIDERS.has(p.id);
            return (
              <button
                key={p.id}
                type="button"
                data-testid={`multimodal-provider-${p.id}`}
                data-provider-id={p.id}
                disabled={isReadOnly}
                onClick={() => {
                  if (multimodalProvider === p.id) return;
                  setMultimodalProvider(p.id);
                  // Different provider ⇒ different derived URL ⇒ the stored key
                  // can no longer be reused. Clear it and demand a new one.
                  setMultimodalApiKey("");
                  setMultimodalModelName("");
                  setMultimodalDirty(true);
                  setMultimodalNeedsFreshKey(true);
                }}
                className={`relative flex flex-col items-start gap-0.5 rounded-lg border px-3 py-2.5 text-left transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
                  active
                    ? p.activeCls
                    : "border-border bg-background text-muted-foreground hover:border-slate-500"
                }`}
              >
                <div className="flex w-full items-center justify-between gap-1">
                  <span className="text-xs font-bold">{p.label}</span>
                  {configOnly && (
                    <span
                      data-testid={`multimodal-config-only-${p.id}`}
                      className="text-[9px] font-bold px-1 py-0.5 rounded border bg-slate-500/10 text-slate-400 border-slate-500/30"
                    >
                      {txt("Configuração apenas", "Config only")}
                    </span>
                  )}
                </div>
                <span className="text-[10px] opacity-70">{p.sub}</span>
                {active && <span className="absolute top-2 right-2 h-1.5 w-1.5 rounded-full bg-current opacity-80" />}
              </button>
            );
          })}
        </div>

        {multimodalNeedsFreshKey && (
          <p
            data-testid="multimodal-fresh-key-hint"
            className="flex items-center gap-1.5 text-[11px] text-amber-400"
          >
            <AlertTriangle className="h-3 w-3 shrink-0" />
            {txt(
              "Trocar de provedor exige uma nova API Key.",
              "Switching providers requires a new API key.",
            )}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sm:col-span-2 space-y-1">
            <label className="text-xs font-medium text-muted-foreground" htmlFor="multimodal-api-url">
              {txt("API URL (definida pelo provedor)", "API URL (set by the provider)")}
            </label>
            <input
              id="multimodal-api-url"
              data-testid="multimodal-api-url"
              readOnly
              value={MULTIMODAL_PROVIDER_API_URL[multimodalProvider]}
              className="w-full rounded-md border border-border bg-muted/40 px-3 py-1.5 text-xs font-mono text-muted-foreground cursor-not-allowed focus:outline-none"
            />
          </div>
          <ApiInput
            label="API Key"
            value={multimodalApiKey}
            onChange={(value) => { setMultimodalApiKey(value); setMultimodalDirty(true); }}
            type="password"
            placeholder={status?.multimodal_api_key_set
              ? "••••••••• (manter atual)"
              : multimodalProvider === "openrouter" ? "sk-or-..."
              : multimodalProvider === "groq" ? "gsk_..."
              : "sk-..."}
            disabled={isReadOnly}
            configured={status?.multimodal_api_key_set}
          />
          <ApiInput
            label={txt("Nome do Modelo", "Model Name")}
            value={multimodalModelName}
            onChange={(value) => { setMultimodalModelName(value); setMultimodalDirty(true); }}
            placeholder={MULTIMODAL_PROVIDER_MODEL_PLACEHOLDER[multimodalProvider]}
            disabled={isReadOnly}
            mono
          />
        </div>
      </div>

      {/* ── TTS - Text to Speech ── */}
      <div
        className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4"
        data-testid="tts-settings-section"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Volume2 className="h-3.5 w-3.5 text-accent" />
          <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            TTS - Text to Speech
          </p>
          <span className={`ml-auto text-[9px] font-bold px-1.5 py-0.5 rounded border ${
            ttsEnabled && status?.tts_readiness === "active"
              ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/30"
              : "bg-slate-500/10 text-slate-400 border-slate-500/30"
          }`}>
            {ttsEnabled
              ? status?.tts_readiness === "active" ? txt("Ativo", "Active") : txt("Incompleto", "Incomplete")
              : txt("Desativado", "Disabled")}
          </span>
        </div>

        <p className="text-[11px] text-muted-foreground">
          {txt(
            "Quando ativado, as respostas do agente são sintetizadas e enviadas como áudio no WhatsApp. Se o provedor falhar, a resposta é enviada em texto.",
            "When enabled, agent replies are synthesized and sent as WhatsApp audio. If the provider fails, the reply is sent as text.",
          )}
        </p>

        <SwitchRow
          label={txt("Responder em áudio", "Reply with audio")}
          hint={txt(
            "Converte automaticamente as respostas normais do agente em mensagens de voz.",
            "Automatically converts regular agent replies into voice messages.",
          )}
          ariaLabel={txt("Ativar respostas em áudio", "Enable audio replies")}
          checked={ttsEnabled}
          onCheckedChange={(checked) => { setTtsEnabled(checked); setTtsDirty(true); }}
          disabled={isReadOnly}
        />

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2" data-testid="tts-provider-list">
          {TTS_PROVIDERS.map(p => {
            const active = ttsProvider === p.id;
            return (
              <button
                key={p.id}
                type="button"
                data-testid={`tts-provider-${p.id}`}
                data-provider-id={p.id}
                disabled={isReadOnly}
                onClick={() => {
                  if (ttsProvider === p.id) return;
                  setTtsProvider(p.id);
                  setTtsApiKey("");
                  setTtsApiUrl(TTS_PROVIDER_DEFAULTS[p.id].apiUrl);
                  setTtsModelName(TTS_PROVIDER_DEFAULTS[p.id].modelName);
                  setTtsVoiceId(TTS_PROVIDER_DEFAULTS[p.id].voiceId);
                  setTtsDirty(true);
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

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <ApiInput
              label="API URL"
              value={ttsApiUrl}
              onChange={(value) => { setTtsApiUrl(value); setTtsDirty(true); }}
              placeholder={TTS_PROVIDER_DEFAULTS[ttsProvider].apiUrl}
              disabled={isReadOnly}
              mono
            />
          </div>
          <ApiInput
            label="API Key"
            value={ttsApiKey}
            onChange={(value) => { setTtsApiKey(value); setTtsDirty(true); }}
            type="password"
            placeholder={status?.tts_api_key_set
              ? "••••••••• (manter atual)"
              : ttsProvider === "elevenlabs" ? "sk_..." : "AIza..."}
            disabled={isReadOnly}
            configured={status?.tts_api_key_set}
          />
          <ApiInput
            label={ttsProvider === "elevenlabs" ? txt("Modelo", "Model") : txt("Nome da voz", "Voice name")}
            value={ttsModelName}
            onChange={(value) => { setTtsModelName(value); setTtsDirty(true); }}
            placeholder={TTS_PROVIDER_DEFAULTS[ttsProvider].modelName}
            disabled={isReadOnly}
            mono
          />
          {ttsProvider === "elevenlabs" && (
            <div className="sm:col-span-2">
              <ApiInput
                label="Voice ID"
                value={ttsVoiceId}
                onChange={(value) => { setTtsVoiceId(value); setTtsDirty(true); }}
                placeholder={TTS_PROVIDER_DEFAULTS.elevenlabs.voiceId}
                disabled={isReadOnly}
                mono
              />
            </div>
          )}
        </div>
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
  ariaLabel,
  checked,
  onCheckedChange,
  disabled,
}: {
  label: string;
  hint?: string;
  ariaLabel?: string;
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
      <Switch aria-label={ariaLabel} checked={checked} onCheckedChange={onCheckedChange} disabled={disabled} className="shrink-0" />
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
  full_address: "Rua das Flores, 123, Sala 45, Jardim Paulista, São Paulo/SP, CEP 01234-000",
  parking_info: "Estacionamento conveniado no Shopping Paulista, entrada pela Av. Brasil",
  working_hours: "Seg–Sex: 9h–19h | Sáb: 9h–14h | Dom: fechado",
  scheduling_hours: "Seg–Sex: 8h–18h",
  timezone: "America/Sao_Paulo",
  business_hours_enabled: true,
  business_hours_open: "09:00",
  business_hours_close: "19:00",
  business_hours_days: ["mon", "tue", "wed", "thu", "fri", "sat"],
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
  const { clinicId, isReadOnly, isClinicAdmin, role, isBuilder, isSuperAdmin, isInvestor, viewingClinicId } = useAuth();
  const { t, locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;
  const { toast } = useToast();
  const search = useSearch();
  const [, setLocation] = useLocation();

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
        const configUrl = (isBuilder || isSuperAdmin) && viewingClinicId
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
  const [schedulingLinkEnabled, setSchedulingLinkEnabled] = useState(false);
  const [schedulingLinkUrl, setSchedulingLinkUrl] = useState("");
  const [schedulingLinkMessage, setSchedulingLinkMessage] = useState("");
  const [schedulingLinkErrors, setSchedulingLinkErrors] = useState<Record<string, string>>({});
  const [serviceFormat, setServiceFormat] = useState("");
  const [timezone, setTimezone] = useState("America/Sao_Paulo");
  const [businessHoursEnabled, setBusinessHoursEnabled] = useState(false);
  const [businessHoursOpen, setBusinessHoursOpen] = useState("");
  const [businessHoursClose, setBusinessHoursClose] = useState("");
  const [businessHoursDays, setBusinessHoursDays] = useState<BusinessDay[]>([]);
  const [businessHoursErrors, setBusinessHoursErrors] = useState<Record<string, string>>({});
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
  const [lgpdLinkMode, setLgpdLinkMode] = useState<"neurostack" | "custom">("neurostack");
  const [lgpdCustomLink, setLgpdCustomLink] = useState("");
  const [clinicLegalName, setClinicLegalName] = useState("");
  const [clinicCnpj, setClinicCnpj] = useState("");
  const [clinicContact, setClinicContact] = useState("");
  const [dpoName, setDpoName] = useState("");
  const [dpoContact, setDpoContact] = useState("");
  const [lgpdErrors, setLgpdErrors] = useState<Record<string, string>>({});
  const [builderLgpdConsent, setBuilderLgpdConsent] = useState(false);
  const [builderSlmTraining, setBuilderSlmTraining] = useState(false);
  const [languagePolicy, setLanguagePolicy] = useState("neurostack_default");
  const [languagePolicyCustom, setLanguagePolicyCustom] = useState("");
  const [agentLanguage, setAgentLanguage] = useState("pt-BR");

  // Bot active toggle (independent from billing — pauses AI without deactivating clinic)
  const [botActive, setBotActive] = useState(true);
  const [isTogglingBot, setIsTogglingBot] = useState(false);

  // Tab 4 — Handoff
  const [handoffActive, setHandoffActive] = useState("yes");
  const [standbyMode, setStandbyMode] = useState("active");
  const [handoffPeriod, setHandoffPeriod] = useState("permanent");
  const [handoffCycleDays, setHandoffCycleDays] = useState("");
  const [handoffInactiveDefaultHours, setHandoffInactiveDefaultHours] = useState("24");
  const [hitlActive, setHitlActive] = useState(false);
  const [hitlCooldown, setHitlCooldown] = useState("15");
  const [metaMinutosAtendimento, setMetaMinutosAtendimento] = useState("10");

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
    setSchedulingLinkEnabled(
      config.scheduling_link_enabled === true ||
      config.scheduling_link_enabled === "yes" ||
      config.scheduling_link_enabled === "true",
    );
    setSchedulingLinkUrl(config.scheduling_link_url ?? "");
    setSchedulingLinkMessage(config.scheduling_link_message ?? "");
    setServiceFormat(config.service_format ?? "");
    setTimezone(config.timezone ?? "America/Sao_Paulo");
    setBusinessHoursEnabled(config.business_hours_enabled === true);
    setBusinessHoursOpen(config.business_hours_open ?? "");
    setBusinessHoursClose(config.business_hours_close ?? "");
    setBusinessHoursDays(
      Array.isArray(config.business_hours_days)
        ? config.business_hours_days.filter((day: unknown): day is BusinessDay =>
            typeof day === "string" && BUSINESS_DAYS.includes(day as BusinessDay),
          )
        : [],
    );
    setDifferentials(config.differentials ?? "");
    setAcceptsInsurance(config.accepts_insurance ?? "no");
    setInsuranceList(config.insurance_list ?? "");
    setInitialConsultation(config.initial_consultation ?? "");
    setProvidePrices(config.provide_prices ?? "no");
    setLgpdActive(config.lgpd_active ?? "yes");
    setPrivacyLink(config.privacy_link ?? "");
    setLgpdEnabled(config.lgpd_enabled === "yes");
    setLgpdMessage(config.lgpd_message || DEFAULT_LGPD_MESSAGE);
    setLgpdLinkMode(config.lgpd_custom_link ? "custom" : "neurostack");
    setLgpdCustomLink(config.lgpd_custom_link ?? "");
    setClinicLegalName(config.clinic_legal_name ?? "");
    setClinicCnpj(config.clinic_cnpj ?? "");
    setClinicContact(config.clinic_contact ?? "");
    setDpoName(config.dpo_name ?? "");
    setDpoContact(config.dpo_contact ?? "");
    setBuilderLgpdConsent(config.builder_lgpd_consent === true || config.builder_lgpd_consent === "true");
    setBuilderSlmTraining(config.builder_slm_training === true || config.builder_slm_training === "true");
    setLanguagePolicy(config.language_policy ?? "neurostack_default");
    setLanguagePolicyCustom(config.language_policy_custom ?? "");
    setAgentLanguage(config.agent_language ?? "pt-BR");
    setHandoffActive(config.handoff_active ?? "yes");
    setStandbyMode(config.standby_mode ?? "active");
    setHandoffPeriod(config.handoff_period ?? "permanent");
    setHandoffCycleDays(config.handoff_cycle_days ?? "");
    setHandoffInactiveDefaultHours(config.handoff_inactive_default_hours ?? "24");
    setMetaMinutosAtendimento(String(config.meta_minutos_atendimento ?? "10"));
    setOnlineServicesDesc(config.online_services_desc ?? "");
    setPresentialServicesDesc(config.presential_services_desc ?? "");
    setServices(config.services ?? []);
    // bot_active: treat null/undefined as true (default on)
    setBotActive(config.bot_active !== false);
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

  const handleBotToggle = async (newValue: boolean) => {
    setIsTogglingBot(true);
    setBotActive(newValue); // optimistic update
    try {
      const token = getToken();
      const toggleUrl = (isBuilder || isSuperAdmin) && viewingClinicId
        ? `/v1/config/bot-toggle?tenant_id=${viewingClinicId}`
        : "/v1/config/bot-toggle";
      const res = await fetch(toggleUrl, {
        method: "PATCH",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Toggle failed: ${res.status}`);
      const data = await res.json();
      setBotActive(data.bot_active);
      toast({
        title: data.bot_active
          ? txt("IA Ativada ✅", "AI Enabled ✅")
          : txt("IA Pausada ⏸️", "AI Paused ⏸️"),
        description: data.bot_active
          ? txt("O assistente voltou a responder normalmente.", "The assistant is responding normally again.")
          : txt("O assistente não responderá mensagens até ser reativado.", "The assistant won't respond to messages until re-enabled."),
      });
    } catch {
      setBotActive(!newValue); // revert on error
      toast({
        title: txt("Erro", "Error"),
        description: txt("Não foi possível alterar o estado da IA.", "Could not change AI state."),
        variant: "destructive",
      });
    } finally {
      setIsTogglingBot(false);
    }
  };

  const handleSave = async () => {
    // LGPD hard block — validate before API call
    const lgpdValidationErrors: Record<string, string> = {};
    const requiredMsg = txt("Campo obrigatório para cumprimento da LGPD", "Required for LGPD compliance");
    if (!clinicLegalName.trim()) lgpdValidationErrors.clinic_legal_name = requiredMsg;
    if (lgpdLinkMode === "neurostack") {
      if (!clinicCnpj.trim()) lgpdValidationErrors.clinic_cnpj = requiredMsg;
      if (!dpoName.trim()) lgpdValidationErrors.dpo_name = requiredMsg;
      if (!dpoContact.trim()) lgpdValidationErrors.dpo_contact = requiredMsg;
    } else if (lgpdLinkMode === "custom") {
      if (!lgpdCustomLink.trim()) lgpdValidationErrors.lgpd_custom_link = requiredMsg;
    }
    if (!builderLgpdConsent) {
      lgpdValidationErrors.builder_lgpd_consent = "É necessário confirmar o contrato com a clínica antes de salvar.";
    }
    if (Object.keys(lgpdValidationErrors).length > 0) {
      setLgpdErrors(lgpdValidationErrors);
      toast({ title: txt("Preencha os campos obrigatórios de LGPD antes de salvar.", "Fill in the required LGPD fields before saving."), variant: "destructive" });
      return;
    }
    setLgpdErrors({});

    // Scheduling link — https-only URL validation (aligned with backend)
    const schedulingLinkValidationErrors: Record<string, string> = {};
    const trimmedSchedulingLinkUrl = schedulingLinkUrl.trim();
    if (schedulingLinkEnabled) {
      if (!trimmedSchedulingLinkUrl) {
        schedulingLinkValidationErrors.scheduling_link_url = txt(
          "Informe a URL do link de agendamento (https://…).",
          "Provide the scheduling link URL (https://…).",
        );
      } else {
        let parsed: URL | null = null;
        try { parsed = new URL(trimmedSchedulingLinkUrl); } catch { parsed = null; }
        if (
          !parsed ||
          parsed.protocol !== "https:" ||
          !parsed.hostname ||
          !trimmedSchedulingLinkUrl.toLowerCase().startsWith("https://")
        ) {
          schedulingLinkValidationErrors.scheduling_link_url = txt(
            "A URL deve começar com https:// (http:// e outros esquemas não são aceitos).",
            "The URL must start with https:// (http:// and other schemes are not accepted).",
          );
        }
      }
    }
    if (Object.keys(schedulingLinkValidationErrors).length > 0) {
      setSchedulingLinkErrors(schedulingLinkValidationErrors);
      toast({
        title: txt(
          "URL do link de agendamento inválida.",
          "Invalid scheduling link URL.",
        ),
        variant: "destructive",
      });
      return;
    }
    setSchedulingLinkErrors({});

    const businessHoursValidationErrors: Record<string, string> = {};
    if (businessHoursEnabled) {
      if (!businessHoursOpen) {
        businessHoursValidationErrors.business_hours_open = txt("Informe a hora de abertura.", "Provide the opening time.");
      }
      if (!businessHoursClose) {
        businessHoursValidationErrors.business_hours_close = txt("Informe a hora de fechamento.", "Provide the closing time.");
      } else if (businessHoursOpen && businessHoursClose <= businessHoursOpen) {
        businessHoursValidationErrors.business_hours_close = txt(
          "O fechamento deve ser posterior à abertura.",
          "Closing time must be later than opening time.",
        );
      }
      if (businessHoursDays.length === 0) {
        businessHoursValidationErrors.business_hours_days = txt(
          "Selecione pelo menos um dia de atendimento.",
          "Select at least one business day.",
        );
      }
    }
    if (Object.keys(businessHoursValidationErrors).length > 0) {
      setBusinessHoursErrors(businessHoursValidationErrors);
      toast({
        title: txt("Revise o horário de atendimento.", "Review the business hours."),
        description: txt(
          "Preencha abertura, fechamento e dias; o fechamento deve ser posterior à abertura.",
          "Provide opening, closing, and days; closing must be later than opening.",
        ),
        variant: "destructive",
      });
      return;
    }
    setBusinessHoursErrors({});

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
      scheduling_link_enabled: schedulingLinkEnabled,
      scheduling_link_url: schedulingLinkEnabled ? (trimmedSchedulingLinkUrl || null) : null,
      scheduling_link_message: schedulingLinkEnabled ? (schedulingLinkMessage.trim() || null) : null,
      service_format: serviceFormat || null,
      timezone: timezone,
      business_hours_enabled: businessHoursEnabled,
      business_hours_open: businessHoursEnabled ? businessHoursOpen : null,
      business_hours_close: businessHoursEnabled ? businessHoursClose : null,
      business_hours_days: businessHoursEnabled ? businessHoursDays : null,
      accepts_insurance: acceptsInsurance || null,
      insurance_list: insuranceList.trim() || null,
      initial_consultation: initialConsultation || null,
      provide_prices: providePrices || null,
      lgpd_active: lgpdActive || null,
      privacy_link: privacyLink.trim() || null,
      lgpd_enabled: lgpdEnabled ? "yes" : "no",
      lgpd_message: lgpdMessage.trim() || null,
      lgpd_custom_link: lgpdLinkMode === "custom" ? lgpdCustomLink.trim() || null : null,
      clinic_legal_name: clinicLegalName.trim() || null,
      clinic_cnpj: clinicCnpj.trim() || null,
      clinic_contact: clinicContact.trim() || null,
      dpo_name: dpoName.trim() || null,
      dpo_contact: dpoContact.trim() || null,
      builder_lgpd_consent: builderLgpdConsent,
      builder_slm_training: builderSlmTraining,
      language_policy: languagePolicy || null,
      language_policy_custom: languagePolicyCustom.trim() || null,
      agent_language: agentLanguage || "pt-BR",
      handoff_active: "yes",
      standby_mode: standbyMode || null,
      handoff_period: handoffPeriod || null,
      handoff_cycle_days: handoffCycleDays.trim() || null,
      handoff_inactive_default_hours: parseInt(handoffInactiveDefaultHours, 10) || 24,
      meta_minutos_atendimento: parseInt(metaMinutosAtendimento, 10) || 10,
      online_services_desc: onlineServicesDesc.trim() || null,
      presential_services_desc: presentialServicesDesc.trim() || null,
    };
    try {
      const token = getToken();
      const patchUrl = (isBuilder || isSuperAdmin) && viewingClinicId
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
      const responseBody: unknown = await res.json().catch(() => null);
      if (!res.ok) {
        toast({
          title: t("config_save_fail"),
          description: describeConfigSaveFailure(responseBody, res.status, locale),
          variant: "destructive",
        });
        return;
      }
      const updated = responseBody;
      setConfig(updated);
      toast({ title: t("config_saved"), description: t("config_saved_desc") });
    } catch {
      toast({
        title: t("config_save_fail"),
        description: txt(
          "Não foi possível conectar ao servidor. Verifique sua conexão e tente novamente.",
          "Could not connect to the server. Check your connection and try again.",
        ),
        variant: "destructive",
      });
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

  // ── Tab state controlled by `?tab=...` query string ─────────────────────────
  // Wouter's `useSearch()` returns the raw query string (without the leading
  // `?`). When a requested tab is not allowed for the current role, we fall
  // back to the first tab that IS allowed rather than silently exposing a
  // hidden tab or crashing. Any state change writes back to the URL via
  // `setLocation` so deep-links (e.g. `/voice-ai` → `/config?tab=integracoes`)
  // land exactly where the user expects.
  const ALL_TABS = [
    { value: "identidade",  clinicHidden: false },
    { value: "operacao",    clinicHidden: false },
    { value: "negocio",     clinicHidden: false },
    { value: "handoff",     clinicHidden: true  },
    { value: "servicos",    clinicHidden: false },
    { value: "integracoes", clinicHidden: true  },
  ];
  const allowedTabValues = ALL_TABS.filter(
    (tab) => !(isClinicAdmin && tab.clinicHidden),
  ).map((tab) => tab.value);
  const requestedTab = new URLSearchParams(search).get("tab") ?? "identidade";
  const activeTab = allowedTabValues.includes(requestedTab)
    ? requestedTab
    : (allowedTabValues[0] ?? "identidade");

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

      {/* ── Controle da IA — bot_active toggle (clinic_admin + builder only) ── */}
      {!isInvestor && !isSuperAdmin && (
        <Card className={`${cardShell} ${!botActive ? "border-amber-500/40" : ""}`} data-testid="card-bot-active">
          <CardContent className="pt-4 pb-4">
            <div className="flex items-center justify-between gap-4">
              <div className="flex items-center gap-3">
                <div className={`flex h-9 w-9 items-center justify-center rounded-lg border ${botActive ? "bg-emerald-500/10 border-emerald-500/20" : "bg-amber-500/10 border-amber-500/20"}`}>
                  <Cpu className={`h-4 w-4 ${botActive ? "text-emerald-400" : "text-amber-400"}`} />
                </div>
                <div>
                  <p className="text-sm font-semibold text-foreground">
                    {txt("Controle da IA", "AI Control")}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {botActive
                      ? txt("Assistente ativo, respondendo normalmente", "Assistant active, responding normally")
                      : txt("IA pausada, mensagens não serão respondidas pela IA", "AI paused, messages won't be answered by the AI")}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {isTogglingBot && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
                {!botActive && (
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-400 bg-amber-500/10 border border-amber-500/30 rounded px-1.5 py-0.5">
                    {txt("Pausada", "Paused")}
                  </span>
                )}
                <Switch
                  checked={botActive}
                  onCheckedChange={handleBotToggle}
                  disabled={isTogglingBot || isReadOnly}
                  data-testid="switch-bot-active"
                />
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={(v) => setLocation(`/config?tab=${v}`)}
        className="w-full"
      >
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
                    <SelectItem value="name_only">{txt('Nome - "Olá, sou a Lumina"', 'Name - "Hi, I\'m Lumina"')}</SelectItem>
                    <SelectItem value="name_attendant">{txt("Nome - Atendente Virtual", "Name - Virtual Attendant")}</SelectItem>
                    <SelectItem value="name_ai">{txt("Nome - Assistente IA", "Name - AI Assistant")}</SelectItem>
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
                    <SelectItem value="affirm_ai">{txt("Afirma ser IA, foco em agilidade", "Confirms being AI, speed-focused")}</SelectItem>
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
                  placeholder={txt("Ex: Rua das Flores, 123, Sala 45, Jardim Paulista, São Paulo/SP, CEP 01234-000", "e.g. 123 Flower St, Suite 45, Downtown, São Paulo/SP, ZIP 01234-000")}
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

              {/* Scheduling link (stopgap pré-ERP) */}
              <SwitchRow
                label={txt("Link de Agendamento", "Scheduling Link")}
                hint={txt(
                  "Quando ativo, a IA envia o link (definido abaixo) ao paciente na confirmação do agendamento e cria um handoff pendente de verificação.",
                  "When enabled, the AI sends the link (defined below) to the patient on scheduling confirmation and creates a pending-verification handoff.",
                )}
                checked={schedulingLinkEnabled}
                onCheckedChange={(v) => {
                  setSchedulingLinkEnabled(v);
                  setSchedulingLinkErrors((prev) => { const n = { ...prev }; delete n.scheduling_link_url; return n; });
                }}
                disabled={isReadOnly}
              />
              {schedulingLinkEnabled && (
                <>
                  <Field
                    label={txt("URL do Link de Agendamento", "Scheduling Link URL")}
                    hint={txt(
                      "Somente https:// é aceito. Esta URL é entregue exatamente como configurada ao paciente.",
                      "Only https:// is accepted. This URL is delivered to the patient exactly as configured.",
                    )}
                  >
                    <Input
                      value={schedulingLinkUrl}
                      onChange={(e) => {
                        setSchedulingLinkUrl(e.target.value);
                        setSchedulingLinkErrors((prev) => { const n = { ...prev }; delete n.scheduling_link_url; return n; });
                      }}
                      placeholder="https://agenda.suaclinica.com.br/marcar"
                      type="url"
                      className={`text-sm font-mono${schedulingLinkErrors.scheduling_link_url ? " border-red-500" : ""}`}
                      disabled={isReadOnly}
                      data-testid="input-scheduling-link-url"
                    />
                    {schedulingLinkErrors.scheduling_link_url && (
                      <p className="text-xs text-red-400 mt-1">{schedulingLinkErrors.scheduling_link_url}</p>
                    )}
                  </Field>
                  <Field
                    label={txt("Mensagem do Link de Agendamento", "Scheduling Link Message")}
                    hint={txt(
                      "Texto enviado ao paciente antes do link. A URL será adicionada logo abaixo.",
                      "Text sent to the patient before the link. The URL will be appended right below.",
                    )}
                  >
                    <Textarea
                      value={schedulingLinkMessage}
                      onChange={(e) => setSchedulingLinkMessage(e.target.value)}
                      placeholder={txt(
                        "Ex: Perfeito! Para concluir seu agendamento, acesse o link abaixo e escolha o horário desejado. Depois me avise se conseguiu confirmar. 😊",
                        "e.g. Great! To finish scheduling, use the link below and pick a time. Let me know once you've confirmed. 😊",
                      )}
                      rows={3}
                      className="resize-none text-sm"
                      disabled={isReadOnly}
                      data-testid="input-scheduling-link-message"
                    />
                  </Field>
                </>
              )}

              <Field
                label={txt("Fuso horário", "Timezone")}
                hint={txt("Define o horário local usado nas saudações e na métrica fora do horário.", "Sets the local time used for greetings and the outside-hours metric.")}
              >
                <Select value={timezone} onValueChange={setTimezone} disabled={isReadOnly}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue placeholder={txt("Selecione...", "Select...")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="America/Sao_Paulo">{txt("São Paulo (UTC-3)", "São Paulo (UTC-3)")}</SelectItem>
                    <SelectItem value="America/Manaus">{txt("Manaus (UTC-4)", "Manaus (UTC-4)")}</SelectItem>
                    <SelectItem value="America/Rio_Branco">{txt("Rio Branco (UTC-5)", "Rio Branco (UTC-5)")}</SelectItem>
                    <SelectItem value="America/Noronha">{txt("Fernando de Noronha (UTC-2)", "Fernando de Noronha (UTC-2)")}</SelectItem>
                    <SelectItem value="America/New_York">{txt("Nova York (UTC-5/-4)", "New York (UTC-5/-4)")}</SelectItem>
                    <SelectItem value="America/Chicago">{txt("Chicago (UTC-6/-5)", "Chicago (UTC-6/-5)")}</SelectItem>
                    <SelectItem value="America/Denver">{txt("Denver (UTC-7/-6)", "Denver (UTC-7/-6)")}</SelectItem>
                    <SelectItem value="America/Los_Angeles">{txt("Los Angeles (UTC-8/-7)", "Los Angeles (UTC-8/-7)")}</SelectItem>
                    <SelectItem value="America/Mexico_City">{txt("Cidade do México (UTC-6)", "Mexico City (UTC-6)")}</SelectItem>
                    <SelectItem value="America/Bogota">{txt("Bogotá (UTC-5)", "Bogotá (UTC-5)")}</SelectItem>
                    <SelectItem value="America/Lima">{txt("Lima (UTC-5)", "Lima (UTC-5)")}</SelectItem>
                    <SelectItem value="America/Buenos_Aires">{txt("Buenos Aires (UTC-3)", "Buenos Aires (UTC-3)")}</SelectItem>
                    <SelectItem value="America/Santiago">{txt("Santiago (UTC-4/-3)", "Santiago (UTC-4/-3)")}</SelectItem>
                    <SelectItem value="Atlantic/Reykjavik">{txt("Reykjavik (UTC+0)", "Reykjavik (UTC+0)")}</SelectItem>
                    <SelectItem value="Europe/Lisbon">{txt("Lisboa (UTC+0/+1)", "Lisbon (UTC+0/+1)")}</SelectItem>
                    <SelectItem value="Europe/London">{txt("Londres (UTC+0/+1)", "London (UTC+0/+1)")}</SelectItem>
                    <SelectItem value="Europe/Madrid">{txt("Madri (UTC+1/+2)", "Madrid (UTC+1/+2)")}</SelectItem>
                    <SelectItem value="Europe/Paris">{txt("Paris (UTC+1/+2)", "Paris (UTC+1/+2)")}</SelectItem>
                    <SelectItem value="Europe/Berlin">{txt("Berlim (UTC+1/+2)", "Berlin (UTC+1/+2)")}</SelectItem>
                    <SelectItem value="Europe/Rome">{txt("Roma (UTC+1/+2)", "Rome (UTC+1/+2)")}</SelectItem>
                  </SelectContent>
                </Select>
              </Field>

              <SwitchRow
                label={txt("Métrica de atendimentos fora do horário", "Outside-hours metric")}
                ariaLabel={txt("Ativar métrica de atendimentos fora do horário", "Enable outside-hours metric")}
                hint={txt(
                  "Use um horário estruturado para medir conversas recebidas fora do período de atendimento.",
                  "Use structured hours to measure conversations received outside business hours.",
                )}
                checked={businessHoursEnabled}
                onCheckedChange={(enabled) => {
                  setBusinessHoursEnabled(enabled);
                  setBusinessHoursErrors({});
                }}
                disabled={isReadOnly}
              />
              {businessHoursEnabled && (
                <div className="space-y-4 rounded-md border border-border bg-muted/10 p-3">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label={txt("Abertura", "Opening time")}>
                      <Input
                        type="time"
                        aria-label={txt("Hora de abertura", "Opening time")}
                        aria-invalid={Boolean(businessHoursErrors.business_hours_open)}
                        aria-describedby={businessHoursErrors.business_hours_open ? "business-hours-open-error" : undefined}
                        value={businessHoursOpen}
                        onChange={(e) => {
                          setBusinessHoursOpen(e.target.value);
                          setBusinessHoursErrors((prev) => {
                            const next = { ...prev };
                            delete next.business_hours_open;
                            delete next.business_hours_close;
                            return next;
                          });
                        }}
                        className={businessHoursErrors.business_hours_open ? "border-red-500" : ""}
                        disabled={isReadOnly}
                        data-testid="input-business-hours-open"
                      />
                      {businessHoursErrors.business_hours_open && (
                        <p id="business-hours-open-error" role="alert" className="text-xs text-red-400 mt-1">
                          {businessHoursErrors.business_hours_open}
                        </p>
                      )}
                    </Field>
                    <Field label={txt("Fechamento", "Closing time")}>
                      <Input
                        type="time"
                        aria-label={txt("Hora de fechamento", "Closing time")}
                        aria-invalid={Boolean(businessHoursErrors.business_hours_close)}
                        aria-describedby={businessHoursErrors.business_hours_close ? "business-hours-close-error" : undefined}
                        value={businessHoursClose}
                        onChange={(e) => {
                          setBusinessHoursClose(e.target.value);
                          setBusinessHoursErrors((prev) => {
                            const next = { ...prev };
                            delete next.business_hours_close;
                            return next;
                          });
                        }}
                        className={businessHoursErrors.business_hours_close ? "border-red-500" : ""}
                        disabled={isReadOnly}
                        data-testid="input-business-hours-close"
                      />
                      {businessHoursErrors.business_hours_close && (
                        <p id="business-hours-close-error" role="alert" className="text-xs text-red-400 mt-1">
                          {businessHoursErrors.business_hours_close}
                        </p>
                      )}
                    </Field>
                  </div>
                  <Field
                    label={txt("Dias de atendimento", "Business days")}
                    hint={txt("Selecione todos os dias em que a clínica atende.", "Select every day the clinic is open.")}
                  >
                    <div
                      role="group"
                      aria-label={txt("Dias de atendimento", "Business days")}
                      className="grid grid-cols-4 gap-2 sm:grid-cols-7"
                      aria-invalid={Boolean(businessHoursErrors.business_hours_days)}
                      aria-describedby={businessHoursErrors.business_hours_days ? "business-hours-days-error" : undefined}
                    >
                      {BUSINESS_DAYS.map((day, index) => {
                        const selected = businessHoursDays.includes(day);
                        const labels = [
                          txt("Seg", "Mon"), txt("Ter", "Tue"), txt("Qua", "Wed"), txt("Qui", "Thu"),
                          txt("Sex", "Fri"), txt("Sáb", "Sat"), txt("Dom", "Sun"),
                        ];
                        return (
                          <button
                            key={day}
                            type="button"
                            aria-pressed={selected}
                            onClick={() => {
                              setBusinessHoursDays((prev) =>
                                selected ? prev.filter((value) => value !== day) : [...prev, day],
                              );
                              setBusinessHoursErrors((prev) => {
                                const next = { ...prev };
                                delete next.business_hours_days;
                                return next;
                              });
                            }}
                            disabled={isReadOnly}
                            data-testid={`toggle-business-day-${day}`}
                            className={`rounded-md border px-2 py-2 text-xs font-medium transition-colors disabled:opacity-50 ${
                              selected
                                ? "border-accent bg-accent text-accent-foreground"
                                : "border-border bg-background text-muted-foreground hover:text-foreground"
                            }`}
                          >
                            {labels[index]}
                          </button>
                        );
                      })}
                    </div>
                    {businessHoursErrors.business_hours_days && (
                      <p id="business-hours-days-error" role="alert" className="text-xs text-red-400 mt-1">
                        {businessHoursErrors.business_hours_days}
                      </p>
                    )}
                  </Field>
                </div>
              )}

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
                hint={txt("Injetado como contexto quando o SRM detecta pedido de preço, antes de qualquer valor.", "Injected as context when SRM detects a price request, before any value.")}
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
                hint={txt("Envia mensagem de abertura com aviso de privacidade na primeira interação", "Sends a privacy notice on the first interaction")}
                checked={lgpdEnabled}
                onCheckedChange={setLgpdEnabled}
                disabled={isReadOnly}
              />

              {/* Ergonomist banner — always visible, explains why fields are needed even when OFF */}
              <div className="rounded-md border border-amber-500/20 bg-amber-500/5 px-3 py-2 flex items-start gap-2">
                <Shield className="h-3.5 w-3.5 text-amber-400 shrink-0 mt-0.5" />
                <p className="text-[11px] text-amber-400/90 leading-relaxed">
                  {txt(
                    "Os dados abaixo são obrigatórios mesmo com o aviso desativado. Quando um paciente perguntar sobre privacidade ou LGPD, o agente precisa fornecer o link da política, independente do toggle.",
                    "The fields below are required even when the notice is off. When a patient asks about privacy or LGPD, the agent must provide the policy link, regardless of the toggle."
                  )}
                </p>
              </div>

              {/* Privacy link selector — always visible */}
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
                    <div>
                      <Input
                        value={lgpdCustomLink}
                        onChange={(e) => { setLgpdCustomLink(e.target.value); setLgpdErrors(prev => { const n = {...prev}; delete n.lgpd_custom_link; return n; }); }}
                        placeholder="https://suaclinica.com.br/privacidade"
                        type="url"
                        className={`text-sm font-mono${lgpdErrors.lgpd_custom_link ? " border-red-500" : ""}`}
                        disabled={isReadOnly}
                      />
                      {lgpdErrors.lgpd_custom_link && <p className="text-xs text-red-400 mt-1">{lgpdErrors.lgpd_custom_link}</p>}
                    </div>
                  )}
                </div>
              </Field>

              {/* Clinic data fields — always visible (required for LGPD compliance) */}
              <div className="rounded-md border border-violet-500/20 bg-violet-500/5 p-3 space-y-2">
                <p className="text-[11px] font-medium text-violet-400">
                  {txt("Dados da clínica responsável pelo tratamento de dados *", "Clinic data controller fields *")}
                </p>
                <div className="grid grid-cols-2 gap-2">
                  <Field label={txt("Nome Legal da Clínica *", "Clinic Legal Name *")} hint="">
                    <Input
                      value={clinicLegalName}
                      onChange={(e) => { setClinicLegalName(e.target.value); setLgpdErrors(prev => { const n = {...prev}; delete n.clinic_legal_name; return n; }); }}
                      placeholder={txt("Clínica Saúde Ltda.", "Health Clinic Ltd.")}
                      className={`text-sm h-8${lgpdErrors.clinic_legal_name ? " border-red-500" : ""}`}
                      disabled={isReadOnly}
                    />
                    {lgpdErrors.clinic_legal_name && <p className="text-xs text-red-400 mt-1">{lgpdErrors.clinic_legal_name}</p>}
                  </Field>
                  <Field label={lgpdLinkMode === "neurostack" ? txt("CNPJ *", "Tax ID / CNPJ *") : txt("CNPJ", "Tax ID / CNPJ")} hint="">
                    <Input
                      value={clinicCnpj}
                      onChange={(e) => { setClinicCnpj(e.target.value); setLgpdErrors(prev => { const n = {...prev}; delete n.clinic_cnpj; return n; }); }}
                      placeholder="00.000.000/0001-00"
                      className={`text-sm h-8 font-mono${lgpdErrors.clinic_cnpj ? " border-red-500" : ""}`}
                      disabled={isReadOnly}
                    />
                    {lgpdErrors.clinic_cnpj && <p className="text-xs text-red-400 mt-1">{lgpdErrors.clinic_cnpj}</p>}
                  </Field>
                </div>
                <Field label={txt("E-mail / Telefone de Contato da Clínica", "Clinic Contact Email / Phone")} hint="">
                  <Input value={clinicContact} onChange={(e) => setClinicContact(e.target.value)} placeholder={txt("contato@suaclinica.com.br / (11) 3000-0000", "contact@yourclinic.com / +1 (555) 000-0000")} className="text-sm h-8" disabled={isReadOnly} />
                </Field>
                <div className="grid grid-cols-2 gap-2">
                  <Field label={lgpdLinkMode === "neurostack" ? txt("Nome do Encarregado (DPO) *", "DPO Name *") : txt("Nome do Encarregado (DPO)", "DPO Name")} hint="">
                    <Input
                      value={dpoName}
                      onChange={(e) => { setDpoName(e.target.value); setLgpdErrors(prev => { const n = {...prev}; delete n.dpo_name; return n; }); }}
                      placeholder={txt("João Silva", "John Smith")}
                      className={`text-sm h-8${lgpdErrors.dpo_name ? " border-red-500" : ""}`}
                      disabled={isReadOnly}
                    />
                    {lgpdErrors.dpo_name && <p className="text-xs text-red-400 mt-1">{lgpdErrors.dpo_name}</p>}
                  </Field>
                  <Field label={lgpdLinkMode === "neurostack" ? txt("E-mail / Contato do DPO *", "DPO Email / Contact *") : txt("E-mail / Contato do DPO", "DPO Email / Contact")} hint="">
                    <Input
                      value={dpoContact}
                      onChange={(e) => { setDpoContact(e.target.value); setLgpdErrors(prev => { const n = {...prev}; delete n.dpo_contact; return n; }); }}
                      placeholder={txt("dpo@suaclinica.com.br", "dpo@yourclinic.com")}
                      className={`text-sm h-8${lgpdErrors.dpo_contact ? " border-red-500" : ""}`}
                      disabled={isReadOnly}
                    />
                    {lgpdErrors.dpo_contact && <p className="text-xs text-red-400 mt-1">{lgpdErrors.dpo_contact}</p>}
                  </Field>
                </div>

                {lgpdLinkMode === "neurostack" && (
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
                )}
              </div>

              {/* Opening message — only when toggle ON */}
              {lgpdEnabled && (
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
              )}

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

              {/* ── Builder Legal Consents ── */}
              <div className="space-y-3 rounded-lg border border-slate-700/40 bg-muted/10 p-4">
                <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Declarações do Builder
                </p>

                {/* Mandatory: LGPD contract */}
                <label className={`flex items-start gap-3 cursor-pointer ${isReadOnly ? "opacity-50 pointer-events-none" : ""}`}>
                  <input
                    type="checkbox"
                    checked={builderLgpdConsent}
                    onChange={e => setBuilderLgpdConsent(e.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-border accent-violet-500"
                  />
                  <span className="text-xs text-foreground leading-snug">
                    <span className="font-semibold text-red-400 mr-1">*</span>
                    Confirmo que possuo contrato formal com esta clínica e autorização legal para processar os dados
                    de seus pacientes via inteligência artificial, assumindo a responsabilidade como{" "}
                    <strong>Operador dos dados</strong> perante a LGPD.
                  </span>
                </label>
                {lgpdErrors.builder_lgpd_consent && (
                  <p className="text-xs text-destructive flex items-center gap-1.5">
                    <AlertTriangle className="h-3 w-3" /> {lgpdErrors.builder_lgpd_consent}
                  </p>
                )}

                {/* Optional: SLM training */}
                <label className={`flex items-start gap-3 cursor-pointer ${isReadOnly ? "opacity-50 pointer-events-none" : ""}`}>
                  <input
                    type="checkbox"
                    checked={builderSlmTraining}
                    onChange={e => setBuilderSlmTraining(e.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-border accent-violet-500"
                  />
                  <span className="text-xs text-foreground leading-snug">
                    Confirmo que o contrato com a clínica autoriza expressamente a anonimização e o uso do histórico
                    de interações para o treinamento e sintonia fina (fine-tuning) de{" "}
                    <strong>Modelos de Linguagem Próprios (SLMs)</strong>, visando a especialização do atendimento
                    e a redução de custos da infraestrutura.
                  </span>
                </label>
                {builderSlmTraining && (
                  <p className="text-[10px] text-amber-400/80 ml-7 leading-snug">
                    Ao marcar esta opção, um adendo especial sobre uso de dados anonimizados para IA será exibido
                    na Política de Privacidade pública da clínica.
                  </p>
                )}
              </div>
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
                {txt("Handoff sempre ativo, agentes especializados estão no roadmap. Em breve será possível alternar entre handoff humano e agentes treinados por área.", "Handoff always active, specialized agents are on the roadmap. Soon you'll be able to switch between human handoff and area-specialized agents.")}
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
                  <div className="text-[11px] text-muted-foreground -mt-1 ml-1">
                    {txt(
                      `Se inativo, opera em handoff padrão de ${handoffInactiveDefaultHours}h (janela WhatsApp API)`,
                      `If inactive, operates with default handoff of ${handoffInactiveDefaultHours}h (WhatsApp API window)`
                    )}
                  </div>

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

                  <Field
                    label={txt("Período padrão (horas) quando Standby está inativo", "Default period (hours) when Standby is inactive")}
                    hint={txt("Padrão: 24h. Aplicado automaticamente quando o modo Stand-by está desativado.", "Default: 24h. Applied automatically when Stand-by mode is off.")}
                  >
                    <Input
                      type="number"
                      min="1"
                      max="720"
                      value={handoffInactiveDefaultHours}
                      onChange={(e) => setHandoffInactiveDefaultHours(e.target.value)}
                      placeholder="24"
                      className="w-32 text-sm"
                      disabled={isReadOnly}
                    />
                  </Field>

                  <Field
                    label={t("handoff_meta_minutos_label")}
                    hint={t("handoff_meta_minutos_hint")}
                  >
                    <Input
                      type="number"
                      min={1}
                      max={120}
                      value={metaMinutosAtendimento}
                      onChange={(e) => setMetaMinutosAtendimento(e.target.value)}
                      placeholder="10"
                      className="w-32 text-sm"
                      disabled={isReadOnly}
                    />
                  </Field>
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
                clinicId={(isBuilder || isSuperAdmin) && viewingClinicId ? viewingClinicId : clinicId}
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
