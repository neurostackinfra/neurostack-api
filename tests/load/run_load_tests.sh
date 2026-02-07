#!/bin/bash
# ============================================================================
# NeuroStack API - Load Test Runner Script
# ============================================================================
#
# Script para executar testes de carga do NeuroStack API.
#
# Uso:
#   ./tests/load/run_load_tests.sh [SCENARIO] [OPTIONS]
#
# Cenários:
#   smoke     - Validação rápida (10 usuários, 1 min)
#   load      - Teste de carga padrão (100 usuários, 5 min)
#   stress    - Teste de stress (500 usuários, 10 min)
#   spike     - Pico de tráfego repentino
#   endurance - Teste de longa duração (30 min)
#   benchmark - Target 1000 req/s
#
# Exemplos:
#   ./run_load_tests.sh smoke
#   ./run_load_tests.sh load --host http://staging.neurostack.ai
#   ./run_load_tests.sh benchmark --users 200
#
# ============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
SCENARIO="${1:-load}"
HOST="${HOST:-http://localhost:8000}"
OUTPUT_DIR="load_test_results"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Parse scenario-specific defaults
case $SCENARIO in
    smoke)
        USERS=10
        SPAWN_RATE=2
        DURATION=60
        ;;
    load)
        USERS=100
        SPAWN_RATE=10
        DURATION=300
        ;;
    stress)
        USERS=500
        SPAWN_RATE=50
        DURATION=600
        ;;
    spike)
        USERS=200
        SPAWN_RATE=200
        DURATION=180
        ;;
    endurance)
        USERS=100
        SPAWN_RATE=10
        DURATION=1800
        ;;
    benchmark)
        USERS=200
        SPAWN_RATE=20
        DURATION=600
        ;;
    *)
        echo -e "${RED}Unknown scenario: $SCENARIO${NC}"
        echo "Available scenarios: smoke, load, stress, spike, endurance, benchmark"
        exit 1
        ;;
esac

# Parse additional arguments
shift 2>/dev/null || true
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)
            HOST="$2"
            shift 2
            ;;
        --users)
            USERS="$2"
            shift 2
            ;;
        --spawn-rate)
            SPAWN_RATE="$2"
            shift 2
            ;;
        --duration)
            DURATION="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --web-ui)
            WEB_UI=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [SCENARIO] [OPTIONS]"
            echo ""
            echo "Scenarios:"
            echo "  smoke     - Quick validation (10 users, 1 min)"
            echo "  load      - Standard load test (100 users, 5 min)"
            echo "  stress    - Push limits (500 users, 10 min)"
            echo "  spike     - Sudden traffic spike"
            echo "  endurance - Long-running test (30 min)"
            echo "  benchmark - Target 1000 req/s"
            echo ""
            echo "Options:"
            echo "  --host URL         Target host (default: http://localhost:8000)"
            echo "  --users N          Number of users"
            echo "  --spawn-rate N     Users per second"
            echo "  --duration N       Test duration in seconds"
            echo "  --output-dir DIR   Output directory"
            echo "  --web-ui           Run with web UI"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Print configuration
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}NeuroStack Load Test${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""
echo -e "Scenario:    ${GREEN}$SCENARIO${NC}"
echo -e "Host:        ${GREEN}$HOST${NC}"
echo -e "Users:       ${GREEN}$USERS${NC}"
echo -e "Spawn Rate:  ${GREEN}$SPAWN_RATE/s${NC}"
echo -e "Duration:    ${GREEN}${DURATION}s${NC}"
echo -e "Output:      ${GREEN}$OUTPUT_DIR${NC}"
echo ""

# Check if Locust is installed
if ! command -v locust &> /dev/null; then
    echo -e "${RED}Locust is not installed. Installing...${NC}"
    pip install locust
fi

# Check if API is responding
echo -e "${YELLOW}Checking API availability...${NC}"
if curl -s -o /dev/null -w "%{http_code}" "$HOST/live" | grep -q "200"; then
    echo -e "${GREEN}✓ API is available${NC}"
else
    echo -e "${RED}✗ API is not responding at $HOST${NC}"
    echo "Please ensure the API is running before starting load tests."
    exit 1
fi

# Generate timestamp for file names
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HTML_REPORT="$OUTPUT_DIR/report_${SCENARIO}_${TIMESTAMP}.html"
CSV_PREFIX="$OUTPUT_DIR/stats_${SCENARIO}_${TIMESTAMP}"

# Build Locust command
LOCUST_CMD="locust -f $SCRIPT_DIR/locustfile.py --host=$HOST"

if [ "$WEB_UI" = true ]; then
    echo -e "${YELLOW}Starting Locust with web UI...${NC}"
    echo "Open http://localhost:8089 in your browser"
    $LOCUST_CMD
else
    echo -e "${YELLOW}Starting headless load test...${NC}"
    echo ""
    
    $LOCUST_CMD \
        --headless \
        --users=$USERS \
        --spawn-rate=$SPAWN_RATE \
        --run-time=${DURATION}s \
        --html=$HTML_REPORT \
        --csv=$CSV_PREFIX
    
    EXIT_CODE=$?
    
    echo ""
    echo -e "${BLUE}============================================${NC}"
    echo -e "${BLUE}Test Complete${NC}"
    echo -e "${BLUE}============================================${NC}"
    echo ""
    echo -e "HTML Report: ${GREEN}$HTML_REPORT${NC}"
    echo -e "CSV Stats:   ${GREEN}${CSV_PREFIX}_stats.csv${NC}"
    echo ""
    
    # Quick results summary from CSV
    if [ -f "${CSV_PREFIX}_stats.csv" ]; then
        echo -e "${YELLOW}Quick Summary:${NC}"
        # Extract aggregated row
        AGGREGATED=$(grep "Aggregated" "${CSV_PREFIX}_stats.csv" || echo "")
        if [ -n "$AGGREGATED" ]; then
            # Parse CSV fields (simplified)
            echo "$AGGREGATED" | awk -F',' '{
                printf "  Total Requests: %s\n", $3;
                printf "  Failed: %s\n", $4;
                printf "  Avg Response Time: %s ms\n", $6;
                printf "  p95: %s ms\n", $14;
                printf "  Requests/s: %s\n", $10;
            }'
            
            # Check p95 threshold
            P95=$(echo "$AGGREGATED" | awk -F',' '{print $14}')
            if (( $(echo "$P95 < 200" | bc -l) )); then
                echo -e "  ${GREEN}✓ p95 latency < 200ms${NC}"
            else
                echo -e "  ${RED}✗ p95 latency >= 200ms${NC}"
            fi
        fi
    fi
    
    exit $EXIT_CODE
fi

