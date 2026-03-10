# ------------------------------------------------------------------
# [운영용 v7]
# - "진짜 기사"만 + "진짜 원문 링크"만
# - Google News / googlesearch 완전 제거
# - 인벤: RSS(FeedBurner) 사용
# - 게임메카/게임플/게임톡: HTML 리스트에서 기사 URL 수집
# - 기사 검증: (1) URL 패턴 (도메인별) + (2) 제목 힌트
# - 기간: 최근 영업일 3일 00:00 ~ 최근 영업일 09:59:59
# - 디버깅 로그 추가:
#   * main 시작 시각
#   * 소스별 fetch 시작/종료 시각
#   * Slack 전송 시작/종료 시각
# - 키워드 확장:
#   * AI / IT / 게임업계 / 주요 타이틀 / 주요 기업
# - 넥슨 관련 기사 분류 개선:
#   * title + snippet 기반
#   * 점수 기반 관련도 산정
#   * 관련도 + 최신순 정렬
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
UTC = ZoneInfo("UTC")
USER_AGENT = "Mozilla/5.0 (GameNewsBot/7.0; +https://github.com/)"
TIMEOUT = 12

PRIMARY_KEYWORDS = [
    # 기존 산업/비즈니스
    "신작", "성과", "호재", "악재", "리스크", "정책", "업데이트", "출시",
    "매출", "순위", "소송", "규제", "CBT", "OBT", "인수", "투자", "M&A",
    "서비스 종료", "종료", "공정위", "과태료", "제재", "행정처분",
    "분쟁", "환불", "확률형", "표시광고", "제재금", "1위", "흥행",

    # AI / 최신 기술
    "AI", "인공지능", "생성형 AI", "AI NPC", "AI 캐릭터", "AI 번역", "AI 음성",
    "LLM", "대형언어모델", "GPT", "ChatGPT", "자동화", "클라우드",
    "엔진", "언리얼", "유니티", "그래픽스", "렌더링", "최적화",
    "멀티플랫폼", "크로스플레이",

    # 게임 산업 운영
    "사전예약", "런칭", "테스트", "얼리 액세스", "얼리액세스",
    "글로벌", "글로벌 출시", "퍼블리싱", "퍼블리셔", "신규 IP",
    "리메이크", "리마스터", "확장팩", "대규모 업데이트", "시즌 업데이트",
    "라이브 서비스", "서비스 개편",

    # 유저/지표
    "이용자", "동접", "DAU", "MAU", "트래픽",

    # 주요 기업
    "넥슨", "넷마블", "엔씨", "NC", "크래프톤", "카카오게임즈",
    "펄어비스", "네오위즈", "위메이드", "컴투스", "시프트업", "스마일게이트",
    "웹젠", "네오플",

    # 주요 게임 타이틀
    "메이플스토리", "메이플", "던전앤파이터", "던파", "FC 온라인", "FC온라인",
    "서든어택", "마비노기", "카트라이더", "카트라이더 드리프트",
    "리니지", "리니지M", "리니지W", "리니지2M",
    "로스트아크", "배틀그라운드", "PUBG", "검은사막", "아이온"
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

NEXON_TERMS = [
    "넥슨", "nexon",
    "넥슨코리아", "넥슨게임즈", "넥슨네트웍스", "네오플",
    "넥슨지티", "넥슨devcat", "민트로켓", "mintrocket",
    "데이브 더 다이버", "데이브더다이버",
    "퍼스트 디센던트", "퍼스트디센던트", "the first descendant",
    "메이플스토리", "메이플", "던전앤파이터", "던파",
    "마비노기", "서든어택", "카트라이더", "카트라이더 드리프트",
    "블루 아카이브", "블루아카이브", "바람의나라"
]


# ==========================
# 2026 KR 공휴일 (하드코딩)
# ==========================
KR_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 3, 1), date(2026, 3, 2),
    date(2026, 5, 5),
    date(2026, 5, 24), date(2026, 5, 25),
    date(2026, 6, 3),
    date(2026, 6, 6),
    date(2026, 8, 15), date(2026, 8, 17),
    date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 26),
    date(2026, 10, 3), date(2026, 10, 5),
    date(2026, 10, 9),
    date(2026, 12, 25),
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

def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d.year == 2026 and d in KR_HOLIDAYS_2026:
        return False
    return True

def compute_window(now_kst: datetime) -> Tuple[datetime, datetime]:
    """
    최근 영업일 3일 기준 윈도우
    예:
      - 월요일 실행 -> 목요일 00:00 ~ 월요일 09:59:59
      - 공휴일/주말 실행 -> 가장 최근 영업일을 end 기준일로 삼음
    """
    base_day = now_kst.date()
    while not is_business_day(base_day):
        base_day = base_day - timedelta(days=1)

    business_days = [base_day]
    cursor = base_day
    while len(business_days) < 3:
        cursor = cursor - timedelta(days=1)
        if is_business_day(cursor):
            business_days.append(cursor)

    oldest_day = business_days[-1]

    start = datetime(
        oldest_day.year, oldest_day.month, oldest_day.day,
        0, 0, 0, tzinfo=KST
    )
    end = datetime(
        base_day.year, base_day.month, base_day.day,
        9, 59, 59, tzinfo=KST
    )
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

def log_time(label: str) -> None:
    now_kst = datetime.now(KST)
    now_utc = datetime.now(UTC)
    print(f"[TIME] {label} | KST={now_kst.isoformat()} | UTC={now_utc.isoformat()}")

def extract_meta_description(html: str) -> str:
    patterns = [
        r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
        r'<meta[^>]+name="description"[^>]+content="([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return clean_text(strip_html(m.group(1)))
    return ""

def score_nexon_relevance(title: str, snippet: str) -> int:
    title_l = (title or "").lower()
    snippet_l = (snippet or "").lower()

    score = 0

    strong_terms = ["넥슨", "nexon", "넥슨코리아", "넥슨게임즈", "네오플"]
    if any(term.lower() in title_l for term in strong_terms):
        score += 5
    if any(term.lower() in snippet_l for term in strong_terms):
        score += 3

    if any(term.lower() in title_l for term in NEXON_TERMS):
        score += 3
    if any(term.lower() in snippet_l for term in NEXON_TERMS):
        score += 2

    issue_terms = [
        "출시", "신작", "업데이트", "매출", "흥행", "1위", "사전예약",
        "규제", "소송", "공정위", "과태료", "제재", "서비스 종료"
    ]
    if any(term in (title or "") for term in issue_terms):
        score += 2
    if any(term in (snippet or "") for term in issue_terms):
        score += 1

    return score

def contains_nexon(title: str, snippet: str) -> bool:
    return score_nexon_relevance(title, snippet) >= 3


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

    if "news.google.com" in host or "google.com" in host:
        return False

    if host.endswith("inven.co.kr"):
        if "/board/" in path:
            return False
        if path.startswith("/webzine/news"):
            if "news" not in qs:
                return False
        if "keyword" in qs and "news" not in qs:
            return False

    if host.endswith("gameple.co.kr"):
        if "/news/articleview.html" not in path:
            return False
        if "idxno" not in qs:
            return False

    if host.endswith("gametoc.co.kr"):
        if "/news/articleview.html" not in path:
            return False
        if "idxno" not in qs:
            return False

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

        if not is_valid_article_url(link):
            continue

        if looks_like_non_article(title):
            continue

        t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if not t:
            continue
        pub = datetime(*t[:6], tzinfo=UTC).astimezone(KST)

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

        snippet = ""
        try:
            rr = s.get(url, timeout=TIMEOUT)
            rr.raise_for_status()
            snippet = extract_meta_description(rr.text)[:180]
        except Exception:
            pass

        out.append(Article(
            press="게임메카",
            title=title[:140],
            url=url,
            published=pub,
            snippet=snippet,
        ))

        time.sleep(random.uniform(0.05, 0.12))
    return out


# ==========================
# 수집기 3) 게임플 홈(HTML)
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

        snippet = extract_meta_description(art_html)[:180]

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
            snippet=snippet,
        ))

        time.sleep(random.uniform(0.05, 0.12))
    return out


# ==========================
# 수집기 4) 게임톡 리스트(HTML)
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

        snippet = extract_meta_description(art_html)[:180]

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
            snippet=snippet,
        ))

        time.sleep(random.uniform(0.05, 0.12))
    return out


# ==========================
# 집계/필터/정렬
# ==========================
def keyword_filter(articles: List[Article], keywords: List[str]) -> List[Article]:
    out = []
    for a in articles:
        blob = f"{a.title} {a.snippet}".lower()
        if any(k.lower() in blob for k in keywords):
            out.append(a)
    return out

def dedup(articles: List[Article]) -> List[Article]:
    seen: Dict[str, Article] = {}
    for a in articles:
        sid = stable_id(a.title, a.url)
        seen[sid] = a
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

    body += "\n---\n### 🏢 넥슨 관련 주요 기사 (관련도 기반)\n"
    if not nexon:
        body += "- (없음)\n"
    else:
        for a in nexon[:NEXON_SEND_LIMIT]:
            body += fmt(a)

    full = header + body

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
    log_time("main_started")

    now = datetime.now(KST)
    start, end = compute_window(now)

    print(f"[INFO] now_kst={now.isoformat()}")
    print(f"[INFO] window: {start} ~ {end} (KST)")

    collected: List[Article] = []
    stats = {}

    try:
        log_time("fetch_inven_start")
        inv = fetch_inven_rss(start, end)
        log_time("fetch_inven_end")
        stats["inven"] = len(inv)
        collected.extend(inv)
    except Exception as e:
        print(f"[WARN] inven failed: {e}")
        stats["inven"] = 0

    try:
        log_time("fetch_gamemeca_start")
        gm = fetch_gamemeca_list(start, end)
        log_time("fetch_gamemeca_end")
        stats["gamemeca"] = len(gm)
        collected.extend(gm)
    except Exception as e:
        print(f"[WARN] gamemeca failed: {e}")
        stats["gamemeca"] = 0

    try:
        log_time("fetch_gameple_start")
        gp = fetch_gameple(start, end)
        log_time("fetch_gameple_end")
        stats["gameple"] = len(gp)
        collected.extend(gp)
    except Exception as e:
        print(f"[WARN] gameple failed: {e}")
        stats["gameple"] = 0

    try:
        log_time("fetch_gametoc_start")
        gt = fetch_gametoc(start, end)
        log_time("fetch_gametoc_end")
        stats["gametoc"] = len(gt)
        collected.extend(gt)
    except Exception as e:
        print(f"[WARN] gametoc failed: {e}")
        stats["gametoc"] = 0

    log_time("filter_start")
    filtered = keyword_filter(collected, PRIMARY_KEYWORDS)
    filtered = [a for a in filtered if is_valid_article_url(a.url)]
    general = dedup(filtered)

    nexon_candidates = [a for a in general if contains_nexon(a.title, a.snippet)]
    nexon = dedup(nexon_candidates)
    nexon = sorted(
        nexon,
        key=lambda a: (score_nexon_relevance(a.title, a.snippet), a.published),
        reverse=True
    )
    log_time("filter_end")

    print(f"[INFO] stats: {stats}")
    print(f"[INFO] collected={len(collected)} filtered={len(filtered)} general={len(general)} nexon={len(nexon)}")

    print("[INFO] preview general:")
    for a in general[:10]:
        print(" -", a.title, "::", a.url)

    print("[INFO] preview nexon:")
    for a in nexon[:10]:
        print(" -", a.title, f"(score={score_nexon_relevance(a.title, a.snippet)})", "::", a.url)

    msgs = build_slack_message(general, nexon, start, end)

    log_time("slack_send_start")
    for i, m in enumerate(msgs, 1):
        send_to_slack(m)
        print(f"[INFO] sent slack {i}/{len(msgs)}")
        time.sleep(0.2)
    log_time("slack_send_end")


if __name__ == "__main__":
    main()
