#!/bin/bash

# ============================================
# NeuroStack API - Script de Deploy para VPS
# ============================================
# Este script automatiza parte do processo de deploy
# Use com cuidado e revise antes de executar

set -e  # Parar em caso de erro

# Cores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Função para imprimir mensagens
info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Verificar se está rodando como root ou com sudo
check_permissions() {
    if [ "$EUID" -ne 0 ] && ! sudo -n true 2>/dev/null; then
        error "Este script precisa ser executado como root ou com sudo"
        exit 1
    fi
}

# Verificar se Docker está instalado
check_docker() {
    if ! command -v docker &> /dev/null; then
        error "Docker não está instalado. Por favor, instale o Docker primeiro."
        exit 1
    fi
    info "Docker encontrado: $(docker --version)"
}

# Verificar se Docker Compose está instalado
check_docker_compose() {
    if ! command -v docker compose &> /dev/null; then
        error "Docker Compose não está instalado. Por favor, instale o Docker Compose primeiro."
        exit 1
    fi
    info "Docker Compose encontrado: $(docker compose version)"
}

# Verificar se arquivo .env existe
check_env_file() {
    if [ ! -f .env ]; then
        warn "Arquivo .env não encontrado. Criando a partir de env.example..."
        if [ -f env.example ]; then
            cp env.example .env
            warn "Por favor, edite o arquivo .env antes de continuar!"
            exit 1
        else
            error "Arquivo env.example não encontrado!"
            exit 1
        fi
    fi
    info "Arquivo .env encontrado"
}

# Gerar SECRET_KEY se não estiver definida
generate_secret_key() {
    if grep -q "SECRET_KEY=dev-secret-key-change-in-production" .env; then
        warn "SECRET_KEY padrão detectada. Gerando nova chave..."
        SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || openssl rand -hex 32)
        if [ -n "$SECRET_KEY" ]; then
            # Atualizar .env (Linux/Mac)
            if [[ "$OSTYPE" == "darwin"* ]]; then
                sed -i '' "s/SECRET_KEY=.*/SECRET_KEY=$SECRET_KEY/" .env
            else
                sed -i "s/SECRET_KEY=.*/SECRET_KEY=$SECRET_KEY/" .env
            fi
            info "Nova SECRET_KEY gerada e configurada"
        else
            warn "Não foi possível gerar SECRET_KEY automaticamente. Configure manualmente no .env"
        fi
    fi
}

# Parar containers existentes
stop_containers() {
    info "Parando containers existentes..."
    docker compose down 2>/dev/null || true
}

# Construir imagens
build_images() {
    info "Construindo imagens Docker..."
    docker compose build --no-cache
}

# Iniciar containers
start_containers() {
    info "Iniciando containers..."
    docker compose up -d
    
    info "Aguardando serviços iniciarem..."
    sleep 10
}

# Verificar saúde dos serviços
check_health() {
    info "Verificando saúde dos serviços..."
    
    # Verificar PostgreSQL
    if docker compose exec -T postgres pg_isready -U neurostack > /dev/null 2>&1; then
        info "✓ PostgreSQL está saudável"
    else
        warn "✗ PostgreSQL não está respondendo"
    fi
    
    # Verificar Redis
    if docker compose exec -T redis redis-cli ping > /dev/null 2>&1; then
        info "✓ Redis está saudável"
    else
        warn "✗ Redis não está respondendo"
    fi
    
    # Verificar API
    sleep 5
    if curl -f http://localhost:8000/health > /dev/null 2>&1; then
        info "✓ API está respondendo"
    else
        warn "✗ API não está respondendo. Verifique os logs com: docker compose logs api"
    fi
}

# Executar migrações
run_migrations() {
    info "Executando migrações do banco de dados..."
    if docker compose exec -T api alembic upgrade head 2>/dev/null; then
        info "✓ Migrações executadas com sucesso"
    else
        warn "✗ Erro ao executar migrações. Execute manualmente: docker compose exec api alembic upgrade head"
    fi
}

# Mostrar status
show_status() {
    info "Status dos containers:"
    docker compose ps
    
    echo ""
    info "Endpoints disponíveis:"
    echo "  - API: http://localhost:8000"
    echo "  - Docs: http://localhost:8000/docs"
    echo "  - Health: http://localhost:8000/health"
    
    echo ""
    info "Para ver logs:"
    echo "  docker compose logs -f"
    echo "  docker compose logs -f api"
}

# Função principal
main() {
    echo "============================================"
    echo "  NeuroStack API - Deploy para VPS"
    echo "============================================"
    echo ""
    
    # Verificações
    check_permissions
    check_docker
    check_docker_compose
    check_env_file
    generate_secret_key
    
    echo ""
    read -p "Deseja continuar com o deploy? (s/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Ss]$ ]]; then
        info "Deploy cancelado pelo usuário"
        exit 0
    fi
    
    echo ""
    
    # Deploy
    stop_containers
    build_images
    start_containers
    check_health
    run_migrations
    
    echo ""
    show_status
    
    echo ""
    info "Deploy concluído!"
    warn "Lembre-se de:"
    warn "  1. Verificar e ajustar o arquivo .env com valores de produção"
    warn "  2. Configurar Nginx como reverse proxy (opcional)"
    warn "  3. Configurar SSL com Let's Encrypt (opcional)"
    warn "  4. Configurar backups regulares"
    warn "  5. Configurar monitoramento"
}

# Executar função principal
main

