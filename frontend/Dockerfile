# ─── Stage 1: Build ──────────────────────────────────────────────────────────
FROM node:22-slim AS builder

RUN npm install -g pnpm@latest

WORKDIR /app

# Copy monorepo manifests + TypeScript base configs
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY tsconfig.base.json tsconfig.json ./

# Copy workspace packages (api-zod, db, api-spec, api-client-react)
COPY lib/ ./lib/

# Copy api-server manifest (for pnpm install scoping)
COPY artifacts/api-server/package.json ./artifacts/api-server/

# Install ALL deps (incl. devDeps needed for esbuild)
RUN pnpm install --frozen-lockfile

# Copy full api-server source
COPY artifacts/api-server/ ./artifacts/api-server/

# Compile TypeScript → dist/index.mjs via esbuild
RUN pnpm --filter @workspace/api-server run build

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM node:22-slim AS runtime

RUN npm install -g pnpm@latest

WORKDIR /app

# Copy monorepo manifests
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./

# Copy workspace packages (needed for pnpm to resolve native deps)
COPY lib/ ./lib/

# Copy api-server manifest
COPY artifacts/api-server/package.json ./artifacts/api-server/

# Install ONLY production deps (native modules: better-sqlite3, bcryptjs)
RUN pnpm install --frozen-lockfile --prod

# Copy compiled output from builder
COPY --from=builder /app/artifacts/api-server/dist ./artifacts/api-server/dist

WORKDIR /app/artifacts/api-server

# Cloud Run injects PORT; our server reads process.env.PORT
ENV NODE_ENV=production
EXPOSE 8080

# SQLite database file — ephemeral for MVP pilot
ENV DB_PATH=/data/neurostack.db
RUN mkdir -p /data

CMD ["node", "dist/index.mjs"]
