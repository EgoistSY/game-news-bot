# ------------------------------------------------------------------
# [운영용 v5] "진짜 기사"만 + "진짜 원문 링크"만 + KST 10시 기준 전 영업일 윈도우
# - Google News / googlesearch 완전 제거 (news.google.com 링크 원천 차단)
# - 인벤: RSS(FeedBurner) 사용
# - 게임메카/게임플/게임톡: HTML 리스트에서 기사 URL 수집
# - 기사 검증: (1) URL 패턴 (도메인별) + (2) 제목 힌트
# - 기간: "전 영업일 00:00 ~ 오늘 09:59" (주말/공휴일 롤백)
#
# requirements.txt:
#   requests
#   feedparser
# ------------------------------------------------------------------

import os
import re
import json
import time
import random
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta, date
from urllib.parse import urlparse, parse_qs, urljoin

import requests
import feedparser
from zoneinfo import ZoneInfo

# ==========================
# 설정
# ==========================
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

KST = ZoneInfo("Asia/Seoul")
USER_AGENT = "Mozilla/5.0 (GameNewsBot/5.0; +https://github.com/)"
TIMEOUT = 12

PRIMARY_KEYWORDS = [
    "신작", "성과", "호재", "악재", "리스크", "정책", "업데이트", "출시",
    "매출", "순위", "소송", "규제", "CBT", "OBT", "인수", "투자", "M&A"
]

# Slack 출력 제한
GENERAL_SEND_LIMIT = 40
NEXON_SEND_LIMIT = 5
SLACK_TEXT_LIMIT = 3500

# 인벤 RSS (FeedBurner)
INVEN_RSS = "https://feeds.feedburner.com/inven"

# HTML 리스트 소스
GAMEMECA_LIST = "https://www.gamemeca.com/news.php"
GAMEPLE_HOME = "https://www.gameple.co.kr/"
GAMETOC_LIST = "https://www.gametoc.co.kr/news/articleList.html?view_type=sm"

# 기사 아닌 글 힌트(제목 기반)
NON_ARTICLE_TITLE_HINTS = [
    "공략", "팁", "노하우", "질문", "Q&A", "인증", "후기", "스샷", "스크린샷",
    "길드", "길드모집", "모집", "파티", "팟", "고정팟", "클랜", "클랜모집",
    "거래", "나눔", "판매", "삽니다", "버그제보", "건의", "토론",
]

NEXON_TERMS = ["넥슨", "nexon", "넥슨코리아", "넥슨게임즈", "네오플", "넥슨네트웍스"]


# ==========================
# 2026 KR 공휴일 (하드코딩, 외부 라이브러리 불필요)
# 출처: VisitKorea 2026 Public Holidays 표 기반
# ==========================
KR_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),  # 설날
    date(2026, 3, 1), date(2026, 3, 2),  # 삼일절(+대체)
    date(2026, 5, 5),  # 어린이날
    date(2026, 5, 24), date(2026, 5, 25),  # 부처님오신날(+대체)
    date(2026, 6, 3),  # 지방선거
    date(2026, 6, 6),  # 현충일
    date(2026, 8, 15), date(2026, 8, 17),  # 광복절(+대체)
    date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),  # 추석
    date(2026, 10, 3), date(2026, 10, 5),  # 개천절(+대체)
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25),  # 성탄절
}

# ==========================
# 유틸
# ==========================
def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def stable_id(title: str, link: str) -> str:
    return hashlib.sha1((title + "||" + link).encode("utf-8")).hexdigest()[:16]

def looks_like_non_article(title: str) -> bool:
    t = (title or "").lower()
    return any(h.lower() in t for h in NON_ARTICLE_TITLE_HINTS)

def contains_nexon(title: str, snippet: str) -> bool:
    blob = f"{title} {snippet}".lower()
    return any(t.lower() in blob for t in NEXON_TERMS)

def is_business_day(d: date) -> bool:
    # 월(0)~금(4) and not holiday
    if d.weekday() >= 5:
        return False
    if d.year == 2026 and d in KR_HOLIDAYS_2026:
        return False
    return True

def compute_window(now_kst: datetime) -> Tuple[datetime, datetime]:
    """
    실행 시각이 (대체로) KST 10:00이라고 가정.
    윈도우: 전 영업일 00:00 ~ 오늘 09:59:59
    단, 오늘이 영업일이 아니면 오늘도 롤백해서 '마지막 영업일'의 다음날 09:59까지로 잡는다.
    """
    # 오늘 09:59:59 (KST)
    end = now_kst.replace(hour=9, minute=59, second=59, microsecond=0)

    # 오늘이 영업일이 아니면 end 자체를 "영업일 다음날 09:59"로 맞추기 위해
    # now_kst의 날짜를 영업일이 될 때까지 뒤로 민다.
    base_day = now_kst.date()
    while not is_business_day(base_day):
        base_day = base_day - timedelta(days=1)

    # end를 base_day의 "다음날 09:59"로 보정 (즉, base_day 커버 끝)
    end = datetime(base_day.year, base_day.month, base_day.day, 9, 59, 59, tzinfo=KST)
    # 전 영업일 찾기
    prev_bd = base_day - timedelta(days=1)
    while not is_business_day(prev_bd):
        prev_bd = prev_bd - timedelta(days=1)

    start = datetime(prev_bd.year, prev_bd.month, prev_bd.day, 0, 0, 0, tzinfo=KST)
    return start, end

def in_window(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return start <= dt <= end

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })
    return s


# ==========================
# 도메인별 "진짜 기사 URL" 검증
# ==========================
def is_valid_article_url(url: str) -> bool:
    if not url:
        return False

    p = urlparse(url)
    host = (p.netloc or "").lower()
    path = (p.path or "").lower()
    qs = parse_qs(p.query or "")

    # Google / 외부 중간 링크 차단
    if "news.google.com" in host or "google.com" in host:
        return False

    # Inven: /board/ 는 무조건 게시판
    if host.endswith("inven.co.kr"):
        if "/board/" in path:
            return False
        # webzine news인데 news 파라미터 없는 검색/목록은 제외
        if path.startswith("/webzine/news"):
            if "news" not in qs:
                return False
        # keyword만 있는 페이지 제외
        if "keyword" in qs and "news" not in qs:
            return False

    # Gameple: 기사 URL은 /news/articleView.html?idxno= 가 사실상 정답
    if host.endswith("gameple.co.kr"):
        if "/news/articleview.html" not in path:
            return False
        if "idxno" not in qs:
            return False

    # Gametoc: 기사 URL은 /news/articleView.html?idxno=
    if host.endswith("gametoc.co.kr"):
        if "/news/articleview.html" not in path:
            return False
        if "idxno" not in qs:
            return False

    # Gamemeca: 기사 URL은 /view.php?gid=...
    if host.endswith("gamemeca.com"):
        if "/view.php" not in path:
            return False
        if "gid" not in qs:
            return False

    return True


# ==========================
# 데이터 구조
# ==========================
@dataclass
class Article:
    press: str
    title: str
    url: str
    published: datetime
    snippet: str = ""
    keyword: str = ""

def to_kst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


# ==========================
# 수집기 1) 인벤 RSS
# ==========================
def fetch_inven_rss(start: datetime, end: datetime) -> List[Article]:
    s = http_session()
    r = s.get(INVEN_RSS, timeout=TIMEOUT)
    r.raise_for_status()
    feed = feedparser.parse(r.text)

    out: List[Article] = []
    for e in feed.entries[:200]:
        title = clean_text(getattr(e, "title", ""))
        link = clean_text(getattr(e, "link", ""))
        if not title or not link:
            continue

        # RSS에서 들어오는 link가 인벤 기사/뉴스가 아닐 수도 있어서 URL 필터
        if not is_valid_article_url(link):
            continue

        if looks_like_non_article(title):
            continue

        t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if not t:
            continue
        pub = datetime(*t[:6], tzinfo=ZoneInfo("UTC")).astimezone(KST)

        if not in_window(pub, start, end):
            continue

        snippet_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        snippet = clean_text(strip_html(snippet_raw))[:180]

        out.append(Article(
            press="인벤",
            title=title[:140],
            url=link,
            published=pub,
            snippet=snippet,
        ))
    return out


# ==========================
# 수집기 2) 게임메카 리스트(HTML)
# ==========================
_GAMEMECA_ITEM_RE = re.compile(
    r'href="(?P<href>/view\.php\?gid=\d+)"[^>]*>(?P<title>[^<]+)</a>.*?\n.*?(?P<dt>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2})',
    re.DOTALL
)

def fetch_gamemeca_list(start: datetime, end: datetime) -> List[Article]:
    s = http_session()
    r = s.get(GAMEMECA_LIST, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text

    out: List[Article] = []
    for m in _GAMEMECA_ITEM_RE.finditer(html):
        href = m.group("href")
        title = clean_text(m.group("title"))
        dt_str = m.group("dt")

        if not title:
            continue
        if looks_like_non_article(title):
            continue

        url = urljoin("https://www.gamemeca.com", href)
        if not is_valid_article_url(url):
            continue

        try:
            pub = datetime.strptime(dt_str, "%Y.%m.%d %H:%M").replace(tzinfo=KST)
        except Exception:
            continue

        if not in_window(pub, start, end):
            continue

        out.append(Article(
            press="게임메카",
            title=title[:140],
            url=url,
            published=pub,
        ))
    return out


# ==========================
# 수집기 3) 게임플 홈(HTML) → articleView 링크 추출 후 기사 페이지에서 입력시간 파싱
# ==========================
_GAMEPLE_LINK_RE = re.compile(r'href="(?P<href>/news/articleView\.html\?idxno=\d+)"')
_GAMEPLE_TIME_RE = re.compile(r"입력\s*(\d{4}\.\d{2}\.\d{2})\s*(\d{2}:\d{2})")

def fetch_gameple(start: datetime, end: datetime) -> List[Article]:
    s = http_session()
    r = s.get(GAMEPLE_HOME, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text

    hrefs = list({m.group("href") for m in _GAMEPLE_LINK_RE.finditer(html)})[:60]
    out: List[Article] = []

    for href in hrefs:
        url = urljoin(GAMEPLE_HOME, href)
        if not is_valid_article_url(url):
            continue

        # 기사 페이지에서 시간/제목 확보
        try:
            rr = s.get(url, timeout=TIMEOUT)
            rr.raise_for_status()
            art_html = rr.text
        except Exception:
            continue

        # 제목(og:title 우선)
        title = ""
        og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', art_html)
        if og:
            title = clean_text(og.group(1))
        if not title:
            # h 태그 fallback
            h = re.search(r"<h\d[^>]*>([^<]+)</h\d>", art_html)
            if h:
                title = clean_text(h.group(1))

        if not title or looks_like_non_article(title):
            continue

        tm = _GAMEPLE_TIME_RE.search(art_html)
        if not tm:
            continue
        dt_str = tm.group(1) + " " + tm.group(2)
        try:
            pub = datetime.strptime(dt_str, "%Y.%m.%d %H:%M").replace(tzinfo=KST)
        except Exception:
            continue

        if not in_window(pub, start, end):
            continue

        out.append(Article(
            press="게임플",
            title=title[:140],
            url=url,
            published=pub,
        ))

        time.sleep(random.uniform(0.05, 0.12))  # 경량 예의상 슬립
    return out


# ==========================
# 수집기 4) 게임톡 리스트(HTML) → articleView 링크 추출 + 기사 페이지에서 입력시간 파싱
# (403이 있을 수 있어 UA 헤더로 시도)
# ==========================
_GAMETOC_LINK_RE = re.compile(r'href="(?P<href>/news/articleView\.html\?idxno=\d+)"')
_GAMETOC_TIME_RE = re.compile(r"입력\s*(\d{4}\.\d{2}\.\d{2})\s*(\d{2}:\d{2})")

def fetch_gametoc(start: datetime, end: datetime) -> List[Article]:
    s = http_session()
    try:
        r = s.get(GAMETOC_LIST, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception:
        return []

    html = r.text
    hrefs = list({m.group("href") for m in _GAMETOC_LINK_RE.finditer(html)})[:60]
    out: List[Article] = []

    for href in hrefs:
        url = urljoin("https://www.gametoc.co.kr", href)
        if not is_valid_article_url(url):
            continue

        try:
            rr = s.get(url, timeout=TIMEOUT)
            rr.raise_for_status()
            art_html = rr.text
        except Exception:
            continue

        title = ""
        og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', art_html)
        if og:
            title = clean_text(og.group(1))
        if not title:
            h = re.search(r"<h\d[^>]*>([^<]+)</h\d>", art_html)
            if h:
                title = clean_text(h.group(1))

        if not title or looks_like_non_article(title):
            continue

        tm = _GAMETOC_TIME_RE.search(art_html)
        if not tm:
            continue
        dt_str = tm.group(1) + " " + tm.group(2)
        try:
            pub = datetime.strptime(dt_str, "%Y.%m.%d %H:%M").replace(tzinfo=KST)
        except Exception:
            continue

        if not in_window(pub, start, end):
            continue

        out.append(Article(
            press="게임톡",
            title=title[:140],
            url=url,
            published=pub,
        ))

        time.sleep(random.uniform(0.05, 0.12))
    return out


# ==========================
# 집계/필터/정렬
# ==========================
def keyword_filter(articles: List[Article], keywords: List[str]) -> List[Article]:
    out = []
    for a in articles:
        blob = (a.title or "")
        if any(k.lower() in blob.lower() for k in keywords):
            out.append(a)
    return out

def dedup(articles: List[Article]) -> List[Article]:
    seen: Dict[str, Article] = {}
    for a in articles:
        sid = stable_id(a.title, a.url)
        seen[sid] = a
    # 최신순
    return sorted(seen.values(), key=lambda x: x.published, reverse=True)

def build_slack_message(general: List[Article], nexon: List[Article], start: datetime, end: datetime) -> List[str]:
    header = (
        f"## 📰 주요 게임업계 뉴스 브리핑\n"
        f"- window: {start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')} (KST)\n\n"
    )

    def fmt(a: Article) -> str:
        pub = a.published.strftime("%Y-%m-%d %H:%M")
        return f":arrow_forward: [{a.press}] {a.title} ({pub})\n    - <{a.url}|링크>\n"

    body = "### 🌐 주요 기사\n"
    if not general:
        body += "- (없음)\n"
    else:
        for a in general[:GENERAL_SEND_LIMIT]:
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 기사 (정확매칭: 제목/요약에 넥슨 포함)\n"
    if not nexon:
        body += "- (없음)\n"
    else:
        for a in nexon[:NEXON_SEND_LIMIT]:
            body += fmt(a)

    full = header + body

    # 슬랙 길이 분할
    msgs: List[str] = []
    chunk = ""
    for line in full.splitlines(True):
        if len(chunk) + len(line) > SLACK_TEXT_LIMIT:
            msgs.append(chunk)
            chunk = ""
        chunk += line
    if chunk.strip():
        msgs.append(chunk)
    return msgs

def send_to_slack(text: str) -> None:
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL env is missing")
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        data=json.dumps({"text": text}),
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()


# ==========================
# Main
# ==========================
def main():
    now = datetime.now(KST)
    start, end = compute_window(now)

    print(f"[INFO] window: {start} ~ {end} (KST)")

    # 1) 소스별 수집
    collected: List[Article] = []
    stats = {}

    try:
        inv = fetch_inven_rss(start, end)
        stats["inven"] = len(inv)
        collected.extend(inv)
    except Exception as e:
        print(f"[WARN] inven failed: {e}")
        stats["inven"] = 0

    try:
        gm = fetch_gamemeca_list(start, end)
        stats["gamemeca"] = len(gm)
        collected.extend(gm)
    except Exception as e:
        print(f"[WARN] gamemeca failed: {e}")
        stats["gamemeca"] = 0

    try:
        gp = fetch_gameple(start, end)
        stats["gameple"] = len(gp)
        collected.extend(gp)
    except Exception as e:
        print(f"[WARN] gameple failed: {e}")
        stats["gameple"] = 0

    try:
        gt = fetch_gametoc(start, end)
        stats["gametoc"] = len(gt)
        collected.extend(gt)
    except Exception as e:
        print(f"[WARN] gametoc failed: {e}")
        stats["gametoc"] = 0

    # 2) 키워드 필터(너가 원래 원했던 “게임업계 핵심 이슈”만 남김)
    filtered = keyword_filter(collected, PRIMARY_KEYWORDS)

    # 3) URL 최종 검증 + 중복 제거
    filtered = [a for a in filtered if is_valid_article_url(a.url)]
    general = dedup(filtered)

    # 4) 넥슨: “키워드 + 넥슨(정확매칭)” 교집합
    nexon_candidates = [a for a in general if contains_nexon(a.title, a.snippet)]
    nexon = dedup(nexon_candidates)

    print(f"[INFO] stats: {stats}")
    print(f"[INFO] general={len(general)} nexon={len(nexon)}")
    print("[INFO] preview general:")
    for a in general[:10]:
        print(" -", a.title, "::", a.url)
    print("[INFO] preview nexon:")
    for a in nexon[:10]:
        print(" -", a.title, "::", a.url)

    # 5) Slack 전송
    msgs = build_slack_message(general, nexon, start, end)
    for i, m in enumerate(msgs, 1):
        send_to_slack(m)
        print(f"[INFO] sent slack {i}/{len(msgs)}")
        time.sleep(0.2)

if __name__ == "__main__":
    main()
