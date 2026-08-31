"""List Gemini models available to this API key."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai

from app.config import load_config

config = load_config()
client = genai.Client(api_key=config.llm_api_key)

print("Models supporting generateContent:\n")

for model in client.models.list():
    actions = getattr(model, "supported_actions", None) or []

    if "generateContent" in actions or not actions:
        print(f"  {model.name}")