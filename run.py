"""Start the Jobbunt application."""
import os
import sys

# Ensure data directory exists
os.makedirs(os.path.join(os.path.dirname(__file__), "data", "uploads"), exist_ok=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
