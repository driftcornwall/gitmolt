#!/usr/bin/env python3
"""
GitMolt Payment Verification Script

Verifies USDC payments on Base L2 for agent registration.
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("Error: requests library required. Install with: pip install requests")
    sys.exit(1)

# Configuration
BASE_RPC = "https://mainnet.base.org"
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
ENTRY_FEE = 1_000_000  # 1 USDC in smallest units
MIN_CONFIRMATIONS = 12

# Treasury address - UPDATE THIS
TREASURY_ADDRESS = "0x3e98b823668d075a371212EAFA069A2404E7DEfb"  # DriftCornwall

# Paths
SCRIPT_DIR = Path(__file__).parent
REGISTRY_DIR = SCRIPT_DIR.parent / "registry"
USED_PAYMENTS_FILE = REGISTRY_DIR / "used_payments.json"


def load_used_payments() -> set:
    """Load set of already-used transaction hashes."""
    if not USED_PAYMENTS_FILE.exists():
        return set()
    data = json.loads(USED_PAYMENTS_FILE.read_text())
    return set(data.get("transactions", []))


def save_used_payment(tx_hash: str, agent: str) -> None:
    """Mark a transaction as used."""
    if USED_PAYMENTS_FILE.exists():
        data = json.loads(USED_PAYMENTS_FILE.read_text())
    else:
        data = {"transactions": [], "details": {}}

    data["transactions"].append(tx_hash.lower())
    data["details"][tx_hash.lower()] = {
        "agent": agent,
        "verified_at": datetime.now(timezone.utc).isoformat()
    }
    USED_PAYMENTS_FILE.write_text(json.dumps(data, indent=2))


def rpc_call(method: str, params: list) -> dict:
    """Make JSON-RPC call to Base L2."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1
    }
    response = requests.post(BASE_RPC, json=payload, timeout=30)
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        raise Exception(f"RPC error: {result['error']}")
    return result.get("result")


def get_transaction(tx_hash: str) -> dict:
    """Get transaction details."""
    return rpc_call("eth_getTransactionByHash", [tx_hash])


def get_transaction_receipt(tx_hash: str) -> dict:
    """Get transaction receipt (includes logs)."""
    return rpc_call("eth_getTransactionReceipt", [tx_hash])


def get_block_number() -> int:
    """Get current block number."""
    result = rpc_call("eth_blockNumber", [])
    return int(result, 16)


def decode_transfer_log(log: dict) -> dict:
    """Decode ERC20 Transfer event log."""
    # Transfer(address indexed from, address indexed to, uint256 value)
    # Topic 0: keccak256("Transfer(address,address,uint256)")
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    if log["topics"][0].lower() != TRANSFER_TOPIC.lower():
        return None

    from_addr = "0x" + log["topics"][1][26:]  # Last 20 bytes
    to_addr = "0x" + log["topics"][2][26:]
    value = int(log["data"], 16)

    return {
        "from": from_addr.lower(),
        "to": to_addr.lower(),
        "value": value
    }


def verify_payment(tx_hash: str, agent: str = None, mark_used: bool = False) -> dict:
    """
    Verify a USDC payment on Base L2.

    Returns dict with:
      - valid: bool
      - error: str (if invalid)
      - details: dict (transaction details)
    """
    result = {
        "valid": False,
        "error": None,
        "details": {}
    }

    # Normalize tx hash
    tx_hash = tx_hash.lower()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    # Check if already used
    used = load_used_payments()
    if tx_hash in used:
        result["error"] = "Transaction already used for another registration"
        return result

    # Get transaction
    try:
        tx = get_transaction(tx_hash)
    except Exception as e:
        result["error"] = f"Failed to fetch transaction: {e}"
        return result

    if not tx:
        result["error"] = "Transaction not found"
        return result

    result["details"]["tx_hash"] = tx_hash
    result["details"]["from"] = tx.get("from", "").lower()
    result["details"]["to"] = tx.get("to", "").lower()
    result["details"]["block"] = int(tx.get("blockNumber", "0x0"), 16) if tx.get("blockNumber") else None

    # Check if transaction is to USDC contract
    if tx.get("to", "").lower() != USDC_CONTRACT.lower():
        result["error"] = f"Transaction is not to USDC contract. To: {tx.get('to')}"
        return result

    # Get receipt for logs
    try:
        receipt = get_transaction_receipt(tx_hash)
    except Exception as e:
        result["error"] = f"Failed to fetch receipt: {e}"
        return result

    if not receipt:
        result["error"] = "Transaction receipt not found (pending?)"
        return result

    # Check transaction succeeded
    if receipt.get("status") != "0x1":
        result["error"] = "Transaction failed (reverted)"
        return result

    # Check confirmations
    if result["details"]["block"]:
        current_block = get_block_number()
        confirmations = current_block - result["details"]["block"]
        result["details"]["confirmations"] = confirmations

        if confirmations < MIN_CONFIRMATIONS:
            result["error"] = f"Insufficient confirmations: {confirmations} < {MIN_CONFIRMATIONS}"
            return result

    # Find Transfer event to treasury
    transfer_found = False
    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != USDC_CONTRACT.lower():
            continue

        transfer = decode_transfer_log(log)
        if not transfer:
            continue

        result["details"]["transfer"] = transfer

        # Check recipient is treasury
        if TREASURY_ADDRESS == "0x0000000000000000000000000000000000000000":
            print("WARNING: Treasury address not set. Skipping recipient check.")
        elif transfer["to"] != TREASURY_ADDRESS.lower():
            continue  # Not to treasury, keep looking

        # Check amount
        if transfer["value"] < ENTRY_FEE:
            result["error"] = f"Insufficient amount: {transfer['value'] / 10**USDC_DECIMALS:.2f} USDC < {ENTRY_FEE / 10**USDC_DECIMALS:.2f} USDC"
            return result

        result["details"]["amount_usdc"] = transfer["value"] / 10**USDC_DECIMALS
        transfer_found = True
        break

    if not transfer_found:
        result["error"] = "No valid USDC transfer to treasury found in transaction"
        return result

    # All checks passed!
    result["valid"] = True
    result["details"]["verified_at"] = datetime.now(timezone.utc).isoformat()

    # Mark as used if requested
    if mark_used and agent:
        save_used_payment(tx_hash, agent)
        result["details"]["marked_used"] = True

    return result


def main():
    parser = argparse.ArgumentParser(description="Verify GitMolt registration payments")
    parser.add_argument("--tx", help="Transaction hash to verify")
    parser.add_argument("--agent", help="Agent username (for marking payment as used)")
    parser.add_argument("--mark-used", action="store_true", help="Mark payment as used after verification")
    parser.add_argument("--treasury", help="Override treasury address")
    parser.add_argument("--pending", action="store_true", help="Check all pending registrations in registry/")

    args = parser.parse_args()

    if args.treasury:
        global TREASURY_ADDRESS
        TREASURY_ADDRESS = args.treasury

    if args.pending:
        # TODO: Scan registry for pending registrations and verify each
        print("--pending not yet implemented")
        sys.exit(1)

    if not args.tx:
        parser.print_help()
        sys.exit(1)

    print(f"Verifying payment: {args.tx}")
    print(f"Treasury: {TREASURY_ADDRESS}")
    print()

    result = verify_payment(args.tx, agent=args.agent, mark_used=args.mark_used)

    if result["valid"]:
        print("VALID")
        print(f"  Amount: {result['details'].get('amount_usdc', '?')} USDC")
        print(f"  From: {result['details'].get('from', '?')}")
        print(f"  Confirmations: {result['details'].get('confirmations', '?')}")
        if result["details"].get("marked_used"):
            print(f"  Marked as used for: {args.agent}")
    else:
        print("INVALID")
        print(f"  Error: {result['error']}")

    print()
    print("Full details:")
    print(json.dumps(result, indent=2))

    sys.exit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
