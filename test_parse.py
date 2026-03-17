"""Test JSON parsing from Gemini responses."""
import os
import re
import json
from dotenv import load_dotenv
load_dotenv('.env')

from backend.services.ai import ai_generate, ai_generate_json
import asyncio

prompt = 'Analyze this job. Return JSON: {"culture_insights": "brief", "interview_process": "brief", "growth_opportunities": "brief", "day_in_life": "brief", "hiring_sentiment": "brief"}\n\nJOB: CISO at KPMG, Toronto'

raw = asyncio.run(ai_generate(prompt, max_tokens=800, model_tier='smart'))
print("RAW length:", len(raw))
print("RAW first 100:", repr(raw[:100]))
print()

# Try the fence match regex
fence_match = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", raw)
print("Fence match:", bool(fence_match))
if fence_match:
    content = fence_match.group(1).strip()
    print("Extracted length:", len(content))
    print("Extracted first 100:", repr(content[:100]))
    try:
        parsed = json.loads(content)
        print("PARSED OK:", type(parsed).__name__)
        if isinstance(parsed, dict):
            print("Keys:", list(parsed.keys()))
    except Exception as e:
        print("Parse error:", e)
        print("Full content:", content[:500])
else:
    # Try the object extraction approach
    obj_match = re.search(r"(\{[\s\S]*\})", raw)
    print("Object match:", bool(obj_match))
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(1))
            print("Object PARSED OK:", type(parsed).__name__)
        except Exception as e:
            print("Object parse error:", e)

# Now test via ai_generate_json
print("\n--- ai_generate_json test ---")
result = asyncio.run(ai_generate_json(prompt, max_tokens=800, model_tier='smart'))
print("Result type:", type(result).__name__)
if isinstance(result, dict):
    print("Keys:", list(result.keys()))
    for k, v in result.items():
        preview = str(v)[:80]
        print(f"  {k}: {preview}")
else:
    print("Result:", result)
