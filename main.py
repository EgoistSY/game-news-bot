# -----------------------------------------------------
# GitHub Actions을 위한 최종 코드 (2026-02-20 버전)
# -----------------------------------------------------
import feedparser
import requests
import json
import os # 'os' 라이브러리를 불러옵니다.
from datetime import datetime, timedelta

# googlesearch-python 라이브러리가 필요합니다.
# pip install googlesearch-python
from googlesearch import search

# --- (1) 설정 부분 ---
# GitHub Actions의 'Secrets' 기능에서 웹훅 URL을 안전하게 가져옵니다.
SLACK_WEBHOOK_URL = os.environ['SLACK_WEBHOOK_URL']

NEWS_FEEDS = {
    "인벤": "https://www.inven.co.kr/webzine/rss.php",
    "게임메카": "http://www.gamemeca.com/rss/rss.xml",
    "디스이즈게임": "https://www.thisisgame.com/webzine/rss/nboard/11",
    "게임톡": "http://www.gametoc.co.kr/rss/S1N1.xml",
    "게임플": "https://www.gameple.co.kr/rss/all.xml",
    "ZDNetKorea": "https://www.zdnet.co.kr/Include/EgovRss.asp?cid=0020",
    "DigitalDaily": "http://www.ddaily.co.kr/rss.xml"
}

PRIMARY_KEYWORDS = [
    "신작", "성과", "호재", "악재", "리스크", "정책", "업데이트", "출시", 
    "매출", "순위", "소송", "규제", "CBT", "OBT", "인수", "투자", "M&A"
]

# --- (2) 코드 실행 부분 (수정 불필요) ---
def get_correct_link_from_google(title):
    try:
        yesterday = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        query = f'"{title}" after:{yesterday}'
        for link in search(query, tld="co.kr", num=1, stop=1, pause=2, lang="ko"):
            return link
    except Exception:
        return None
    return None

def find_all_news():
    yesterday_morning = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    
    all_important_articles = []
    nexon_specific_articles = []

    for press, feed_url in NEWS_FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                published_time = datetime(*entry.published_parsed[:6])
                if published_time < yesterday_morning:
                    continue

                title = entry.title
                content = title + entry.get('summary', '')
                
                if any(keyword in content for keyword in PRIMARY_KEYWORDS):
                    correct_link = get_correct_link_from_google(title)
                    if correct_link:
                        article_data = {"press": press, "title": title, "link": correct_link}
                        all_important_articles.append(article_data)
                        if '넥슨' in content:
                            nexon_specific_articles.append(article_data)
        except Exception:
            continue
            
    return all_important_articles, nexon_specific_articles

def create_report_message():
    all_articles, nexon_articles = find_all_news()
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    final_message = f"## 📰 {today_str} 게임업계 뉴스 브리핑\n\n"
    final_message += "### 🌐 주요 게임업계 뉴스\n"
    if not all_articles:
        final_message += "- 어제와 오늘, 지정된 키워드를 포함한 주요 뉴스가 없습니다.\n\n"
    else:
        for article in all_articles:
            final_message += f"▶ *[{article['press']}] {article['title']}*\n"
            final_message += f"   - 링크: <{article['link']}>\n"
        final_message += "\n"

    final_message += "---\n### 🏢 넥슨 관련 주요 뉴스\n"
    if not nexon_articles:
        final_message += "- 위 기사들 중, '넥슨'을 포함한 뉴스는 없습니다.\n"
    else:
        for article in nexon_articles:
            final_message += f"▶ *[{article['press']}] {article['title']}*\n"
            final_message += f"   - 링크: <{article['link']}>\n"
    
    return final_message

def send_to_slack(message):
    payload = {"text": message}
    headers = {"Content-Type": "application/json"}
    requests.post(SLACK_WEBHOOK_URL, data=json.dumps(payload), headers=headers)

if __name__ == '__main__':
    report = create_report_message()
    send_to_slack(report)

