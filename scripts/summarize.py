#!/usr/bin/env python3
"""
한경 모닝루틴 자동 요약 스크립트
매일 오전 9시(KST) GitHub Actions에 의해 실행됨
"""
import os
import json
import re
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from groq import Groq
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
    """hankyung.com/mr에서 해당 날짜의 기사 (제목, URL) 목록을 가져옴"""
    target_date = os.environ.get('TEST_DATE') or datetime.now(KST).strftime('%Y%m%d')
    print(f'     대상 날짜: {target_date}')

    url  = f'https://www.hankyung.com/mr?date={target_date}'
    res  = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(res.text, 'html.parser')

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

        # 제목: 링크 텍스트 → 부모 heading 텍스트 순으로 시도
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

    og_desc = soup.find('meta', property='og:description')
    desc_text = og_desc['content'].strip() if og_desc and og_desc.get('content') else ''

    body = (
        soup.find('div', class_=re.compile(r'article|content|body', re.I)) or
        soup.find('article')
    )
    body_text = body.get_text(separator=' ', strip=True) if body else ''
    body_text = re.sub(r'\s+', ' ', body_text)[:800]

    return f"{desc_text} {body_text}".strip() or '본문 없음'


def summarize(articles_data):
    """Groq으로 기사 목록을 요약 — JSON 반환"""
    combined = '\n\n'.join(
        f'[기사 {i+1}] {title}\n{body}'
        for i, (title, body) in enumerate(articles_data)
    )

    prompt = f"""다음은 한국경제신문 모닝루틴 Pick 기사들입니다.

{combined}

아래 JSON 형식으로만 답하세요. 다른 텍스트는 절대 포함하지 마세요.

{{
  "items": [
    {{"title": "뉴스 항목 제목", "content": "요약 내용"}}
  ]
}}

규칙:
- items: 각 기사마다 하나의 항목, title은 반드시 제공된 [기사 N] 제목을 그대로 사용할 것 (절대 수정 금지), content는 4~5문장으로 요약
- 반드시 제공된 기사 내용만 사용할 것. 기사에 없는 내용 추가 금지
- 동일하거나 유사한 주제는 하나의 항목으로만 작성
- 문체는 '~다', '~했다' 형식의 신문 기사체로 통일
- 모든 내용은 한국어로 작성
"""

    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model='llama-3.3-70b-versatile',
        max_tokens=4096,
        messages=[{'role': 'user', 'content': prompt}]
    )

    text  = response.choices[0].message.content.strip()
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f'JSON 파싱 실패: {text[:200]}')
    result = json.loads(match.group())

    # 중복 제거
    seen, deduped = set(), []
    for item in result.get('items', []):
        key = re.sub(r'\s+', '', item['title'])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    # 원본 제목으로 강제 교체
    original_titles = [title for title, _ in articles_data]
    for i, item in enumerate(deduped):
        if i < len(original_titles):
            item['title'] = original_titles[i]

    result['items'] = deduped
    return result


def generate_html(summary, video=None):
    """template.html을 채워 index.html 생성"""
    kst_now      = datetime.now(KST)
    date_display = f"{kst_now.strftime('%Y-%m-%d')} {DAYS[kst_now.weekday()]}"

    items_html = ''.join(
        f'''<div class="accordion-item">
          <div class="accordion-header" onclick="toggle(this)">
            <span class="accordion-toggle">▶</span>
            <span class="accordion-num">{str(i+1).zfill(2)}</span>
            <span class="accordion-title">{item["title"]}</span>
            <button class="bookmark-btn" onclick="toggleBookmark(event, this, {json.dumps(item['title'], ensure_ascii=False)})">
              <svg viewBox="0 0 14 18"><path d="M1 1h12v16l-6-3-6 3V1z"/></svg>
            </button>
          </div>
          <div class="accordion-body">{item["content"]}</div>
        </div>'''
        for i, item in enumerate(summary['items'])
    )

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'template.html'), encoding='utf-8') as f:
        template = f.read()

    video_id    = video['video_id'] if video else ''
    video_title = video['title']   if video else f"한경 모닝루틴 {kst_now.strftime('%Y-%m-%d')}"

    html = template
    html = html.replace('{{DATE_DISPLAY}}',  date_display)
    html = html.replace('{{VIDEO_ID}}',      video_id)
    html = html.replace('{{VIDEO_TITLE}}',   video_title)
    html = html.replace('{{NEWS_ITEMS}}',    items_html)
    html = html.replace('{{TOTAL_COUNT}}',   str(len(summary['items'])))

    out = os.path.join(root, 'index.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f'   → {out} 저장 완료')


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
            articles_data.append((title, body))
            print(f'     ✓ {title[:40]}')
        except Exception as e:
            print(f'     ✗ {title[:40]} — {e}')

    if not articles_data:
        print('     기사 본문 수집 실패. 종료합니다.')
        return

    print('[3/4] AI 요약 생성 중...')
    summary = summarize(articles_data)
    print(f'     요약 완료: {len(summary["items"])}개 항목')

    print('[4/4] HTML 생성 및 알림 전송 중...')
    video = get_today_video()
    generate_html(summary, video)
    send_notification(datetime.now(KST).strftime('%Y-%m-%d'))
    print('     완료!')

    print('── 모든 작업 완료 ───────────────────────')


if __name__ == '__main__':
    main()
