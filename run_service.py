"""Start the EdSider check API with auto-reload.

Run with:  venv\\Scripts\\python run_service.py

Same as typing the full uvicorn command, but saves retyping it. Reload is
on for local work; the Azure startup script (startup.sh) runs the service
without it.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("service:app", host="0.0.0.0", port=8000, reload=True)
