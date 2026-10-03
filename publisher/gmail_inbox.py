"""Cowork が送った「投稿用メール」を受け取り用の Gmail から取り出し、publish/inbox/ に置く。

メールの形式（Cowork のルーティンが Gmail コネクタで送る）:

    差出人: PUBLISH_SENDER（Cowork の Gmail コネクタのアドレス）
    宛先:   GMAIL_ADDRESS（受け取り専用の Gmail。ここに IMAP でログインする）
    件名:   [PUBLISH] 2026-10-04_weekend        ← weekday / weekend / test
    本文:   TOKEN: <PUBLISH_TOKEN>
            -----BEGIN-----
            # ② note記事 ...
            -----END-----

なりすまし対策として、差出人が PUBLISH_SENDER であること・本文の TOKEN が
GitHub Secret の PUBLISH_TOKEN と一致することの両方を確認する。
処理したメールには Gmail のラベル「published」を付け、二度と拾わない。

使い方:
    python -m publisher.gmail_inbox fetch   # 新着を publish/inbox/ に保存し、保存したパスを出力
    python -m publisher.gmail_inbox mark    # fetch で拾ったメールにラベルを付ける
"""

from __future__ import annotations

import email
import email.policy
import hmac
import html
import imaplib
import json
import os
import re
import sys
import textwrap
from email.message import Message
from email.utils import parseaddr
from pathlib import Path

INBOX_DIR = Path("publish/inbox")
STATE_FILE = Path(".gmail_fetched.json")  # fetch → mark の受け渡し用（コミットしない）
LABEL = "published"
SUBJECT_RE = re.compile(r"\[PUBLISH\]\s*(\d{4}-\d{2}-\d{2}_(?:weekday|weekend|test))\b")
BODY_RE = re.compile(r"-----BEGIN-----[ \t]*\r?\n(.*?)\r?\n[ \t]*-----END-----", re.S)
TOKEN_RE = re.compile(r"^[ \t>]*TOKEN:\s*(\S+)\s*$", re.M)


class Rejected(ValueError):
    pass


def _plain_text(msg: Message) -> str:
    parts = [p for p in msg.walk() if not p.is_multipart() and not p.get_filename()]
    for part in parts:
        if part.get_content_type() == "text/plain":
            return part.get_content()
    for part in parts:
        if part.get_content_type() == "text/html":
            text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", part.get_content(), flags=re.I)
            return html.unescape(re.sub(r"<[^>]+>", "", text))
    raise Rejected("本文がありません")


def extract(raw: bytes, *, sender: str, token: str) -> tuple[str, str]:
    """メール1通を検査して (ファイル名の stem, 中身) を返す。条件を満たさなければ Rejected。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    m = SUBJECT_RE.search(str(msg.get("Subject", "")))
    if not m:
        raise Rejected("件名が [PUBLISH] YYYY-MM-DD_<種類> の形ではありません")
    from_addr = parseaddr(str(msg.get("From", "")))[1].lower()
    if from_addr != sender.lower():
        raise Rejected(f"差出人が想定外です: {from_addr}")
    body = _plain_text(msg).replace("\r\n", "\n")
    t = TOKEN_RE.search(body)
    if not t or not hmac.compare_digest(t.group(1), token):
        raise Rejected("TOKEN が一致しません")
    b = BODY_RE.search(body)
    if not b or not b.group(1).strip():
        raise Rejected("-----BEGIN----- 〜 -----END----- の中身がありません")
    # 指示文の字下げごとコピーされた場合に備えて、共通の字下げを外す
    return m.group(1), textwrap.dedent(b.group(1)).strip() + "\n"


def _connect() -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(os.environ["GMAIL_ADDRESS"].strip(), os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
    imap.select("INBOX")
    return imap


def fetch() -> list[str]:
    address = os.environ["GMAIL_ADDRESS"].strip()
    # 差出人の指定がなければ「自分から自分へ」とみなす
    sender = os.environ.get("PUBLISH_SENDER", "").strip() or address
    token = os.environ["PUBLISH_TOKEN"].strip()
    imap = _connect()
    query = f'from:{sender} subject:"[PUBLISH]" -label:{LABEL} newer_than:3d'
    typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{query}"')
    uids = data[0].split() if typ == "OK" and data and data[0] else []
    saved, fetched = [], []
    for uid in uids:
        typ, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
        if raw is None:
            continue
        try:
            stem, content = extract(raw, sender=sender, token=token)
        except Rejected as e:
            print(f"スキップ（UID {uid.decode()}）: {e}", file=sys.stderr)
            continue
        fetched.append(uid.decode())
        path = INBOX_DIR / f"{stem}.md"
        if path.exists():
            print(f"既に受け取り済み: {path}", file=sys.stderr)
            continue
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        saved.append(str(path))
    imap.logout()
    STATE_FILE.write_text(json.dumps(fetched))
    return saved


def mark() -> None:
    if not STATE_FILE.exists():
        return
    uids = json.loads(STATE_FILE.read_text())
    if not uids:
        return
    imap = _connect()
    for uid in uids:
        imap.uid("STORE", uid, "+X-GM-LABELS", f"({LABEL})")
    imap.logout()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        print(" ".join(fetch()))
    elif cmd == "mark":
        mark()
    else:
        sys.exit("usage: python -m publisher.gmail_inbox fetch|mark")
