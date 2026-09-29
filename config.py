"""Configuration for the standalone SpankBang API."""
import os
from dotenv import load_dotenv

load_dotenv()

PORT = int(os.getenv("PORT", "8000"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
