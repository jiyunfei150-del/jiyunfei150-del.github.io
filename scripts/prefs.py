"""把页面上「同步到索引」提交的 issue 写进加密的 data/prefs.enc。

issue 是公开的，所以页面在浏览器里先用索引密钥把改动加密，正文只带一段 ```json {"enc": {"iv": ..., "ct": ...}} ```；
解开后是 {"ops": [{"op": "fav"|"hide"|"note", "k": "仓库/路径", "v": ...}]}，按顺序应用：
fav/hide 的 v 是布尔，note 的 v 是字符串（空串表示删掉备注）。
正文从环境变量 ISSUE_BODY 读，不拼进命令行。格式不对就以非零退出，issue 不会被关掉。
"""
import json
import os
import re
import sys

from vault import Vault

KEY_RE = re.compile(r'^[\w.\-/:]{1,200}$')
NOTE_MAX = 500


def parse_ops(body, vault):
    m = re.search(r'```json\s*(\{.*?\})\s*```', body or '', re.S)
    if not m:
        raise ValueError('issue 正文里没有 ```json 代码块')
    box = json.loads(m.group(1)).get('enc')
    if not isinstance(box, dict):
        raise ValueError('没有加密的改动（enc）')
    ops = vault.open(box).get('ops')
    if not isinstance(ops, list) or not ops:
        raise ValueError('ops 为空')
    clean = []
    for op in ops:
        kind, key, val = op.get('op'), op.get('k'), op.get('v')
        if kind not in ('fav', 'hide', 'note') or not isinstance(key, str) or not KEY_RE.match(key):
            raise ValueError('有一处改动格式不对')
        if kind == 'note':
            if not isinstance(val, str):
                raise ValueError('备注必须是字符串')
            val = val.strip()[:NOTE_MAX]
        elif not isinstance(val, bool):
            raise ValueError(f'{kind} 的值必须是 true/false')
        clean.append((kind, key, val))
    return clean


def apply(ops, vault):
    prefs = vault.load('prefs.enc') or {}
    for section in ('fav', 'hide', 'note'):
        prefs.setdefault(section, {})
    for kind, key, val in ops:
        if val:
            prefs[kind][key] = val
        else:
            prefs[kind].pop(key, None)
    vault.save('prefs.enc', {k: dict(sorted(v.items())) for k, v in prefs.items()})
    return prefs


def main():
    vault = Vault()
    try:
        ops = parse_ops(os.environ.get('ISSUE_BODY', ''), vault)
    except (ValueError, json.JSONDecodeError) as e:
        print(f'没有应用：{e}', file=sys.stderr)
        sys.exit(1)
    prefs = apply(ops, vault)
    # Actions 日志公开，只打数量
    print(f'应用 {len(ops)} 处改动；收藏 {len(prefs["fav"])}，删除 {len(prefs["hide"])}，备注 {len(prefs["note"])}', file=sys.stderr)


if __name__ == '__main__':
    main()
