# ------------------------------------------------------------------
# [v4.5.3] FIX: Google 페이지 파싱 의존 제거
# - Google News RSS 엔트리 내부(entry.links/content/summary)에서 원문 URL을 우선 추출
# - 원문 URL이 없을 때만 네트워크로 google 링크 해제(보조)
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

# holidays는 선택(없으면 주말만 처리)
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
USER_AGENT = "Mozilla/5.0 (GameNewsBot/1.53; SlackWebhook)"
SLEEP_BETWEEN_FEEDS = (0.05, 0.12)

MAX_ENTRIES_PER_FEED = 30
MAX_ENTRIES_PER_NEXON_FEED = 20

GENERAL_SEND_LIMIT = 50
NEXON_SEND_LIMIT = 5

# 네트워크 resolve는 보조로만
RESOLVE_BUDGET_GENERAL = 40
RESOLVE_BUDGET_NEXON = 20

SLACK_TEXT_LIMIT = 3500
TITLE_MAX = 120
SNIPPET_MAX = 180
PREVIEW_TOP_N = 10

GAME_CONTEXT_OR = [
    "게임", "게이밍", "게임업계", "게임사", "퍼블리셔", "개발사",
    "모바일게임", "PC게임", "콘솔", "스팀", "Steam", "PS5", "플레이스테이션", "닌텐도", "Xbox",
    "RPG", "MMORPG", "FPS", "MOBA", "e스포츠", "esports"
]
GAME_CONTEXT_QUERY = "(" + " OR ".join(GAME_CONTEXT_OR) + ")"

NON_ARTICLE_HINTS = [
    "공략", "팁", "노하우", "질문", "q&a", "인증", "후기",
    "길드", "길드모집", "길드 모집", "모집", "클랜", "클랜모집",
    "파티", "고정팟", "팟구함",
    "거래", "나눔", "판매", "삽니다",
]

NEXON_TERMS = ["넥슨", "nexon", "넥슨코리아", "넥슨게임즈", "네오플", "넥슨네트웍스"]
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
def _clean(s: str) -> str:
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
            t = _clean(src.get("title", ""))
            if t:
                return t
    except Exception:
        pass
    return "NEWS"

def _looks_like_non_article(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(h.lower() in blob for h in NON_ARTICLE_HINTS)

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

def _is_google_url(url: str) -> bool:
    try:
        h = (urlparse(url).netloc or "").lower()
        return h in _GOOGLE_HOSTS or h.endswith(".google.com")
    except Exception:
        return True

# ==========================
# "기사 URL" 판정(원문 기준)
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

    # 인벤: board/ 제외 + webzine/news 는 news= 있어야 기사
    if host.endswith("inven.co.kr"):
        if "/board/" in path:
            return False
        if path.startswith("/webzine/news"):
            if "news" not in qs:
                return False
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
# 날짜 범위(주말/공휴일 누적)
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
# ✅ 핵심: RSS 엔트리 내부에서 원문 URL 추출
# - entry.links 안에 원문이 들어있는 경우가 많음(가장 가볍고 확실)
# - content/summary에 들어있는 URL도 보조로 긁음
# ==========================
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

def _pick_first_publisher_url(urls: List[str]) -> Optional[str]:
    for u in urls:
        if not u:
            continue
        try:
            u = u.split("&amp;")[0]
            u = unquote(u)
        except Exception:
            pass
        if _is_google_url(u):
            continue
        if any(site in u for site in TARGET_SITES):
            return u
    return None

def extract_publisher_url_from_entry(entry) -> Optional[str]:
    candidates: List[str] = []

    # 1) entry.links (가장 중요)
    links = getattr(entry, "links", None)
    if links and isinstance(links, list):
        for l in links:
            href = ""
            try:
                href = l.get("href", "") if isinstance(l, dict) else ""
            except Exception:
                href = ""
            if href:
                candidates.append(href)

    # 2) entry.source.href (대개 홈이지만 혹시 원문일 수 있어 보조로)
    try:
        src = getattr(entry, "source", None)
        if src and isinstance(src, dict):
            href = src.get("href", "")
            if href:
                candidates.append(href)
    except Exception:
        pass

    # 3) summary/content에 포함된 URL들
    for field in ("summary", "description"):
        txt = getattr(entry, field, "") or ""
        if txt:
            candidates += _URL_RE.findall(txt)

    content = getattr(entry, "content", None)
    if content and isinstance(content, list):
        for c in content:
            try:
                val = c.get("value", "") if isinstance(c, dict) else ""
            except Exception:
                val = ""
            if val:
                candidates += _URL_RE.findall(val)

    return _pick_first_publisher_url(candidates)

# ==========================
# 보조: 네트워크로 google 링크 해제(가능하면)
# ==========================
class UrlResolver:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        })
        self.cache: Dict[str, Optional[str]] = {}

    def try_resolve(self, google_url: str) -> Optional[str]:
        if not google_url:
            return None
        if google_url in self.cache:
            return self.cache[google_url]

        try:
            r = self.session.get(google_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
            final = r.url or ""
            if final and (not _is_google_url(final)) and any(s in final for s in TARGET_SITES):
                self.cache[google_url] = final
                return final
        except Exception:
            pass

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
        "publisher_embedded": 0,
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

                title = _truncate(_clean(getattr(e, "title", "")), TITLE_MAX)
                if not title:
                    continue

                google_link = _clean(getattr(e, "link", "") or "")
                if not google_link:
                    continue

                snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                snippet = _truncate(_clean(_strip_html(snippet_raw)), SNIPPET_MAX)

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

                # ✅ RSS 엔트리에서 원문 URL을 먼저 뽑아둔다
                embedded_pub = extract_publisher_url_from_entry(e)
                if embedded_pub:
                    stats["publisher_embedded"] += 1

                sid = _stable_id(title, google_link)
                if sid in found:
                    continue

                found[sid] = {
                    "track": track,
                    "keyword": kw,
                    "press": press,
                    "title": title,
                    "google_link": google_link,
                    "publisher_hint": embedded_pub,  # ✅ 여기!
                    "link": None,
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
# 원문 확정 + 기사 URL 필터
# ==========================
def finalize_items(items: List[Dict], resolver: UrlResolver, budget: int) -> Tuple[List[Dict], Dict[str, int]]:
    stats = {"resolved_ok": 0, "resolve_fail_drop": 0, "non_article_url_drop": 0, "from_embedded": 0, "from_network": 0}
    out: List[Dict] = []
    used = 0

    for a in items:
        pub_url = a.get("publisher_hint")

        # 1) RSS 내부 원문 URL이 있으면 그걸 쓴다(네트워크 0)
        if pub_url:
            if is_valid_article_url(pub_url):
                a["link"] = pub_url
                stats["resolved_ok"] += 1
                stats["from_embedded"] += 1
                out.append(a)
                continue
            else:
                stats["non_article_url_drop"] += 1
                continue

        # 2) 없으면 네트워크로 보조 해제(예산 내)
        if used >= budget:
            stats["resolve_fail_drop"] += 1
            continue

        pub_url = resolver.try_resolve(a["google_link"])
        used += 1

        if not pub_url:
            stats["resolve_fail_drop"] += 1
            continue

        if not is_valid_article_url(pub_url):
            stats["non_article_url_drop"] += 1
            continue

        a["link"] = pub_url
        stats["resolved_ok"] += 1
        stats["from_network"] += 1
        out.append(a)

    # 링크 기준 중복 제거
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
        f"added={stats['general_rss']['added']}, publisher_embedded={stats['general_rss']['publisher_embedded']}, "
        f"resolved_ok={stats['general_final']['resolved_ok']}, from_embedded={stats['general_final']['from_embedded']}, "
        f"from_network={stats['general_final']['from_network']}, resolve_fail_drop={stats['general_final']['resolve_fail_drop']}, "
        f"url_drop={stats['general_final']['non_article_url_drop']}\n"
    )
    header += (
        f"- nexon: feeds={stats['nexon_rss']['feeds_called']}, seen={stats['nexon_rss']['entries_seen']}, "
        f"added={stats['nexon_rss']['added']}, publisher_embedded={stats['nexon_rss']['publisher_embedded']}, "
        f"resolved_ok={stats['nexon_final']['resolved_ok']}, from_embedded={stats['nexon_final']['from_embedded']}, "
        f"from_network={stats['nexon_final']['from_network']}, resolve_fail_drop={stats['nexon_final']['resolve_fail_drop']}, "
        f"url_drop={stats['nexon_final']['non_article_url_drop']}\n\n"
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
    now_kst = datetime.now(KST)
    start_dt, end_dt, window_label = compute_window_kst(now_kst)

    # 쿼리용 after/before는 날짜만 넉넉히
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

    resolver = UrlResolver()
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
