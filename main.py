# ------------------------------------------------------------------
# [운영용 v4.2] FAST Google News RSS -> Slack Digest (1-day + Nexon dedicated track)
# - Python 3.9 호환
# - 변경점:
#   1) SEARCH_DAYS=1 (하루치)
#   2) 넥슨 전용 트랙: Nexon 관련 쿼리를 별도로 수행하여 중요 기사 누락 방지
#   3) 넥슨 트랙은 필터를 완화하고 결과 상한을 낮춰 "정확도+커버리지" 균형
# ------------------------------------------------------------------
import os
import re
import json
import time
import hashlib
import random
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import quote

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

# ✅ 하루치만
SEARCH_DAYS = 1

KEYWORD_BATCH_PRIMARY = 10
KEYWORD_BATCH_FALLBACK = 18
MAX_ENTRIES_PER_FEED = 30

# 넥슨 전용 트랙은 호출 수/결과 수를 제한 (속도/품질)
NEXON_QUERIES = [
    "넥슨",
    "Nexon",
    # 필요하면 아래처럼 게임/이슈를 추가해도 좋음(너가 실제로 중요하게 보는 축)
    # "메이플스토리", "던전앤파이터", "FC 온라인", "블루 아카이브"
]
MAX_ENTRIES_PER_NEXON_FEED = 25  # 넥슨 전용은 충분히

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (FastNewsDigestBot/1.2; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.15)

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 20

# --------------------------
# 게임 컨텍스트 (일반 트랙 노이즈 억제)
# --------------------------
GAME_CONTEXT_OR = [
    "게임", "게이밍", "게임업계", "게임사", "퍼블리셔", "개발사",
    "모바일게임", "PC게임", "콘솔", "스팀", "Steam", "PS5", "플레이스테이션", "닌텐도", "Xbox",
    "RPG", "MMORPG", "FPS", "MOBA",
]
GAME_CONTEXT_QUERY = "(" + " OR ".join(GAME_CONTEXT_OR) + ")"

STRICT_SITES = {"zdnet.co.kr", "ddaily.co.kr"}

GAME_HINTS = [
    "게임", "게이밍", "신작", "업데이트", "출시", "스팀", "콘솔", "모바일", "PC",
    "플레이스테이션", "닌텐도", "Xbox", "RPG", "MMORPG", "FPS", "MOBA", "e스포츠", "esports",
    "넥슨", "엔씨", "크래프톤", "넷마블", "카카오게임", "스마일게이트", "펄어비스",
]

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

def _build_query_general(keyword: str, sites: List[str], days: int) -> str:
    # 일반 트랙: 게임 컨텍스트 + 키워드 + 사이트 + when
    if sites:
        return f"{GAME_CONTEXT_QUERY} {keyword} {_site_or_query(sites)} when:{days}d"
    return f"{GAME_CONTEXT_QUERY} {keyword} when:{days}d"

def _build_query_nexon(nexon_term: str, sites: List[str], days: int) -> str:
    # 넥슨 트랙: 넥슨 자체가 강한 시그널이므로 게임 컨텍스트를 강제하지 않음(누락 방지)
    # 대신 사이트 제한은 유지
    if sites:
        return f'{nexon_term} {_site_or_query(sites)} when:{days}d'
    return f'{nexon_term} when:{days}d'

def _has_game_hint(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    for h in GAME_HINTS:
        if h.lower() in blob:
            return True
    return False

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
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    for kw in keywords:
        q = _build_query_general(kw, sites, days)
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

                published_dt = _parse_published(e)
                if not _within_days(published_dt, days):
                    stats["date_filtered_out"] += 1
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _clean_text(_strip_html(snippet_raw))
                snippet = _truncate(snippet, SNIPPET_MAX)

                # zdnet/ddaily 추가 엄격 필터 (일반 트랙만 적용)
                if any(s in link for s in STRICT_SITES) or any(s in title for s in ("지디넷", "디지털데일리")):
                    if not _has_game_hint(title, snippet):
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

def fetch_nexon(sites: List[str], days: int) -> Tuple[List[Dict], Dict[str, int]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats = {
        "feeds_called": 0,
        "entries_seen": 0,
        "date_filtered_out": 0,
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    for term in NEXON_QUERIES:
        q = _build_query_nexon(term, sites, days)
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

                published_dt = _parse_published(e)
                if not _within_days(published_dt, days):
                    stats["date_filtered_out"] += 1
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _clean_text(_strip_html(snippet_raw))
                snippet = _truncate(snippet, SNIPPET_MAX)

                sid = _stable_id(title, link)
                if sid in articles:
                    continue

                articles[sid] = {
                    "track": "nexon",
                    "keyword": term,
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
            print(f"[WARN] RSS call failed (nexon term={term}): {ex}")
            continue

    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats

def _is_nexon(a: Dict) -> bool:
    blob = f"{a.get('title','')} {a.get('snippet','')} {a.get('link','')}".lower()
    return ("넥슨" in blob) or ("nexon" in blob)

def build_messages(general_articles: List[Dict], nexon_articles: List[Dict],
                   stats_general: Dict[str, int], stats_nexon: Dict[str, int], days: int) -> List[str]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    header = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {days}일)\n"
    header += f"- general: feeds={stats_general.get('feeds_called',0)}, entries={stats_general.get('entries_seen',0)}, added={stats_general.get('added',0)}, strict_drop={stats_general.get('strict_filtered_out',0)}\n"
    header += f"- nexon: feeds={stats_nexon.get('feeds_called',0)}, entries={stats_nexon.get('entries_seen',0)}, added={stats_nexon.get('added',0)}\n\n"

    def fmt(a: Dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        sn = f"\n    - {a['snippet']}" if a.get("snippet") else ""
        return f"▶ *[{a.get('press','NEWS')}]* <{a['link']}|{a['title']}>{pub}{sn}\n"

    # 일반 뉴스 (상한 유지)
    body = "### 🌐 주요 게임업계 뉴스\n"
    if not general_articles:
        body += f"- 최근 {days}일 기준 뉴스가 없습니다.\n"
    else:
        for a in general_articles[:80]:
            body += fmt(a)

    # 넥슨 뉴스는 “전용 트랙 결과 + (일반 트랙에서 넥슨으로 걸린 것)” 합쳐서 중복 제거
    merged = {}
    for a in nexon_articles:
        merged[_stable_id(a["title"], a["link"])] = a
    for a in general_articles:
        if _is_nexon(a):
            merged[_stable_id(a["title"], a["link"])] = a

    merged_list = list(merged.values())
    merged_list.sort(key=lambda x: x["published_dt"] if x.get("published_dt") else datetime.min, reverse=True)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not merged_list:
        body += "- '넥슨' 관련 기사(제목/요약/URL 기준)가 없습니다.\n"
    else:
        for a in merged_list[:30]:
            body += fmt(a)

    full = header + body

    # Slack 길이 제한 대응
    messages: List[str] = []
    chunk = ""
    for line in full.splitlines(True):
        if len(chunk) + len(line) > 3500:
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

def main() -> None:
    # 일반 트랙
    primary = PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY]
    general, stats_g = fetch_general(primary, TARGET_SITES, SEARCH_DAYS)
    if not general:
        general, stats_g = fetch_general(PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK], TARGET_SITES, SEARCH_DAYS)

    # 넥슨 트랙 (별도)
    nexon, stats_n = fetch_nexon(TARGET_SITES, SEARCH_DAYS)

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
        time.sleep(0.2)

if __name__ == "__main__":
    main()
