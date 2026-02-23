# ------------------------------------------------------------------
# [운영용 v4.4] 정확 링크 + 기사 아닌 게시물 제거 (경량)
# - Python 3.9 호환
# - 목표:
#   1) Slack에 보내는 링크는 "원문 URL"만 (Google 중간 링크 제거)
#   2) Inven board/ 공략/ 길드모집/ keyword 리스트 페이지 제거
#   3) 매일 KST 10시에 "전날" 기사만 발송되도록 after/before 날짜 범위 고정
#
# 핵심:
# - RSS 수집은 빠르게 (feedparser)
# - 원문 URL은 "보낼 기사 상위 N개만" 리다이렉트 해제(HEAD/GET stream) -> 경량 유지
# - 본문 크롤링/파싱 없음
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
from zoneinfo import ZoneInfo

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

# 전날 범위로 고정 (KST)
KST = ZoneInfo("Asia/Seoul")

REQUEST_TIMEOUT = 10
USER_AGENT = "Mozilla/5.0 (GameNewsBot/1.4; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.12)

MAX_ENTRIES_PER_FEED = 30
MAX_ENTRIES_PER_NEXON_FEED = 20

# Slack 출력 상한(너무 많으면 노이즈)
GENERAL_SEND_LIMIT = 50
NEXON_SEND_LIMIT = 5

# 리다이렉트 해제는 "보낼 기사" + 백필용 일부만 수행 (경량)
RESOLVE_BUDGET_GENERAL = 80   # 일반 기사 URL 해제 최대 개수
RESOLVE_BUDGET_NEXON = 30     # 넥슨 URL 해제 최대 개수

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 12

# ==========================
# 컨텍스트/필터
# ==========================
GAME_CONTEXT_OR = [
    "게임", "게이밍", "게임업계", "게임사", "퍼블리셔", "개발사",
    "모바일게임", "PC게임", "콘솔", "스팀", "Steam", "PS5", "플레이스테이션", "닌텐도", "Xbox",
    "RPG", "MMORPG", "FPS", "MOBA", "e스포츠", "esports"
]
GAME_CONTEXT_QUERY = "(" + " OR ".join(GAME_CONTEXT_OR) + ")"

# 게시글/커뮤니티성(공략/길드모집 등) 1차 컷: 제목/요약 기반
NON_ARTICLE_TITLE_HINTS = [
    "공략", "팁", "노하우", "질문", "Q&A", "인증", "후기", "스샷", "스크린샷",
    "길드", "길드모집", "모집", "파티", "팟", "고정팟", "클랜", "클랜모집",
    "거래", "나눔", "판매", "삽니다",
    "버그제보", "건의", "토론",
]

# Inven URL 패턴 필터(원문 URL 기준으로만 적용)
def is_valid_article_url(url: str) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        qs = parse_qs(p.query or "")
    except Exception:
        return True  # 파싱 실패는 과하게 버리기보다 통과

    # 인벤: board는 기사 아님, keyword 리스트도 기사 아님
    if host.endswith("inven.co.kr"):
        if "/board/" in path:
            return False
        if path.startswith("/webzine/news") or path.startswith("/webzine/news/"):
            # 기사면 news=가 있어야 함
            if "news" not in qs:
                return False
        # news 없이 keyword만 있는 리스트 페이지 제거
        if "keyword" in qs and "news" not in qs:
            return False

    return True

# 넥슨 검증(제목/요약에 넥슨이 실제 포함되어야만 넥슨 섹션에 포함)
NEXON_TERMS = [
    "넥슨", "nexon",
    "넥슨코리아", "넥슨게임즈", "넥슨네트웍스", "네오플"
]

def contains_nexon(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(t.lower() in blob for t in NEXON_TERMS)

# 넥슨 중요도(가벼운 점수)
NEXON_IMPORTANCE = [
    ("M&A", 5), ("인수", 5), ("합병", 5),
    ("투자", 4), ("지분", 4),
    ("소송", 5), ("규제", 4),
    ("매출", 4), ("실적", 4), ("영업이익", 4), ("순이익", 4),
    ("출시", 3), ("업데이트", 3),
    ("리스크", 3), ("악재", 3), ("호재", 3),
]

def nexon_score(a: Dict) -> int:
    blob = f"{a.get('title','')} {a.get('snippet','')}".lower()
    score = 0
    for kw, w in NEXON_IMPORTANCE:
        if kw.lower() in blob:
            score += w
    if contains_nexon(a.get("title",""), a.get("snippet","")):
        score += 2
    return score

# ==========================
# 유틸
# ==========================
def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"

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

def _sleep():
    time.sleep(random.uniform(*SLEEP_BETWEEN_FEEDS))

def _google_news_rss_search_url(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"

def _site_or_query(sites: List[str]) -> str:
    return "(" + " OR ".join([f"site:{s}" for s in sites]) + ")"

def _press_guess(entry) -> str:
    # Google News RSS는 source.title로 언론사 이름이 들어오는 경우가 많음(표시용)
    try:
        src = getattr(entry, "source", None)
        if src and isinstance(src, dict):
            t = _clean_text(src.get("title", ""))
            if t:
                return t
    except Exception:
        pass
    return "NEWS"

def _yesterday_range_kst() -> Tuple[str, str, str]:
    now = datetime.now(KST)
    today = now.date()
    yday = today - timedelta(days=1)
    # Google search operator: after/before는 날짜 문자열 사용
    return yday.isoformat(), yday.isoformat(), today.isoformat()

def _looks_like_non_article(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(h.lower() in blob for h in NON_ARTICLE_TITLE_HINTS)

# ==========================
# 핵심: Google 중간 링크 -> 원문 URL 해제 (경량)
# - HEAD 시도 -> 막히면 GET stream (본문 다운로드 없음)
# - 캐시로 중복 해제 방지
# ==========================
class UrlResolver:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        })
        self.cache: Dict[str, str] = {}

    def resolve(self, url: str) -> str:
        if not url:
            return url
        if url in self.cache:
            return self.cache[url]

        final_url = url
        try:
            # 1) HEAD allow_redirects
            r = self.session.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
            if r.url:
                final_url = r.url
        except Exception:
            # 2) fallback: GET stream (본문 다운로드 없이)
            try:
                r = self.session.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT, stream=True)
                if r.url:
                    final_url = r.url
                try:
                    r.close()
                except Exception:
                    pass
            except Exception:
                final_url = url

        self.cache[url] = final_url
        return final_url

# ==========================
# 쿼리 빌더
# ==========================
def build_query_general(keyword: str, sites: List[str], after: str, before: str) -> str:
    # 전날 고정 after/before
    return f"{GAME_CONTEXT_QUERY} {keyword} {_site_or_query(sites)} after:{after} before:{before}"

def build_query_nexon(keyword: str, sites: List[str], after: str, before: str) -> str:
    # 넥슨은 키워드+넥슨 교집합
    nexon_expr = '("넥슨" OR Nexon OR "넥슨게임즈" OR 네오플)'
    return f'{nexon_expr} {keyword} {_site_or_query(sites)} after:{after} before:{before}'

# ==========================
# RSS 수집 (링크는 아직 google link 상태로 저장)
# ==========================
def fetch_track(track: str,
                keywords: List[str],
                max_entries_per_feed: int,
                query_builder,
                after: str,
                before: str) -> Tuple[List[Dict], Dict[str, int]]:

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats = {
        "feeds_called": 0,
        "entries_seen": 0,
        "non_article_hint_drop": 0,
        "added": 0,
    }

    found: Dict[str, Dict] = {}

    for kw in keywords:
        q = query_builder(kw, TARGET_SITES, after, before)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            for e in feed.entries[:max_entries_per_feed]:
                stats["entries_seen"] += 1

                title = _clean_text(getattr(e, "title", ""))
                if not title:
                    continue

                google_link = _clean_text(getattr(e, "link", "") or "")
                if not google_link:
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _truncate(_clean_text(_strip_html(snippet_raw)), SNIPPET_MAX)

                # 1차: 제목/요약 기반으로 공략/길드모집 등 컷 (매우 가벼움)
                if _looks_like_non_article(title, snippet):
                    stats["non_article_hint_drop"] += 1
                    continue

                published_dt = _parse_published(e)
                press = _press_guess(e)

                sid = _stable_id(title, google_link)
                if sid in found:
                    continue

                found[sid] = {
                    "track": track,
                    "keyword": kw,
                    "press": press,
                    "title": _truncate(title, TITLE_MAX),
                    "google_link": google_link,   # 중간 링크
                    "link": google_link,          # 최종적으로 원문으로 교체할 예정
                    "published_dt": published_dt,
                    "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                    "snippet": snippet,
                }
                stats["added"] += 1

            _sleep()
        except Exception as ex:
            print(f"[WARN] RSS call failed ({track} kw={kw}): {ex}")
            continue

    # 최신순 기본 정렬
    def sort_key(a: Dict) -> datetime:
        return a["published_dt"] if a.get("published_dt") else datetime.min

    items = sorted(list(found.values()), key=sort_key, reverse=True)
    return items, stats

# ==========================
# 링크 해제 + URL 패턴 필터 적용 + 백필
# ==========================
def finalize_links_and_filter(items: List[Dict], resolver: UrlResolver, budget: int) -> Tuple[List[Dict], Dict[str, int]]:
    stats = {
        "resolved": 0,
        "url_pattern_drop": 0,
        "resolve_failed_or_google_left": 0,
    }

    out: List[Dict] = []
    used = 0

    for a in items:
        if used >= budget:
            # 예산 넘으면 google 링크 그대로(단, 인벤 게시판 같은 걸 못 걸러서 위험)
            # -> 예산은 SEND_LIMIT보다 여유 있게 잡아둠(위에서 80/30)
            a["link"] = a["google_link"]
            stats["resolve_failed_or_google_left"] += 1
            out.append(a)
            continue

        g = a.get("google_link", "")
        final = resolver.resolve(g)
        used += 1
        stats["resolved"] += 1

        a["link"] = final

        # 원문 URL 패턴으로 "기사 아닌 링크" 확실하게 제거
        if not is_valid_article_url(final):
            stats["url_pattern_drop"] += 1
            continue

        out.append(a)

    # 중복(원문 URL 기준)
    dedup: Dict[str, Dict] = {}
    for a in out:
        sid = _stable_id(a.get("title",""), a.get("link",""))
        dedup[sid] = a

    # 다시 최신순 정렬
    def sort_key(a: Dict) -> datetime:
        return a["published_dt"] if a.get("published_dt") else datetime.min

    return sorted(list(dedup.values()), key=sort_key, reverse=True), stats

# ==========================
# Slack 메시지
# ==========================
def build_messages(general: List[Dict], nexon: List[Dict],
                   stats: Dict[str, Dict], yday_label: str) -> List[str]:
    header = f"## 📰 {yday_label} 전날 주요 게임업계 뉴스 브리핑 (발송: KST 10:00)\n"
    header += f"- general: feeds={stats['general_rss']['feeds_called']}, entries={stats['general_rss']['entries_seen']}, rss_added={stats['general_rss']['added']}, hint_drop={stats['general_rss']['non_article_hint_drop']}, resolved={stats['general_finalize']['resolved']}, url_drop={stats['general_finalize']['url_pattern_drop']}\n"
    header += f"- nexon: feeds={stats['nexon_rss']['feeds_called']}, entries={stats['nexon_rss']['entries_seen']}, rss_added={stats['nexon_rss']['added']}, hint_drop={stats['nexon_rss']['non_article_hint_drop']}, resolved={stats['nexon_finalize']['resolved']}, url_drop={stats['nexon_finalize']['url_pattern_drop']}\n\n"

    def fmt(a: Dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        sn = f"\n    - {a['snippet']}" if a.get("snippet") else ""
        return f"▶ *[{a.get('press','NEWS')}]* <{a['link']}|{a['title']}>{pub}{sn}\n"

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not general:
        body += "- 전날 기준 수집된 뉴스가 없습니다.\n"
    else:
        for a in general[:GENERAL_SEND_LIMIT]:
            body += fmt(a)

    # 넥슨: 정확도 강제(제목/요약 넥슨 포함) + 중요도 Top 5
    nexon_true = [a for a in nexon if contains_nexon(a.get("title",""), a.get("snippet",""))]
    nexon_sorted = sorted(nexon_true, key=lambda x: (nexon_score(x), x["published_dt"] or datetime.min), reverse=True)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스 (Top 5)\n"
    if not nexon_sorted:
        body += "- 전날 기준 넥슨 관련 주요 뉴스를 찾지 못했습니다.\n"
    else:
        for a in nexon_sorted[:NEXON_SEND_LIMIT]:
            body += fmt(a)

    full = header + body

    # Slack 길이 분할
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

def send_to_slack(message: str) -> None:
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
# Main
# ==========================
def main() -> None:
    yday_label, after, before = _yesterday_range_kst()

    # 1) RSS로 후보 수집
    general_keywords = PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY]
    general_items, stats_general_rss = fetch_track(
        track="general",
        keywords=general_keywords,
        max_entries_per_feed=MAX_ENTRIES_PER_FEED,
        query_builder=build_query_general,
        after=after,
        before=before,
    )
    # 너무 적으면 키워드 확장
    if len(general_items) < 10:
        general_items, stats_general_rss = fetch_track(
            track="general",
            keywords=PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK],
            max_entries_per_feed=MAX_ENTRIES_PER_FEED,
            query_builder=build_query_general,
            after=after,
            before=before,
        )

    nexon_items, stats_nexon_rss = fetch_track(
        track="nexon",
        keywords=PRIMARY_KEYWORDS,
        max_entries_per_feed=MAX_ENTRIES_PER_NEXON_FEED,
        query_builder=build_query_nexon,
        after=after,
        before=before,
    )

    # 2) "보낼 만큼만" 리다이렉트 해제해서 원문 URL 확정 + URL 패턴 필터
    resolver = UrlResolver()
    general_final, stats_general_finalize = finalize_links_and_filter(general_items, resolver, RESOLVE_BUDGET_GENERAL)
    nexon_final, stats_nexon_finalize = finalize_links_and_filter(nexon_items, resolver, RESOLVE_BUDGET_NEXON)

    # 3) 디버그 미리보기(액션 로그용)
    print(f"[INFO] KST range: after={after} before={before} (yday={yday_label})")
    print(f"[INFO] general rss={len(general_items)} final={len(general_final)}")
    print(f"[INFO] nexon rss={len(nexon_items)} final={len(nexon_final)}")

    print("[INFO] preview general:")
    for i, a in enumerate(general_final[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. {a.get('title','')} :: {a.get('link','')}")
    print("[INFO] preview nexon:")
    for i, a in enumerate(nexon_final[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. {a.get('title','')} :: {a.get('link','')}")

    stats = {
        "general_rss": stats_general_rss,
        "nexon_rss": stats_nexon_rss,
        "general_finalize": stats_general_finalize,
        "nexon_finalize": stats_nexon_finalize,
    }

    # 4) Slack 전송
    messages = build_messages(general_final, nexon_final, stats, yday_label)
    for idx, msg in enumerate(messages, 1):
        send_to_slack(msg)
        print(f"[INFO] sent slack {idx}/{len(messages)}")
        time.sleep(0.15)

if __name__ == "__main__":
    main()
