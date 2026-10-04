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
import quopri
import re
import sys
import textwrap
from email.message import Message
from email.utils import parseaddr
from pathlib import Path

INBOX_DIR = Path("publish/inbox")
STATE_FILE = Path(".gmail_fetched.json")  # fetch → mark の受け渡し用（コミットしない）
LABEL = "published"
SUBJECT_RE = re.compile(r"\[PUBLISH\]\s*(\d{4}-\d{2}-\d{2}_(?:weekday|weekend|test|drafttest))\b")
BODY_RE = re.compile(r"-----BEGIN-----[ \t]*\r?\n(.*?)\r?\n[ \t]*-----END-----", re.S)
TOKEN_RE = re.compile(r"^[ \t>]*TOKEN:\s*(\S+)\s*$", re.M)


class Rejected(ValueError):
    pass


def _unflow(part: Message) -> str:
    """format=flowed（RFC 3676）の本文を元の行に戻す。

    Gmail は長い行を「行末に空白を残して改行」する形で折り返して届ける。素直に読むと
    折り返し位置に本物の改行が入り、文の途中で改行された記事になってしまう。
    quoted-printable では行末の空白がデコード時に消えるため、デコード前につなぎ直す。
    """
    delsp = str(part.get_param("delsp", "no")).lower() == "yes"
    join = "" if delsp else " "
    if str(part.get("Content-Transfer-Encoding", "")).strip().lower() == "quoted-printable":
        encoded = str(part.get_payload(decode=False))
        encoded = re.sub(r"(?: |=20)\r?\n", join, encoded)
        text = quopri.decodestring(encoded.encode("ascii", "replace")).decode(
            part.get_content_charset() or "utf-8", "replace")
    else:
        text = re.sub(r" \r?\n", join, part.get_content())
    # 行頭の空白・「>」・「From 」の前に足された 1 文字（space-stuffing）を外す
    return re.sub(r"(?m)^ ", "", text.replace("\r\n", "\n"))


def _structure(raw: bytes) -> str:
    """原因調査用に、本文の中身を出さずにメールの形（符号化・折り返し方）だけを要約する。"""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        lines = str(part.get_payload(decode=False)).split("\n")
        out.append(
            f"{part.get_content_type()} format={part.get_param('format', '-')} "
            f"delsp={part.get_param('delsp', '-')} cte={part.get('Content-Transfer-Encoding', '-')} "
            f"行数={len(lines)} 最長={max(map(len, lines))} 行末空白={sum(l.rstrip(chr(13)).endswith(' ') for l in lines)} "
            f"行末=20={sum(l.rstrip(chr(13)).endswith('=20') for l in lines)}"
        )
    return " / ".join(out)


def _plain_text(msg: Message) -> str:
    parts = [p for p in msg.walk() if not p.is_multipart() and not p.get_filename()]
    for part in parts:
        if part.get_content_type() == "text/plain":
            if str(part.get_param("format", "")).lower() == "flowed":
                return _unflow(part)
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
    # X-GM-RAW の検索語は全体を "..." で囲んで送るので、中に " を入れない（件名の厳密な確認は extract で行う）
    query = f"from:{sender} subject:PUBLISH -label:{LABEL} newer_than:3d"
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
        print(f"受け取り（UID {uid.decode()}）: {_structure(raw)}", file=sys.stderr)
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
