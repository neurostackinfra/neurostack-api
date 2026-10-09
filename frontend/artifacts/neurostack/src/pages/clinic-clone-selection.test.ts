import { describe, expect, it } from "vitest";

import {
  buildCloneRequest,
  cloneSelectionEquals,
  isCloneConsentValid,
  type CloneSelection,
} from "@/pages/clinic-clone-selection";
import cloneContract from "../../../../../tests/fixtures/clinic_clone_contract.json";

const sourceClinic = { id: "11111111-1111-4111-8111-111111111111" };
const secondClinic = { id: "33333333-3333-4333-8333-333333333333" };

describe("clinic clone selection helpers", () => {
  const selection: CloneSelection = { sourceId: sourceClinic.id, includeRag: true, includeMedia: false };

  it("treats consent as valid only while the snapshot equals the current selection", () => {
    expect(isCloneConsentValid({ ...selection }, selection)).toBe(true);
    expect(isCloneConsentValid(null, selection)).toBe(false);
    expect(isCloneConsentValid(selection, { ...selection, sourceId: secondClinic.id })).toBe(false);
    expect(isCloneConsentValid(selection, { ...selection, includeRag: false })).toBe(false);
    expect(isCloneConsentValid(selection, { ...selection, includeMedia: true })).toBe(false);
    expect(isCloneConsentValid({ ...selection, sourceId: "" }, { ...selection, sourceId: "" })).toBe(false);
    expect(cloneSelectionEquals(selection, { ...selection })).toBe(true);
  });

  it("builds the request with exactly the two booleans of the current selection", () => {
    expect(buildCloneRequest(selection)).toEqual({ include_rag: true, include_media: false });
    expect(buildCloneRequest({ ...selection, includeRag: true, includeMedia: true }))
      .toEqual(cloneContract.success.request);
  });
});
