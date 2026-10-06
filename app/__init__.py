# Pete AI Package
__version__ = "1.0.0"

# Configure the console before any module can print to it. Imported here (not in
# main.py) because the app is also started via `uvicorn app.api:app` and by the
# test scripts, and those paths must get the same protection. See app/console.py
# for why an unencodable log line was able to mask a real browser error.
from app.console import configure_stdio

configure_stdio()
