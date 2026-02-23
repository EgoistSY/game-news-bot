# ------------------------------------------------------------------
# [운영용 최종 v3] Google News RSS + 원본 링크 추출(HTML 파싱) + 본문 요약 + Slack 전송
# - Python 3.9 호환
# - 0건 방지: (1) redirect resolve (2) news.google HTML에서 원본 링크 추출 (3) 최후 폴백(필터 완화)
# - Actions 로그에 미리보기 출력(Top N)
# ------------------------------------------------------------------
import os
import re
import json
import time
import hashlib
import random
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse, parse_qs, urlunparse, unquote

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
MAX_ITEMS_PER_QUERY = 15   # 조금 늘림(0건 방지에 도움)

REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (NewsDigestBot/1.1; SlackWebhook)"
SLEEP_BETWEEN_REQUESTS = (0.15, 0.45)

SUMMARY_CHARS = 320
SLACK_TEXT_LIMIT = 3500

# Actions 로그 미리보기 출력 개수
PREVIEW_TOP_N = 20

# 리졸브 캐시
_RESOLVE_CACHE: Dict[str, str] = {}


# ==========================
# 유틸
# ==========================
def _sleep() -> None:
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

def _google_news_rss_url(keyword: str, site: str, days: int) -> str:
    # 따옴표는 결과를 줄일 수 있어 제거 + when:Nd 유지
    q = f"{keyword} site:{site} when:{days}d"
    return "https://news.google.com/rss/search?q=" + quote(q) + "&hl=ko&gl=KR&ceid=KR:ko"


def _extract_original_from_google_news_html(html: str) -> Optional[str]:
    """
    news.google.com/articles/... 페이지의 HTML에서 원본 기사 URL 추출 시도.
    (케이스에 따라 구조가 바뀌므로, 여러 힌트를 폭넓게 탐색)
    """
    soup = BeautifulSoup(html, "lxml")

    # 1) canonical / og:url
    for sel in [
        ("link", {"rel": "canonical"}, "href"),
        ("meta", {"property": "og:url"}, "content"),
    ]:
        tag = soup.find(sel[0], attrs=sel[1])
        if tag and tag.get(sel[2]):
            u = _clean_text(tag.get(sel[2]))
            if u and "news.google" not in u:
                return u

    # 2) a 태그 중 외부 https 링크 우선 탐색
    # 구글 뉴스 페이지는 외부 링크가 /articles/... 내부 라우팅이거나 google.com/url?q= 형태일 수 있음
    candidates: List[str] = []
    for a in soup.find_all("a", href=True):
        href = _clean_text(a["href"])
        if not href:
            continue

        # 상대경로면 스킵(원본 추출 목적)
        if href.startswith("/"):
            continue

        # google redirect 형식이면 q 파라미터를 파싱해서 원본으로
        href = _normalize_url(href)

        if href.startswith("http") and ("news.google" not in href):
            candidates.append(href)

    # 가장 그럴듯한(길이가 길고 외부) 링크를 반환
    if candidates:
        candidates = sorted(set(candidates), key=len, reverse=True)
        return candidates[0]

    return None


def resolve_final_url(raw_url: str, session: requests.Session, stats: Dict[str, int]) -> str:
    """
    1) allow_redirects로 최종 URL 추적
    2) 여전히 news.google이면 HTML을 GET해서 원본 기사 링크 추출
    """
    raw_url = _clean_text(raw_url)
    if not raw_url:
        return raw_url

    if raw_url in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[raw_url]

    url = _normalize_url(raw_url)
    final_url = url

    # 1) redirect 따라가기
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
        final_url = _normalize_url(resp.url)
        resp.close()
        stats["resolved_redirect"] += 1
    except Exception:
        final_url = url

    # 2) 최종이 여전히 Google News면 HTML 파싱으로 원본 링크 추출
    try:
        netloc = urlparse(final_url).netloc.lower()
        if "news.google" in netloc:
            stats["still_google_news_after_redirect"] += 1
            resp2 = session.get(final_url, timeout=REQUEST_TIMEOUT)
            resp2.raise_for_status()
            orig = _extract_original_from_google_news_html(resp2.text)
            if orig:
                final_url = _normalize_url(orig)
                stats["extracted_from_html"] += 1
    except Exception:
        pass

    _RESOLVE_CACHE[raw_url] = final_url
    return final_url


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
def fetch_articles() -> Tuple[List[Dict], Dict[str, int]]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    })

    articles: Dict[str, Dict] = {}
    stats = {
        "rss_entries_seen": 0,
        "resolved_redirect": 0,
        "still_google_news_after_redirect": 0,
        "extracted_from_html": 0,
        "date_filtered_out": 0,
        "domain_filtered_out": 0,
        "added": 0,
        "fallback_added_without_domain_match": 0,
    }

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

                    stats["rss_entries_seen"] += 1

                    title = _clean_text(getattr(entry, "title", ""))
                    raw_link = _clean_text(getattr(entry, "link", ""))
                    published_dt = _parse_published(entry)

                    if not title or not raw_link:
                        continue

                    if not _within_days(published_dt, SEARCH_DAYS):
                        stats["date_filtered_out"] += 1
                        continue

                    final_link = resolve_final_url(raw_link, session, stats)

                    # 도메인 필터: 원본 링크 기준
                    if site not in final_link:
                        stats["domain_filtered_out"] += 1
                        continue

                    sid = _stable_id(title, final_link)
                    if sid in articles:
                        continue

                    articles[sid] = {
                        "keyword": kw,
                        "press": _press_from_url(final_link),
                        "title": title,
                        "link": final_link,
                        "published_dt": published_dt,
                        "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                        "summary": "",
                    }
                    stats["added"] += 1
                    got += 1

                _sleep()

            except Exception as e:
                print(f"[WARN] RSS 실패 (kw={kw}, site={site}): {e}")
                continue

    # 0건이면: 최후 폴백 (도메인 필터를 완화해서라도 결과를 확보)
    # - 운영 요구가 “무조건 기사 보내기”라면, 0건은 실패이므로 최소한 RSS 결과라도 보내게 함
    if not articles:
        session2 = requests.Session()
        session2.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
        })

        for kw in PRIMARY_KEYWORDS[:8]:  # 폴백은 과도한 트래픽 방지 위해 일부 키워드만
            rss = "https://news.google.com/rss/search?q=" + quote(f"{kw} when:{SEARCH_DAYS}d") + "&hl=ko&gl=KR&ceid=KR:ko"
            try:
                resp = session2.get(rss, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)
                for entry in feed.entries[:25]:
                    title = _clean_text(getattr(entry, "title", ""))
                    raw_link = _clean_text(getattr(entry, "link", ""))
                    published_dt = _parse_published(entry)
                    if not title or not raw_link:
                        continue
                    if not _within_days(published_dt, SEARCH_DAYS):
                        continue

                    final_link = resolve_final_url(raw_link, session2, stats)
                    sid = _stable_id(title, final_link)
                    if sid in articles:
                        continue

                    articles[sid] = {
                        "keyword": kw,
                        "press": _press_from_url(final_link),
                        "title": title,
                        "link": final_link,
                        "published_dt": published_dt,
                        "published": published_dt.strftime("%Y-%m-%d %H:%M") if published_dt else "",
                        "summary": "",
                    }
                    stats["fallback_added_without_domain_match"] += 1

                _sleep()
            except Exception:
                continue

    # 요약 채우기
    for a in articles.values():
        a["summary"] = extract_summary(a["link"], session)
        _sleep()

    def sort_key(x: Dict) -> datetime:
        return x["published_dt"] if x.get("published_dt") else datetime.min

    return sorted(list(articles.values()), key=sort_key, reverse=True), stats


# ==========================
# Slack 메시지 생성/전송
# ==========================
def is_nexon(article: Dict) -> bool:
    blob = f"{article.get('title','')} {article.get('summary','')} {article.get('link','')}".lower()
    return ("넥슨" in blob) or ("nexon" in blob)

def build_messages(articles: List[Dict], stats: Dict[str, int]) -> List[str]:
    today_str = datetime.now().strftime("%Y-%m-%d")
    header = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {SEARCH_DAYS}일)\n"
    header += f"- stats: entries={stats.get('rss_entries_seen',0)}, added={stats.get('added',0)}, fallback={stats.get('fallback_added_without_domain_match',0)}\n"
    header += f"- resolve: redirect={stats.get('resolved_redirect',0)}, still_google={stats.get('still_google_news_after_redirect',0)}, html_extract={stats.get('extracted_from_html',0)}\n"
    header += f"- filtered: domain={stats.get('domain_filtered_out',0)}, date={stats.get('date_filtered_out',0)}\n\n"

    def fmt(a: Dict) -> str:
        pub = f" ({a['published']})" if a.get("published") else ""
        summ = f"\n    - {_truncate(a.get('summary',''), 500)}" if a.get("summary") else ""
        return f"▶ *[{a['press']}]* <{a['link']}|{a['title']}>{pub}{summ}\n"

    major = articles
    nexon = [a for a in articles if is_nexon(a)]

    body = "### 🌐 주요 게임업계 뉴스\n"
    if not major:
        body += f"- 최근 {SEARCH_DAYS}일간 뉴스가 없습니다.\n"
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
    articles, stats = fetch_articles()

    # Actions 로그 미리보기 (Slack 오기 전에도 결과 확인 가능)
    print(f"[INFO] fetched articles: {len(articles)}")
    print(f"[INFO] stats: {stats}")
    print("[INFO] preview:")
    for i, a in enumerate(articles[:PREVIEW_TOP_N], 1):
        print(f"  {i:02d}. [{a.get('press','')}] {a.get('title','')} :: {a.get('link','')}")

    # Slack 전송
    messages = build_messages(articles, stats)
    for i, msg in enumerate(messages, 1):
        send_to_slack_text(msg)
        print(f"[INFO] sent slack message {i}/{len(messages)}")
        time.sleep(0.4)

if __name__ == "__main__":
    main()
