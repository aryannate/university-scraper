#!/bin/bash

# Install Playwright browser binaries (needed for browser fallback)
playwright install

# Now launch your API server
uvicorn scraper_service:app --host 0.0.0.0 --port $PORT 
