#!/usr/bin/env python3
"""
한경 모닝루틴 자동 요약 스크립트
매일 오전 9시(KST) GitHub Actions에 의해 실행됨
"""
import os
import json
import re
import shutil
import time
from datetime import datetime, timezone, timedelta, date
import requests
from bs4 import BeautifulSoup
from groq import Groq
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build

# ── 환경변수 ──────────────────────────────────────────────
GROQ_API_KEY    = os.environ['GROQ_API_KEY']
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_API_KEY', '')
PLAYLIST_ID     = 'PLVups02-DZEWWyOMyk4jjGaWJ_0o1N1iO'
NTFY_TOPIC      = os.environ.get('NTFY_TOPIC', '')

KST  = timezone(timedelta(hours=9))
DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
}


def get_today_video():
    """오늘 날짜의 YouTube 영상 ID와 제목을 반환"""
    if not YOUTUBE_API_KEY:
        return None
    target_date = os.environ.get('TEST_DATE') or datetime.now(KST).strftime('%Y%m%d')
    youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
    response = youtube.playlistItems().list(
        part='snippet', playlistId=PLAYLIST_ID, maxResults=5
    ).execute()
    for item in response['items']:
        snippet = item['snippet']
        title   = snippet['title']
        if target_date in title:
            return {
                'video_id': snippet['resourceId']['videoId'],
                'title':    title,
            }
    return None


def get_articles():
    """hankyung.com/mr에서 날짜 버튼을 클릭해 해당 날짜의 기사 (제목, URL) 목록을 가져옴"""
    target_date = os.environ.get('TEST_DATE') or datetime.now(KST).strftime('%Y%m%d')
    td = datetime.strptime(target_date, '%Y%m%d')
    date_label = f"{td.month:02d}월 {td.day:02d}일"
    print(f'     대상 날짜: {target_date} ({date_label})')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS['User-Agent'])
        page.goto('https://www.hankyung.com/mr', wait_until='domcontentloaded', timeout=30000)

        # 날짜 버튼이 DOM에 나타날 때까지 대기
        page.wait_for_selector(f'text={date_label}', timeout=15000)
        page.get_by_text(date_label, exact=True).first.click()

        # 클릭 후 기사 링크가 갱신될 때까지 대기
        page.wait_for_selector('a[href*="/article/"]', timeout=15000)
        page.wait_for_timeout(2000)

        content = page.content()
        browser.close()

    soup = BeautifulSoup(content, 'html.parser')

    seen, articles = set(), []
    for a in soup.find_all('a', href=True):
        href = a['href']
        if not re.search(r'/article/\w+', href):
            continue
        if href.startswith('/'):
            href = 'https://www.hankyung.com' + href
        href = href.split('?')[0]
        if href in seen:
            continue

        title = a.get_text(strip=True)
        if not title:
            for tag in ['h1', 'h2', 'h3', 'h4']:
                parent = a.find_parent(tag)
                if parent:
                    title = parent.get_text(strip=True)
                    break
        if not title:
            continue

        seen.add(href)
        articles.append((title, href))

    print(f'     기사 {len(articles)}개 발견')
    return articles


def fetch_article(url):
    """기사 URL에서 본문을 추출"""
    res  = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(res.text, 'html.parser')

    body = (
        soup.find('div', class_=re.compile(r'article-body|newsct_article|article_txt|articleText', re.I)) or
        soup.find('div', class_=re.compile(r'article|content|body', re.I)) or
        soup.find('article')
    )
    if body:
        body_text = body.get_text(separator=' ', strip=True)
    else:
        paras = [p.get_text(strip=True) for p in soup.find_all('p') if len(p.get_text(strip=True)) > 30]
        body_text = ' '.join(paras)

    return re.sub(r'\s+', ' ', body_text) or '본문 없음'


def has_hallucination(content, body):
    """요약에 원문에 없는 한자·영어·일본어가 포함됐는지 검사"""
    if re.search(r'[぀-ヿ]', content):  # 일본어(히라가나·가타카나): 항상 오류
        return True
    if re.search(r'[Ѐ-ӿ]', content):  # 키릴 문자(러시아어 등): 항상 오류
        return True
    for ch in re.findall(r'[一-鿿㐀-䶿]', content):  # 한자: 원문에 없으면 오류
        if ch not in body:
            return True
    for word in re.findall(r'[a-zA-Z]{3,}', content):  # 영어 단어(3자+): 원문에 없으면 오류
        if word.lower() not in body.lower():
            return True
    return False


def summarize(articles_data):
    """기사별 개별 Groq 호출로 요약 (TPM 분산을 위해 호출 간 8초 대기)"""
    client = Groq(api_key=GROQ_API_KEY)
    items  = []

    for i, (title, body, url) in enumerate(articles_data):
        if i > 0:
            time.sleep(30)

        prompt = f"""다음은 한국경제신문 기사입니다.

제목: {title}
본문: {body}

아래 JSON 형식으로만 답하세요. 다른 텍스트는 절대 포함하지 마세요.

{{"content": "요약 내용"}}

규칙:
- content는 4~5문장으로 요약
- 첫 문장에 기사 제목을 그대로 반복하지 말 것. 제목에 없는 새로운 사실·수치·원인부터 시작할 것
- 구체적인 수치, 인물, 날짜, 원인을 반드시 포함할 것
- 반드시 제공된 기사 내용만 사용할 것. 기사에 없는 내용 추가 금지
- 문체는 '~다', '~했다' 형식의 신문 기사체로 통일
- 반드시 한국어로만 작성. 일본어·중국어·영어 등 다른 언어 절대 사용 금지
"""

        content = ''
        MAX_RETRIES = 2
        try:
            for attempt in range(MAX_RETRIES + 1):
                if attempt > 0:
                    print(f'       재시도 {attempt}/{MAX_RETRIES} (할루시네이션 감지)...')
                    time.sleep(15)
                response = client.chat.completions.create(
                    model='llama-3.3-70b-versatile',
                    max_tokens=400,
                    messages=[{'role': 'user', 'content': prompt}]
                )
                text  = response.choices[0].message.content.strip()
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if not match:
                    raise ValueError(f'JSON 파싱 실패: {text[:200]}')
                content = json.loads(match.group()).get('content', '')
                if not has_hallucination(content, body):
                    break
                if attempt == MAX_RETRIES:
                    print(f'       경고: 할루시네이션 제거 실패, 마지막 결과 사용')
            items.append({'title': title, 'content': content, 'url': url})
            print(f'     ✓ [{i+1}/{len(articles_data)}] {title[:35]}')
        except Exception as e:
            print(f'     ✗ [{i+1}/{len(articles_data)}] {title[:35]} — {e}')

    return {'items': items}


def save_json(summary, video, target_date):
    """요약 데이터를 data/YYYYMMDD.json으로 저장, index.json 업데이트"""
    td = datetime(
        int(target_date[:4]), int(target_date[4:6]), int(target_date[6:8]),
        tzinfo=KST
    )
    date_display = f"{td.strftime('%Y-%m-%d')} {DAYS[td.weekday()]}"

    data = {
        'date_display': date_display,
        'video_id':    video['video_id'] if video else '',
        'video_title': video['title']    if video else f"한경 모닝루틴 {td.strftime('%Y-%m-%d')}",
        'items':       [{'title': item['title'], 'content': item['content'], 'url': item.get('url', '')}
                        for item in summary['items']],
    }

    root     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.path.join(root, 'data')
    os.makedirs(data_dir, exist_ok=True)

    with open(os.path.join(data_dir, f'{target_date}.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'   → data/{target_date}.json 저장 완료')

    index_path = os.path.join(data_dir, 'index.json')
    index = []
    if os.path.exists(index_path):
        with open(index_path, encoding='utf-8') as f:
            index = json.load(f)
    if target_date not in index and td.weekday() < 5:  # 평일만 추가
        index.insert(0, target_date)
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False)
    print(f'   → data/index.json 업데이트 ({len(index)}개 날짜)')


def ensure_index_html():
    """index.html이 없으면 template.html에서 생성"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst  = os.path.join(root, 'index.html')
    if not os.path.exists(dst):
        shutil.copy(os.path.join(root, 'template.html'), dst)
        print('   → index.html 생성 완료')


def send_notification(date):
    """ntfy 푸시 알림 전송"""
    if not NTFY_TOPIC:
        return
    from urllib.parse import quote
    requests.post(
        f'https://ntfy.sh/{NTFY_TOPIC}',
        data=f'{date} 한경 모닝루틴 요약이 준비됐습니다'.encode('utf-8'),
        headers={'Title': quote('한경 모닝루틴'), 'Priority': 'default'},
        timeout=10
    )


def main():
    print('── 한경 모닝루틴 요약 시작 ──────────────')
    target_date = os.environ.get('TEST_DATE') or datetime.now(KST).strftime('%Y%m%d')

    print('[1/4] 기사 목록 가져오는 중...')
    articles = get_articles()
    if not articles:
        print('     기사를 찾을 수 없음. 종료합니다.')
        return

    print('[2/4] 각 기사 본문 수집 중...')
    articles_data = []
    for title, url in articles:
        try:
            body = fetch_article(url)
            articles_data.append((title, body, url))
            print(f'     ✓ {title[:40]}')
        except Exception as e:
            print(f'     ✗ {title[:40]} — {e}')

    if not articles_data:
        print('     기사 본문 수집 실패. 종료합니다.')
        return

    print('[3/4] AI 요약 생성 중...')
    summary = summarize(articles_data)
    print(f'     요약 완료: {len(summary["items"])}개 항목')

    print('[4/4] JSON 저장 및 알림 전송 중...')
    video = get_today_video()
    save_json(summary, video, target_date)
    ensure_index_html()
    send_notification(datetime.now(KST).strftime('%Y-%m-%d'))
    print('     완료!')

    print('── 모든 작업 완료 ───────────────────────')


if __name__ == '__main__':
    main()
