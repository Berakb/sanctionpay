# 🛡 SanctionPay

> **AI-powered sanction screening agent on Casper Network**  
> Built for [Casper Agentic Buildathon 2026](https://dorahacks.io/hackathon/casper-agentic-buildathon/detail) · $150,000 prize pool

## What is SanctionPay?

SanctionPay is an autonomous compliance agent that screens crypto wallets, entities, and individuals against international sanction lists (OFAC SDN, UN Security Council, EU Consolidated). 

Every screening is:
- **Paid via x402** — agent pays micropayments autonomously on Casper Network
- **Analyzed by AI** — Claude provides compliance recommendations
- **Recorded on-chain** — results are immutably stored on Casper blockchain

## Architecture

```
┌─────────────────┐    x402 payment    ┌─────────────────┐
│   AI Agent      │ ─────────────────► │  FastAPI Backend │
│  (Claude API)   │ ◄───────────────── │  (Sanction Check)│
└─────────────────┘   check result     └────────┬────────┘
                                                 │
                                    record_check │ on-chain
                                                 ▼
                                       ┌─────────────────┐
                                       │  Casper Network  │
                                       │  (Odra Contract) │
                                       └─────────────────┘
```

**Stack:**
| Layer | Technology |
|---|---|
| Smart Contract | Rust + Odra Framework → Casper |
| Backend | Python + FastAPI + x402 |
| AI Engine | Claude API (claude-sonnet-4-6) |
| Payment | x402 Facilitator (CSPR.cloud) |
| Local Testing | casper-nctl-docker |
| Frontend | HTML/JS + CSPR.design tokens |

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_USERNAME/sanctionpay
cd sanctionpay
cp .env.example .env
# Edit .env with your API keys
```

### 2. Start with Docker

```bash
docker-compose up
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API Docs: http://localhost:8000/docs
- Casper NCTL: http://localhost:11101

### 2b. Or run natively (no Docker)

Fastest way to bring up the backend + UI. Missing keys degrade gracefully
(AI analysis and on-chain recording simply no-op).

```bash
# Backend — http://localhost:8000
cd backend
python -m venv .venv && . .venv/Scripts/activate   # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend — http://localhost:3000 (separate shell)
cd frontend && python -m http.server 3000
```

On Windows, `./run-local.ps1` starts both. On startup the backend fetches live
OFAC (SDN) and UN Security Council lists into a local SQLite DB (~20k records);
`GET /stats` shows what loaded.

### 3. Deploy smart contract

```bash
# Install Rust + Odra
rustup target add wasm32-unknown-unknown
cargo install cargo-odra --locked

# Build and test
cd contracts
cargo odra test
cargo odra test -b casper

# Deploy to testnet
cargo odra deploy --network testnet
```

### 4. Run the AI agent

```bash
cd agent
pip install httpx anthropic
python agent.py
```

## Testnet Deployment & Verification

On-chain artifacts for the Casper **testnet** deployment. (Replace the
placeholders once the contract is deployed — see step 3 above.)

| Artifact | Value |
|---|---|
| Contract package hash | `_pending deployment_` |
| Contract hash | `_pending deployment_` |
| Deploy (init) tx | `_pending deployment_` |
| Sample `record_check` tx | `_pending deployment_` |
| Explorer | https://testnet.cspr.live/ |

Set `CONTRACT_HASH` (and the authorized `WRITER_SECRET_KEY`) in `.env` to enable
the backend's on-chain recording of each screening.

### Testing playbook (for reviewers)

1. **Bring it up** — `docker compose up` (or the native path in step 2b), then
   open http://localhost:3000.
2. **Screen a sanctioned entity** — enter `Kim Chol` → expect **BLOCK**, a
   UN-SC match, and a high risk score. Try a crypto wallet or a clean company
   name to see **ALLOW**.
3. **Verify the x402 flow** — call the protected endpoint directly:
   ```bash
   # 1) No payment header → 402 Payment Required
   curl -i -X POST http://localhost:8000/check \
     -H 'Content-Type: application/json' \
     -d '{"query":"Kim Chol","query_type":"entity"}'

   # 2) Retry with an X-PAYMENT header → 200 + screening result
   curl -s -X POST http://localhost:8000/check \
     -H 'Content-Type: application/json' \
     -H 'X-PAYMENT: <token>' \
     -d '{"query":"Kim Chol","query_type":"entity"}'
   ```
4. **Inspect loaded data** — `GET http://localhost:8000/stats`.
5. **Check on-chain record** — once deployed, the response includes
   `casper_tx_hash`; open it on [cspr.live](https://testnet.cspr.live/).

## API Reference

### `POST /check` (x402 protected)

Screen an entity for sanctions. Requires x402 payment in `X-PAYMENT` header.

```bash
# First call returns 402 with payment requirements
curl -X POST http://localhost:8000/check \
  -H "Content-Type: application/json" \
  -d '{"query": "0x7f268357a8c2552623316e2562d90e642bb538e5", "query_type": "wallet"}'

# Retry with payment header
curl -X POST http://localhost:8000/check \
  -H "Content-Type: application/json" \
  -H "X-PAYMENT: <x402-payment-token>" \
  -d '{"query": "0x7f268357a8c2552623316e2562d90e642bb538e5", "query_type": "wallet"}'
```

### `POST /check/demo` (no payment required)

Same as `/check` but without x402 — for testing and demos.

### `GET /stats`

Service statistics and configuration.

## Sanction Lists Monitored

| List | Coverage |
|---|---|
| OFAC SDN | US Treasury — individuals, entities, wallets |
| OFAC Crypto | Sanctioned blockchain addresses |
| UN Security Council | International arms/terrorism sanctions |
| EU Consolidated | European Union sanctions list |

## x402 Payment Flow

```
Agent → POST /check → 402 Payment Required
     ← {accepts: [{network: "casper-testnet", amount: "0.01 CSPR", ...}]}
Agent → Signs payment with Casper wallet
Agent → POST /check + X-PAYMENT header
     ← {is_sanctioned: bool, risk_score: int, ai_analysis: str, casper_tx_hash: str}
```

## Smart Contract (Odra/Casper)

The `SanctionPay` contract records every check on-chain:

```rust
// Store a sanction check result
contract.record_check(
    query_hash,      // SHA256 of queried entity (privacy preserving)
    is_sanctioned,   // bool
    risk_score,      // u8 (0-100)
    lists_matched,   // "OFAC-SDN,UN-SC"
);

// Retrieve any past check
let result = contract.get_result(query_hash);
```

Every result is timestamped and signed by the authorized writer, creating an immutable audit trail.

## Team

Built with compliance expertise in banking, finance, and regulation.

---

*SanctionPay — Compliance infrastructure for the agentic economy*
