// Canonical Cloning Center selection. Clinic configuration is always copied, so
// only the source and the two optional parts are part of the selection.
export interface CloneSelection {
  sourceId: string;
  includeRag: boolean;
  includeMedia: boolean;
}

// Body of POST /v1/builder/clinics/{source_id}/clone.
export interface CloneRequestBody {
  include_rag: boolean;
  include_media: boolean;
}

// Mirrors the backend `ClinicCloneErrorDetail.error` literals
// (src/api/schemas/builder.py).
export type CloneErrorCode =
  | "clinic_clone_forbidden"
  | "clinic_clone_source_not_found"
  | "clinic_clone_invalid"
  | "clinic_clone_persistence_failed"
  | "clinic_clone_request_invalid"
  | "clinic_clone_media_storage_cap_exceeded";

export function cloneSelectionEquals(a: CloneSelection, b: CloneSelection): boolean {
  return a.sourceId === b.sourceId
    && a.includeRag === b.includeRag
    && a.includeMedia === b.includeMedia;
}

// Consent is the snapshot stored when the user ticked the acknowledgement; it
// only counts while it still describes the current selection.
export function isCloneConsentValid(
  consent: CloneSelection | null,
  selection: CloneSelection,
): boolean {
  return consent !== null && selection.sourceId !== "" && cloneSelectionEquals(consent, selection);
}

export function buildCloneRequest(selection: CloneSelection): CloneRequestBody {
  return { include_rag: selection.includeRag, include_media: selection.includeMedia };
}
