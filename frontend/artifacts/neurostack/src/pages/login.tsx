import { useState } from "react";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { Lock, Mail, Globe } from "lucide-react";
import logoSrc from "@assets/logo_png_1775411054369.png";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [hasError, setHasError] = useState(false);
  const [isPending, setIsPending] = useState(false);

  const { login } = useAuth();
  const { t, locale, setLocale } = useI18n();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setHasError(false);
    setIsPending(true);

    try {
      await login(email, password);
    } catch {
      setHasError(true);
    } finally {
      setIsPending(false);
    }
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center bg-sidebar px-4">
      <button
        onClick={() => setLocale(locale === "en" ? "pt" : "en")}
        className="absolute top-4 right-4 flex items-center gap-1.5 text-xs font-medium text-sidebar-foreground/60 hover:text-sidebar-foreground transition-colors"
        title="Switch language"
      >
        <Globe className="h-3.5 w-3.5" />
        <span className={locale === "pt" ? "font-bold text-sidebar-foreground" : ""}>PT</span>
        <span className="opacity-40 mx-0.5">·</span>
        <span className={locale === "en" ? "font-bold text-sidebar-foreground" : ""}>EN</span>
      </button>

      <div className="w-full max-w-md space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <img src={logoSrc} alt="NeuroStack" className="h-48 w-auto object-contain" />
          <p className="text-sm text-muted-foreground">
            {t("app_tagline")}
          </p>
        </div>

        <Card>
          <CardHeader className="text-center">
            <CardTitle className="text-lg">{t("login_title")}</CardTitle>
            <CardDescription>
              {t("login_subtitle")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-1">
                <Label htmlFor="email">{t("login_email")}</Label>
                <div className="relative">
                  <Mail className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    id="email"
                    type="email"
                    className="pl-9"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div className="space-y-1">
                <Label htmlFor="password">{t("login_password")}</Label>
                <div className="relative">
                  <Lock className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    id="password"
                    type="password"
                    className="pl-9"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                  />
                </div>
              </div>

              {hasError && (
                <p className="text-sm text-destructive">{t("login_error")}</p>
              )}

              <Button
                type="submit"
                className="w-full"
                disabled={isPending}
              >
                {isPending ? "Entrando..." : "Acessar Plataforma"}
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
