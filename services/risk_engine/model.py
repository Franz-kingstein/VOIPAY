import os
import pickle
import numpy as np
from services.risk_engine.train_synthetic import train_and_save_model

class RiskModelWrapper:
    def __init__(self, model_path: str = "model.pkl"):
        self.model_path = model_path
        if not os.path.exists(model_path):
            print(f"Model path {model_path} not found. Training a new model...")
            train_and_save_model(model_path)
        
        with open(model_path, "rb") as f:
            self.model = pickle.load(f)

    def predict_risk(
        self,
        amount: float,
        hour_of_day: int,
        velocity_last_1h: int,
        is_new_payee: int,
        device_trust_score: float,
        mandate_flag: int
    ) -> float:
        features = np.array([[
            amount,
            hour_of_day,
            velocity_last_1h,
            is_new_payee,
            device_trust_score,
            mandate_flag
        ]])
        
        # score_samples returns negative values (lower = more anomalous)
        # Typically normal is around -0.4 to -0.5, anomalous is < -0.6
        raw_score = self.model.score_samples(features)[0]
        
        # Convert raw_score to a 0.0 to 1.0 probability range (1.0 = highly anomalous)
        # normal threshold score_samples is typically around -0.45.
        # Let's map -0.45 -> 0.0 and -0.75 -> 1.0
        anomaly_prob = float(np.clip((raw_score - (-0.45)) / (-0.3), 0.0, 1.0))
        return anomaly_prob
