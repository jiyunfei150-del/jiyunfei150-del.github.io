"""换密码：用旧密码解开 data/ 下所有密文，再用新密码重新加密写回（盐不变）。

  INDEX_PASSWORD=新密码 INDEX_PASSWORD_OLD=旧密码 python scripts/rekey.py

平时的 Vault.save 会在明文没变时跳过写入，换密码时那样会留下旧密钥的密文，所以这里无条件重写。
"""
import json

from vault import DATA, Vault


def main():
    vault = Vault()
    if vault.old_key is None:
        raise SystemExit('换密码需要同时给 INDEX_PASSWORD（新）和 INDEX_PASSWORD_OLD（旧）')
    names = sorted(p.name for p in DATA.iterdir() if p.name.endswith('.enc') or p.name.endswith('.enc.json'))
    for name in names:
        obj = vault.load(name)
        (DATA / name).write_text(json.dumps(vault.seal(obj)), encoding='utf-8')
    print(f'已用新密码重新加密 {len(names)} 个文件')


if __name__ == '__main__':
    main()
