"""
NeuroStack API - Load Testing Module

This module contains load testing scenarios using Locust for performance
validation and benchmarking of the NeuroStack API.

Targets:
- 1000 requests per second
- p95 latency < 200ms
- Zero error rate under normal load

Usage:
    locust -f tests/load/locustfile.py --host=http://localhost:8000
    
Or use the run script:
    ./tests/load/run_load_tests.sh
"""

