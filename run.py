"""Start the Jobbunt application."""
import os
import sys

# Ensure data directory exists
os.makedirs(os.path.join(os.path.dirname(__file__), "data", "uploads"), exist_ok=True)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=port,
        reload=os.environ.get("ENV", "dev") != "production",
    )
