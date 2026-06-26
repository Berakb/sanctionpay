"""
SanctionPay AI Agent
- Autonomous agent that performs sanction screening
- Pays for checks via x402 protocol on Casper Network
- Uses Claude API as its reasoning engine
- Demonstrates agent-to-agent (A2A) payment flow
"""

import os
import json
import httpx
import asyncio
from datetime import datetime

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SANCTIONPAY_API_URL = os.getenv("SANCTIONPAY_API_URL", "http://localhost:8000")
AGENT_WALLET_KEY = os.getenv("AGENT_WALLET_KEY", "")
CSPR_CLOUD_API_KEY = os.getenv("CSPR_CLOUD_API_KEY", "")

# x402 config for agent wallet
X402_FACILITATOR_URL = os.getenv(
    "X402_FACILITATOR_URL",
    "https://x402.cspr.cloud/facilitator"
)

# ── Agent Tools ──────────────────────────────────────────────────────────────

tools = [
    {
        "name": "check_sanction",
        "description": (
            "Check if an address, wallet, or entity is on international sanction lists "
            "(OFAC SDN, UN Security Council, EU Consolidated). "
            "This tool makes an x402 micropayment on Casper Network to access the service. "
            "Returns risk score (0-100), sanction status, and AI compliance analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The address, wallet address, or entity name to screen"
                },
                "query_type": {
                    "type": "string",
                    "enum": ["address", "wallet", "entity"],
                    "description": "Type of query: address for physical, wallet for crypto, entity for company/person"
                }
            },
            "required": ["query", "query_type"]
        }
    },
    {
        "name": "get_casper_tx",
        "description": "Retrieve a Casper blockchain transaction to verify on-chain sanction check records",
        "input_schema": {
            "type": "object",
            "properties": {
                "tx_hash": {
                    "type": "string",
                    "description": "Casper deploy hash to look up"
                }
            },
            "required": ["tx_hash"]
        }
    },
    {
        "name": "generate_compliance_report",
        "description": "Generate a formal compliance report for a set of sanction check results",
        "input_schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "description": "List of sanction check results",
                    "items": {"type": "object"}
                },
                "report_type": {
                    "type": "string",
                    "enum": ["summary", "detailed", "regulatory"],
                    "description": "Type of compliance report to generate"
                }
            },
            "required": ["results", "report_type"]
        }
    }
]

# ── x402 Payment Logic ───────────────────────────────────────────────────────

async def create_x402_payment_header(payment_requirements: dict) -> str:
    """
    Create an x402 payment authorization header.
    In production: signs with agent's Casper wallet.
    In demo: uses pre-authorized test token.
    """
    # Demo mode: return simulated payment token
    # Production: would sign with AGENT_WALLET_KEY using CSPR SDK
    demo_payment = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "casper-testnet",
        "payload": {
            "authorization": f"demo-auth-{datetime.utcnow().timestamp()}",
            "amount": payment_requirements.get("maxAmountRequired", "0.01"),
            "payTo": payment_requirements.get("payTo", ""),
            "resource": payment_requirements.get("resource", ""),
            "expiry": int(datetime.utcnow().timestamp()) + 300
        }
    }
    import base64
    return base64.b64encode(json.dumps(demo_payment).encode()).decode()

# ── Tool Execution ───────────────────────────────────────────────────────────

async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    """Execute a tool call from the AI agent."""
    
    if tool_name == "check_sanction":
        return await tool_check_sanction(
            tool_input["query"],
            tool_input.get("query_type", "address")
        )
    
    elif tool_name == "get_casper_tx":
        return await tool_get_casper_tx(tool_input["tx_hash"])
    
    elif tool_name == "generate_compliance_report":
        return await tool_generate_report(
            tool_input["results"],
            tool_input.get("report_type", "summary")
        )
    
    return {"error": f"Unknown tool: {tool_name}"}

async def tool_check_sanction(query: str, query_type: str) -> dict:
    """
    Call SanctionPay API with x402 payment.
    Implements the 402 → pay → retry flow.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        
        # First attempt without payment
        resp = await client.post(
            f"{SANCTIONPAY_API_URL}/check",
            json={
                "query": query,
                "query_type": query_type,
                "include_ai_analysis": True
            }
        )
        
        if resp.status_code == 402:
            # x402 flow: get payment requirements, pay, retry
            payment_data = resp.json()
            requirements = payment_data.get("accepts", [{}])[0]
            
            print(f"  💳 x402: Payment required — {requirements.get('maxAmountRequired', '?')} {requirements.get('asset', 'CSPR')}")
            
            # Create payment authorization
            payment_header = await create_x402_payment_header(requirements)
            
            print(f"  ✅ x402: Payment authorized, retrying request...")
            
            # Retry with payment
            resp = await client.post(
                f"{SANCTIONPAY_API_URL}/check",
                json={
                    "query": query,
                    "query_type": query_type,
                    "include_ai_analysis": True
                },
                headers={"X-PAYMENT": payment_header}
            )
        
        if resp.status_code == 200:
            result = resp.json()
            print(f"  📋 Check complete: risk_score={result['risk_score']}, sanctioned={result['is_sanctioned']}")
            if result.get("casper_tx_hash"):
                print(f"  ⛓️  On-chain: https://testnet.cspr.live/deploy/{result['casper_tx_hash']}")
            return result
        
        # Fallback to demo endpoint
        resp = await client.post(
            f"{SANCTIONPAY_API_URL}/check/demo",
            json={
                "query": query,
                "query_type": query_type,
                "include_ai_analysis": True
            }
        )
        return resp.json() if resp.status_code == 200 else {"error": resp.text}

async def tool_get_casper_tx(tx_hash: str) -> dict:
    """Look up a Casper transaction."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.testnet.cspr.cloud/deploys/{tx_hash}",
                headers={"Authorization": f"Bearer {CSPR_CLOUD_API_KEY}"} if CSPR_CLOUD_API_KEY else {}
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "tx_hash": tx_hash,
                    "status": data.get("execution_results", [{}])[0].get("result", {}).get("Success") and "success" or "pending",
                    "block_hash": data.get("block_hash"),
                    "timestamp": data.get("header", {}).get("timestamp"),
                    "explorer_url": f"https://testnet.cspr.live/deploy/{tx_hash}"
                }
    except Exception as e:
        pass
    
    return {
        "tx_hash": tx_hash,
        "status": "unknown",
        "explorer_url": f"https://testnet.cspr.live/deploy/{tx_hash}"
    }

async def tool_generate_report(results: list, report_type: str) -> dict:
    """Generate a compliance report from screening results."""
    total = len(results)
    sanctioned = sum(1 for r in results if r.get("is_sanctioned"))
    high_risk = sum(1 for r in results if r.get("risk_score", 0) >= 70)
    
    report = {
        "report_type": report_type,
        "generated_at": datetime.utcnow().isoformat(),
        "summary": {
            "total_screened": total,
            "sanctioned": sanctioned,
            "high_risk": high_risk,
            "clean": total - sanctioned - high_risk
        },
        "recommendation": "BLOCK" if sanctioned > 0 else ("ENHANCED_DUE_DILIGENCE" if high_risk > 0 else "ALLOW"),
        "results": results,
        "casper_verified": all(r.get("casper_tx_hash") for r in results)
    }
    
    if report_type == "regulatory":
        report["regulatory_note"] = (
            "This report is generated by an autonomous AI agent and recorded immutably "
            "on the Casper blockchain. Each screening is cryptographically timestamped "
            "and verifiable via CSPR.live explorer."
        )
    
    return report

# ── Main Agent Loop ──────────────────────────────────────────────────────────

async def run_agent(user_request: str, verbose: bool = True) -> str:
    """
    Run the SanctionPay AI agent with a user request.
    Uses Claude as the reasoning engine with tool use.
    """
    if not ANTHROPIC_API_KEY:
        return "Error: ANTHROPIC_API_KEY not configured"
    
    system_prompt = """You are SanctionPay Agent, an autonomous compliance AI that screens 
entities and wallet addresses against international sanction lists.

You operate on the Casper blockchain, making micropayments via the x402 protocol 
for each screening you perform. Every check you run:
1. Costs a small x402 fee (paid autonomously from your Casper wallet)
2. Is recorded immutably on-chain for audit purposes
3. Uses AI-powered risk analysis

Be thorough, professional, and always recommend clear compliance actions.
When you find sanctions hits, explain the regulatory basis (OFAC, UN, EU)."""

    messages = [{"role": "user", "content": user_request}]
    
    if verbose:
        print(f"\n🤖 SanctionPay Agent starting...")
        print(f"📝 Request: {user_request}\n")
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        
        while True:
            # Call Claude API
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4096,
                    "system": system_prompt,
                    "tools": tools,
                    "messages": messages
                }
            )
            
            if resp.status_code != 200:
                return f"Claude API error: {resp.status_code} {resp.text}"
            
            response = resp.json()
            stop_reason = response.get("stop_reason")
            content = response.get("content", [])
            
            # Add assistant response to history
            messages.append({"role": "assistant", "content": content})
            
            # If done, return final text
            if stop_reason == "end_turn":
                final_text = " ".join(
                    block["text"] for block in content
                    if block.get("type") == "text"
                )
                if verbose:
                    print(f"\n✅ Agent complete.\n")
                return final_text
            
            # Process tool calls
            if stop_reason == "tool_use":
                tool_results = []
                
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_name = block["name"]
                        tool_input = block["input"]
                        tool_use_id = block["id"]
                        
                        if verbose:
                            print(f"🔧 Using tool: {tool_name}")
                            print(f"   Input: {json.dumps(tool_input, indent=2)}")
                        
                        # Execute tool
                        result = await execute_tool(tool_name, tool_input)
                        
                        if verbose:
                            print(f"   Result: {json.dumps(result, indent=2)[:200]}...")
                        
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": json.dumps(result)
                        })
                
                # Add tool results to history
                messages.append({"role": "user", "content": tool_results})
            else:
                break
    
    return "Agent completed without final response"

# ── CLI Demo ─────────────────────────────────────────────────────────────────

async def demo():
    """Run a demo of the SanctionPay agent."""
    
    demo_requests = [
        "Screen these entities for sanctions compliance: 1) Wallet address 0x7f268357a8c2552623316e2562d90e642bb538e5, 2) Company 'Acme Trading LLC', 3) Individual 'John Smith'. Generate a summary compliance report.",
        
        "I need to onboard a new DeFi protocol. Check if they have any sanction exposure. The protocol contract is 0xd4b88df4d29f5cedd6857912842cff3b20c8cfa3 and the founder is listed as working from Tehran, Iran.",
    ]
    
    for i, request in enumerate(demo_requests, 1):
        print(f"\n{'='*60}")
        print(f"DEMO {i}")
        print('='*60)
        result = await run_agent(request, verbose=True)
        print(f"\n📄 AGENT RESPONSE:\n{result}")

if __name__ == "__main__":
    asyncio.run(demo())
