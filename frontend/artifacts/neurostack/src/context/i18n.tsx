import {
  createContext,
  useContext,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import { en, type TranslationKey } from "@/locales/en";
import { pt } from "@/locales/pt";

export type Locale = "en" | "pt";

type Translations = Record<TranslationKey, string>;
const LOCALES: Record<Locale, Translations> = { en, pt };

const LS_KEY = "ns_locale";

function loadLocale(): Locale {
  try {
    const saved = localStorage.getItem(LS_KEY);
    if (saved === "en" || saved === "pt") return saved;
  } catch {
    // ignore
  }
  return "pt"; // default to Portuguese for pilot clinics
}

interface I18nContextValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: TranslationKey) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(loadLocale);

  const setLocale = useCallback((l: Locale) => {
    try {
      localStorage.setItem(LS_KEY, l);
    } catch {
      // ignore
    }
    setLocaleState(l);
  }, []);

  const t = useCallback(
    (key: TranslationKey) => LOCALES[locale][key],
    [locale],
  );

  return (
    <I18nContext.Provider value={{ locale, setLocale, t }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useI18n() {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be inside I18nProvider");
  return ctx;
}
