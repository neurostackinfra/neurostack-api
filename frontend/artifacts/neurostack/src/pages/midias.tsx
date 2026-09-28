import {
  FolderOpen,
  Upload,
  Video,
  Mic,
  FileText,
  Image,
  Sparkles,
  FlaskConical,
  Plus,
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
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-sky-500/20 shrink-0">
        <span className="text-[9px] font-bold text-sky-500">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-sky-500">{label}</span>
      <div className="flex-1 h-px bg-sky-500/20" />
    </div>
  );
}

function AssetCard({
  icon: Icon,
  label,
  example,
  count,
  color,
}: {
  icon: React.ElementType;
  label: string;
  example: string;
  count: number;
  color: string;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border/60 bg-muted/20 px-4 py-4 opacity-75 cursor-not-allowed">
      <div className={`flex h-9 w-9 items-center justify-center rounded-md ${color}`}>
        <Icon className="h-4.5 w-4.5" />
      </div>
      <div>
        <p className="text-sm font-semibold text-foreground">{label}</p>
        <p className="text-[11px] font-medium text-foreground/60 mt-0.5 leading-snug">{example}</p>
      </div>
      <div className="mt-auto pt-1 flex items-center justify-between">
        <span className="text-[10px] font-medium text-foreground/45">
          {count} {label.toLowerCase()}{count !== 1 ? "s" : ""}
        </span>
        <Badge variant="outline" className="text-[9px] border-border/50 text-foreground/40 px-1.5">
          Em breve
        </Badge>
      </div>
    </div>
  );
}

export default function Midias() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  return (
    <div className="space-y-8 max-w-4xl">

      {/* ── Page header ──────────────────────────────────────────────────────── */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-sky-500/10 border border-sky-500/20">
              <FolderOpen className="h-5 w-5 text-sky-500" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                {txt("Central de Mídias e Ativos", "Media & Assets Hub")}
              </h1>
              <p className="text-sm font-medium text-foreground/70 mt-0.5">
                {txt(
                  "Repositório centralizado de conteúdo para envio automatizado ou manual pelo agente",
                  "Centralized content repository for automated or manual delivery by the agent"
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-sky-950/60 border-sky-500/30 text-sky-300 font-semibold text-[10px] uppercase tracking-wider px-3 py-1.5"
          >
            <FlaskConical className="h-3 w-3 mr-1.5" />
            {txt("Roadmap · Gestão de Ativos Digitais", "Roadmap · Digital Asset Management")}
          </Badge>
        </div>

        {/* Banner */}
        <div className="rounded-lg border border-indigo-800 bg-indigo-950 px-5 py-4">
          <div className="flex items-center gap-4">
            <FolderOpen className="h-8 w-8 text-white shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-bold text-white">
                {txt("ROADMAP: Gestão de Ativos Digitais", "ROADMAP: Digital Asset Management")}
              </p>
              <p className="text-xs font-medium text-white/80 mt-0.5 leading-relaxed">
                {txt(
                  "Faça upload de vídeos, áudios, PDFs e imagens autorizados. A IA detecta o momento certo e envia o conteúdo automaticamente para o paciente, no contexto correto da conversa.",
                  "Upload authorized videos, audio, PDFs and images. The AI detects the right moment and automatically sends the content to the patient in the correct conversation context."
                )}
              </p>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0 text-right">
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("Envio Contextual", "Contextual Delivery")}
              </Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("Multi-formato", "Multi-format")}
              </Badge>
              <Badge variant="outline" className="text-[9px] font-bold tracking-widest bg-indigo-900/40 border-indigo-500/30 text-indigo-200 px-2">
                {txt("Automático", "Automatic")}
              </Badge>
            </div>
          </div>
        </div>

        {/* Stat strip */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: txt("Formatos Suportados", "Supported Formats"), value: "4",     note: txt("vídeo, áudio, PDF, imagem", "video, audio, PDF, image") },
            { label: txt("Gatilhos de IA", "AI Triggers"),             value: "∞",    note: txt("configuráveis por ativo", "configurable per asset")     },
            { label: txt("Taxa de Engajamento", "Engagement Rate"),    value: "+62%", note: txt("vs mensagem de texto puro", "vs plain text message")     },
          ].map(s => (
            <div key={s.label} className="rounded-lg border border-indigo-800 bg-indigo-950 px-4 py-3 text-center">
              <p className="text-xl font-bold text-white">{s.value}</p>
              <p className="text-[11px] font-semibold text-white/90 mt-0.5">{s.label}</p>
              <p className="text-[10px] font-medium text-indigo-300 mt-0.5">{s.note}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ── Block 1: Upload & Config ──────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label={txt("Upload e Configuração de Ativos", "Asset Upload & Configuration")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Upload className="h-4 w-4 text-sky-500" />
                <CardTitle className="text-sm font-semibold text-foreground">
                  {txt("Adicionar Novo Ativo", "Add New Asset")}
                </CardTitle>
              </div>
              <Button disabled size="sm" className="gap-1.5 opacity-50 cursor-not-allowed">
                <Plus className="h-3.5 w-3.5" />
                {txt("Adicionar Nova Mídia", "Add New Media")}
              </Button>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Faça upload do arquivo e defina quando a IA deve enviá-lo automaticamente.",
                "Upload the file and define when the AI should send it automatically."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-col items-center justify-center rounded-lg border-2 border-dashed border-border/50 bg-muted/10 px-6 py-8 opacity-60 cursor-not-allowed">
              <Upload className="h-8 w-8 text-foreground/30 mb-2" />
              <p className="text-sm font-semibold text-foreground/50">
                {txt("Arraste um arquivo ou clique para selecionar", "Drag a file or click to select")}
              </p>
              <p className="text-[11px] font-medium text-foreground/35 mt-1">
                {txt("MP4, MP3, PDF, JPG, PNG - até 50 MB", "MP4, MP3, PDF, JPG, PNG - up to 50 MB")}
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label className="text-xs font-semibold text-foreground/80">
                  {txt("Nome do Arquivo", "File Name")}
                </Label>
                <Input
                  disabled
                  placeholder={txt("Ex: Orientações Pós-Botox", "e.g. Post-Botox Instructions")}
                  className="text-sm opacity-60 cursor-not-allowed"
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs font-semibold text-foreground/80">{txt("Tipo", "Type")}</Label>
                <Select disabled>
                  <SelectTrigger className="h-9 text-sm opacity-60 cursor-not-allowed">
                    <SelectValue placeholder={txt("Vídeo / Áudio / PDF / Imagem", "Video / Audio / PDF / Image")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="video">{txt("Vídeo", "Video")}</SelectItem>
                    <SelectItem value="audio">{txt("Áudio", "Audio")}</SelectItem>
                    <SelectItem value="pdf">PDF</SelectItem>
                    <SelectItem value="imagem">{txt("Imagem", "Image")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs font-semibold text-foreground/80">
                {txt("Gatilho de Uso (Motivo)", "Usage Trigger (Reason)")}
              </Label>
              <Select disabled>
                <SelectTrigger className="h-9 text-sm opacity-60 cursor-not-allowed">
                  <SelectValue placeholder={txt("Selecione o contexto de envio", "Select send context")} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="pos-procedimento">{txt("Instruções Pós-Procedimento", "Post-Procedure Instructions")}</SelectItem>
                  <SelectItem value="apresentacao">{txt("Apresentação da Clínica", "Clinic Introduction")}</SelectItem>
                  <SelectItem value="tabela">{txt("Tabela de Valores Oficiais", "Official Price List")}</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-[11px] font-medium text-foreground/50">
                {txt(
                  "A IA enviará este arquivo quando detectar a intenção correspondente na conversa.",
                  "The AI will send this file when it detects the corresponding intent in the conversation."
                )}
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 2: Asset Library ────────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label={txt("Biblioteca de Ativos", "Asset Library")} />
        <div className="grid grid-cols-2 gap-3">
          <AssetCard icon={Video}    label={txt("Vídeos", "Videos")}   example={txt("Orientações do Doutor", "Doctor Instructions")} count={0} color="bg-rose-500/10 text-rose-500"    />
          <AssetCard icon={Mic}      label={txt("Áudios", "Audios")}   example={txt("Boas-vindas Personalizadas", "Personalized Welcome")} count={0} color="bg-amber-500/10 text-amber-500"  />
          <AssetCard icon={FileText} label="PDFs"                       example={txt("E-books e Preparatórios", "E-books & Prep Materials")} count={0} color="bg-sky-500/10 text-sky-500"      />
          <AssetCard icon={Image}    label={txt("Imagens", "Images")}   example={txt("Antes e Depois Autorizados", "Authorized Before & After")} count={0} color="bg-emerald-500/10 text-emerald-500" />
        </div>
      </div>

      {/* ── Block 3: AI Assertiveness Logic ──────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="03" label={txt("Lógica de Assertividade (AI Logic)", "Assertiveness Logic (AI Logic)")} />
        <Card className="border-border/60 bg-card opacity-80">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-sky-500" />
              <CardTitle className="text-sm font-semibold text-foreground">
                {txt("Sugestão Automática por IA", "Automatic AI Suggestion")}
              </CardTitle>
            </div>
            <p className="text-xs font-medium text-foreground/75 mt-1">
              {txt(
                "Configure para que a IA sugira o envio deste arquivo automaticamente quando detectar a intenção correta do paciente.",
                "Configure the AI to automatically suggest sending this file when it detects the correct patient intent."
              )}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between rounded-lg border border-border/60 bg-muted/20 px-4 py-3 cursor-not-allowed">
              <div className="space-y-0.5">
                <p className="text-sm font-semibold text-foreground">
                  {txt("Sugestão Automática por IA", "Automatic AI Suggestion")}
                </p>
                <p className="text-xs font-medium text-foreground/60">
                  {txt(
                    "A IA analisa o contexto e envia o ativo no momento de maior impacto",
                    "The AI analyzes context and sends the asset at the highest-impact moment"
                  )}
                </p>
              </div>
              <Switch checked={false} disabled className="opacity-50 cursor-not-allowed shrink-0" />
            </div>

            <div className="grid grid-cols-3 gap-2">
              {[
                { label: txt("Detecção de intenção", "Intent detection"), value: "NLP" },
                { label: txt("Precisão do gatilho", "Trigger precision"), value: "94%" },
                { label: txt("Formatos suportados", "Supported formats"), value: "4"   },
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
