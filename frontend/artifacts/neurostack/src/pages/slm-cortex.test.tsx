import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { I18nProvider, type Locale } from "@/context/i18n";
import SlmCortex, { CORTEX_PRICES_PER_M, EXAMPLE_VOLUME_M, buildCostComparison } from "./slm-cortex";

// Copy from the approved DESIGN (slm-cortex-seguranca-mock, "G2, SLM Cortex"), in table order.
// Spaces here are plain spaces; the rendered text has U+00A0 after "US$" and is normalized below.
const ROWS: ReadonlyArray<{ pt: string; en: string }> = [
  { pt: "NeuroStack SLM Cortex", en: "NeuroStack SLM Cortex" },
  { pt: "Motor de linguagem próprio da NeuroStack, em desenvolvimento", en: "NeuroStack's own language engine, in development" },
  { pt: "Roadmap · Alpha", en: "Roadmap · Alpha" },
  { pt: "Os valores desta tela são uma projeção.", en: "The figures on this page are a projection." },
  { pt: "Comparativo de custo por 1M de tokens", en: "Cost comparison per 1M tokens" },
  { pt: "GPT (OpenAI)", en: "GPT (OpenAI)" },
  { pt: "US$ 5,00", en: "$5.00" },
  { pt: "Dropout", en: "Dropout" },
  { pt: "US$ 2,50", en: "$2.50" },
  { pt: "SLM Cortex", en: "SLM Cortex" },
  { pt: "US$ 0,50", en: "$0.50" },
  { pt: "Economia mensal", en: "Monthly savings" },
  { pt: "US$ 135,00", en: "$135.00" },
  { pt: "vs GPT, com 30 milhões de tokens/mês", en: "vs GPT, at 30 million tokens/month" },
  { pt: "Redução de custo", en: "Cost reduction" },
  { pt: "90%", en: "90%" },
  { pt: "em relação ao GPT", en: "compared to GPT" },
  {
    pt: "Custo mensal com 30 milhões de tokens: GPT US$ 150,00 · Dropout US$ 75,00 · SLM Cortex US$ 15,00",
    en: "Monthly cost at 30 million tokens: GPT $150.00 · Dropout $75.00 · SLM Cortex $15.00",
  },
  {
    pt: "Quando o Cortex não tiver confiança suficiente, a conversa seguirá automaticamente para o GPT ou o Dropout.",
    en: "When Cortex is not confident enough, the conversation will automatically go to GPT or Dropout.",
  },
  { pt: "Especialidades previstas", en: "Planned specialties" },
  { pt: "Odontologia Estética", en: "Aesthetic Dentistry" },
  { pt: "Dermatologia & Estética", en: "Dermatology & Aesthetics" },
  { pt: "Harmonização Facial", en: "Facial Harmonization" },
  { pt: "Cirurgia Plástica", en: "Plastic Surgery" },
  { pt: "Ortopedia", en: "Orthopedics" },
  { pt: "Oftalmologia", en: "Ophthalmology" },
];

const REMOVED_TEXT = [
  "cortex.neurostack.local",
  "Gerar API Key",
  "Generate Cortex API Key",
  "< 180ms",
  "Domínios Treinados",
  "Trained Domains",
  "Zero Data Egress",
  "10×",
  "GPT-4o",
];

const normalize = (text: string) => text.replace(/ /g, " ").replace(/\s+/g, " ").trim();
const opts = { normalizer: normalize };

function renderPage(locale: Locale) {
  localStorage.setItem("ns_locale", locale);
  return render(
    <I18nProvider>
      <SlmCortex />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe("buildCostComparison", () => {
  it("computes the screen's figures from the price table", () => {
    const c = buildCostComparison(CORTEX_PRICES_PER_M, EXAMPLE_VOLUME_M);
    expect(c.monthlySavings).toBeCloseTo(135);
    expect(c.costReduction).toBeCloseTo(0.9);
    expect(c.providers.gpt.monthlyCost).toBeCloseTo(150);
    expect(c.providers.dropout.monthlyCost).toBeCloseTo(75);
    expect(c.providers.cortex.monthlyCost).toBeCloseTo(15);
    expect(c.providers.gpt.barFraction).toBeCloseTo(1);
    expect(c.providers.dropout.barFraction).toBeCloseTo(0.5);
    expect(c.providers.cortex.barFraction).toBeCloseTo(0.1);
    expect(c.providers.gpt.price).toBe(5);
    expect(c.providers.dropout.price).toBe(2.5);
    expect(c.providers.cortex.price).toBe(0.5);
  });

  it("stays coherent with a second price table", () => {
    const c = buildCostComparison({ gpt: 4, dropout: 2, cortex: 1 }, 10);
    expect(c.monthlySavings).toBeCloseTo(30);
    expect(c.costReduction).toBeCloseTo(0.75);
    expect(c.providers.gpt.monthlyCost).toBeCloseTo(40);
    expect(c.providers.dropout.monthlyCost).toBeCloseTo(20);
    expect(c.providers.cortex.monthlyCost).toBeCloseTo(10);
    expect(c.providers.gpt.barFraction).toBeCloseTo(1);
    expect(c.providers.dropout.barFraction).toBeCloseTo(0.5);
    expect(c.providers.cortex.barFraction).toBeCloseTo(0.25);
  });
});

describe("SLM Cortex page", () => {
  it.each(["pt", "en"] as const)("renders exactly the DESIGN copy, in table order, in %s", (locale) => {
    const other: Locale = locale === "pt" ? "en" : "pt";
    const { container } = renderPage(locale);

    const elements = ROWS.map((row) => screen.getByText(row[locale], opts));
    for (let i = 1; i < elements.length; i += 1) {
      const position = elements[i - 1].compareDocumentPosition(elements[i]);
      expect(position & Node.DOCUMENT_POSITION_FOLLOWING, `row ${i} after row ${i - 1}`).toBeTruthy();
    }

    for (const row of ROWS) {
      if (row.pt === row.en) continue;
      expect(screen.queryByText(row[other], opts), row[other]).not.toBeInTheDocument();
    }

    const pageText = normalize(container.textContent ?? "");
    for (const text of REMOVED_TEXT) {
      expect(pageText, text).not.toContain(text);
    }
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
