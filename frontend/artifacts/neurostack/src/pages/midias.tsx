import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  Download,
  FileAudio,
  FileImage,
  FileText,
  FileVideo,
  FolderOpen,
  Loader2,
  Pencil,
  RefreshCw,
  Search,
  Sparkles,
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { CreditAllocationNotice } from "@/components/plan-usage-summary";
import { useAuth } from "@/context/auth";
import { useI18n } from "@/context/i18n";
import { getIntentLabel } from "@/lib/intent-presentation";
import type { TranslationKey } from "@/locales/en";

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
  // The intents whose turns may send this file, in the API's canonical order.
  trigger_intents: string[];
  created_at: string;
  updated_at: string;
}

type CategoryByteLimits = Record<MediaCategory, number>;

interface MediaTopic {
  id: string;
  intents: string[];
}

// GET /v1/media-assets/bindable-intents, per clinic. "loading" and "error"
// promise nothing: no delivery rule, no client-side size check.
interface BindableState {
  scope: string;
  status: "loading" | "ready" | "error";
  // The storage allowlist, in the API's canonical order.
  intents: string[];
  // The "Quando enviar" checkboxes, in screen order: each topic id is an
  // intent id (its label) and binds every intent it lists.
  topics: MediaTopic[];
  deliveryEnabled: boolean;
  maxBytes: CategoryByteLimits | null;
  // The canonical MIME types the clinic's channel sends (Z-API: no OGG or
  // M4A). Optional in the contract: null when the API does not publish it,
  // sends null or sends something malformed, and then no format alert is
  // shown.
  sendableMimeTypes: string[] | null;
}

function loadingBindable(scope: string): BindableState {
  return {
    scope,
    status: "loading",
    intents: [],
    topics: [],
    deliveryEnabled: false,
    maxBytes: null,
    sendableMimeTypes: null,
  };
}

interface MediaListResponse {
  items: MediaAsset[];
  next_cursor: string | null;
  has_more: boolean;
}

type FeedbackKind = "upload" | "edit" | "delete" | "download";

interface ListFilters {
  category: MediaCategory | "";
  name: string;
}

interface CatalogState extends ListFilters {
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
  name: string;
  usageTrigger: string;
  usageTriggerIsNull: boolean;
  autoSuggest: boolean;
  triggerIntents: string[];
}

interface DeleteState {
  scope: string;
  asset: MediaAsset;
}

const EMPTY_CATALOG: Omit<CatalogState, "scope" | "category" | "name"> = {
  items: [],
  nextCursor: null,
  hasMore: false,
  loading: false,
  loadingMore: false,
  error: false,
};

// The order the owner lists the four types in, used by both selects.
const MEDIA_CATEGORIES: readonly MediaCategory[] = [
  "image",
  "video",
  "audio",
  "pdf",
];

const CATEGORY_LABEL_KEY: Record<MediaCategory, TranslationKey> = {
  image: "media_category_image",
  video: "media_category_video",
  audio: "media_category_audio",
  pdf: "media_category_pdf",
};

const CATEGORY_HELP_KEY: Record<MediaCategory, TranslationKey> = {
  image: "media_file_help_image",
  video: "media_file_help_video",
  audio: "media_file_help_audio",
  pdf: "media_file_help_pdf",
};

// The same help with the API's limit in {limit}; the key above is the
// fallback while max_bytes_by_category is not read.
const CATEGORY_HELP_LIMITS_KEY: Record<MediaCategory, TranslationKey> = {
  image: "media_file_help_image_limits",
  video: "media_file_help_video_limits",
  audio: "media_file_help_audio_limits",
  pdf: "media_file_help_pdf_limits",
};

const CATEGORY_TOO_LARGE_KEY: Record<MediaCategory, TranslationKey> = {
  image: "media_file_too_large_image",
  video: "media_file_too_large_video",
  audio: "media_file_too_large_audio",
  pdf: "media_file_too_large_pdf",
};

interface MediaFormat {
  mime: string;
  category: MediaCategory;
  // Extensions a file of this format may carry; the first one is canonical.
  extensions: readonly string[];
}

// Mirrors MEDIA_CANONICAL_MIME_TYPES in src/services/media_assets.py. The
// server decides the real format from the content signature.
const MEDIA_FORMATS: readonly MediaFormat[] = [
  { mime: "image/jpeg", category: "image", extensions: ["jpg", "jpeg"] },
  { mime: "image/png", category: "image", extensions: ["png"] },
  { mime: "video/mp4", category: "video", extensions: ["mp4"] },
  { mime: "audio/mpeg", category: "audio", extensions: ["mp3"] },
  { mime: "audio/ogg", category: "audio", extensions: ["ogg", "opus"] },
  { mime: "audio/mp4", category: "audio", extensions: ["m4a"] },
  { mime: "application/pdf", category: "pdf", extensions: ["pdf"] },
];

const NAME_SEARCH_DEBOUNCE_MS = 300;
const NAME_MAX_LENGTH = 200;

const UPLOAD_ERROR_KEY: Record<string, TranslationKey> = {
  media_size_exceeded: "media_upload_error_size",
  media_request_size_exceeded: "media_upload_error_size",
  media_type_unsupported: "media_upload_error_format",
  media_type_mismatch: "media_upload_error_format",
  media_signature_invalid: "media_upload_error_format",
  media_audio_not_mono: "media_upload_error_not_mono",
  media_storage_cap_exceeded: "media_upload_error_storage_full",
  credit_pending: "media_upload_error_credits",
  credit_category_exhausted: "media_upload_error_credits",
  credit_suspended: "media_upload_error_credits",
  media_concurrency_exceeded: "media_upload_error_busy",
  media_storage_unavailable: "media_upload_error_busy",
};

// Chrome and Safari label .m4a as audio/x-m4a; the server folds the same
// aliases into audio/mp4 (canonical_declared_mime).
const MIME_ALIASES: Record<string, string> = {
  "audio/x-m4a": "audio/mp4",
  "audio/m4a": "audio/mp4",
};

function canonicalMime(type: string) {
  const mime = type.split(";")[0].trim().toLowerCase();
  return MIME_ALIASES[mime] ?? mime;
}

function extensionOf(name: string) {
  return /\.([^.]+)$/.exec(name.toLowerCase())?.[1];
}

function formatOfMime(mime: string) {
  return MEDIA_FORMATS.find((format) => format.mime === mime);
}

// The accepted format a picked file claims: by its declared type first, then
// by its extension, because some systems declare no type at all.
function formatOfFile(file: File) {
  const extension = extensionOf(file.name);
  return (
    formatOfMime(canonicalMime(file.type)) ??
    MEDIA_FORMATS.find(
      (format) => extension !== undefined && format.extensions.includes(extension),
    )
  );
}

// A name keeps its own extension when its format allows it (photo.jpeg stays
// .jpeg); anything else gets the format's canonical one.
function extensionFor(mime: string, filename: string) {
  const format = formatOfMime(mime);
  const own = extensionOf(filename);
  if (!format) return own ?? "";
  return own && format.extensions.includes(own) ? own : format.extensions[0];
}

function stripExtension(name: string, mime: string) {
  const format = formatOfMime(mime);
  const own = extensionOf(name);
  return format && own && format.extensions.includes(own)
    ? name.slice(0, -(own.length + 1))
    : name;
}

// "Nome do arquivo" is the one name the clinic sees: the list, the download
// and the file a patient receives all use it. The extension is locked to the
// real format so a rename can never claim another type.
function storedNames(name: string, mime: string, extension: string) {
  // NFC so a name macOS handed over decomposed is stored as it is typed; the
  // trailing dots and spaces would otherwise end up before the extension.
  const base = stripExtension(name.normalize("NFC").trim(), mime).replace(
    /[.\s]+$/,
    "",
  );
  return {
    display_name: base,
    original_filename: extension ? `${base}.${extension}` : base,
  };
}

// The name the catalog shows is the one that goes out: the download and the
// file a patient receives both carry original_filename. Rows saved before the
// single name field may still hold a different display_name; it is not shown.
function fileNameOf(asset: MediaAsset) {
  return asset.original_filename;
}

function formatLabel(mime: string, filename: string) {
  if (mime === "audio/ogg") return "OGG/OPUS";
  return extensionFor(mime, filename).toUpperCase();
}

function acceptFor(category: MediaCategory | "") {
  return MEDIA_FORMATS.filter(
    (format) => !category || format.category === category,
  )
    .flatMap((format) => [
      format.mime,
      ...format.extensions.map((extension) => `.${extension}`),
    ])
    .join(",");
}

// Mirrors _NAME_SEARCH_* in src/api/routes/media_assets.py exactly: lower
// case, the listed accented letters to their base, combining marks dropped.
const NAME_SEARCH_ACCENTED =
  "\u00e1\u00e0\u00e2\u00e3\u00e4\u00e5\u00e9\u00e8\u00ea\u00eb\u00ed\u00ec\u00ee\u00ef" +
  "\u00f3\u00f2\u00f4\u00f5\u00f6\u00fa\u00f9\u00fb\u00fc\u00e7\u00f1";
const NAME_SEARCH_PLAIN = "aaaaaaeeeeiiiiooooouuuucn";

function foldName(text: string) {
  let folded = "";
  for (const character of text.toLowerCase()) {
    const code = character.codePointAt(0) ?? 0;
    if (code >= 0x0300 && code <= 0x036f) continue;
    const index = NAME_SEARCH_ACCENTED.indexOf(character);
    folded += index === -1 ? character : NAME_SEARCH_PLAIN[index];
  }
  return folded;
}

// Client-side twin of the list filters, used only to place an item the user
// just uploaded or renamed; the server stays the source of every page.
function matchesFilters(asset: MediaAsset, filters: ListFilters) {
  if (filters.category && asset.category !== filters.category) return false;
  return (
    !filters.name ||
    foldName(asset.original_filename).includes(foldName(filters.name))
  );
}

// Reads the bindable-intents body strictly: anything off-contract in the
// fields that drive the binding, the promise or the size block is treated as
// a failed read, so the page falls back to promising nothing. The optional,
// alert-only sendable_mime_types never takes the rest of the body down.
function parseBindable(
  body: unknown,
): Omit<BindableState, "scope" | "status"> | null {
  if (!body || typeof body !== "object") return null;
  const {
    intents,
    topics,
    delivery_enabled,
    max_bytes_by_category,
    sendable_mime_types,
  } = body as {
    intents?: unknown;
    topics?: unknown;
    delivery_enabled?: unknown;
    max_bytes_by_category?: unknown;
    sendable_mime_types?: unknown;
  };
  if (
    !Array.isArray(intents) ||
    !intents.every((intent) => typeof intent === "string") ||
    typeof delivery_enabled !== "boolean" ||
    !max_bytes_by_category ||
    typeof max_bytes_by_category !== "object"
  ) {
    return null;
  }
  const parsedTopics = parseTopics(topics, intents as string[]);
  if (!parsedTopics) return null;
  const limits = max_bytes_by_category as Record<string, unknown>;
  const maxBytes = {} as CategoryByteLimits;
  for (const category of MEDIA_CATEGORIES) {
    const limit = limits[category];
    if (typeof limit !== "number" || !Number.isFinite(limit) || limit <= 0)
      return null;
    maxBytes[category] = limit;
  }
  // Absent, null or malformed all read as "not published": no format alert.
  const sendableMimeTypes =
    Array.isArray(sendable_mime_types) &&
    sendable_mime_types.every((mime) => typeof mime === "string")
      ? (sendable_mime_types as string[])
      : null;
  return {
    intents: intents as string[],
    topics: parsedTopics,
    deliveryEnabled: delivery_enabled,
    maxBytes,
    sendableMimeTypes,
  };
}

// Every topic needs a unique string id and a non-empty list of intents the
// allowlist carries, and the topics must split the allowlist exactly: each
// intent in one topic, none left out. One bad topic fails the whole read, and
// so does an empty list (no checkbox, and a save would unbind everything).
function parseTopics(
  topics: unknown,
  allowed: readonly string[],
): MediaTopic[] | null {
  if (!Array.isArray(topics) || topics.length === 0) return null;
  const parsed: MediaTopic[] = [];
  for (const topic of topics) {
    if (!topic || typeof topic !== "object") return null;
    const { id, intents } = topic as { id?: unknown; intents?: unknown };
    if (
      typeof id !== "string" ||
      parsed.some((other) => other.id === id) ||
      !Array.isArray(intents) ||
      intents.length === 0 ||
      !intents.every(
        (intent) => typeof intent === "string" && allowed.includes(intent),
      )
    ) {
      return null;
    }
    parsed.push({ id, intents: [...(intents as string[])] });
  }
  const bound = parsed.flatMap((topic) => topic.intents);
  if (
    bound.length !== allowed.length ||
    !allowed.every((intent) => bound.includes(intent))
  ) {
    return null;
  }
  return parsed;
}

// A topic reads as checked when any of its intents is bound, so a file bound
// to part of a topic (before topics existed) still shows it.
function isTopicSelected(topic: MediaTopic, selected: readonly string[]) {
  return topic.intents.some((intent) => selected.includes(intent));
}

function toggleTopic(
  selected: readonly string[],
  topic: MediaTopic,
  on: boolean,
) {
  const rest = selected.filter((intent) => !topic.intents.includes(intent));
  return on ? [...rest, ...topic.intents] : rest;
}

// What is saved is what is shown: every intent of every checked topic, in
// the API's canonical order. An intent no topic carries (a legacy "faq" or
// "saudacao") is dropped, because the server would refuse it with a 422.
function boundIntents(
  selected: readonly string[],
  bindable: Pick<BindableState, "intents" | "topics">,
) {
  const checked = bindable.topics.filter((topic) =>
    isTopicSelected(topic, selected),
  );
  return bindable.intents.filter((intent) =>
    checked.some((topic) => topic.intents.includes(intent)),
  );
}

// split/join, not replace: a file name or label must never be read as a
// replacement pattern.
function fill(template: string, values: Record<string, string>) {
  return Object.entries(values).reduce(
    (text, [key, value]) => text.split(`{${key}}`).join(value),
    template,
  );
}

function formatMegabyteAmount(bytes: number, locale: string) {
  return new Intl.NumberFormat(locale === "pt" ? "pt-BR" : "en-US", {
    maximumFractionDigits: 1,
  }).format(bytes / 1_000_000);
}

function formatMegabytes(bytes: number, locale: string) {
  return `${formatMegabyteAmount(bytes, locale)} MB`;
}

async function uploadErrorKey(response: Response): Promise<TranslationKey> {
  try {
    const body = (await response.json()) as { detail?: { code?: string } };
    const code = body.detail?.code;
    if (code && code in UPLOAD_ERROR_KEY) return UPLOAD_ERROR_KEY[code];
  } catch {
    // a body that is not the documented envelope falls back to the generic text
  }
  return "media_upload_error";
}

function authToken() {
  try {
    const raw = localStorage.getItem("ns_auth");
    return raw ? ((JSON.parse(raw) as { token?: string }).token ?? "") : "";
  } catch {
    return "";
  }
}

function categoryIcon(category: MediaCategory) {
  if (category === "video") return FileVideo;
  if (category === "audio") return FileAudio;
  if (category === "pdf") return FileText;
  return FileImage;
}

const SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

function AutoSuggestSection({
  id,
  checked,
  onCheckedChange,
  disabled,
}: {
  id: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled: boolean;
}) {
  const { t } = useI18n();
  return (
    <section className="space-y-2" aria-labelledby={`${id}-title`}>
      <h3
        id={`${id}-title`}
        className="flex items-center gap-2 text-sm font-semibold text-foreground"
      >
        <Sparkles className="h-4 w-4 shrink-0 text-sky-500" aria-hidden />
        {t("media_auto_suggest_label")}
      </h3>
      <p className="text-sm text-muted-foreground">
        {t("media_auto_suggest_description")}
      </p>
      <div className="flex items-center justify-between gap-4 rounded-lg border border-border bg-muted/30 px-4 py-3">
        <div className="space-y-0.5">
          <Label htmlFor={id} className="text-sm font-medium">
            {t("media_auto_suggest_label")}
          </Label>
          <p className="text-sm text-muted-foreground">
            {t("media_auto_suggest_help")}
          </p>
        </div>
        <Switch
          id={id}
          checked={checked}
          onCheckedChange={onCheckedChange}
          disabled={disabled}
        />
      </div>
    </section>
  );
}

// "Quando enviar": one checkbox per topic, in the API's order and labelled
// like the intent its id names. Checking a topic binds all of its intents and
// unchecking it unbinds them all. Greetings and FAQ ("Outras") are always
// included by the server and get no checkbox. Two columns at most, because
// the edit dialog is 512 px wide and the breakpoints follow the window.
function TriggerIntentsField({
  id,
  bindable,
  selected,
  onChange,
  disabled,
  onRetry,
}: {
  id: string;
  bindable: BindableState;
  selected: readonly string[];
  onChange: (selected: string[]) => void;
  disabled: boolean;
  onRetry: () => void;
}) {
  const { t } = useI18n();
  return (
    <fieldset className="space-y-1.5" aria-describedby={`${id}-help`}>
      <legend className="text-sm font-medium leading-none">
        {t("media_trigger_intents_label")}
      </legend>
      <p id={`${id}-help`} className="pt-1 text-xs text-muted-foreground">
        {t("media_trigger_intents_help")}
      </p>
      {bindable.status === "loading" ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          {t("media_trigger_intents_loading")}
        </p>
      ) : bindable.status === "error" ? (
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm text-destructive">
            {t("media_trigger_intents_error")}
          </p>
          <Button type="button" size="sm" variant="outline" onClick={onRetry}>
            <RefreshCw className="mr-2 h-4 w-4" />
            {t("media_retry")}
          </Button>
        </div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {bindable.topics.map((topic) => (
            <label
              key={topic.id}
              className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm"
            >
              <input
                type="checkbox"
                checked={isTopicSelected(topic, selected)}
                onChange={(event) =>
                  onChange(toggleTopic(selected, topic, event.target.checked))
                }
                disabled={disabled}
              />
              {getIntentLabel(topic.id, t)}
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}

// amber-700 on the light card and amber-400 on the dark one both clear WCAG
// AA (4.5:1) at 12 px; amber-600 measured 3.2:1.
function MediaWarning({ children }: { children: React.ReactNode }) {
  return (
    <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-400">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{children}</span>
    </p>
  );
}

// The one line under the automatic-suggestion card that says what really
// happens. Nothing is said while the read is loading; a failed read says it
// could not be checked, never that sending is off; the rule shows only while
// the switch is on, because with it off the file is never sent.
function DeliveryNotice({
  bindable,
  autoSuggest,
  testId,
}: {
  bindable: BindableState;
  autoSuggest: boolean;
  testId: string;
}) {
  const { t } = useI18n();
  if (bindable.status === "loading") return null;
  let text: string | null;
  if (bindable.status === "error") {
    text = t("media_delivery_unknown");
  } else if (!bindable.deliveryEnabled) {
    text = t("media_delivery_inactive");
  } else {
    text = autoSuggest
      ? fill(t("media_delivery_rule"), {
          when: t("media_trigger_intents_label"),
          trigger: t("media_usage_trigger_label"),
        })
      : null;
  }
  if (text === null) return null;
  return (
    <p className="text-sm text-muted-foreground" data-testid={testId}>
      {text}
    </p>
  );
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

  const [categoryFilter, setCategoryFilter] = useState<MediaCategory | "">("");
  const [nameQuery, setNameQuery] = useState("");
  const [appliedName, setAppliedName] = useState("");
  const listKey = JSON.stringify([scope, categoryFilter, appliedName]);
  const listKeyRef = useRef(listKey);
  listKeyRef.current = listKey;

  const mutationControllersRef = useRef(new Set<AbortController>());
  const listControllersRef = useRef(new Set<AbortController>());
  const catalogRevisionRef = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const autoNameRef = useRef("");
  const [reloadVersion, setReloadVersion] = useState(0);
  const [catalog, setCatalog] = useState<CatalogState>({
    scope,
    category: categoryFilter,
    name: appliedName,
    ...EMPTY_CATALOG,
  });
  const [file, setFile] = useState<File | null>(null);
  const [fileType, setFileType] = useState<MediaCategory | "">("");
  const [fileName, setFileName] = useState("");
  const [usageTrigger, setUsageTrigger] = useState("");
  const [autoSuggest, setAutoSuggest] = useState(false);
  const [triggerIntents, setTriggerIntents] = useState<string[]>([]);
  const [bindableVersion, setBindableVersion] = useState(0);
  const [bindable, setBindable] = useState<BindableState>(() =>
    loadingBindable(scope),
  );
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
    messageKey?: TranslationKey;
  } | null>(null);
  // limitBytes fills the {limit} of a too-large message with the API's value.
  const [fileError, setFileError] = useState<{
    key: TranslationKey;
    limitBytes?: number;
  } | null>(null);

  function abortAll(controllers: Set<AbortController>) {
    for (const controller of controllers) controller.abort();
    controllers.clear();
  }

  function requestController(controllers: Set<AbortController>) {
    const controller = new AbortController();
    controllers.add(controller);
    return controller;
  }

  function mediaUrl(
    path = "",
    cursor?: string | null,
    filters?: ListFilters,
  ) {
    const params = new URLSearchParams();
    if (requiresSelectedClinic) params.set("tenant_id", viewingClinicId);
    if (filters?.category) params.set("category", filters.category);
    if (filters?.name) params.set("name", filters.name);
    if (cursor) params.set("cursor", cursor);
    const query = params.toString();
    return `/v1/media-assets${path}${query ? `?${query}` : ""}`;
  }

  function clearFileInput() {
    setFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  useEffect(() => {
    const handle = setTimeout(
      () => setAppliedName(nameQuery.trim()),
      NAME_SEARCH_DEBOUNCE_MS,
    );
    return () => clearTimeout(handle);
  }, [nameQuery]);

  // A clinic change drops every in-flight mutation and every per-clinic draft.
  useEffect(() => {
    const mutations = mutationControllersRef.current;
    abortAll(mutations);
    clearFileInput();
    setFileType("");
    setFileName("");
    autoNameRef.current = "";
    setUsageTrigger("");
    setAutoSuggest(false);
    setTriggerIntents([]);
    setFileError(null);
    setUploadingScope(null);
    setEditState(null);
    setDeleteState(null);
    setBusyAsset(null);
    setFeedback(null);
    return () => abortAll(mutations);
  }, [scope]);

  // What the clinic may bind a file to, whether the agent sends it today and
  // the per-category limits; read again for every clinic.
  useEffect(() => {
    const loading = loadingBindable(scope);
    setBindable(loading);
    if (!canManage) return;
    const controller = new AbortController();
    void fetch(mediaUrl("/bindable-intents"), {
      headers: { Authorization: `Bearer ${authToken()}` },
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`media-bindable-${response.status}`);
        const parsed = parseBindable(await response.json());
        if (!parsed) throw new Error("media-bindable-shape");
        return parsed;
      })
      .then((parsed) => {
        if (controller.signal.aborted) return;
        setBindable({ scope, status: "ready", ...parsed });
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setBindable({ ...loading, status: "error" });
      });
    return () => controller.abort();
    // bindableVersion intentionally replays the read for Retry.
  }, [canManage, scope, bindableVersion]);

  // The first page for the current clinic and filters. Changing a filter only
  // replaces the list; it never aborts an upload, edit or delete.
  useEffect(() => {
    const lists = listControllersRef.current;
    abortAll(lists);
    const filters: ListFilters = {
      category: categoryFilter,
      name: appliedName,
    };
    setCatalog({ scope, ...filters, ...EMPTY_CATALOG, loading: canManage });

    if (!canManage) return () => abortAll(lists);

    const requestScope = scope;
    const requestKey = listKey;
    // A first page answered after an upload, edit or delete landed may predate
    // it, so it is asked for again rather than shown or dropped: dropping it
    // would leave a filtered list empty, and showing it could undo the change.
    const loadFirstPage = () => {
      const requestRevision = catalogRevisionRef.current;
      const controller = requestController(lists);
      void fetch(mediaUrl("", null, filters), {
        headers: { Authorization: `Bearer ${authToken()}` },
        signal: controller.signal,
      })
        .then(async (response) => {
          if (!response.ok) throw new Error(`media-list-${response.status}`);
          return (await response.json()) as MediaListResponse;
        })
        .then((page) => {
          if (listKeyRef.current !== requestKey || controller.signal.aborted)
            return;
          if (catalogRevisionRef.current !== requestRevision) {
            loadFirstPage();
            return;
          }
          setCatalog({
            scope: requestScope,
            ...filters,
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
            listKeyRef.current !== requestKey
          ) {
            return;
          }
          if (catalogRevisionRef.current !== requestRevision) {
            loadFirstPage();
            return;
          }
          setCatalog({
            scope: requestScope,
            ...filters,
            ...EMPTY_CATALOG,
            error: true,
          });
        })
        .finally(() => lists.delete(controller));
    };
    loadFirstPage();

    return () => abortAll(lists);
    // reloadVersion intentionally replays the initial keyset page for Retry.
  }, [canManage, reloadVersion, listKey]);

  const currentCatalog =
    catalog.scope === scope &&
    catalog.category === categoryFilter &&
    catalog.name === appliedName
      ? catalog
      : {
          scope,
          category: categoryFilter,
          name: appliedName,
          ...EMPTY_CATALOG,
          loading: canManage,
        };
  const filtersActive = Boolean(categoryFilter || appliedName);
  const currentBindable =
    bindable.scope === scope ? bindable : loadingBindable(scope);
  const categoryLimits =
    currentBindable.status === "ready" ? currentBindable.maxBytes : null;
  const retryBindable = () => setBindableVersion((version) => version + 1);
  const tooLargeText = (category: MediaCategory) =>
    categoryLimits
      ? fill(t("media_alert_too_large"), {
          limit: formatMegabytes(categoryLimits[category], locale),
        })
      : null;
  const megabytes = (bytes: number) => formatMegabytes(bytes, locale);
  // "5 MB para imagem, 20 MB para PDF, 16 MB para áudio ou vídeo", from the
  // API's limits; audio and video share a phrase only while they are equal.
  const limitsSummary = (limits: CategoryByteLimits) =>
    limits.audio === limits.video
      ? fill(t("media_limits_summary"), {
          image: megabytes(limits.image),
          pdf: megabytes(limits.pdf),
          audio_video: megabytes(limits.audio),
        })
      : fill(t("media_limits_summary_split"), {
          image: megabytes(limits.image),
          pdf: megabytes(limits.pdf),
          audio: megabytes(limits.audio),
          video: megabytes(limits.video),
        });
  // Each text keeps today's fixed wording while the limits are not read.
  const supportedFormatsText = (() => {
    if (!categoryLimits) return t("media_supported_formats");
    const values = MEDIA_CATEGORIES.map((category) => categoryLimits[category]);
    const min = Math.min(...values);
    const max = Math.max(...values);
    return fill(t("media_supported_formats_limits"), {
      range:
        min === max
          ? megabytes(max)
          : fill(t("media_limits_range"), {
              min: formatMegabyteAmount(min, locale),
              max: megabytes(max),
            }),
    });
  })();
  const fileHelpText = (category: MediaCategory | "") => {
    if (!categoryLimits)
      return t(category ? CATEGORY_HELP_KEY[category] : "media_file_help");
    return category
      ? fill(t(CATEGORY_HELP_LIMITS_KEY[category]), {
          limit: megabytes(categoryLimits[category]),
        })
      : fill(t("media_file_help_limits"), {
          limits: limitsSummary(categoryLimits),
        });
  };
  const feedbackErrorText = (key: TranslationKey) =>
    key === "media_upload_error_size" && categoryLimits
      ? fill(t("media_upload_error_size_limits"), {
          limits: limitsSummary(categoryLimits),
        })
      : t(key);
  const isTooLarge = (target: MediaAsset) =>
    categoryLimits !== null &&
    target.size_bytes > categoryLimits[target.category];
  // Only while sending is active: when it is not, the inactive notice already
  // says nothing is sent, whatever the format.
  const isUnsendableFormat = (mime: string) =>
    currentBindable.status === "ready" &&
    currentBindable.deliveryEnabled &&
    currentBindable.sendableMimeTypes !== null &&
    !currentBindable.sendableMimeTypes.includes(mime);
  const currentEdit = editState?.scope === scope ? editState : null;
  const currentDelete = deleteState?.scope === scope ? deleteState : null;
  const currentBusy = busyAsset?.scope === scope ? busyAsset : null;
  const currentFeedback = feedback?.scope === scope ? feedback : null;
  const isUploading = uploadingScope === scope;
  const fileFormat = file ? formatOfFile(file) : undefined;
  const fileExtension =
    file && fileFormat ? extensionFor(fileFormat.mime, file.name) : "";
  const uploadNames = fileFormat
    ? storedNames(fileName, fileFormat.mime, fileExtension)
    : null;
  const canUpload =
    canManage &&
    !isUploading &&
    Boolean(file && fileFormat && fileType && uploadNames?.display_name);

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
    const requestKey = listKey;
    const filters: ListFilters = {
      category: currentCatalog.category,
      name: currentCatalog.name,
    };
    const cursor = currentCatalog.nextCursor;
    const requestRevision = catalogRevisionRef.current;
    const controller = requestController(listControllersRef.current);
    setCatalog((current) =>
      current.scope === requestScope
        ? { ...current, loadingMore: true, error: false }
        : current,
    );
    try {
      const response = await fetch(mediaUrl("", cursor, filters), {
        headers: { Authorization: `Bearer ${authToken()}` },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`media-list-${response.status}`);
      const page = (await response.json()) as MediaListResponse;
      if (listKeyRef.current !== requestKey || controller.signal.aborted)
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
        listKeyRef.current !== requestKey
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
      listControllersRef.current.delete(controller);
    }
  }

  function rejectFile(
    event: React.ChangeEvent<HTMLInputElement>,
    key: TranslationKey,
    limitBytes?: number,
  ) {
    event.target.value = "";
    setFile(null);
    setFileError({ key, limitBytes });
  }

  function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0] ?? null;
    setFeedback(null);
    if (!selected) {
      setFile(null);
      setFileError(null);
      return;
    }
    const format = formatOfFile(selected);
    if (!format) {
      rejectFile(event, "media_upload_error_format");
      return;
    }
    if (fileType && format.category !== fileType) {
      rejectFile(event, "media_file_wrong_type");
      return;
    }
    // The limits come from the API (max_bytes_by_category). Until they are
    // read, or when the read failed, the server's 413 is the only check.
    const limit = categoryLimits?.[format.category];
    if (limit !== undefined && selected.size > limit) {
      rejectFile(event, CATEGORY_TOO_LARGE_KEY[format.category], limit);
      return;
    }
    setFileError(null);
    setFile(selected);
    setFileType(format.category);
    // A name the user typed survives a new file; one we filled in does not.
    // The updater runs later, so it must compare against the previous fill.
    const suggested = selected.name
      .normalize("NFC")
      .replace(/\.[^.]+$/, "")
      .slice(0, NAME_MAX_LENGTH);
    const previousSuggestion = autoNameRef.current;
    autoNameRef.current = suggested;
    setFileName((current) =>
      current === "" || current === previousSuggestion ? suggested : current,
    );
  }

  function handleTypeChange(value: MediaCategory | "") {
    setFileType(value);
    setFeedback(null);
    setFileError(null);
    if (file && formatOfFile(file)?.category !== value) clearFileInput();
  }

  async function uploadAsset(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canUpload || !file || !fileFormat || !uploadNames) return;
    const requestScope = scope;
    const controller = requestController(mutationControllersRef.current);
    // Some systems declare no type for .opus or .jpeg; the server needs the
    // declared type to agree with the signature, so send the one we resolved.
    const payload = formatOfMime(canonicalMime(file.type))
      ? file
      : new File([file], file.name, {
          type: fileFormat.mime,
          lastModified: file.lastModified,
        });
    const form = new FormData();
    form.append("file", payload);
    form.append("display_name", uploadNames.display_name);
    form.append("original_filename", uploadNames.original_filename);
    if (usageTrigger !== "") form.append("usage_trigger", usageTrigger);
    form.append("auto_suggest", autoSuggest ? "true" : "false");
    // One part per bound intent: the API reads trigger_intents as a repeated
    // field, never as one joined value.
    if (currentBindable.status === "ready") {
      for (const intent of boundIntents(triggerIntents, currentBindable)) {
        form.append("trigger_intents", intent);
      }
    }
    setUploadingScope(requestScope);
    setFeedback(null);
    try {
      const response = await fetch(mediaUrl(), {
        method: "POST",
        headers: { Authorization: `Bearer ${authToken()}` },
        body: form,
        signal: controller.signal,
      });
      if (!response.ok) {
        const messageKey = await uploadErrorKey(response);
        if (scopeRef.current !== requestScope || controller.signal.aborted)
          return;
        setFeedback({
          scope: requestScope,
          kind: "upload",
          success: false,
          messageKey,
        });
        return;
      }
      const created = (await response.json()) as MediaAsset;
      if (scopeRef.current !== requestScope || controller.signal.aborted)
        return;
      catalogRevisionRef.current += 1;
      setCatalog((current) => {
        if (current.scope !== requestScope) return current;
        const rest = current.items.filter((item) => item.id !== created.id);
        return {
          ...current,
          error: false,
          items: matchesFilters(created, current) ? [created, ...rest] : rest,
        };
      });
      clearFileInput();
      setFileType("");
      setFileName("");
      autoNameRef.current = "";
      setUsageTrigger("");
      setAutoSuggest(false);
      setTriggerIntents([]);
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
      mutationControllersRef.current.delete(controller);
      if (scopeRef.current === requestScope) setUploadingScope(null);
    }
  }

  function openEdit(asset: MediaAsset) {
    setFeedback(null);
    setEditState({
      scope,
      asset,
      // original_filename may hold up to 255 characters; display_name only 200.
      name: stripExtension(asset.original_filename, asset.mime_type).slice(
        0,
        NAME_MAX_LENGTH,
      ),
      usageTrigger: asset.usage_trigger ?? "",
      usageTriggerIsNull: asset.usage_trigger === null,
      autoSuggest: asset.auto_suggest,
      triggerIntents: [...asset.trigger_intents],
    });
  }

  const editExtension = currentEdit
    ? extensionFor(
        currentEdit.asset.mime_type,
        currentEdit.asset.original_filename,
      )
    : "";
  const editNames = currentEdit
    ? storedNames(currentEdit.name, currentEdit.asset.mime_type, editExtension)
    : null;

  async function saveEdit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!currentEdit || !editNames?.display_name) return;
    const requestScope = scope;
    const assetId = currentEdit.asset.id;
    const controller = requestController(mutationControllersRef.current);
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
          display_name: editNames.display_name,
          original_filename: editNames.original_filename,
          usage_trigger: currentEdit.usageTriggerIsNull
            ? null
            : currentEdit.usageTrigger,
          auto_suggest: currentEdit.autoSuggest,
          // The whole list replaces the bound intents. Without the allowlist
          // the field is left out, so a failed read never unbinds anything.
          ...(currentBindable.status === "ready"
            ? {
                trigger_intents: boundIntents(
                  currentEdit.triggerIntents,
                  currentBindable,
                ),
              }
            : {}),
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
              error: false,
              items: current.items.flatMap((item) => {
                if (item.id !== updated.id) return [item];
                return matchesFilters(updated, current) ? [updated] : [];
              }),
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
      mutationControllersRef.current.delete(controller);
      if (scopeRef.current === requestScope) setBusyAsset(null);
    }
  }

  async function downloadAsset(asset: MediaAsset) {
    const requestScope = scope;
    const controller = requestController(mutationControllersRef.current);
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
      mutationControllersRef.current.delete(controller);
      if (scopeRef.current === requestScope) setBusyAsset(null);
    }
  }

  async function deleteAsset() {
    if (!currentDelete) return;
    const requestScope = scope;
    const assetId = currentDelete.asset.id;
    const controller = requestController(mutationControllersRef.current);
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
      mutationControllersRef.current.delete(controller);
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

  const deleteName = currentDelete ? fileNameOf(currentDelete.asset) : "";

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
        <Badge variant="outline" className="whitespace-normal">
          {supportedFormatsText}
        </Badge>
      </div>

      {/* Credits (Group 6): the clinic's builder's media allocation, read
          only by a builder or super admin viewing a clinic; hidden while
          the credit API is unavailable. */}
      <CreditAllocationNotice
        category="media"
        locale={locale}
        tenantId={viewingClinicId}
        enabled={canManage && requiresSelectedClinic}
      />

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
                accept={acceptFor(fileType)}
                onChange={handleFileChange}
                required
                disabled={isUploading}
              />
              <p className="text-xs text-muted-foreground">
                {fileHelpText(fileType)}
              </p>
              {fileError && (
                <p className="text-sm text-destructive" role="alert">
                  {fill(t(fileError.key), {
                    limit:
                      fileError.limitBytes === undefined
                        ? ""
                        : formatMegabytes(fileError.limitBytes, locale),
                  })}
                </p>
              )}
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="media-file-name">
                  {t("media_file_name_label")}
                </Label>
                <div className="flex items-center gap-2">
                  <Input
                    id="media-file-name"
                    value={fileName}
                    onChange={(event) => setFileName(event.target.value)}
                    maxLength={NAME_MAX_LENGTH}
                    required
                    disabled={isUploading}
                  />
                  {fileExtension && (
                    <span
                      className="shrink-0 text-sm text-muted-foreground"
                      data-testid="media-file-extension"
                    >
                      .{fileExtension}
                    </span>
                  )}
                </div>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="media-file-type">
                  {t("media_file_type_label")}
                </Label>
                <select
                  id="media-file-type"
                  className={SELECT_CLASS}
                  value={fileType}
                  onChange={(event) =>
                    handleTypeChange(event.target.value as MediaCategory | "")
                  }
                  required
                  disabled={isUploading}
                >
                  <option value="">{t("media_file_type_placeholder")}</option>
                  {MEDIA_CATEGORIES.map((category) => (
                    <option key={category} value={category}>
                      {t(CATEGORY_LABEL_KEY[category])}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <TriggerIntentsField
              id="media-trigger-intents"
              bindable={currentBindable}
              selected={triggerIntents}
              onChange={setTriggerIntents}
              disabled={isUploading}
              onRetry={retryBindable}
            />

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

            <AutoSuggestSection
              id="media-auto-suggest"
              checked={autoSuggest}
              onCheckedChange={setAutoSuggest}
              disabled={isUploading}
            />
            <DeliveryNotice
              bindable={currentBindable}
              autoSuggest={autoSuggest}
              testId="media-delivery-notice"
            />
            {/* Polite live region, rendered (and in the accessibility tree)
                before its warnings so a screen reader announces them as the
                file changes. Never hide it while empty: a
                region that appears with its content is often not announced.
                Empty, it has no height and its margin collapses into the
                form's gap, so it adds no space. */}
            <div
              className="space-y-1"
              aria-live="polite"
              data-testid="media-upload-warnings"
            >
              {fileFormat && isUnsendableFormat(fileFormat.mime) && (
                <MediaWarning>{t("media_alert_format_not_sent")}</MediaWarning>
              )}
            </div>

            <div className="flex justify-end">
              <Button type="submit" disabled={!canUpload}>
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
          {currentFeedback.success
            ? t(`media_${currentFeedback.kind}_success`)
            : feedbackErrorText(
                currentFeedback.messageKey ??
                  `media_${currentFeedback.kind}_error`,
              )}
        </div>
      )}

      <Card>
        <CardHeader className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-base">
              {t("media_library_title")}
            </CardTitle>
            {!currentCatalog.loading && (
              <span className="text-xs text-muted-foreground">
                {currentCatalog.items.length} {t("media_items_loaded")}
              </span>
            )}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="relative flex-1">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                aria-hidden="true"
              />
              <Input
                type="search"
                className="pl-8"
                value={nameQuery}
                onChange={(event) => setNameQuery(event.target.value)}
                maxLength={NAME_MAX_LENGTH}
                placeholder={t("media_search_placeholder")}
                aria-label={t("media_search_label")}
              />
            </div>
            <select
              className={`${SELECT_CLASS} sm:w-48`}
              value={categoryFilter}
              onChange={(event) =>
                setCategoryFilter(event.target.value as MediaCategory | "")
              }
              aria-label={t("media_filter_type_label")}
            >
              <option value="">{t("media_filter_all_types")}</option>
              {MEDIA_CATEGORIES.map((category) => (
                <option key={category} value={category}>
                  {t(CATEGORY_LABEL_KEY[category])}
                </option>
              ))}
            </select>
          </div>
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
              {t(filtersActive ? "media_empty_filtered" : "media_empty")}
            </div>
          ) : (
            <div className="space-y-3">
              <Table aria-label={t("media_library_title")}>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("media_column_name")}</TableHead>
                    <TableHead className="hidden sm:table-cell">
                      {t("media_column_type")}
                    </TableHead>
                    <TableHead className="hidden sm:table-cell">
                      {t("media_column_format")}
                    </TableHead>
                    <TableHead className="text-right">
                      <span className="sr-only">
                        {t("media_column_actions")}
                      </span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {currentCatalog.items.map((asset) => {
                    const Icon = categoryIcon(asset.category);
                    const assetBusy = currentBusy?.id === asset.id;
                    const name = fileNameOf(asset);
                    const typeLabel = t(CATEGORY_LABEL_KEY[asset.category]);
                    const format = formatLabel(
                      asset.mime_type,
                      asset.original_filename,
                    );
                    const tooLarge = isTooLarge(asset);
                    const unsendable = isUnsendableFormat(asset.mime_type);
                    return (
                      <TableRow
                        key={asset.id}
                        data-testid={`media-item-${asset.id}`}
                      >
                        <TableCell>
                          <div className="flex min-w-0 items-start gap-2">
                            <Icon
                              className="mt-0.5 h-4 w-4 shrink-0 text-sky-500"
                              aria-hidden="true"
                            />
                            <div className="min-w-0">
                              <span className="font-medium [overflow-wrap:anywhere]">
                                {name}
                              </span>
                              {/* Below sm the type and format columns fold
                                  under the name so it keeps the row width. */}
                              <p className="mt-0.5 text-xs text-muted-foreground sm:hidden">
                                {typeLabel} · {format}
                              </p>
                              {(tooLarge || unsendable) && (
                                <div className="mt-1 space-y-0.5">
                                  {tooLarge && (
                                    <MediaWarning>
                                      {tooLargeText(asset.category)}
                                    </MediaWarning>
                                  )}
                                  {unsendable && (
                                    <MediaWarning>
                                      {t("media_alert_format_not_sent")}
                                    </MediaWarning>
                                  )}
                                </div>
                              )}
                            </div>
                          </div>
                        </TableCell>
                        <TableCell className="hidden sm:table-cell">
                          {typeLabel}
                        </TableCell>
                        <TableCell className="hidden whitespace-nowrap sm:table-cell">
                          {format}
                        </TableCell>
                        <TableCell>
                          <div className="flex justify-end gap-1">
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-8 w-8 p-0"
                              onClick={() => void downloadAsset(asset)}
                              disabled={assetBusy}
                              aria-label={`${t("media_download_action")}: ${name}`}
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
                              className="h-8 w-8 p-0"
                              onClick={() => openEdit(asset)}
                              disabled={assetBusy}
                              aria-label={`${t("media_edit_action")}: ${name}`}
                            >
                              <Pencil className="h-4 w-4" />
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              className="h-8 w-8 p-0"
                              onClick={() => setDeleteState({ scope, asset })}
                              disabled={assetBusy}
                              aria-label={`${t("media_delete_action")}: ${name}`}
                            >
                              <Trash2 className="h-4 w-4 text-destructive" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>

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
        {/* The dialog is fixed and centred and Radix locks the page scroll,
            so it scrolls itself once it is taller than the viewport. */}
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("media_edit_title")}</DialogTitle>
            <DialogDescription>{t("media_edit_description")}</DialogDescription>
          </DialogHeader>
          {currentEdit && (
            <form className="space-y-4" onSubmit={saveEdit}>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="media-edit-file-name">
                    {t("media_file_name_label")}
                  </Label>
                  <div className="flex items-center gap-2">
                    <Input
                      id="media-edit-file-name"
                      value={currentEdit.name}
                      onChange={(event) =>
                        setEditState((current) =>
                          current?.scope === scope
                            ? { ...current, name: event.target.value }
                            : current,
                        )
                      }
                      maxLength={NAME_MAX_LENGTH}
                      required
                      disabled={currentBusy?.kind === "edit"}
                    />
                    {editExtension && (
                      <span className="shrink-0 text-sm text-muted-foreground">
                        .{editExtension}
                      </span>
                    )}
                  </div>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="media-edit-file-type">
                    {t("media_file_type_label")}
                  </Label>
                  <Input
                    id="media-edit-file-type"
                    value={t(CATEGORY_LABEL_KEY[currentEdit.asset.category])}
                    readOnly
                    disabled
                  />
                </div>
              </div>
              {isTooLarge(currentEdit.asset) && (
                <MediaWarning>
                  {tooLargeText(currentEdit.asset.category)}
                </MediaWarning>
              )}
              {isUnsendableFormat(currentEdit.asset.mime_type) && (
                <MediaWarning>{t("media_alert_format_not_sent")}</MediaWarning>
              )}
              <TriggerIntentsField
                id="media-edit-trigger-intents"
                bindable={currentBindable}
                selected={currentEdit.triggerIntents}
                onChange={(selected) =>
                  setEditState((current) =>
                    current?.scope === scope
                      ? { ...current, triggerIntents: selected }
                      : current,
                  )
                }
                disabled={currentBusy?.kind === "edit"}
                onRetry={retryBindable}
              />
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
                  placeholder={t("media_usage_trigger_placeholder")}
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
              <AutoSuggestSection
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
              <DeliveryNotice
                bindable={currentBindable}
                autoSuggest={currentEdit.autoSuggest}
                testId="media-edit-delivery-notice"
              />
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
                    currentBusy?.kind === "edit" || !editNames?.display_name
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
            <AlertDialogTitle className="break-words">
              {t("media_delete_title").split("{name}").join(deleteName)}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("media_delete_description")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={currentBusy?.kind === "delete"}>
              {t("media_delete_cancel")}
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
