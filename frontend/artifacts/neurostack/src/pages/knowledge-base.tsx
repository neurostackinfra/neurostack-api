import { Badge } from "@/components/ui/badge";
import {
  Library,
  FileText,
  FileType2,
  FileCode2,
  Lock,
  Sparkles,
  Upload,
  Search,
  Zap,
  ShieldCheck,
} from "lucide-react";
import { useI18n } from "@/context/i18n";

const mockDocuments = [
  { id: 1, name: "Protocolo_Pos_Operatorio_Botox.pdf",      type: "PDF",  size: "1.2 MB", status: "syncing" },
  { id: 2, name: "Regras_Internas_Clinica_Lumina.docx",     type: "DOCX", size: "340 KB", status: "ready"   },
];

function FileIcon({ type }: { type: string }) {
  const base = "h-4 w-4 shrink-0";
  if (type === "PDF")  return <FileText  className={`${base} text-red-400`}    />;
  if (type === "DOCX") return <FileType2 className={`${base} text-blue-400`}   />;
  return                      <FileCode2 className={`${base} text-emerald-400`}/>;
}

function StatusBadge({ status }: { status: string }) {
  if (status === "ready")
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-400 border border-emerald-500/20">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
        Ready
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold text-amber-400 border border-amber-500/20">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
      Syncing…
    </span>
  );
}

export default function KnowledgeBase() {
  const { locale } = useI18n();
  const txt = <T,>(p: T, e: T): T => (locale === "en" ? e : p);

  return (
    <div className="max-w-2xl mx-auto space-y-6 pb-10 relative">

      {/* ── Coming Soon ribbon ──────────────────────────────────────────── */}
      <div className="flex items-center gap-3 rounded-lg border border-violet-400/30 bg-violet-500/5 px-4 py-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet-500/15 shrink-0">
          <Lock className="h-4 w-4 text-violet-400" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-semibold text-violet-300">
            {txt(
              "Funcionalidade em desenvolvimento, disponível nos próximos releases",
              "Feature under development, available in upcoming releases"
            )}
          </p>
          <p className="text-[11px] text-muted-foreground mt-0.5">
            {txt(
              "Esta tela é uma prévia do roadmap para demonstração a investidores e clínicas Enterprise.",
              "This screen is a roadmap preview for investor and Enterprise clinic demonstrations."
            )}
          </p>
        </div>
        <Badge className="shrink-0 bg-violet-500/20 text-violet-300 border-violet-400/30 text-[10px] font-bold uppercase tracking-wider">
          {txt("EM BREVE", "COMING SOON")}
        </Badge>
      </div>

      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="flex items-start gap-3">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-blue-500/10 border border-blue-500/20 shrink-0">
          <Library className="h-5 w-5 text-blue-400" />
        </div>
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <h1 className="text-xl font-semibold text-foreground">
              {txt("Base de Conhecimento (RAG)", "Knowledge Base (RAG)")}
            </h1>
          </div>
          <p className="text-sm text-muted-foreground mt-0.5">
            {txt(
              "Ingira protocolos e documentos internos. A IA consulta seus arquivos em tempo real.",
              "Ingest internal protocols and documents. The AI queries your files in real time."
            )}
          </p>
        </div>
      </div>

      {/* ── Upload Dropzone (read-only mockup) ──────────────────────────── */}
      <div className="relative rounded-xl border-2 border-dashed border-slate-600/60 bg-slate-900/40 p-10 text-center pointer-events-none select-none">
        <div className="absolute inset-0 rounded-xl z-10 flex items-end justify-center pb-4">
          <span className="inline-flex items-center gap-1.5 rounded-full bg-slate-800/80 border border-slate-600/50 px-3 py-1 text-[10px] font-bold uppercase tracking-wider text-slate-400 backdrop-blur-sm">
            <Lock className="h-3 w-3" />
            {txt("Ingestão disponível em breve", "Ingestion available soon")}
          </span>
        </div>

        <div className="flex flex-col items-center gap-3">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-500/10 border border-blue-500/20">
            <Upload className="h-6 w-6 text-blue-400/60" />
          </div>
          <div>
            <p className="text-sm font-medium text-muted-foreground">
              {txt(
                "Arraste seus protocolos, PDFs ou manuais da clínica aqui",
                "Drag your protocols, PDFs or clinic manuals here"
              )}
            </p>
            <p className="text-xs text-muted-foreground/60 mt-1">
              {txt(
                "ou clique para selecionar arquivos do seu computador",
                "or click to select files from your computer"
              )}
            </p>
          </div>

          <div className="flex items-center gap-2 mt-1">
            {["PDF", "DOCX", "TXT"].map((fmt) => (
              <span
                key={fmt}
                className="inline-flex items-center gap-1 rounded-md bg-slate-800 border border-slate-700 px-2 py-0.5 text-[10px] font-mono font-semibold text-slate-400"
              >
                {fmt}
              </span>
            ))}
            <span className="text-[10px] text-muted-foreground/50">
              {txt("suportados", "supported")}
            </span>
          </div>
        </div>
      </div>

      {/* ── Mock document list ───────────────────────────────────────────── */}
      <div className="space-y-2">
        <div className="flex items-center justify-between px-1">
          <p className="text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
            {txt("Documentos Indexados", "Indexed Documents")}
          </p>
          <span className="text-[10px] text-muted-foreground font-mono">
            {mockDocuments.length} {txt(
              `arquivo${mockDocuments.length !== 1 ? "s" : ""}`,
              `file${mockDocuments.length !== 1 ? "s" : ""}`
            )}
          </span>
        </div>

        {mockDocuments.map((doc) => (
          <div
            key={doc.id}
            className="flex items-center gap-3 rounded-lg border border-border bg-muted/20 px-4 py-3"
          >
            <FileIcon type={doc.type} />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-foreground truncate">{doc.name}</p>
              <p className="text-[11px] text-muted-foreground mt-0.5">{doc.size}</p>
            </div>
            <StatusBadge status={doc.status} />
          </div>
        ))}
      </div>

      {/* ── Technical explanation card ───────────────────────────────────── */}
      <div className="rounded-xl border border-slate-700 bg-slate-800/80 p-5 space-y-4">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-blue-400 shrink-0" />
          <p className="text-sm font-bold text-blue-400">
            {txt("Como funciona o RAG Engine", "How the RAG Engine works")}
          </p>
        </div>
        <p className="text-xs text-slate-200 leading-relaxed">
          {txt(
            <>Nossa engine <span className="font-semibold text-blue-300">RAG (Retrieval-Augmented Generation)</span> permitirá que a IA consulte seus documentos privados em tempo real para respostas{" "}<span className="font-bold text-white">100% precisas baseadas nos seus protocolos</span>, sem alucinar, sem inventar procedimentos, sempre dentro do seu manual interno.</>,
            <>Our <span className="font-semibold text-blue-300">RAG (Retrieval-Augmented Generation)</span> engine will allow the AI to query your private documents in real time for{" "}<span className="font-bold text-white">100% accurate answers based on your protocols</span>, no hallucinations, no invented procedures, always within your internal guidelines.</>
          )}
        </p>

        <div className="grid grid-cols-3 gap-3 pt-1">
          {[
            {
              icon: Upload,
              label: txt("Ingestão", "Ingestion"),
              desc: txt("PDFs, DOCXs e TXTs indexados automaticamente", "PDFs, DOCXs and TXTs indexed automatically"),
              iconCls: "text-amber-400",
              accentBorder: "border-t-2 border-t-amber-400",
              iconBg: "bg-amber-400/15",
            },
            {
              icon: Search,
              label: "Retrieval",
              desc: txt("Chunks relevantes recuperados por similaridade semântica", "Relevant chunks retrieved by semantic similarity"),
              iconCls: "text-blue-400",
              accentBorder: "border-t-2 border-t-blue-400",
              iconBg: "bg-blue-400/15",
            },
            {
              icon: Zap,
              label: "Generation",
              desc: txt("Resposta gerada com base nos documentos da clínica", "Response generated based on clinic documents"),
              iconCls: "text-emerald-400",
              accentBorder: "border-t-2 border-t-emerald-400",
              iconBg: "bg-emerald-400/15",
            },
          ].map(({ icon: Icon, label, desc, iconCls, accentBorder, iconBg }) => (
            <div
              key={label}
              className={`rounded-lg bg-slate-900 border border-slate-600 ${accentBorder} p-3 space-y-2`}
            >
              <div className="flex items-center gap-2">
                <div className={`flex h-6 w-6 items-center justify-center rounded-md ${iconBg} shrink-0`}>
                  <Icon className={`h-3.5 w-3.5 ${iconCls}`} />
                </div>
                <span className="text-[11px] font-bold text-white uppercase tracking-wider">{label}</span>
              </div>
              <p className="text-[10px] text-slate-300 leading-snug">{desc}</p>
            </div>
          ))}
        </div>

        <div className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-950/40 px-3 py-2.5">
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-400 shrink-0 mt-0.5" />
          <p className="text-[11px] text-emerald-200 leading-snug">
            {txt(
              <><span className="font-bold text-emerald-400">Isolamento total por tenant.</span>{" "}Cada clínica acessa apenas seus próprios documentos. Zero cross-contamination entre bases de conhecimento.</>,
              <><span className="font-bold text-emerald-400">Full per-tenant isolation.</span>{" "}Each clinic accesses only its own documents. Zero cross-contamination between knowledge bases.</>
            )}
          </p>
        </div>
      </div>

    </div>
  );
}
