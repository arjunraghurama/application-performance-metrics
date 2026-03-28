import logging
import time
import random
import json
import os

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import redis
from pymongo import MongoClient

from opentelemetry import trace, metrics

# ── Tracer & Meter ──────────────────────────────────────────────
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# ── Custom Metrics ──────────────────────────────────────────────
request_counter = meter.create_counter(
    name="http.requests.total",
    description="Total number of HTTP requests",
    unit="1",
)

error_counter = meter.create_counter(
    name="http.errors.total",
    description="Total number of HTTP errors",
    unit="1",
)

request_duration = meter.create_histogram(
    name="http.request.duration",
    description="Duration of HTTP requests in milliseconds",
    unit="ms",
)

active_requests = meter.create_up_down_counter(
    name="http.requests.active",
    description="Number of active HTTP requests",
    unit="1",
)

cache_hit_counter = meter.create_counter(
    name="cache.hits",
    description="Number of cache hits",
    unit="1",
)

cache_miss_counter = meter.create_counter(
    name="cache.misses",
    description="Number of cache misses",
    unit="1",
)

# ── Logging ─────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ── External Services ───────────────────────────────────────────
CACHE_HOST = os.getenv("CACHE_HOST", "cache")
CACHE_PORT = int(os.getenv("CACHE_PORT", "6379"))
DB_HOST = os.getenv("DB_HOST", "db")
DB_PORT = int(os.getenv("DB_PORT", "27017"))
CACHE_TTL = 60  # seconds

cache = redis.Redis(host=CACHE_HOST, port=CACHE_PORT, decode_responses=True)
mongo = MongoClient(host=DB_HOST, port=DB_PORT)
db = mongo["app_metrics"]
items_collection = db["items"]


# ── Seed Data ───────────────────────────────────────────────────
SAMPLE_ITEMS = [
    {"item_id": i, "name": f"Item {i}", "price": round(random.uniform(5.0, 500.0), 2), "category": random.choice(["electronics", "books", "clothing", "food"])}
    for i in range(1, 101)
]


def seed_data():
    """Seed MongoDB with sample items if empty."""
    if items_collection.count_documents({}) == 0:
        items_collection.insert_many(SAMPLE_ITEMS)
        logger.info("Seeded MongoDB with %d items", len(SAMPLE_ITEMS))


# ── FastAPI App ─────────────────────────────────────────────────
app = FastAPI(title="Application Performance Metrics")


@app.on_event("startup")
def startup():
    seed_data()
    logger.info("App started — connected to Valkey at %s:%s, MongoDB at %s:%s", CACHE_HOST, CACHE_PORT, DB_HOST, DB_PORT)


@app.middleware("http")
async def telemetry_middleware(request: Request, call_next):
    """Middleware that records metrics and logs for every request."""
    attributes = {
        "http.method": request.method,
        "http.route": request.url.path,
    }

    active_requests.add(1, attributes)
    request_counter.add(1, attributes)
    start = time.perf_counter()

    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        request_duration.record(duration_ms, attributes)

        logger.info(
            "Request completed",
            extra={
                "http.method": request.method,
                "http.route": request.url.path,
                "http.status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )

        if response.status_code >= 400:
            error_counter.add(1, {**attributes, "http.status_code": response.status_code})

        return response
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        request_duration.record(duration_ms, attributes)
        error_counter.add(1, {**attributes, "error.type": type(exc).__name__})

        logger.exception("Request failed", extra={"http.route": request.url.path})
        raise
    finally:
        active_requests.add(-1, attributes)


# ── Routes ──────────────────────────────────────────────────────

@app.get("/")
def read_root():
    logger.info("Root endpoint called")
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int):
    """
    Fetch an item — checks Valkey cache first, falls back to MongoDB.
    Traces show: HTTP span → cache lookup → (cache miss) → DB query → cache write.
    """
    cache_key = f"item:{item_id}"

    # 1. Check cache
    with tracer.start_as_current_span("cache-lookup", attributes={"cache.key": cache_key}) as cache_span:
        cached = cache.get(cache_key)

        if cached:
            cache_hit_counter.add(1, {"cache.key_prefix": "item"})
            cache_span.set_attribute("cache.hit", True)
            logger.info("Cache HIT", extra={"item_id": item_id})
            return json.loads(cached)

        cache_miss_counter.add(1, {"cache.key_prefix": "item"})
        cache_span.set_attribute("cache.hit", False)
        logger.info("Cache MISS", extra={"item_id": item_id})

    # 2. Fetch from MongoDB
    with tracer.start_as_current_span("db-query", attributes={"db.collection": "items", "db.operation": "find_one"}) as db_span:
        doc = items_collection.find_one({"item_id": item_id}, {"_id": 0})

        if not doc:
            db_span.set_attribute("db.result", "not_found")
            logger.warning("Item not found in DB", extra={"item_id": item_id})
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")

        db_span.set_attribute("db.result", "found")
        logger.info("Item fetched from DB", extra={"item_id": item_id})

    # 3. Store in cache
    with tracer.start_as_current_span("cache-write", attributes={"cache.key": cache_key, "cache.ttl": CACHE_TTL}):
        cache.setex(cache_key, CACHE_TTL, json.dumps(doc))
        logger.info("Item cached", extra={"item_id": item_id, "ttl": CACHE_TTL})

    return doc


@app.get("/items")
def list_items(category: str | None = None, limit: int = 10):
    """
    List items from MongoDB with optional category filter.
    Demonstrates a more complex DB query trace.
    """
    with tracer.start_as_current_span("db-query-list", attributes={"db.collection": "items", "db.operation": "find"}) as span:
        query = {"category": category} if category else {}
        span.set_attribute("db.query_filter", str(query))

        docs = list(items_collection.find(query, {"_id": 0}).limit(limit))

        span.set_attribute("db.result_count", len(docs))
        logger.info("Listed items", extra={"category": category, "count": len(docs)})

    return {"items": docs, "count": len(docs)}


@app.post("/items")
def create_item(name: str, price: float, category: str = "misc"):
    """Create a new item in MongoDB and invalidate related cache."""
    with tracer.start_as_current_span("db-insert", attributes={"db.collection": "items", "db.operation": "insert_one"}):
        item_id = (items_collection.find_one(sort=[("item_id", -1)]) or {"item_id": 0})["item_id"] + 1
        doc = {"item_id": item_id, "name": name, "price": price, "category": category}
        items_collection.insert_one(doc)
        logger.info("Item created", extra={"item_id": item_id})

    # Invalidate cache for this item
    with tracer.start_as_current_span("cache-invalidate"):
        cache.delete(f"item:{item_id}")

    return {"item_id": item_id, **{k: v for k, v in doc.items() if k != "_id"}}


@app.delete("/items/{item_id}")
def delete_item(item_id: int):
    """Delete an item and remove from cache."""
    with tracer.start_as_current_span("db-delete", attributes={"db.collection": "items", "db.operation": "delete_one"}):
        result = items_collection.delete_one({"item_id": item_id})
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
        logger.info("Item deleted from DB", extra={"item_id": item_id})

    with tracer.start_as_current_span("cache-invalidate"):
        cache.delete(f"item:{item_id}")
        logger.info("Item removed from cache", extra={"item_id": item_id})

    return {"deleted": item_id}


@app.get("/error")
def trigger_error():
    """Endpoint to generate error traces and logs for testing."""
    logger.error("Simulated error triggered")
    raise HTTPException(status_code=500, detail="Simulated server error")


@app.get("/slow")
def slow_endpoint():
    """Endpoint with artificial latency to test duration metrics."""
    with tracer.start_as_current_span("slow-operation") as span:
        delay = random.uniform(0.5, 2.0)
        span.set_attribute("delay_seconds", round(delay, 2))
        logger.warning("Slow endpoint called", extra={"delay_seconds": round(delay, 2)})
        time.sleep(delay)
        return {"message": "slow response", "delay_seconds": round(delay, 2)}


@app.get("/flush-cache")
def flush_cache():
    """Flush the entire Valkey cache — useful for testing cache miss traces."""
    with tracer.start_as_current_span("cache-flush"):
        cache.flushdb()
        logger.info("Cache flushed")
    return {"message": "cache flushed"}