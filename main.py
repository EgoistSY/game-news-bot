# ------------------------------------------------------------------
# [최적 운영용] Google News RSS + 본문 요약 + Slack 전송(자동 분할) (2026-02-23)
# - googlesearch-python 사용 안 함 (차단/0건 리스크 제거)
# - requirements.txt: requests, feedparser, beautifulsoup4, lxml, trafilatura
# ------------------------------------------------------------------
import os
import re
import json
import time
import hashlib
import random
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse, parse_qs, urlunparse

import requests
import feedparser
from bs4 import BeautifulSoup

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
MAX_ITEMS_PER_QUERY = 12

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (NewsDigestBot/1.0; SlackWebhook)"
SLEEP_BETWEEN_REQUESTS = (0.2, 0.6)

SUMMARY_CHARS = 320

# Slack 텍스트가 너무 길면 실패/잘림 위험 → 보수적으로 분할
SLACK_TEXT_LIMIT = 3500


# ==========================
# 유틸
# ==========================
def _sleep():
    time.sleep(random.uniform(*SLEEP_BETWEEN_REQUESTS))

def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"

def _normalize_url(raw_url: str) -> str:
    """
    - google.com/url?q=... 형태면 q에서 실 URL 복원
    - fragment 제거
    - utm_* 제거
    """
    if not raw_url:
        return raw_url

    # Google redirect URL 처리
    try:
        p = urlparse(raw_url)
        if p.netloc in ("www.google.com", "google.com") and p.path == "/url":
            q = parse_qs(p.query).get("q")
            if q and q[0]:
                raw_url = q[0]
    except Exception:
        pass

    # UTM 제거 + fragment 제거
    try:
        p = urlparse(raw_url)
        qs = parse_qs(p.query)
        for k in list(qs.keys()):
            if k.lower().startswith("utm_"):
                qs.pop(k, None)

        parts = []
        for k, vs in qs.items():
            for v in vs:
                parts.append(f"{k}={v}")
        new_query = "&".join(parts)

        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))
    except Exception:
        return raw_url

def _press_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower().replace("www.", "")
        return netloc.split(".")[0].upper() if netloc else "NEWS"
    except Exception:
        return "NEWS"

def _stable_id(title: str, link: str) -> str:
    return hashlib.sha1(f"{title}||{link}".encode("utf-8")).hexdigest()[:16]

def _parse_published(entry) -> datetime | None:
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    try:
        return datetime(*t[:6])
    except Exception:
        return None

def _within_days(dt: datetime | None, days: int) -> bool:
    if not dt:
        # 날짜가 없으면 포함(너무 엄격하면 결과가 0 될 수 있음)
        return True
    return dt >= (datetime.now() - timedelta(days=days))

def _google_news_rss_url(keyword: str, site: str, days: int) -> str:
    # when:Nd는 최근 N일 중심으로 결과를 안정적으로 끌어오는 편
    q = f'"{keyword}" site:{site} when:{days}d'
    return "https://news.google.com/rss/search?q=" + quote(q) + "&hl=ko&gl=KR&ceid=KR:ko"


# ==========================
# 본문 요약 추출
# ==========================
def extract_summary(url: str, session: requests.Session) -> str:
    """
    우선: trafilatura (설치되어 있으면)
    fallback: og/meta description + 첫 문단 조합
    """
    # 1) trafilatura (optional)
    try:
        import trafilatura  # type: ignore
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(
                downloaded,
                include_comments=False,
                include_tables=False,
                favor_recall=False,
            )
            text = _clean_text(text)
            if text:
                return _truncate(text, SUMMARY_CHARS)
    except Exception:
        pass

    # 2) BeautifulSoup fallback
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        desc = _clean_text(meta.get("content")) if meta and meta.get("content") else ""

        paras = []
        for p in soup.find_all("p"):
            t = _clean_text(p.get_text(" ", strip=True))
            if len(t) >= 35:
                paras.append(t)
            if len(" ".join(paras)) >= 900:
                break

        combined = _clean_text(" ".join([desc] + paras))
        return _truncate(combined, SUMMARY_CHARS) if combined else ""
    except Exception:
        return ""


# ==========================
# RSS 수집
# ==========================
def fetch_articles() -> list[dict]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    articles = {}  # sid -> article

    for kw in PRIMARY_KEYWORDS:
        for site in TARGET_SITES:
            rss = _google_news_rss_url(kw, site, SEARCH_DAYS)
            try:
                resp = session.get(rss, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)

                got = 0
                for entry in feed.entries:
                    if got >= MAX_ITEMS_PER_QUERY:
                        break

                    title = _clean_text(getattr(entry, "title", ""))
                    link = _normalize_url(_clean_text(getattr(entry, "link", "")))
                    published_dt = _parse_published(entry)

                    if not title or not link:
                        continue
                    # 안전망: 지정 도메인만
                    if site not in link:
                        continue
                    # 날짜 안전망
                    if not _within_days(published_dt, SEARCH_DAYS):
                        continue

                    sid = _stable_id(title, link)
                    if sid in articles:
                        continue

                    articles[sid] = {
                        "keyword": kw,
                        "press": _press_from_url(link),
                        "title": title,
                        "link": link,
                        "published_dt": published_dt,
                        "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                        "summary": "",
                    }
                    got += 1

                _sleep()
            except Exception as e:
                print(f"[WARN] RSS 실패 (kw={kw}, site={site}): {e}")
                continue

    # 요약 채우기
    for a in articles.values():
        a["summary"] = extract_summary(a["link"], session)
        _sleep()

    # 최신순 정렬 (published_dt 없는 건 뒤로)
    def sort_key(x: dict):
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(articles.values(), key=sort_key, reverse=True)


# ==========================
# Slack 메시지 생성/전송
# ==========================
def is_nexon(article: dict) -> bool:
    blob = f"{article.get('title','')} {article.get('summary','')} {article.get('link','')}".lower()
    return ("넥슨" in blob) or ("nexon" in blob)

def build_messages(articles: list[dict]) -> list[str]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    header = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {SEARCH_DAYS}일, Google News RSS)\n"
    header += f"- 대상 사이트: {', '.join(TARGET_SITES)}\n"
    header += f"- 키워드: {', '.join(PRIMARY_KEYWORDS)}\n\n"

    def fmt(a: dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        summ = f"\n    - {_truncate(a.get('summary',''), 500)}" if a.get("summary") else ""
        return f"▶ *[{a['press']}]* <{a['link']}|{a['title']}>{pub}{summ}\n"

    major = articles
    nexon = [a for a in articles if is_nexon(a)]

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not major:
        body += f"- 최근 {SEARCH_DAYS}일간, 지정 조건의 뉴스가 없습니다.\n"
    else:
        for a in major:
            body += fmt(a)

    body += "\n---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not nexon:
        body += "- '넥슨' 관련 기사(제목/요약/URL 기준)가 없습니다.\n"
    else:
        for a in nexon:
            body += fmt(a)

    full = header + body

    # Slack 길이 제한 대응: 라인 단위 분할
    messages = []
    chunk = ""
    for line in full.splitlines(True):
        if len(chunk) + len(line) > SLACK_TEXT_LIMIT:
            messages.append(chunk)
            chunk = ""
        chunk += line
    if chunk.strip():
        messages.append(chunk)

    return messages

def send_to_slack_text(message: str):
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

def main():
    articles = fetch_articles()
    print(f"[INFO] fetched articles: {len(articles)}")
    messages = build_messages(articles)

    for i, msg in enumerate(messages, 1):
        send_to_slack_text(msg)
        print(f"[INFO] sent slack message {i}/{len(messages)}")
        time.sleep(0.5)

if __name__ == "__main__":
    main()
