import { BookOpen, Video, FileText, GraduationCap, Lock } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useI18n } from "@/context/i18n";

export default function Onboarding() {
  const { locale } = useI18n();
  const txt = (p: string, e: string) => locale === "en" ? e : p;

  const MODULES = [
    {
      icon: BookOpen,
      badge: txt("Leitura", "Reading"),
      badgeColor: "text-blue-400 border-blue-500/20 bg-blue-500/10",
      title: txt("Mapa Mental da Operação", "Operation Mind Map"),
    },
    {
      icon: Video,
      badge: txt("Vídeo", "Video"),
      badgeColor: "text-violet-400 border-violet-500/20 bg-violet-500/10",
      title: txt("Treinamentos em Vídeo", "Video Training"),
    },
    {
      icon: FileText,
      badge: "E-book",
      badgeColor: "text-indigo-400 border-indigo-500/20 bg-indigo-500/10",
      title: txt("E-book: Boas Práticas de IA na Saúde", "E-book: AI Best Practices in Healthcare"),
    },
  ];

  return (
    <div className="space-y-8 max-w-3xl">

      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-muted">
          <GraduationCap className="h-5 w-5 text-muted-foreground" />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-foreground">
            NeuroStack Academy & Onboarding
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            {txt(
              "Materiais de capacitação para administradores de clínicas",
              "Training materials for clinic administrators"
            )}
          </p>
        </div>
        <Badge
          variant="outline"
          className="ml-auto shrink-0 text-[10px] font-bold uppercase tracking-wider text-muted-foreground border-border"
        >
          <Lock className="h-2.5 w-2.5 mr-1" />
          {txt("Em breve", "Coming soon")}
        </Badge>
      </div>

      {/* Module cards */}
      <div className="grid gap-4 md:grid-cols-3">
        {MODULES.map((mod) => (
          <Card
            key={mod.title}
            className="relative flex flex-col border border-border/50 bg-card/50 opacity-60 cursor-not-allowed select-none"
          >
            <CardContent className="flex flex-col items-center gap-3 py-8 px-5 text-center">
              <div className="flex h-11 w-11 items-center justify-center rounded-lg bg-muted">
                <mod.icon className="h-5 w-5 text-muted-foreground" />
              </div>
              <Badge variant="outline" className={`text-[10px] font-semibold ${mod.badgeColor}`}>
                {mod.badge}
              </Badge>
              <p className="text-sm font-medium text-foreground leading-snug">
                {mod.title}
              </p>
            </CardContent>
          </Card>
        ))}
      </div>

      <p className="text-xs text-muted-foreground text-center">
        {txt(
          "Esta seção estará disponível em breve para todos os usuários da plataforma.",
          "This section will be available soon for all platform users."
        )}
      </p>
    </div>
  );
}
