"""
NeuroStack API - Bottleneck Analyzer

Analyzes load test results to identify performance bottlenecks
and provide optimization recommendations.

Usage:
    python tests/load/bottleneck_analyzer.py --csv-dir load_test_results/
"""

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class EndpointMetrics:
    """Metrics for a single endpoint."""
    
    name: str
    method: str = ""
    requests: int = 0
    failures: int = 0
    avg_response_time: float = 0.0
    min_response_time: float = 0.0
    max_response_time: float = 0.0
    p50: float = 0.0
    p66: float = 0.0
    p75: float = 0.0
    p80: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    rps: float = 0.0
    
    @property
    def error_rate(self) -> float:
        """Calculate error rate percentage."""
        if self.requests == 0:
            return 0.0
        return (self.failures / self.requests) * 100
    
    @property
    def is_slow(self) -> bool:
        """Check if endpoint is slow (p95 > 200ms)."""
        return self.p95 > 200
    
    @property
    def has_high_variance(self) -> bool:
        """Check if endpoint has high latency variance."""
        if self.avg_response_time == 0:
            return False
        return (self.p99 / self.avg_response_time) > 3
    
    @property
    def has_errors(self) -> bool:
        """Check if endpoint has errors."""
        return self.error_rate > 0.1


@dataclass
class BottleneckAnalysis:
    """Analysis results with identified bottlenecks."""
    
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    total_requests: int = 0
    total_failures: int = 0
    overall_error_rate: float = 0.0
    overall_p95: float = 0.0
    overall_rps: float = 0.0
    
    # Identified issues
    slow_endpoints: list[dict] = field(default_factory=list)
    high_variance_endpoints: list[dict] = field(default_factory=list)
    error_prone_endpoints: list[dict] = field(default_factory=list)
    
    # Recommendations
    recommendations: list[str] = field(default_factory=list)
    
    # Status
    meets_latency_target: bool = False
    meets_error_target: bool = False
    meets_throughput_target: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "summary": {
                "total_requests": self.total_requests,
                "total_failures": self.total_failures,
                "overall_error_rate": self.overall_error_rate,
                "overall_p95_ms": self.overall_p95,
                "overall_rps": self.overall_rps,
            },
            "targets": {
                "latency_p95_met": self.meets_latency_target,
                "error_rate_met": self.meets_error_target,
                "throughput_met": self.meets_throughput_target,
            },
            "bottlenecks": {
                "slow_endpoints": self.slow_endpoints,
                "high_variance_endpoints": self.high_variance_endpoints,
                "error_prone_endpoints": self.error_prone_endpoints,
            },
            "recommendations": self.recommendations,
        }


class BottleneckAnalyzer:
    """Analyzes load test results to identify bottlenecks."""
    
    # Target thresholds
    TARGET_P95_MS = 200.0
    TARGET_ERROR_RATE = 0.1  # 0.1%
    TARGET_RPS = 1000.0
    
    def __init__(self, csv_dir: Path):
        """Initialize analyzer with results directory."""
        self.csv_dir = csv_dir
        self.endpoints: list[EndpointMetrics] = []
        self.analysis = BottleneckAnalysis()
    
    def load_stats(self, stats_file: Path) -> None:
        """Load statistics from CSV file."""
        with open(stats_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("Name", "")
                if not name or name == "Aggregated":
                    if name == "Aggregated":
                        self._process_aggregated(row)
                    continue
                
                endpoint = EndpointMetrics(
                    name=name,
                    method=row.get("Type", ""),
                    requests=int(row.get("Request Count", 0)),
                    failures=int(row.get("Failure Count", 0)),
                    avg_response_time=float(row.get("Average Response Time", 0)),
                    min_response_time=float(row.get("Min Response Time", 0)),
                    max_response_time=float(row.get("Max Response Time", 0)),
                    p50=float(row.get("50%", 0)),
                    p66=float(row.get("66%", 0)),
                    p75=float(row.get("75%", 0)),
                    p80=float(row.get("80%", 0)),
                    p90=float(row.get("90%", 0)),
                    p95=float(row.get("95%", 0)),
                    p99=float(row.get("99%", 0)),
                    rps=float(row.get("Requests/s", 0)),
                )
                self.endpoints.append(endpoint)
    
    def _process_aggregated(self, row: dict) -> None:
        """Process aggregated statistics."""
        self.analysis.total_requests = int(row.get("Request Count", 0))
        self.analysis.total_failures = int(row.get("Failure Count", 0))
        self.analysis.overall_p95 = float(row.get("95%", 0))
        self.analysis.overall_rps = float(row.get("Requests/s", 0))
        
        if self.analysis.total_requests > 0:
            self.analysis.overall_error_rate = (
                self.analysis.total_failures / self.analysis.total_requests * 100
            )
    
    def analyze(self) -> BottleneckAnalysis:
        """Run full bottleneck analysis."""
        # Find slow endpoints
        for endpoint in self.endpoints:
            if endpoint.is_slow:
                self.analysis.slow_endpoints.append({
                    "name": endpoint.name,
                    "method": endpoint.method,
                    "p95_ms": endpoint.p95,
                    "avg_ms": endpoint.avg_response_time,
                    "requests": endpoint.requests,
                })
            
            if endpoint.has_high_variance:
                self.analysis.high_variance_endpoints.append({
                    "name": endpoint.name,
                    "method": endpoint.method,
                    "avg_ms": endpoint.avg_response_time,
                    "p99_ms": endpoint.p99,
                    "variance_ratio": round(endpoint.p99 / endpoint.avg_response_time, 2),
                })
            
            if endpoint.has_errors:
                self.analysis.error_prone_endpoints.append({
                    "name": endpoint.name,
                    "method": endpoint.method,
                    "error_rate": round(endpoint.error_rate, 3),
                    "failures": endpoint.failures,
                    "requests": endpoint.requests,
                })
        
        # Check targets
        self.analysis.meets_latency_target = (
            self.analysis.overall_p95 <= self.TARGET_P95_MS
        )
        self.analysis.meets_error_target = (
            self.analysis.overall_error_rate <= self.TARGET_ERROR_RATE
        )
        self.analysis.meets_throughput_target = (
            self.analysis.overall_rps >= self.TARGET_RPS
        )
        
        # Generate recommendations
        self._generate_recommendations()
        
        return self.analysis
    
    def _generate_recommendations(self) -> None:
        """Generate optimization recommendations based on analysis."""
        recommendations = []
        
        # Latency recommendations
        if not self.analysis.meets_latency_target:
            recommendations.append(
                f"⚠️ LATÊNCIA: p95 ({self.analysis.overall_p95:.0f}ms) excede target "
                f"({self.TARGET_P95_MS}ms). Considere:"
            )
            
            if self.analysis.slow_endpoints:
                slowest = max(self.analysis.slow_endpoints, key=lambda x: x["p95_ms"])
                recommendations.append(
                    f"  - Otimizar endpoint mais lento: {slowest['name']} "
                    f"(p95: {slowest['p95_ms']:.0f}ms)"
                )
            
            recommendations.extend([
                "  - Adicionar cache (Redis) para dados frequentemente acessados",
                "  - Otimizar queries de banco de dados",
                "  - Considerar connection pooling otimizado",
                "  - Profile código para identificar hotspots",
            ])
        
        # Error rate recommendations
        if not self.analysis.meets_error_target:
            recommendations.append(
                f"⚠️ ERROS: Taxa de erro ({self.analysis.overall_error_rate:.2f}%) "
                f"excede target ({self.TARGET_ERROR_RATE}%). Considere:"
            )
            
            if self.analysis.error_prone_endpoints:
                most_errors = max(
                    self.analysis.error_prone_endpoints, 
                    key=lambda x: x["error_rate"]
                )
                recommendations.append(
                    f"  - Investigar endpoint problemático: {most_errors['name']} "
                    f"(error rate: {most_errors['error_rate']:.2f}%)"
                )
            
            recommendations.extend([
                "  - Revisar logs de erro para identificar causa raiz",
                "  - Aumentar timeouts se necessário",
                "  - Verificar limites de rate limiting",
                "  - Adicionar retry logic para operações transientes",
            ])
        
        # Throughput recommendations
        if not self.analysis.meets_throughput_target:
            recommendations.append(
                f"⚠️ THROUGHPUT: RPS ({self.analysis.overall_rps:.0f}) abaixo do "
                f"target ({self.TARGET_RPS}). Considere:"
            )
            recommendations.extend([
                "  - Aumentar número de workers Uvicorn",
                "  - Escalar horizontalmente (mais instâncias)",
                "  - Otimizar operações I/O-bound com async",
                "  - Verificar saturação de CPU/memória",
            ])
        
        # High variance recommendations
        if self.analysis.high_variance_endpoints:
            recommendations.append(
                "⚠️ VARIÂNCIA: Alguns endpoints têm latência inconsistente:"
            )
            for ep in self.analysis.high_variance_endpoints[:3]:
                recommendations.append(
                    f"  - {ep['name']}: avg={ep['avg_ms']:.0f}ms, "
                    f"p99={ep['p99_ms']:.0f}ms (ratio: {ep['variance_ratio']}x)"
                )
            recommendations.extend([
                "  - Investigar garbage collection",
                "  - Verificar contenção de recursos",
                "  - Considerar circuit breakers",
            ])
        
        # All targets met
        if (self.analysis.meets_latency_target and 
            self.analysis.meets_error_target and
            self.analysis.meets_throughput_target):
            recommendations.append(
                "✅ Todos os targets de performance foram atingidos!"
            )
            recommendations.append(
                "Sugestões para manter a performance:"
            )
            recommendations.extend([
                "  - Implementar monitoramento contínuo de performance",
                "  - Configurar alertas para degradação de latência",
                "  - Executar testes de carga regularmente",
                "  - Documentar baseline de performance",
            ])
        
        self.analysis.recommendations = recommendations
    
    def find_latest_stats(self) -> Path | None:
        """Find the most recent stats CSV file."""
        csv_files = list(self.csv_dir.glob("*_stats.csv"))
        if not csv_files:
            return None
        return max(csv_files, key=lambda x: x.stat().st_mtime)
    
    def print_report(self) -> None:
        """Print analysis report to console."""
        print("\n" + "=" * 70)
        print("ANÁLISE DE BOTTLENECKS - NeuroStack API")
        print("=" * 70)
        
        # Summary
        print("\n📊 RESUMO GERAL")
        print("-" * 40)
        print(f"Total de Requests:  {self.analysis.total_requests:,}")
        print(f"Total de Falhas:    {self.analysis.total_failures:,}")
        print(f"Taxa de Erro:       {self.analysis.overall_error_rate:.3f}%")
        print(f"p95 Latência:       {self.analysis.overall_p95:.0f}ms")
        print(f"Throughput:         {self.analysis.overall_rps:.0f} req/s")
        
        # Targets
        print("\n🎯 TARGETS")
        print("-" * 40)
        
        status = "✅" if self.analysis.meets_latency_target else "❌"
        print(f"{status} Latência p95 < {self.TARGET_P95_MS}ms")
        
        status = "✅" if self.analysis.meets_error_target else "❌"
        print(f"{status} Taxa de Erro < {self.TARGET_ERROR_RATE}%")
        
        status = "✅" if self.analysis.meets_throughput_target else "❌"
        print(f"{status} Throughput >= {self.TARGET_RPS} req/s")
        
        # Bottlenecks
        if self.analysis.slow_endpoints:
            print("\n🐢 ENDPOINTS LENTOS (p95 > 200ms)")
            print("-" * 40)
            for ep in sorted(self.analysis.slow_endpoints, 
                           key=lambda x: x["p95_ms"], reverse=True)[:5]:
                print(f"  {ep['method']} {ep['name']}")
                print(f"      p95: {ep['p95_ms']:.0f}ms | avg: {ep['avg_ms']:.0f}ms")
        
        if self.analysis.error_prone_endpoints:
            print("\n❌ ENDPOINTS COM ERROS")
            print("-" * 40)
            for ep in sorted(self.analysis.error_prone_endpoints,
                           key=lambda x: x["error_rate"], reverse=True)[:5]:
                print(f"  {ep['method']} {ep['name']}")
                print(f"      errors: {ep['failures']}/{ep['requests']} "
                      f"({ep['error_rate']:.2f}%)")
        
        # Recommendations
        print("\n💡 RECOMENDAÇÕES")
        print("-" * 40)
        for rec in self.analysis.recommendations:
            print(rec)
        
        print("\n" + "=" * 70 + "\n")


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze load test results for bottlenecks"
    )
    parser.add_argument(
        "--csv-dir",
        type=str,
        default="load_test_results",
        help="Directory containing CSV results",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output JSON file for analysis results",
    )
    parser.add_argument(
        "--stats-file",
        type=str,
        help="Specific stats CSV file to analyze",
    )
    
    args = parser.parse_args()
    
    csv_dir = Path(args.csv_dir)
    if not csv_dir.exists():
        print(f"Error: Directory not found: {csv_dir}")
        return 1
    
    analyzer = BottleneckAnalyzer(csv_dir)
    
    # Find stats file
    if args.stats_file:
        stats_file = Path(args.stats_file)
    else:
        stats_file = analyzer.find_latest_stats()
    
    if not stats_file or not stats_file.exists():
        print("Error: No stats file found. Run load tests first.")
        return 1
    
    print(f"Analyzing: {stats_file}")
    
    # Load and analyze
    analyzer.load_stats(stats_file)
    analysis = analyzer.analyze()
    
    # Print report
    analyzer.print_report()
    
    # Save JSON output
    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w") as f:
            json.dump(analysis.to_dict(), f, indent=2)
        print(f"Analysis saved to: {output_path}")
    
    # Return exit code based on targets
    if (analysis.meets_latency_target and 
        analysis.meets_error_target and 
        analysis.meets_throughput_target):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

