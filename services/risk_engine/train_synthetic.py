import numpy as np
import pickle
import os
from sklearn.ensemble import IsolationForest

def generate_synthetic_data(num_samples: int = 5000):
    np.random.seed(42)
    
    # 95% normal transactions
    num_normal = int(num_samples * 0.95)
    num_fraud = num_samples - num_normal
    
    # Normal data generation
    normal_amounts = np.random.exponential(scale=1000, size=num_normal) + 10  # average around ₹1000
    normal_hours = np.random.randint(8, 23, size=num_normal)  # daytime
    normal_velocity = np.random.poisson(lam=1.5, size=num_normal) + 1  # 1 to 3 transactions
    normal_new_payee = np.random.binomial(n=1, p=0.2, size=num_normal)  # 20% new payee
    normal_device_trust = np.random.uniform(0.7, 1.0, size=num_normal)  # high trust
    normal_mandate = np.random.binomial(n=1, p=0.3, size=num_normal)  # 30% mandate
    
    normal_features = np.column_stack((
        normal_amounts,
        normal_hours,
        normal_velocity,
        normal_new_payee,
        normal_device_trust,
        normal_mandate
    ))

    # Fraudulent data generation (anomalies)
    fraud_amounts = np.random.uniform(15000, 150000, size=num_fraud)  # high amounts
    fraud_hours = np.random.randint(0, 6, size=num_fraud)  # late night/early morning
    fraud_velocity = np.random.poisson(lam=8, size=num_fraud) + 2  # high velocity
    fraud_new_payee = np.ones(num_fraud)  # always new payee
    fraud_device_trust = np.random.uniform(0.0, 0.4, size=num_fraud)  # low trust
    fraud_mandate = np.zeros(num_fraud)  # never mandate
    
    fraud_features = np.column_stack((
        fraud_amounts,
        fraud_hours,
        fraud_velocity,
        fraud_new_payee,
        fraud_device_trust,
        fraud_mandate
    ))

    X = np.vstack((normal_features, fraud_features))
    return X

def train_and_save_model(model_path: str = "model.pkl"):
    print("Generating synthetic transactions dataset...")
    X = generate_synthetic_data()
    
    print("Training IsolationForest anomaly detection model...")
    # IsolationForest configuration
    # contamination = 0.05 (matches our synthetic anomaly ratio)
    model = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    model.fit(X)
    
    print(f"Saving trained model to {model_path}...")
    with open(model_path, "wb") as f:
        pickle.dump(model, f)
    print("Model training complete.")

if __name__ == "__main__":
    train_and_save_model()
