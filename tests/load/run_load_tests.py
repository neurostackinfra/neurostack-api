#!/usr/bin/env python3
"""
NeuroStack API - Load Test Runner

Script to run load tests with predefined scenarios and generate reports.

Usage:
    python tests/load/run_load_tests.py --scenario smoke
    python tests/load/run_load_tests.py --scenario load --host http://localhost:8000
    python tests/load/run_load_tests.py --scenario benchmark --users 200

Scenarios:
    smoke     - Quick validation (10 users, 1 min)
    load      - Standard load test (100 users, 5 min)
    stress    - Push limits (500 users, 10 min)
    spike     - Sudden traffic spike
    endurance - Long-running test (30 min)
    benchmark - Target 1000 req/s
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.load.config import (
    DEFAULT_THRESHOLDS,
    LoadTestConfig,
    LoadTestProfile,
    SCENARIOS,
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run NeuroStack load tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    
    parser.add_argument(
        "--scenario",
        type=str,
        choices=list(SCENARIOS.keys()),
        default="load",
        help="Test scenario to run (default: load)",
    )
    
    parser.add_argument(
        "--host",
        type=str,
        default="http://localhost:8000",
        help="Target host URL (default: http://localhost:8000)",
    )
    
    parser.add_argument(
        "--users",
        type=int,
        help="Number of concurrent users (overrides scenario default)",
    )
    
    parser.add_argument(
        "--spawn-rate",
        type=int,
        help="User spawn rate per second (overrides scenario default)",
    )
    
    parser.add_argument(
        "--duration",
        type=int,
        help="Test duration in seconds (overrides scenario default)",
    )
    
    parser.add_argument(
        "--output-dir",
        type=str,
        default="load_test_results",
        help="Directory for test results (default: load_test_results)",
    )
    
    parser.add_argument(
        "--tags",
        type=str,
        nargs="+",
        help="Run only tests with specific tags (e.g., --tags events critical)",
    )
    
    parser.add_argument(
        "--exclude-tags",
        type=str,
        nargs="+",
        help="Exclude tests with specific tags",
    )
    
    parser.add_argument(
        "--web-ui",
        action="store_true",
        help="Run with Locust web UI instead of headless",
    )
    
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Number of worker processes (0 = single process)",
    )
    
    return parser.parse_args()


def ensure_output_dir(output_dir: str) -> Path:
    """Create output directory if it doesn't exist."""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_locust_command(
    config: LoadTestConfig,
    output_dir: Path,
    args: argparse.Namespace,
) -> list[str]:
    """Build the Locust command with all arguments."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    cmd = [
        "locust",
        "-f", str(Path(__file__).parent / "locustfile.py"),
        f"--host={config.host}",
    ]
    
    # Headless or web UI
    if not args.web_ui:
        cmd.append("--headless")
        cmd.extend([
            f"--users={config.users}",
            f"--spawn-rate={config.spawn_rate}",
            f"--run-time={config.run_time_seconds}s",
        ])
    
    # Output files
    html_report = output_dir / f"report_{args.scenario}_{timestamp}.html"
    csv_prefix = output_dir / f"stats_{args.scenario}_{timestamp}"
    
    cmd.extend([
        f"--html={html_report}",
        f"--csv={csv_prefix}",
    ])
    
    # Tags filtering
    if args.tags:
        cmd.extend(["--tags", ",".join(args.tags)])
    
    if args.exclude_tags:
        cmd.extend(["--exclude-tags", ",".join(args.exclude_tags)])
    
    # Distributed mode
    if args.workers > 0:
        cmd.append("--master")
    
    return cmd


def run_worker_processes(num_workers: int, host: str) -> list[subprocess.Popen]:
    """Start worker processes for distributed testing."""
    workers = []
    
    for _ in range(num_workers):
        worker_cmd = [
            "locust",
            "-f", str(Path(__file__).parent / "locustfile.py"),
            "--worker",
            f"--master-host={host.replace('http://', '').split(':')[0]}",
        ]
        
        proc = subprocess.Popen(
            worker_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        workers.append(proc)
    
    return workers


def analyze_results(csv_prefix: Path) -> dict:
    """Analyze CSV results and check against thresholds."""
    stats_file = Path(f"{csv_prefix}_stats.csv")
    
    if not stats_file.exists():
        return {"error": "Stats file not found"}
    
    import csv
    
    results = {
        "passed": True,
        "checks": [],
        "summary": {},
    }
    
    with open(stats_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("Name") == "Aggregated":
                # Extract metrics
                avg_time = float(row.get("Average Response Time", 0))
                p95_time = float(row.get("95%", 0))
                p99_time = float(row.get("99%", 0))
                fail_count = int(row.get("Failure Count", 0))
                req_count = int(row.get("Request Count", 0))
                rps = float(row.get("Requests/s", 0))
                
                error_rate = (fail_count / req_count * 100) if req_count > 0 else 0
                
                results["summary"] = {
                    "total_requests": req_count,
                    "failed_requests": fail_count,
                    "error_rate_percent": round(error_rate, 3),
                    "avg_response_time_ms": round(avg_time, 2),
                    "p95_response_time_ms": round(p95_time, 2),
                    "p99_response_time_ms": round(p99_time, 2),
                    "requests_per_second": round(rps, 2),
                }
                
                # Check thresholds
                thresholds = DEFAULT_THRESHOLDS
                
                # p95 latency check
                p95_passed = thresholds.check_latency(p95_time)
                results["checks"].append({
                    "name": "p95_latency",
                    "passed": p95_passed,
                    "actual": p95_time,
                    "threshold": thresholds.p95_response_time_ms,
                    "unit": "ms",
                })
                if not p95_passed:
                    results["passed"] = False
                
                # Error rate check
                error_passed = thresholds.check_error_rate(error_rate)
                results["checks"].append({
                    "name": "error_rate",
                    "passed": error_passed,
                    "actual": error_rate,
                    "threshold": thresholds.max_error_rate,
                    "unit": "%",
                })
                if not error_passed:
                    results["passed"] = False
                
                # Throughput check
                rps_passed = thresholds.check_throughput(rps)
                results["checks"].append({
                    "name": "throughput",
                    "passed": rps_passed,
                    "actual": rps,
                    "threshold": thresholds.min_requests_per_second,
                    "unit": "req/s",
                })
                
                break
    
    return results


def print_results(results: dict) -> None:
    """Print results summary to console."""
    print("\n" + "=" * 70)
    print("LOAD TEST RESULTS")
    print("=" * 70)
    
    if "error" in results:
        print(f"❌ Error: {results['error']}")
        return
    
    summary = results.get("summary", {})
    print(f"\nTotal Requests:     {summary.get('total_requests', 'N/A')}")
    print(f"Failed Requests:    {summary.get('failed_requests', 'N/A')}")
    print(f"Error Rate:         {summary.get('error_rate_percent', 'N/A')}%")
    print(f"Avg Response Time:  {summary.get('avg_response_time_ms', 'N/A')}ms")
    print(f"p95 Response Time:  {summary.get('p95_response_time_ms', 'N/A')}ms")
    print(f"p99 Response Time:  {summary.get('p99_response_time_ms', 'N/A')}ms")
    print(f"Requests/s:         {summary.get('requests_per_second', 'N/A')}")
    
    print("\n" + "-" * 70)
    print("THRESHOLD CHECKS")
    print("-" * 70)
    
    for check in results.get("checks", []):
        status = "✅" if check["passed"] else "❌"
        print(f"{status} {check['name']}: {check['actual']}{check['unit']} "
              f"(threshold: {check['threshold']}{check['unit']})")
    
    print("\n" + "=" * 70)
    overall = "✅ ALL CHECKS PASSED" if results.get("passed") else "❌ SOME CHECKS FAILED"
    print(overall)
    print("=" * 70 + "\n")


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Get scenario configuration
    scenario_config = SCENARIOS.get(args.scenario)
    if not scenario_config:
        print(f"Unknown scenario: {args.scenario}")
        return 1
    
    # Override with command-line arguments
    if args.host:
        scenario_config.host = args.host
    if args.users:
        scenario_config.users = args.users
    if args.spawn_rate:
        scenario_config.spawn_rate = args.spawn_rate
    if args.duration:
        scenario_config.run_time_seconds = args.duration
    
    # Setup output directory
    output_dir = ensure_output_dir(args.output_dir)
    
    print(f"Starting load test: {args.scenario}")
    print(f"  Host:       {scenario_config.host}")
    print(f"  Users:      {scenario_config.users}")
    print(f"  Spawn Rate: {scenario_config.spawn_rate}/s")
    print(f"  Duration:   {scenario_config.run_time_seconds}s")
    print(f"  Output:     {output_dir}")
    print()
    
    # Build command
    cmd = build_locust_command(scenario_config, output_dir, args)
    print(f"Command: {' '.join(cmd)}\n")
    
    # Start workers if distributed
    workers = []
    if args.workers > 0:
        print(f"Starting {args.workers} worker processes...")
        workers = run_worker_processes(args.workers, scenario_config.host)
    
    try:
        # Run Locust
        result = subprocess.run(cmd, check=False)
        
        # Analyze results if headless
        if not args.web_ui:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Find the most recent CSV file
            csv_files = list(output_dir.glob(f"stats_{args.scenario}_*_stats.csv"))
            if csv_files:
                csv_prefix = str(csv_files[-1]).replace("_stats.csv", "")
                results = analyze_results(Path(csv_prefix))
                print_results(results)
                
                # Save JSON results
                json_file = output_dir / f"results_{args.scenario}_{timestamp}.json"
                with open(json_file, "w") as f:
                    json.dump(results, f, indent=2)
                print(f"Results saved to: {json_file}")
                
                return 0 if results.get("passed", False) else 1
        
        return result.returncode
    
    finally:
        # Cleanup workers
        for worker in workers:
            worker.terminate()
            worker.wait()


if __name__ == "__main__":
    sys.exit(main())

