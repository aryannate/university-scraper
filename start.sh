#!/bin/bash
uvicorn scraper_service:app --host 0.0.0.0 --port $PORT --reload
