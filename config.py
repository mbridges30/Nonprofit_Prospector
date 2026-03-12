"""
Configuration for the Flask web UI.
"""

import os

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-prospect-explorer-key")
API_DELAY = float(os.environ.get("API_DELAY", "0.5"))
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "1") != "0"
PORT = int(os.environ.get("PORT", "5000"))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
