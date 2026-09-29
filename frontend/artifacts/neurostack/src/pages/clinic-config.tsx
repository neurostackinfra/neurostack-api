import { useEffect, useState, useCallback, useRef } from "react";
import { useSearch, useLocation } from "wouter";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";

interface ClinicProfessional {
  name: string;
  description: string;
  specialty: string;
}

type ServiceFormat = "online" | "presential" | "both";

interface ClinicService {
  name: string;
  description: string;
  price?: string;
  authority_notes?: string;
  service_format?: ServiceFormat | null;
  /** Internal hydration marker; it is deliberately excluded from PATCH bodies. */
  __legacyServiceString?: string;
  [key: string]: unknown;
}

const SERVICE_FORMATS: readonly ServiceFormat[] = ["online", "presential", "both"];

function isServiceFormat(value: unknown): value is ServiceFormat {
  return typeof value === "string" && SERVICE_FORMATS.includes(value as ServiceFormat);
}

/**
 * Legacy string entries need a card in the new UI, but must remain strings in
 * the persisted array until the person explicitly confirms a modality.
 */
function hydrateClinicService(value: unknown): ClinicService {
  if (typeof value === "string") {
    return {
      name: value,
      description: "",
      price: "",
      authority_notes: "",
      service_format: null,
      __legacyServiceString: value,
    };
  }

  if (isRecord(value)) {
    return {
      ...value,
      name: typeof value.name === "string" ? value.name : "",
      description: typeof value.description === "string" ? value.description : "",
      price: typeof value.price === "string" ? value.price : "",
      authority_notes: typeof value.authority_notes === "string" ? value.authority_notes : "",
      service_format: isServiceFormat(value.service_format) ? value.service_format : null,
    };
  }

  return { name: "", description: "", price: "", authority_notes: "", service_format: null };
}

function serializeClinicService(service: ClinicService): ClinicService | string {
  if (service.__legacyServiceString !== undefined && !isServiceFormat(service.service_format)) {
    return service.__legacyServiceString;
  }
  const { __legacyServiceString: _legacyServiceString, ...serialized } = service;
  return serialized;
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
  Cpu, MessageSquare, Lock, ChevronDown, ExternalLink, FileText, Mic2, Volume2,
  AudioLines,
} from "lucide-react";
import {
  ProviderCredentialCard,
  type ProviderCardState,
} from "@/components/api-settings/provider-credential-card";
import { VoiceResponseControls } from "@/components/api-settings/voice-response-controls";
import {
  BusinessHoursEditor,
  BUSINESS_DAY_ORDER,
  CompactHoursCard,
  type BusinessDayCode,
  type BusinessHoursMap,
} from "@/components/business-hours-editor";

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
  business_hours: { pt: "horários de funcionamento", en: "business hours" },
  handoff_cycle_days: { pt: "dias do ciclo de handoff", en: "handoff cycle days" },
  handoff_inactive_default_hours: { pt: "horas de inatividade do handoff", en: "handoff inactivity hours" },
  meta_minutos_atendimento: { pt: "meta de minutos para atendimento", en: "service-time target in minutes" },
  whatsapp_provider: { pt: "provedor do WhatsApp", en: "WhatsApp provider" },
  zapi_instance_id: { pt: "ID da instância Z-API", en: "Z-API instance ID" },
  zapi_token: { pt: "token da Z-API", en: "Z-API token" },
  zapi_client_token: { pt: "token de cliente da Z-API", en: "Z-API client token" },
  zapi_inbound_token_digest: { pt: "token de entrada da Z-API", en: "Z-API inbound token" },
  evolution_api_url: { pt: "URL da Evolution API", en: "Evolution API URL" },
  evolution_instance_name: { pt: "nome da instância Evolution", en: "Evolution instance name" },
  evolution_api_key: { pt: "API Key da Evolution API", en: "Evolution API key" },
  meta_phone_number_id: { pt: "Phone Number ID da Meta", en: "Meta Phone Number ID" },
  meta_waba_id: { pt: "WABA ID da Meta", en: "Meta WABA ID" },
  meta_permanent_token: { pt: "token permanente da Meta", en: "Meta permanent token" },
  meta_app_secret: { pt: "App Secret da Meta", en: "Meta App Secret" },
  meta_verify_token: { pt: "token de verificação da Meta", en: "Meta verification token" },
  uazapi_api_url: { pt: "URL da Uazapi", en: "Uazapi URL" },
  uazapi_instance_name: { pt: "nome da instância Uazapi", en: "Uazapi instance name" },
  uazapi_token: { pt: "token da Uazapi", en: "Uazapi token" },
  llm_provider: { pt: "provedor de LLM", en: "LLM provider" },
  llm_api_key: { pt: "API Key do LLM", en: "LLM API key" },
  llm_api_url: { pt: "URL da API do LLM", en: "LLM API URL" },
  llm_base_url: { pt: "URL base do LLM", en: "LLM base URL" },
  llm_model_name: { pt: "modelo do LLM", en: "LLM model" },
  stt_provider: { pt: "provedor de transcrição", en: "transcription provider" },
  stt_api_key: { pt: "API Key de transcrição", en: "transcription API key" },
  stt_api_url: { pt: "URL da API de transcrição", en: "transcription API URL" },
  stt_model_name: { pt: "modelo de transcrição", en: "transcription model" },
  whisper_api_key: { pt: "API Key do Whisper", en: "Whisper API key" },
  whisper_model: { pt: "modelo do Whisper", en: "Whisper model" },
  audio_max_seconds: { pt: "duração máxima do áudio", en: "maximum audio duration" },
  multimodal_provider: { pt: "provedor multimodal", en: "multimodal provider" },
  multimodal_api_key: { pt: "API Key multimodal", en: "multimodal API key" },
  multimodal_api_url: { pt: "URL da API multimodal", en: "multimodal API URL" },
  multimodal_model_name: { pt: "modelo multimodal", en: "multimodal model" },
  multimodal_text_fallback_enabled: {
    pt: "fallback textual multimodal",
    en: "multimodal text fallback",
  },
  tts_provider: { pt: "provedor de voz", en: "voice provider" },
  tts_api_key: { pt: "API Key de voz", en: "voice API key" },
  tts_api_url: { pt: "URL da API de voz", en: "voice API URL" },
  tts_model_name: { pt: "modelo de voz", en: "voice model" },
  tts_voice_id: { pt: "voz", en: "voice" },
  tts_enabled: { pt: "respostas em áudio", en: "audio replies" },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeConfigFieldLabel(field: unknown, locale: ConfigSaveErrorLocale): string | null {
  if (typeof field !== "string" || !/^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(field)) return null;
  return CONFIG_SAVE_FIELD_LABELS[field]?.[locale] ?? field.replaceAll("_", " ");
}

function configSaveErrorFieldNames(detail: unknown): string[] {
  const issues = Array.isArray(detail)
    ? detail
    : isRecord(detail) && Array.isArray(detail.fields)
      ? detail.fields
      : isRecord(detail) && typeof detail.field === "string"
        ? [detail.field]
        : [];
  return [...new Set(issues.flatMap((issue) => {
    if (typeof issue === "string") return [issue];
    if (!isRecord(issue)) return [];
    if (typeof issue.field === "string") return [issue.field];
    if (Array.isArray(issue.loc)) {
      // Nested pydantic `loc` for `business_hours` can be depth 1, 2, or 3
      // (field-level / cross-field / day-key). Collapse to the top-level
      // field so the banner labels the real config field rather than the
      // raw sub-token ("open"/"mon").
      if (issue.loc[0] === "business_hours") return [issue.loc[0]];
      return [issue.loc.at(-1)];
    }
    return [];
  }).filter((field): field is string => typeof field === "string"))];
}

function configSaveErrorFields(detail: unknown, locale: ConfigSaveErrorLocale): string[] {
  return configSaveErrorFieldNames(detail)
    .map((field) => safeConfigFieldLabel(field, locale))
    .filter((field): field is string => Boolean(field));
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
  const fieldNames = configSaveErrorFieldNames(detail);

  if (errorCode === "service_format_conflict") {
    return txtForLocale(
      locale,
      "A modalidade de um serviço não é compatível com a modalidade geral da clínica. Revise o catálogo.",
      "A service delivery mode is incompatible with the clinic's overall delivery mode. Review the catalog.",
    );
  }

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
    invalid_provider_for_capability: {
      pt: "O provedor selecionado não é válido para esta capacidade de API.",
      en: "The selected provider is not valid for this API capability.",
    },
    invalid_provider_endpoint: {
      pt: "A URL da API não corresponde ao endpoint oficial deste provedor. Use o endpoint oficial indicado no campo URL da API.",
      en: "The API URL does not match this provider's official endpoint. Use the official endpoint shown in the API URL field.",
    },
    provider_switch_bundle_required: {
      pt: "Para trocar o provedor, informe juntos a URL da API, uma nova API Key e o modelo.",
      en: "To switch providers, enter the API URL, a new API key, and the model together.",
    },
    provider_not_available: {
      pt: "Este provedor ainda não está liberado para nova configuração.",
      en: "This provider is not available for new configuration yet.",
    },
    meta_phone_number_id_conflict: {
      pt: "Este Phone Number ID da Meta já está vinculado a outra clínica. Use um número diferente.",
      en: "This Meta Phone Number ID is already linked to another clinic. Use a different number.",
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

  const fields = configSaveErrorFields(detail, locale);
  const capabilityForFields = configSaveCapability(fieldNames);
  const capabilityFromDetail = apiCapabilityId(detailRecord?.capability);
  const capability = capabilityFromDetail ?? capabilityForFields;
  const capabilityPrefix = capability ? `${apiCapability(capability).short}: ` : "";

  if (errorCode === "provider_not_implemented") {
    const provider = typeof detailRecord?.provider === "string" ? detailRecord.provider : null;
    const providerLabel = provider && WA_PROVIDER_LABEL[provider as WhatsappProvider]
      ? WA_PROVIDER_LABEL[provider as WhatsappProvider]
      : txtForLocale(locale, "este provedor", "this provider");
    return locale === "en"
      ? `${capabilityPrefix}${providerLabel} is not implemented yet. Use Meta Cloud API, Z-API, or Evolution API.`
      : `${capabilityPrefix}${providerLabel} ainda não está implementado. Use Meta Cloud API, Z-API ou Evolution API.`;
  }

  if (errorCode === "fresh_api_key_required") {
    const multimodalSuffix = capability === "multimodal"
      ? txtForLocale(locale, " do provedor multimodal", " for the multimodal provider")
      : "";
    return locale === "en"
      ? `${capabilityPrefix}enter a new API key${multimodalSuffix} - switching providers or endpoints requires a new key.`
      : `${capabilityPrefix}informe uma nova API Key${multimodalSuffix} - trocar de provedor ou de endpoint exige uma chave nova.`;
  }

  if (errorCode === "unsafe_api_url" || errorCode === "invalid_credentials_payload") {
    const base = knownErrors[errorCode][locale];
    if (fields.length > 0) {
      const fieldSuffix = locale === "en"
        ? ` Fields: ${fields.join(", ")}.`
        : ` Campos: ${fields.join(", ")}.`;
      return `${capabilityPrefix}${base}${fieldSuffix}`;
    }
    return `${capabilityPrefix}${base}`;
  }

  if (
    errorCode === "invalid_provider_for_capability"
    || errorCode === "invalid_provider_endpoint"
    || errorCode === "provider_switch_bundle_required"
    || errorCode === "meta_phone_number_id_conflict"
  ) {
    return `${capabilityPrefix}${knownErrors[errorCode][locale]}`;
  }

  if (errorCode && knownErrors[errorCode]) return knownErrors[errorCode][locale];

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
  // The backend projects capability readiness and provider labels alongside the
  // documented fields below; `providerCardState` reads those by name.
  [key: string]: unknown;
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
  llm_api_url?:             string | null;
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
  // Fail-closed textual opt-in gate for the multimodal fallback. When true,
  // the primary-LLM failure path sends the prompt and conversation history to
  // the configured multimodal provider. Legacy default is `false`; the
  // backend projects an explicit boolean here even when the raw DB row is
  // absent (see `credential_status()` in `src/services/api_credentials.py`).
  multimodal_text_fallback_enabled?: boolean | null;
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

type LlmProvider = "openai" | "gemini" | "dropout" | "slm_cortex" | "outros";
type WhatsappProvider =
  | "meta_cloud"
  | "dialog360"
  | "twilio"
  | "zapi"
  | "evolution"
  | "uazapi";
type SttProvider =
  | "openai_whisper"
  | "elevenlabs_scribe"
  | "google_stt"
  | "deepgram_stt"
  | "microsoft_stt"
  | "outros";
type TtsProvider = "elevenlabs" | "google" | "openai";
type MultimodalProvider = "openrouter";

// Every canonical multimodal provider id declared in the backend allowlist
// (`PROVIDER_ALLOWLISTS["multimodal"]` in `src/services/api_credentials.py`).
export const MULTIMODAL_PROVIDERS: {
  id: MultimodalProvider; label: string; sub: string;
}[] = [
  { id: "openrouter", label: "OpenRouter", sub: "Gemini / GPT-4o audio" },
];

// Single source of truth for the 5 API capability blocks in the clinic-config
// "API" tab. Each entry drives the block's short header name, its inline
// description paragraph, and the required/optional marker that sits next to
// the header. Any drift from this registry means the UI is showing something
// the product spec never promised.
export type ApiCapabilityId = "whatsapp" | "llm" | "stt" | "multimodal" | "tts";

export type ProviderAvailability = "active" | "coming_soon";

export type ProviderAvailabilityMatrix = {
  readonly [Capability in ApiCapabilityId]: Readonly<Record<string, ProviderAvailability>>;
};

/**
 * Frontend mirror of `PROVIDER_AVAILABILITY` in
 * `src/services/api_credentials.py`.
 *
 * Marking a pair available here without the backend agreeing only produces a
 * card that opens and then fails its PATCH, so the two contracts must move
 * together. Keeping it capability-keyed prevents a provider id being
 * accidentally rendered under a different credential shape.
 */
export const API_PROVIDER_AVAILABILITY = {
  whatsapp: {
    meta_cloud: "coming_soon",
    zapi: "coming_soon",
    uazapi: "coming_soon",
    evolution: "coming_soon",
    dialog360: "coming_soon",
    twilio: "coming_soon",
  },
  llm: {
    dropout: "coming_soon",
    gemini: "active",
    openai: "coming_soon",
    outros: "coming_soon",
    slm_cortex: "coming_soon",
  },
  multimodal: {
    openrouter: "coming_soon",
  },
  stt: {
    outros: "coming_soon",
    openai_whisper: "coming_soon",
    elevenlabs_scribe: "coming_soon",
    google_stt: "coming_soon",
    deepgram_stt: "coming_soon",
    microsoft_stt: "coming_soon",
  },
  tts: {
    elevenlabs: "coming_soon",
    google: "coming_soon",
    openai: "coming_soon",
  },
} as const satisfies ProviderAvailabilityMatrix;

export const API_CAPABILITIES: readonly {
  id: ApiCapabilityId;
  short: string;
  desc: { pt: string; en: string };
  required: boolean;
}[] = [
  {
    id: "whatsapp",
    short: "API WhatsApp",
    desc: {
      pt: "Canal de mensagens (recebe e envia no WhatsApp)",
      en: "Messaging channel (receives and sends on WhatsApp)",
    },
    required: true,
  },
  {
    id: "llm",
    short: "API LLM",
    desc: {
      pt: "Large Language Model (gera as respostas em texto)",
      en: "Large Language Model (generates the text replies)",
    },
    required: true,
  },
  {
    id: "stt",
    short: "API STT",
    desc: {
      pt: "Speech to Text (transcrição de áudio)",
      en: "Speech to Text (audio transcription)",
    },
    required: false,
  },
  {
    id: "multimodal",
    short: "FallBack LLM Multimodal",
    desc: {
      pt: "Fallback multimodal para duas contingências: quando o LLM primário falhar (com prompt e histórico enviados ao provedor multimodal) e quando o STT falhar (o modelo ouve o áudio direto).",
      en: "Multimodal fallback for two contingencies: when the primary LLM fails (prompt and history are sent to the multimodal provider) and when STT fails (the model listens to the audio directly).",
    },
    required: false,
  },
  {
    id: "tts",
    short: "API TTS",
    desc: {
      pt: "Text to Speech (gerar áudio)",
      en: "Text to Speech (generate audio)",
    },
    required: false,
  },
];

function apiCapability(id: ApiCapabilityId) {
  const entry = API_CAPABILITIES.find(c => c.id === id);
  if (!entry) throw new Error(`Unknown API capability: ${id}`);
  return entry;
}

function txtForLocale(locale: ConfigSaveErrorLocale, pt: string, en: string): string {
  return locale === "en" ? en : pt;
}

function apiCapabilityId(value: unknown): ApiCapabilityId | null {
  return typeof value === "string" && API_CAPABILITIES.some(c => c.id === value)
    ? value as ApiCapabilityId
    : null;
}

// Keep the more specific credential prefixes explicit and ordered so a field
// can be mapped to the same capability used by the API tab's shared registry.
const CONFIG_SAVE_CAPABILITY_PREFIXES: readonly (readonly [string, ApiCapabilityId])[] = [
  ["whatsapp_", "whatsapp"],
  ["zapi_", "whatsapp"],
  ["evolution_", "whatsapp"],
  ["meta_", "whatsapp"],
  ["uazapi_", "whatsapp"],
  ["llm_", "llm"],
  ["stt_", "stt"],
  ["whisper_", "stt"],
  ["multimodal_", "multimodal"],
  ["tts_", "tts"],
  ["audio_max_seconds", "stt"],
];

function configSaveCapability(fieldNames: readonly string[]): ApiCapabilityId | null {
  for (const [prefix, capability] of CONFIG_SAVE_CAPABILITY_PREFIXES) {
    if (fieldNames.some(field => field.startsWith(prefix))) return capability;
  }
  return null;
}

export const WA_PROVIDERS: {
  id: WhatsappProvider;
  label: string;
  sub: string;
}[] = [
  // Official WhatsApp Business API providers
  { id: "meta_cloud", label: "Meta Cloud API", sub: "by Meta (direct)" },
  { id: "dialog360", label: "360dialog",      sub: "BSP oficial" },
  { id: "twilio", label: "Twilio",         sub: "BSP oficial" },
  // WhatsApp Web / unofficial providers
  { id: "zapi", label: "Z-API",         sub: "by Z-API.io" },
  { id: "evolution", label: "Evolution API", sub: "Open Source" },
  { id: "uazapi", label: "Uazapi",        sub: "WhatsApp Web" },
];

const WA_PROVIDER_LABEL: Record<WhatsappProvider, string> = WA_PROVIDERS.reduce(
  (acc, wp) => ({ ...acc, [wp.id]: wp.label }),
  {} as Record<WhatsappProvider, string>,
);

// Every canonical STT provider id declared in the backend allowlist
// (`PROVIDER_ALLOWLISTS["stt"]` in `src/services/api_credentials.py`) must be
// represented here. Missing an id would break credential save/load parity.
export const STT_PROVIDERS: { id: SttProvider; label: string; sub: string }[] = [
  { id: "openai_whisper",    label: "Whisper",    sub: "by OpenAI"    },
  { id: "elevenlabs_scribe", label: "Scribe",     sub: "by ElevenLabs" },
  { id: "google_stt",        label: "Cloud STT",  sub: "by Google"          },
  { id: "deepgram_stt",      label: "Deepgram",   sub: "by Deepgram" },
  { id: "microsoft_stt",     label: "Azure STT",  sub: "by Microsoft"       },
  { id: "outros",            label: "Outros",     sub: "Groq / OpenRouter"    },
];

export const TTS_PROVIDERS: { id: TtsProvider; label: string; sub: string }[] = [
  { id: "elevenlabs", label: "ElevenLabs",      sub: "Voz natural e humana" },
  { id: "google",     label: "Google Cloud TTS", sub: "Escala e bom custo"       },
  { id: "openai",     label: "OpenAI TTS",       sub: "gpt-4o-audio / tts-1" },
];

// The display catalog is intentionally availability-agnostic: the canonical
// Gemini-active/provider-coming-soon state lives above.
export const PROVIDERS: {
  id: LlmProvider;
  label: string;
  sub: string;
}[] = [
  { id: "slm_cortex", label: "SLM Cortex", sub: "by NeuroStack" },
  { id: "dropout",    label: "Dropout",    sub: "by Neuralake" },
  { id: "openai",     label: "GPT",        sub: "by OpenAI" },
  { id: "gemini",     label: "Gemini",     sub: "by Google" },
  { id: "outros",     label: "Outros",     sub: "Custom / OSS" },
];

type ProviderDisplay = { id: string; label: string; sub: string };

function providersForCapability(capability: ApiCapabilityId): readonly ProviderDisplay[] {
  switch (capability) {
    case "whatsapp": return WA_PROVIDERS;
    case "llm": return PROVIDERS;
    case "stt": return STT_PROVIDERS;
    case "multimodal": return MULTIMODAL_PROVIDERS;
    case "tts": return TTS_PROVIDERS;
  }
}

type CredentialField = {
  name: string;
  label: { pt: string; en: string };
  secret?: boolean;
  numeric?: boolean;
};

// WhatsApp is the one capability whose credential shape depends on the
// provider; the rest share a single bundle shape per capability.
const WHATSAPP_PROVIDER_FIELDS: Record<string, readonly CredentialField[]> = {
  zapi: [
    { name: "zapi_instance_id", label: { pt: "ID da instância", en: "Instance ID" } },
    { name: "zapi_token", label: { pt: "Token", en: "Token" }, secret: true },
    { name: "zapi_client_token", label: { pt: "Client token", en: "Client token" }, secret: true },
  ],
  evolution: [
    { name: "evolution_api_url", label: { pt: "URL da API", en: "API URL" } },
    { name: "evolution_instance_name", label: { pt: "Nome da instância", en: "Instance name" } },
    { name: "evolution_api_key", label: { pt: "Chave da API", en: "API key" }, secret: true },
  ],
  meta_cloud: [
    { name: "meta_phone_number_id", label: { pt: "Phone number ID", en: "Phone number ID" } },
    { name: "meta_waba_id", label: { pt: "WABA ID", en: "WABA ID" } },
    { name: "meta_permanent_token", label: { pt: "Token permanente", en: "Permanent token" }, secret: true },
    { name: "meta_app_secret", label: { pt: "App secret", en: "App secret" }, secret: true },
  ],
  uazapi: [
    { name: "uazapi_api_url", label: { pt: "URL da API", en: "API URL" } },
    { name: "uazapi_instance_name", label: { pt: "Nome da instância", en: "Instance name" } },
    { name: "uazapi_token", label: { pt: "Token", en: "Token" }, secret: true },
  ],
  dialog360: [],
  twilio: [],
};

// The non-secret field each outbound path needs, mirroring the backend's
// WHATSAPP_OUTBOUND_FIELDS. Emptying it is the only way to retire a bundle,
// because a blanked secret reads as "keep the stored value" on both sides.
const WHATSAPP_DECOMMISSION_LEVER: Record<string, string> = {
  zapi: "zapi_instance_id",
  evolution: "evolution_api_url",
  meta_cloud: "meta_phone_number_id",
  uazapi: "uazapi_api_url",
};

// Voice triggers (`tts_enabled`, `tts_reply_policy`) belong to
// VoiceResponseControls and are deliberately absent here.
const CAPABILITY_FIELDS: Record<string, readonly CredentialField[]> = {
  llm: [
    { name: "llm_api_url", label: { pt: "URL da API", en: "API URL" } },
    { name: "llm_api_key", label: { pt: "Chave da API", en: "API key" }, secret: true },
    { name: "llm_model_name", label: { pt: "Modelo", en: "Model" } },
  ],
  stt: [
    { name: "stt_api_url", label: { pt: "URL da API", en: "API URL" } },
    { name: "stt_api_key", label: { pt: "Chave da API", en: "API key" }, secret: true },
    { name: "stt_model_name", label: { pt: "Modelo", en: "Model" } },
    { name: "audio_max_seconds", label: { pt: "Duração máxima (s)", en: "Max duration (s)" }, numeric: true },
  ],
  multimodal: [
    { name: "multimodal_api_url", label: { pt: "URL da API", en: "API URL" } },
    { name: "multimodal_api_key", label: { pt: "Chave da API", en: "API key" }, secret: true },
    { name: "multimodal_model_name", label: { pt: "Modelo", en: "Model" } },
  ],
  tts: [
    { name: "tts_api_url", label: { pt: "URL da API", en: "API URL" } },
    { name: "tts_api_key", label: { pt: "Chave da API", en: "API key" }, secret: true },
    { name: "tts_model_name", label: { pt: "Modelo", en: "Model" } },
    { name: "tts_voice_id", label: { pt: "ID da voz", en: "Voice ID" } },
  ],
};

const GEMINI_OPENAI_COMPATIBLE_BASE_URL =
  "https://generativelanguage.googleapis.com/v1beta/openai";
const OPENAI_BASE_URL = "https://api.openai.com/v1";

function newProviderFieldDefault(
  capability: ApiCapabilityId,
  providerId: string,
  fieldName: string,
): string {
  if (capability === "llm" && providerId === "gemini" && fieldName === "llm_api_url") {
    return GEMINI_OPENAI_COMPATIBLE_BASE_URL;
  }
  if (capability === "tts" && providerId === "openai" && fieldName === "tts_api_url") {
    return OPENAI_BASE_URL;
  }
  return "";
}

const CAPABILITY_PROVIDER_FIELD: Record<ApiCapabilityId, string> = {
  whatsapp: "whatsapp_provider",
  llm: "llm_provider",
  stt: "stt_provider",
  multimodal: "multimodal_provider",
  tts: "tts_provider",
};

const CAPABILITY_READINESS_FIELD: Record<ApiCapabilityId, string> = {
  whatsapp: "whatsapp_readiness",
  llm: "llm_readiness",
  stt: "stt_readiness",
  multimodal: "multimodal_readiness",
  tts: "tts_readiness",
};

export function credentialFields(
  capability: ApiCapabilityId,
  provider: string,
): readonly CredentialField[] {
  return capability === "whatsapp"
    ? WHATSAPP_PROVIDER_FIELDS[provider] ?? []
    : CAPABILITY_FIELDS[capability];
}

// Keyed reads only, so the looser shape keeps `CredentialStatus` documented
// while still accepting a partial projection from the backend.
type CredentialStatusFields = Readonly<Record<string, unknown>>;

function statusText(status: CredentialStatusFields | null, field: string): string {
  const value = status?.[field];
  return value === null || value === undefined ? "" : String(value);
}

function statusCredentialText(status: CredentialStatusFields | null, field: string): string {
  if (field !== "llm_api_url") return statusText(status, field);
  const direct = status?.llm_api_url;
  return direct === null || direct === undefined
    ? statusText(status, "llm_base_url")
    : String(direct);
}

// Allowlist rather than denylist: `signature_pending` means a Meta bundle is
// missing its app secret, so it must read as partial. Only these two readiness
// values stand for a whole bundle.
const COMPLETE_READINESS = new Set(["active", "configuration_only"]);

/**
 * Availability is authoritative even when a tenant still has legacy
 * credentials stored for the provider. That prevents an existing bundle from
 * bypassing a shutdown decision and reopening its editor. For an active pair,
 * the persisted readiness still distinguishes configured from incomplete.
 */
export function providerCardState(
  capability: ApiCapabilityId,
  providerId: string,
  status: CredentialStatusFields | null,
  providerAvailability: ProviderAvailabilityMatrix = API_PROVIDER_AVAILABILITY,
): ProviderCardState {
  const availability: Readonly<Record<string, ProviderAvailability>> =
    providerAvailability[capability];
  if (availability[providerId] !== "active") return "coming_soon";

  const persisted = statusText(status, CAPABILITY_PROVIDER_FIELD[capability]);
  if (persisted && persisted === providerId) {
    return COMPLETE_READINESS.has(statusText(status, CAPABILITY_READINESS_FIELD[capability]))
      ? "configured"
      : "incomplete";
  }
  return "available";
}

export function ApiCredentialsForm({
  clinicId,
  isReadOnly,
  providerAvailability = API_PROVIDER_AVAILABILITY,
}: {
  clinicId: string;
  isReadOnly: boolean;
  isSuperAdmin: boolean;
  providerAvailability?: ProviderAvailabilityMatrix;
}) {
  const { locale } = useI18n();
  const { toast } = useToast();
  const txt = (pt: string, en: string) => (locale === "en" ? en : pt);
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [baseline, setBaseline] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const clinicGenerationRef = useRef({ clinicId, generation: 0 });
  if (clinicGenerationRef.current.clinicId !== clinicId) {
    clinicGenerationRef.current = {
      clinicId,
      generation: clinicGenerationRef.current.generation + 1,
    };
  }

  const badgeLabel: Record<ProviderCardState, string> = {
    configured: txt("CONFIGURADO", "CONFIGURED"),
    incomplete: txt("INCOMPLETO", "INCOMPLETE"),
    available: txt("DISPONÍVEL", "AVAILABLE"),
    coming_soon: txt("EM BREVE", "COMING SOON"),
  };

  const credentialsUrl = `/v1/config/api-credentials?tenant_id=${encodeURIComponent(clinicId)}`;

  const loadStatus = useCallback(async () => {
    const requestGeneration = clinicGenerationRef.current.generation;
    const isCurrentClinic = () => clinicGenerationRef.current.generation === requestGeneration;
    // A builder switching clinics changes `clinicId` without remounting, so the
    // previous tenant's open editor and draft must be dropped here. Clearing
    // status too keeps every card inert while the new tenant's state loads.
    setExpanded(null);
    setDraft({});
    setBaseline({});
    setStatus(null);
    setSaving(false);
    if (!clinicId) { setLoadFailed(true); return; }
    try {
      const token = getToken();
      const res = await fetch(credentialsUrl, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!isCurrentClinic()) return;
      if (!res.ok) throw new Error(`Credential status fetch failed: ${res.status}`);
      const nextStatus = await res.json() as CredentialStatus;
      if (!isCurrentClinic()) return;
      setStatus(nextStatus);
      setLoadFailed(false);
    } catch {
      if (!isCurrentClinic()) return;
      setStatus(null);
      setLoadFailed(true);
    }
  }, [clinicId, credentialsUrl]);

  useEffect(() => { void loadStatus(); }, [loadStatus]);

  function toggleProvider(capability: ApiCapabilityId, providerId: string) {
    const key = `${capability}:${providerId}`;
    if (expanded === key) { setExpanded(null); return; }
    // Secrets never pre-fill: an empty input means "keep the stored value",
    // which is also how the backend reads an omitted secret field. Non-secrets
    // only pre-fill for the provider actually held, because the field names are
    // flat and shared — hydrating another provider's card would present one
    // provider's endpoint and model as if they belonged to the other.
    const held =
      statusText(status, CAPABILITY_PROVIDER_FIELD[capability]) === providerId;
    const hydrated: Record<string, string> = {};
    for (const field of credentialFields(capability, providerId)) {
      hydrated[field.name] =
        field.secret
          ? ""
          : held
            ? statusCredentialText(status, field.name)
            : newProviderFieldDefault(capability, providerId, field.name);
    }
    setBaseline(hydrated);
    setDraft(hydrated);
    setExpanded(key);
  }

  async function saveProvider(capability: ApiCapabilityId, providerId: string) {
    const requestGeneration = clinicGenerationRef.current.generation;
    const isCurrentClinic = () => clinicGenerationRef.current.generation === requestGeneration;
    const body: Record<string, string | number> = {};
    // Adoption only takes effect if the provider field travels with the
    // bundle; omitting it would persist credentials under the previous
    // provider's label and leave the card unable to reach CONFIGURADO.
    const providerField = CAPABILITY_PROVIDER_FIELD[capability];
    const held = statusText(status, providerField);
    if (held !== providerId) {
      body[providerField] = providerId;
      // The retired bundle stays populated and goes on resolving its own
      // inbound webhooks, so it would keep replying under the new provider's
      // name. Emptying its identity field here retires it while the switch is
      // still expressible; afterwards its card reads empty and there is
      // nothing left to blank.
      const lever =
        capability === "whatsapp" ? WHATSAPP_DECOMMISSION_LEVER[held] : undefined;
      if (lever) body[lever] = "";
    }
    for (const field of credentialFields(capability, providerId)) {
      const value = draft[field.name] ?? "";
      if (field.secret) {
        if (value) body[field.name] = value;
      } else if (held !== providerId || value !== baseline[field.name]) {
        body[field.name] = field.numeric ? Number(value) : value;
      }
    }
    if (Object.keys(body).length === 0) { setExpanded(null); return; }

    setSaving(true);
    try {
      const token = getToken();
      const res = await fetch(credentialsUrl, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body),
      });
      if (!isCurrentClinic()) return;
      if (!res.ok) {
        const payload: unknown = await res.json().catch(() => null);
        if (!isCurrentClinic()) return;
        toast({
          variant: "destructive",
          title: txt("Não foi possível salvar", "Could not save"),
          description: describeConfigSaveFailure(payload, res.status, locale),
        });
        return;
      }
      const nextStatus = await res.json() as CredentialStatus;
      if (!isCurrentClinic()) return;
      setStatus(nextStatus);
      setExpanded(null);
      toast({ title: txt("Credenciais salvas", "Credentials saved") });
    } catch {
      if (!isCurrentClinic()) return;
      toast({
        variant: "destructive",
        title: txt("Não foi possível salvar", "Could not save"),
        description: txt("Tente novamente.", "Please try again."),
      });
    } finally {
      if (isCurrentClinic()) setSaving(false);
    }
  }

  return (
    <section className="space-y-5" data-testid="api-settings-section">
      <p className="text-sm text-muted-foreground" data-testid="api-settings-legacy-state-note">
        {txt(
          "Gemini está disponível; os demais provedores permanecem EM BREVE.",
          "Gemini is available; the other providers remain COMING SOON.",
        )}
      </p>
      {loadFailed && (
        <p className="text-xs text-destructive" data-testid="api-settings-load-error">
          {txt(
            "Não foi possível carregar o estado das credenciais. Recarregue a página.",
            "Credential status could not be loaded. Try reloading the page.",
          )}
        </p>
      )}

      {API_CAPABILITIES.map((capability) => {
        const providers = providersForCapability(capability.id);
        const availability = providerAvailability[capability.id];

        return (
          <div
            key={capability.id}
            className="space-y-3 rounded-lg border border-slate-700/50 bg-muted/20 p-4"
            data-testid={`api-capability-${capability.id}`}
          >
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                {capability.short}
              </p>
              <span
                data-testid={`capability-requirement-${capability.id}`}
                className="text-[9px] font-bold uppercase tracking-wider text-muted-foreground/80"
              >
                {capability.required ? txt("obrigatório", "Required") : txt("opcional", "Optional")}
              </span>
            </div>
            <p className="text-[11px] text-muted-foreground">
              {locale === "en" ? capability.desc.en : capability.desc.pt}
            </p>
            <div
              className="grid grid-cols-2 gap-2 sm:grid-cols-3"
              data-testid={`${capability.id}-provider-list`}
            >
              {providers.map((provider) => {
                if (!(provider.id in availability)) return null;
                // Until the tenant's status lands, which provider is held is
                // unknown, so every card stays inert rather than offering an
                // adoption that could silently displace a stored provider.
                const state = status === null
                  ? "coming_soon"
                  : providerCardState(
                      capability.id,
                      provider.id,
                      status,
                      providerAvailability,
                    );
                const key = `${capability.id}:${provider.id}`;
                return (
                  <ProviderCredentialCard
                    key={provider.id}
                    id={`${capability.id}-${provider.id}`}
                    label={provider.label}
                    sub={provider.sub}
                    state={state}
                    badgeLabel={badgeLabel[state]}
                    isExpanded={expanded === key}
                    onToggle={state === "coming_soon" || isReadOnly
                      ? undefined
                      : () => toggleProvider(capability.id, provider.id)}
                  />
                );
              })}
            </div>

            {providers.map((provider) => {
              if (expanded !== `${capability.id}:${provider.id}`) return null;
              const fields = credentialFields(capability.id, provider.id);
              return (
                <div
                  key={`${provider.id}-editor`}
                  className="space-y-3 rounded-md border border-border bg-background p-3"
                  data-testid={`provider-editor-${capability.id}-${provider.id}`}
                >
                  {fields.length === 0 ? (
                    <p className="text-[11px] text-muted-foreground">
                      {txt(
                        "Este provedor não tem campos editáveis nesta versão.",
                        "This provider has no editable fields in this release.",
                      )}
                    </p>
                  ) : (
                    <>
                      <div className="grid gap-3 sm:grid-cols-2">
                        {fields.map((field) => (
                          <div key={field.name} className="space-y-1">
                            <Label
                              htmlFor={`field-${field.name}`}
                              className="text-xs font-medium text-muted-foreground"
                            >
                              {locale === "en" ? field.label.en : field.label.pt}
                            </Label>
                            <Input
                              id={`field-${field.name}`}
                              data-testid={`field-${field.name}`}
                              type={field.secret ? "password" : field.numeric ? "number" : "text"}
                              value={draft[field.name] ?? ""}
                              placeholder={field.secret
                                && statusText(status, CAPABILITY_PROVIDER_FIELD[capability.id]) === provider.id
                                && status?.[`${field.name}_set`] === true
                                ? txt("•••••••• (manter atual)", "•••••••• (keep current)")
                                : undefined}
                              onChange={(e) => setDraft(
                                (prev) => ({ ...prev, [field.name]: e.target.value }),
                              )}
                              className="text-xs"
                            />
                          </div>
                        ))}
                      </div>
                      <Button
                        type="button"
                        size="sm"
                        disabled={saving}
                        data-testid={`save-${capability.id}-${provider.id}`}
                        onClick={() => void saveProvider(capability.id, provider.id)}
                      >
                        {saving
                          ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          : <Save className="h-3.5 w-3.5" />}
                        <span className="ml-1.5">{txt("Salvar", "Save")}</span>
                      </Button>
                    </>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
    </section>
  );
}

// ── Small layout helpers ──────────────────────────────────────────────────────

const BUSINESS_DAY_SET = new Set<BusinessDayCode>(BUSINESS_DAY_ORDER);
const HHMM_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

function isBusinessDayCode(value: unknown): value is BusinessDayCode {
  return typeof value === "string" && BUSINESS_DAY_SET.has(value as BusinessDayCode);
}

function sanitizeBusinessHoursForHydration(raw: unknown): BusinessHoursMap | null {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const source = raw as Record<string, unknown>;
  const next: BusinessHoursMap = {};
  for (const [day, window] of Object.entries(source)) {
    if (!isBusinessDayCode(day)) continue;
    if (!window || typeof window !== "object" || Array.isArray(window)) continue;
    const w = window as Record<string, unknown>;
    const open = typeof w.open === "string" ? w.open : "";
    const close = typeof w.close === "string" ? w.close : "";
    next[day] = { open, close };
  }
  return Object.keys(next).length === 0 ? null : next;
}

function hhmmToMinutes(value: string): number {
  const [h, m] = value.split(":");
  return Number(h) * 60 + Number(m);
}

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
  scheduling_hours: "Seg–Sex: 8h–18h",
  timezone: "America/Sao_Paulo",
  business_hours: {
    mon: { open: "09:00", close: "19:00" },
    tue: { open: "09:00", close: "19:00" },
    wed: { open: "09:00", close: "19:00" },
    thu: { open: "09:00", close: "19:00" },
    fri: { open: "09:00", close: "19:00" },
    sat: { open: "09:00", close: "14:00" },
  },
  service_format: "both",
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
    { name: "Toxina Botulínica (Botox)", description: "Aplicação para rugas dinâmicas na testa, glabela e região dos olhos", price: "R$ 1.200", authority_notes: "Procedimento realizado por Dra. Ana Lima, dermatologista com mais de 500 aplicações bem-sucedidas", service_format: "presential" },
    { name: "Preenchimento Labial", description: "Ácido hialurônico para volume e contorno labial", price: "R$ 2.500", authority_notes: "Técnica avançada com produto Juvederm, referência mundial em preenchimento", service_format: "presential" },
    { name: "Limpeza de Pele Profunda", description: "Extração, peeling enzimático, máscara calmante e proteção solar", price: "R$ 350", authority_notes: "Protocolo exclusivo desenvolvido pela equipe de estética da clínica", service_format: "both" },
    { name: "Consulta de Avaliação", description: "Avaliação inicial para indicar o cuidado mais adequado", price: "R$ 250", authority_notes: "Orientação personalizada pela equipe clínica", service_format: "online" },
  ],
};

export function HitlComingSoonControl() {
  const { locale } = useI18n();
  const modeTitle = locale === "en"
    ? "Co-pilot mode - COMING SOON"
    : "Modo co-piloto - EM BREVE";

  return (
    <div
      className="flex items-center justify-between gap-3 rounded-lg border border-border/60 bg-muted/20 px-4 py-4"
      data-testid="hitl-coming-soon"
    >
      <div className="space-y-1">
        <p className="text-sm font-semibold text-foreground">
          Human-in-the-loop (HITL)
        </p>
        <p className="text-xs text-muted-foreground">
          {modeTitle}
        </p>
      </div>
      <Switch
        aria-label={modeTitle}
        checked={false}
        disabled
        className="shrink-0"
      />
    </div>
  );
}

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
  const [schedulingHours, setSchedulingHours] = useState("");
  const [schedulingLinkEnabled, setSchedulingLinkEnabled] = useState(false);
  const [schedulingLinkUrl, setSchedulingLinkUrl] = useState("");
  const [schedulingLinkMessage, setSchedulingLinkMessage] = useState("");
  const [schedulingLinkErrors, setSchedulingLinkErrors] = useState<Record<string, string>>({});
  const [serviceFormat, setServiceFormat] = useState("");
  const [timezone, setTimezone] = useState("America/Sao_Paulo");
  const [businessHours, setBusinessHours] = useState<BusinessHoursMap | null>(null);
  const [businessHoursErrors, setBusinessHoursErrors] = useState<Record<string, string>>({});
  const [metaMinutosAtendimento, setMetaMinutosAtendimento] = useState("10");

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

  // Tab 5 — Catálogo de Serviços
  const [services, setServices] = useState<ClinicService[]>([]);
  const [serviceFormatErrors, setServiceFormatErrors] = useState<Record<number, string>>({});


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
    setBusinessHours(sanitizeBusinessHoursForHydration(config.business_hours));
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
    setServices(Array.isArray(config.services) ? config.services.map(hydrateClinicService) : []);
    setServiceFormatErrors({});
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
    setServices((prev) => [...prev, { name: "", description: "", price: "", authority_notes: "", service_format: null }]);
  };
  const removeService = (idx: number) => {
    setServices((prev) => prev.filter((_, i) => i !== idx));
    setServiceFormatErrors({});
  };
  const updateService = (idx: number, field: keyof ClinicService, value: string | ServiceFormat | null) => {
    setServices((prev) => prev.map((s, i) => (i === idx ? { ...s, [field]: value } : s)));
    setServiceFormatErrors((prev) => {
      const { [idx]: _resolved, ...remaining } = prev;
      return remaining;
    });
  };

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
    if (businessHours) {
      for (const day of BUSINESS_DAY_ORDER) {
        const window = businessHours[day];
        if (!window) continue;
        if (!window.open || !HHMM_PATTERN.test(window.open)) {
          businessHoursValidationErrors[day] = txt(
            "Informe a hora de abertura em formato HH:MM.",
            "Provide the opening time in HH:MM format.",
          );
          continue;
        }
        if (!window.close || !HHMM_PATTERN.test(window.close)) {
          businessHoursValidationErrors[day] = txt(
            "Informe a hora de fechamento em formato HH:MM.",
            "Provide the closing time in HH:MM format.",
          );
          continue;
        }
        if (hhmmToMinutes(window.close) <= hhmmToMinutes(window.open)) {
          businessHoursValidationErrors[day] = txt(
            "O fechamento deve ser posterior à abertura.",
            "Closing time must be later than opening time.",
          );
        }
      }
    }
    if (Object.keys(businessHoursValidationErrors).length > 0) {
      setBusinessHoursErrors(businessHoursValidationErrors);
      toast({
        title: txt("Revise o horário de atendimento.", "Review the business hours."),
        description: txt(
          "Cada dia aberto precisa de abertura e fechamento válidos; o fechamento deve ser posterior à abertura.",
          "Every open day needs valid opening and closing times; closing must be later than opening.",
        ),
        variant: "destructive",
      });
      return;
    }
    setBusinessHoursErrors({});

    const serviceValidationErrors: Record<number, string> = {};
    for (const [index, service] of services.entries()) {
      if (!service.name.trim()) continue;
      if (!isServiceFormat(service.service_format)) {
        serviceValidationErrors[index] = txt(
          "Confirme a modalidade deste serviço antes de salvar.",
          "Confirm this service's delivery mode before saving.",
        );
      } else if (isServiceFormat(serviceFormat) && serviceFormat !== "both" && service.service_format !== serviceFormat) {
        serviceValidationErrors[index] = txt(
          "A modalidade deste serviço não é compatível com a modalidade geral da clínica.",
          "This service's delivery mode is incompatible with the clinic's overall delivery mode.",
        );
      }
    }
    if (Object.keys(serviceValidationErrors).length > 0) {
      setServiceFormatErrors(serviceValidationErrors);
      toast({
        title: txt("Revise as modalidades dos serviços.", "Review service delivery modes."),
        description: txt(
          "Confirme a modalidade de cada serviço e verifique a modalidade geral da clínica.",
          "Confirm each service's delivery mode and check the clinic's overall delivery mode.",
        ),
        variant: "destructive",
      });
      return;
    }
    setServiceFormatErrors({});

    setIsSaving(true);
    const payload: Record<string, any> = {
      clinic_name: clinicName.trim(),
      agent_name: agentName.trim(),
      differentials: differentials.trim(),
      services: services.filter((s) => s.name.trim()).map(serializeClinicService),
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
      scheduling_hours: schedulingHours.trim() || null,
      scheduling_link_enabled: schedulingLinkEnabled,
      scheduling_link_url: schedulingLinkEnabled ? (trimmedSchedulingLinkUrl || null) : null,
      scheduling_link_message: schedulingLinkEnabled ? (schedulingLinkMessage.trim() || null) : null,
      service_format: serviceFormat || null,
      timezone: timezone,
      business_hours: businessHours,
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
              <Field
                label={txt("Horário de Funcionamento", "Business Hours")}
                hint={txt(
                  "Marque cada dia em que a clínica atende e informe abertura e fechamento.",
                  "Toggle every day the clinic is open and set opening and closing times.",
                )}
              >
                <BusinessHoursEditor
                  value={businessHours}
                  onChange={(next) => {
                    setBusinessHours(next);
                    setBusinessHoursErrors({});
                  }}
                  errors={businessHoursErrors}
                  disabled={isReadOnly}
                  locale={locale === "en" ? "en" : "pt"}
                />
              </Field>
              <Field label={txt("Horário do Setor de Agendamento", "Scheduling Department Hours")} hint={txt("Quando difere do horário geral de atendimento.", "When it differs from the general working hours.")}>
                <CompactHoursCard
                  summary={schedulingHours.trim() || txt("Mesmo horário de funcionamento", "Same as business hours")}
                  editLabel={txt("Editar", "Edit")}
                  closeLabel={txt("Concluir", "Done")}
                  testId="scheduling-hours-editor"
                >
                  <Input
                    value={schedulingHours}
                    onChange={(e) => setSchedulingHours(e.target.value)}
                    placeholder={txt("Ex: Seg–Sex: 8h–18h (agendamentos apenas por WhatsApp)", "e.g. Mon–Fri: 8am–6pm (scheduling via WhatsApp only)")}
                    aria-label={txt("Horário do Setor de Agendamento", "Scheduling Department Hours")}
                    disabled={isReadOnly}
                  />
                </CompactHoursCard>
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
                  data-testid="input-meta-minutos-atendimento"
                />
              </Field>

              {/* Scheduling link (stopgap pré-ERP) */}
              <SwitchRow
                label={txt("Link de Agendamento", "Scheduling Link")}
                hint={txt(
                  "Quando ativo, a IA envia o link (definido abaixo) ao paciente após ele confirmar a solicitação de agendamento e cria um handoff pendente de verificação.",
                  "When enabled, the AI sends the link (defined below) after the patient confirms the scheduling request and creates a pending-verification handoff.",
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
                  <HitlComingSoonControl />

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

                  <Field label={txt("Modalidade", "Delivery mode")}>
                    <Select
                      value={service.service_format ?? ""}
                      onValueChange={(value) => updateService(idx, "service_format", value as ServiceFormat)}
                      disabled={isReadOnly}
                    >
                      <SelectTrigger
                        className="h-8 text-sm"
                        data-testid={`select-service-format-${idx}`}
                      >
                        <SelectValue placeholder={txt("Confirme a modalidade", "Confirm delivery mode")} />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="online">{txt("Online", "Online")}</SelectItem>
                        <SelectItem value="presential">{txt("Presencial", "In person")}</SelectItem>
                        <SelectItem value="both">{txt("Online e Presencial", "Online and In person")}</SelectItem>
                      </SelectContent>
                    </Select>
                    {serviceFormatErrors[idx] && (
                      <p className="text-xs text-destructive" data-testid={`service-format-error-${idx}`}>
                        {serviceFormatErrors[idx]}
                      </p>
                    )}
                  </Field>

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
            <CardContent className="space-y-5 pt-5">
              <VoiceResponseControls
                clinicId={(isBuilder || isSuperAdmin) && viewingClinicId ? viewingClinicId : clinicId}
                isReadOnly={isReadOnly}
                role={role}
              />
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
