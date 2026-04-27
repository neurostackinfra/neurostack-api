import {
  ShieldCheck,
  Globe2,
  Database,
  FileText,
  Users,
  Brain,
  Lock,
  Info,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/context/i18n";

function BlockHeader({ icon: Icon, color, title, subtitle }: {
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  title: string;
  subtitle: string;
}) {
  return (
    <CardHeader className="pb-4">
      <div className="flex items-center gap-3">
        <div className={`flex h-9 w-9 items-center justify-center rounded-lg ${color}`}>
          <Icon className="h-4.5 w-4.5 h-5 w-5" />
        </div>
        <div>
          <CardTitle className="text-sm font-semibold text-foreground">{title}</CardTitle>
          <p className="text-xs text-muted-foreground mt-0.5">{subtitle}</p>
        </div>
      </div>
    </CardHeader>
  );
}

function DisabledToggle({ label, defaultChecked = false, sublabel }: {
  label: string;
  defaultChecked?: boolean;
  sublabel?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2.5 border-b border-border/50 last:border-0">
      <div>
        <Label className="text-xs font-medium text-foreground/60 cursor-not-allowed">{label}</Label>
        {sublabel && <p className="text-[10px] text-muted-foreground/60 mt-0.5">{sublabel}</p>}
      </div>
      <Switch checked={defaultChecked} disabled className="opacity-50 cursor-not-allowed" />
    </div>
  );
}

function DisabledSelect({ label, value, options }: {
  label: string;
  value: string;
  options?: string[];
}) {
  return (
    <div className="space-y-1.5 py-2.5 border-b border-border/50 last:border-0">
      <Label className="text-xs font-medium text-foreground/60 cursor-not-allowed">{label}</Label>
      <select
        disabled
        defaultValue={value}
        className="w-full rounded-md border border-border/50 bg-muted/30 px-3 py-1.5 text-xs text-muted-foreground/70 opacity-50 cursor-not-allowed appearance-none"
      >
        <option value={value}>{value}</option>
        {options?.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );
}

function DisabledInfoField({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1.5 py-2.5 border-b border-border/50 last:border-0">
      <Label className="text-xs font-medium text-foreground/60">{label}</Label>
      <div className="flex items-center gap-2 rounded-md border border-border/40 bg-muted/20 px-3 py-1.5 opacity-50">
        <Info className="h-3 w-3 text-muted-foreground shrink-0" />
        <span className="text-xs text-muted-foreground font-mono">{value}</span>
      </div>
    </div>
  );
}

function SectionDivider({ num, label }: { num: string; label: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-700 shrink-0">
        <span className="text-[9px] font-bold text-slate-300">{num}</span>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest text-slate-500">{label}</span>
      <div className="flex-1 h-px bg-border/30" />
    </div>
  );
}

export default function Seguranca() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  return (
    <div className="space-y-8 max-w-4xl">

      {/* Page header */}
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-800 border border-slate-700">
              <ShieldCheck className="h-5 w-5 text-slate-300" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-foreground">
                {txt("Policy Layer & Governança", "Policy Layer & Governance")}
              </h1>
              <p className="text-sm text-muted-foreground mt-0.5">
                {txt(
                  "Camada de conformidade regulatória e controle de acesso de dados sensíveis",
                  "Regulatory compliance layer and sensitive data access control"
                )}
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="shrink-0 mt-1 bg-slate-900 border-slate-600 text-slate-300 font-semibold text-[10px] uppercase tracking-wider px-3 py-1.5"
          >
            <Lock className="h-3 w-3 mr-1.5" />
            Enterprise
          </Badge>
        </div>

        {/* Global enterprise banner */}
        <div className="relative overflow-hidden rounded-lg border-2 border-slate-500 bg-gradient-to-r from-slate-900 via-slate-800 to-slate-900 px-5 py-4">
          <div className="absolute inset-0 opacity-[0.03]"
            style={{ backgroundImage: "repeating-linear-gradient(45deg, currentColor 0, currentColor 1px, transparent 0, transparent 50%)", backgroundSize: "12px 12px" }}
          />
          <div className="relative flex items-center gap-4">
            <ShieldCheck className="h-8 w-8 text-slate-300 shrink-0" />
            <div className="flex-1">
              <p className="text-sm font-bold text-gray-100">
                {txt("ROADMAP: Disponível no plano Enterprise", "ROADMAP: Available on Enterprise plan")}
              </p>
              <p className="text-xs text-gray-300 mt-0.5">
                {txt(
                  "A Policy Layer é a camada de governança regulatória do NeuroStack. Todos os campos abaixo são configuráveis por tenant com suporte a múltiplos regimes jurídicos simultâneos.",
                  "The Policy Layer is NeuroStack's regulatory governance layer. All fields below are configurable per tenant with support for multiple simultaneous legal frameworks."
                )}
              </p>
            </div>
            <div className="flex flex-col items-end gap-1 shrink-0">
              {["LGPD", "HIPAA", "GDPR"].map(r => (
                <Badge key={r} variant="outline" className="text-[9px] font-bold tracking-widest bg-slate-800 border-slate-600 text-slate-400 px-2">
                  {r}
                </Badge>
              ))}
            </div>
          </div>
        </div>

        {/* Status grid */}
        <div className="grid grid-cols-3 gap-3">
          {[
            { label: txt("Audit Events / Dia", "Audit Events / Day"), value: "—",      note: "Enterprise"                 },
            { label: txt("Jurisdições Suportadas", "Supported Jurisdictions"), value: "12",     note: txt("países", "countries") },
            { label: "Uptime SLA",                                            value: "99.99%", note: txt("garantido", "guaranteed") },
          ].map(s => (
            <div key={s.label} className="rounded-lg border border-border bg-card px-4 py-3 text-center">
              <p className="text-xl font-bold text-foreground">{s.value}</p>
              <p className="text-[10px] text-muted-foreground mt-0.5">{s.label}</p>
              <Badge variant="outline" className="mt-1 text-[9px] text-muted-foreground border-border">
                {s.note}
              </Badge>
            </div>
          ))}
        </div>
      </div>

      {/* ── Block 1: Jurisdiction Pack ──────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="01" label="Jurisdiction Pack" />
        <Card className="border-border bg-card opacity-80">
          <BlockHeader
            icon={Globe2}
            color="bg-blue-500/10"
            title={txt("Pacote de Jurisdição", "Jurisdiction Pack")}
            subtitle={txt(
              "Configuração de regime regulatório por país, estado e transferência internacional",
              "Regulatory regime configuration by country, state and international transfer"
            )}
          />
          <CardContent className="space-y-0 divide-y divide-border/30">
            <DisabledSelect
              label={txt("País", "Country")}
              value={txt("Brasil (BR)", "Brazil (BR)")}
              options={["United States (US)", "European Union (EU)", "United Kingdom (UK)"]}
            />
            <DisabledSelect
              label={txt("Estado / Região", "State / Region")}
              value={txt("São Paulo — SP", "São Paulo — SP")}
              options={["Rio de Janeiro — RJ", "Minas Gerais — MG"]}
            />
            <DisabledSelect
              label={txt("Regime Regulatório", "Regulatory Framework")}
              value={txt("LGPD (Lei Geral de Proteção de Dados)", "LGPD (Brazilian General Data Protection Law)")}
              options={[
                "HIPAA (Health Insurance Portability)",
                "GDPR (General Data Protection Regulation)",
                txt("Mixed — LGPD + GDPR", "Mixed — LGPD + GDPR"),
              ]}
            />
            <div className="pt-1">
              <DisabledToggle
                label={txt("Permitir transferência cross-border", "Allow cross-border transfer")}
                defaultChecked={false}
                sublabel={txt(
                  "Habilita envio de dados para provedores de LLM fora da jurisdição principal",
                  "Enables sending data to LLM providers outside the primary jurisdiction"
                )}
              />
              <DisabledToggle
                label={txt("Residência de dados obrigatória", "Mandatory data residency")}
                defaultChecked={true}
                sublabel={txt(
                  "Força processamento e armazenamento dentro da jurisdição configurada",
                  "Forces processing and storage within the configured jurisdiction"
                )}
              />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* ── Block 2: Sensitive Health Data Controls ─────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="02" label="Sensitive Health Data Controls" />
        <Card className="border-border bg-card opacity-80">
          <BlockHeader
            icon={Database}
            color="bg-rose-500/10"
            title={txt("Controles de Dados Sensíveis de Saúde", "Sensitive Health Data Controls")}
            subtitle={txt(
              "Minimização, mascaramento e pseudonimização de PHI/PII no pipeline do agente",
              "Minimization, masking and pseudonymization of PHI/PII in the agent pipeline"
            )}
          />
          <CardContent className="space-y-0 divide-y divide-border/30">
            <DisabledToggle
              label={txt("Minimização de Dados", "Data Minimization")}
              defaultChecked={true}
              sublabel={txt(
                "Coleta apenas dados estritamente necessários para a operação clínica",
                "Collects only data strictly necessary for clinical operation"
              )}
            />
            <DisabledToggle
              label={txt("Mascaramento / Redação (PHI/PII)", "Masking / Redaction (PHI/PII)")}
              defaultChecked={true}
              sublabel={txt(
                "Ofusca dados pessoais de saúde nos logs e traces armazenados",
                "Obfuscates personal health data in stored logs and traces"
              )}
            />
            <DisabledToggle
              label={txt("Pseudonimização", "Pseudonymization")}
              defaultChecked={false}
              sublabel={txt(
                "Substitui identificadores diretos por tokens não-reversíveis por padrão",
                "Replaces direct identifiers with non-reversible tokens by default"
              )}
            />
            <DisabledSelect
              label={txt("Política de Retenção de Dados", "Data Retention Policy")}
              value={txt("90 dias (padrão piloto)", "90 days (pilot default)")}
              options={[
                txt("30 dias", "30 days"),
                txt("180 dias", "180 days"),
                txt("1 ano", "1 year"),
                txt("Indefinido (Enterprise)", "Indefinite (Enterprise)"),
              ]}
            />
            <DisabledSelect
              label={txt("Nível de Audit Trail", "Audit Trail Level")}
              value={txt("Completo — Todas as operações", "Full — All operations")}
              options={[
                txt("Básico — Leitura/Escrita", "Basic — Read/Write"),
                txt("Intermediário — + Decisões SRM", "Intermediate — + SRM Decisions"),
                txt("Completo — Todas as operações", "Full — All operations"),
              ]}
            />
          </CardContent>
        </Card>
      </div>

      {/* ── Block 3: Consent & Notice ───────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="03" label="Consent & Notice" />
        <Card className="border-border bg-card opacity-80">
          <BlockHeader
            icon={FileText}
            color="bg-emerald-500/10"
            title={txt("Consentimento & Aviso de Privacidade", "Consent & Privacy Notice")}
            subtitle={txt(
              "Gestão de consentimento explícito e versionamento de avisos de privacidade",
              "Explicit consent management and privacy notice versioning"
            )}
          />
          <CardContent className="space-y-0 divide-y divide-border/30">
            <DisabledToggle
              label={txt("Aviso de Privacidade Ativo", "Active Privacy Notice")}
              defaultChecked={true}
              sublabel={txt(
                "Exibe aviso automático ao início de cada conversa com o agente",
                "Displays automatic notice at the start of each conversation with the agent"
              )}
            />
            <DisabledToggle
              label={txt("Consentimento Explícito Obrigatório", "Mandatory Explicit Consent")}
              defaultChecked={false}
              sublabel={txt(
                "Bloqueia interação até confirmação ativa do paciente",
                "Blocks interaction until active patient confirmation"
              )}
            />
            <DisabledInfoField
              label={txt("Log de Consentimento Versionado", "Versioned Consent Log")}
              value="consent-log-v2.4.1 · 2025-04-06 · SHA256:a3f1..."
            />
            <DisabledInfoField
              label={txt("Base Legal por Fluxo (LGPD Art. 7º)", "Legal Basis per Flow (LGPD Art. 7)")}
              value={txt(
                "Legítimo interesse clínico · Fluxo: agendar, qualify",
                "Legitimate clinical interest · Flow: schedule, qualify"
              )}
            />
          </CardContent>
        </Card>
      </div>

      {/* ── Block 4: Access Control ─────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="04" label="Access Control" />
        <Card className="border-border bg-card opacity-80">
          <BlockHeader
            icon={Users}
            color="bg-violet-500/10"
            title={txt("Controle de Acesso", "Access Control")}
            subtitle={txt(
              "RBAC por tenant, segregação de clínicas e trilha de auditoria de handoffs",
              "RBAC per tenant, clinic segregation and handoff audit trail"
            )}
          />
          <CardContent className="space-y-0 divide-y divide-border/30">
            <DisabledToggle
              label="RBAC — Role-Based Access Control"
              defaultChecked={true}
              sublabel={txt(
                "Permissões atribuídas por papel: super_admin, clinic_admin, investor, read-only",
                "Permissions assigned by role: super_admin, clinic_admin, investor, read-only"
              )}
            />
            <DisabledToggle
              label={txt("Segregação Estrita de Clínicas", "Strict Clinic Segregation")}
              defaultChecked={true}
              sublabel={txt(
                "Dados de cada tenant completamente isolados — sem acesso cruzado",
                "Each tenant's data completely isolated — no cross-access"
              )}
            />
            <DisabledToggle
              label={txt("Audit Trail de Handoff Humano", "Human Handoff Audit Trail")}
              defaultChecked={true}
              sublabel={txt(
                "Registra timestamp, agente responsável e motivo de cada escalada",
                "Records timestamp, responsible agent and reason for each escalation"
              )}
            />
          </CardContent>
        </Card>
      </div>

      {/* ── Block 5: AI Governance ──────────────────────────────────────────── */}
      <div className="space-y-3">
        <SectionDivider num="05" label="AI Governance" />
        <Card className="border-border bg-card opacity-80">
          <BlockHeader
            icon={Brain}
            color="bg-indigo-500/10"
            title={txt("Governança de IA", "AI Governance")}
            subtitle={txt(
              "Supervisão humana obrigatória, rastreabilidade de decisão e políticas de resposta",
              "Mandatory human oversight, decision traceability and response policies"
            )}
          />
          <CardContent className="space-y-0 divide-y divide-border/30">
            <DisabledToggle
              label={txt("Supervisão Humana Obrigatória", "Mandatory Human Oversight")}
              defaultChecked={true}
              sublabel={txt(
                "Toda decisão de alto risco requer confirmação de profissional de saúde",
                "Every high-risk decision requires confirmation from a healthcare professional"
              )}
            />
            <DisabledToggle
              label={txt("Trace de Justificativa de Decisão", "Decision Justification Trace")}
              defaultChecked={true}
              sublabel={txt(
                "SRM registra intent, stage, action e constraints de cada resposta gerada",
                "SRM records intent, stage, action and constraints of each generated response"
              )}
            />
            <DisabledInfoField
              label={txt("Política de Respostas Proibidas", "Prohibited Response Policy")}
              value={txt(
                "banned-responses-v1.3 · CFM Res. 2.299/21 · odontologia, psiquiatria",
                "banned-responses-v1.3 · CFM Res. 2.299/21 · dentistry, psychiatry"
              )}
            />
            <DisabledInfoField
              label={txt("Referência de Política", "Policy Reference")}
              value="https://policy.neurostack.global/docs/ai-governance/v2"
            />
          </CardContent>
        </Card>
      </div>

    </div>
  );
}
