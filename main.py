# ------------------------------------------------------------------
# [진짜 최종] 구글 직접 검색 방식으로 전면 수정한 코드 (2026-02-21)
# ------------------------------------------------------------------
import requests
import json
import os
from datetime import datetime, timedelta

# googlesearch-python 라이브러리가 필요합니다.
# pip install googlesearch-python
from googlesearch import search

# --- (1) 설정 부분 ---
SLACK_WEBHOOK_URL = os.environ['SLACK_WEBHOOK_URL']

# 검색할 웹사이트 목록 (RSS 주소가 아닌, 대표 도메인)
TARGET_SITES = [
    "inven.co.kr",
    "gamemeca.com",
    "thisisgame.com",
    "gametoc.co.kr",
    "gameple.co.kr",
    "zdnet.co.kr",
    "ddaily.co.kr"
]

PRIMARY_KEYWORDS = [
    "신작", "성과", "호재", "악재", "리스크", "정책", "업데이트", "출시", 
    "매출", "순위", "소송", "규제", "CBT", "OBT", "인수", "투자", "M&A"
]

# 검색할 기간 (일)
SEARCH_DAYS = 14

# --- (2) 새로운 코드 실행 부분 ---
def find_news_by_google():
    """구글 검색을 통해 직접 뉴스를 찾아내는 새로운 메인 함수"""
    start_date = (datetime.now() - timedelta(days=SEARCH_DAYS)).strftime('%Y-%m-%d')
    
    found_articles = {} # 중복 기사를 제거하기 위해 딕셔너리 사용

    # 1. 키워드별로 순회
    for keyword in PRIMARY_KEYWORDS:
        # 2. 웹사이트별로 순회
        for site in TARGET_SITES:
            try:
                # 3. 구글 검색어 조합: "키워드" site:사이트주소 after:날짜
                query = f'"{keyword}" site:{site} after:{start_date}'
                
                # 구글 검색 실행 (결과는 최대 5개로 제한, 너무 많아지는 것을 방지)
                for link in search(query, tld="co.kr", num=5, stop=5, pause=2, lang="ko"):
                    # 링크를 기반으로 기사 제목을 가져오려고 시도 (간단한 버전)
                    # 실제로는 더 복잡한 파싱이 필요하지만, 여기서는 링크 자체를 제목으로 활용
                    title_guess = link.split('/')[-1].replace('-', ' ').replace('_', ' ')
                    
                    # 중복된 링크가 아니면 추가
                    if link not in found_articles:
                        found_articles[link] = {
                            "press": site.split('.')[0].capitalize(), # 간단히 도메인 이름으로 언론사명 추정
                            "title": title_guess,
                            "link": link
                        }
            except Exception as e:
                print(f"구글 검색 중 오류 발생: {e}")
                continue
    
    # 딕셔너리의 값들(기사 정보)을 리스트로 변환
    all_important_articles = list(found_articles.values())
    
    # 그 중에서 넥슨 관련 기사 필터링
    nexon_specific_articles = [
        article for article in all_important_articles 
        if '넥슨' in article['title'] or 'nexon' in article['link']
    ]
            
    return all_important_articles, nexon_specific_articles

def create_report_message():
    all_articles, nexon_articles = find_news_by_google()
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    final_message = f"## 📰 {today_str} 게임업계 뉴스 브리핑 (최근 {SEARCH_DAYS}일, 구글 직접 검색)\n\n"
    final_message += "### 🌐 주요 게임업계 뉴스\n"
    if not all_articles:
        final_message += f"- 최근 {SEARCH_DAYS}일간, 지정된 키워드를 포함한 주요 뉴스가 없습니다.\n\n"
    else:
        for article in all_articles:
            final_message += f"▶ *[{article['press']}]* <{article['link']}|{article['title']}>\n"
        final_message += "\n"

    final_message += "---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not nexon_articles:
        final_message += "- 위 기사들 중, '넥슨'을 포함한 뉴스는 없습니다.\n"
    else:
        for article in nexon_articles:
            final_message += f"▶ *[{article['press']}]* <{article['link']}|{article['title']}>\n"
    
    return final_message

def send_to_slack(message):
    payload = {"text": message}
    headers = {"Content-Type": "application/json"}
    requests.post(SLACK_WEBHOOK_URL, data=json.dumps(payload), headers=headers)

if __name__ == '__main__':
    report = create_report_message()
    send_to_slack(report)
