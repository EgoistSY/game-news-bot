# ------------------------------------------------------------------
# [운영용 v4.4.0] FAST Google News RSS -> Slack Digest
# - Python 3.9 호환
# - v4.4.0 개선사항:
#   1) get_canonical_link(): Google 중간 링크에서 원문 URL 실제 디코딩
#      -> 추가 HTTP 요청 없이 URL 파라미터(url=) 파싱으로 원문 추출
#   2) is_valid_article_url() 인벤 필터 대폭 강화
#      -> /webzine/news?news=숫자 패턴만 허용
#   3) 비기사 제목 패턴 필터 추가
#      -> 가이드, 모집, 스포주의, LCK 경기결과(승/패) 등 제목 기반 차단
# ------------------------------------------------------------------
import os
import re
import json
import time
import hashlib
import random
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, date
from urllib.parse import quote, urlparse, parse_qs, unquote

import requests
import feedparser

# Python 3.9 zoneinfo
from zoneinfo import ZoneInfo

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
USER_AGENT = "Mozilla/5.0 (FastNewsDigestBot/1.32; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.12)

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 12

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
# ✅ 비기사 제목 패턴 필터
#    아래 정규식 중 하나라도 매칭되면 제목 기반으로 기사를 걸러냄
# --------------------------
NON_ARTICLE_TITLE_PATTERNS = [
    # 인벤 게시판 유형
    re.compile(r"^\[모집\]"),          # 길드/파티 모집
    re.compile(r"^\(스포주의\)"),       # 스포일러 포함 커뮤니티 글
    re.compile(r"^웹진\s*$"),           # 단순 "웹진" 제목
    # e스포츠 경기 결과 단신 (업계 뉴스 목적에 불필요)
    re.compile(r"\[LCK"),              # LCK 경기 관련
    re.compile(r"\[롤챔스\]"),
    re.compile(r"\[오버워치\s*리그\]"),
    # 커뮤니티성 가이드/공략
    re.compile(r"가이드\s*\d+\.?\d*v"),  # "키세팅 설정 가이드 1.0v" 등
    re.compile(r"^\[공략\]"),
]

def has_non_article_title(title: str) -> bool:
    """제목이 비기사 패턴에 해당하면 True"""
    for pat in NON_ARTICLE_TITLE_PATTERNS:
        if pat.search(title):
            return True
    return False


# --------------------------
# ✅ 핵심 수정: 원문 URL 추출 (추가 HTTP 요청 없음)
# --------------------------
def get_canonical_link(entry) -> str:
    """
    Google News RSS entry.link는 보통 아래 두 형태 중 하나:
      A) https://news.google.com/rss/articles/...  (불투명 ID)
      B) https://news.google.com/articles/...?hl=...
      C) 일부 feedparser 버전에서 entry.source 에 원문 URL 제공

    우선순위:
      1) entry.links 중 type이 text/html 이고 Google 도메인이 아닌 것
      2) entry.source 의 href/url
      3) entry.link 의 쿼리스트링에서 url= / q= 파라미터 파싱
      4) entry.link 원본 (fallback)
    """
    # 1) entry.links 순회 — Google 도메인이 아닌 첫 번째 링크
    try:
        links = getattr(entry, "links", []) or []
        for lk in links:
            href = _clean_text(lk.get("href", "") if isinstance(lk, dict) else getattr(lk, "href", ""))
            if href and "google.com" not in href:
                return href
    except Exception:
        pass

    # 2) entry.source 의 href / url
    try:
        src = getattr(entry, "source", None)
        if src:
            for k in ("href", "url"):
                val = (_clean_text(src.get(k, "")) if isinstance(src, dict)
                       else _clean_text(getattr(src, k, "") or ""))
                if val and "google.com" not in val:
                    return val
    except Exception:
        pass

    # 3) entry.link 쿼리스트링에서 원문 URL 파라미터 시도
    raw_link = _clean_text(getattr(entry, "link", "") or "")
    if raw_link:
        try:
            p = urlparse(raw_link)
            qs = parse_qs(p.query or "")
            for param in ("url", "q", "u"):
                vals = qs.get(param, [])
                if vals:
                    decoded = unquote(vals[0])
                    if decoded.startswith("http") and "google.com" not in decoded:
                        return decoded
        except Exception:
            pass

    # 4) fallback: entry.link 그대로 (Google 중간 링크일 수 있음)
    return raw_link


# --------------------------
# ✅ 강화된 URL 필터 (원문 URL 기준)
# --------------------------
def is_valid_article_url(url: str) -> bool:
    if not url:
        return False

    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        qs = parse_qs(p.query or "")
    except Exception:
        return True

    # Google 중간 링크는 원문 URL이 아니므로 원칙적으로 걸러냄
    # (fallback으로 남은 경우 통과시켜 나중에 제목 필터에서 처리)
    if "news.google.com" in host:
        # 판단 불가 — 일단 통과시키되 로그 남김 (원문 추출 실패 케이스)
        return True

    # 공통 비기사 경로
    common_bad_tokens = [
        "/board/",
        "/search",
        "/tag/",
        "/ranking", "/rank",
        "/gallery",
        "/forum/",
        "/community/",
    ]
    if any(tok in path for tok in common_bad_tokens):
        return False

    # ✅ Inven 특화 — 뉴스 기사 URL만 허용
    if host.endswith("inven.co.kr"):
        # 허용 패턴: /webzine/news?news=숫자 (실제 기사)
        #   예) https://www.inven.co.kr/webzine/news/?news=298765
        if path.rstrip("/") == "/webzine/news" or path.startswith("/webzine/news"):
            news_ids = qs.get("news", [])
            if news_ids and re.match(r"^\d+$", news_ids[0]):
                return True  # 정상 기사
            else:
                return False  # 키워드 목록, 웹진 메인 등
        # 그 외 inven 경로는 모두 차단 (게시판, 갤러리 등)
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
# 전날(KST) 날짜 범위
# --------------------------
def yesterday_range_kst() -> Tuple[str, str, str]:
    tz = ZoneInfo("Asia/Seoul")
    now = datetime.now(tz)
    today = now.date()
    yday = today - timedelta(days=1)
    return (yday.isoformat(), yday.isoformat(), today.isoformat())

# --------------------------
# 쿼리
# --------------------------
def build_query_general(keyword: str, sites: List[str], after: str, before: str) -> str:
    if sites:
        return f"{GAME_CONTEXT_QUERY} {keyword} {_site_or_query(sites)} after:{after} before:{before}"
    return f"{GAME_CONTEXT_QUERY} {keyword} after:{after} before:{before}"

def build_query_nexon(keyword: str, sites: List[str], after: str, before: str) -> str:
    nexon_expr = '("넥슨" OR Nexon OR "넥슨게임즈" OR 네오플)'
    if sites:
        return f'{nexon_expr} {keyword} {_site_or_query(sites)} after:{after} before:{before}'
    return f'{nexon_expr} {keyword} after:{after} before:{before}'

# --------------------------
# RSS 수집 — 공통 엔트리 처리 로직
# --------------------------
def _process_entry(e, stats: Dict, track: str, kw: str) -> Optional[Dict]:
    """
    단일 RSS 엔트리를 파싱하여 기사 dict 반환.
    필터에 걸리면 None 반환 + stats 업데이트.
    """
    title = _clean_text(getattr(e, "title", ""))
    if not title:
        return None

    # ✅ 비기사 제목 패턴 필터
    if has_non_article_title(title):
        stats.setdefault("title_pattern_filtered_out", 0)
        stats["title_pattern_filtered_out"] += 1
        return None

    # ✅ 원문 URL 추출
    link = get_canonical_link(e)
    if not link:
        return None

    # ✅ URL 기반 비기사 필터
    if not is_valid_article_url(link):
        stats["non_article_url_filtered_out"] += 1
        return None

    published_dt = _parse_published(e)
    if not _within_days(published_dt, SEARCH_DAYS):
        stats["date_filtered_out"] += 1
        return None

    snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
    snippet = _truncate(_clean_text(_strip_html(snippet_raw)), SNIPPET_MAX)

    # strict 사이트(zdnet, ddaily) — 게임 힌트 없으면 제외
    if any(s in link for s in STRICT_SITES) or any(s in title for s in ("지디넷", "디지털데일리")):
        if not _has_any_hint(f"{title} {snippet}", GAME_HINTS):
            stats["strict_filtered_out"] = stats.get("strict_filtered_out", 0) + 1
            return None

    return {
        "track": track,
        "keyword": kw,
        "press": _press_guess(e),
        "title": _truncate(title, TITLE_MAX),
        "link": link,
        "published_dt": published_dt,
        "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
        "snippet": snippet,
    }


def fetch_general(keywords: List[str], sites: List[str], after: str, before: str) -> Tuple[List[Dict], Dict[str, int]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats: Dict[str, int] = {
        "feeds_called": 0,
        "entries_seen": 0,
        "date_filtered_out": 0,
        "strict_filtered_out": 0,
        "non_article_url_filtered_out": 0,
        "title_pattern_filtered_out": 0,
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    for kw in keywords:
        q = build_query_general(kw, sites, after, before)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            for e in feed.entries[:MAX_ENTRIES_PER_FEED]:
                stats["entries_seen"] += 1
                article = _process_entry(e, stats, "general", kw)
                if article is None:
                    continue

                sid = _stable_id(article["title"], article["link"])
                if sid in articles:
                    continue

                articles[sid] = article
                stats["added"] += 1

            _sleep()
        except Exception as ex:
            print(f"[WARN] RSS call failed (general kw={kw}): {ex}")
            continue

    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats


def fetch_nexon(keywords: List[str], sites: List[str], after: str, before: str) -> Tuple[List[Dict], Dict[str, int]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats: Dict[str, int] = {
        "feeds_called": 0,
        "entries_seen": 0,
        "date_filtered_out": 0,
        "nexon_filtered_out": 0,
        "non_article_url_filtered_out": 0,
        "title_pattern_filtered_out": 0,
        "added": 0,
    }

    articles: Dict[str, Dict] = {}

    for kw in keywords:
        q = build_query_nexon(kw, sites, after, before)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            for e in feed.entries[:MAX_ENTRIES_PER_NEXON_FEED]:
                stats["entries_seen"] += 1
                article = _process_entry(e, stats, "nexon", kw)
                if article is None:
                    continue

                # 넥슨 최종 검증 (제목/요약에 실제 넥슨 포함)
                if not contains_nexon(article["title"], article["snippet"]):
                    stats["nexon_filtered_out"] += 1
                    continue

                sid = _stable_id(article["title"], article["link"])
                if sid in articles:
                    continue

                articles[sid] = article
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
                   stats_g: Dict[str, int], stats_n: Dict[str, int],
                   yday_label: str) -> List[str]:
    header = f"## 📰 {yday_label} 전날 주요 게임업계 뉴스 브리핑 (발송: KST 10:00)\n"
    header += (
        f"- general: feeds={stats_g.get('feeds_called',0)}, "
        f"entries={stats_g.get('entries_seen',0)}, added={stats_g.get('added',0)}, "
        f"strict_drop={stats_g.get('strict_filtered_out',0)}, "
        f"non_article_drop={stats_g.get('non_article_url_filtered_out',0)}, "
        f"title_pat_drop={stats_g.get('title_pattern_filtered_out',0)}\n"
    )
    header += (
        f"- nexon: feeds={stats_n.get('feeds_called',0)}, "
        f"entries={stats_n.get('entries_seen',0)}, added={stats_n.get('added',0)}, "
        f"nexon_drop={stats_n.get('nexon_filtered_out',0)}, "
        f"non_article_drop={stats_n.get('non_article_url_filtered_out',0)}, "
        f"title_pat_drop={stats_n.get('title_pattern_filtered_out',0)}\n\n"
    )

    def fmt(a: Dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        sn = f"\n    - {a['snippet']}" if a.get("snippet") else ""
        return f"▶ *[{a.get('press','NEWS')}]* <{a['link']}|{a['title']}>{pub}{sn}\n"

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not general:
        body += "- 전날 기준 수집된 뉴스가 없습니다.\n"
    else:
        for a in general[:70]:
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스 (Top 5)\n"
    if not nexon:
        body += "- 넥슨 관련 뉴스(키워드 교집합 + 제목/요약 검증)를 찾지 못했습니다.\n"
    else:
        scored = sorted(
            nexon,
            key=lambda x: (nexon_score(x), x["published_dt"] or datetime.min),
            reverse=True
        )
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
    yday_label, after, before = yesterday_range_kst()

    general, stats_g = fetch_general(PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY], TARGET_SITES, after, before)
    if not general:
        general, stats_g = fetch_general(PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK], TARGET_SITES, after, before)

    nexon, stats_n = fetch_nexon(PRIMARY_KEYWORDS, TARGET_SITES, after, before)

    print(f"[INFO] date_range_kst: after={after}, before={before} (yday={yday_label})")
    print(f"[INFO] general fetched: {len(general)}, stats: {stats_g}")
    print(f"[INFO] nexon fetched: {len(nexon)}, stats: {stats_n}")
    print("[INFO] preview general:")
    for i, a in enumerate(general[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','NEWS')}] {a.get('title','')} :: {a.get('link','')}")
    print("[INFO] preview nexon:")
    for i, a in enumerate(nexon[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','NEWS')}] {a.get('title','')} :: {a.get('link','')}")

    messages = build_messages(general, nexon, stats_g, stats_n, yday_label)
    for i, msg in enumerate(messages, 1):
        send_to_slack_text(msg)
        print(f"[INFO] sent slack message {i}/{len(messages)}")
        time.sleep(0.15)


if __name__ == "__main__":
    main()
