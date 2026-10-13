"""手调目录（分类、描述、隐藏名单）加密存在 data/catalog.enc，改它要先解出来、改完再封回去。

  INDEX_PASSWORD=... python scripts/catalog_tool.py export   # 解到 catalog.json（.gitignore 挡着，不会提交）
  INDEX_PASSWORD=... python scripts/catalog_tool.py import   # 把 catalog.json 加密写回 data/catalog.enc，并删掉明文
"""
import json
import sys
from pathlib import Path

from vault import Vault

PLAIN = Path(__file__).resolve().parent.parent / 'catalog.json'


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('export', 'import'):
        raise SystemExit(__doc__)
    vault = Vault()
    if sys.argv[1] == 'export':
        PLAIN.write_text(json.dumps(vault.load('catalog.enc'), ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'已解到 {PLAIN.name}，改完运行 import')
    else:
        vault.save('catalog.enc', json.loads(PLAIN.read_text(encoding='utf-8')))
        PLAIN.unlink()
        print('已加密写回 data/catalog.enc，明文已删')


if __name__ == '__main__':
    main()
