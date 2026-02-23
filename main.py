# ------------------------------------------------------------------
# [운영용 v4.3.1] FAST Google News RSS -> Slack Digest (1-day + Nexon precision + non-article URL filter)
# - Python 3.9 호환
# - 추가: "기사 아닌 링크" URL 패턴 필터(초경량)
#   * inven.co.kr/board/ 제외
#   * inven.co.kr/webzine/news/?keyword= 등 news id 없는 리스트 페이지 제외
# ------------------------------------------------------------------
import os
import re
import json
import time
import hashlib
import random
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse, parse_qs

import requests
import feedparser

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

TARGET_SITES = [
    "inven.co.kr",
    "gamemeca.com",
    "thisisgame.com",
    "gametoc.co.kr",
    "gameple.co.kr",
    "zdnet.co.kr",
    "ddaily.co.kr",
]

PRIMARY_KEYWORDS = [
    "신작", "성과", "호재", "악재", "리스크", "정책", "업데이트", "출시",
    "매출", "순위", "소송", "규제", "CBT", "OBT", "인수", "투자", "M&A"
]

SEARCH_DAYS = 1

KEYWORD_BATCH_PRIMARY = 10
KEYWORD_BATCH_FALLBACK = 18
MAX_ENTRIES_PER_FEED = 30
MAX_ENTRIES_PER_NEXON_FEED = 20

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (FastNewsDigestBot/1.31; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.12)

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 15

# --------------------------
# 컨텍스트/필터
# --------------------------
GAME_CONTEXT_OR = [
    "게임", "게이밍", "게임업계", "게임사", "퍼블리셔", "개발사",
    "모바일게임", "PC게임", "콘솔", "스팀", "Steam", "PS5", "플레이스테이션", "닌텐도", "Xbox",
    "RPG", "MMORPG", "FPS", "MOBA", "e스포츠", "esports"
]
GAME_CONTEXT_QUERY = "(" + " OR ".join(GAME_CONTEXT_OR) + ")"

STRICT_SITES = {"zdnet.co.kr", "ddaily.co.kr"}

GAME_HINTS = [
    "게임", "게이밍", "신작", "업데이트", "출시", "스팀", "콘솔", "모바일", "PC",
    "플레이스테이션", "닌텐도", "Xbox", "RPG", "MMORPG", "FPS", "MOBA",
    "e스포츠", "esports",
    "넥슨", "엔씨", "크래프톤", "넷마블", "카카오게임", "스마일게이트", "펄어비스",
]

NEXON_TERMS = [
    "넥슨", "nexon",
    "넥슨코리아", "넥슨게임즈", "넥슨 네트웍스", "넥슨네트웍스",
    "네오플", "넥슨GT", "넥슨지티",
]

NEXON_IMPORTANCE = [
    ("M&A", 5), ("인수", 5), ("합병", 5),
    ("투자", 4), ("지분", 4),
    ("소송", 5), ("규제", 4),
    ("매출", 4), ("실적", 4), ("영업이익", 4), ("순이익", 4),
    ("출시", 3), ("업데이트", 3),
    ("CBT", 2), ("OBT", 2),
    ("리스크", 3), ("악재", 3), ("호재", 3),
]

# --------------------------
# URL 유효성(기사 여부) 필터: "초경량"
# --------------------------
def is_valid_article_url(url: str) -> bool:
    """
    '기사'가 아닌 페이지(게시판, 검색/리스트, 태그, 랭킹 등)를 URL 패턴으로 제거.
    - 무거운 본문 크롤링 없이 처리(운영 속도 유지).
    """
    if not url:
        return False

    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        qs = parse_qs(p.query or "")
    except Exception:
        return True  # 파싱 실패는 과하게 버리지 않기 위해 통과

    # 공통: 보드/포럼류 경로 제거
    # (추가 사이트가 생기면 여기에 계속 얹으면 됨)
    common_bad_path_tokens = [
        "/board/",        # 인벤 게시판
        "/search",        # 검색 페이지
        "/tag/",          # 태그 목록
        "/rank",          # 랭킹
        "/ranking",
        "/gallery",       # 갤러리(기사와 무관할 가능성 높음)
    ]
    if any(tok in path for tok in common_bad_path_tokens):
        return False

    # Inven 특화
    if host.endswith("inven.co.kr"):
        # 게시판은 무조건 제외
        if "/board/" in path:
            return False

        # webzine/news 는 "news=기사ID"가 있어야 기사로 간주
        if path.startswith("/webzine/news/") or path == "/webzine/news":
            if "news" not in qs:
                # keyword= 같은 리스트 페이지 차단
                return False

        # 그 외 인벤 도메인에서 기사로 보기 애매한 루트들도 제외(필요시 확장)
        if path in ("/", "/webzine", "/webzine/"):
            return False

    return True

# --------------------------
# 유틸
# --------------------------
def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"

def _sleep():
    time.sleep(random.uniform(*SLEEP_BETWEEN_FEEDS))

def _stable_id(title: str, link: str) -> str:
    return hashlib.sha1((title + "||" + link).encode("utf-8")).hexdigest()[:16]

def _parse_published(entry) -> Optional[datetime]:
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    try:
        return datetime(*t[:6])
    except Exception:
        return None

def _within_days(dt: Optional[datetime], days: int) -> bool:
    if not dt:
        return True
    return dt >= (datetime.now() - timedelta(days=days))

def _press_guess(entry) -> str:
    try:
        src = getattr(entry, "source", None)
        if src and isinstance(src, dict):
            t = _clean_text(src.get("title", ""))
            if t:
                return t
    except Exception:
        pass
    return "NEWS"

def _google_news_rss_search_url(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"

def _site_or_query(sites: List[str]) -> str:
    return "(" + " OR ".join([f"site:{s}" for s in sites]) + ")"

def _has_any_hint(text: str, hints: List[str]) -> bool:
    blob = (text or "").lower()
    return any(h.lower() in blob for h in hints)

def contains_nexon(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(t.lower() in blob for t in NEXON_TERMS)

def nexon_score(article: Dict) -> int:
    blob = f"{article.get('title','')} {article.get('snippet','')}".lower()
    score = 0
    for kw, w in NEXON_IMPORTANCE:
        if kw.lower() in blob:
            score += w
    if contains_nexon(article.get("title", ""), article.get("snippet", "")):
        score += 2
    return score

# --------------------------
# 쿼리
# --------------------------
def build_query_general(keyword: str, sites: List[str], days: int) -> str:
    if sites:
        return f"{GAME_CONTEXT_QUERY} {keyword} {_site_or_query(sites)} when:{days}d"
    return f"{GAME_CONTEXT_QUERY} {keyword} when:{days}d"

def build_query_nexon(keyword: str, sites: List[str], days: int) -> str:
    nexon_expr = '("넥슨" OR Nexon OR "넥슨게임즈" OR 네오플)'
    if sites:
        return f'{nexon_expr} {keyword} {_site_or_query(sites)} when:{days}d'
    return f'{nexon_expr} {keyword} when:{days}d'

# --------------------------
# RSS 수집
# --------------------------
def fetch_general(keywords: List[str], sites: List[str], days: int) -> Tuple[List[Dict], Dict[str, int]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats = {
        "feeds_called": 0,
        "entries_seen": 0,
        "date_filtered_out": 0,
        "strict_filtered_out": 0,
        "non_article_url_filtered_out": 0,
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    for kw in keywords:
        q = build_query_general(kw, sites, days)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            for e in feed.entries[:MAX_ENTRIES_PER_FEED]:
                stats["entries_seen"] += 1

                title = _clean_text(getattr(e, "title", ""))
                link = _clean_text(getattr(e, "link", ""))
                if not title or not link:
                    continue

                # ✅ 기사 URL 패턴 필터
                if not is_valid_article_url(link):
                    stats["non_article_url_filtered_out"] += 1
                    continue

                published_dt = _parse_published(e)
                if not _within_days(published_dt, days):
                    stats["date_filtered_out"] += 1
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _truncate(_clean_text(_strip_html(snippet_raw)), SNIPPET_MAX)

                # 일반 트랙: zdnet/ddaily는 게임 힌트가 없으면 제거
                if any(s in link for s in STRICT_SITES) or any(s in title for s in ("지디넷", "디지털데일리")):
                    if not _has_any_hint(f"{title} {snippet}", GAME_HINTS):
                        stats["strict_filtered_out"] += 1
                        continue

                sid = _stable_id(title, link)
                if sid in articles:
                    continue

                articles[sid] = {
                    "track": "general",
                    "keyword": kw,
                    "press": _press_guess(e),
                    "title": _truncate(title, TITLE_MAX),
                    "link": link,
                    "published_dt": published_dt,
                    "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                    "snippet": snippet,
                }
                stats["added"] += 1

            _sleep()
        except Exception as ex:
            print(f"[WARN] RSS call failed (general kw={kw}): {ex}")
            continue

    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats

def fetch_nexon(keywords: List[str], sites: List[str], days: int) -> Tuple[List[Dict], Dict[str, int]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats = {
        "feeds_called": 0,
        "entries_seen": 0,
        "date_filtered_out": 0,
        "nexon_filtered_out": 0,
        "non_article_url_filtered_out": 0,
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    for kw in keywords:
        q = build_query_nexon(kw, sites, days)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            for e in feed.entries[:MAX_ENTRIES_PER_NEXON_FEED]:
                stats["entries_seen"] += 1

                title = _clean_text(getattr(e, "title", ""))
                link = _clean_text(getattr(e, "link", ""))
                if not title or not link:
                    continue

                # ✅ 기사 URL 패턴 필터
                if not is_valid_article_url(link):
                    stats["non_article_url_filtered_out"] += 1
                    continue

                published_dt = _parse_published(e)
                if not _within_days(published_dt, days):
                    stats["date_filtered_out"] += 1
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _truncate(_clean_text(_strip_html(snippet_raw)), SNIPPET_MAX)

                # ✅ 넥슨 최종 검증(제목/요약에 실제 넥슨 포함)
                if not contains_nexon(title, snippet):
                    stats["nexon_filtered_out"] += 1
                    continue

                sid = _stable_id(title, link)
                if sid in articles:
                    continue

                articles[sid] = {
                    "track": "nexon",
                    "keyword": kw,
                    "press": _press_guess(e),
                    "title": _truncate(title, TITLE_MAX),
                    "link": link,
                    "published_dt": published_dt,
                    "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                    "snippet": snippet,
                }
                stats["added"] += 1

            _sleep()
        except Exception as ex:
            print(f"[WARN] RSS call failed (nexon kw={kw}): {ex}")
            continue

    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats

# --------------------------
# Slack 메시지
# --------------------------
def build_messages(general: List[Dict], nexon: List[Dict],
                   stats_g: Dict[str, int], stats_n: Dict[str, int], days: int) -> List[str]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    header = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {days}일)\n"
    header += f"- general: feeds={stats_g.get('feeds_called',0)}, entries={stats_g.get('entries_seen',0)}, added={stats_g.get('added',0)}, strict_drop={stats_g.get('strict_filtered_out',0)}, non_article_drop={stats_g.get('non_article_url_filtered_out',0)}\n"
    header += f"- nexon: feeds={stats_n.get('feeds_called',0)}, entries={stats_n.get('entries_seen',0)}, added={stats_n.get('added',0)}, nexon_drop={stats_n.get('nexon_filtered_out',0)}, non_article_drop={stats_n.get('non_article_url_filtered_out',0)}\n\n"

    def fmt(a: Dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        sn = f"\n    - {a['snippet']}" if a.get("snippet") else ""
        return f"▶ *[{a.get('press','NEWS')}]* <{a['link']}|{a['title']}>{pub}{sn}\n"

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not general:
        body += "- 오늘 기준 수집된 뉴스가 없습니다.\n"
    else:
        for a in general[:70]:
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스 (Top 5)\n"
    if not nexon:
        body += "- 넥슨 관련 뉴스(키워드 교집합 + 제목/요약 검증)를 찾지 못했습니다.\n"
    else:
        scored = sorted(nexon, key=lambda x: (nexon_score(x), x["published_dt"] or datetime.min), reverse=True)
        for a in scored[:5]:
            body += fmt(a)

    full = header + body

    messages: List[str] = []
    chunk = ""
    for line in full.splitlines(True):
        if len(chunk) + len(line) > SLACK_TEXT_LIMIT:
            messages.append(chunk)
            chunk = ""
        chunk += line
    if chunk.strip():
        messages.append(chunk)

    return messages

def send_to_slack_text(message: str) -> None:
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("환경변수 SLACK_WEBHOOK_URL이 설정되어 있지 않습니다.")

    payload = {"text": message}
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()

# --------------------------
# Main
# --------------------------
def main() -> None:
    general, stats_g = fetch_general(PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY], TARGET_SITES, SEARCH_DAYS)
    if not general:
        general, stats_g = fetch_general(PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK], TARGET_SITES, SEARCH_DAYS)

    nexon, stats_n = fetch_nexon(PRIMARY_KEYWORDS, TARGET_SITES, SEARCH_DAYS)

    print(f"[INFO] general fetched: {len(general)}, stats: {stats_g}")
    print(f"[INFO] nexon fetched: {len(nexon)}, stats: {stats_n}")
    print("[INFO] preview general:")
    for i, a in enumerate(general[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','NEWS')}] {a.get('title','')} :: {a.get('link','')}")
    print("[INFO] preview nexon:")
    for i, a in enumerate(nexon[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','NEWS')}] {a.get('title','')} :: {a.get('link','')}")

    messages = build_messages(general, nexon, stats_g, stats_n, SEARCH_DAYS)
    for i, msg in enumerate(messages, 1):
        send_to_slack_text(msg)
        print(f"[INFO] sent slack message {i}/{len(messages)}")
        time.sleep(0.15)

if __name__ == "__main__":
    main()
