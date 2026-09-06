import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { I18nProvider } from "@/context/i18n";
import KnowledgeBase from "./knowledge-base";

describe("<KnowledgeBase />", () => {
  it("presents Botox as a post-procedure document", () => {
    render(
      <I18nProvider>
        <KnowledgeBase />
      </I18nProvider>,
    );

    expect(screen.getByText("Protocolo_Pos_Procedimento_Botox.pdf")).toBeInTheDocument();
    expect(screen.queryByText("Protocolo_Pos_Operatorio_Botox.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("Regras_Internas_Clinica_Lumina.docx")).toBeInTheDocument();
  });
});
