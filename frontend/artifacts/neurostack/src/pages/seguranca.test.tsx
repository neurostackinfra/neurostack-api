import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { I18nProvider, type Locale } from "@/context/i18n";
import Seguranca from "./seguranca";

type Copy = { pt: string; en: string };

// Copy from the approved DESIGN (slm-cortex-seguranca-mock, "G2, Segurança & Compliance").
const TITLE: Copy = { pt: "Segurança & Compliance", en: "Security & Compliance" };
const SUBTITLE: Copy = {
  pt: "O que já está ativo e o que está em andamento",
  en: "What is already active and what is in progress",
};
const STRIP: Copy = { pt: "Em preparação para o SOC 2", en: "Preparing for SOC 2" };
const ACTIVE_HEADER: Copy = { pt: "Ativo", en: "Active" };
const IN_PROGRESS_HEADER: Copy = { pt: "Em andamento", en: "In progress" };

const ACTIVE: readonly Copy[] = [
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

const IN_PROGRESS: readonly Copy[] = [
  { pt: "Relatório SOC 2", en: "SOC 2 report" },
  { pt: "Exclusão automática de dados antigos", en: "Automatic deletion of old data" },
  { pt: "Login em duas etapas (2FA)", en: "Two-step login (2FA)" },
  { pt: "Detecção de ameaças na infraestrutura AWS", en: "Threat detection on the AWS infrastructure" },
];

const REMOVED_TEXT = [
  "99.99%",
  "HIPAA",
  "GDPR",
  "Jurisdições",
  "Jurisdictions",
  "Enterprise",
  "Policy Layer",
  "policy.neurostack.global",
  "consent-log",
  "banned-responses",
  "Minimização",
  "Minimization",
  "dados sensíveis",
  "sensitive data",
  "Certificação SOC 2",
  "SOC 2 certification",
];

const normalize = (text: string) => text.replace(/ /g, " ").replace(/\s+/g, " ").trim();
const opts = { normalizer: normalize };

function renderPage(locale: Locale) {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <Seguranca />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe("Security & Compliance page", () => {
  it.each(["pt", "en"] as const)("renders the static active / in-progress panel in %s", (locale) => {
    const other: Locale = locale === "pt" ? "en" : "pt";
    const { container } = renderPage(locale);

    for (const copy of [TITLE, SUBTITLE, STRIP, ACTIVE_HEADER, IN_PROGRESS_HEADER]) {
      expect(screen.getByText(copy[locale], opts)).toBeInTheDocument();
    }

    const activeList = screen.getByRole("list", { name: ACTIVE_HEADER[locale] });
    const activeItems = within(activeList).getAllByRole("listitem");
    expect(activeItems).toHaveLength(9);
    expect(activeItems.map((li) => normalize(li.textContent ?? ""))).toEqual(ACTIVE.map((c) => c[locale]));

    const inProgressList = screen.getByRole("list", { name: IN_PROGRESS_HEADER[locale] });
    const inProgressItems = within(inProgressList).getAllByRole("listitem");
    expect(inProgressItems).toHaveLength(4);
    expect(inProgressItems.map((li) => normalize(li.textContent ?? ""))).toEqual(IN_PROGRESS.map((c) => c[locale]));

    for (const copy of [...ACTIVE, ...IN_PROGRESS]) {
      expect(screen.getByText(copy[locale], opts)).toBeInTheDocument();
    }

    const allCopy = [TITLE, SUBTITLE, STRIP, ACTIVE_HEADER, IN_PROGRESS_HEADER, ...ACTIVE, ...IN_PROGRESS];
    for (const copy of allCopy) {
      if (copy.pt === copy.en) continue;
      expect(screen.queryByText(copy[other], opts), copy[other]).not.toBeInTheDocument();
    }

    const pageText = normalize(container.textContent ?? "");
    expect(pageText).not.toContain("·");
    for (const text of REMOVED_TEXT) {
      expect(pageText, text).not.toContain(text);
    }
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(container.querySelector("select")).toBeNull();
  });
});
