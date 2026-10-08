import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Download,
  FileAudio,
  FileImage,
  FileText,
  FileVideo,
  FolderOpen,
  Loader2,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";

type MediaCategory = "video" | "audio" | "pdf" | "image";

interface MediaAsset {
  id: string;
  tenant_id: string;
  display_name: string;
  original_filename: string;
  category: MediaCategory;
  mime_type: string;
  size_bytes: number;
  usage_trigger: string | null;
  auto_suggest: boolean;
  created_at: string;
  updated_at: string;
}

interface MediaListResponse {
  items: MediaAsset[];
  next_cursor: string | null;
  has_more: boolean;
}

type FeedbackKind = "upload" | "edit" | "delete" | "download";

interface CatalogState {
  scope: string;
  items: MediaAsset[];
  nextCursor: string | null;
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  error: boolean;
}

interface EditState {
  scope: string;
  asset: MediaAsset;
  displayName: string;
  originalFilename: string;
  usageTrigger: string;
  usageTriggerIsNull: boolean;
  autoSuggest: boolean;
}

interface DeleteState {
  scope: string;
  asset: MediaAsset;
}

const EMPTY_CATALOG: Omit<CatalogState, "scope"> = {
  items: [],
  nextCursor: null,
  hasMore: false,
  loading: false,
  loadingMore: false,
  error: false,
};

const MAX_FILE_BYTES = 50_000_000;

function authToken() {
  try {
    const raw = localStorage.getItem("ns_auth");
    return raw ? ((JSON.parse(raw) as { token?: string }).token ?? "") : "";
  } catch {
    return "";
  }
}

function formatBytes(bytes: number, locale: "pt" | "en") {
  const megabytes = bytes / 1_000_000;
  if (megabytes >= 1) {
    return `${new Intl.NumberFormat(locale === "pt" ? "pt-BR" : "en-US", {
      maximumFractionDigits: 1,
    }).format(megabytes)} MB`;
  }
  return `${Math.max(1, Math.round(bytes / 1_000))} KB`;
}

function categoryIcon(category: MediaCategory) {
  if (category === "video") return FileVideo;
  if (category === "audio") return FileAudio;
  if (category === "pdf") return FileText;
  return FileImage;
}

export default function Midias() {
  const {
    clinicId,
    tenantId,
    role,
    isBuilder,
    isClinicAdmin,
    isInvestor,
    isSuperAdmin,
    viewingClinicId,
  } = useAuth();
  const { locale, t } = useI18n();
  const requiresSelectedClinic = isBuilder || isSuperAdmin;
  const canManage =
    !isInvestor &&
    (isClinicAdmin ||
      ((isBuilder || isSuperAdmin) && Boolean(viewingClinicId)));
  const ownTenantIdentity = tenantId || clinicId;
  const targetIdentity = requiresSelectedClinic
    ? viewingClinicId
    : ownTenantIdentity;
  const scope = `${role}:${requiresSelectedClinic ? "override" : "own"}:${targetIdentity}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;

  const controllersRef = useRef(new Set<AbortController>());
  const catalogRevisionRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [catalog, setCatalog] = useState<CatalogState>({
    scope,
    ...EMPTY_CATALOG,
  });
  const [file, setFile] = useState<File | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [originalFilename, setOriginalFilename] = useState("");
  const [usageTrigger, setUsageTrigger] = useState("");
  const [autoSuggest, setAutoSuggest] = useState(false);
  const [uploadingScope, setUploadingScope] = useState<string | null>(null);
  const [editState, setEditState] = useState<EditState | null>(null);
  const [deleteState, setDeleteState] = useState<DeleteState | null>(null);
  const [busyAsset, setBusyAsset] = useState<{
    scope: string;
    id: string;
    kind: FeedbackKind;
  } | null>(null);
  const [feedback, setFeedback] = useState<{
    scope: string;
    kind: FeedbackKind;
    success: boolean;
  } | null>(null);
  const [fileError, setFileError] = useState(false);

  function abortRequests() {
    for (const controller of controllersRef.current) controller.abort();
    controllersRef.current.clear();
  }

  function requestController() {
    const controller = new AbortController();
    controllersRef.current.add(controller);
    return controller;
  }

  function finishRequest(controller: AbortController) {
    controllersRef.current.delete(controller);
  }

  function mediaUrl(path = "", cursor?: string | null) {
    const params = new URLSearchParams();
    if (requiresSelectedClinic) params.set("tenant_id", viewingClinicId);
    if (cursor) params.set("cursor", cursor);
    const query = params.toString();
    return `/v1/media-assets${path}${query ? `?${query}` : ""}`;
  }

  useEffect(() => {
    abortRequests();
    setCatalog({ scope, ...EMPTY_CATALOG, loading: canManage });
    setFile(null);
    setDisplayName("");
    setOriginalFilename("");
    setUsageTrigger("");
    setAutoSuggest(false);
    setFileError(false);
    setUploadingScope(null);
    setEditState(null);
    setDeleteState(null);
    setBusyAsset(null);
    setFeedback(null);
    if (fileInputRef.current) fileInputRef.current.value = "";

    if (!canManage) return abortRequests;

    const requestScope = scope;
    const requestRevision = catalogRevisionRef.current;
    const controller = requestController();
    void fetch(mediaUrl(), {
      headers: { Authorization: `Bearer ${authToken()}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`media-list-${response.status}`);
        return (await response.json()) as MediaListResponse;
      })
      .then((page) => {
        if (scopeRef.current !== requestScope || controller.signal.aborted)
          return;
        if (catalogRevisionRef.current !== requestRevision) {
          setCatalog((current) =>
            current.scope === requestScope
              ? { ...current, loading: false, loadingMore: false }
              : current,
          );
          return;
        }
        setCatalog({
          scope: requestScope,
          items: page.items,
          nextCursor: page.next_cursor,
          hasMore: page.has_more,
          loading: false,
          loadingMore: false,
          error: false,
        });
      })
      .catch((error: unknown) => {
        if (
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError") ||
          scopeRef.current !== requestScope
        ) {
          return;
        }
        if (catalogRevisionRef.current !== requestRevision) {
          setCatalog((current) =>
            current.scope === requestScope
              ? { ...current, loading: false, loadingMore: false }
              : current,
          );
          return;
        }
        setCatalog({
          scope: requestScope,
          ...EMPTY_CATALOG,
          error: true,
        });
      })
      .finally(() => finishRequest(controller));

    return abortRequests;
    // reloadVersion intentionally replays the initial keyset page for Retry.
  }, [canManage, reloadVersion, scope]);

  const currentCatalog =
    catalog.scope === scope
      ? catalog
      : { scope, ...EMPTY_CATALOG, loading: canManage };
  const currentEdit = editState?.scope === scope ? editState : null;
  const currentDelete = deleteState?.scope === scope ? deleteState : null;
  const currentBusy = busyAsset?.scope === scope ? busyAsset : null;
  const currentFeedback = feedback?.scope === scope ? feedback : null;
  const isUploading = uploadingScope === scope;

  async function loadMore() {
    if (
      !canManage ||
      currentCatalog.loadingMore ||
      !currentCatalog.hasMore ||
      !currentCatalog.nextCursor
    ) {
      return;
    }
    const requestScope = scope;
    const cursor = currentCatalog.nextCursor;
    const requestRevision = catalogRevisionRef.current;
    const controller = requestController();
    setCatalog((current) =>
      current.scope === requestScope
        ? { ...current, loadingMore: true, error: false }
        : current,
    );
    try {
      const response = await fetch(mediaUrl("", cursor), {
        headers: { Authorization: `Bearer ${authToken()}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`media-list-${response.status}`);
      const page = (await response.json()) as MediaListResponse;
      if (scopeRef.current !== requestScope || controller.signal.aborted)
        return;
      setCatalog((current) => {
        if (current.scope !== requestScope) return current;
        if (catalogRevisionRef.current !== requestRevision) {
          return { ...current, loadingMore: false };
        }
        const known = new Set(current.items.map((asset) => asset.id));
        return {
          ...current,
          items: [
            ...current.items,
            ...page.items.filter((asset) => !known.has(asset.id)),
          ],
          nextCursor: page.next_cursor,
          hasMore: page.has_more,
          loadingMore: false,
        };
      });
    } catch (error) {
      if (
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError") ||
        scopeRef.current !== requestScope
      ) {
        return;
      }
      setCatalog((current) =>
        current.scope === requestScope
          ? {
              ...current,
              loadingMore: false,
              error:
                catalogRevisionRef.current === requestRevision
                  ? true
                  : current.error,
            }
          : current,
      );
    } finally {
      finishRequest(controller);
    }
  }

  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] ?? null;
    setFeedback(null);
    if (selected && selected.size > MAX_FILE_BYTES) {
      event.target.value = "";
      setFile(null);
      setFileError(true);
      return;
    }
    setFileError(false);
    setFile(selected);
    if (selected) {
      setOriginalFilename(selected.name);
      setDisplayName(
        (current) => current || selected.name.replace(/\.[^.]+$/, ""),
      );
    } else {
      setOriginalFilename("");
    }
  }

  async function uploadAsset(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file || !displayName || !originalFilename || !canManage) return;
    const requestScope = scope;
    const controller = requestController();
    const form = new FormData();
    form.append("file", file);
    form.append("display_name", displayName);
    form.append("original_filename", originalFilename);
    if (usageTrigger !== "") form.append("usage_trigger", usageTrigger);
    form.append("auto_suggest", autoSuggest ? "true" : "false");
    setUploadingScope(requestScope);
    setFeedback(null);
    try {
      const response = await fetch(mediaUrl(), {
        method: "POST",
        headers: { Authorization: `Bearer ${authToken()}` },
        body: form,
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`media-upload-${response.status}`);
      const created = (await response.json()) as MediaAsset;
      if (scopeRef.current !== requestScope || controller.signal.aborted)
        return;
      catalogRevisionRef.current += 1;
      setCatalog((current) =>
        current.scope === requestScope
          ? {
              ...current,
              loading: false,
              loadingMore: false,
              error: false,
              items: [
                created,
                ...current.items.filter((item) => item.id !== created.id),
              ],
            }
          : current,
      );
      setFile(null);
      setDisplayName("");
      setOriginalFilename("");
      setUsageTrigger("");
      setAutoSuggest(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setFeedback({ scope: requestScope, kind: "upload", success: true });
    } catch (error) {
      if (
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError") ||
        scopeRef.current !== requestScope
      ) {
        return;
      }
      setFeedback({ scope: requestScope, kind: "upload", success: false });
    } finally {
      finishRequest(controller);
      if (scopeRef.current === requestScope) setUploadingScope(null);
    }
  }

  function openEdit(asset: MediaAsset) {
    setFeedback(null);
    setEditState({
      scope,
      asset,
      displayName: asset.display_name,
      originalFilename: asset.original_filename,
      usageTrigger: asset.usage_trigger ?? "",
      usageTriggerIsNull: asset.usage_trigger === null,
      autoSuggest: asset.auto_suggest,
    });
  }

  async function saveEdit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !currentEdit ||
      !currentEdit.displayName ||
      !currentEdit.originalFilename
    )
      return;
    const requestScope = scope;
    const assetId = currentEdit.asset.id;
    const controller = requestController();
    setBusyAsset({ scope: requestScope, id: assetId, kind: "edit" });
    setFeedback(null);
    try {
      const response = await fetch(mediaUrl(`/${assetId}`), {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${authToken()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          display_name: currentEdit.displayName,
          original_filename: currentEdit.originalFilename,
          usage_trigger: currentEdit.usageTriggerIsNull
            ? null
            : currentEdit.usageTrigger,
          auto_suggest: currentEdit.autoSuggest,
        }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`media-edit-${response.status}`);
      const updated = (await response.json()) as MediaAsset;
      if (scopeRef.current !== requestScope || controller.signal.aborted)
        return;
      catalogRevisionRef.current += 1;
      setCatalog((current) =>
        current.scope === requestScope
          ? {
              ...current,
              loading: false,
              loadingMore: false,
              error: false,
              items: current.items.map((item) =>
                item.id === updated.id ? updated : item,
              ),
            }
          : current,
      );
      setEditState(null);
      setFeedback({ scope: requestScope, kind: "edit", success: true });
    } catch (error) {
      if (
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError") ||
        scopeRef.current !== requestScope
      ) {
        return;
      }
      setFeedback({ scope: requestScope, kind: "edit", success: false });
    } finally {
      finishRequest(controller);
      if (scopeRef.current === requestScope) setBusyAsset(null);
    }
  }

  async function downloadAsset(asset: MediaAsset) {
    const requestScope = scope;
    const controller = requestController();
    setBusyAsset({ scope: requestScope, id: asset.id, kind: "download" });
    setFeedback(null);
    try {
      const response = await fetch(mediaUrl(`/${asset.id}/content`), {
        headers: { Authorization: `Bearer ${authToken()}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`media-download-${response.status}`);
      const blob = await response.blob();
      if (scopeRef.current !== requestScope || controller.signal.aborted)
        return;
      const objectUrl = URL.createObjectURL(blob);
      if (scopeRef.current !== requestScope || controller.signal.aborted) {
        URL.revokeObjectURL(objectUrl);
        return;
      }
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = asset.original_filename;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setFeedback({ scope: requestScope, kind: "download", success: true });
    } catch (error) {
      if (
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError") ||
        scopeRef.current !== requestScope
      ) {
        return;
      }
      setFeedback({ scope: requestScope, kind: "download", success: false });
    } finally {
      finishRequest(controller);
      if (scopeRef.current === requestScope) setBusyAsset(null);
    }
  }

  async function deleteAsset() {
    if (!currentDelete) return;
    const requestScope = scope;
    const assetId = currentDelete.asset.id;
    const controller = requestController();
    setBusyAsset({ scope: requestScope, id: assetId, kind: "delete" });
    setFeedback(null);
    try {
      const response = await fetch(mediaUrl(`/${assetId}`), {
        method: "DELETE",
        headers: { Authorization: `Bearer ${authToken()}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`media-delete-${response.status}`);
      if (scopeRef.current !== requestScope || controller.signal.aborted)
        return;
      catalogRevisionRef.current += 1;
      setCatalog((current) =>
        current.scope === requestScope
          ? {
              ...current,
              loading: false,
              loadingMore: false,
              error: false,
              items: current.items.filter((item) => item.id !== assetId),
            }
          : current,
      );
      setDeleteState(null);
      setFeedback({ scope: requestScope, kind: "delete", success: true });
    } catch (error) {
      if (
        controller.signal.aborted ||
        (error instanceof DOMException && error.name === "AbortError") ||
        scopeRef.current !== requestScope
      ) {
        return;
      }
      setFeedback({ scope: requestScope, kind: "delete", success: false });
    } finally {
      finishRequest(controller);
      if (scopeRef.current === requestScope) setBusyAsset(null);
    }
  }

  if (isInvestor || (!isClinicAdmin && !isBuilder && !isSuperAdmin)) {
    return (
      <div
        className="flex min-h-64 items-center justify-center rounded-lg border border-border bg-card p-8 text-center"
        role="alert"
        data-testid="media-access-denied"
      >
        <div className="space-y-2">
          <AlertCircle className="mx-auto h-8 w-8 text-destructive" />
          <h1 className="text-lg font-semibold">
            {t("media_access_denied_title")}
          </h1>
          <p className="text-sm text-muted-foreground">
            {t("media_access_denied_description")}
          </p>
        </div>
      </div>
    );
  }

  if (requiresSelectedClinic && !viewingClinicId) {
    return (
      <div
        className="flex min-h-64 items-center justify-center rounded-lg border border-border bg-card p-8 text-center"
        role="status"
        data-testid="media-select-clinic"
      >
        <div className="space-y-2">
          <FolderOpen className="mx-auto h-8 w-8 text-muted-foreground" />
          <h1 className="text-lg font-semibold">
            {t("media_select_clinic_title")}
          </h1>
          <p className="text-sm text-muted-foreground">
            {t("media_select_clinic_description")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl space-y-6" data-testid="media-catalog">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg border border-sky-500/20 bg-sky-500/10">
            <FolderOpen className="h-5 w-5 text-sky-500" />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">
              {t("media_title")}
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {t("media_subtitle")}
            </p>
          </div>
        </div>
        <Badge variant="outline">{t("media_supported_formats")}</Badge>
      </div>

      <div
        className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-foreground"
        role="note"
      >
        <strong>{t("media_catalog_only_title")}</strong>{" "}
        {t("media_catalog_only_description")}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Upload className="h-4 w-4 text-sky-500" />
            {t("media_upload_title")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={uploadAsset}>
            <div className="space-y-1.5">
              <Label htmlFor="media-file">{t("media_file_label")}</Label>
              <Input
                ref={fileInputRef}
                id="media-file"
                type="file"
                accept="video/mp4,audio/mpeg,application/pdf,image/jpeg,image/png,.mp4,.mp3,.pdf,.jpg,.jpeg,.png"
                onChange={handleFileChange}
                required
                disabled={isUploading}
              />
              <p className="text-xs text-muted-foreground">
                {t("media_file_help")}
              </p>
              {fileError && (
                <p className="text-sm text-destructive" role="alert">
                  {t("media_file_too_large")}
                </p>
              )}
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="media-display-name">
                  {t("media_display_name_label")}
                </Label>
                <Input
                  id="media-display-name"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  maxLength={200}
                  required
                  disabled={isUploading}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="media-original-filename">
                  {t("media_original_filename_label")}
                </Label>
                <Input
                  id="media-original-filename"
                  value={originalFilename}
                  onChange={(event) => setOriginalFilename(event.target.value)}
                  maxLength={255}
                  required
                  disabled={isUploading}
                />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="media-usage-trigger">
                {t("media_usage_trigger_label")}
              </Label>
              <Textarea
                id="media-usage-trigger"
                value={usageTrigger}
                onChange={(event) => setUsageTrigger(event.target.value)}
                maxLength={500}
                disabled={isUploading}
                placeholder={t("media_usage_trigger_placeholder")}
              />
            </div>

            <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-4 py-3">
              <div>
                <Label htmlFor="media-auto-suggest">
                  {t("media_auto_suggest_label")}
                </Label>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {t("media_auto_suggest_help")}
                </p>
              </div>
              <Switch
                id="media-auto-suggest"
                checked={autoSuggest}
                onCheckedChange={setAutoSuggest}
                disabled={isUploading}
              />
            </div>

            <div className="flex justify-end">
              <Button
                type="submit"
                disabled={
                  isUploading || !file || !displayName || !originalFilename
                }
              >
                {isUploading ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="mr-2 h-4 w-4" />
                )}
                {isUploading ? t("media_uploading") : t("media_upload_action")}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {currentFeedback && (
        <div
          className={`rounded-lg border px-4 py-3 text-sm ${
            currentFeedback.success
              ? "border-emerald-500/30 bg-emerald-500/10"
              : "border-destructive/30 bg-destructive/10 text-destructive"
          }`}
          role={currentFeedback.success ? "status" : "alert"}
        >
          {t(
            currentFeedback.success
              ? `media_${currentFeedback.kind}_success`
              : `media_${currentFeedback.kind}_error`,
          )}
        </div>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">
            {t("media_library_title")}
          </CardTitle>
          {!currentCatalog.loading && (
            <span className="text-xs text-muted-foreground">
              {currentCatalog.items.length} {t("media_items_loaded")}
            </span>
          )}
        </CardHeader>
        <CardContent>
          {currentCatalog.loading ? (
            <div
              className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground"
              role="status"
            >
              <Loader2 className="h-5 w-5 animate-spin" />
              {t("media_loading")}
            </div>
          ) : currentCatalog.error && currentCatalog.items.length === 0 ? (
            <div className="space-y-3 py-10 text-center" role="alert">
              <AlertCircle className="mx-auto h-6 w-6 text-destructive" />
              <p className="text-sm text-destructive">
                {t("media_list_error")}
              </p>
              <Button
                type="button"
                variant="outline"
                onClick={() => setReloadVersion((version) => version + 1)}
              >
                <RefreshCw className="mr-2 h-4 w-4" />
                {t("media_retry")}
              </Button>
            </div>
          ) : currentCatalog.items.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">
              {t("media_empty")}
            </div>
          ) : (
            <div className="space-y-3">
              <ul
                className="divide-y divide-border"
                aria-label={t("media_library_title")}
              >
                {currentCatalog.items.map((asset) => {
                  const Icon = categoryIcon(asset.category);
                  const assetBusy = currentBusy?.id === asset.id;
                  return (
                    <li
                      key={asset.id}
                      className="flex flex-col gap-3 py-4 first:pt-0 sm:flex-row sm:items-center"
                      data-testid={`media-item-${asset.id}`}
                    >
                      <div className="flex min-w-0 flex-1 items-start gap-3">
                        <div className="mt-0.5 rounded-lg bg-sky-500/10 p-2 text-sky-500">
                          <Icon className="h-5 w-5" aria-hidden="true" />
                        </div>
                        <div className="min-w-0 space-y-1">
                          <p className="break-words font-semibold">
                            {asset.display_name}
                          </p>
                          <p className="break-all text-xs text-muted-foreground">
                            {asset.original_filename} ·{" "}
                            {formatBytes(asset.size_bytes, locale)}
                          </p>
                          <p className="break-words text-sm text-muted-foreground">
                            <span className="font-medium text-foreground">
                              {t("media_usage_trigger_label")}:
                            </span>{" "}
                            {asset.usage_trigger ?? t("media_no_usage_trigger")}
                          </p>
                          <Badge
                            variant={asset.auto_suggest ? "default" : "outline"}
                          >
                            {asset.auto_suggest
                              ? t("media_auto_suggest_on")
                              : t("media_auto_suggest_off")}
                          </Badge>
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2 sm:justify-end">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => void downloadAsset(asset)}
                          disabled={assetBusy}
                          aria-label={`${t("media_download_action")}: ${asset.display_name}`}
                        >
                          {assetBusy && currentBusy?.kind === "download" ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <Download className="h-4 w-4" />
                          )}
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => openEdit(asset)}
                          disabled={assetBusy}
                          aria-label={`${t("media_edit_action")}: ${asset.display_name}`}
                        >
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => setDeleteState({ scope, asset })}
                          disabled={assetBusy}
                          aria-label={`${t("media_delete_action")}: ${asset.display_name}`}
                        >
                          <Trash2 className="h-4 w-4 text-destructive" />
                        </Button>
                      </div>
                    </li>
                  );
                })}
              </ul>

              {currentCatalog.error && (
                <p
                  className="text-center text-sm text-destructive"
                  role="alert"
                >
                  {t("media_list_error")}
                </p>
              )}
              {currentCatalog.hasMore && (
                <div className="flex justify-center border-t border-border pt-4">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => void loadMore()}
                    disabled={currentCatalog.loadingMore}
                  >
                    {currentCatalog.loadingMore && (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    )}
                    {currentCatalog.loadingMore
                      ? t("media_loading_more")
                      : t("media_load_more")}
                  </Button>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={Boolean(currentEdit)}
        onOpenChange={(open) => {
          if (!open && currentBusy?.kind !== "edit") setEditState(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("media_edit_title")}</DialogTitle>
            <DialogDescription>{t("media_edit_description")}</DialogDescription>
          </DialogHeader>
          {currentEdit && (
            <form className="space-y-4" onSubmit={saveEdit}>
              <div className="space-y-1.5">
                <Label htmlFor="media-edit-display-name">
                  {t("media_display_name_label")}
                </Label>
                <Input
                  id="media-edit-display-name"
                  value={currentEdit.displayName}
                  onChange={(event) =>
                    setEditState((current) =>
                      current?.scope === scope
                        ? { ...current, displayName: event.target.value }
                        : current,
                    )
                  }
                  maxLength={200}
                  required
                  disabled={currentBusy?.kind === "edit"}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="media-edit-original-filename">
                  {t("media_original_filename_label")}
                </Label>
                <Input
                  id="media-edit-original-filename"
                  value={currentEdit.originalFilename}
                  onChange={(event) =>
                    setEditState((current) =>
                      current?.scope === scope
                        ? { ...current, originalFilename: event.target.value }
                        : current,
                    )
                  }
                  maxLength={255}
                  required
                  disabled={currentBusy?.kind === "edit"}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="media-edit-usage-trigger">
                  {t("media_usage_trigger_label")}
                </Label>
                <Textarea
                  id="media-edit-usage-trigger"
                  value={currentEdit.usageTrigger}
                  onChange={(event) =>
                    setEditState((current) =>
                      current?.scope === scope
                        ? {
                            ...current,
                            usageTrigger: event.target.value,
                            usageTriggerIsNull: false,
                          }
                        : current,
                    )
                  }
                  maxLength={500}
                  disabled={
                    currentBusy?.kind === "edit" ||
                    currentEdit.usageTriggerIsNull
                  }
                />
                <label className="flex items-center gap-2 text-sm text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={currentEdit.usageTriggerIsNull}
                    onChange={(event) =>
                      setEditState((current) =>
                        current?.scope === scope
                          ? {
                              ...current,
                              usageTriggerIsNull: event.target.checked,
                            }
                          : current,
                      )
                    }
                    disabled={currentBusy?.kind === "edit"}
                  />
                  {t("media_no_usage_trigger")}
                </label>
              </div>
              <div className="flex items-center justify-between gap-4 rounded-lg border border-border px-4 py-3">
                <Label htmlFor="media-edit-auto-suggest">
                  {t("media_auto_suggest_label")}
                </Label>
                <Switch
                  id="media-edit-auto-suggest"
                  checked={currentEdit.autoSuggest}
                  onCheckedChange={(checked) =>
                    setEditState((current) =>
                      current?.scope === scope
                        ? { ...current, autoSuggest: checked }
                        : current,
                    )
                  }
                  disabled={currentBusy?.kind === "edit"}
                />
              </div>
              <DialogFooter>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setEditState(null)}
                  disabled={currentBusy?.kind === "edit"}
                >
                  {t("media_cancel")}
                </Button>
                <Button
                  type="submit"
                  disabled={
                    currentBusy?.kind === "edit" ||
                    !currentEdit.displayName ||
                    !currentEdit.originalFilename
                  }
                >
                  {currentBusy?.kind === "edit" && (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  )}
                  {currentBusy?.kind === "edit"
                    ? t("media_saving")
                    : t("media_save")}
                </Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={Boolean(currentDelete)}
        onOpenChange={(open) => {
          if (!open && currentBusy?.kind !== "delete") setDeleteState(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("media_delete_title")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("media_delete_description")}{" "}
              <strong>{currentDelete?.asset.display_name}</strong>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={currentBusy?.kind === "delete"}>
              {t("media_cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                void deleteAsset();
              }}
              disabled={currentBusy?.kind === "delete"}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {currentBusy?.kind === "delete" && (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              )}
              {currentBusy?.kind === "delete"
                ? t("media_deleting")
                : t("media_delete_confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
