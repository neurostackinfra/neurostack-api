import { useState } from "react";
import { Building2, Mail, Phone, User, Loader2, Brain } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/context/auth";

const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ─── Translations ─────────────────────────────────────────────────────────────

const T = {
  PT: {
    title: "Bem-vindo ao Demo Exclusivo",
    subtitle: "Para acessar a plataforma, precisamos de algumas informações de contato.",
    labelNome: "Nome Completo",
    placeholderNome: "João Silva",
    labelEmail: "E-mail Profissional",
    placeholderEmail: "joao@empresa.com.br",
    labelEmpresa: "Empresa / Fundo",
    placeholderEmpresa: "Fundo Capital XYZ / Clínica ABC",
    labelTelefone: "Telefone / WhatsApp",
    placeholderTelefone: "+55 11 99999-9999",
    submit: "Acessar Demonstração",
    saving: "Salvando...",
    footer: "Seus dados são usados exclusivamente para qualificar este contato comercial.",
    errRequired: "Todos os campos são obrigatórios.",
    errEmail: "Informe um e-mail válido.",
    errNetwork: "Erro de rede. Tente novamente.",
    errGeneric: "Erro ao salvar. Tente novamente.",
  },
  EN: {
    title: "Welcome to the Exclusive Demo",
    subtitle: "To access the platform, we need a few contact details.",
    labelNome: "Full Name",
    placeholderNome: "John Smith",
    labelEmail: "Professional Email",
    placeholderEmail: "john@company.com",
    labelEmpresa: "Company / Fund",
    placeholderEmpresa: "Capital Fund XYZ / Clinic ABC",
    labelTelefone: "Phone / WhatsApp",
    placeholderTelefone: "+1 (555) 000-0000",
    submit: "Access Demo",
    saving: "Saving...",
    footer: "Your data is used exclusively to qualify this commercial contact.",
    errRequired: "All fields are required.",
    errEmail: "Please enter a valid email address.",
    errNetwork: "Network error. Please try again.",
    errGeneric: "Failed to save. Please try again.",
  },
} as const;

type Lang = keyof typeof T;

// ─── Component ────────────────────────────────────────────────────────────────

export function DemoLeadGate() {
  const { setLeadSubmitted } = useAuth();
  const [lang, setLang] = useState<Lang>("EN");
  const [nome, setNome] = useState("");
  const [emailContato, setEmailContato] = useState("");
  const [empresa, setEmpresa] = useState("");
  const [telefone, setTelefone] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const t = T[lang];

  const token = (() => {
    try {
      return (JSON.parse(localStorage.getItem("ns_auth") ?? "{}") as { token?: string }).token ?? "";
    } catch { return ""; }
  })();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!nome.trim() || !emailContato.trim() || !empresa.trim() || !telefone.trim()) {
      setError(t.errRequired);
      return;
    }
    if (!EMAIL_REGEX.test(emailContato.trim())) {
      setError(t.errEmail);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/v1/demo-leads`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ name: nome, email: emailContato, company: empresa, phone: telefone }),
      });
      if (!res.ok) {
        const data = await res.json() as { error?: string };
        setError(data.error ?? t.errGeneric);
        return;
      }
    } catch {
      // Endpoint may not exist yet — log locally and proceed
      console.warn("demo-leads endpoint unavailable, proceeding without save");
    } finally {
      setLoading(false);
    }
    // Always grant access after form submission
    setLeadSubmitted();
  };

  return (
    /* Outer: scroll container — overflow-y-auto prevents top-clipping on short screens */
    <div className="fixed inset-0 z-50 bg-black/95 overflow-y-auto">
      {/* Inner: flex column centering with generous vertical padding */}
      <div className="min-h-full w-full flex flex-col items-center justify-center py-12 px-4">
        <div className="w-full max-w-md">

          {/* ── Logo / Brand ─────────────────────────────────────────────── */}
          <div className="flex items-center justify-center gap-2 mb-8">
            <div className="p-2 rounded-xl bg-violet-500/20 border border-violet-500/30">
              <Brain className="h-6 w-6 text-violet-400" />
            </div>
            <span className="text-xl font-bold text-white">NeuroStack</span>
            <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/30 ml-1">
              DEMO
            </span>
          </div>

          {/* ── Card ─────────────────────────────────────────────────────── */}
          <div className="rounded-2xl border border-border bg-card p-8 shadow-2xl shadow-black/50">

            {/* Language Toggle */}
            <div className="flex justify-center mb-6">
              <div className="inline-flex rounded-lg border border-border bg-muted p-1 gap-1">
                {(["PT", "EN"] as Lang[]).map((l) => (
                  <button
                    key={l}
                    type="button"
                    onClick={() => { setLang(l); setError(null); }}
                    className={[
                      "px-5 py-1.5 rounded-md text-sm font-semibold transition-all duration-150 select-none",
                      lang === l
                        ? "bg-violet-600 text-white shadow-sm"
                        : "text-muted-foreground hover:text-foreground",
                    ].join(" ")}
                  >
                    {l}
                  </button>
                ))}
              </div>
            </div>

            {/* Header */}
            <div className="mb-6 text-center">
              <h1 className="text-xl font-bold text-foreground mb-1">
                {t.title}
              </h1>
              <p className="text-sm text-muted-foreground">
                {t.subtitle}
              </p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">

              {/* Nome */}
              <div className="space-y-1.5">
                <Label htmlFor="nome" className="text-foreground font-medium">
                  {t.labelNome}
                </Label>
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    id="nome"
                    value={nome}
                    onChange={(e) => setNome(e.target.value)}
                    placeholder={t.placeholderNome}
                    className="pl-9"
                    autoFocus
                    disabled={loading}
                  />
                </div>
              </div>

              {/* E-mail Profissional */}
              <div className="space-y-1.5">
                <Label htmlFor="emailContato" className="text-foreground font-medium">
                  {t.labelEmail}
                </Label>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    id="emailContato"
                    type="email"
                    value={emailContato}
                    onChange={(e) => setEmailContato(e.target.value)}
                    placeholder={t.placeholderEmail}
                    className="pl-9"
                    disabled={loading}
                  />
                </div>
              </div>

              {/* Empresa */}
              <div className="space-y-1.5">
                <Label htmlFor="empresa" className="text-foreground font-medium">
                  {t.labelEmpresa}
                </Label>
                <div className="relative">
                  <Building2 className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    id="empresa"
                    value={empresa}
                    onChange={(e) => setEmpresa(e.target.value)}
                    placeholder={t.placeholderEmpresa}
                    className="pl-9"
                    disabled={loading}
                  />
                </div>
              </div>

              {/* Telefone */}
              <div className="space-y-1.5">
                <Label htmlFor="telefone" className="text-foreground font-medium">
                  {t.labelTelefone}
                </Label>
                <div className="relative">
                  <Phone className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    id="telefone"
                    value={telefone}
                    onChange={(e) => setTelefone(e.target.value)}
                    placeholder={t.placeholderTelefone}
                    className="pl-9"
                    disabled={loading}
                  />
                </div>
              </div>

              {error && (
                <p className="text-sm text-destructive font-medium">{error}</p>
              )}

              <Button
                type="submit"
                className="w-full gap-2 mt-2"
                disabled={loading}
              >
                {loading ? (
                  <><Loader2 className="h-4 w-4 animate-spin" /> {t.saving}</>
                ) : (
                  t.submit
                )}
              </Button>
            </form>

            <p className="text-[11px] text-muted-foreground text-center mt-4">
              {t.footer}
            </p>
          </div>
          {/* end card */}

        </div>
        {/* end max-w-md */}
      </div>
      {/* end centering wrapper */}
    </div>
    /* end scroll container */
  );
}
