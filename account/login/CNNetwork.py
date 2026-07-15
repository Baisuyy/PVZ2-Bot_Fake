import json
import requests
from Crypto.Cipher import AES, DES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad
from base64 import b64decode, b64encode, urlsafe_b64decode, urlsafe_b64encode
from hashlib import md5
import binascii

from proxy_pool import 启用全局代理

启用全局代理()

DEFAULT_SECRET = "1geh6fvq4r20M02s"


def to_std_b64(text: str) -> str:
    text = text.replace("-", "+").replace("_", "/").replace(",", "=")
    return text + "=" * ((4 - len(text) % 4) % 4)


def from_std_b64(text: str) -> str:
    return text.replace("+", "-").replace("/", "_").replace("=", ",")


def key_iv(secret: str, req: str):
    key = md5((secret + req).encode("utf-8")).hexdigest().encode("ascii")
    iv = md5(key).hexdigest().encode("ascii")[:16]
    return key, iv


def aes_encrypt(plain: str | bytes, secret: str, req: str) -> str:
    if isinstance(plain, str):
        plain = plain.encode("utf-8")
    key, iv = key_iv(secret, req)
    data = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, AES.block_size))
    return from_std_b64(b64encode(data).decode("ascii"))


def aes_decrypt(e: str, secret: str, req: str) -> bytes:
    key, iv = key_iv(secret, req)
    data = b64decode(to_std_b64(e))
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    return unpad(plain, AES.block_size)


RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAw8N6nNnnW8diYTOj/vcB
8L2+3P9pHZ5ZTRkNRcRZ/1ItgTXlx5GX5ju8EgTxGiVWAl9920UlMGPgeBd+m4Jo
Baxc0uAGsNb/pPloydWoT4ntr5/+Hg9Q+EB2DkQi3JgUxyC/AjwB8odz4jOT85vy
fXmzrttg2W7cYoMTfBOjLJGqERZP47hjueiKusArsGaY4r1rWyShmorct0jDNQH6
tLj9fJLvwIgzKK002z9zke2DdXg52WcreailN1cf02cTHsOMwUQzSEL6h/K2J3xV
VgD53y9AF9kr0m0pdzaf6uxC1iDkp9fbVv97ZZlsOBB51EnxOBgLEZTB/ybg3nKU
4wIDAQAB
-----END PUBLIC KEY-----
"""


def rsa_encrypt_v202(plain: str | bytes) -> str:
    if isinstance(plain, str):
        plain = plain.encode("utf-8")
    key = RSA.import_key(RSA_PUBLIC_KEY)
    cipher = PKCS1_v1_5.new(key)
    block_size = key.size_in_bytes() - 11
    encrypted = b"".join(cipher.encrypt(plain[i:i + block_size]) for i in range(0, len(plain), block_size))
    return from_std_b64(b64encode(encrypted).decode("ascii"))


def decode_cloud_response(resp: dict, secret: str = DEFAULT_SECRET) -> dict:
    if "e" not in resp:
        return resp
    return json.loads(aes_decrypt(resp["e"], secret, resp["i"]))


def DES加解密(消息: str) -> str:
    密钥 = b'TwPay001'
    初始向量 = b'\x01\x02\x03\x04\x05\x06\x07\x08'
    try:
        binascii.unhexlify(消息)
        return DES解密(消息, 密钥, 初始向量)
    except (binascii.Error, ValueError):
        return DES加密(消息, 密钥, 初始向量)


def DES加密(明文: str, 密钥: bytes, 初始向量: bytes) -> str:
    明文 = 明文.encode('utf-8')
    密码器 = DES.new(密钥, DES.MODE_CBC, 初始向量)
    填充明文 = pad(明文, DES.block_size)
    密文 = 密码器.encrypt(填充明文)
    return binascii.hexlify(密文).decode('utf-8')


def DES解密(密文: str, 密钥: bytes, 初始向量: bytes) -> str:
    密文 = binascii.unhexlify(密文)
    密码器 = DES.new(密钥, DES.MODE_CBC, 初始向量)
    明文 = 密码器.decrypt(密文)
    return unpad(明文, DES.block_size).decode('utf-8')