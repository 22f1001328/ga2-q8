import base64
import time
import uuid
from collections import deque

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI()

TOTAL_ORDERS = 54
RATE_LIMIT = 19
WINDOW_SECONDS = 10

# Fixed catalog: IDs 1..TOTAL_ORDERS, static/deterministic
CATALOG = {
    i: {"id": i, "item": f"Order-{i}", "amount": round(10.5 * i, 2)}
    for i in range(1, TOTAL_ORDERS + 1)
}

# Idempotency store: key -> created order dict
IDEMPOTENCY_STORE = {}

# Rate limit buckets: client_id -> deque of request timestamps
RATE_BUCKETS = {}


def encode_cursor(next_id: int) -> str:
    raw = str(next_id).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode())
        return int(raw.decode())
    except Exception:
        return 1


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_id = request.headers.get("X-Client-Id")

    if client_id:
        now = time.time()
        bucket = RATE_BUCKETS.setdefault(client_id, deque())

        while bucket and now - bucket[0] > WINDOW_SECONDS:
            bucket.popleft()

        if len(bucket) >= RATE_LIMIT:
            oldest = bucket[0]
            retry_after = max(1, int(WINDOW_SECONDS - (now - oldest)) + 1)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)

    return await call_next(request)


# CORS must be the outermost middleware (added last) so that 429 responses
# returned directly by rate_limit_middleware still get CORS headers, and so
# Retry-After is visible to cross-origin JS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


@app.post("/orders")
async def create_order(request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    if not idempotency_key:
        return JSONResponse(status_code=400, content={"detail": "Idempotency-Key header is required"})

    if idempotency_key in IDEMPOTENCY_STORE:
        existing = IDEMPOTENCY_STORE[idempotency_key]
        return JSONResponse(status_code=200, content=existing)

    try:
        body = await request.json()
    except Exception:
        body = {}

    new_order = {
        "id": str(uuid.uuid4()),
        "item": body.get("item", "Unnamed Order") if isinstance(body, dict) else "Unnamed Order",
        "amount": body.get("amount", 0) if isinstance(body, dict) else 0,
    }

    IDEMPOTENCY_STORE[idempotency_key] = new_order
    return JSONResponse(status_code=201, content=new_order)


@app.get("/orders")
async def list_orders(limit: int = 10, cursor: str | None = None):
    limit = max(1, min(limit, TOTAL_ORDERS))
    start_id = decode_cursor(cursor) if cursor else 1
    start_id = max(1, start_id)

    end_id = min(start_id + limit - 1, TOTAL_ORDERS)
    items = [CATALOG[i] for i in range(start_id, end_id + 1)]

    next_id = end_id + 1
    next_cursor = encode_cursor(next_id) if next_id <= TOTAL_ORDERS else None

    return {
        "items": items,
        "orders": items,
        "next_cursor": next_cursor,
        "next": next_cursor,
    }