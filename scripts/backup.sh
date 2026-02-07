#!/bin/bash

# ============================================
# NeuroStack API - Script de Backup
# ============================================
# Este script faz backup do banco de dados PostgreSQL
# e dos volumes Docker

set -e

# Configurações
BACKUP_DIR="${BACKUP_DIR:-/opt/backups/neurostack}"
DATE=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS="${RETENTION_DAYS:-7}"

# Cores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Criar diretório de backup
mkdir -p "$BACKUP_DIR"

info "Iniciando backup em: $BACKUP_DIR"

# Verificar se docker compose está disponível
if ! command -v docker compose &> /dev/null; then
    error "Docker Compose não encontrado!"
    exit 1
fi

# Verificar se estamos no diretório correto
if [ ! -f "docker-compose.yml" ]; then
    error "docker-compose.yml não encontrado. Execute este script do diretório neurostack-api/"
    exit 1
fi

# Backup do banco de dados PostgreSQL
info "Fazendo backup do PostgreSQL..."
if docker compose exec -T postgres pg_dump -U neurostack neurostack > "$BACKUP_DIR/postgres_$DATE.sql" 2>/dev/null; then
    # Compactar
    gzip "$BACKUP_DIR/postgres_$DATE.sql"
    info "✓ Backup do PostgreSQL concluído: postgres_$DATE.sql.gz"
else
    error "✗ Falha ao fazer backup do PostgreSQL"
    exit 1
fi

# Backup de volumes (opcional - descomente se necessário)
# info "Fazendo backup dos volumes Docker..."
# 
# # Backup PostgreSQL volume
# docker run --rm \
#   -v neurostack-api_postgres_data:/data:ro \
#   -v "$BACKUP_DIR":/backup \
#   alpine tar czf "/backup/postgres_data_$DATE.tar.gz" -C /data . 2>/dev/null || warn "Falha ao fazer backup do volume PostgreSQL"
# 
# # Backup Redis volume
# docker run --rm \
#   -v neurostack-api_redis_data:/data:ro \
#   -v "$BACKUP_DIR":/backup \
#   alpine tar czf "/backup/redis_data_$DATE.tar.gz" -C /data . 2>/dev/null || warn "Falha ao fazer backup do volume Redis"
# 
# # Backup Qdrant volume
# docker run --rm \
#   -v neurostack-api_qdrant_data:/data:ro \
#   -v "$BACKUP_DIR":/backup \
#   alpine tar czf "/backup/qdrant_data_$DATE.tar.gz" -C /data . 2>/dev/null || warn "Falha ao fazer backup do volume Qdrant"

# Limpar backups antigos
info "Removendo backups mais antigos que $RETENTION_DAYS dias..."
find "$BACKUP_DIR" -name "postgres_*.sql.gz" -type f -mtime +$RETENTION_DAYS -delete 2>/dev/null || true
find "$BACKUP_DIR" -name "*_data_*.tar.gz" -type f -mtime +$RETENTION_DAYS -delete 2>/dev/null || true

# Mostrar tamanho do backup
BACKUP_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
info "Tamanho total dos backups: $BACKUP_SIZE"

info "Backup concluído com sucesso!"

