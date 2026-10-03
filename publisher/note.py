"""note.com に記事を作成する。

note には公式の投稿 API がないため、ブラウザのエディタが内部で使っている API を
ログイン済みブラウザの Cookie で呼ぶ。素の Python HTTP クライアントは note 側の
CloudFront に弾かれやすいので、通信はブラウザに近いヘッダを付けた curl で行う。

- 下書き保存（NOTE_MODE=draft）: 実績のある手順。既定はこちら。
- 公開（NOTE_MODE=publish）: エディタの「投稿」ボタンと同じ呼び出しを再現した実験的機能。
  note 側の仕様変更で突然動かなくなる可能性がある。
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import uuid
from dataclasses import dataclass

BASE = "https://note.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- Markdown → note HTML


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # note のエディタに斜体はないので記号だけ外す
    text = re.sub(r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)", r"\1", text)
    return text


def _attrs() -> str:
    uid = str(uuid.uuid4())
    return f' name="{uid}" id="{uid}"'


def markdown_to_note_html(md: str) -> str:
    """Cowork の記事で使う範囲の Markdown（見出し・段落・太字・箇条書き・引用・区切り線・表）を変換する。"""
    blocks: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if re.match(r"^-{3,}$|^\*{3,}$", stripped):
            blocks.append(f"<hr{_attrs()}>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            # note は大見出し(h2)・小見出し(h3)の2段階だけ
            tag = "h2" if len(m.group(1)) <= 2 else "h3"
            blocks.append(f"<{tag}{_attrs()}>{_inline(m.group(2))}</{tag}>")
            i += 1
            continue
        if re.match(r"^[-*・]\s+", stripped) or re.match(r"^\d+[.)]\s+", stripped):
            ordered = bool(re.match(r"^\d+[.)]\s+", stripped))
            items = []
            while i < len(lines) and (
                re.match(r"^\s*[-*・]\s+", lines[i]) or re.match(r"^\s*\d+[.)]\s+", lines[i])
            ):
                item = re.sub(r"^\s*(?:[-*・]|\d+[.)])\s+", "", lines[i])
                items.append(f"<li{_attrs()}>{_inline(item)}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}{_attrs()}>{''.join(items)}</{tag}>")
            continue
        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(_inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            blocks.append(f"<blockquote{_attrs()}><p>{'<br>'.join(quote)}</p></blockquote>")
            continue
        if stripped.startswith("|"):
            # note は表を持てないので、1行ずつ「・ セル / セル」の段落にする
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-+:?$", c) for c in cells if c):
                    rows.append(_inline(" ／ ".join(c for c in cells if c)))
                i += 1
            blocks.append(f"<p{_attrs()}>{'<br>'.join(rows)}</p>")
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(_inline(lines[i].strip()))
            i += 1
        blocks.append(f"<p{_attrs()}>{'<br>'.join(para)}</p>")
    return "".join(blocks)


def _starts_block(line: str) -> bool:
    s = line.strip()
    return bool(
        re.match(r"^(#{1,6}\s|[-*・]\s|\d+[.)]\s|>|\|)", s) or re.match(r"^-{3,}$|^\*{3,}$", s)
    )


# ---------------------------------------------------------------- API


class NoteError(RuntimeError):
    pass


@dataclass
class NoteResult:
    note_id: str
    key: str
    status: str  # "draft" | "published"
    url: str | None  # 公開した場合のみ


def _xsrf_from_cookie(cookie: str) -> str | None:
    m = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie)
    return m.group(1) if m else None


class NoteClient:
    def __init__(self, cookie: str, urlname: str | None = None, runner=subprocess.run):
        if not cookie:
            raise NoteError("NOTE_COOKIE が空です")
        self.cookie = cookie.strip()
        self.urlname = urlname
        self._run = runner

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = [
            f"User-Agent: {USER_AGENT}",
            "Accept: application/json, text/plain, */*",
            "Accept-Language: ja,en-US;q=0.9,en;q=0.8",
            "Content-Type: application/json",
            "Origin: https://editor.note.com",
            "Referer: https://editor.note.com/",
            "X-Requested-With: XMLHttpRequest",
            f"Cookie: {self.cookie}",
        ]
        xsrf = _xsrf_from_cookie(self.cookie)
        if xsrf:
            headers.append(f"X-XSRF-TOKEN: {xsrf}")
        cmd = ["curl", "-sS", "--compressed", "-X", method, f"{BASE}{path}", "-w", "\n%{http_code}"]
        for h in headers:
            cmd += ["-H", h]
        if body is not None:
            cmd += ["--data-binary", "@-"]
        proc = self._run(
            cmd,
            input=json.dumps(body, ensure_ascii=False) if body is not None else None,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise NoteError(f"curl が失敗しました: {proc.stderr.strip()[:300]}")
        text, _, code = proc.stdout.rpartition("\n")
        if not code.isdigit() or not 200 <= int(code) < 300:
            hint = ""
            if code in ("401", "403"):
                hint = "（Cookie の期限切れか、note 側のアクセス制限の可能性があります。NOTE_COOKIE を取り直してください）"
            raise NoteError(f"{method} {path} が HTTP {code} で失敗しました{hint}: {text[:300]}")
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise NoteError(f"{method} {path} の応答が JSON ではありません: {text[:200]}") from e

    def create(self, title: str, body_md: str, *, publish: bool) -> NoteResult:
        body_html = markdown_to_note_html(body_md)
        body_length = len(re.sub(r"<[^>]+>", "", body_html))

        created = self._request("POST", "/api/v1/text_notes", {"template_key": None})
        data = created.get("data") or {}
        note_id, key = str(data.get("id", "")), str(data.get("key", ""))
        if not note_id or not key:
            raise NoteError(f"記事の枠を作れませんでした: {json.dumps(created)[:300]}")
        urlname = self.urlname or (data.get("user") or {}).get("urlname")

        self._request(
            "POST",
            f"/api/v1/text_notes/draft_save?id={note_id}&is_temp_saved=true",
            {"body": body_html, "body_length": body_length, "name": title, "index": False, "is_lead_form": False},
        )
        if not publish:
            return NoteResult(note_id, key, "draft", None)

        self._request(
            "PUT",
            f"/api/v1/text_notes/{note_id}",
            {
                "author_ids": [],
                "body": body_html,
                "body_length": body_length,
                "circle_permissions": [],
                "discount_campaigns": [],
                "free_body": body_html,
                "hashtags": [],
                "image_keys": [],
                "index": False,
                "is_refund": False,
                "limited": False,
                "magazine_ids": [],
                "magazine_keys": [],
                "name": title,
                "pay_body": "",
                "price": 0,
                "send_notifications_flag": True,
                "separator": None,
                "slug": f"slug-{key}",
                "status": "published",
                "lead_form": {"is_active": False, "consent_url": ""},
                "line_add_friend": {"is_active": False, "keyword": "", "add_friend_url": ""},
                "line_add_friend_access_token": "",
            },
        )
        url = f"{BASE}/{urlname}/n/{key}" if urlname else f"{BASE}/n/{key}"
        return NoteResult(note_id, key, "published", url)
