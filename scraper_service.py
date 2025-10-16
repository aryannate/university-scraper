# save as scraper_service.py
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import re
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
GOOGLE_CX = "54f975bde5a684412"     # replace with your search engine ID
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
    snippets: List[str]   # descriptive, program-specific snippets
    rawHTML: Optional[Dict] = None

# -------------------------------
# Regex patterns for numeric scores
# -------------------------------
EXAM_REGEX = re.compile(
    r"\b(GRE|GMAT|TOEFL|IELTS|GATE|CAT|SAT|ACT)\b[^0-9]{0,20}(\d{2,3}(-\d{2,3})?)", re.IGNORECASE
)
WORK_EXP_REGEX = re.compile(
    r"\b(work experience|professional experience|years of experience|internship requirement)\b[^.]{0,200}", re.IGNORECASE
)
KEYWORDS = ["GRE", "GMAT", "TOEFL", "IELTS", "GATE", "CAT", "SAT", "ACT", 
            "English language", "admission requirement", "admissions requirements",
            "work experience", "professional experience", "internship"]

# -------------------------------
# Google CSE search
# -------------------------------
def google_cse_search(query: str, num: int = 5) -> List[str]:
    logger.info(f"Performing Google CSE search: {query}")
    params = {"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "num": num}
    r = requests.get(GOOGLE_CSE_API, params=params, timeout=15, headers=headers)
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
    if url.lower().endswith(".pdf"):
        logger.info(f"Skipping PDF URL: {url}")
        return ""
    logger.info(f"Fetching URL: {url}")
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.text

# -------------------------------
# Extract descriptive snippets
# -------------------------------
def extract_snippets(html_text: str, max_snippets: int = 10) -> List[str]:
    soup = BeautifulSoup(html_text, "lxml")
    snippets = []

    for tag in soup.find_all(['h1','h2','h3','h4','p','li','td']):
        text = tag.get_text(separator=" ", strip=True)
        if not text:
            continue
        if any(k.lower() in text.lower() for k in KEYWORDS):
            # capture the line itself
            snippet = text
            # include next sibling for context
            sib = tag.find_next_sibling()
            if sib:
                sib_text = sib.get_text(separator=" ", strip=True)
                if sib_text and len(sib_text) < 1000:
                    snippet += " " + sib_text
            snippets.append(snippet)

        if len(snippets) >= max_snippets:
            break

    # Extract numeric exam scores
    numeric_snippets = []
    for snip in snippets:
        exams = EXAM_REGEX.findall(snip)
        work_exp = WORK_EXP_REGEX.findall(snip)
        if exams or work_exp:
            numeric_snippets.append(snip)

    # deduplicate
    unique_snippets = []
    for s in numeric_snippets:
        if s not in unique_snippets:
            unique_snippets.append(s)
    logger.info(f"Extracted {len(unique_snippets)} descriptive snippets")
    return unique_snippets[:max_snippets]

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

    query = f"site:.edu \"{req_data.university}\" \"{req_data.program}\" admissions GRE GMAT TOEFL IELTS"
    try:
        urls = google_cse_search(query, num=req_data.max_results)
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=f"Search error: {e}")

    # Keep only relevant educational pages, skip PDFs
    def keep(url: str) -> bool:
        lower = url.lower()
        if any(bad in lower for bad in ["login", "apply", "register", "contact"]):
            return False
        if lower.endswith(".pdf"):
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

    logger.info(f"Returning result for {req_data.university}|{req_data.program}, dataFound={result['dataFound']}")
    return result
