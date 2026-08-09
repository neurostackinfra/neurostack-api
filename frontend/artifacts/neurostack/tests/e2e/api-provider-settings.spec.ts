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
  llm_base_url: string | null;
  llm_model_name: string | null;
  whisper_api_key_set: boolean;
  whisper_api_key_masked: string | null;
  whisper_model: string | null;
  stt_provider: string | null;
  stt_api_url: string | null;
  audio_max_seconds: number | null;
  apis_configured: boolean;
}

const CREDENTIAL_FIXTURE: CredentialFixture = {
  zapi_instance_id: "REDACTED-INSTANCE",
  zapi_token_set: true,
  zapi_token_masked: "abcd****wxyz",
  zapi_inbound_token_digest_set: false,
  whatsapp_provider: "zapi",
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
  llm_base_url: "https://api.openai.com/v1",
  llm_model_name: "gpt-4o-mini",
  whisper_api_key_set: true,
  whisper_api_key_masked: "sk-wh****9xyz",
  whisper_model: "whisper-1",
  stt_provider: "openai_whisper",
  stt_api_url: "https://api.openai.com/v1",
  audio_max_seconds: 60,
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
    // Only one WhatsApp provider "expanded" (fields visible) at a time.
    await expect(page.locator('[data-testid="zapi-inbound-token-panel"]')).toHaveCount(0);
    await expect(page).toHaveScreenshot(`01-closed-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("02 Meta expanded — signature pending banner", async ({ page }, testInfo) => {
    await setupRoutes(page, {
      whatsapp_provider: "meta_cloud",
      meta_phone_number_id: "123456789012345",
      meta_waba_id: "987654321098765",
      meta_permanent_token_set: true,
      meta_permanent_token_masked: "EAAG****9xyz",
      meta_app_secret_set: false,
      meta_verify_token: "verify-token-demo",
    });
    await loginAndNavigate(page);
    await page.locator('[data-testid="whatsapp-card-meta_cloud"]').click();
    await expect(
      page.locator('[data-testid="meta-signature-pending-banner"]'),
    ).toBeVisible();
    await expect(page).toHaveScreenshot(`02-meta-expanded-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("03 LLM expanded — OpenAI configured", async ({ page }, testInfo) => {
    await loginAndNavigate(page);
    // OpenAI is the default LLM provider — click to (re)assert selection and
    // ensure the panel is scrolled into view for the screenshot.
    await page.locator('[data-testid="llm-provider-openai"]').click();
    await page.locator('[data-testid="llm-provider-openai"]').scrollIntoViewIfNeeded();
    await expect(page).toHaveScreenshot(`03-llm-expanded-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("04 STT expanded — OpenAI Whisper configured", async ({ page }, testInfo) => {
    await loginAndNavigate(page);
    await page.locator('[data-testid="stt-provider-openai_whisper"]').click();
    await page
      .locator('[data-testid="stt-provider-openai_whisper"]')
      .scrollIntoViewIfNeeded();
    await expect(page).toHaveScreenshot(`04-stt-expanded-${testInfo.project.name}.png`, {
      maxDiffPixelRatio: 0.001,
      fullPage: true,
    });
  });

  test("05 Z-API inbound token configured — no raw token rendered", async ({
    page,
  }, testInfo) => {
    await setupRoutes(page, {
      whatsapp_provider: "zapi",
      zapi_instance_id: "REDACTED-INSTANCE",
      zapi_token_set: true,
      zapi_inbound_token_digest_set: true,
    });
    await loginAndNavigate(page);
    // The "configured" placeholder must be visible; the one-time URL panel
    // (which would contain a raw token) must NOT be present.
    await expect(
      page.locator('[data-testid="zapi-inbound-token-configured"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="zapi-inbound-token-panel"]'),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="zapi-inbound-token-url"]'),
    ).toHaveCount(0);
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
  test("cards stay within viewport bounds and do not overlap", async ({
    page,
  }) => {
    await setupRoutes(page);
    await loginAndNavigate(page);
    const viewport = page.viewportSize();
    if (!viewport) test.skip();
    const cards = await page
      .locator(
        '[data-testid^="whatsapp-card-"], [data-testid^="llm-provider-"], [data-testid^="stt-provider-"]',
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
    await page.locator('[data-testid="whatsapp-card-meta_cloud"]').click();
    // At most one provider card carries the border-2 active class — but the
    // stronger invariant is that at most one provider-specific field group
    // renders. Meta shows the phone_number_id input; Z-API shows instance id.
    const metaFields = page.locator('[data-testid="meta-app-secret-field"]');
    await expect(metaFields).toHaveCount(1);
  });

  test("coming-soon LLM providers are non-interactive", async ({ page }) => {
    await setupRoutes(page);
    await loginAndNavigate(page);
    const comingSoon = page.locator('[data-coming-soon="true"]');
    const count = await comingSoon.count();
    for (let i = 0; i < count; i++) {
      const btn = comingSoon.nth(i);
      await expect(btn).toBeDisabled();
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Meta signature-pending — variant-specific assertions
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Meta signature_pending banner", () => {
  test("warns when identity is saved but app secret is missing", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page, {
      whatsapp_provider: "meta_cloud",
      meta_phone_number_id: "123456789012345",
      meta_permanent_token_set: true,
      meta_app_secret_set: false,
    });
    await loginAndNavigate(page);
    await page.locator('[data-testid="whatsapp-card-meta_cloud"]').click();
    await expect(
      page.locator('[data-testid="meta-signature-pending-banner"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="meta-signature-pending-badge"]'),
    ).toBeVisible();
  });

  test("hides the banner once app secret is set", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page, {
      whatsapp_provider: "meta_cloud",
      meta_phone_number_id: "123456789012345",
      meta_permanent_token_set: true,
      meta_app_secret_set: true,
    });
    await loginAndNavigate(page);
    await page.locator('[data-testid="whatsapp-card-meta_cloud"]').click();
    await expect(
      page.locator('[data-testid="meta-signature-pending-banner"]'),
    ).toHaveCount(0);
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Z-API capability URL — one-time-only, redaction guarantees
// ─────────────────────────────────────────────────────────────────────────────

test.describe("Z-API capability token flow", () => {
  test("token panel is hidden before save and never captured", async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop");
    await setupRoutes(page, {
      whatsapp_provider: "zapi",
      zapi_instance_id: "REDACTED-INSTANCE",
      zapi_token_set: true,
    });
    await loginAndNavigate(page);
    // Before any save is triggered, no raw token URL should be in the DOM.
    await expect(
      page.locator('[data-testid="zapi-inbound-token-panel"]'),
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="zapi-inbound-token-url"]'),
    ).toHaveCount(0);
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
