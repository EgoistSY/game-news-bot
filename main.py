# ------------------------------------------------------------------
# [수정본] googlesearch-python advanced + tbs(cdr)로 제목/기간 정확도 개선 (2026-02-23)
# ------------------------------------------------------------------
import os
import json
import time
import random
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlunparse

# pip install googlesearch-python
from googlesearch import search

# --- (1) 설정 부분 ---
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")  # KeyError 방지

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
MAX_RESULTS_PER_QUERY = 5

# --- (2) 유틸 ---
def _normalize_url(raw_url: str) -> str:
    """
    Google 결과에 종종 섞이는 리다이렉트/트래킹/프래그먼트 제거.
    - https://www.google.com/url?q=... 형태면 q 파라미터를 실제 URL로 사용
    - fragment(#...) 제거
    - 흔한 UTM 제거
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

    try:
        p = urlparse(raw_url)
        qs = parse_qs(p.query)
        # UTM 제거
        for k in list(qs.keys()):
            if k.lower().startswith("utm_"):
                qs.pop(k, None)

        # query 재조립
        new_query = "&".join(
            f"{k}={v[0]}" if len(v) == 1 else "&".join([f"{k}={x}" for x in v])
            for k, v in qs.items()
        )
        cleaned = urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))  # fragment 제거
        return cleaned
    except Exception:
        return raw_url

def _press_from_url(url: str) -> str:
    """도메인에서 언론사/매체 라벨 생성(간단 버전)."""
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.replace("www.", "")
        # 예: zdnet.co.kr -> zdnet
        base = netloc.split(".")[0]
        return base.upper() if base else "NEWS"
    except Exception:
        return "NEWS"

def _build_tbs_custom_range(start_dt: datetime, end_dt: datetime) -> str:
    """
    Google 검색 tbs 커스텀 기간:
    tbs=cdr:1,cd_min:MM/DD/YYYY,cd_max:MM/DD/YYYY
    (googlesearch-python이 tbs를 그대로 전달할 수 있음) :contentReference[oaicite:2]{index=2}
    """
    cd_min = start_dt.strftime("%m/%d/%Y")
    cd_max = end_dt.strftime("%m/%d/%Y")
    return f"cdr:1,cd_min:{cd_min},cd_max:{cd_max}"

# --- (3) 메인 로직 ---
def find_news_by_google():
    now = datetime.now()
    start_dt = now - timedelta(days=SEARCH_DAYS)
    end_dt = now

    tbs = _build_tbs_custom_range(start_dt, end_dt)

    found_articles = {}  # normalized_url -> article

    for keyword in PRIMARY_KEYWORDS:
        for site in TARGET_SITES:
            query = f'"{keyword}" site:{site}'
            try:
                # advanced=True면 title/url/description을 받음 :contentReference[oaicite:3]{index=3}
                # tbs로 기간 강제 :contentReference[oaicite:4]{index=4}
                results = search(
                    query,
                    lang="ko",
                    advanced=True,
                    tbs=tbs,
                    num=MAX_RESULTS_PER_QUERY,
                    stop=MAX_RESULTS_PER_QUERY,
                    pause=random.uniform(2.0, 3.5),
                )

                for r in results:
                    # r: SearchResult (title, url, description) :contentReference[oaicite:5]{index=5}
                    url = _normalize_url(getattr(r, "url", "") or "")
                    if not url:
                        continue

                    # 도메인 필터(안전망)
                    if site not in url:
                        continue

                    title = (getattr(r, "title", "") or "").strip()
                    desc = (getattr(r, "description", "") or "").strip()

                    # 제목이 비어있으면(가끔 있음) 마지막 fallback으로 URL 조각 사용
                    if not title:
                        title = urlparse(url).path.strip("/").split("/")[-1].replace("-", " ").replace("_", " ")

                    if url not in found_articles:
                        found_articles[url] = {
                            "press": _press_from_url(url),
                            "title": title,
                            "link": url,
                            "desc": desc,
                        }

                # (선택) 쿼리 사이 약간 쉬어주기(차단/429 방지)
                time.sleep(random.uniform(0.3, 0.8))

            except Exception as e:
                print(f"[WARN] 구글 검색 오류 (keyword={keyword}, site={site}): {e}")
                continue

    all_articles = list(found_articles.values())

    # 넥슨 관련: title/desc/url 모두에서 탐지 (기존보다 정확)
    def is_nexon(a):
        blob = f"{a.get('title','')} {a.get('desc','')} {a.get('link','')}".lower()
        return ("넥슨" in blob) or ("nexon" in blob)

    nexon_articles = [a for a in all_articles if is_nexon(a)]
    return all_articles, nexon_articles

def create_report_message():
    all_articles, nexon_articles = find_news_by_google()
    today_str = datetime.now().strftime("%Y-%m-%d")

    msg = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {SEARCH_DAYS}일, Google 검색)\n\n"

    msg += "### 🌐 주요 게임업계 뉴스\n"
    if not all_articles:
        msg += f"- 최근 {SEARCH_DAYS}일간, 지정된 키워드를 포함한 주요 뉴스가 없습니다.\n\n"
    else:
        for a in all_articles:
            # Slack 링크 포맷: <url|text>
            msg += f"▶ *[{a['press']}]* <{a['link']}|{a['title']}>\n"
        msg += "\n"

    msg += "---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not nexon_articles:
        msg += "- 위 기사들 중, '넥슨' 관련 키워드를 포함한 뉴스는 없습니다.\n"
    else:
        for a in nexon_articles:
            msg += f"▶ *[{a['press']}]* <{a['link']}|{a['title']}>\n"

    return msg

def send_to_slack(message: str):
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("환경변수 SLACK_WEBHOOK_URL이 설정되어 있지 않습니다.")

    payload = {"text": message}
    headers = {"Content-Type": "application/json"}
    resp = requests.post(SLACK_WEBHOOK_URL, data=json.dumps(payload), headers=headers, timeout=15)
    resp.raise_for_status()

if __name__ == "__main__":
    report = create_report_message()
    send_to_slack(report)
