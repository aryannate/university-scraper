# save as scraper_service.py
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import re
from typing import List, Optional, Dict, Tuple
import logging
import json
from urllib.parse import urlparse

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
# CONFIG - preserve your Gemini key and CSE ID
# -------------------------------
GOOGLE_CSE_API = "https://www.googleapis.com/customsearch/v1"
GOOGLE_API_KEY = "AIzaSyD5Cqp1faiTxm9DqKGgNDIxqnn1vaTszH0"
GOOGLE_CX = "54f975bde5a684412"
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
    snippets: List[str]   # descriptive, program-specific and English-req snippets
    rawHTML: Optional[Dict] = None

# -------------------------------
# Heuristics and regex
# -------------------------------
# Looser capture: allow IELTS/TOEFL/PTE patterns with words like 'overall', 'minimum', 'each band'
ENGLISH_PATTERNS = [
    r"IELTS[^.\n]{0,80}\b(6(\.5)?|7(\.0)?)\b[^.\n]{0,120}(band|each|minimum)",
    r"TOEFL[^.\n]{0,80}\b(79|80|90|100)\b[^.\n]{0,140}(reading|listening|speaking|writing|section|minimum)",
    r"\bPTE\b[^.\n]{0,80}\b(58|60|61)\b[^.\n]{0,140}(communicative|skill|minimum|each)",
    r"English language requirement|English proficiency|minimum English",
]
ENGLISH_REGEX = re.compile("|".join(ENGLISH_PATTERNS), re.IGNORECASE)

# Numeric exam/work patterns (retained, but expanded to include PTE and words)
EXAM_REGEX = re.compile(
    r"\b(GRE|GMAT|TOEFL|IELTS|PTE|GATE|CAT|SAT|ACT)\b[^0-9]{0,40}(\d{1,3}(\.\d)?|\d{2,3}(-\d{2,3})?)",
    re.IGNORECASE
)
WORK_EXP_REGEX = re.compile(
    r"\b(work experience|professional experience|years of experience|internship requirement)\b[^.]{0,200}",
    re.IGNORECASE
)

# General keyword anchors for scraping relevant segments
GENERAL_KEYWORDS = [
    "English language", "entry requirements", "admission requirements", "eligibility",
    "prerequisite", "mathematics", "ATAR", "GPA", "tuition", "annual fees", "credit points",
    "duration", "intake", "application deadline"
]

# Used to bias URLs that look like official course pages
PREFERRED_PATH_HINTS = ["handbook", "study", "courses", "course", "program", "programs", "study-areas"]

# Optional mapping: tighten domain when known; otherwise the scraper falls back to full web
UNIVERSITY_DOMAINS = {
    "Monash University": "monash.edu",
    "University of Washington": "uw.edu",
    "UH Manoa": "manoa.hawaii.edu",
    # Add more universities optionally
}

# -------------------------------
# HTTP helpers
# -------------------------------
def http_get_json(url: str, params: dict, timeout: int = 20) -> dict:
    r = requests.get(url, params=params, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.json()

def http_get_text(url: str, timeout: int = 15) -> str:
    if url.lower().endswith(".pdf"):
        logger.info(f"Skipping PDF URL: {url}")
        return ""
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.text

# -------------------------------
# Google CSE search
# -------------------------------
def google_cse_search(queries: List[str], num: int = 5) -> List[str]:
    """Run up to len(queries) calls; de-duplicate URLs and return ranked by first-seen order."""
    seen = set()
    ranked = []
    for q in queries:
        logger.info(f"CSE query: {q}")
        params = {"q": q, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "num": num}
        try:
            data = http_get_json(GOOGLE_CSE_API, params=params)
            items = data.get("items", [])
            for it in items:
                link = it.get("link")
                if link and link not in seen:
                    seen.add(link)
                    ranked.append(link)
        except Exception as e:
            logger.warning(f"CSE error for '{q}': {e}")
    logger.info(f"CSE total unique URLs: {len(ranked)}")
    return ranked

# -------------------------------
# URL filtering and scoring
# -------------------------------
def is_same_domain(url: str, domain: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower()
        return domain in netloc
    except Exception:
        return False

def score_url(url: str, university_domain: Optional[str]) -> int:
    """Score URLs to prefer official course/handbook/study pages."""
    score = 0
    lower = url.lower()
    # Prefer official domain
    if university_domain and university_domain in lower:
        score += 5
    # Prefer handbook/study/course paths
    for hint in PREFERRED_PATH_HINTS:
        if f"/{hint}/" in lower or lower.endswith(f"/{hint}") or f".{hint}." in lower:
            score += 3
    # Penalize irrelevant paths
    for bad in ["login", "apply", "register", "contact", "privacy", "terms", "calendar"]:
        if f"/{bad}" in lower or bad in lower:
            score -= 3
    # Slightly penalize non-https or non-official domains
    if not lower.startswith("https://"):
        score -= 1
    return score

def filter_and_rank_urls(urls: List[str], university: str, university_domain: Optional[str], limit: int = 10) -> List[str]:
    uni_norm = university.lower().replace(" ", "")
    filtered = []
    for url in urls:
        lower = url.lower()
        if lower.endswith(".pdf"):
            continue
        # If a university domain is known, keep only that domain for course discovery
        if university_domain:
            if not is_same_domain(url, university_domain):
                continue
        else:
            # If domain unknown, at least ensure university name token appears somewhere (host or path)
            if uni_norm not in lower:
                # Allow if it appears to be an aggregator only for english requirements later; here we focus course page
                continue
        filtered.append(url)
    # Rank by heuristic score
    ranked = sorted(filtered, key=lambda u: score_url(u, university_domain), reverse=True)
    return ranked[:limit]

# -------------------------------
# HTML snippet extraction
# -------------------------------
def extract_snippets(html_text: str, max_snippets: int = 12) -> List[str]:
    soup = BeautifulSoup(html_text, "lxml")
    snippets: List[str] = []

    # Include headings, paragraphs, list items, table cells, and definition lists
    targets = soup.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'li', 'td', 'th', 'dt', 'dd'])
    for tag in targets:
        text = tag.get_text(separator=" ", strip=True)
        if not text:
            continue
        # Select if contains general anchors or English requirement signals or numeric exams/work
        if any(k.lower() in text.lower() for k in GENERAL_KEYWORDS) or ENGLISH_REGEX.search(text) or EXAM_REGEX.search(text) or WORK_EXP_REGEX.search(text):
            snippet = text
            # Try to add one sibling for context if short
            sib = tag.find_next_sibling()
            if sib:
                sib_text = sib.get_text(separator=" ", strip=True)
                if sib_text and len(sib_text) < 600:
                    snippet = snippet + " " + sib_text
            snippets.append(snippet)
        if len(snippets) >= max_snippets:
            break

    # Deduplicate and keep order
    unique = []
    seen = set()
    for s in snippets:
        if s not in seen:
            unique.append(s)
            seen.add(s)
    logger.info(f"Extracted {len(unique)} raw snippets")
    return unique[:max_snippets]

# -------------------------------
# English requirement normalization
# -------------------------------
def normalize_english_requirements(text_blocks: List[str]) -> List[str]:
    """
    Pull likely IELTS/TOEFL/PTE thresholds into standardized sentences when detected.
    Keeps original lines if already descriptive.
    """
    results: List[str] = []
    for t in text_blocks:
        t_norm = " ".join(t.split())
        if ENGLISH_REGEX.search(t_norm):
            results.append(t_norm)
            continue
        # Simple pattern-based normalization examples (fallbacks)
        if ("ielts" in t_norm.lower() and ("6.5" in t_norm or "7.0" in t_norm)) or ("toefl" in t_norm.lower() and any(s in t_norm for s in ["79", "80", "90", "100"])) or ("pte" in t_norm.lower() and any(s in t_norm for s in ["58", "60", "61"])):
            results.append(t_norm)
    # Deduplicate
    final = []
    seen = set()
    for s in results:
        if s not in seen:
            final.append(s)
            seen.add(s)
    return final

# -------------------------------
# Study level and keyword strategy
# -------------------------------
def detect_study_level(program: str) -> str:
    p = program.lower()
    if "master" in p or "msc" in p or "ms " in p or "m.s" in p:
        return "postgraduate"
    if "bachelor" in p or "btech" in p or "b.e" in p or "undergrad" in p:
        return "undergraduate"
    if "phd" in p or "doctor" in p or "d.phil" in p:
        return "research"
    return "unknown"

def build_queries(university: str, program: str, university_domain: Optional[str], year: Optional[int], max_results: int) -> Tuple[List[str], List[str]]:
    """
    Returns (course_discovery_queries, english_queries)
    Course discovery queries are tuned to find official course/handbook/study pages.
    English queries target centralized English requirements pages for the university.
    """
    level = detect_study_level(program)
    # For generality, avoid GRE/GMAT coupling for undergrad; allow for PG but not required in query
    # Focus on "handbook", "course", "entry requirements", "prerequisites"
    base_course_terms = [
        f"\"{program}\" site:{university_domain}" if university_domain else f"\"{program}\" \"{university}\"",
        f"{program} site:{university_domain} handbook" if university_domain else f"{program} {university} handbook",
        f"{program} site:{university_domain} course" if university_domain else f"{program} {university} course",
        f"{program} site:{university_domain} study" if university_domain else f"{program} {university} study",
        f"{program} site:{university_domain} \"entry requirements\"" if university_domain else f"{program} {university} \"entry requirements\"",
        f"{program} site:{university_domain} prerequisites" if university_domain else f"{program} {university} prerequisites",
    ]

    # Add generic CS/Engineering synonyms if program is very general
    p = program.lower()
    synonym_terms = []
    if "computer science" in p or "cs" in p:
        synonym_terms += [
            f"\"computer science\" site:{university_domain} handbook" if university_domain else f"\"computer science\" \"{university}\" handbook",
            f"bachelor computer science site:{university_domain}" if university_domain else f"bachelor computer science \"{university}\"",
            f"master computer science site:{university_domain}" if university_domain else f"master computer science \"{university}\"",
        ]
    if "biomedical" in p and "engineer" in p:
        synonym_terms += [
            f"biomedical engineering site:{university_domain} handbook" if university_domain else f"biomedical engineering \"{university}\" handbook",
            f"bachelor biomedical engineering site:{university_domain}" if university_domain else f"bachelor biomedical engineering \"{university}\"",
            f"master biomedical engineering site:{university_domain}" if university_domain else f"master biomedical engineering \"{university}\"",
        ]

    course_queries = base_course_terms + synonym_terms

    # English queries: centralized pages; include undergraduate/postgraduate tokens but do not overconstrain
    english_terms = [
        f"site:{university_domain} \"English language requirements\"" if university_domain else f"\"English language requirements\" \"{university}\"",
        f"site:{university_domain} English proficiency admission" if university_domain else f"English proficiency admission \"{university}\"",
        f"site:{university_domain} IELTS TOEFL PTE undergraduate" if university_domain else f"IELTS TOEFL PTE undergraduate \"{university}\"",
        f"site:{university_domain} IELTS TOEFL PTE postgraduate" if university_domain else f"IELTS TOEFL PTE postgraduate \"{university}\"",
    ]

    # Limit queries to avoid overuse
    course_queries = [q for q in course_queries if q][:max_results]
    english_queries = [q for q in english_terms if q][:max_results]

    return course_queries, english_queries

# -------------------------------
# Scrape pipeline
# -------------------------------
def discover_course_pages(university: str, program: str, university_domain: Optional[str], year: Optional[int], max_results: int) -> List[str]:
    course_queries, _ = build_queries(university, program, university_domain, year, max_results)
    urls = google_cse_search(course_queries, num=max_results)
    ranked = filter_and_rank_urls(urls, university, university_domain, limit=max_results)
    return ranked

def discover_english_pages(university: str, university_domain: Optional[str], max_results: int) -> List[str]:
    _, english_queries = build_queries(university, "", university_domain, None, max_results)
    urls = google_cse_search(english_queries, num=max_results)
    # Here, allow only official domain for English requirement pages to avoid aggregator noise when possible
    filtered = []
    for u in urls:
        if university_domain:
            if is_same_domain(u, university_domain):
                filtered.append(u)
        else:
            # If domain not mapped, allow all; normalization will attempt to detect English thresholds
            filtered.append(u)
    # Simple dedupe and truncate
    seen = set()
    uniq = []
    for u in filtered:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:max_results]

def parse_and_collect(urls: List[str]) -> Dict[str, List[str]]:
    """Fetch each URL and extract snippets."""
    collected: Dict[str, List[str]] = {}
    for url in urls:
        try:
            html = http_get_text(url)
            if not html:
                continue
            snippets = extract_snippets(html, max_snippets=12)
            if snippets:
                collected[url] = snippets
        except Exception as e:
            logger.warning(f"Failed to fetch/parse {url}: {e}")
    return collected

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

    university_domain = UNIVERSITY_DOMAINS.get(req_data.university, None)

    # Phase A: discover likely official course/handbook/study pages from generic program
    course_urls = discover_course_pages(req_data.university, req_data.program, university_domain, req_data.year, req_data.max_results)

    # Phase B: discover centralized English requirement pages for this university
    english_urls = discover_english_pages(req_data.university, university_domain, req_data.max_results)

    # Fetch and parse both sets
    course_sources = parse_and_collect(course_urls)
    english_sources = parse_and_collect(english_urls)

    # Normalize English lines across all english sources
    english_snippets = []
    for url, blocks in english_sources.items():
        normalized = normalize_english_requirements(blocks)
        # if normalization finds nothing, keep a few original lines that appear relevant
        use_blocks = normalized if normalized else [b for b in blocks if ENGLISH_REGEX.search(b)]
        if use_blocks:
            english_snippets.extend(use_blocks)

    # Merge results
    source_map = {}
    source_map.update(course_sources)
    # Store English separately; if same URL exists, extend
    for url, blocks in english_sources.items():
        if url in source_map:
            # Merge unique
            existing = source_map[url]
            add = [b for b in blocks if b not in existing]
            source_map[url] = existing + add
        else:
            source_map[url] = blocks

    # Build final snippet list: prioritize course snippets, then English normalized snippets
    final_snippets: List[str] = []
    for url in course_urls:
        if url in course_sources:
            final_snippets.extend(course_sources[url])
    final_snippets.extend(english_snippets)

    result = {
        "dataFound": bool(final_snippets),
        "sourceURLs": list(source_map.keys()),
        "snippets": final_snippets[: max(10, req_data.max_results * 3)],
        "rawHTML": None
    }

    logger.info(f"Returning result for {req_data.university}|{req_data.program}, dataFound={result['dataFound']}")
    return result
