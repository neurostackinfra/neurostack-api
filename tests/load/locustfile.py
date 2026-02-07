"""
NeuroStack API - Load Test Scenarios (Locust)

Comprehensive load testing scenarios for validating API performance.

Scenarios:
1. Event Processing - Main cognitive pipeline endpoint
2. Session Management - CRUD operations on sessions
3. Health Checks - Liveness and readiness probes
4. Mixed Workload - Realistic traffic patterns

Performance Targets:
- Throughput: 1000 req/s sustained
- Latency p95: < 200ms
- Latency p99: < 500ms
- Error rate: < 0.1%

Usage:
    # Basic run
    locust -f tests/load/locustfile.py --host=http://localhost:8000
    
    # Headless mode with specific parameters
    locust -f tests/load/locustfile.py \
        --host=http://localhost:8000 \
        --headless \
        --users 100 \
        --spawn-rate 10 \
        --run-time 5m \
        --html=load_test_report.html
"""

import os
import random
import uuid
from datetime import datetime
from typing import Any

from locust import HttpUser, TaskSet, between, events, tag, task
from locust.runners import MasterRunner


# ============================================
# Test Configuration
# ============================================

# API Configuration
API_PREFIX = "/v1"
API_KEY = os.environ.get("NEUROSTACK_API_KEY", "dev-test-key-12345678901234567890")

# Test Data
CHANNELS = ["whatsapp", "web", "telegram", "sms"]
INTENTS = [
    "Olá, gostaria de agendar uma consulta",
    "Qual o horário de funcionamento?",
    "Quero remarcar minha consulta",
    "Preciso falar com um atendente",
    "Quanto custa a consulta?",
    "Quais especialidades estão disponíveis?",
    "Como faço para cancelar?",
    "Vocês aceitam convênio?",
    "Onde fica a clínica?",
    "Preciso de um retorno médico",
]

SAFETY_MESSAGES = [
    "Estou com dor forte no peito",
    "Quero saber qual medicamento tomar",
    "Acho que estou tendo uma reação alérgica",
]


def get_headers() -> dict[str, str]:
    """Get standard headers for API requests."""
    return {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        "X-Request-ID": str(uuid.uuid4()),
    }


def generate_tenant_id() -> str:
    """Generate a random tenant ID for load distribution."""
    # Use a limited set to simulate realistic multi-tenant load
    tenants = [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
        "44444444-4444-4444-4444-444444444444",
        "55555555-5555-5555-5555-555555555555",
    ]
    return random.choice(tenants)


# ============================================
# Task Sets
# ============================================


class EventProcessingTasks(TaskSet):
    """
    Event processing load tests - the main cognitive pipeline.
    
    This is the critical path that must maintain < 200ms p95 latency.
    """

    def on_start(self) -> None:
        """Initialize session data for the user."""
        self.tenant_id = generate_tenant_id()
        self.session_id = str(uuid.uuid4())
        self.user_id = f"load-test-user-{uuid.uuid4().hex[:8]}"
        self.turn_count = 0

    @tag("events", "critical")
    @task(10)
    def process_normal_message(self) -> None:
        """Process a normal user message through the cognitive pipeline."""
        self.turn_count += 1
        
        payload = {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "event_type": "message.in",
            "payload": {
                "content": random.choice(INTENTS),
                "channel": random.choice(CHANNELS),
                "user_id": self.user_id,
            },
        }

        with self.client.post(
            f"{API_PREFIX}/events",
            json=payload,
            headers=get_headers(),
            name="POST /v1/events (normal)",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if "decision" in data and "prompt" in data:
                    response.success()
                else:
                    response.failure("Invalid response structure")
            elif response.status_code == 429:
                response.failure("Rate limited")
            else:
                response.failure(f"Error: {response.status_code}")

    @tag("events", "safety")
    @task(1)
    def process_safety_message(self) -> None:
        """Process a safety-concern message (should trigger safety mode)."""
        payload = {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "event_type": "message.in",
            "payload": {
                "content": random.choice(SAFETY_MESSAGES),
                "channel": random.choice(CHANNELS),
                "user_id": self.user_id,
            },
        }

        with self.client.post(
            f"{API_PREFIX}/events",
            json=payload,
            headers=get_headers(),
            name="POST /v1/events (safety)",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if data.get("mode") in ["safety", "handoff", "normal"]:
                    response.success()
                else:
                    response.failure("Invalid mode for safety message")
            else:
                response.failure(f"Error: {response.status_code}")

    @tag("events")
    @task(2)
    def start_new_session(self) -> None:
        """Start a new session (simulates new users arriving)."""
        self.session_id = str(uuid.uuid4())
        self.turn_count = 0
        
        payload = {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "event_type": "session.start",
            "payload": {
                "content": "",
                "channel": random.choice(CHANNELS),
                "user_id": self.user_id,
            },
        }

        with self.client.post(
            f"{API_PREFIX}/events",
            json=payload,
            headers=get_headers(),
            name="POST /v1/events (session.start)",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Error: {response.status_code}")


class SessionManagementTasks(TaskSet):
    """
    Session management load tests.
    
    Tests CRUD operations on sessions.
    """

    def on_start(self) -> None:
        """Initialize test data."""
        self.tenant_id = generate_tenant_id()
        self.created_sessions: list[str] = []

    @tag("sessions")
    @task(5)
    def create_session(self) -> None:
        """Create a new session."""
        payload = {
            "tenant_id": self.tenant_id,
            "user_id": f"user-{uuid.uuid4().hex[:8]}",
            "channel": random.choice(CHANNELS),
            "metadata": {"source": "load_test"},
        }

        with self.client.post(
            f"{API_PREFIX}/sessions",
            json=payload,
            headers=get_headers(),
            name="POST /v1/sessions",
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                data = response.json()
                if "id" in data:
                    self.created_sessions.append(data["id"])
                    # Limit stored sessions to avoid memory issues
                    if len(self.created_sessions) > 100:
                        self.created_sessions.pop(0)
                    response.success()
                else:
                    response.failure("Missing session ID")
            else:
                response.failure(f"Error: {response.status_code}")

    @tag("sessions")
    @task(3)
    def list_sessions(self) -> None:
        """List sessions for tenant."""
        with self.client.get(
            f"{API_PREFIX}/sessions",
            params={"tenant_id": self.tenant_id, "page": 1, "page_size": 20},
            headers=get_headers(),
            name="GET /v1/sessions",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Error: {response.status_code}")

    @tag("sessions")
    @task(2)
    def get_session(self) -> None:
        """Get a specific session (if available)."""
        if not self.created_sessions:
            return

        session_id = random.choice(self.created_sessions)
        
        with self.client.get(
            f"{API_PREFIX}/sessions/{session_id}",
            headers=get_headers(),
            name="GET /v1/sessions/{id}",
            catch_response=True,
        ) as response:
            # 404 is acceptable since sessions might expire
            if response.status_code in [200, 404]:
                response.success()
            else:
                response.failure(f"Error: {response.status_code}")


class HealthCheckTasks(TaskSet):
    """
    Health check endpoint tests.
    
    These should be extremely fast (< 10ms).
    """

    @tag("health", "critical")
    @task(5)
    def liveness_check(self) -> None:
        """Simple liveness probe."""
        with self.client.get(
            "/live",
            name="GET /live",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if data.get("alive"):
                    response.success()
                else:
                    response.failure("Not alive")
            else:
                response.failure(f"Error: {response.status_code}")

    @tag("health")
    @task(3)
    def readiness_check(self) -> None:
        """Readiness probe with dependency checks."""
        with self.client.get(
            "/ready",
            name="GET /ready",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Error: {response.status_code}")

    @tag("health")
    @task(2)
    def full_health_check(self) -> None:
        """Full health check with component status."""
        with self.client.get(
            "/health",
            name="GET /health",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if data.get("status") in ["healthy", "degraded"]:
                    response.success()
                else:
                    response.failure("Unhealthy status")
            else:
                response.failure(f"Error: {response.status_code}")


# ============================================
# User Classes
# ============================================


class EventProcessingUser(HttpUser):
    """
    User class focused on event processing (main workload).
    
    Simulates an AI agent processing user messages through
    the NeuroStack cognitive pipeline.
    """

    tasks = [EventProcessingTasks]
    wait_time = between(0.1, 0.5)  # Fast paced for load testing
    weight = 7  # 70% of traffic


class SessionManagementUser(HttpUser):
    """
    User class for session management operations.
    
    Simulates builders managing sessions.
    """

    tasks = [SessionManagementTasks]
    wait_time = between(0.5, 1.5)  # Less frequent operations
    weight = 2  # 20% of traffic


class HealthCheckUser(HttpUser):
    """
    User class for health check monitoring.
    
    Simulates load balancers and monitoring systems.
    """

    tasks = [HealthCheckTasks]
    wait_time = between(1.0, 3.0)  # Periodic checks
    weight = 1  # 10% of traffic


class MixedWorkloadUser(HttpUser):
    """
    User class that combines all workloads.
    
    More realistic simulation of actual traffic patterns.
    """

    wait_time = between(0.1, 1.0)

    def on_start(self) -> None:
        """Initialize user data."""
        self.tenant_id = generate_tenant_id()
        self.session_id = str(uuid.uuid4())
        self.user_id = f"mixed-user-{uuid.uuid4().hex[:8]}"

    @tag("events", "critical")
    @task(10)
    def process_event(self) -> None:
        """Process event - main task."""
        payload = {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "event_type": "message.in",
            "payload": {
                "content": random.choice(INTENTS),
                "channel": random.choice(CHANNELS),
                "user_id": self.user_id,
            },
        }

        self.client.post(
            f"{API_PREFIX}/events",
            json=payload,
            headers=get_headers(),
            name="POST /v1/events",
        )

    @tag("sessions")
    @task(2)
    def create_session(self) -> None:
        """Create new session."""
        # Start new session periodically
        self.session_id = str(uuid.uuid4())
        
        payload = {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "channel": random.choice(CHANNELS),
        }

        self.client.post(
            f"{API_PREFIX}/sessions",
            json=payload,
            headers=get_headers(),
            name="POST /v1/sessions",
        )

    @tag("health")
    @task(1)
    def health_check(self) -> None:
        """Quick health check."""
        self.client.get("/live", name="GET /live")


# ============================================
# Event Handlers
# ============================================


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Initialize on test start."""
    if isinstance(environment.runner, MasterRunner):
        print("Starting NeuroStack load test (master)")
    else:
        print("Starting NeuroStack load test")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Log test start."""
    print(f"Load test starting at {datetime.now().isoformat()}")
    print(f"Target host: {environment.host}")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Log test completion and summary."""
    print(f"\nLoad test completed at {datetime.now().isoformat()}")
    
    if environment.stats.total.num_requests > 0:
        stats = environment.stats.total
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Total Requests: {stats.num_requests}")
        print(f"Failed Requests: {stats.num_failures}")
        print(f"Failure Rate: {stats.fail_ratio * 100:.2f}%")
        print(f"Avg Response Time: {stats.avg_response_time:.2f}ms")
        print(f"Median Response Time: {stats.median_response_time:.2f}ms")
        print(f"p95 Response Time: {stats.get_response_time_percentile(0.95):.2f}ms")
        print(f"p99 Response Time: {stats.get_response_time_percentile(0.99):.2f}ms")
        print(f"Requests/s: {stats.total_rps:.2f}")
        print("=" * 60)
        
        # Check against targets
        p95 = stats.get_response_time_percentile(0.95)
        if p95 < 200:
            print("✅ p95 latency target MET (< 200ms)")
        else:
            print(f"❌ p95 latency target MISSED ({p95:.2f}ms > 200ms)")
        
        if stats.fail_ratio < 0.001:
            print("✅ Error rate target MET (< 0.1%)")
        else:
            print(f"❌ Error rate target MISSED ({stats.fail_ratio * 100:.2f}% > 0.1%)")

