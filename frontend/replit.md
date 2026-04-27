# NeuroStack

## Overview

NeuroStack is a cognitive infrastructure layer for AI agents in healthcare — an MVP that proves the core concept: **decision → controlled context → LLM → trace**.

The LLM never decides business logic. A deterministic SRM (Service Reasoning Module) controls intent classification, context selection, and prompt engineering before any generation happens.

## Multi-tenant Architecture

### Users & Roles
| ID | Email | Password | clinic_id | Role |
|----|-------|----------|-----------|------|
| 1 | pilot1@clinic.com | pilot123 | 1 | clinic_admin |
| 2 | pilot2@clinic.com | pilot123 | 2 | clinic_admin |
| 3 | pilot3@clinic.com | pilot123 | 3 | clinic_admin |
| 4 | pilot4@clinic.com | pilot123 | 5 | clinic_admin |
| 5 | pilot5@clinic.com | pilot123 | 6 | clinic_admin |
| 6 | investidor@demo.com | demo123 | 4 | investor |
| 7 | admin@neurostack.com | global07 | 7 | super_admin |

### Clinic IDs
- 1: Clínica Odonto Prime (pilot1)
- 2: Instituto Face SP (pilot2)
- 3: Estética Rio Norte (pilot3)
- 4: Demo (investor) 
- 5: Sorriso Moderno BH (pilot4)
- 6: VisSorrir Curitiba (pilot5)
- 7: NeuroStack Admin Workspace (dogfooding — admin only)

### RBAC Enforcement
- **Token format**: `ns-<base64(email:clinicId:role)>` — role decoded server-side
- **Pilot isolation**: `clinic_id` always locked to token value — URL params ignored for `clinic_admin` role
- **Trace isolation**: `GET /api/traces/:id` returns 403 if trace.clinic_id ≠ auth.clinic_id (for non-admins)
- **Config isolation**: `GET /api/config` locks to token's clinic_id for non-super_admin
- **Admin API**: `GET/POST /api/admin/clinics`, `GET /api/admin/stats`, `GET /api/admin/demo-leads`, `PATCH /api/admin/clinics/:id/status` — requires Bearer super_admin token
- **Investor**: read-only + Demo Lead Gate modal on first login
- **`viewingClinicId`** in auth context: super_admin can switch which clinic they're viewing data for

### Demo Lead Gate
- `investidor@demo.com` sees a full-screen modal on first login requiring: Nome Completo, **E-mail Profissional**, Empresa/Fundo, Telefone/WhatsApp
- E-mail field validated (format) both client-side and server-side (regex `POST /api/demo-leads`)
- `demo_leads` table columns: `id, email (auth login), nome, email_contato (professional), empresa, telefone, created_at`
- Admin monitors leads in Master Admin → "Leads do Demo Gate" section (columns: Nome, E-mail, Empresa, Telefone, Data)
- `leadSubmitted` flag persisted in localStorage to avoid re-prompting

### RLHF Tracking
- All RLHF feedback persisted to `rlhf_feedback` DB table
- Admin submissions marked `source="[Admin]"` for differentiation
- Clinic admin submissions marked `source="Clínica #N"`

### Admin Dogfooding
- admin@neurostack.com uses clinic_id=7 "NeuroStack Admin Workspace" (isolated)
- Workspace has same Z-API/LLM credentials as clinic 1 (copied at migration)
- Admin can test chat, traces, config just like a real pilot

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Validation**: Zod (`zod/v4`), generated from OpenAPI
- **API codegen**: Orval (from OpenAPI spec)
- **Frontend**: React + Vite + Tailwind CSS + Lucide Icons
- **Build**: esbuild (CJS bundle for API server)

## Architecture

### Backend Pipeline (per request)

1. **Input** — Receive user message via `POST /api/chat`
2. **SRM** (`src/lib/srm.ts`) — Keyword/regex logic determines `intent`, `stage`, `action`, `constraints`, `handoff_required`, `safety_concern`, and `confidence`. Module-level `authority_done` state tracks whether authority has been established. Key constraint: `C_SEQUENCE_AUTHORITY_BEFORE_PRICE` — pricing is gated until authority turn completes.
3. **Context Selector** (`src/lib/contextSelector.ts`) — Receives SRM decision + action; for `generate_authority_first` selects `clinic_differentials` only (no pricing data); for `provide_price` selects `pricing_rules`
4. **Prompt Engine** (`src/lib/promptEngine.ts`) — Builds final prompt: user message + SRM block + selected context + base rules. Injects `CONSTRAINT_INSTRUCTIONS` for each active constraint (e.g. explicit "MUST NOT give prices" for C_SEQUENCE_AUTHORITY_BEFORE_PRICE)
5. **LLM Router** (`src/lib/mockLLM.ts`) — If clinic has `llm_provider` + `llm_api_key` configured (BYOK), makes a real HTTP call to OpenAI or Dropout (OpenAI-compatible). Falls back to deterministic mock on any error or if no key is configured.
6. **Trace** (`src/lib/traceStore.ts`) — Saves full `DecisionTrace` to in-memory store

### API Routes

- `POST /api/auth/login` — Mock auth (admin@neurostack.com / admin)
- `GET /api/clinics` — List all tenant clinics
- `POST /api/chat` — Full pipeline execution (accepts `clinic_id`)
- `GET /api/config?clinic_id=N` — Get clinic config for tenant N
- `POST /api/config` — Update clinic config (body includes `clinic_id`)
- `GET /api/traces?clinic_id=N` — List decision traces for tenant
- `GET /api/traces/stats?clinic_id=N` — Aggregated stats for tenant
- `GET /api/traces/:id` — Single trace detail
- `GET /api/healthz` — Health check

### Frontend Pages

- `/` (Login gate) — Shows login form if not authenticated; redirects to Chat on success
- `/` — Chat Simulator + Live Decision Trace Viewer (split panel)
- `/traces` — Trace history list
- `/traces/:id` — Full trace detail
- `/dashboard` — System stats overview
- `/config` — Clinic Config editor

### Multi-Tenant Architecture

- `AuthContext` (`src/context/auth.tsx`) — localStorage-backed auth state (token + email). Guards all routes.
- `TenantContext` (`src/context/tenant.tsx`) — Active `clinic_id` + clinic list loaded from `/api/clinics`. Switches context per tenant, resets conversation on switch.
- Sidebar tenant switcher — `Select` dropdown showing all seeded clinics.
- Per-tenant `authority_done` state in `chat.ts` — a `Map<number, boolean>` keyed by `clinic_id`.

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/api-server run dev` — run API server locally
- `pnpm --filter @workspace/neurostack run dev` — run frontend locally

## Design System

- **Primary/Backgrounds**: Deep Slate `#1a232c`
- **App Background**: Off-White `#F8FAFC`
- **Accent**: Sky Blue `#0284C7`
- **Typography**: Inter font
- **Style**: Minimalist, rounded-md borders, subtle shadows

## Artifacts

- `artifacts/api-server` — Express backend
- `artifacts/neurostack` — React frontend

## Security Layer (HIPAA/LGPD)

### Password Hashing
- All user passwords stored as **bcrypt** hashes (cost=12) — never plain text
- `auth.ts` uses `bcrypt.compare()` for login validation
- Startup migration (`db.ts`) auto-hashes any plain-text legacy passwords at boot
- New clinics created via Master Admin use `bcrypt.hashSync()` at creation time

### API Key Encryption at Rest (AES-256-GCM)
- Sensitive fields encrypted in DB: `llm_api_key`, `zapi_token`, `evolution_api_key`
- Key derivation: `SHA-256(SESSION_SECRET)` → 32-byte AES key
- Format stored: `enc:<iv_hex>:<ciphertext_hex>:<auth_tag_hex>`
- `encryption.ts` provides `encrypt()`, `decrypt()`, `encryptIfNeeded()`, `decryptIfNeeded()`
- `clinicConfigDB.ts` decrypts on read (`rowToConfig`), encrypts on write
- Startup migration (`db.ts`) auto-encrypts any plain-text keys at boot
- API keys **never** returned to frontend in plain text — GET /api/config returns `null` for sensitive fields

### Drizzle ORM Schema
- `schema.ts` — formal Drizzle schema for all 6 tables
- `drizzle.ts` — ORM instance using `drizzle-orm/better-sqlite3`
- **PostgreSQL migration path**: swap driver in `drizzle.ts` to `node-postgres`, change `sqliteTable` → `pgTable` in `schema.ts`

## Persistence

**SQLite** (`better-sqlite3`) — `artifacts/neurostack.db` (6 tables)

- `clinic_config` — multi-tenant clinic configs; sensitive API key fields AES-256-GCM encrypted
- `traces` — full decision traces with clinic_id FK  
- `users` — bcrypt-hashed passwords; strict clinic_id isolation
- `support_tickets` — per-clinic support; status (open/closed)
- `rlhf_feedback` — per-clinic feedback with `forced_override` flag
- `demo_leads` — investor demo gate registrations

`clinicConfigDB.ts` exposes `readClinicConfig(clinicId)`, `writeClinicConfig(config)`, `writeClinicApiCredentials(clinicId, creds)`. All functions decrypt on read and encrypt on write.

## Shared Libraries

- `lib/api-spec` — OpenAPI spec + Orval codegen config
- `lib/api-client-react` — Generated React Query hooks
- `lib/api-zod` — Generated Zod validation schemas
- `lib/db` — Drizzle ORM setup (available for future persistence)
