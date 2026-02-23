# ------------------------------------------------------------------
# [운영용 v4.6] 원문 URL 100% 지향 + 기사 아닌 링크 제거 + KST 윈도우(주말/공휴일 누적)
# - Python 3.9 호환
# - 목표:
#   1) Slack 링크는 "원문 URL"만 (news.google.com 링크는 발송하지 않음)
#   2) Inven board/ 공략/ 길드모집/ keyword 리스트 페이지 제거
#   3) 매일 KST 10시에 "전날(주말/공휴일 누적)" 윈도우 기사 발송
#
# 핵심 수정:
# - Google 중간 링크( news.google.com/rss/articles/... )를 열어 HTML에서 원문 URL을 추출
# - HTML을 너무 조금만 읽어서 실패하던 문제 해결: 최대 512KB까지 읽으며 URL 탐색
# - 원문 URL이 안 나오면 "버림"(구글 링크 발송 금지)
# ------------------------------------------------------------------
import os
import re
import json
import time
import hashlib
import random
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, time as dtime, timezone
from urllib.parse import quote, urlparse, parse_qs, unquote
from email.utils import parsedate_to_datetime

import requests
import feedparser
from zoneinfo import ZoneInfo

# holidays는 선택(없으면 주말만)
try:
    import holidays as holidays_lib  # pip install holidays
except Exception:
    holidays_lib = None

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

KEYWORD_BATCH_PRIMARY = 10
KEYWORD_BATCH_FALLBACK = 18

KST = ZoneInfo("Asia/Seoul")
SEND_HOUR = 10
END_CUTOFF = dtime(9, 59, 59)

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (GameNewsBot/1.60; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.12)

MAX_ENTRIES_PER_FEED = 30
MAX_ENTRIES_PER_NEXON_FEED = 20

GENERAL_SEND_LIMIT = 50
NEXON_SEND_LIMIT = 5

# 원문 해제는 후보를 넉넉히(하지만 과도하게 무겁지 않게)
RESOLVE_BUDGET_GENERAL = 120
RESOLVE_BUDGET_NEXON = 80

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 12

# Google 중간 링크 HTML에서 원문 URL 찾을 때 읽을 최대 바이트
MAX_HTML_BYTES = 512 * 1024  # 512KB

# ==========================
# 컨텍스트/필터
# ==========================
GAME_CONTEXT_OR = [
    "게임", "게이밍", "게임업계", "게임사", "퍼블리셔", "개발사",
    "모바일게임", "PC게임", "콘솔", "스팀", "Steam", "PS5", "플레이스테이션", "닌텐도", "Xbox",
    "RPG", "MMORPG", "FPS", "MOBA", "e스포츠", "esports"
]
GAME_CONTEXT_QUERY = "(" + " OR ".join(GAME_CONTEXT_OR) + ")"

NON_ARTICLE_TITLE_HINTS = [
    "공략", "팁", "노하우", "질문", "q&a", "Q&A", "인증", "후기", "스샷", "스크린샷",
    "길드", "길드모집", "길드 모집", "모집", "클랜", "클랜모집",
    "파티", "팟", "고정팟",
    "거래", "나눔", "판매", "삽니다",
    "버그제보", "건의", "토론",
]

NEXON_TERMS = ["넥슨", "nexon", "넥슨코리아", "넥슨게임즈", "넥슨네트웍스", "네오플"]

NEXON_IMPORTANCE = [
    ("M&A", 5), ("인수", 5), ("합병", 5),
    ("투자", 4), ("지분", 4),
    ("소송", 5), ("규제", 4),
    ("매출", 4), ("실적", 4), ("영업이익", 4), ("순이익", 4),
    ("출시", 3), ("업데이트", 3),
    ("리스크", 3), ("악재", 3), ("호재", 3),
]

_GOOGLE_HOSTS = {"news.google.com", "www.google.com", "google.com"}

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

def _sleep():
    time.sleep(random.uniform(*SLEEP_BETWEEN_FEEDS))

def _google_news_rss_search_url(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + quote(query) + "&hl=ko&gl=KR&ceid=KR:ko"

def _site_or_query(sites: List[str]) -> str:
    return "(" + " OR ".join([f"site:{s}" for s in sites]) + ")"

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

def _looks_like_non_article(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(h.lower() in blob for h in NON_ARTICLE_TITLE_HINTS)

def _is_google_url(url: str) -> bool:
    try:
        h = (urlparse(url).netloc or "").lower()
        return h in _GOOGLE_HOSTS or h.endswith(".google.com")
    except Exception:
        return True

def contains_nexon(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(t.lower() in blob for t in NEXON_TERMS)

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
# URL 패턴 필터(원문 URL 기준)
# ==========================
def is_valid_article_url(url: str) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        qs = parse_qs(p.query or "")
    except Exception:
        return False

    if path in ("", "/"):
        return False

    if host.endswith("inven.co.kr"):
        # board는 기사 아님
        if "/board/" in path:
            return False
        # webzine/news 는 news= 있어야 기사
        if path.startswith("/webzine/news"):
            if "news" not in qs:
                return False
        # news 없이 keyword만 있는 리스트는 제거
        if "keyword" in qs and "news" not in qs:
            return False

    return True

# ==========================
# 날짜 파싱(KST aware)
# ==========================
def parse_entry_datetime_kst(entry) -> Optional[datetime]:
    for attr in ("published", "updated"):
        s = getattr(entry, attr, None)
        if s:
            try:
                dt = parsedate_to_datetime(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(KST)
            except Exception:
                pass

    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                naive = datetime(*t[:6])
                return naive.replace(tzinfo=timezone.utc).astimezone(KST)
            except Exception:
                pass
    return None

# ==========================
# 윈도우 계산(주말/공휴일 누적)
# - 예: 월요일이면 금요일 00:00 ~ 월 09:59
# ==========================
def compute_window_kst(now_kst: datetime) -> Tuple[datetime, datetime, str]:
    end_dt = datetime.combine(now_kst.date(), END_CUTOFF, tzinfo=KST)

    kr_holidays = None
    if holidays_lib is not None:
        try:
            kr_holidays = holidays_lib.country_holidays("KR", years=[now_kst.year, now_kst.year - 1])
        except Exception:
            kr_holidays = None

    if kr_holidays is None:
        print("[WARN] holidays 미설치/오류로 공휴일은 제외하지 않고 주말만 누적 처리합니다.")

    def is_business_day(d) -> bool:
        if d.weekday() >= 5:
            return False
        if kr_holidays is not None and d in kr_holidays:
            return False
        return True

    d = now_kst.date() - timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    prev_business = d

    start_dt = datetime.combine(prev_business, dtime(SEND_HOUR, 0, 0), tzinfo=KST)

    # 월요일 + 직전 영업일이 금요일이면 금요일 00:00부터
    if now_kst.weekday() == 0 and prev_business.weekday() == 4:
        start_dt = datetime.combine(prev_business, dtime(0, 0, 0), tzinfo=KST)

    label = f"{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')} (KST)"
    return start_dt, end_dt, label

# ==========================
# 쿼리 빌더
# ==========================
def build_query_general(keyword: str, sites: List[str], after_date: str, before_date: str) -> str:
    return f"{GAME_CONTEXT_QUERY} {keyword} {_site_or_query(sites)} after:{after_date} before:{before_date}"

def build_query_nexon(keyword: str, sites: List[str], after_date: str, before_date: str) -> str:
    nexon_expr = '("넥슨" OR Nexon OR "넥슨게임즈" OR 네오플)'
    return f'{nexon_expr} {keyword} {_site_or_query(sites)} after:{after_date} before:{before_date}'

# ==========================
# 핵심: Google 중간 링크 HTML에서 원문 URL 추출
# ==========================
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

def _collect_target_urls(text: str) -> List[str]:
    urls = _URL_RE.findall(text or "")
    out = []
    for u in urls:
        u = u.split("&amp;")[0]
        try:
            u = unquote(u)
        except Exception:
            pass
        if any(site in u for site in TARGET_SITES):
            out.append(u)
    return out

def _score_candidate(u: str) -> int:
    """원문 URL 후보 점수(높을수록 좋음)."""
    sc = 0
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "")
        qs = parse_qs(p.query or "")
    except Exception:
        return -999

    # target site이면 기본 가산
    if any(s in u for s in TARGET_SITES):
        sc += 10

    # 너무 짧은 경로(홈/섹션) 감점
    if path in ("", "/"):
        sc -= 50
    if len(path) < 6:
        sc -= 10

    # 인벤은 기사(news=) 강한 가산, board 강한 감점
    if "inven.co.kr" in host:
        if "/board/" in (path or "").lower():
            sc -= 80
        if "/webzine/news" in (path or "").lower() and "news" in qs:
            sc += 60
        if "keyword" in qs and "news" not in qs:
            sc -= 60

    return sc

class PublisherResolver:
    """news.google.com/rss/articles/... 를 열어서 원문 URL을 최대한 뽑아낸다."""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        })
        self.cache: Dict[str, Optional[str]] = {}

    def resolve(self, google_url: str) -> Optional[str]:
        if not google_url:
            return None
        if google_url in self.cache:
            return self.cache[google_url]

        # 1) 단순 리다이렉트로 바로 원문이 나오면 베스트
        try:
            r = self.session.get(google_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
            final = r.url or ""
            if final and (not _is_google_url(final)) and any(s in final for s in TARGET_SITES):
                if is_valid_article_url(final):
                    self.cache[google_url] = final
                    return final
        except Exception:
            pass

        # 2) HTML을 일부 읽어 원문 후보 URL 추출(차단/동의페이지여도 URL이 박혀있는 경우가 많음)
        try:
            r = self.session.get(google_url, allow_redirects=True, timeout=REQUEST_TIMEOUT, stream=True)
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=16384):
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) >= MAX_HTML_BYTES:
                    break
            try:
                r.close()
            except Exception:
                pass

            text = buf.decode("utf-8", errors="ignore")
            cands = _collect_target_urls(text)
            if cands:
                cands = list(dict.fromkeys(cands))  # stable unique
                cands.sort(key=_score_candidate, reverse=True)

                # 점수 높은 것부터 기사 URL 필터 통과하는 것 선택
                for u in cands[:25]:
                    if is_valid_article_url(u):
                        self.cache[google_url] = u
                        return u
        except Exception:
            pass

        # 실패
        self.cache[google_url] = None
        return None

# ==========================
# RSS 수집
# ==========================
def fetch_track(track: str,
                keywords: List[str],
                max_entries_per_feed: int,
                query_builder,
                after_date: str,
                before_date: str,
                start_dt: datetime,
                end_dt: datetime) -> Tuple[List[Dict], Dict[str, int]]:

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    stats = {
        "feeds_called": 0,
        "entries_seen": 0,
        "hint_drop": 0,
        "no_date_drop": 0,
        "too_old_drop": 0,
        "window_drop": 0,
        "added": 0,
    }

    found: Dict[str, Dict] = {}
    hard_old_cutoff = start_dt - timedelta(days=1)  # 2010 같은 이상치 방지

    for kw in keywords:
        q = query_builder(kw, TARGET_SITES, after_date, before_date)
        url = _google_news_rss_search_url(q)

        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            stats["feeds_called"] += 1

            feed = feedparser.parse(resp.text)
            for e in feed.entries[:max_entries_per_feed]:
                stats["entries_seen"] += 1

                title = _truncate(_clean_text(getattr(e, "title", "")), TITLE_MAX)
                if not title:
                    continue

                google_link = _clean_text(getattr(e, "link", "") or "")
                if not google_link:
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _truncate(_clean_text(_strip_html(snippet_raw)), SNIPPET_MAX)

                if _looks_like_non_article(title, snippet):
                    stats["hint_drop"] += 1
                    continue

                pub_kst = parse_entry_datetime_kst(e)
                if not pub_kst:
                    stats["no_date_drop"] += 1
                    continue

                if pub_kst < hard_old_cutoff:
                    stats["too_old_drop"] += 1
                    continue

                if pub_kst < start_dt or pub_kst > end_dt:
                    stats["window_drop"] += 1
                    continue

                press = _press_guess(e)
                sid = _stable_id(title, google_link)
                if sid in found:
                    continue

                found[sid] = {
                    "track": track,
                    "keyword": kw,
                    "press": press,
                    "title": title,
                    "google_link": google_link,   # google 중간
                    "link": None,                # 원문으로 확정될 값
                    "published_dt": pub_kst,
                    "published": pub_kst.strftime("%Y-%m-%d %H:%M"),
                    "snippet": snippet,
                }
                stats["added"] += 1

            _sleep()
        except Exception as ex:
            print(f"[WARN] RSS call failed ({track} kw={kw}): {ex}")
            continue

    items = sorted(found.values(), key=lambda a: a["published_dt"], reverse=True)
    return items, stats

# ==========================
# 원문 확정 + 필터
# - 원문 URL 확정 실패(=google 링크만 남음)이면 버림 (요구사항: google 링크 금지)
# ==========================
def finalize_items(items: List[Dict], resolver: PublisherResolver, budget: int) -> Tuple[List[Dict], Dict[str, int]]:
    stats = {"resolved_ok": 0, "resolve_fail_drop": 0, "non_article_url_drop": 0}
    out: List[Dict] = []
    used = 0

    for a in items:
        if used >= budget:
            stats["resolve_fail_drop"] += 1
            continue

        pub_url = resolver.resolve(a["google_link"])
        used += 1

        if not pub_url:
            stats["resolve_fail_drop"] += 1
            continue

        if not is_valid_article_url(pub_url):
            stats["non_article_url_drop"] += 1
            continue

        a["link"] = pub_url
        stats["resolved_ok"] += 1
        out.append(a)

    # 원문 URL 기준 중복 제거
    dedup: Dict[str, Dict] = {}
    for a in out:
        sid = _stable_id(a["title"], a["link"])
        dedup[sid] = a

    final = sorted(dedup.values(), key=lambda x: x["published_dt"], reverse=True)
    return final, stats

# ==========================
# Slack 메시지
# ==========================
def build_messages(window_label: str,
                   general: List[Dict], nexon: List[Dict],
                   stats: Dict[str, Dict]) -> List[str]:
    header = f"## 📰 주요 게임업계 뉴스 브리핑\n- 범위: {window_label}\n"
    header += (
        f"- general: feeds={stats['general_rss']['feeds_called']}, seen={stats['general_rss']['entries_seen']}, "
        f"added={stats['general_rss']['added']}, hint_drop={stats['general_rss']['hint_drop']}, "
        f"window_drop={stats['general_rss']['window_drop']}, resolved_ok={stats['general_final']['resolved_ok']}, "
        f"resolve_fail_drop={stats['general_final']['resolve_fail_drop']}, url_drop={stats['general_final']['non_article_url_drop']}\n"
    )
    header += (
        f"- nexon: feeds={stats['nexon_rss']['feeds_called']}, seen={stats['nexon_rss']['entries_seen']}, "
        f"added={stats['nexon_rss']['added']}, hint_drop={stats['nexon_rss']['hint_drop']}, "
        f"window_drop={stats['nexon_rss']['window_drop']}, resolved_ok={stats['nexon_final']['resolved_ok']}, "
        f"resolve_fail_drop={stats['nexon_final']['resolve_fail_drop']}, url_drop={stats['nexon_final']['non_article_url_drop']}\n\n"
    )

    def fmt(a: Dict) -> str:
        sn = f"\n    - {a['snippet']}" if a.get("snippet") else ""
        return f"▶ *[{a.get('press','NEWS')}]* <{a['link']}|{a['title']}> ({a['published']}){sn}\n"

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not general:
        body += "- 해당 범위에서 뉴스가 없습니다.\n"
    else:
        for a in general[:GENERAL_SEND_LIMIT]:
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스 (Top 5)\n"
    nexon_true = [a for a in nexon if contains_nexon(a["title"], a.get("snippet",""))]
    nexon_sorted = sorted(nexon_true, key=lambda x: (nexon_score(x), x["published_dt"]), reverse=True)

    if not nexon_sorted:
        body += "- 해당 범위에서 넥슨 관련 주요 뉴스를 찾지 못했습니다.\n"
    else:
        for a in nexon_sorted[:NEXON_SEND_LIMIT]:
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
    now_kst = datetime.now(KST)
    start_dt, end_dt, window_label = compute_window_kst(now_kst)

    # Google after/before는 날짜 단위만 적용되므로 넉넉히 잡고,
    # 실제 시간 필터는 RSS published(KST)로 정확히 컷한다.
    after_date = start_dt.date().isoformat()
    before_date = (end_dt.date() + timedelta(days=1)).isoformat()

    general_keywords = PRIMARY_KEYWORDS[:KEYWORD_BATCH_PRIMARY]
    general_raw, stats_general_rss = fetch_track(
        "general", general_keywords, MAX_ENTRIES_PER_FEED,
        build_query_general, after_date, before_date, start_dt, end_dt
    )
    if len(general_raw) < 10:
        general_raw, stats_general_rss = fetch_track(
            "general", PRIMARY_KEYWORDS[:KEYWORD_BATCH_FALLBACK], MAX_ENTRIES_PER_FEED,
            build_query_general, after_date, before_date, start_dt, end_dt
        )

    nexon_raw, stats_nexon_rss = fetch_track(
        "nexon", PRIMARY_KEYWORDS, MAX_ENTRIES_PER_NEXON_FEED,
        build_query_nexon, after_date, before_date, start_dt, end_dt
    )

    resolver = PublisherResolver()
    general_final, stats_general_final = finalize_items(general_raw, resolver, RESOLVE_BUDGET_GENERAL)
    nexon_final, stats_nexon_final = finalize_items(nexon_raw, resolver, RESOLVE_BUDGET_NEXON)

    print(f"[INFO] window: {window_label}")
    print(f"[INFO] general raw={len(general_raw)} final={len(general_final)} stats={stats_general_final}")
    print(f"[INFO] nexon raw={len(nexon_raw)} final={len(nexon_final)} stats={stats_nexon_final}")

    print("[INFO] preview general:")
    for i, a in enumerate(general_final[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. {a['published']} {a['title']} :: {a['link']}")
    print("[INFO] preview nexon:")
    for i, a in enumerate(nexon_final[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. {a['published']} {a['title']} :: {a['link']}")

    stats = {
        "general_rss": stats_general_rss,
        "nexon_rss": stats_nexon_rss,
        "general_final": stats_general_final,
        "nexon_final": stats_nexon_final,
    }

    messages = build_messages(window_label, general_final, nexon_final, stats)
    for idx, msg in enumerate(messages, 1):
        send_to_slack(msg)
        print(f"[INFO] sent slack {idx}/{len(messages)}")
        time.sleep(0.15)

if __name__ == "__main__":
    main()
