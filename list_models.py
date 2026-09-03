import os
import httpx
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

try:
    resp = httpx.get(url, timeout=15.0)
    print(f"Status Code: {resp.status_code}")
    if resp.status_code == 200:
        models_data = resp.json()
        print("Available Models:")
        for model in models_data.get("models", []):
            name = model.get("name", "")
            methods = model.get("supportedGenerationMethods", [])
            print(f"- {name} (Methods: {methods})")
    else:
        print(f"Failed: {resp.text}")
except Exception as e:
    print(f"Exception: {e}")
