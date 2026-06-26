"""
SanctionPay Backend
- FastAPI + x402 sanction check endpoint
- Gerçek sanction DB'den arama (OFAC, UN, EU, UK, OpenSanctions)
- AI analiz (Claude API)
- Casper on-chain kayıt
"""

import os, json, hashlib, asyncio, httpx
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sanctions.fetcher import search_name, search_crypto_address, get_stats, init_db, SOURCES
from sanctions.scheduler import create_scheduler, startup_fetch

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY", "")
CSPR_CLOUD_API_KEY      = os.getenv("CSPR_CLOUD_API_KEY", "")
CASPER_NODE_URL         = os.getenv("CASPER_NODE_URL", "https://rpc.testnet.casperlabs.io")
CONTRACT_HASH           = os.getenv("CONTRACT_HASH", "")
WRITER_SECRET_KEY       = os.getenv("WRITER_SECRET_KEY", "")
X402_FACILITATOR_URL    = os.getenv("X402_FACILITATOR_URL", "https://x402.cspr.cloud/facilitator")
PAYMENT_RECEIVER_ADDRESS= os.getenv("PAYMENT_RECEIVER_ADDRESS", "")
PRICE_PER_CHECK         = "0.01"

# ── Lifespan (startup / shutdown) ────────────────────────────────────────────
scheduler = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    init_db()
    # İlk yüklemede listeleri çek (arka planda, uygulamayı bloklamadan)
    asyncio.create_task(startup_fetch())
    scheduler = create_scheduler()
    scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown()

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SanctionPay API",
    description="x402-powered AI sanction screening on Casper Network",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Models ───────────────────────────────────────────────────────────────────
class SanctionCheckRequest(BaseModel):
    query: str
    query_type: str = "entity"   # "wallet" | "entity" | "address"
    include_ai_analysis: bool = True

class SanctionCheckResponse(BaseModel):
    query: str
    query_hash: str
    is_sanctioned: bool
    risk_score: int
    lists_matched: list[str]
    matched_entries: list[dict]
    ai_analysis: Optional[str]
    casper_tx_hash: Optional[str]
    timestamp: str
    payment_verified: bool

# ── x402 ─────────────────────────────────────────────────────────────────────
def payment_required_body():
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": "casper-testnet",
            "maxAmountRequired": PRICE_PER_CHECK,
            "resource": f"{os.getenv('API_BASE_URL','http://localhost:8000')}/check",
            "description": "SanctionPay — AI sanction screening (per check)",
            "mimeType": "application/json",
            "payTo": PAYMENT_RECEIVER_ADDRESS,
            "maxTimeoutSeconds": 300,
            "asset": "CSPR",
        }],
        "error": "Payment required"
    }

async def verify_payment(token: str) -> bool:
    if not token: return False
    if not X402_FACILITATOR_URL or "localhost" in X402_FACILITATOR_URL: return True
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{X402_FACILITATOR_URL}/verify", json={"payment": token}, timeout=10)
            return r.status_code == 200 and r.json().get("isValid", False)
    except:
        return False

async def settle_payment(token: str):
    if not token or not X402_FACILITATOR_URL or "localhost" in X402_FACILITATOR_URL: return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{X402_FACILITATOR_URL}/settle", json={"payment": token}, timeout=10)
    except:
        pass

# ── Sanction Arama ────────────────────────────────────────────────────────────
def run_sanction_check(query: str, query_type: str) -> tuple[bool, int, list[str], list[dict]]:
    """
    Gerçek DB'den arama yapar.
    Returns: (is_sanctioned, risk_score, lists_matched, matched_entries)
    """
    matched_entries = []
    lists_matched = set()

    if query_type == "wallet" or query.startswith("0x") or len(query) in (42, 64):
        # Kripto adres araması
        hits = search_crypto_address(query)
        for h in hits:
            lists_matched.add(h["source"])
            matched_entries.append({
                "name": h.get("entity_name", "Unknown"),
                "source": h["source"],
                "type": "crypto_wallet",
                "blockchain": h.get("blockchain"),
            })
    else:
        # İsim / kuruluş araması
        hits = search_name(query)
        for h in hits:
            if h.get("match_score", 0) >= 0.3:
                lists_matched.add(h["source"])
                matched_entries.append({
                    "name": h["name"],
                    "source": h["source"],
                    "type": h.get("entity_type", "unknown"),
                    "programs": h.get("programs"),
                    "listed_on": h.get("listed_on"),
                    "match_score": round(h.get("match_score", 0), 2),
                })

    is_sanctioned = len(matched_entries) > 0
    lists_matched = sorted(lists_matched)

    # Risk skoru
    if is_sanctioned:
        base = 85
        risk_score = min(100, base + len(lists_matched) * 3 + len(matched_entries))
    else:
        risk_score = 5

    return is_sanctioned, risk_score, lists_matched, matched_entries


# ── AI Analiz ────────────────────────────────────────────────────────────────
async def ai_analysis(query, is_sanctioned, risk_score, lists_matched, entries) -> str:
    if not ANTHROPIC_API_KEY:
        return "AI analysis unavailable (ANTHROPIC_API_KEY not set)"
    entries_summary = json.dumps(entries[:3], ensure_ascii=False)
    prompt = f"""You are a senior compliance officer. Analyze this sanction screening result.

Entity screened: {query}
Sanctioned: {is_sanctioned}
Risk score: {risk_score}/100
Lists matched: {', '.join(lists_matched) if lists_matched else 'None'}
Matched entries (sample): {entries_summary}

Provide:
1. Risk assessment (2-3 sentences)
2. Recommended action: ALLOW / BLOCK / ENHANCED_DUE_DILIGENCE
3. Regulatory basis if applicable

Be concise and professional. Max 150 words."""

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 300,
                      "messages": [{"role": "user", "content": prompt}]}
            )
            if r.status_code == 200:
                return r.json()["content"][0]["text"]
    except Exception as e:
        return f"AI analysis error: {e}"
    return "AI analysis unavailable"


# ── Casper Kayıt ─────────────────────────────────────────────────────────────
async def record_on_casper(query_hash, is_sanctioned, risk_score, lists_matched) -> Optional[str]:
    if not CONTRACT_HASH or not WRITER_SECRET_KEY:
        return None
    lists_str = ",".join(lists_matched)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"https://api.testnet.cspr.cloud/deploys",
                headers={"Authorization": f"Bearer {CSPR_CLOUD_API_KEY}", "Content-Type": "application/json"},
                json={"session": {"StoredContractByHash": {
                    "hash": CONTRACT_HASH, "entry_point": "record_check",
                    "args": [
                        ["query_hash", {"cl_type": "String", "bytes": query_hash}],
                        ["is_sanctioned", {"cl_type": "Bool", "bytes": str(is_sanctioned).lower()}],
                        ["risk_score", {"cl_type": "U8", "bytes": str(risk_score)}],
                        ["lists_matched", {"cl_type": "String", "bytes": lists_str}],
                    ]
                }}}
            )
            if r.status_code in (200, 201):
                return r.json().get("deploy_hash")
    except:
        pass
    return None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "service": "SanctionPay", "version": "1.0.0",
        "sources": list(SOURCES.keys()),
        "price_per_check": f"{PRICE_PER_CHECK} CSPR",
        "payment_protocol": "x402",
    }

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/stats")
async def stats():
    return get_stats()

@app.post("/update/{source_key}")
async def trigger_update(source_key: str):
    """Manuel güncelleme tetikle (admin endpoint)."""
    if source_key not in SOURCES and source_key != "all":
        raise HTTPException(404, f"Unknown source: {source_key}")
    if source_key == "all":
        asyncio.create_task(startup_fetch())
        return {"status": "started", "source": "all"}
    from sanctions.fetcher import fetch_source
    from sanctions.scheduler import update_single_source
    asyncio.create_task(update_single_source(source_key))
    return {"status": "started", "source": source_key}

@app.post("/check", response_model=SanctionCheckResponse)
async def check(
    body: SanctionCheckRequest,
    x_payment: Optional[str] = Header(None, alias="X-PAYMENT"),
):
    # x402 kontrolü
    if not x_payment:
        return Response(
            content=json.dumps(payment_required_body()),
            status_code=402, media_type="application/json",
            headers={"X-ACCEPTS-PAYMENT": "x402", "Access-Control-Expose-Headers": "X-ACCEPTS-PAYMENT"},
        )
    if not await verify_payment(x_payment):
        raise HTTPException(402, "Invalid or expired payment")

    return await _do_check(body, payment_verified=True)

@app.post("/check/demo", response_model=SanctionCheckResponse)
async def check_demo(body: SanctionCheckRequest):
    """Demo endpoint — ödeme gerektirmez."""
    return await _do_check(body, payment_verified=False)

async def _do_check(body: SanctionCheckRequest, payment_verified: bool) -> SanctionCheckResponse:
    query = body.query.strip()
    query_hash = hashlib.sha256(query.lower().encode()).hexdigest()

    is_sanctioned, risk_score, lists_matched, matched_entries = run_sanction_check(
        query, body.query_type
    )

    analysis = None
    if body.include_ai_analysis:
        analysis = await ai_analysis(query, is_sanctioned, risk_score, lists_matched, matched_entries)

    tx_hash = await record_on_casper(query_hash, is_sanctioned, risk_score, lists_matched)

    if payment_verified:
        await settle_payment("")

    return SanctionCheckResponse(
        query=query,
        query_hash=query_hash,
        is_sanctioned=is_sanctioned,
        risk_score=risk_score,
        lists_matched=lists_matched,
        matched_entries=matched_entries,
        ai_analysis=analysis,
        casper_tx_hash=tx_hash,
        timestamp=datetime.utcnow().isoformat(),
        payment_verified=payment_verified,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
