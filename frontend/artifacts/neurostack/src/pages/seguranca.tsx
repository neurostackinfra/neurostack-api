import { CheckCircle2, Clock, ShieldCheck } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useI18n } from "@/context/i18n";

type Copy = { pt: string; en: string };

/** Controls that are live today. Each one was verified against the code or AWS on 2026-11-13. */
const ACTIVE_ITEMS: readonly Copy[] = [
  { pt: "Criptografia em trânsito: HTTPS com TLS 1.2 e 1.3", en: "Encryption in transit: HTTPS with TLS 1.2 and 1.3" },
  {
    pt: "Criptografia em repouso: banco de dados, mídias e backups com chave KMS",
    en: "Encryption at rest: database, media and backups with KMS keys",
  },
  { pt: "Isolamento dos dados de cada clínica", en: "Each clinic's data kept isolated" },
  { pt: "Acesso por perfil e senhas protegidas com bcrypt", en: "Role-based access and bcrypt-hashed passwords" },
  {
    pt: "Mascaramento de números de carteirinha nos traces e nas exportações de traces e de treino",
    en: "Insurance card numbers masked in traces and in trace and training exports",
  },
  {
    pt: "Exportação para auditoria: até 6 meses para trás, arquivos de até 60 dias",
    en: "Audit export: up to 6 months back, files of up to 60 days",
  },
  {
    pt: "Backups diários e banco redundante em produção (Multi-AZ)",
    en: "Daily backups and a redundant production database (Multi-AZ)",
  },
  { pt: "Trilha de auditoria da infraestrutura (AWS CloudTrail)", en: "Infrastructure audit trail (AWS CloudTrail)" },
  { pt: "LGPD: política de privacidade publicada para cada clínica", en: "LGPD: privacy policy published for each clinic" },
];

/** Work in progress (owner's list, 2026-11-13). No dates are promised. */
const IN_PROGRESS_ITEMS: readonly Copy[] = [
  { pt: "Relatório SOC 2", en: "SOC 2 report" },
  { pt: "Exclusão automática de dados antigos", en: "Automatic deletion of old data" },
  { pt: "Login em duas etapas (2FA)", en: "Two-step login (2FA)" },
  { pt: "Detecção de ameaças na infraestrutura AWS", en: "Threat detection on the AWS infrastructure" },
];

function StatusList({ id, title, items, status, txt }: {
  id: string;
  title: string;
  items: readonly Copy[];
  status: "active" | "in-progress";
  txt: (p: string, e: string) => string;
}) {
  const Icon = status === "active" ? CheckCircle2 : Clock;
  const iconClass = status === "active" ? "text-emerald-500" : "text-amber-500";
  return (
    <Card className="border-border/60 bg-card">
      <CardHeader className="pb-3">
        <CardTitle id={id} className="text-sm font-semibold text-foreground">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ul aria-labelledby={id} className="space-y-2.5">
          {items.map((item) => (
            <li key={item.pt} className="flex items-start gap-2.5">
              <Icon className={`h-4 w-4 mt-0.5 shrink-0 ${iconClass}`} aria-hidden="true" />
              <span className="text-sm text-foreground/85 leading-snug">{txt(item.pt, item.en)}</span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

export default function Seguranca() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => (locale === "en" ? e : p);

  return (
    <div className="space-y-6 max-w-4xl">
      {/* Page header */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-slate-800 border border-slate-700">
          <ShieldCheck className="h-5 w-5 text-slate-300" />
        </div>
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            {txt("Segurança & Compliance", "Security & Compliance")}
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            {txt("O que já está ativo e o que está em andamento", "What is already active and what is in progress")}
          </p>
        </div>
      </div>

      {/* SOC 2 strip */}
      <div className="flex items-center gap-3 rounded-lg border border-slate-600 bg-slate-900 px-4 py-3">
        <Clock className="h-4 w-4 shrink-0 text-slate-300" aria-hidden="true" />
        <p className="text-sm font-semibold text-white">{txt("Em preparação para o SOC 2", "Preparing for SOC 2")}</p>
      </div>

      <StatusList id="seguranca-ativo" title={txt("Ativo", "Active")} items={ACTIVE_ITEMS} status="active" txt={txt} />
      <StatusList
        id="seguranca-em-andamento"
        title={txt("Em andamento", "In progress")}
        items={IN_PROGRESS_ITEMS}
        status="in-progress"
        txt={txt}
      />
    </div>
  );
}
