import yaml
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

class UPIReservePay:
    def __init__(self, config_path: str = "config.yaml"):
        self.max_single_amount = 100000.0
        self.mandates: Dict[str, Dict[str, Any]] = {}
        
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f)
                    self.max_single_amount = cfg.get("mandate_limits", {}).get("max_single_amount", 100000.0)
            except Exception as e:
                print(f"Error loading config.yaml in UPIReservePay: {e}")

    def create_mandate(self, mandate_id: str, max_amount: float, payee_name: str, frequency: str = "monthly") -> Dict[str, Any]:
        if max_amount > self.max_single_amount:
            raise ValueError(f"Mandate amount ₹{max_amount} exceeds maximum allowed simulator limit ₹{self.max_single_amount}")
        
        self.mandates[mandate_id] = {
            "mandate_id": mandate_id,
            "max_amount": max_amount,
            "remaining_limit": max_amount,
            "frequency": frequency,
            "payee_name": payee_name,
            "status": "active",
            "expires_at": (datetime.utcnow() + timedelta(days=365)).isoformat()
        }
        return self.mandates[mandate_id]

    def get_mandate(self, mandate_id: str) -> Optional[Dict[str, Any]]:
        return self.mandates.get(mandate_id)

    def validate_and_debit(self, mandate_id: str, amount: float) -> Dict[str, Any]:
        mandate = self.mandates.get(mandate_id)
        if not mandate:
            return {"valid": False, "reason": "UPI: Mandate not found"}
        
        if mandate["status"] != "active":
            return {"valid": False, "reason": f"UPI: Mandate is {mandate['status']}"}
            
        if amount > mandate["remaining_limit"]:
            return {
                "valid": False, 
                "reason": f"UPI: Amount ₹{amount} exceeds remaining mandate limit ₹{mandate['remaining_limit']}"
            }
            
        # Deduct the amount from the mandate limit
        mandate["remaining_limit"] -= amount
        if mandate["remaining_limit"] <= 0:
            mandate["status"] = "exhausted"
            
        return {"valid": True, "reason": "UPI: Mandate validated and debited successfully", "mandate": mandate}
