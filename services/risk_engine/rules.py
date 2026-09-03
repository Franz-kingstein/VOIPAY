import time
from typing import Dict, List, Tuple

# Keep track of timestamps of transactions per session in memory for velocity check
# In production, this would use Redis.
session_transaction_times: Dict[str, List[float]] = {}

def clean_old_timestamps(session_id: str, window_seconds: float = 60.0):
    now = time.time()
    if session_id in session_transaction_times:
        # Keep only timestamps within window
        session_transaction_times[session_id] = [
            t for t in session_transaction_times[session_id] if now - t <= window_seconds
        ]

def evaluate_hard_rules(
    amount: float,
    velocity_last_1h: int,
    mandate_flag: int,
    session_id: str
) -> Tuple[str, List[str]]:
    reasons = []
    decision = "allow"

    # Rule 1: High Velocity Decline
    # Clean up and log this transaction timestamp
    now = time.time()
    clean_old_timestamps(session_id, window_seconds=60.0)
    
    if session_id not in session_transaction_times:
        session_transaction_times[session_id] = []
    session_transaction_times[session_id].append(now)
    
    # Check 1-minute velocity
    minute_velocity = len(session_transaction_times[session_id])
    if minute_velocity > 5:
        reasons.append("VELOCITY_EXCEEDED: More than 5 transactions per minute from same session")
        decision = "decline"
        return decision, reasons

    # Rule 2: Amount Threshold without Mandate -> auto-review
    if amount > 100000.0 and mandate_flag == 0:
        reasons.append("HIGH_VALUE_NO_MANDATE: Amount exceeding ₹1,00,000 without an active mandate")
        decision = "review"

    return decision, reasons
