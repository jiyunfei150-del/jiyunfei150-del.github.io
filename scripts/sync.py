"""扫描 OWNER 名下所有公开仓库里的 HTML 页面，增量维护 data/state.json，并生成站点用的 data/index.json。

用法：
  python scripts/sync.py            # 联网增量扫描 + 生成索引
  python scripts/sync.py --offline  # 不联网，只按现有 state 重新生成索引（截图完成后用）

需要环境变量 GITHUB_TOKEN（Actions 自带）或 GH_TOKEN。
在 GitHub Actions 里运行时会向 $GITHUB_OUTPUT 写 need_shots=true/false。
"""
import base64
import concurrent.futures as cf
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vault import Vault

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
THUMBS = ROOT / 'thumbs'
OWNER = os.environ.get('OWNER', 'jiyunfei150-del')
SELF_REPO = f'{OWNER}.github.io'
TOKEN = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN') or ''
BJ = timezone(timedelta(hours=8))
TEXT_CAP = 60000
NEW_DAYS = 7


# ---------- GitHub API ----------

def api(path):
    """GET 一个 API 路径；404/409（空仓库）返回 None，其余错误重试 3 次后抛出。"""
    url = path if path.startswith('http') else f'https://api.github.com/{path}'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'page-index-sync',
               'X-GitHub-Api-Version': '2022-11-28'}
    if TOKEN:
        headers['Authorization'] = f'Bearer {TOKEN}'
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                return None
            if e.code == 403 and '/pages' in url:
                return None
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'GitHub API 失败：{path}：{last}')


def list_repos():
    out, page = [], 1
    while True:
        batch = api(f'users/{OWNER}/repos?per_page=100&type=owner&page={page}') or []
        out += batch
        if len(batch) < 100:
            return out
        page += 1


# ---------- 文本抽取 ----------

def title_of(s):
    m = re.search(r'<title[^>]*>(.*?)</title>', s, re.S | re.I)
    return re.sub(r'\s+', ' ', htmllib.unescape(m.group(1))).strip() if m else ''


def _strip(s, sep):
    s = re.sub(r'(?is)<(script|style|noscript|template)[^>]*>.*?</\1>', sep, s)
    s = re.sub(r'(?s)<!--.*?-->', sep, s)
    s = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h[1-6]|section|header|td|tr)>', '\n', s)
    return htmllib.unescape(re.sub(r'(?s)<[^>]+>', sep, s))


def visible_text(s):
    return re.sub(r'\s+', ' ', _strip(s, ' ')).strip()


def script_strings(s):
    """JS 渲染的页面正文藏在脚本字符串里，抽含中文的字面量补进检索。"""
    out = []
    for p in re.findall(r'(?is)<script[^>]*>(.*?)</script>', s):
        if len(p) > 2_000_000:
            continue
        for m in re.findall(r'''(["'`])((?:(?!\1)[^\\\n]|\\.){2,400}?)\1''', p):
            v = m[1]
            if re.search(r'[一-鿿]', v):
                out.append(re.sub(r'<[^>]+>|\$\{[^}]*\}', ' ', v))
    t = ' '.join(out)
    t = re.sub(r'\{/\*.*?\*/\}|className=|<\w+|[<>{}]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def auto_desc(s, title):
    """新页面没有手写描述时，取 meta description，否则取正文里第一句像样的中文说明。"""
    m = re.search(r'''<meta[^>]+(?:name|property)=["'](?:og:)?description["'][^>]*content=["']([^"']{8,200})''', s, re.I)
    if m:
        return htmllib.unescape(m.group(1)).strip()
    tnorm = norm(title)
    body = re.search(r'(?is)<body[^>]*>(.*)', s)
    for line in _strip(body.group(1) if body else s, '\n').split('\n'):
        line = re.sub(r'\s+', ' ', line).strip(' ·•|-—')
        lnorm = norm(line)
        if not 12 <= len(line) <= 160 or len(re.findall(r'[一-鿿]', line)) < 6:
            continue
        if not lnorm or lnorm in tnorm or (tnorm and tnorm in lnorm and len(lnorm) - len(tnorm) < 8):
            continue
        if re.match(r'(?i)(with|select|from|http|[A-Za-z]\(|\d{4}[-./])', line):
            continue
        return line[:90] + ('…' if len(line) > 90 else '')
    return ''


def norm(t):
    return re.sub(r'[\s·•\-—_|｜:：()（）【】\[\]<>《》,.，。、!！?？"“”\'‘’]+', '', t.lower())


def extract(raw):
    s = raw.decode('utf-8', errors='replace')
    stripped = s.lstrip('﻿ \r\n\t')
    title = title_of(s)
    text = (visible_text(s) + ' ' + script_strings(s)).strip()[:TEXT_CAP]
    return {'title': title, 'text': text, 'desc_auto': auto_desc(s, title),
            'stub': stripped.startswith('{') and '"deploy_id"' in stripped[:600]}


# ---------- 增量扫描 ----------

def pages_info(repo):
    if not repo.get('has_pages'):
        return None
    p = api(f'repos/{OWNER}/{repo["name"]}/pages')
    if p and p.get('html_url'):
        src = (p.get('source') or {}).get('path') or '/'
        return {'url': p['html_url'].rstrip('/') + '/', 'path': src}
    return {'url': f'https://{OWNER}.github.io/{repo["name"]}/', 'path': '/'}


def scan_repo(repo, old):
    name, branch = repo['name'], repo.get('default_branch') or 'main'
    entry = {'pushed_at': repo['pushed_at'], 'created_at': repo['created_at'], 'branch': branch,
             'description': repo.get('description') or '',
             'pages': old.get('pages') if old.get('pages') else pages_info(repo), 'files': {}}
    tree = api(f'repos/{OWNER}/{name}/git/trees/{urllib.parse.quote(branch)}?recursive=1')
    old_files = old.get('files', {})
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    for t in (tree or {}).get('tree', []):
        if t['type'] != 'blob' or not t['path'].lower().endswith(('.html', '.htm')):
            continue
        path, prev = t['path'], old_files.get(t['path'])
        if prev and prev['sha'] == t['sha']:
            entry['files'][path] = prev
            continue
        c = api(f'repos/{OWNER}/{name}/commits?path={urllib.parse.quote(path)}&sha={urllib.parse.quote(branch)}&per_page=1')
        b = api(f'repos/{OWNER}/{name}/git/blobs/{t["sha"]}')
        if not b:
            continue
        info = extract(base64.b64decode(b['content']))
        info.update({'sha': t['sha'], 'size': t.get('size', 0),
                     'date': c[0]['commit']['committer']['date'] if c else repo['pushed_at'],
                     'first_seen': prev['first_seen'] if prev else (old.get('scanned_at') and now) or repo['created_at']})
        entry['files'][path] = info
    entry['scanned_at'] = now
    return name, entry


def scan(state):
    repos = [r for r in list_repos() if r['name'] != SELF_REPO and not r.get('fork')]
    old = state.get('repos', {})
    todo = [r for r in repos if old.get(r['name'], {}).get('pushed_at') != r['pushed_at']]
    fresh = {r['name']: old[r['name']] for r in repos if r['name'] in old and r not in todo}
    with cf.ThreadPoolExecutor(6) as ex:
        for name, entry in ex.map(lambda r: scan_repo(r, old.get(r['name'], {})), todo):
            fresh[name] = entry
    print(f'仓库 {len(repos)} 个，本次重扫 {len(todo)} 个', file=sys.stderr)
    return {'repos': dict(sorted(fresh.items()))}


# ---------- 生成索引 ----------

def page_url(repo, path, info):
    pg = info.get('pages')
    if pg:
        src = pg['path'].strip('/')
        if not src or path.startswith(src + '/'):
            rel = path[len(src) + 1:] if src else path
            rel = re.sub(r'(^|/)index\.html?$', r'\1', rel)
            return pg['url'] + rel
    return f'https://github.com/{OWNER}/{repo}/blob/{info["branch"]}/{path}'


def shingles(t, k=3):
    t = ''.join(re.findall(r'[一-鿿A-Za-z0-9]+', t))[:20000]
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def similar(a, b):
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / max(1, len(sa | sb))


def auto_cat(cat_rules, title, text):
    """先只看标题，标题判不出再看正文开头；正文里“Generated by 小D”这类页脚不算数。"""
    text = re.sub(r'Generated by[^|]{0,40}', ' ', text[:3000])
    for hay in (title, text):
        for cat, pat in cat_rules:
            if re.search(pat, hay, re.I):
                return cat
    return 'misc'


def build(state, catalog):
    over, hide = catalog.get('pages', {}), catalog.get('hide', {})
    pages, hidden, dups = [], {}, 0
    for repo, info in state['repos'].items():
        for path, f in info['files'].items():
            key = f'{repo}/{path}'
            reason = hide.get(key) or hide.get(repo)
            if not reason and f.get('stub'):
                reason = '迁移失败的空壳页，打开只显示部署元数据'
            elif not reason and re.search(r'模板|【替换', f['title']):
                reason = '未填内容的模板'
            elif not reason and len(f['text']) < 40:
                reason = '几乎没有内容'
            if reason:
                hidden.setdefault(reason, []).append(key)
                continue
            pages.append({'key': key, 'repo': repo, 'path': path, 'url': page_url(repo, path, info),
                          'repo_desc': info.get('description') or '', **f})
    # 逐字节相同只留最新
    by_sha = {}
    for p in sorted(pages, key=lambda p: p['date'], reverse=True):
        if p['sha'] in by_sha:
            dups += 1
            continue
        by_sha[p['sha']] = p
    # 同一页面的多个版本：catalog 指定的 family 优先，否则按归一化标题
    fams = {}
    for p in by_sha.values():
        fam = over.get(p['key'], {}).get('family') or 't:' + norm(p['title'] or p['key'])
        fams.setdefault(fam, []).append(p)
    items = []
    for members in fams.values():
        members.sort(key=lambda p: p['date'], reverse=True)
        kept = []
        for p in members:
            if any(similar(p['text'], k['text']) >= 0.9 for k in kept):
                dups += 1
                continue
            kept.append(p)
        head = kept[0]
        o = next((over[p['key']] for p in kept if p['key'] in over and ('cat' in over[p['key']] or 'desc' in over[p['key']])), {})
        thumb = f'thumbs/{head["sha"][:12]}.webp'
        first_seen = min(p['first_seen'] for p in kept)
        items.append({
            'k': head['key'], 't': head['title'] or head['path'], 'u': head['url'], 'r': head['repo'], 'p': head['path'],
            'c': o.get('cat') or auto_cat(catalog['auto_cat'], head['title'], head['text']),
            # 页面里抽不出说明时用仓库描述兜底（DataClaw 批量建的仓库描述都是同一句，不用）
            'desc': o.get('desc') or head['desc_auto'] or ('' if head['repo_desc'] == 'DataClaw flowcharts' else head['repo_desc']),
            'd': head['date'], 'fs': first_seen,
            'g': f'https://github.com/{OWNER}/{head["repo"]}',
            'img': thumb if (ROOT / thumb).exists() else None, 'sha': head['sha'],
            'v': [{'k': p['key'], 't': p['title'], 'd': p['date'], 'u': p['url'], 'r': p['repo']} for p in kept[1:]],
            'ks': [p['key'] for p in kept],
            'x': head['text'],
        })
    items.sort(key=lambda i: i['d'], reverse=True)
    total = sum(len(i['files']) for i in state['repos'].values())
    folded = sum(len(i['v']) for i in items) + dups
    cats = {k: v for k, v in catalog['cats'].items() if any(i['c'] == k for i in items)}
    grouped = {c for g in catalog.get('groups', []) for c in g['cats']}
    groups = [{'name': g['name'], 'cats': [c for c in g['cats'] if c in cats]} for g in catalog.get('groups', [])]
    if set(cats) - grouped:  # 没归进任何大块的分类兜底放最后
        groups.append({'name': '其他', 'cats': [c for c in cats if c not in grouped]})
    return {
        'owner': OWNER, 'cats': cats, 'new_days': NEW_DAYS,
        'groups': [g for g in groups if g['cats']],
        'stats': {'total': total, 'shown': len(items), 'folded': folded, 'hidden': sum(len(v) for v in hidden.values())},
        'items': items,
        'omitted': [{'why': k, 'list': sorted(v)} for k, v in hidden.items()],
    }


def load_prefs(vault):
    """用户在页面上点的收藏 / 删除 / 备注，经 issue 同步后加密存在 data/prefs.enc。"""
    prefs = vault.load('prefs.enc') or _plain(DATA / 'prefs.json') or {}
    return {k: prefs.get(k, {}) for k in ('fav', 'hide', 'note')}


def _plain(path):
    """上锁前留下的明文文件，只在迁移那一次读。"""
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def apply_prefs(index, prefs):
    """按页面所有版本的 key 匹配，换了新版本收藏和备注也不丢；删掉的页面留在数据里带 hid 标记，页面上可以恢复。"""
    for it in index['items']:
        ks = it.get('ks') or [it['k']]
        it['fav'] = any(prefs['fav'].get(k) for k in ks)
        it['hid'] = any(prefs['hide'].get(k) for k in ks)
        it['note'] = next((prefs['note'][k] for k in ks if prefs['note'].get(k)), '')
    index['stats']['shown'] = sum(1 for it in index['items'] if not it['hid'])
    index['stats']['mine_hidden'] = sum(1 for it in index['items'] if it['hid'])
    return index


def main():
    """仓库是公开的：状态、目录、标记、截图、索引都只以密文进 data/；thumbs/ 和 data/*.json 明文只在运行时存在（.gitignore 挡住）。"""
    offline = '--offline' in sys.argv
    vault = Vault()
    DATA.mkdir(exist_ok=True)
    THUMBS.mkdir(exist_ok=True)
    state = vault.load('state.enc') or _plain(DATA / 'state.json') or {'repos': {}}
    catalog = vault.load('catalog.enc') or _plain(ROOT / 'catalog.json')
    vault.save('catalog.enc', catalog)
    # 截图以 {sha12: base64} 存在密文里，运行时展开成文件给截图和建索引用
    for sha, b64 in (vault.load('thumbs.enc') or {}).items():
        f = THUMBS / f'{sha}.webp'
        if not f.exists():
            f.write_bytes(base64.b64decode(b64))
    if not offline:
        state = scan(state)
    failed_file = DATA / 'shot_failed.json'
    failed = set(state.get('shot_failed', [])) | set(_plain(failed_file) or [])
    state['shot_failed'] = sorted(failed)
    vault.save('state.enc', state)
    index = apply_prefs(build(state, catalog), load_prefs(vault))
    # 截图：只给主条目拍，失败过的不再重试；不再引用的旧截图删掉
    todo = [{'sha': i['sha'][:12], 'url': i['u']} for i in index['items']
            if not i['img'] and i['sha'][:12] not in failed and i['u'].startswith(f'https://{OWNER}.github.io/')]
    (DATA / 'shots_todo.json').write_text(json.dumps(todo, ensure_ascii=False), encoding='utf-8')
    keep = {i['sha'][:12] for i in index['items']}
    for f in THUMBS.glob('*.webp'):
        if f.stem not in keep:
            f.unlink()
    vault.save('thumbs.enc', {f.stem: base64.b64encode(f.read_bytes()).decode() for f in sorted(THUMBS.glob('*.webp'))})
    for it in index['items']:  # 截图直接嵌进加密索引，不单独公开
        if it['img']:
            it['img'] = 'data:image/webp;base64,' + base64.b64encode((ROOT / it['img']).read_bytes()).decode()
    index['updated_at'] = datetime.now(BJ).strftime('%Y-%m-%d %H:%M')
    changed = vault.save('index.enc.json', index, volatile=('updated_at',))
    # Actions 日志是公开的，只打数量，不打页面名和网址
    print(f'展示 {index["stats"]["shown"]} 个，折叠 {index["stats"]["folded"]} 个，未收录 {index["stats"]["hidden"]} 个，'
          f'待截图 {len(todo)} 个，索引{"有" if changed else "无"}变化', file=sys.stderr)
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a', encoding='utf-8') as fh:
            fh.write(f'need_shots={"true" if todo else "false"}\n')


if __name__ == '__main__':
    main()
