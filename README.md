# Application Performance Metrics

Learn application performance monitoring using **OpenTelemetry** + **SigNoz** with a FastAPI app backed by MongoDB and Valkey cache.

## Architecture

```
┌──────────────┐     OTLP/gRPC      ┌──────────────────┐     ClickHouse     ┌─────────────┐
│  FastAPI App │ ──────────────────▶ │  OTel Collector  │ ─────────────────▶ │   SigNoz    │
│  (port 8000) │                    │  (port 4317)     │                    │  (port 8080)│
└──────┬───────┘                    └──────────────────┘                    └─────────────┘
       │
       ├──▶ Valkey Cache (port 6379)
       └──▶ MongoDB (port 27017)
```

## Quick Start

```bash
# Clone the repo
git clone <repo-url> && cd application-performance-metrics

# Clone SigNoz (one-time setup)
cd signoz && git clone --depth=1 https://github.com/SigNoz/signoz.git && cd ..

# Start everything
docker compose up --build -d

# Wait ~2 minutes for ClickHouse migrations to complete, then open:
# SigNoz UI → http://localhost:8080
# FastAPI   → http://localhost:8000
```

## OpenTelemetry Instrumentation

### How It Works

OpenTelemetry's data model has three pillars:

| Signal     | What It Captures                                             |
|------------|--------------------------------------------------------------|
| **Traces** | The journey of a request through your system (spans)         |
| **Metrics**| Quantitative measurements (counters, histograms, gauges)     |
| **Logs**   | Contextual event information, correlated to traces           |

### Dependencies

```toml
# pyproject.toml
dependencies = [
    "opentelemetry-distro",       # Auto-configuration & instrumentation
    "opentelemetry-exporter-otlp", # OTLP exporter (gRPC + HTTP)
    "redis",                       # Auto-instrumented by OTel
    "pymongo",                     # Auto-instrumented by OTel
]
```

### Dockerfile Setup

```dockerfile
# Install OTel instrumentations for detected libraries (FastAPI, redis, pymongo, etc.)
RUN uv run opentelemetry-bootstrap --action=install

# Run the app wrapped with opentelemetry-instrument (zero code changes needed for basic tracing)
CMD ["uv", "run", "opentelemetry-instrument", "fastapi", "run", "main.py", "--host", "0.0.0.0", "--port", "8000"]
```

`opentelemetry-bootstrap` scans installed packages and installs matching instrumentations (e.g., `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-redis`, `opentelemetry-instrumentation-pymongo`).

### Environment Variables

```yaml
# docker-compose.yml
environment:
  - OTEL_SERVICE_NAME=fastapi-app                      # Service name in SigNoz
  - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317  # OTel Collector gRPC
  - OTEL_EXPORTER_OTLP_PROTOCOL=grpc
  - OTEL_LOGS_EXPORTER=otlp                            # Export logs via OTLP
  - OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true
  - OTEL_PYTHON_LOG_CORRELATION=true                   # Add trace_id to logs
```

### Custom Instrumentation in Code

Auto-instrumentation gives you HTTP spans for free. For deeper visibility, add manual spans:

```python
from opentelemetry import trace, metrics

tracer = trace.get_tracer(__name__)
meter  = metrics.get_meter(__name__)

# Custom metrics
cache_hits = meter.create_counter("cache.hits", description="Cache hit count")
request_duration = meter.create_histogram("http.request.duration", unit="ms")

# Custom spans — these appear as children of the auto-instrumented HTTP span
with tracer.start_as_current_span("cache-lookup", attributes={"cache.key": key}) as span:
    result = cache.get(key)
    span.set_attribute("cache.hit", result is not None)

with tracer.start_as_current_span("db-query", attributes={"db.operation": "find_one"}):
    doc = collection.find_one({"id": item_id})
```

### What You See in SigNoz

A request to `GET /items/1` produces this trace waterfall:

```
GET /items/1                          ← auto-instrumented HTTP span
├── cache-lookup (cache.hit=false)    ← custom span + auto redis span
│   └── redis GET item:1             ← auto-instrumented by OTel
├── db-query (db.operation=find_one)  ← custom span + auto pymongo span
│   └── pymongo find items           ← auto-instrumented by OTel
└── cache-write (cache.ttl=60)        ← custom span
    └── redis SETEX item:1           ← auto-instrumented by OTel
```

On a second call, only the cache-lookup span appears (cache hit).

## API Endpoints

| Method   | Endpoint                           | Purpose                        |
|----------|------------------------------------|--------------------------------|
| `GET`    | `/`                                | Health check                   |
| `GET`    | `/items/{id}`                      | Fetch item (cache → DB)        |
| `GET`    | `/items?category=X&limit=N`        | List items with filter         |
| `POST`   | `/items?name=X&price=Y&category=Z` | Create item                    |
| `DELETE` | `/items/{id}`                      | Delete item + invalidate cache |
| `GET`    | `/slow`                            | Artificial latency (0.5-2s)    |
| `GET`    | `/error`                           | Simulated 500 error            |
| `GET`    | `/flush-cache`                     | Flush Valkey cache             |

## Reference

- [OpenTelemetry FastAPI Guide (SigNoz)](https://signoz.io/blog/opentelemetry-fastapi/)
- [OpenTelemetry Python Docs](https://opentelemetry.io/docs/languages/python/)
- [SigNoz Self-Hosted Docs](https://signoz.io/docs/install/docker/)