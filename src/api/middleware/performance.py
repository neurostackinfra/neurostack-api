"""
NeuroStack API - Performance Middleware

Middleware components for optimizing API performance:
- Response caching
- Connection keep-alive
- Performance metrics collection

These optimizations help achieve the target of:
- p95 latency < 200ms
- 1000 req/s throughput
"""

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Callable

from fastapi import Request, Response
from prometheus_client import Counter, Histogram, Gauge
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.config.logging import get_logger


logger = get_logger(__name__)


# ============================================
# Prometheus Metrics for Performance Monitoring
# ============================================

REQUEST_LATENCY = Histogram(
    "neurostack_request_latency_seconds",
    "Request latency in seconds",
    ["method", "endpoint", "status"],
    buckets=[0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 5.0],
)

REQUEST_COUNT = Counter(
    "neurostack_requests_total",
    "Total number of requests",
    ["method", "endpoint", "status"],
)

ACTIVE_REQUESTS = Gauge(
    "neurostack_active_requests",
    "Number of active requests",
    ["method"],
)

CACHE_HITS = Counter(
    "neurostack_cache_hits_total",
    "Total cache hits",
    ["cache_type"],
)

CACHE_MISSES = Counter(
    "neurostack_cache_misses_total",
    "Total cache misses",
    ["cache_type"],
)


# ============================================
# In-Memory LRU Cache
# ============================================

@dataclass
class CacheEntry:
    """Cache entry with expiration."""
    value: bytes
    content_type: str
    status_code: int
    expires_at: float
    created_at: float = field(default_factory=time.time)


class LRUCache:
    """
    Thread-safe LRU cache for response caching.
    
    Used for caching idempotent GET responses to reduce
    database/computation load.
    """
    
    def __init__(self, max_size: int = 1000, default_ttl: float = 60.0):
        """
        Initialize LRU cache.
        
        Args:
            max_size: Maximum number of entries
            default_ttl: Default time-to-live in seconds
        """
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = Lock()
    
    def get(self, key: str) -> CacheEntry | None:
        """
        Get value from cache.
        
        Returns None if key doesn't exist or is expired.
        """
        with self._lock:
            if key not in self._cache:
                return None
            
            entry = self._cache[key]
            
            # Check expiration
            if time.time() > entry.expires_at:
                del self._cache[key]
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            return entry
    
    def set(
        self,
        key: str,
        value: bytes,
        content_type: str = "application/json",
        status_code: int = 200,
        ttl: float | None = None,
    ) -> None:
        """
        Set value in cache.
        
        Args:
            key: Cache key
            value: Response body bytes
            content_type: Response content type
            status_code: HTTP status code
            ttl: Time-to-live in seconds (uses default if None)
        """
        ttl = ttl if ttl is not None else self.default_ttl
        
        with self._lock:
            # Remove oldest entries if at capacity
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            
            self._cache[key] = CacheEntry(
                value=value,
                content_type=content_type,
                status_code=status_code,
                expires_at=time.time() + ttl,
            )
    
    def invalidate(self, pattern: str | None = None) -> int:
        """
        Invalidate cache entries.
        
        Args:
            pattern: Key pattern to match (None clears all)
            
        Returns:
            Number of entries invalidated
        """
        with self._lock:
            if pattern is None:
                count = len(self._cache)
                self._cache.clear()
                return count
            
            # Invalidate matching keys
            keys_to_remove = [
                k for k in self._cache.keys() 
                if pattern in k
            ]
            for key in keys_to_remove:
                del self._cache[key]
            
            return len(keys_to_remove)
    
    @property
    def size(self) -> int:
        """Current cache size."""
        return len(self._cache)


# Global cache instance
response_cache = LRUCache(max_size=1000, default_ttl=60.0)


# ============================================
# Performance Monitoring Middleware
# ============================================

class PerformanceMiddleware(BaseHTTPMiddleware):
    """
    Middleware for collecting performance metrics.
    
    Tracks:
    - Request latency (histogram)
    - Request count by status
    - Active requests gauge
    """
    
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process request and collect metrics."""
        method = request.method
        path = self._normalize_path(request.url.path)
        
        # Track active requests
        ACTIVE_REQUESTS.labels(method=method).inc()
        
        start_time = time.perf_counter()
        
        try:
            response = await call_next(request)
            
            # Record metrics
            latency = time.perf_counter() - start_time
            status = str(response.status_code)
            
            REQUEST_LATENCY.labels(
                method=method,
                endpoint=path,
                status=status,
            ).observe(latency)
            
            REQUEST_COUNT.labels(
                method=method,
                endpoint=path,
                status=status,
            ).inc()
            
            # Add timing header for debugging
            response.headers["X-Response-Time"] = f"{latency * 1000:.2f}ms"
            
            return response
            
        finally:
            ACTIVE_REQUESTS.labels(method=method).dec()
    
    def _normalize_path(self, path: str) -> str:
        """
        Normalize path for metrics labels.
        
        Replaces dynamic segments (UUIDs, IDs) with placeholders
        to prevent label explosion.
        """
        import re
        
        # Replace UUIDs
        path = re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "{id}",
            path,
            flags=re.IGNORECASE,
        )
        
        # Replace numeric IDs
        path = re.sub(r"/\d+(?=/|$)", "/{id}", path)
        
        return path


# ============================================
# Response Caching Middleware
# ============================================

class CachingMiddleware(BaseHTTPMiddleware):
    """
    Middleware for caching GET responses.
    
    Caches responses based on:
    - URL path
    - Query parameters
    - Tenant ID header
    
    Only caches successful GET requests.
    """
    
    # Paths that should be cached
    CACHEABLE_PATHS = {
        "/health": 10.0,      # 10 seconds
        "/ready": 5.0,        # 5 seconds
        "/live": 1.0,         # 1 second
        "/v1/policies": 60.0, # 1 minute
    }
    
    # Paths that should never be cached
    NEVER_CACHE = {
        "/v1/events",  # Always dynamic
        "/v1/sessions",  # State changes
        "/metrics",  # Real-time metrics
    }
    
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process request with caching."""
        # Only cache GET requests
        if request.method != "GET":
            return await call_next(request)
        
        path = request.url.path
        
        # Check if path should never be cached
        for never_cache_path in self.NEVER_CACHE:
            if path.startswith(never_cache_path):
                return await call_next(request)
        
        # Generate cache key
        cache_key = self._generate_cache_key(request)
        
        # Try to get from cache
        cached = response_cache.get(cache_key)
        if cached is not None:
            CACHE_HITS.labels(cache_type="response").inc()
            
            return Response(
                content=cached.value,
                status_code=cached.status_code,
                media_type=cached.content_type,
                headers={"X-Cache": "HIT"},
            )
        
        CACHE_MISSES.labels(cache_type="response").inc()
        
        # Get response from handler
        response = await call_next(request)
        
        # Only cache successful responses
        if response.status_code == 200:
            ttl = self._get_ttl_for_path(path)
            if ttl > 0:
                # Read response body
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk
                
                # Cache the response
                response_cache.set(
                    key=cache_key,
                    value=body,
                    content_type=response.media_type or "application/json",
                    status_code=response.status_code,
                    ttl=ttl,
                )
                
                # Return new response with body
                return Response(
                    content=body,
                    status_code=response.status_code,
                    media_type=response.media_type,
                    headers={**dict(response.headers), "X-Cache": "MISS"},
                )
        
        response.headers["X-Cache"] = "BYPASS"
        return response
    
    def _generate_cache_key(self, request: Request) -> str:
        """Generate cache key from request."""
        # Include path, query, and tenant
        parts = [
            request.url.path,
            str(sorted(request.query_params.items())),
            request.headers.get("X-Tenant-ID", ""),
        ]
        
        key_str = "|".join(parts)
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def _get_ttl_for_path(self, path: str) -> float:
        """Get TTL for a path."""
        for cache_path, ttl in self.CACHEABLE_PATHS.items():
            if path.startswith(cache_path):
                return ttl
        return 0.0  # Don't cache


# ============================================
# Connection Optimization
# ============================================

class ConnectionOptimizationMiddleware(BaseHTTPMiddleware):
    """
    Middleware for connection optimization.
    
    Adds headers for:
    - Keep-alive connections
    - Compression hints
    - Performance optimization
    """
    
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Process request with connection optimization."""
        response = await call_next(request)
        
        # Keep-alive header
        response.headers["Connection"] = "keep-alive"
        response.headers["Keep-Alive"] = "timeout=30, max=1000"
        
        # Performance hints
        response.headers["X-Content-Type-Options"] = "nosniff"
        
        return response


# ============================================
# Performance Statistics
# ============================================

@dataclass
class PerformanceStats:
    """Performance statistics collector."""
    
    requests_total: int = 0
    errors_total: int = 0
    latency_sum: float = 0.0
    latency_count: int = 0
    min_latency: float = float("inf")
    max_latency: float = 0.0
    last_reset: datetime = field(default_factory=datetime.utcnow)
    
    _lock: Lock = field(default_factory=Lock, repr=False)
    
    def record(self, latency_ms: float, success: bool = True) -> None:
        """Record a request."""
        with self._lock:
            self.requests_total += 1
            if not success:
                self.errors_total += 1
            
            self.latency_sum += latency_ms
            self.latency_count += 1
            self.min_latency = min(self.min_latency, latency_ms)
            self.max_latency = max(self.max_latency, latency_ms)
    
    @property
    def avg_latency(self) -> float:
        """Average latency in ms."""
        if self.latency_count == 0:
            return 0.0
        return self.latency_sum / self.latency_count
    
    @property
    def error_rate(self) -> float:
        """Error rate percentage."""
        if self.requests_total == 0:
            return 0.0
        return (self.errors_total / self.requests_total) * 100
    
    def reset(self) -> dict:
        """Reset stats and return current values."""
        with self._lock:
            stats = {
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "avg_latency_ms": self.avg_latency,
                "min_latency_ms": self.min_latency if self.min_latency != float("inf") else 0,
                "max_latency_ms": self.max_latency,
                "error_rate_percent": self.error_rate,
                "period_seconds": (datetime.utcnow() - self.last_reset).total_seconds(),
            }
            
            # Reset
            self.requests_total = 0
            self.errors_total = 0
            self.latency_sum = 0.0
            self.latency_count = 0
            self.min_latency = float("inf")
            self.max_latency = 0.0
            self.last_reset = datetime.utcnow()
            
            return stats


# Global stats instance
performance_stats = PerformanceStats()

