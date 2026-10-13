"""索引的加密存储。

仓库是公开的，凡是会暴露页面清单的数据（状态、索引、截图、手调目录、页面标记）都只以密文落盘。
密钥 = PBKDF2-SHA256(密码, data/crypto.json 里的固定盐, 迭代次数)，每份密文各自随机 IV，AES-256-GCM。
盐固定是为了让浏览器「记住这台电脑」后，每次同步出的新索引仍能用同一把密钥打开。

密码从环境变量 INDEX_PASSWORD 读。改密码时把旧密码放进 INDEX_PASSWORD_OLD：读不开就用旧的再试，写回一律用新的。
"""
import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
ITERATIONS = 210_000


def _b64(raw):
    return base64.b64encode(raw).decode('ascii')


def _unb64(text):
    return base64.b64decode(text)


def _params():
    path = DATA / 'crypto.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    params = {'salt': _b64(os.urandom(16)), 'iter': ITERATIONS}
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(params), encoding='utf-8')
    return params


def derive(password, params):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_unb64(params['salt']), iterations=params['iter'])
    return kdf.derive(password.encode('utf-8'))


class Vault:
    """读写 data/ 下的密文 JSON；明文只在内存里。"""

    def __init__(self):
        password = os.environ.get('INDEX_PASSWORD')
        if not password:
            raise SystemExit('缺少环境变量 INDEX_PASSWORD，读写不了加密的索引数据')
        self.params = _params()
        self.key = derive(password, self.params)
        old = os.environ.get('INDEX_PASSWORD_OLD')
        self.old_key = derive(old, self.params) if old else None

    def seal(self, obj):
        iv = os.urandom(12)
        plain = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return {'v': 1, 'salt': self.params['salt'], 'iter': self.params['iter'],
                'iv': _b64(iv), 'ct': _b64(AESGCM(self.key).encrypt(iv, plain, None))}

    def open(self, box):
        for key in (self.key, self.old_key):
            if key is None:
                continue
            try:
                return json.loads(AESGCM(key).decrypt(_unb64(box['iv']), _unb64(box['ct']), None))
            except InvalidTag:
                continue
        raise SystemExit('密码不对，打不开加密数据（刚改过密码的话，把旧密码放进 INDEX_PASSWORD_OLD）')

    def load(self, name, default=None):
        path = DATA / name
        if not path.exists():
            return default
        return self.open(json.loads(path.read_text(encoding='utf-8')))

    def save(self, name, obj, volatile=()):
        """明文没变就不重写（密文每次 IV 不同，比较密文没有意义）。返回是否真的写了。"""
        path = DATA / name
        if path.exists():
            current = self.load(name)
            strip = (lambda o: {k: v for k, v in o.items() if k not in volatile}) if isinstance(obj, dict) else (lambda o: o)
            if isinstance(current, type(obj)) and strip(current) == strip(obj):
                return False
        DATA.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.seal(obj)), encoding='utf-8')
        return True
