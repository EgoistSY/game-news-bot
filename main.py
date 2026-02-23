# ------------------------------------------------------------------
# [운영용 v4.1] FAST Google News RSS -> Slack Digest (Noise-reduced)
# - Python 3.9 호환
# - 핵심 개선:
#   1) 쿼리에 게임 컨텍스트 강제 (노이즈 대폭 감소)
#   2) zdnet/ddaily는 추가로 엄격 필터 (제목/요약에 게임 힌트 필요)
#   3) snippet HTML 제거 (Slack에 <a href=...> 섞이는 문제 방지)
#   4) 폴백에서도 게임 컨텍스트 유지 (은행/유통/인사 기사 방지)
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

SEARCH_DAYS = 14

KEYWORD_BATCH_PRIMARY = 10
KEYWORD_BATCH_FALLBACK = 18
MAX_ENTRIES_PER_FEED = 30

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (FastNewsDigestBot/1.1; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.15)

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 20

# --------------------------
# 게임 컨텍스트 (쿼리 조임)
# --------------------------
GAME_CONTEXT_OR = [
    "게임", "게이밍", "게임업계", "게임사", "퍼블리셔", "개발사",
    "모바일게임", "PC게임", "콘솔", "스팀", "Steam", "PS5", "플레이스테이션", "닌텐도", "Xbox",
    "RPG", "MMORPG", "FPS", "MOBA",
]
GAME_CONTEXT_QUERY = "(" + " OR ".join(GAME_CONTEXT_OR) + ")"

# --------------------------
# “게임 매체가 아닌” 사이트는 더 엄격하게
# --------------------------
STRICT_SITES = {"zdnet.co.kr", "ddaily.co.kr"}

# 제목/요약에 이 힌트가 하나도 없으면(특히 zdnet/ddaily) 버림
GAME_HINTS = [
    "게임", "게이밍", "신작", "업데이트", "출시", "스팀", "콘솔", "모바일", "PC",
    "플레이스테이션", "닌텐도", "Xbox", "RPG", "MMORPG", "FPS", "MOBA", "e스포츠", "esports",
    "넥슨", "엔씨", "크래프톤", "넷마블", "카카오게임", "스마일게이트", "펄어비스",
]

def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _strip_html(s: str) -> str:
    # snippet에 <a ...> 같은 게 섞이는 문제 방지
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
    # entry.source.title이 종종 "게임메카" 등으로 들어옴
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

def _build_query(keyword: str, sites: List[str], days: int, game_context: str) -> str:
    # sites가 비어있으면 site 조건 없이(폴백) game_context + keyword만 유지
    if sites:
        return f"{game_context} {keyword} {_site_or_query(sites)} when:{days}d"
    return f"{game_context} {keyword} when:{days}d"

def _has_game_hint(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    for h in GAME_HINTS:
        if h.lower() in blob:
            return True
    return False

def fetch_fast(keywords: List[str], sites: List[str], days: int) -> Tuple[List[Dict], Dict[str, int]]:
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
        q = _build_query(kw, sites, days, GAME_CONTEXT_QUERY)
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

                # ✅ 사이트별 엄격 필터: zdnet/ddaily는 게임 힌트가 없으면 제거
                # (link가 news.google 중간링크여도, title/summary로 충분히 거를 수 있음)
                if any(s in link for s in STRICT_SITES) or any(s in title for s in ("지디넷", "디지털데일리")):
                    if not _has_game_hint(title, snippet):
                        stats["strict_filtered_out"] += 1
                        continue

                sid = _stable_id(title, link)
                if sid in articles:
                    continue

                articles[sid] = {
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
            print(f"[WARN] RSS call failed (kw={kw}): {ex}")
            continue

    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats

def _is_nexon(a: Dict) -> bool:
    blob = f"{a.get('title','')} {a.get('snippet','')} {a.get('link','')}".lower()
    return ("넥슨" in blob) or ("nexon" in blob)

def build_messages(articles: List[Dict], stats: Dict[str, int], days: int) -> List[str]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    header = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {days}일)\n"
    header += f"- 수집: feeds={stats.get('feeds_called',0)}, entries={stats.get('entries_seen',0)}, added={stats.get('added',0)}, strict_drop={stats.get('strict_filtered_out',0)}\n\n"

    def fmt(a: Dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        sn = f"\n    - {a['snippet']}" if a.get("snippet") else ""
        return f"▶ *[{a.get('press','NEWS')}]* <{a['link']}|{a['title']}>{pub}{sn}\n"

    major = articles
    nexon = [a for a in articles if _is_nexon(a)]

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not major:
        body += f"- 최근 {days}일 기준 뉴스가 없습니다.\n"
    else:
        for a in major[:80]:
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not nexon:
        body += "- '넥슨' 관련 기사(제목/요약/URL 기준)가 없습니다.\n"
    else:
        for a in nexon[:50]:
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

def main() -> None:
    # 1차: 상위 키워드로 사이트 제한 검색
    primary = PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY]
    articles, stats = fetch_fast(primary, TARGET_SITES, SEARCH_DAYS)

    # 0건이면: 키워드 확장
    if not articles:
        print("[INFO] primary fetch returned 0. fallback to full keyword set.")
        articles, stats = fetch_fast(PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK], TARGET_SITES, SEARCH_DAYS)

    # 그래도 0건이면: 최후 폴백(사이트 조건 제거) BUT 게임 컨텍스트는 유지
    if not articles:
        print("[INFO] still 0. final fallback: remove site filters (keep game context).")
        articles, stats = fetch_fast(PRIMARY_KEYWORDS[:10], [], SEARCH_DAYS)

    print(f"[INFO] fetched articles: {len(articles)}")
    print(f"[INFO] stats: {stats}")
    print("[INFO] preview:")
    for i, a in enumerate(articles[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','NEWS')}] {a.get('title','')} :: {a.get('link','')}")

    messages = build_messages(articles, stats, SEARCH_DAYS)
    for i, msg in enumerate(messages, 1):
        send_to_slack_text(msg)
        print(f"[INFO] sent slack message {i}/{len(messages)}")
        time.sleep(0.2)

if __name__ == "__main__":
    main()
