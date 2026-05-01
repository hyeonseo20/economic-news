#!/usr/bin/env python3
"""
한경 모닝루틴 자동 요약 스크립트
매일 오전 9시(KST) GitHub Actions에 의해 실행됨
"""
import os
import json
import re
from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build
from google import genai
import requests

# ── 환경변수 ──────────────────────────────────────────────
YOUTUBE_API_KEY = os.environ['YOUTUBE_API_KEY']
GEMINI_API_KEY  = os.environ['GEMINI_API_KEY']
PLAYLIST_ID     = 'PLVups02-DZEWWyOMyk4jjGaWJ_0o1N1iO'
NTFY_TOPIC      = os.environ.get('NTFY_TOPIC', '')

KST = timezone(timedelta(hours=9))
DAYS = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


def get_today_video():
    """오늘 날짜의 영상을 플레이리스트에서 찾아 반환"""
    youtube = build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
    today   = os.environ.get('TEST_DATE') or datetime.now(KST).strftime('%Y%m%d')

    response = youtube.playlistItems().list(
        part='snippet',
        playlistId=PLAYLIST_ID,
        maxResults=5
    ).execute()

    for item in response['items']:
        snippet = item['snippet']
        title   = snippet['title']
        if today in title:
            return {
                'video_id': snippet['resourceId']['videoId'],
                'title':    title,
                'date':     datetime.now(KST).strftime('%Y-%m-%d'),
            }
    return None


def summarize(video_id, video_title):
    """Gemini AI로 YouTube 영상을 직접 요약 — JSON 반환"""
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""다음은 한국경제신문 뉴스 영상입니다.
영상 제목: {video_title}

아래 JSON 형식으로만 답하세요. 다른 텍스트는 절대 포함하지 마세요.

{{
  "brief": ["핵심 한 줄 요약 1", "핵심 한 줄 요약 2", "핵심 한 줄 요약 3"],
  "items": [
    {{"title": "뉴스 항목 제목", "content": "2~3문단 상세 설명"}}
  ]
}}

규칙:
- brief: 오늘 영상에서 가장 중요한 뉴스 3가지, 각 20자 이내
- items: 영상에서 다룬 주요 뉴스 5~10개, content는 2~3문단
- 모든 내용은 한국어로 작성
- 문체는 '~다', '~했다', '~없다' 형식의 신문 기사체로 통일
- 동일하거나 유사한 주제의 뉴스를 중복 포함하지 말 것. 같은 기업/사건은 하나의 항목으로만 작성
"""

    response = client.models.generate_content(
        model='gemini-2.5-flash-lite',
        contents=[
            genai.types.Part.from_uri(
                file_uri=f'https://www.youtube.com/watch?v={video_id}',
                mime_type='video/mp4'
            ),
            prompt
        ]
    )

    text  = response.text.strip()
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        raise ValueError(f'JSON 파싱 실패: {text[:200]}')
    return json.loads(match.group())


def generate_html(video_info, summary):
    """template.html을 채워 index.html 생성"""
    kst_now       = datetime.now(KST)
    date_display  = f"{kst_now.strftime('%Y-%m-%d')} {DAYS[kst_now.weekday()]}"
    video_id      = video_info['video_id']

    brief_html = ''.join(
        f'<li><span class="brief-num">{str(i+1).zfill(2)}</span>{item}</li>'
        for i, item in enumerate(summary['brief'])
    )

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

    html = template
    html = html.replace('{{DATE_DISPLAY}}',  date_display)
    html = html.replace('{{VIDEO_ID}}',      video_id)
    html = html.replace('{{VIDEO_TITLE}}',   video_info['title'])
    html = html.replace('{{BRIEF_ITEMS}}',   brief_html)
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

    print('[1/4] 오늘 영상 확인 중...')
    video = get_today_video()
    if not video:
        print('     오늘 업로드된 영상 없음. 종료합니다.')
        return
    print(f'     발견: {video["title"]} ({video["video_id"]})')

    print('[2/4] AI 요약 생성 중...')
    summary = summarize(video['video_id'], video['title'])
    print(f'     요약 완료: {len(summary["items"])}개 항목')

    print('[3/4] HTML 생성 중...')
    generate_html(video, summary)

    print('[4/4] 푸시 알림 전송 중...')
    send_notification(video['date'])
    print('     완료!')

    print('── 모든 작업 완료 ───────────────────────')


if __name__ == '__main__':
    main()
