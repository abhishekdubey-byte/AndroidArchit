import os
import uvicorn
from app.logsetup import LOG_CONFIG
from app.config import SERVER_HOST, SERVER_PORT

if __name__ == "__main__":
    # Auto-reload restarts the server whenever code changes (development only): ACC_RELOAD=1
    uvicorn.run("app.main:app", host=SERVER_HOST, port=SERVER_PORT, log_config=LOG_CONFIG, ws_max_size=64 * 1024 * 1024,  # full-resolution HUNT photos
                reload=os.getenv("ACC_RELOAD") == "1")
