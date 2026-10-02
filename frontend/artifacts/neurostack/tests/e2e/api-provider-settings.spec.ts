import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// ─────────────────────────────────────────────────────────────────────────────
// Fixtures
//
// Every value below is redacted or synthetic. No real capability token, no
// real bearer/JWT, no real Meta App Secret. The Z-API test explicitly asserts
// that the raw one-time URL is NOT visible before the user triggers a save,
// so this suite never captures a live token in screenshots, traces, or the
// serialized network mocks.
// ─────────────────────────────────────────────────────────────────────────────

interface CredentialFixture {
  zapi_instance_id: string | null;
  zapi_token_set: boolean;
  zapi_token_masked: string | null;
  zapi_inbound_token_digest_set: boolean;
  whatsapp_provider: string;
  whatsapp_readiness: string;
  evolution_api_url: string | null;
  evolution_instance_name: string | null;
  evolution_api_key_set: boolean;
  evolution_api_key_masked: string | null;
  meta_phone_number_id: string | null;
  meta_waba_id: string | null;
  meta_permanent_token_set: boolean;
  meta_permanent_token_masked: string | null;
  meta_app_secret_set: boolean;
  meta_app_secret_masked: string | null;
  meta_verify_token: string | null;
  uazapi_api_url: string | null;
  uazapi_instance_name: string | null;
  uazapi_token_set: boolean;
  uazapi_token_masked: string | null;
  llm_api_key_set: boolean;
  llm_api_key_masked: string | null;
  llm_provider: string;
  llm_readiness: string;
  llm_base_url: string | null;
  llm_model_name: string | null;
  whisper_api_key_set: boolean;
  whisper_api_key_masked: string | null;
  whisper_model: string | null;
  stt_provider: string | null;
  stt_readiness: string;
  stt_api_url: string | null;
  stt_api_key_set: boolean;
  stt_model_name: string | null;
  audio_max_seconds: number | null;
  multimodal_provider: string | null;
  multimodal_readiness: string;
  tts_provider: string | null;
  tts_readiness: string;
  tts_enabled: boolean;
  tts_reply_policy: "audio_input" | "all_inputs";
  apis_configured: boolean;
}

const CREDENTIAL_FIXTURE: CredentialFixture = {
  zapi_instance_id: "REDACTED-INSTANCE",
  zapi_token_set: true,
  zapi_token_masked: "abcd****wxyz",
  zapi_inbound_token_digest_set: false,
  whatsapp_provider: "zapi",
  whatsapp_readiness: "active",
  evolution_api_url: null,
  evolution_instance_name: null,
  evolution_api_key_set: false,
  evolution_api_key_masked: null,
  meta_phone_number_id: null,
  meta_waba_id: null,
  meta_permanent_token_set: false,
  meta_permanent_token_masked: null,
  meta_app_secret_set: false,
  meta_app_secret_masked: null,
  meta_verify_token: null,
  uazapi_api_url: null,
  uazapi_instance_name: null,
  uazapi_token_set: false,
  uazapi_token_masked: null,
  llm_api_key_set: true,
  llm_api_key_masked: "sk-pr****9xyz",
  llm_provider: "openai",
  llm_readiness: "active",
  llm_base_url: "https://api.openai.com/v1",
  llm_model_name: "gpt-4o-mini",
  whisper_api_key_set: true,
  whisper_api_key_masked: "sk-wh****9xyz",
  whisper_model: "whisper-1",
  stt_provider: "openai_whisper",
  stt_readiness: "active",
  stt_api_url: "https://api.openai.com/v1",
  stt_api_key_set: true,
  stt_model_name: "whisper-1",
  audio_max_seconds: 60,
  multimodal_provider: null,
  multimodal_readiness: "incomplete",
  tts_provider: null,
  tts_readiness: "incomplete",
  tts_enabled: false,
  tts_reply_policy: "audio_input",
  apis_configured: true,
};

const CONFIG_FIXTURE = {
  clinic_id: "test-clinic",
  clinic_name: "Clínica Demo",
  bot_active: true,
};

// Synthetic JWT — the payload decodes to a fake role/clinic. This string is
// hand-assembled so no real signing key is required; the frontend only reads
// the payload, never verifies the signature.
const FAKE_JWT_PAYLOAD = Buffer.from(
  JSON.stringify({
    sub: "test-clinic",
    role: "super_admin",
    clinic_id: "test-clinic",
    exp: 9999999999,
  }),
).toString("base64url");
const FAKE_JWT = `eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.${FAKE_JWT_PAYLOAD}.fake-signature`;

async function setupRoutes(
  page: Page,
  overrides: Partial<CredentialFixture> = {},
) {
  // Unroute any previously-registered handlers so tests that call this fn
  // twice (default + variant fixtures) do not shadow their own overrides
  // with the catch-all `**/v1/**` fallback from the first invocation.
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // Order matters: Playwright evaluates the most-recently-registered handler
  // first, so register broad catch-all patterns FIRST and the most specific
  // (credentials) LAST. That way the credentials override always wins over
  // the generic `**/v1/config**` and `**/v1/**` handlers.
  await page.route("**/v1/**", async (route) => {
    await route.fulfill({ json: {} });
  });
  await page.route("**/v1/admin/clinics**", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/v1/builder/clinics**", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/v1/config**", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: CONFIG_FIXTURE });
    } else {
      await route.fulfill({ json: { ok: true } });
    }
  });
  await page.route("**/v1/config/api-credentials**", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: { ...CREDENTIAL_FIXTURE, ...overrides } });
    } else {
      await route.fulfill({ json: { ok: true, apis_configured: true } });
    }
  });
}

async function loginAndNavigate(page: Page, tab = "integracoes") {
  await page.addInitScript((token) => {
    window.localStorage.setItem(
      "ns_auth",
      JSON.stringify({
        token,
        email: "e2e@example.com",
        clinicId: "test-clinic",
        tenantId: "test-clinic",
        userId: "test-clinic",
        builderId: null,
        role: "super_admin",
        viewingClinicId: "test-clinic",
        leadSubmitted: true,
      }),
    );
  }, FAKE_JWT);
  await page.goto(`/config?tab=${tab}`);
  // Wait for the tab content to render — a stable anchor for every state.
  await page.waitForSelector('[data-testid="api-settings-section"]', {
    timeout: 15_000,
  });
  // Disable any residual CSS animation/transition so pixels are stable.
  await page.addStyleTag({
    content: `*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }`,
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Baseline states — 5 states × 2 viewports = 10 PNGs
// ─────────────────────────────────────────────────────────────────────────────

test.describe("API Provider Settings — integracoes tab baselines", () => {
  test.beforeEach(async ({ page }) => {
    await setupRoutes(page);
  });

  test("01 closed — Z-API default state", async ({ page }, testInfo) => {
    await loginAndNavigate(page);
    await expect(
      page.locator('[data-testid="api-settings-section"]'),
    ).toBeVisible();
    await expect(page.locator('[data-testid^="provider-editor-"]')).toHaveCount(0);
    await expect(page).toHaveScreenshot(`01-closed-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("02 Meta expanded — incomplete credentials", async ({ page }, testInfo) => {
    await setupRoutes(page, {
      whatsapp_provider: "meta_cloud",
      meta_phone_number_id: "123456789012345",
      meta_waba_id: "987654321098765",
      meta_permanent_token_set: true,
      meta_permanent_token_masked: "EAAG****9xyz",
      meta_app_secret_set: false,
      meta_verify_token: "verify-token-demo",
      whatsapp_readiness: "signature_pending",
    });
    await loginAndNavigate(page);
    const metaCard = page.getByTestId("provider-card-whatsapp-meta_cloud");
    await expect(metaCard).toHaveAttribute("data-card-state", "incomplete");
    await metaCard.click();
    await expect(page.getByTestId("provider-editor-whatsapp-meta_cloud")).toBeVisible();
    await expect(page.getByTestId("field-meta_phone_number_id"))
      .toHaveValue("123456789012345");
    await expect(page).toHaveScreenshot(`02-meta-expanded-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("03 LLM expanded — OpenAI configured", async ({ page }, testInfo) => {
    await loginAndNavigate(page);
    // OpenAI is the default LLM provider — click to (re)assert selection and
    // ensure the panel is scrolled into view for the screenshot.
    await page.getByTestId("provider-card-llm-openai").click();
    await page.getByTestId("provider-card-llm-openai").scrollIntoViewIfNeeded();
    await expect(page.getByTestId("provider-editor-llm-openai")).toBeVisible();
    await expect(page).toHaveScreenshot(`03-llm-expanded-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("04 STT expanded — OpenAI Whisper configured", async ({ page }, testInfo) => {
    await loginAndNavigate(page);
    await page.getByTestId("provider-card-stt-openai_whisper").click();
    await page.getByTestId("provider-card-stt-openai_whisper").scrollIntoViewIfNeeded();
    await expect(page.getByTestId("provider-editor-stt-openai_whisper")).toBeVisible();
    await expect(page).toHaveScreenshot(`04-stt-expanded-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("05 Z-API configured — secrets remain write-only", async ({
    page,
  }, testInfo) => {
    await setupRoutes(page, {
      whatsapp_provider: "zapi",
      zapi_instance_id: "REDACTED-INSTANCE",
      zapi_token_set: true,
      zapi_inbound_token_digest_set: true,
    });
    await loginAndNavigate(page);
    const zapiCard = page.getByTestId("provider-card-whatsapp-zapi");
    await expect(zapiCard).toHaveAttribute("data-card-state", "configured");
    await zapiCard.click();
    await expect(page.getByTestId("provider-editor-whatsapp-zapi")).toBeVisible();
    await expect(page.getByTestId("field-zapi_token")).toHaveValue("");
    await expect(page.getByTestId("field-zapi_token"))
      .toHaveAttribute("placeholder", "•••••••• (keep current)");
    await expect(page.locator("body")).not.toContainText("abcd****wxyz");
    await expect(page).toHaveScreenshot(`05-zapi-configured-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Layout invariants — bounds, single-expanded, no coming-soon interaction
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Layout invariants", () => {
  test("renders the API and voice hierarchy in product order", async ({ page }) => {
    await setupRoutes(page);
    await loginAndNavigate(page);

    const hierarchyIds = await page.locator(
      '[data-testid="api-settings-section"] > [data-testid]',
    ).evaluateAll((elements) => elements
      .map((element) => element.getAttribute("data-testid"))
      .filter((testId) => testId?.startsWith("api-capability-")
        || testId === "voice-module-heading"
        || testId === "voice-response-controls"));
    expect(hierarchyIds).toEqual([
      "api-capability-whatsapp",
      "api-capability-llm",
      "api-capability-multimodal",
      "voice-module-heading",
      "api-capability-stt",
      "api-capability-tts",
      "voice-response-controls",
    ]);
    await expect(page.getByTestId("voice-module-heading")).toHaveText("Voice Module");
    await expect(page.getByTestId("voice-response-controls")).toHaveCount(1);
    await expect(page.getByRole("heading", { name: "Voice Triggers" })).toBeVisible();
    await expect(page.getByText("More voice triggers coming soon", { exact: true })).toBeVisible();
  });

  test("cards stay within viewport bounds and do not overlap", async ({
    page,
  }) => {
    await setupRoutes(page);
    await loginAndNavigate(page);
    const viewport = page.viewportSize();
    if (!viewport) test.skip();
    const cards = await page
      .locator(
        '[data-testid^="provider-card-"]',
      )
      .all();
    expect(cards.length).toBeGreaterThan(0);
    for (const card of cards) {
      const box = await card.boundingBox();
      if (!box) continue;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport!.width + 1);
    }
  });

  test("only one WhatsApp provider is active at a time", async ({ page }) => {
    await setupRoutes(page);
    await loginAndNavigate(page);
    await page.getByTestId("provider-card-whatsapp-meta_cloud").click();
    await expect(page.getByTestId("provider-editor-whatsapp-meta_cloud")).toHaveCount(1);
    await page.getByTestId("provider-card-whatsapp-zapi").click();
    await expect(page.getByTestId("provider-editor-whatsapp-meta_cloud")).toHaveCount(0);
    await expect(page.getByTestId("provider-editor-whatsapp-zapi")).toHaveCount(1);
  });

  test("coming-soon LLM providers are non-interactive", async ({ page }) => {
    await setupRoutes(page);
    await loginAndNavigate(page);
    const comingSoon = page.locator('[data-card-state="coming_soon"]');
    await expect(comingSoon).toHaveCount(7);
    for (let i = 0; i < await comingSoon.count(); i++) {
      await expect(comingSoon.nth(i)).toHaveJSProperty("tagName", "ARTICLE");
    }
    await expect(page.locator('[data-card-state="coming_soon"] button')).toHaveCount(0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Meta readiness — variant-specific assertions
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Meta credential readiness", () => {
  test("keeps incomplete credentials write-only when app secret is missing", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page, {
      whatsapp_provider: "meta_cloud",
      meta_phone_number_id: "123456789012345",
      meta_permanent_token_set: true,
      meta_app_secret_set: false,
      whatsapp_readiness: "signature_pending",
    });
    await loginAndNavigate(page);
    const metaCard = page.getByTestId("provider-card-whatsapp-meta_cloud");
    await expect(metaCard).toHaveAttribute("data-card-state", "incomplete");
    await metaCard.click();
    await expect(page.getByTestId("field-meta_app_secret")).toHaveValue("");
    await expect(page.getByTestId("field-meta_app_secret")).not.toHaveAttribute("placeholder");
  });

  test("marks Meta configured once the complete bundle is active", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page, {
      whatsapp_provider: "meta_cloud",
      meta_phone_number_id: "123456789012345",
      meta_permanent_token_set: true,
      meta_app_secret_set: true,
      whatsapp_readiness: "active",
    });
    await loginAndNavigate(page);
    const metaCard = page.getByTestId("provider-card-whatsapp-meta_cloud");
    await expect(metaCard).toHaveAttribute("data-card-state", "configured");
    await metaCard.click();
    await expect(page.getByTestId("field-meta_app_secret"))
      .toHaveAttribute("placeholder", "•••••••• (keep current)");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Z-API capability URL — one-time-only, redaction guarantees
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Z-API secret handling", () => {
  test("stored tokens never hydrate into the editor", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page, {
      whatsapp_provider: "zapi",
      zapi_instance_id: "REDACTED-INSTANCE",
      zapi_token_set: true,
    });
    await loginAndNavigate(page);
    await page.getByTestId("provider-card-whatsapp-zapi").click();
    await expect(page.getByTestId("field-zapi_token")).toHaveValue("");
    await expect(page.getByTestId("field-zapi_client_token")).toHaveValue("");
    await expect(page.locator("body")).not.toContainText("abcd****wxyz");
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Navigation — /voice-ai redirect
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Legacy /voice-ai redirect", () => {
  test("navigates to /config?tab=integracoes", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page);
    await page.addInitScript((token) => {
      window.localStorage.setItem(
        "ns_auth",
        JSON.stringify({
          token,
          role: "clinic_admin",
          clinicId: "test-clinic",
        }),
      );
    }, FAKE_JWT);
    await page.goto("/voice-ai");
    await page.waitForURL(/\/config\?tab=integracoes/, { timeout: 10_000 });
    expect(page.url()).toMatch(/tab=integracoes/);
  });
});
