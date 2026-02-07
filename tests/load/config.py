"""
NeuroStack API - Load Test Configuration

Centralized configuration for load testing scenarios and thresholds.
"""

from dataclasses import dataclass, field
from enum import Enum


class LoadTestProfile(Enum):
    """Predefined load test profiles."""
    
    SMOKE = "smoke"  # Quick validation (10 users, 1 min)
    LOAD = "load"  # Standard load test (100 users, 5 min)
    STRESS = "stress"  # Push limits (500 users, 10 min)
    SPIKE = "spike"  # Sudden traffic spike (0->200 users instantly)
    ENDURANCE = "endurance"  # Long-running test (100 users, 30 min)
    BENCHMARK = "benchmark"  # Target 1000 req/s


@dataclass
class PerformanceThresholds:
    """Performance thresholds and SLOs."""
    
    # Latency thresholds (milliseconds)
    avg_response_time_ms: float = 100.0
    p95_response_time_ms: float = 200.0
    p99_response_time_ms: float = 500.0
    
    # Error thresholds (percentage)
    max_error_rate: float = 0.1  # 0.1%
    
    # Throughput thresholds
    min_requests_per_second: float = 100.0
    target_requests_per_second: float = 1000.0
    
    def check_latency(self, p95_ms: float) -> bool:
        """Check if latency meets threshold."""
        return p95_ms <= self.p95_response_time_ms
    
    def check_error_rate(self, error_percentage: float) -> bool:
        """Check if error rate meets threshold."""
        return error_percentage <= self.max_error_rate
    
    def check_throughput(self, rps: float) -> bool:
        """Check if throughput meets minimum."""
        return rps >= self.min_requests_per_second


@dataclass
class LoadTestConfig:
    """Load test configuration."""
    
    profile: LoadTestProfile = LoadTestProfile.LOAD
    
    # User configuration
    users: int = 100
    spawn_rate: int = 10
    
    # Duration
    run_time_seconds: int = 300  # 5 minutes
    
    # Host configuration
    host: str = "http://localhost:8000"
    
    # Thresholds
    thresholds: PerformanceThresholds = field(default_factory=PerformanceThresholds)
    
    # Output
    html_report: str = "load_test_report.html"
    csv_prefix: str = "load_test"
    
    @classmethod
    def from_profile(cls, profile: LoadTestProfile, host: str = "http://localhost:8000") -> "LoadTestConfig":
        """Create config from predefined profile."""
        configs = {
            LoadTestProfile.SMOKE: cls(
                profile=profile,
                users=10,
                spawn_rate=2,
                run_time_seconds=60,
                host=host,
            ),
            LoadTestProfile.LOAD: cls(
                profile=profile,
                users=100,
                spawn_rate=10,
                run_time_seconds=300,
                host=host,
            ),
            LoadTestProfile.STRESS: cls(
                profile=profile,
                users=500,
                spawn_rate=50,
                run_time_seconds=600,
                host=host,
            ),
            LoadTestProfile.SPIKE: cls(
                profile=profile,
                users=200,
                spawn_rate=200,  # All at once
                run_time_seconds=180,
                host=host,
            ),
            LoadTestProfile.ENDURANCE: cls(
                profile=profile,
                users=100,
                spawn_rate=10,
                run_time_seconds=1800,  # 30 minutes
                host=host,
            ),
            LoadTestProfile.BENCHMARK: cls(
                profile=profile,
                users=200,
                spawn_rate=20,
                run_time_seconds=600,
                host=host,
                thresholds=PerformanceThresholds(
                    target_requests_per_second=1000,
                    p95_response_time_ms=200,
                ),
            ),
        }
        return configs.get(profile, cls(host=host))
    
    def to_locust_args(self) -> list[str]:
        """Generate Locust command-line arguments."""
        return [
            f"--host={self.host}",
            "--headless",
            f"--users={self.users}",
            f"--spawn-rate={self.spawn_rate}",
            f"--run-time={self.run_time_seconds}s",
            f"--html={self.html_report}",
            f"--csv={self.csv_prefix}",
        ]


# Predefined test scenarios
SCENARIOS = {
    "smoke": LoadTestConfig.from_profile(LoadTestProfile.SMOKE),
    "load": LoadTestConfig.from_profile(LoadTestProfile.LOAD),
    "stress": LoadTestConfig.from_profile(LoadTestProfile.STRESS),
    "spike": LoadTestConfig.from_profile(LoadTestProfile.SPIKE),
    "endurance": LoadTestConfig.from_profile(LoadTestProfile.ENDURANCE),
    "benchmark": LoadTestConfig.from_profile(LoadTestProfile.BENCHMARK),
}


# Default thresholds matching PRD requirements
DEFAULT_THRESHOLDS = PerformanceThresholds(
    avg_response_time_ms=100.0,
    p95_response_time_ms=200.0,  # Target from task 14.6
    p99_response_time_ms=500.0,
    max_error_rate=0.1,
    min_requests_per_second=100.0,
    target_requests_per_second=1000.0,  # Target from task 14.3
)

