import { test, expect } from "@playwright/test";
import type { APIResponse } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { inflateRawSync } from "node:zlib";

// ─────────────────────────────────────────────────────────────────────────────
// Credit export E2E (neurostack-credit-system Group 5, Deliverable 1).
//
// Runs against the REAL API -- started with ENVIRONMENT=test,
// CREDIT_HOT_PATH_ENABLED=true and a disposable PostgreSQL at `alembic upgrade
// head` plus a disposable S3-compatible bucket -- reached through the Vite dev
// server's proxy (VITE_PROXY_TARGET=$CREDIT_E2E_API_URL). Nothing here
// intercepts or fakes the network: the archive's bytes come from PostgreSQL.
//
// `scripts/seed-credit-e2e.py` seeds the builder, clinic, user, one PNG, one
// knowledge document and the PNG's additional S3 copy, and prints the builder
// JWT (CREDIT_E2E_JWT) and the copy's VersionId (CREDIT_E2E_S3_VERSION_ID).
// The bytes below are rebuilt exactly as the seed writes them, so the SHA-256
// of what was downloaded is checked against an independent expectation, not
// only against the server's own manifest.
//
// The billing UI arrives in Group 6 (which adds the button test); here the
// download is the direct URL with the bearer token.
// ─────────────────────────────────────────────────────────────────────────────

const EXPORT_PATH = "/v1/credits/export";
const JWT = process.env.CREDIT_E2E_JWT ?? "";
const EXPECTED_VERSION_ID = process.env.CREDIT_E2E_S3_VERSION_ID ?? "";

const MEDIA_ID = "5e000000-0000-4000-8000-000000000004";
const DOCUMENT_ID = "5e000000-0000-4000-8000-000000000005";
const PNG_PAYLOAD = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from("neurostack-credit-e2e-export", "ascii"),
  Buffer.from(Array.from({ length: 256 }, (_unused, index) => index)),
]);
const DOCUMENT_TEXT = "NeuroStack credit export E2E: conteúdo autoritativo\n";
// Independent pins of the two digests (computed from the bytes above).
const PNG_SHA256 = "f66abfd0b91d021193ccbbe19f23f8ed1b40657acfd573dceccc31e5bc4d9329";
const DOCUMENT_SHA256 = "d52f69d7d8d73000842d7207a2894754fd7ecde0f65e805be4b644ca79dd874e";

interface ManifestItem {
  kind: "media" | "knowledge";
  id: string;
  path: string;
  size_bytes: number;
  sha256: string;
  source_sha256?: string | null;
  source_sha256_matches?: boolean | null;
  s3_copy: { object_key: string; version_id: string; etag: string } | null;
}

interface Manifest {
  schema_version: string;
  tenant_id: string;
  hash_algorithm: string;
  items: ManifestItem[];
}

function sha256(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

/** Minimal ZIP reader (central directory; stored or deflated entries). */
function readZip(archive: Buffer): Map<string, Buffer> {
  const endSignature = 0x06054b50;
  let end = -1;
  for (let offset = archive.length - 22; offset >= 0; offset -= 1) {
    if (archive.readUInt32LE(offset) === endSignature) {
      end = offset;
      break;
    }
  }
  if (end < 0) throw new Error("not a ZIP archive: no end of central directory");
  const entries = archive.readUInt16LE(end + 10);
  let cursor = archive.readUInt32LE(end + 16);
  const files = new Map<string, Buffer>();
  for (let index = 0; index < entries; index += 1) {
    if (archive.readUInt32LE(cursor) !== 0x02014b50) {
      throw new Error("corrupt central directory");
    }
    const method = archive.readUInt16LE(cursor + 10);
    const compressedSize = archive.readUInt32LE(cursor + 20);
    const size = archive.readUInt32LE(cursor + 24);
    const nameLength = archive.readUInt16LE(cursor + 28);
    const extraLength = archive.readUInt16LE(cursor + 30);
    const commentLength = archive.readUInt16LE(cursor + 32);
    const localOffset = archive.readUInt32LE(cursor + 42);
    const name = archive.toString("utf8", cursor + 46, cursor + 46 + nameLength);
    if (archive.readUInt32LE(localOffset) !== 0x04034b50) {
      throw new Error(`corrupt local header for ${name}`);
    }
    const dataStart =
      localOffset + 30 + archive.readUInt16LE(localOffset + 26) + archive.readUInt16LE(localOffset + 28);
    const raw = archive.subarray(dataStart, dataStart + compressedSize);
    const data = method === 0 ? Buffer.from(raw) : method === 8 ? inflateRawSync(raw) : null;
    if (data === null) throw new Error(`unsupported compression ${method} for ${name}`);
    if (data.length !== size) throw new Error(`size mismatch for ${name}`);
    if (files.has(name)) throw new Error(`duplicate entry ${name}`);
    files.set(name, data);
    cursor += 46 + nameLength + extraLength + commentLength;
  }
  return files;
}

/** Every assertion the export promise makes, for one downloaded archive. */
function verifyArchive(archive: Buffer): Manifest {
  const files = readZip(archive);
  const manifestBytes = files.get("manifest.json");
  expect(manifestBytes, "manifest.json is in the archive").toBeDefined();
  const manifest = JSON.parse(manifestBytes!.toString("utf8")) as Manifest;
  expect(manifest.schema_version).toBe("credit_export.v1");
  expect(manifest.hash_algorithm).toBe("sha256");

  // Every listed entry exists and its downloaded bytes hash to the manifest.
  for (const item of manifest.items) {
    const data = files.get(item.path);
    expect(data, `${item.path} is in the archive`).toBeDefined();
    expect(sha256(data!)).toBe(item.sha256);
    expect(data!.length).toBe(item.size_bytes);
  }
  expect([...files.keys()].sort()).toEqual(
    [...manifest.items.map((item) => item.path), "manifest.json"].sort(),
  );

  // The seeded bytes, checked against an independent expectation.
  const media = manifest.items.find((item) => item.id === MEDIA_ID);
  expect(media, "the seeded PNG is exported").toBeDefined();
  expect(sha256(PNG_PAYLOAD)).toBe(PNG_SHA256);
  expect(media!.kind).toBe("media");
  expect(media!.sha256).toBe(PNG_SHA256);
  expect(media!.source_sha256).toBe(PNG_SHA256);
  expect(media!.source_sha256_matches).toBe(true);
  expect(files.get(media!.path)!.equals(PNG_PAYLOAD)).toBe(true);
  // The additional S3 copy with the same SHA-256 is named by its VersionId.
  expect(media!.s3_copy, "the S3 copy's VersionId is in the manifest").not.toBeNull();
  expect(media!.s3_copy!.version_id.length).toBeGreaterThan(0);
  if (EXPECTED_VERSION_ID) {
    expect(media!.s3_copy!.version_id).toBe(EXPECTED_VERSION_ID);
  }

  const document = manifest.items.find((item) => item.id === DOCUMENT_ID);
  expect(document, "the seeded document is exported").toBeDefined();
  const documentBytes = Buffer.from(DOCUMENT_TEXT, "utf8");
  expect(sha256(documentBytes)).toBe(DOCUMENT_SHA256);
  expect(document!.sha256).toBe(DOCUMENT_SHA256);
  expect(files.get(document!.path)!.equals(documentBytes)).toBe(true);
  return manifest;
}

async function body(response: APIResponse): Promise<Buffer> {
  return Buffer.from(await response.body());
}

test.describe("credit export download (real API, no network mocks)", () => {
  test.beforeAll(() => {
    // A missing token is a failure of the gate, never a skip.
    if (!JWT) throw new Error("CREDIT_E2E_JWT is required (scripts/seed-credit-e2e.py)");
  });

  test("GET /v1/credits/export returns the authoritative bytes with their SHA-256", async ({
    request,
  }) => {
    const response = await request.get(EXPORT_PATH, {
      headers: { Authorization: `Bearer ${JWT}` },
    });
    expect(response.status()).toBe(200);
    const headers = response.headers();
    expect(headers["content-type"]).toBe("application/zip");
    expect(headers["content-disposition"]).toMatch(/^attachment; filename="neurostack-export-/);
    expect(headers["cache-control"]).toBe("no-store");
    const archive = await body(response);
    expect(sha256(archive)).toBe(headers["x-credit-export-sha256"]);
    const files = readZip(archive);
    expect(sha256(files.get("manifest.json")!)).toBe(headers["x-credit-export-manifest-sha256"]);
    verifyArchive(archive);
  });

  test("the browser downloads the same verified archive", async ({ browser, baseURL }) => {
    const context = await browser.newContext({
      acceptDownloads: true,
      baseURL,
      extraHTTPHeaders: { Authorization: `Bearer ${JWT}` },
    });
    try {
      const page = await context.newPage();
      const downloadEvent = page.waitForEvent("download");
      // Navigating to an attachment starts a download and aborts the
      // navigation itself; that abort is expected.
      await page.goto(EXPORT_PATH).catch(() => undefined);
      const download = await downloadEvent;
      expect(download.suggestedFilename()).toMatch(/^neurostack-export-.*\.zip$/);
      const path = await download.path();
      expect(path).toBeTruthy();
      verifyArchive(await readFile(path!));
    } finally {
      await context.close();
    }
  });

  test("an unauthenticated download is refused", async ({ request }) => {
    const response = await request.get(EXPORT_PATH);
    expect(response.status()).toBe(401);
  });

  test("another tenant id is refused without revealing it", async ({ request }) => {
    const response = await request.get(`${EXPORT_PATH}?tenant_id=00000000-0000-4000-8000-00000000dead`, {
      headers: { Authorization: `Bearer ${JWT}` },
    });
    expect(response.status()).toBe(403);
  });
});
