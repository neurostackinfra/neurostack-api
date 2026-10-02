import { useEffect, useRef, useState } from "react";

import { Loader2, Volume2 } from "lucide-react";

import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/context/i18n";

type VoiceReplyPolicy = "audio_input" | "all_inputs";

interface VoiceCredentialsStatus {
  tts_enabled?: boolean | null;
  tts_readiness?: string | null;
  tts_reply_policy?: VoiceReplyPolicy | null;
}

interface VoiceResponseControlsProps {
  clinicId: string;
  isReadOnly: boolean;
  role: string;
}

function getToken(): string {
  try {
    const auth = JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string };
    return auth.token ?? "";
  } catch {
    return "";
  }
}

function resolvePolicy(value: unknown): VoiceReplyPolicy {
  return value === "all_inputs" ? "all_inputs" : "audio_input";
}

export function VoiceResponseControls({
  clinicId,
  isReadOnly,
  role,
}: VoiceResponseControlsProps) {
  const { locale } = useI18n();
  const canEdit = role === "builder" || role === "super_admin";
  const shouldLoad = canEdit && !isReadOnly;
  const [isLoading, setIsLoading] = useState(shouldLoad);
  const [isPending, setIsPending] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [ttsEnabled, setTtsEnabled] = useState(false);
  const [policy, setPolicy] = useState<VoiceReplyPolicy>("audio_input");
  const [loadFailed, setLoadFailed] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);

  const copy = locale === "en"
    ? {
        title: "Voice Triggers",
        intro: "Choose when configured voice generation may answer a question.",
        masterLabel: "Reply with audio",
        masterDescription: "Pause audio replies without removing the saved voice configuration.",
        policyLabel: "When to send audio",
        audioInputLabel: "Only when the question is audio",
        audioInputDescription: "Text questions keep their text response and avoid voice generation cost.",
        allInputsLabel: "For every type of question",
        allInputsDescription: "Send audio for any supported question type.",
        loading: "Loading voice settings…",
        incomplete: "Complete the saved voice provider setup to enable these controls.",
        readOnly: "Read-only preview. This does not represent persisted clinic settings.",
        loadError: "Voice settings could not be loaded. Try reloading the page.",
        saveError: "The voice setting could not be saved. The previous value was restored.",
        comingSoon: "More voice triggers coming soon",
      }
    : {
        title: "Gatilhos de voz",
        intro: "Escolha quando a geração de voz configurada pode responder a uma pergunta.",
        masterLabel: "Responder em áudio",
        masterDescription: "Pause respostas em áudio sem remover a configuração de voz salva.",
        policyLabel: "Quando enviar áudio",
        audioInputLabel: "Apenas quando receber áudio como pergunta",
        audioInputDescription: "Perguntas em texto mantêm a resposta em texto e evitam custo de geração de voz.",
        allInputsLabel: "Em todos os tipos de pergunta",
        allInputsDescription: "Envie áudio para qualquer tipo de pergunta já suportado.",
        loading: "Carregando configurações de voz…",
        incomplete: "Complete a configuração salva do provedor de voz para habilitar estes controles.",
        readOnly: "Prévia somente leitura. Ela não representa as configurações persistidas da clínica.",
        loadError: "Não foi possível carregar as configurações de voz. Recarregue a página.",
        saveError: "Não foi possível salvar a configuração de voz. O valor anterior foi restaurado.",
        comingSoon: "Outros gatilhos em breve",
      };

  const credentialsUrl = `/v1/config/api-credentials?tenant_id=${encodeURIComponent(clinicId)}`;
  const requestScope = `${shouldLoad ? "persisted" : "static"}:${credentialsUrl}`;
  const requestGenerationRef = useRef(0);
  const activeScopeRef = useRef(requestScope);

  // Invalidate A synchronously while rendering B. The effect resets the
  // visible state after commit, while this ref boundary prevents an already
  // resolved A request from painting into B in the gap before that effect.
  if (activeScopeRef.current !== requestScope) {
    activeScopeRef.current = requestScope;
    requestGenerationRef.current += 1;
  }

  useEffect(() => {
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    const isCurrent = () => requestGenerationRef.current === generation;
    const invalidate = () => {
      if (isCurrent()) requestGenerationRef.current += 1;
    };

    setIsPending(false);
    setIsReady(false);
    setTtsEnabled(false);
    setPolicy("audio_input");
    setLoadFailed(false);
    setSaveFailed(false);
    setLoadedScope(null);

    if (!shouldLoad) {
      setIsLoading(false);
      return invalidate;
    }
    setIsLoading(true);
    if (!clinicId) {
      setIsLoading(false);
      setLoadFailed(true);
      return invalidate;
    }

    async function loadStatus() {
      try {
        const token = getToken();
        const response = await fetch(credentialsUrl, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!response.ok) throw new Error(`Voice settings fetch failed: ${response.status}`);
        const status = await response.json() as VoiceCredentialsStatus;
        if (!isCurrent()) return;
        setTtsEnabled(status.tts_enabled === true);
        setPolicy(resolvePolicy(status.tts_reply_policy));
        setIsReady(status.tts_readiness === "active");
        setLoadedScope(requestScope);
      } catch {
        if (isCurrent()) {
          setIsReady(false);
          setLoadFailed(true);
        }
      } finally {
        if (isCurrent()) setIsLoading(false);
      }
    }
    void loadStatus();
    return invalidate;
  }, [clinicId, credentialsUrl, requestScope, shouldLoad]);

  if (!canEdit && !isReadOnly) return null;

  const controlsDisabled = (
    isReadOnly
    || isLoading
    || isPending
    || !isReady
    || loadedScope !== requestScope
  );

  async function patchVoiceSetting(
    field: "tts_enabled" | "tts_reply_policy",
    value: boolean | VoiceReplyPolicy,
  ) {
    if (controlsDisabled) return;

    const generation = requestGenerationRef.current;
    const isCurrent = () => requestGenerationRef.current === generation;
    const previousEnabled = ttsEnabled;
    const previousPolicy = policy;
    setSaveFailed(false);
    setIsPending(true);
    if (field === "tts_enabled") setTtsEnabled(value as boolean);
    else setPolicy(value as VoiceReplyPolicy);

    try {
      const token = getToken();
      const response = await fetch(credentialsUrl, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ [field]: value }),
      });
      if (!isCurrent()) return;
      if (!response.ok) throw new Error(`Voice settings patch failed: ${response.status}`);

      const status = await response.json() as VoiceCredentialsStatus;
      if (!isCurrent()) return;
      if (field === "tts_enabled" && typeof status.tts_enabled === "boolean") {
        setTtsEnabled(status.tts_enabled);
      }
      if (field === "tts_reply_policy" && status.tts_reply_policy) {
        setPolicy(resolvePolicy(status.tts_reply_policy));
      }
    } catch {
      if (!isCurrent()) return;
      setTtsEnabled(previousEnabled);
      setPolicy(previousPolicy);
      setSaveFailed(true);
    } finally {
      if (isCurrent()) setIsPending(false);
    }
  }

  return (
    <section
      aria-labelledby="voice-response-controls-title"
      className="space-y-4 rounded-lg border border-slate-700/50 bg-muted/20 p-4"
      data-testid="voice-response-controls"
    >
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-accent/20 bg-accent/10">
          <Volume2 aria-hidden="true" className="h-4 w-4 text-accent" />
        </div>
        <div>
          <h3 id="voice-response-controls-title" className="text-sm font-semibold text-foreground">
            {copy.title}
          </h3>
          <p className="text-xs text-muted-foreground">{copy.intro}</p>
        </div>
      </div>

      <div className="flex items-center justify-between gap-4 rounded-md border border-border bg-background/50 px-3 py-3">
        <div className="min-w-0">
          <Label htmlFor="voice-response-enabled" className="text-sm font-medium text-foreground">
            {copy.masterLabel}
          </Label>
          <p id="voice-response-enabled-description" className="mt-0.5 text-xs text-muted-foreground">
            {copy.masterDescription}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {isPending && <Loader2 aria-hidden="true" className="h-4 w-4 animate-spin text-muted-foreground" />}
          <Switch
            id="voice-response-enabled"
            aria-describedby="voice-response-enabled-description"
            aria-label={copy.masterLabel}
            checked={ttsEnabled}
            disabled={controlsDisabled}
            onCheckedChange={(checked) => void patchVoiceSetting("tts_enabled", checked)}
          />
        </div>
      </div>

      <div className="space-y-2">
        <p id="voice-reply-policy-label" className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {copy.policyLabel}
        </p>
        <RadioGroup
          aria-labelledby="voice-reply-policy-label"
          disabled={controlsDisabled}
          onValueChange={(value) => void patchVoiceSetting("tts_reply_policy", resolvePolicy(value))}
          value={policy}
        >
          <div className="flex items-start gap-3 rounded-md border border-border bg-background/50 px-3 py-3">
            <RadioGroupItem
              id="voice-policy-audio-input"
              aria-describedby="voice-policy-audio-input-description"
              value="audio_input"
            />
            <div className="space-y-0.5">
              <Label htmlFor="voice-policy-audio-input" className="text-sm font-medium text-foreground">
                {copy.audioInputLabel}
              </Label>
              <p id="voice-policy-audio-input-description" className="text-xs text-muted-foreground">
                {copy.audioInputDescription}
              </p>
            </div>
          </div>
          <div className="flex items-start gap-3 rounded-md border border-border bg-background/50 px-3 py-3">
            <RadioGroupItem
              id="voice-policy-all-inputs"
              aria-describedby="voice-policy-all-inputs-description"
              value="all_inputs"
            />
            <div className="space-y-0.5">
              <Label htmlFor="voice-policy-all-inputs" className="text-sm font-medium text-foreground">
                {copy.allInputsLabel}
              </Label>
              <p id="voice-policy-all-inputs-description" className="text-xs text-muted-foreground">
                {copy.allInputsDescription}
              </p>
            </div>
          </div>
        </RadioGroup>
      </div>

      {isLoading && <p role="status" className="text-xs text-muted-foreground">{copy.loading}</p>}
      {!isLoading && isReadOnly && <p className="text-xs text-amber-400">{copy.readOnly}</p>}
      {!isLoading && !isReadOnly && !loadFailed && !isReady && (
        <p className="text-xs text-amber-400">{copy.incomplete}</p>
      )}
      {loadFailed && <p role="alert" className="text-xs text-destructive">{copy.loadError}</p>}
      {saveFailed && <p role="alert" className="text-xs text-destructive">{copy.saveError}</p>}

      <p className="text-xs font-medium text-muted-foreground">{copy.comingSoon}</p>
    </section>
  );
}
