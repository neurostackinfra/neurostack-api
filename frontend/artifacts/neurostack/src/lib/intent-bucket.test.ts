import { describe, expect, it } from "vitest";

import { en } from "@/locales/en";
import { pt } from "@/locales/pt";
import {
  BUCKET_PRESENTATION,
  getBucketLabel,
  getBucketPresentation,
  getIntentBucket,
  INTENT_BUCKET,
  INTENT_PRESENTATION,
  type IntentBucket,
} from "./intent-presentation";

const VALID_BUCKETS: readonly IntentBucket[] = [
  "pedido_preco",
  "agendar",
  "interesse_tratamento",
  "reclamacao",
  "pos_atendimento_duvida",
  "outras",
];

const COMMERCIAL_BUCKETS = [
  "pedido_preco",
  "agendar",
  "interesse_tratamento",
  "reclamacao",
  "pos_atendimento_duvida",
] as const;

describe("intent bucket taxonomy", () => {
  it("maps every INTENT_PRESENTATION key to exactly one valid bucket", () => {
    const intentKeys = Object.keys(INTENT_PRESENTATION).sort();
    const bucketKeys = Object.keys(INTENT_BUCKET).sort();

    const missing = intentKeys.filter((key) => !bucketKeys.includes(key));
    const extra = bucketKeys.filter((key) => !intentKeys.includes(key));

    expect({ missing, extra }).toEqual({ missing: [], extra: [] });

    const invalidTargets = Object.entries(INTENT_BUCKET)
      .filter(([, bucket]) => !VALID_BUCKETS.includes(bucket as IntentBucket))
      .map(([key, bucket]) => `${key} -> ${bucket}`);
    expect(invalidTargets).toEqual([]);

    // Every entry is unique (guaranteed by object keys) — assert count anyway
    expect(bucketKeys.length).toBe(29);
  });

  it("has commercial buckets whose tokens equal the same-named raw intent", () => {
    for (const bucket of COMMERCIAL_BUCKETS) {
      const bucketPres = BUCKET_PRESENTATION[bucket];
      const intentPres = INTENT_PRESENTATION[bucket];
      expect({
        baseClassName: bucketPres.baseClassName,
        badgeClassName: bucketPres.badgeClassName,
        hex: bucketPres.hex,
      }).toEqual({
        baseClassName: intentPres.baseClassName,
        badgeClassName: intentPres.badgeClassName,
        hex: intentPres.hex,
      });
    }
  });

  it.each(["constructor", "__proto__", "toString"])(
    "treats inherited object property %s as the 'outras' bucket",
    (key) => {
      expect(getIntentBucket(key)).toBe("outras");
    },
  );

  it("returns 'outras' for unknown keys", () => {
    expect(getIntentBucket("future_intent")).toBe("outras");
    expect(getIntentBucket("")).toBe("outras");
  });

  it("resolves the correct bucket for representative known intents", () => {
    expect(getIntentBucket("orcamento_existente")).toBe("pedido_preco");
    expect(getIntentBucket("fora_do_horario")).toBe("agendar");
    expect(getIntentBucket("cultura_historia")).toBe("interesse_tratamento");
    expect(getIntentBucket("conduta_inadequada_ameaca")).toBe("reclamacao");
    expect(getIntentBucket("pedido_alterar_medicacao")).toBe("pos_atendimento_duvida");
    expect(getIntentBucket("saudacao")).toBe("outras");
    expect(getIntentBucket("faq")).toBe("outras");
  });

  it("localizes the 'outras' bucket label per locale", () => {
    expect(getBucketLabel("outras", (k) => pt[k])).toBe("Outras");
    expect(getBucketLabel("outras", (k) => en[k])).toBe("Other");
  });

  it("exposes bucket presentations via getBucketPresentation", () => {
    for (const bucket of VALID_BUCKETS) {
      expect(getBucketPresentation(bucket)).toBe(BUCKET_PRESENTATION[bucket]);
    }
  });

  it("gives the 'outras' bucket a hex distinct from every other bucket and #64748b", () => {
    const outrasHex = BUCKET_PRESENTATION.outras.hex;
    expect(outrasHex).not.toBe("#64748b");
    for (const bucket of COMMERCIAL_BUCKETS) {
      expect(outrasHex).not.toBe(BUCKET_PRESENTATION[bucket].hex);
    }
  });
});
