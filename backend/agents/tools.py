import re
import requests
from bs4 import BeautifulSoup


def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def get_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }


def search_web(query: str, num_results: int = 5) -> str:
    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
        r = requests.get(url, params=params, timeout=10, headers=get_headers())
        data = r.json()
        results = []
        if data.get("AbstractText"):
            results.append(f"Summary: {data['AbstractText']}")
            if data.get("AbstractURL"):
                results.append(f"Source: {data['AbstractURL']}")
        for topic in data.get("RelatedTopics", [])[:num_results]:
            if isinstance(topic, dict) and topic.get("Text"):
                results.append(f"- {topic['Text']}")
                if topic.get("FirstURL"):
                    results.append(f"  URL: {topic['FirstURL']}")
        return "\n".join(results) if results else f"No results for: {query}"
    except Exception as e:
        return f"Search error: {str(e)}"


def scrape_website(url: str, max_chars: int = 6000) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = requests.get(url, timeout=15, headers=get_headers(), allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
            tag.decompose()
        sections = []
        for sel in ["main", "article", "[class*='about']", "[class*='company']", "[class*='hero']"]:
            s = soup.select_one(sel)
            if s:
                t = clean_text(s.get_text())
                if len(t) > 100:
                    sections.append(t)
        text = " ".join(sections) or clean_text(soup.get_text())
        return (text[:max_chars] + "...[truncated]") if len(text) > max_chars else text
    except Exception as e:
        return f"Scrape error for {url}: {str(e)}"


def find_company_website(company_name: str) -> str:
    try:
        url = "https://api.duckduckgo.com/"
        params = {"q": f"{company_name} official website", "format": "json", "no_html": "1"}
        r = requests.get(url, params=params, timeout=10, headers=get_headers())
        data = r.json()
        if data.get("AbstractURL"):
            return data["AbstractURL"]
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("FirstURL"):
                return topic["FirstURL"]
        return f"Could not find website for {company_name}"
    except Exception as e:
        return f"Error: {str(e)}"


def get_linkedin_info(company_name: str) -> str:
    return search_web(f"site:linkedin.com/company {company_name}", 3)


GROQ_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the internet for company info, news, funding, or any research topic. Use specific queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Specific search query"},
                    "num_results": {"type": "integer", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scrape_website",
            "description": "Fetch and read full text content of a URL. Use for homepages, About pages, pricing pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including https://"},
                    "max_chars": {"type": "integer", "default": 6000}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_company_website",
            "description": "Find the official website URL for a company by name. Call this first before scraping.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string"}
                },
                "required": ["company_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_linkedin_info",
            "description": "Search for company LinkedIn profile — employee count, industry, key people.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string"}
                },
                "required": ["company_name"]
            }
        }
    }
]


def execute_tool(tool_name: str, tool_input: dict) -> str:
    tool_map = {
        "search_web":           lambda i: search_web(i["query"], i.get("num_results", 5)),
        "scrape_website":       lambda i: scrape_website(i["url"], i.get("max_chars", 6000)),
        "find_company_website": lambda i: find_company_website(i["company_name"]),
        "get_linkedin_info":    lambda i: get_linkedin_info(i["company_name"]),
    }
    handler = tool_map.get(tool_name)
    if not handler:
        return f"Unknown tool: {tool_name}"
    try:
        return handler(tool_input)
    except Exception as e:
        return f"Tool error ({tool_name}): {str(e)}"
