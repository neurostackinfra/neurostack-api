import {
  Mic,
  KeyRound,
  FlaskConical,
  AudioLines,
  Settings2,
  Info,
  Ear,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useI18n } from "@/context/i18n";

function SectionDivider({ num, label }: { num: string; label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-rose-500/20 shrink-0">
        <span className="text-[9px] font-bold text-rose-500">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-rose-500">{label}</span>
      <div className="flex-1 h-px bg-rose-500/20" />
    </div>
  );
}

type CostLevel = "Alto" | "Médio" | "Baixo" | "Zero";

function TriggerOption({
  letter,
  label,
  detail,
  cost,
}: {
  letter: string;
  label: string;
  detail: string;
  cost: CostLevel;
}) {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  const costColor: Record<CostLevel, string> = {
    Alto:  "bg-rose-500/10 border-rose-500/25 text-rose-600 dark:text-rose-400",
    Médio: "bg-amber-500/10 border-amber-500/25 text-amber-600 dark:text-amber-400",
    Baixo: "bg-emerald-500/10 border-emerald-500/25 text-emerald-600 dark:text-emerald-400",
    Zero:  "bg-slate-500/10 border-slate-500/25 text-foreground/50",
  };

  const costLabel: Record<CostLevel, string> = {
    Alto:  txt("Alto", "High"),
    Médio: txt("Médio", "Medium"),
    Baixo: txt("Baixo", "Low"),
    Zero:  "Zero",
  };

  return (
    <div className="flex items-start gap-3 rounded-lg border border-border/50 bg-muted/15 px-4 py-3 opacity-70 cursor-not-allowed">
      <div className="flex h-5 w-5 items-center justify-center rounded-full border border-rose-500/30 bg-rose-500/10 shrink-0 mt-0.5">
        <span className="text-[9px] font-bold text-rose-500">{letter}</span>
      </div>
      <div className="flex-1 space-y-0.5">
        <p className="text-xs font-semibold text-foreground">{label}</p>
        <p className="text-[11px] font-medium text-foreground/65 leading-snug">{detail}</p>
      </div>
      <Badge variant="outline" className={`shrink-0 text-[9px] font-bold mt-0.5 ${costColor[cost]}`}>
        {txt("Custo", "Cost")} {costLabel[cost]}
      </Badge>
    </div>
  );
}

export default function VoiceAI() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  return (
    <div className="space-y-8 max-w-4xl">

      {/* ── Page header ──────────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-rose-500/10 border border-rose-500/20">
              <Mic className="h-5 w-5 text-rose-500" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                {txt("Inteligência de Voz & Síntese", "Voice Intelligence & Synthesis")}
                <span className="text-foreground/60 font-normal text-lg ml-2">(Voice AI)</span>
              </h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Síntese, clonagem e transcrição de voz, com resposta humanizada em áudio no WhatsApp",
                  "Voice synthesis, cloning and transcription, with humanized audio responses on WhatsApp"
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-rose-950/60 border-rose-500/30 text-rose-300 font-semibold text-[10px] uppercase tracking-wider px-3 py-1.5"
          >
            <FlaskConical className="h-3 w-3 mr-1.5" />
            {txt("Roadmap · Voice Cloning", "Roadmap · Voice Cloning")}
          </Badge>
        </div>

        {/* Banner */}
        <div className="rounded-lg border border-indigo-800 bg-indigo-950 px-5 py-4">
          <div className="flex items-center gap-4">
            <Mic className="h-8 w-8 text-white shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-bold text-white">
                {txt("ROADMAP: Em desenvolvimento / Voice Cloning", "ROADMAP: In development / Voice Cloning")}
              </p>
              <p className="text-xs font-medium text-white/80 mt-0.5 leading-relaxed">
                {txt(
                  "Conecte provedores de voz sintética, clone a voz do profissional e configure regras inteligentes de quando a IA deve responder com áudio, criando uma experiência de atendimento humanizada e escalável.",
                  "Connect synthetic voice providers, clone the professional's voice and configure smart rules for when the AI should respond with audio, creating a humanized and scalable service experience."
                )}
              </p>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0 text-right">
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">Voice Cloning</Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">TTS {txt("Nativo", "Native")}</Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("Transcrição STT", "STT Transcription")}
              </Badge>
            </div>
          </div>
        </div>

        {/* Stat strip */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: txt("Provedores Suportados", "Supported Providers"), value: "3",     note: "ElevenLabs · OpenAI · PersonaPlex"                       },
            { label: txt("Latência de Síntese", "Synthesis Latency"),     value: "~1.2s", note: txt("média por resposta em áudio", "avg per audio response") },
            { label: txt("Idiomas", "Languages"),                         value: "PT-BR", note: txt("com sotaque regional ajustável", "with adjustable regional accent") },
          ].map(s => (
            <div key={s.label} className="rounded-lg border border-indigo-800 bg-indigo-950 px-4 py-3 text-center">
              <p className="text-xl font-bold text-white">{s.value}</p>
              <p className="text-[11px] font-semibold text-white/90 mt-0.5">{s.label}</p>
              <p className="text-[10px] font-medium text-indigo-300 mt-0.5">{s.note}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ── Block 1: API Integration ──────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Integração de API de Voz", "Voice API Integration")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <AudioLines className="h-4 w-4 text-rose-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Provedor de Voz Sintética", "Synthetic Voice Provider")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Selecione o provedor e informe a API Key para ativar a síntese de voz na sua clínica.",
                "Select the provider and enter the API Key to enable voice synthesis at your clinic."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label className="text-xs font-semibold text-foreground/80">
                {txt("Provedor de Voz Sintética", "Synthetic Voice Provider")}
              </Label>
              <Select disabled>
                <SelectTrigger className="h-9 text-sm opacity-60 cursor-not-allowed">
                  <SelectValue placeholder={txt("Selecione o provedor…", "Select provider…")} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="elevenlabs">ElevenLabs</SelectItem>
                  <SelectItem value="openai-tts">OpenAI TTS</SelectItem>
                  <SelectItem value="personaplex">PersonaPlex</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs font-semibold text-foreground/80">
                {txt("API Key do Provedor", "Provider API Key")}
              </Label>
              <div className="relative">
                <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-foreground/40" />
                <Input
                  disabled
                  type="password"
                  placeholder="sk-••••••••••••••••••••••••"
                  className="pl-8 font-mono text-xs opacity-60 cursor-not-allowed"
                />
              </div>
            </div>

            <div className="flex items-center gap-3 pt-1">
              <Button disabled size="sm" className="gap-2 opacity-50 cursor-not-allowed">
                <Mic className="h-3.5 w-3.5" />
                {txt("Treinar / Clonar Voz da Clínica", "Train / Clone Clinic Voice")}
              </Button>
              <Badge variant="outline" className="text-[10px] font-semibold bg-rose-500/10 border-rose-500/20 text-rose-600 dark:text-rose-400">
                {txt("Suporte a clonagem de voz do profissional", "Support for professional voice cloning")}
              </Badge>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 2: Trigger Logic ────────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Regras de Gatilho (Trigger Logic)", "Trigger Rules (Trigger Logic)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Settings2 className="h-4 w-4 text-rose-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Frequência de Uso de Áudio", "Audio Usage Frequency")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Otimize os custos e a experiência do paciente escolhendo exatamente quando a IA deve responder enviando um arquivo de áudio.",
                "Optimize costs and patient experience by choosing exactly when the AI should respond by sending an audio file."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-2.5">
            <TriggerOption
              letter="A"
              label={txt("Sempre responder em áudio", "Always respond in audio")}
              detail={txt(
                "Todas as respostas da IA são sintetizadas e enviadas como arquivo de áudio.",
                "All AI responses are synthesized and sent as audio files."
              )}
              cost="Alto"
            />
            <TriggerOption
              letter="B"
              label={txt("Misto Fixo (50% ou 25% das interações)", "Fixed Mix (50% or 25% of interactions)")}
              detail={txt(
                "A IA alterna entre texto e áudio em proporção configurável, balanceando experiência e custo.",
                "The AI alternates between text and audio in a configurable ratio, balancing experience and cost."
              )}
              cost="Médio"
            />
            <TriggerOption
              letter="C"
              label={txt(
                "Espelhamento: responder em áudio APENAS se o lead enviar áudio",
                "Mirroring: respond in audio ONLY if the lead sends audio"
              )}
              detail={txt(
                "A IA detecta o formato de entrada do paciente e espelha o canal de resposta automaticamente.",
                "The AI detects the patient's input format and automatically mirrors the response channel."
              )}
              cost="Baixo"
            />
            <TriggerOption
              letter="D"
              label={txt(
                "Quebra de Objeção: áudio se o lead consumir mais de 1.000 tokens na sessão",
                "Objection Breaking: audio if the lead consumes more than 1,000 tokens in the session"
              )}
              detail={txt(
                "Lead com muitas dúvidas recebe uma resposta em voz humanizada para aumentar o engajamento e a confiança.",
                "Leads with many questions receive a humanized voice response to increase engagement and trust."
              )}
              cost="Baixo"
            />
            <TriggerOption
              letter="E"
              label={txt("Nunca responder em áudio (Apenas Texto)", "Never respond in audio (Text Only)")}
              detail={txt(
                "A síntese de voz fica completamente desativada. Todas as respostas são enviadas como texto.",
                "Voice synthesis is completely disabled. All responses are sent as text."
              )}
              cost="Zero"
            />

            <div className="flex items-start gap-2 rounded-lg border border-rose-500/15 bg-rose-500/5 px-3 py-2.5 mt-1">
              <Info className="h-4 w-4 text-rose-500 mt-0.5 shrink-0" />
              <p className="text-xs font-medium text-foreground/80 leading-relaxed">
                {txt(
                  "Cada áudio sintetizado consome créditos do seu saldo. Configure o gatilho mais eficiente para o perfil de atendimento da sua clínica.",
                  "Each synthesized audio consumes credits from your balance. Configure the most efficient trigger for your clinic's service profile."
                )}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 3: Inbound Transcription ───────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="03" label={txt("Transcrição Inbound (Speech-to-Text)", "Inbound Transcription (Speech-to-Text)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Ear className="h-4 w-4 text-rose-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Transcrição Automática de Áudios", "Automatic Audio Transcription")}
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between rounded-lg border border-border/60 bg-muted/20 px-4 py-3 cursor-not-allowed">
              <div className="space-y-0.5">
                <p className="text-sm font-semibold text-foreground">
                  {txt("Transcrever áudios do paciente automaticamente", "Automatically transcribe patient audio")}
                </p>
                <p className="text-xs font-medium text-foreground/65 leading-relaxed max-w-md">
                  {txt(
                    "A IA escuta o áudio do paciente, transcreve para o banco de dados e analisa a intenção antes de responder.",
                    "The AI listens to the patient's audio, transcribes it to the database and analyzes the intent before responding."
                  )}
                </p>
              </div>
              <Switch checked={true} disabled className="opacity-50 cursor-not-allowed shrink-0 ml-4" />
            </div>

            <div className="grid grid-cols-3 gap-2">
              {[
                { label: txt("Precisão STT", "STT Accuracy"),      value: "97%"   },
                { label: txt("Latência", "Latency"),                value: "< 2s"  },
                { label: txt("Idioma principal", "Primary Language"), value: "PT-BR" },
              ].map(m => (
                <div key={m.label} className="rounded-md border border-border/50 bg-muted/20 px-3 py-2.5 text-center opacity-70">
                  <p className="text-sm font-bold text-foreground">{m.value}</p>
                  <p className="text-[10px] font-medium text-foreground/60 mt-0.5 leading-tight">{m.label}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
