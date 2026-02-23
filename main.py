# ------------------------------------------------------------------
# [운영용 v4] FAST Google News RSS -> Slack Digest (2026-02-23)
# 목표: 1~2분 내 완료 + 0건 확률 최소화 (Python 3.9 호환)
#
# 전략:
# - googlesearch-python 제거
# - news.google 링크 리졸브/HTML 파싱/본문 크롤링 제거 (속도↑, 안정↑)
# - RSS 쿼리 수를 줄이기 위해 site: OR 묶음 사용
# - 0건이면 자동으로 필터 완화 폴백 실행
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

# ==========================
# 설정
# ==========================
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

# 속도/안정 밸런스
# - 키워드 전부를 다 때리면 RSS 호출이 늘어남
# - 운영용에서는 상위 N개만 먼저 수집하고, 0건이면 확장 폴백
KEYWORD_BATCH_PRIMARY = 10   # 1차: 상위 10개 키워드
KEYWORD_BATCH_FALLBACK = 18  # 2차(0건일 때): 전체 키워드

# RSS 1회 호출에서 최대 몇 개 entry까지 사용할지
MAX_ENTRIES_PER_FEED = 30

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (FastNewsDigestBot/1.0; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.15)

# Slack 메시지 제한 대응
SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180

# Actions 로그 미리보기
PREVIEW_TOP_N = 20


# ==========================
# 유틸
# ==========================
def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

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

def _press_guess_from_source(entry) -> str:
    # feedparser가 source/title 등을 주는 경우가 있음. 없으면 NEWS.
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
    # Google News RSS Search
    # hl=ko gl=KR ceid=KR:ko 고정
    return "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"

def _site_or_query(sites: List[str]) -> str:
    # (site:a OR site:b OR site:c)
    return "(" + " OR ".join([f"site:{s}" for s in sites]) + ")"

def _build_query(keyword: str, sites: List[str], days: int) -> str:
    # keyword + (site OR ...) + when:Nd
    # 따옴표는 결과를 급감시킬 수 있어 사용하지 않음
    return f"{keyword} {_site_or_query(sites)} when:{days}d"


# ==========================
# RSS 수집 (빠른 버전)
# ==========================
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
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    # 키워드별로 RSS 한 번씩만 호출 (사이트는 OR로 묶음)
    for kw in keywords:
        q = _build_query(kw, sites, days)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            entries = feed.entries[:MAX_ENTRIES_PER_FEED]

            for e in entries:
                stats["entries_seen"] += 1

                title = _clean_text(getattr(e, "title", ""))
                link = _clean_text(getattr(e, "link", ""))
                if not title or not link:
                    continue

                published_dt = _parse_published(e)
                if not _within_days(published_dt, days):
                    stats["date_filtered_out"] += 1
                    continue

                # RSS에서 제공하는 summary가 있을 수 있음(짧게만 사용)
                snippet = _clean_text(getattr(e, "summary", "") or getattr(e, "description", ""))

                sid = _stable_id(title, link)
                if sid in articles:
                    continue

                articles[sid] = {
                    "keyword": kw,
                    "press": _press_guess_from_source(e),
                    "title": _truncate(title, TITLE_MAX),
                    "link": link,
                    "published_dt": published_dt,
                    "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                    "snippet": _truncate(snippet, SNIPPET_MAX) if snippet else "",
                }
                stats["added"] += 1

            _sleep()

        except Exception as ex:
            print(f"[WARN] RSS call failed (kw={kw}): {ex}")
            continue

    # 최신순 정렬
    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats


# ==========================
# Slack 메시지 생성/전송
# ==========================
def _is_nexon(a: Dict) -> bool:
    blob = f"{a.get('title','')} {a.get('snippet','')} {a.get('link','')}".lower()
    return ("넥슨" in blob) or ("nexon" in blob)

def build_messages(articles: List[Dict], stats: Dict[str, int], days: int) -> List[str]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    header = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {days}일)\n"
    header += f"- 수집: feeds={stats.get('feeds_called',0)}, entries={stats.get('entries_seen',0)}, added={stats.get('added',0)}\n\n"

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
        for a in major[:80]:  # 너무 많이 보내면 스팸이므로 상한
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not nexon:
        body += "- '넥슨' 관련 기사(제목/요약/URL 기준)가 없습니다.\n"
    else:
        for a in nexon[:50]:
            body += fmt(a)

    full = header + body

    # Slack 길이 제한 대응: 라인 단위 분할
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


# ==========================
# Main (자동 폴백)
# ==========================
def main() -> None:
    # 1차 시도: 상위 키워드만 빠르게
    primary_keywords = PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY]
    articles, stats = fetch_fast(primary_keywords, TARGET_SITES, SEARCH_DAYS)

    # 0건이면 2차(키워드 확장)
    if not articles:
        print("[INFO] primary fetch returned 0. fallback to full keyword set.")
        articles, stats = fetch_fast(PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK], TARGET_SITES, SEARCH_DAYS)

    # 그래도 0건이면 최후 폴백: 사이트 필터 제거(업계 뉴스라도 보내기)
    if not articles:
        print("[INFO] still 0. final fallback: remove site filters.")
        # 키워드 10개만, when만 유지
        session_sites: List[str] = []
        articles, stats = fetch_fast(PRIMARY_KEYWORDS[:10], session_sites, SEARCH_DAYS)

    print(f"[INFO] fetched articles: {len(articles)}")
    print(f"[INFO] stats: {stats}")
    print("[INFO] preview:")
    for i, a in enumerate(articles[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','NEWS')}] {a.get('title','')} :: {a.get('link','')}")

    # Slack 전송
    messages = build_messages(articles, stats, SEARCH_DAYS)
    for i, msg in enumerate(messages, 1):
        send_to_slack_text(msg)
        print(f"[INFO] sent slack message {i}/{len(messages)}")
        time.sleep(0.2)

if __name__ == "__main__":
    main()
