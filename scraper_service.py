# save as scraper_service.py
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import re
import time
from typing import List, Optional, Dict
import logging
import json

# -------------------------------
# Logging setup
# -------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# -------------------------------
# FastAPI app
# -------------------------------
app = FastAPI()

# -------------------------------
# CONFIG - replace placeholders
# -------------------------------
GOOGLE_CSE_API = "https://www.googleapis.com/customsearch/v1"
GOOGLE_API_KEY = "AIzaSyD5Cqp1faiTxm9DqKGgNDIxqnn1vaTszH0"  # replace with your key
GOOGLE_CX = "54f975bde5a684412"           # replace with your search engine ID
USER_AGENT = "Mozilla/5.0 (compatible; UniversityScraper/1.0)"
headers = {"User-Agent": USER_AGENT}

# -------------------------------
# Request and Response Models
# -------------------------------
class ScrapeRequest(BaseModel):
    university: str
    program: str
    year: Optional[int] = 2025
    max_results: Optional[int] = 5

class ScrapeResponse(BaseModel):
    dataFound: bool
    sourceURLs: List[str]
    snippets: List[str]   # cleaned text snippets (GRE/GMAT/IELTS passages)
    rawHTML: Optional[Dict] = None

# -------------------------------
# Simple in-memory cache
# -------------------------------
CACHE_TTL = 60 * 60 * 24  # 24 hours
_cache = {}

def cache_get(key: str):
    record = _cache.get(key)
    if not record: 
        return None
    ts, val = record
    if time.time() - ts > CACHE_TTL:
        del _cache[key]
        return None
    return val

def cache_set(key: str, val: dict):
    _cache[key] = (time.time(), val)

# -------------------------------
# Google CSE search
# -------------------------------
def google_cse_search(query: str, num: int = 5) -> List[str]:
    logger.info(f"Performing Google CSE search: {query}")
    params = {"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "num": num}
    r = requests.get(GOOGLE_CSE_API, params=params, timeout=10, headers=headers)
    r.raise_for_status()
    data = r.json()
    items = data.get("items", [])
    urls = [it.get("link") for it in items if it.get("link")]
    logger.info(f"Found URLs: {urls}")
    return urls

# -------------------------------
# Fetch webpage
# -------------------------------
def fetch_url(url: str, timeout: int = 10) -> str:
    logger.info(f"Fetching URL: {url}")
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.text

# -------------------------------
# Snippet extraction
# -------------------------------
KEYWORDS = ["GRE", "GMAT", "TOEFL", "IELTS", "English language", "admission requirement", "admissions requirements"]

def extract_snippets(html_text: str, max_snippets: int = 6) -> List[str]:
    soup = BeautifulSoup(html_text, "lxml")
    texts = []

    for tag in soup.find_all(['h1','h2','h3','h4','p','li','td']):
        t = tag.get_text(separator=" ", strip=True)
        if not t:
            continue
        if any(k.lower() in t.lower() for k in KEYWORDS):
            # keep current tag
            texts.append(t)
            # add next sibling for context
            sib = tag.find_next_sibling()
            if sib:
                s = sib.get_text(separator=" ", strip=True)
                if s and len(s) < 1000:
                    texts.append(s)
        if len(texts) >= max_snippets:
            break

    # deduplicate
    unique = []
    for s in texts:
        if s not in unique:
            unique.append(s)
    logger.info(f"Extracted {len(unique)} snippets")
    return unique[:max_snippets]

# Regex for numeric scores
NUM_RE = re.compile(r"\b(GRE|GMAT|TOEFL|IELTS)\b[^0-9]{0,20}(\d{2,3}(-\d{2,3})?)", re.IGNORECASE)

# -------------------------------
# Scrape endpoint
# -------------------------------
@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(req: Request):
    raw_body = await req.body()
    raw_text = raw_body.decode("utf-8").strip()

    # Handle n8n leading = sign
    if raw_text.startswith('='):
        raw_text = raw_text[1:]

    try:
        data_dict = json.loads(raw_text)
        req_data = ScrapeRequest(**data_dict)
    except Exception as e:
        logger.error(f"Failed to parse request JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON format")

    logger.info(f"Received request: {req_data.json()}")

    cache_key = f"{req_data.university}|{req_data.program}"
    cached = cache_get(cache_key)
    if cached:
        logger.info(f"Returning cached result for {cache_key}")
        return cached

    query = f"site:.edu \"{req_data.university}\" \"{req_data.program}\" admissions GRE GMAT TOEFL IELTS"
    try:
        urls = google_cse_search(query, num=req_data.max_results)
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=f"Search error: {e}")

    # -------------------------------
    # Filter URLs to keep only relevant educational pages
    # -------------------------------
    def keep(url: str) -> bool:
        lower = url.lower()
        if any(bad in lower for bad in ["login", "apply", "register", "contact"]):
            return False
        return any(domain in lower for domain in [".edu", ".ac.", ".edu.au", ".ac.uk", ".edu.ca"])

    urls = [u for u in urls if u and keep(u)]
    logger.info(f"Filtered URLs: {urls}")

    snippets = []
    source_map = {}
    for url in urls:
        try:
            html = fetch_url(url)
            extracted = extract_snippets(html)
            if extracted:
                snippets.extend(extracted)
                source_map[url] = extracted
        except Exception as e:
            logger.warning(f"Failed to fetch or parse {url}: {e}")
            continue

    result = {
        "dataFound": bool(snippets),
        "sourceURLs": list(source_map.keys()),
        "snippets": snippets,
        "rawHTML": None
    }

    cache_set(cache_key, result)
    logger.info(f"Returning result for {cache_key}, dataFound={result['dataFound']}")
    return result
