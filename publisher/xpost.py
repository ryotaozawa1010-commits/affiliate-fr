"""X（旧Twitter）にスレッドを投稿する。公式 API v2（POST /2/tweets）+ OAuth 1.0a。

外部ライブラリを増やさないため、OAuth 1.0a の署名は標準ライブラリで行う。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import requests

API_URL = "https://api.x.com/2/tweets"

# X の文字数は「重み付き」で数える。日本語・絵文字は 1 文字 = 2、英数字は 1、URL は一律 23。
# 無料アカウントの上限は 280（= 日本語だと 140 文字）。
STANDARD_LIMIT = 280
PREMIUM_LIMIT = 25000
URL_WEIGHT = 23
# twitter-text v3 で重み 1 として数える範囲（それ以外は 2）
_LIGHT_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
_URL_RE = re.compile(r"https?://\S+")


def weighted_length(text: str) -> int:
    total = 0
    pos = 0
    for m in _URL_RE.finditer(text):
        total += _chars_weight(text[pos : m.start()]) + URL_WEIGHT
        pos = m.end()
    return total + _chars_weight(text[pos:])


def _chars_weight(text: str) -> int:
    n = 0
    for ch in text:
        cp = ord(ch)
        if 0xFE00 <= cp <= 0xFE0F or cp == 0x200D:
            continue  # 絵文字の異体字セレクタ・結合子は数えない
        n += 1 if any(lo <= cp <= hi for lo, hi in _LIGHT_RANGES) else 2
    return n


def clean(text: str) -> str:
    """X は Markdown を解釈しないので、太字記号などを外す。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_to_fit(text: str, limit: int) -> list[str]:
    """1 ポストに収まらない文章を、行 → 句点 → 文字の順で切って複数ポストにする。"""
    if weighted_length(text) <= limit:
        return [text]

    # (かけら, 直前とのつなぎ文字)。同じ行の続きなら "" で、元の改行位置なら "\n" でつなぐ
    pieces: list[tuple[str, str]] = []
    for line in text.split("\n"):
        if weighted_length(line) <= limit:
            pieces.append((line, "\n"))
            continue
        joiner = "\n"
        for sentence in re.split(r"(?<=[。！？!?])", line):
            if not sentence:
                continue
            while weighted_length(sentence) > limit:
                cut = _cut_index(sentence, limit)
                pieces.append((sentence[:cut], joiner))
                sentence, joiner = sentence[cut:], ""
            pieces.append((sentence, joiner))
            joiner = ""

    chunks: list[str] = []
    current = ""
    for piece, joiner in pieces:
        candidate = f"{current}{joiner}{piece}" if current else piece
        if current and weighted_length(candidate) > limit:
            chunks.append(current.strip())
            current = piece
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def _cut_index(text: str, limit: int) -> int:
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if weighted_length(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return lo


def build_thread(posts: list[str], *, link: str | None = None, premium: bool = False) -> list[str]:
    """投稿前の最終形を作る。リンクは最後のポストにだけ付ける（リンク付きは API 料金が高いため）。"""
    limit = PREMIUM_LIMIT if premium else STANDARD_LIMIT
    cleaned = [clean(p) for p in posts if clean(p)]
    if link and cleaned:
        cleaned[-1] = f"{cleaned[-1]}\n{link}"
    thread: list[str] = []
    for post in cleaned:
        thread.extend(split_to_fit(post, limit))
    return thread


@dataclass
class XCredentials:
    api_key: str
    api_secret: str
    access_token: str
    access_secret: str


def _pct(s: str) -> str:
    return urllib.parse.quote(s, safe="~")


def oauth1_header(method: str, url: str, creds: XCredentials, *, nonce: str | None = None,
                  timestamp: str | None = None) -> str:
    params = {
        "oauth_consumer_key": creds.api_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": creds.access_token,
        "oauth_version": "1.0",
    }
    # JSON ボディは署名対象に含めない（OAuth 1.0a の仕様どおり）
    param_str = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    base = "&".join([method.upper(), _pct(url), _pct(param_str)])
    key = f"{_pct(creds.api_secret)}&{_pct(creds.access_secret)}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    params["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(params.items()))


class XPostError(RuntimeError):
    def __init__(self, message: str, posted_ids: list[str]):
        super().__init__(message)
        self.posted_ids = posted_ids


def post_thread(thread: list[str], creds: XCredentials, *, session: requests.Session | None = None) -> list[str]:
    """スレッドを順番にリプライでつないで投稿し、ポスト ID の一覧を返す。"""
    http = session or requests.Session()
    ids: list[str] = []
    for i, text in enumerate(thread):
        body: dict = {"text": text}
        if ids:
            body["reply"] = {"in_reply_to_tweet_id": ids[-1]}
        resp = http.post(
            API_URL,
            json=body,
            headers={"Authorization": oauth1_header("POST", API_URL, creds)},
            timeout=30,
        )
        if resp.status_code != 201:
            raise XPostError(
                f"X への投稿に失敗しました（{i + 1}/{len(thread)} 本目, HTTP {resp.status_code}）: {resp.text[:500]}",
                ids,
            )
        ids.append(resp.json()["data"]["id"])
    return ids
