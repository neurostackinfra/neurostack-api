# Wish: Imagens e arquivos no canal Evolution (paridade)

| Field | Value |
|-------|-------|
| **Status** | DRAFT |
| **Slug** | `whatsapp-image-understanding-evolution` |
| **Date** | 2026-11-07 |
| **Author** | Claude (stub de acompanhamento de `whatsapp-image-understanding`) |
| **Appetite** | small |
| **Branch** | `wish/whatsapp-image-understanding-evolution` |
| **Repos touched** | neurostack-api |
| **Design** | _No brainstorm — direct wish_ |

## Summary

Stub. Leva para o canal Evolution o que `whatsapp-image-understanding` entrega em uazapi, Z-API e
Meta: imagem descrita pelo OpenRouter e fluxo "arquivo não analisado" para documento, vídeo e imagem
não descrita. Hoje o handler Evolution descarta como `non-text` tudo que não é `conversation` ou
`extendedTextMessage` (`src/api/routes/webhook.py`, `evolution_webhook`), inclusive áudio. O
repositório também não tem payload de mídia capturado nem helper de download para esse canal.

## Scope

### IN

- Grupo 1 (descoberta, limitado): capturar numa instância Evolution real um `messages.upsert` com
  `imageMessage`, `documentMessage`, `videoMessage` e visualização única, mais a resposta de
  download de mídia (`/chat/getBase64FromMediaMessage/{instance}`). Guardar com dados pessoais
  removidos em `tests/fixtures/evolution/`.
- Os grupos seguintes serão detalhados depois da evidência: download seguro, ramo de imagem e fluxo
  de arquivo no handler Evolution, reusando os helpers de `whatsapp-image-understanding`.

### OUT

- Áudio no Evolution (transcrição): é outra lacuna, que fica num wish próprio se for pedida.
- Qualquer mudança em uazapi, Z-API ou Meta.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Começar pela evidência, não pelo código | Não há payload nem contrato de download provados (revisão de plano de `whatsapp-image-understanding`, 2026-11-07); misturar descoberta com implementação trava a revisão. |

## Simplicity Case

- **Simplest complete design:** reusar integralmente os helpers do wish pai; aqui, só a leitura do payload e o download ficam específicos do Evolution.
- **Added machinery:** nenhuma até a evidência existir.
- **Deferred until measured:** todos os grupos de implementação, até o Grupo 1 entregar os fixtures.
- **Complexity removed:** nenhum helper paralelo aos do wish pai.

## Dependencies

**depends-on:** whatsapp-image-understanding
**blocks:** none

## Success Criteria

- [ ] Fixtures reais (com dados pessoais removidos) de imagem, documento, vídeo, visualização única e download de mídia do Evolution estão em `tests/fixtures/evolution/`, com `_meta` de origem e data.
- [ ] Os grupos de implementação estão detalhados neste WISH.md e aprovados em revisão de plano.

## Execution Strategy

### Wave 1 (sequential)

| Group | Agent | Complexity | Model | Description |
|-------|-------|------------|-------|-------------|
| 1 | engineer | 1 — sem teste determinístico (+1): depende de uma instância externa | `engineer-trivial` / low | Capturar e redigir os payloads do Evolution |

## Execution Groups

### Group 1: Evidência de mídia do Evolution

**Goal:** Ter payloads reais de mídia do Evolution versionados como fixtures.

**Deliverables:**
1. `tests/fixtures/evolution/media-payloads.json` com `image`, `document`, `video` e `view_once`, e `tests/fixtures/evolution/media-download.json` com a resposta do download; ambos com dados pessoais removidos.

**Acceptance Criteria:**
- [ ] Os arquivos são JSON válido, cada um com `_meta.captured_from` e `_meta.captured_at`, e nenhum número de telefone real.

**Validation:**
```bash
python3 -c "import json,sys; [json.load(open(p)) for p in sys.argv[1:]]" tests/fixtures/evolution/media-payloads.json tests/fixtures/evolution/media-download.json
! grep -qE '55[0-9]{10,11}' tests/fixtures/evolution/media-payloads.json tests/fixtures/evolution/media-download.json
```

**depends-on:** none

---

## QA Criteria

- [ ] A definir quando os grupos de implementação forem detalhados.

---

## Assumptions / Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Não há instância Evolution disponível para capturar o payload | Medium | O wish fica em DRAFT até existir uma instância; o wish pai não depende deste. |

---

## Review Results

_The read-only reviewer returns evidence; the invoking orchestrator appends a timestamped block here after plan, execution, and PR reviews._

---

## Files to Create/Modify

```
tests/fixtures/evolution/media-payloads.json   # G1 (new)
tests/fixtures/evolution/media-download.json   # G1 (new)
```
