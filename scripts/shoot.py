"""给 data/shots_todo.json 里的页面拍首屏缩略图，存成 thumbs/<sha12>.webp。

拍不了的记进 data/shot_failed.json，之后不再重试（页面内容变了 sha 会变，自然会重拍）。
"""
import io
import json
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DATA, THUMBS = ROOT / 'data', ROOT / 'thumbs'
VIEW = (1280, 800)
THUMB = (640, 400)


def main():
    todo = json.loads((DATA / 'shots_todo.json').read_text(encoding='utf-8'))
    failed_path = DATA / 'shot_failed.json'
    failed = set(json.loads(failed_path.read_text()) if failed_path.exists() else [])
    THUMBS.mkdir(exist_ok=True)
    ok = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={'width': VIEW[0], 'height': VIEW[1]}, locale='zh-CN',
                                  timezone_id='Asia/Shanghai', color_scheme='light')
        for t in todo:
            page = ctx.new_page()
            try:
                try:
                    page.goto(t['url'], wait_until='networkidle', timeout=40000)
                except Exception:
                    page.goto(t['url'], wait_until='load', timeout=40000)
                page.wait_for_timeout(1800)
                png = page.screenshot(type='png')
                img = Image.open(io.BytesIO(png)).convert('RGB').resize(THUMB, Image.LANCZOS)
                img.save(THUMBS / f'{t["sha"]}.webp', 'WEBP', quality=74, method=6)
                ok += 1
            except Exception as e:
                failed.add(t['sha'])
                print(f'截图失败 {t["sha"]}: {type(e).__name__}', file=sys.stderr)  # 日志公开，不打网址
            finally:
                page.close()
        browser.close()
    failed_path.write_text(json.dumps(sorted(failed)), encoding='utf-8')
    print(f'截图成功 {ok} 个，失败 {len(todo) - ok} 个', file=sys.stderr)


if __name__ == '__main__':
    main()
