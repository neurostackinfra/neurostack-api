import { useState, useRef, useCallback } from "react";
import {
  Paperclip, Send, X, FileText, Image, HelpCircle,
  MessageSquare, Circle, FlaskConical,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { useI18n } from "@/context/i18n";
import { useAuth } from "@/context/auth";

const MAX_BYTES = 5 * 1024 * 1024;
const ALLOWED = ["image/png", "image/jpeg", "application/pdf"];

function getToken(): string {
  try {
    return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? "";
  } catch { return ""; }
}

function fileToBase64(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve((reader.result as string).split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(f);
  });
}

function fileIcon(type: string) {
  if (type.startsWith("image/")) return <Image className="h-4 w-4 text-sky-500" />;
  return <FileText className="h-4 w-4 text-rose-500" />;
}

function formatBytes(bytes: number) {
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(0)} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function Suporte() {
  const { t } = useI18n();
  const { isClinicAdmin } = useAuth();
  const { toast } = useToast();
  const [tipo, setTipo] = useState("");
  const [mensagem, setMensagem] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const applyFile = (f: File) => {
    if (!ALLOWED.includes(f.type)) {
      toast({ title: t("support_toast_format_title"), description: t("support_toast_format_desc"), variant: "destructive" });
      return;
    }
    if (f.size > MAX_BYTES) {
      toast({ title: t("support_toast_size_title"), description: t("support_toast_size_desc"), variant: "destructive" });
      return;
    }
    setFile(f);
  };

  const onInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.[0]) applyFile(e.target.files[0]);
  };

  const onDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files?.[0]) applyFile(e.dataTransfer.files[0]);
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tipo || !mensagem.trim()) {
      toast({ title: t("support_toast_fields_title"), description: t("support_toast_fields_desc"), variant: "destructive" });
      return;
    }
    setSubmitting(true);
    try {
      let file_data: string | undefined;
      if (file) file_data = await fileToBase64(file);
      const res = await fetch(`/v1/support/tickets`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
        body: JSON.stringify({ tipo, mensagem, filename: file?.name, file_type: file?.type, file_data }),
      });
      if (!res.ok) throw new Error("fail");
      const data = await res.json();
      if (!data.email_sent) {
        toast({ title: "Erro ao enviar email", description: "SMTP não configurado ou falhou. Contate o suporte diretamente.", variant: "destructive" });
        return;
      }
      toast({
        title: t("support_toast_ok_title"),
        description: file
          ? `${t("support_toast_ok_file")} "${file.name}".`
          : t("support_toast_ok_desc"),
      });
      setTipo("");
      setMensagem("");
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
    } catch {
      toast({ title: t("support_toast_err_title"), description: t("support_toast_err_desc"), variant: "destructive" });
    } finally {
      setSubmitting(false);
    }
  };

  if (isClinicAdmin) {
    return (
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted border border-border">
            <HelpCircle className="h-5 w-5 text-foreground/70" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-foreground">{t("support_title")}</h1>
            <p className="text-sm font-medium text-foreground/65 mt-0.5">
              {t("support_subtitle")}
            </p>
          </div>
        </div>
        <Card className="border-border/70 shadow-sm">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-semibold text-foreground">Suporte</CardTitle>
              <Badge variant="outline" className="border-amber-500/40 text-amber-500 text-[10px] font-semibold">
                Em breve
              </Badge>
            </div>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Sistema de tickets em breve. Entre em contato com seu builder para suporte.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">

      {/* ── Page header ─────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted border border-border">
          <HelpCircle className="h-5 w-5 text-foreground/70" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">{t("support_title")}</h1>
          <p className="text-sm font-medium text-foreground/65 mt-0.5">
            {t("support_subtitle")}
          </p>
        </div>
      </div>

      {/* ── Two-column layout ────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 items-start">

        {/* ══════════════════════════════════════════════════════════════════════
            LEFT — Active ticket system
        ══════════════════════════════════════════════════════════════════════ */}
        <Card className="border-border/70 shadow-sm">
          <CardHeader className="pb-3 border-b border-border/50">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <CardTitle className="text-sm font-semibold text-foreground">
                  {t("support_ticket_heading")}
                </CardTitle>
              </div>
              <Badge className="bg-emerald-500/15 text-emerald-500 border-emerald-500/30 text-[10px] font-semibold">
                <Circle className="h-1.5 w-1.5 mr-1 fill-emerald-500" />
                {t("support_active_badge")}
              </Badge>
            </div>
            <p className="text-xs font-medium text-foreground/55 mt-1">
              {t("support_response_hint")}{" "}
              <span className="text-foreground/75">info@neurostack.global</span>
            </p>
          </CardHeader>
          <CardContent className="pt-5">
            <form onSubmit={handleSubmit} className="space-y-4">

              {/* Type */}
              <div className="space-y-1.5">
                <Label className="text-xs font-semibold text-foreground/80">{t("support_tipo_label")}</Label>
                <Select value={tipo} onValueChange={setTipo}>
                  <SelectTrigger className="h-9 text-sm">
                    <SelectValue placeholder={t("support_tipo_ph")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="solicitacao">{t("support_tipo_request")}</SelectItem>
                    <SelectItem value="feedback">{t("support_tipo_feedback")}</SelectItem>
                    <SelectItem value="financeiro">{t("support_tipo_financial")}</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {/* Message */}
              <div className="space-y-1.5">
                <Label className="text-xs font-semibold text-foreground/80">{t("support_message_label")}</Label>
                <Textarea
                  value={mensagem}
                  onChange={(e) => setMensagem(e.target.value)}
                  placeholder={t("support_message_ph")}
                  className="min-h-[130px] resize-none text-sm text-foreground/90 placeholder:text-foreground/35"
                />
              </div>

              {/* File attachment */}
              <div className="space-y-1.5">
                <Label className="text-xs font-semibold text-foreground/80">
                  {t("support_attach_label")}{" "}
                  <span className="font-normal text-foreground/45">{t("support_attach_optional")}</span>
                </Label>
                <div
                  onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={onDrop}
                  onClick={() => !file && inputRef.current?.click()}
                  className={`flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-5 py-5 transition-colors ${
                    dragOver
                      ? "border-foreground/40 bg-muted/40"
                      : "border-border/50 bg-muted/10 hover:border-foreground/25 hover:bg-muted/20"
                  } ${file ? "cursor-default" : "cursor-pointer"}`}
                >
                  <input
                    ref={inputRef}
                    type="file"
                    accept=".png,.jpg,.jpeg,.pdf"
                    className="hidden"
                    onChange={onInputChange}
                  />
                  {file ? (
                    <div className="flex w-full items-center gap-3 rounded-md border border-border/60 bg-background px-3 py-2.5">
                      {fileIcon(file.type)}
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-semibold text-foreground truncate">{file.name}</p>
                        <p className="text-[10px] font-medium text-foreground/50 mt-0.5">{formatBytes(file.size)}</p>
                      </div>
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); setFile(null); if (inputRef.current) inputRef.current.value = ""; }}
                        className="p-1 rounded hover:bg-muted transition-colors text-foreground/40 hover:text-foreground/80"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <>
                      <Paperclip className="h-5 w-5 text-foreground/30" />
                      <p className="text-xs font-medium text-foreground/60 text-center">
                        {t("support_attach_cta")}
                      </p>
                      <p className="text-[10px] text-foreground/40">{t("support_attach_limits")}</p>
                    </>
                  )}
                </div>
              </div>

              {/* Submit */}
              <Button type="submit" disabled={submitting} className="w-full gap-2">
                <Send className="h-3.5 w-3.5" />
                {submitting ? t("support_sending_btn") : t("support_send_btn")}
              </Button>
            </form>
          </CardContent>
        </Card>

        {/* ══════════════════════════════════════════════════════════════════════
            RIGHT — Concierge / Live Chat (Roadmap)
        ══════════════════════════════════════════════════════════════════════ */}
        <div className="opacity-60 pointer-events-none select-none">
          <Card className="border-border/50 shadow-sm">
            <CardHeader className="pb-3 border-b border-border/50">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <MessageSquare className="h-4 w-4 text-foreground/60" />
                  <CardTitle className="text-sm font-semibold text-foreground">
                    {t("support_chat_heading")}
                  </CardTitle>
                </div>
                <Badge variant="outline" className="border-indigo-500/40 text-indigo-400 text-[10px] font-semibold gap-1">
                  <FlaskConical className="h-2.5 w-2.5" />
                  {t("support_chat_badge")}
                </Badge>
              </div>
              <p className="text-xs font-medium text-foreground/50 mt-1">
                {t("support_chat_desc")}
              </p>
            </CardHeader>
            <CardContent className="pt-5 space-y-4">

              {/* Presence banner */}
              <div className="flex items-center gap-2.5 rounded-lg border border-emerald-500/25 bg-emerald-500/8 px-3 py-2.5">
                <span className="relative flex h-2.5 w-2.5 shrink-0">
                  <span className="absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-emerald-500" />
                </span>
                <p className="text-xs font-semibold text-emerald-600 dark:text-emerald-400">
                  {t("support_online_status")}
                  <span className="font-normal text-foreground/60 ml-1">· {t("support_online_time")}</span>
                </p>
              </div>

              {/* Mock chat window */}
              <div className="rounded-lg border border-border/50 bg-muted/10 overflow-hidden">
                {/* Chat header */}
                <div className="flex items-center gap-2 border-b border-border/40 bg-muted/20 px-3 py-2">
                  <div className="h-6 w-6 rounded-full bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center shrink-0">
                    <MessageSquare className="h-3 w-3 text-indigo-400" />
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold text-foreground">NeuroStack CS</p>
                    <p className="text-[9px] text-foreground/50">{t("support_agent_role")}</p>
                  </div>
                  <div className="ml-auto flex items-center gap-1">
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                    <span className="text-[9px] text-foreground/50">Online</span>
                  </div>
                </div>

                {/* Messages */}
                <div className="space-y-3 px-3 py-3 min-h-[130px]">
                  <div className="flex items-end gap-2 max-w-[85%]">
                    <div className="h-5 w-5 rounded-full bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center shrink-0 mb-0.5">
                      <MessageSquare className="h-2.5 w-2.5 text-indigo-400" />
                    </div>
                    <div className="rounded-2xl rounded-bl-sm bg-muted/50 border border-border/40 px-3 py-2">
                      <p className="text-xs text-foreground/80 leading-relaxed">
                        {t("support_chat_greeting")}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 px-1">
                    <span className="text-[10px] text-foreground/35 italic">{t("support_chat_waiting")}</span>
                  </div>
                </div>

                {/* Input row */}
                <div className="flex items-center gap-2 border-t border-border/40 bg-muted/10 px-3 py-2.5">
                  <Input
                    disabled
                    placeholder={t("support_chat_input_ph")}
                    className="h-8 text-xs flex-1 bg-background/50 border-border/40"
                  />
                  <Button disabled size="sm" className="h-8 px-3 gap-1.5 shrink-0">
                    <Send className="h-3 w-3" />
                  </Button>
                </div>
              </div>

              {/* Feature hints */}
              <div className="grid grid-cols-2 gap-2">
                {[
                  { label: t("support_feat_history"),    note: t("support_feat_history_note")    },
                  { label: t("support_feat_files"),      note: t("support_feat_files_note")      },
                  { label: t("support_feat_escalation"), note: t("support_feat_escalation_note") },
                  { label: t("support_feat_sla"),        note: t("support_feat_sla_note")        },
                ].map((f) => (
                  <div key={f.label} className="rounded-md border border-border/40 bg-muted/10 px-2.5 py-2">
                    <p className="text-[11px] font-semibold text-foreground/70">{f.label}</p>
                    <p className="text-[10px] text-foreground/40 mt-0.5">{f.note}</p>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        </div>

      </div>
    </div>
  );
}
