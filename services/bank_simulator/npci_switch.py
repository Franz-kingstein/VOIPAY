import asyncio
import random
import yaml
import os
from typing import Dict, Any

class NPCISwitch:
    def __init__(self, config_path: str = "config.yaml"):
        # Default config in case file load fails
        self.min_latency = 800
        self.max_latency = 2500
        self.decline_rate = 0.03
        
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f)
                    self.min_latency = cfg.get("latency_ms", {}).get("min", 800)
                    self.max_latency = cfg.get("latency_ms", {}).get("max", 2500)
                    self.decline_rate = cfg.get("decline_rate", 0.03)
            except Exception as e:
                print(f"Error loading config.yaml in NPCISwitch: {e}")

    async def process_debit(self, amount: float, payee: str) -> Dict[str, Any]:
        # Simulate network latency
        sleep_time = random.randint(self.min_latency, self.max_latency) / 1000.0
        await asyncio.sleep(sleep_time)

        # Roll for random decline
        if random.random() < self.decline_rate:
            return {
                "status": "failed",
                "reason": "NPCI: Insufficient funds or routing failure",
                "utr_number": None
            }

        # Successful transaction
        utr_number = f"UTR{random.randint(100000000000, 999999999999)}"
        return {
            "status": "captured",
            "reason": "NPCI: Transaction approved",
            "utr_number": utr_number
        }
