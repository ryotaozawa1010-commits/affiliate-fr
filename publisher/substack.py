"""Substack に下書きを作る。

Substack にも公式の投稿 API はないため、ブラウザのエディタが使っている API を
ログイン済みブラウザの Cookie（connect.sid / substack.sid）で呼ぶ。
Substack は Cloudflare の後ろにあり、ブラウザ以外の User-Agent は 403（error code: 1010）になるので、
note と同じくブラウザに近いヘッダを付けた curl で通信する。

- 下書き保存のみ（公開はしない）
- 見出し画像（cover_image）とグラフ画像を付ける
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from publisher.note import IMAGE, USER_AGENT, _shape

PROFILE_URL = "https://substack.com/api/v1/user/profile/self"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


class SubstackError(RuntimeError):
    pass


@dataclass
class SubstackResult:
    draft_id: str
    edit_url: str
    cover: str | None = None
    warnings: list[str] = field(default_factory=list)


def session_cookie(raw: str) -> str:
    """貼られた Cookie からログインの本体だけを取り出し、connect.sid と substack.sid の両方の名前で送る。"""
    raw = re.sub(r"(?i)^\s*cookie\s*:\s*", "", raw.strip()).replace("\n", "").replace("\r", "")
    value = raw
    if re.search(r"(?:^|;)\s*(connect|substack)\.sid=", raw):
        pairs = dict(p.strip().split("=", 1) for p in raw.split(";") if "=" in p)
        value = pairs.get("connect.sid") or pairs.get("substack.sid") or ""
    if not value:
        raise SubstackError("Cookie に connect.sid も substack.sid も見つかりません")
    return f"connect.sid={value}; substack.sid={value}"


# ---------------------------------------------------------------- Markdown → Substack（ProseMirror）


def _inline(text: str) -> list[dict]:
    """太字（**）だけを扱う。斜体の * は記号を外す。"""
    nodes = []
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        part = re.sub(r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)", r"\1", part)
        if part:
            node = {"type": "text", "text": part}
            if i % 2:
                node["marks"] = [{"type": "strong"}]
            nodes.append(node)
    return nodes


def _para(text: str) -> dict:
    content = _inline(text)
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def _image(src: str, caption: str, width: int | None, height: int | None) -> dict:
    return {"type": "captionedImage", "content": [
        {"type": "image2", "attrs": {
            "src": src, "srcNoWatermark": None, "fullscreen": False, "imageSize": "normal",
            "height": height, "width": width, "resizeWidth": width, "bytes": None, "alt": caption,
            "title": None, "type": "image/png", "href": None, "belowTheFold": False, "topImage": False,
            "internalRedirect": None, "isProcessing": False, "align": None, "offset": False}},
        {"type": "caption", "content": [{"type": "text", "text": caption}]},
    ]}


def _starts_block(s: str) -> bool:
    return bool(re.match(r"^(#{1,6}\s|[-*・]\s|\d+[.)]\s|>|\|)", s) or re.match(r"^-{3,}$|^\*{3,}$", s)
                or IMAGE.match(s))


def markdown_to_prosemirror(md: str) -> dict:
    """Cowork の記事で使う範囲の Markdown を Substack のエディタ形式（ProseMirror の JSON）にする。"""
    content: list[dict] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if re.match(r"^-{3,}$|^\*{3,}$", s):
            content.append({"type": "horizontal_rule"})
            i += 1
            continue
        m = IMAGE.match(s)
        if m:
            width = int(m.group(3)) if m.group(3) else None
            height = int(m.group(4)) if m.group(4) else None
            content.append(_image(m.group(2), m.group(1), width, height))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            level = 2 if len(m.group(1)) <= 2 else 3
            content.append({"type": "heading", "attrs": {"level": level}, "content": _inline(m.group(2))})
            i += 1
            continue
        if re.match(r"^([-*・]|\d+[.)])\s+", s):
            ordered = bool(re.match(r"^\d+[.)]\s+", s))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*・]|\d+[.)])\s+", lines[i]):
                text = re.sub(r"^\s*(?:[-*・]|\d+[.)])\s+", "", lines[i])
                items.append({"type": "list_item", "content": [_para(text)]})
                i += 1
            content.append({"type": "ordered_list" if ordered else "bullet_list", "content": items})
            continue
        if s.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(_para(lines[i].strip().lstrip(">").strip()))
                i += 1
            content.append({"type": "blockquote", "content": quote})
            continue
        if s.startswith("|"):
            # 表は持てないので、1行ずつ「セル / セル」の段落にする
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-+:?$", c) for c in cells if c):
                    content.append(_para(" / ".join(c for c in cells if c)))
                i += 1
            continue
        # 段落内の改行は、それぞれ別の段落にする（改行ノードの名前の揺れを避ける）
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i].strip()):
            content.append(_para(lines[i].strip()))
            i += 1
    return {"type": "doc", "content": content}


# ---------------------------------------------------------------- API


def _curl(run, method: str, url: str, body: dict | None, *, cookie: str, referer: str) -> tuple[int, str]:
    headers = [
        f"User-Agent: {USER_AGENT}",
        "Accept: application/json, text/plain, */*",
        "Accept-Language: fr-FR,fr;q=0.9,ja;q=0.8,en;q=0.7",
        f"Referer: {referer}",
        f"Cookie: {cookie}",
    ]
    if body is not None:
        headers.append("Content-Type: application/json")
    cmd = ["curl", "-sS", "--compressed", "-X", method, url, "-w", "\n%{http_code}"]
    for h in headers:
        cmd += ["-H", h]
    if body is not None:
        # 画像の中身（base64）は大きいので、コマンドラインではなく標準入力で渡す
        cmd += ["--data-binary", "@-"]
    proc = run(cmd, input=json.dumps(body, ensure_ascii=False) if body is not None else None,
               capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise SubstackError(f"curl が失敗しました: {proc.stderr.strip()[:300]}")
    out, _, code = proc.stdout.rpartition("\n")
    return (int(code) if code.isdigit() else 0), out


class SubstackClient:
    def __init__(self, cookie: str, publication_url: str | None = None, runner=subprocess.run):
        if not cookie:
            raise SubstackError("Substack のログイン情報（Cookie）が空です")
        self.cookie = session_cookie(cookie)
        self._run = runner
        self.publication_url = (publication_url or "").rstrip("/") or None
        self.user_id: int | None = None

    def _request(self, method: str, url: str, body: dict | None = None) -> dict:
        referer = f"{self.publication_url}/publish/home" if self.publication_url else "https://substack.com/"
        code, text = _curl(self._run, method, url, body, cookie=self.cookie, referer=referer)
        if not 200 <= code < 300:
            hint = ""
            if code in (401, 403):
                hint = "（Substack のログインの期限切れか、Cookie の貼り間違いの可能性があります）"
            raise SubstackError(f"{method} {url.split('?')[0]} が HTTP {code} で失敗しました{hint}: {_shape(text)}")
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise SubstackError(f"{method} {url} の応答が JSON ではありません: {_shape(text)}") from e

    def whoami(self) -> None:
        """ユーザー ID と、記事を書く publication の URL を調べる。"""
        profile = self._request("GET", PROFILE_URL)
        self.user_id = profile.get("id")
        if not self.user_id:
            raise SubstackError(f"Substack のユーザー情報を取れませんでした: keys={sorted(profile)[:20]}")
        if self.publication_url:
            return
        pubs = profile.get("publicationUsers") or []
        chosen = next((p for p in pubs if p.get("is_primary")), pubs[0] if pubs else None)
        pub = (chosen or {}).get("publication") or profile.get("primaryPublication") or {}
        if pub.get("custom_domain") and pub.get("custom_domain_optional") is not True:
            self.publication_url = f"https://{pub['custom_domain']}"
        elif pub.get("subdomain"):
            self.publication_url = f"https://{pub['subdomain']}.substack.com"
        else:
            raise SubstackError("書き込める Substack の publication が見つかりません（SUBSTACK_URL を設定してください）")

    def logged_in(self) -> bool:
        try:
            self.whoami()
            return True
        except SubstackError:
            return False

    def upload_image(self, image: Path) -> str:
        if self.user_id is None:
            self.whoami()
        mime = IMAGE_TYPES.get(image.suffix.lower())
        if not mime:
            raise SubstackError(f"画像の形式が未対応です: {image.name}")
        data = base64.b64encode(image.read_bytes()).decode()
        res = self._request("POST", f"{self.publication_url}/api/v1/image", {"image": f"data:{mime};base64,{data}"})
        if not res.get("url"):
            raise SubstackError(f"画像の URL が返ってきませんでした: keys={sorted(res)}")
        return res["url"]

    def create_draft(self, title: str, subtitle: str, body_md: str, *, cover: Path | None = None) -> SubstackResult:
        if self.user_id is None:
            self.whoami()
        draft = self._request("POST", f"{self.publication_url}/api/v1/drafts", {
            "draft_title": title,
            "draft_subtitle": subtitle,
            "draft_body": json.dumps(markdown_to_prosemirror(body_md), ensure_ascii=False),
            "draft_bylines": [{"id": int(self.user_id), "is_guest": False}],
            "audience": "everyone",
            "type": "newsletter",
        })
        draft_id = str(draft.get("id", ""))
        if not draft_id:
            raise SubstackError(f"下書きを作れませんでした: {_shape(json.dumps(draft))}")
        result = SubstackResult(draft_id, f"{self.publication_url}/publish/post/{draft_id}")
        if cover:
            # 見出し画像が付けられなくても下書きそのものは残す
            try:
                url = self.upload_image(cover)
                self._request("PUT", f"{self.publication_url}/api/v1/drafts/{draft_id}", {"cover_image": url})
                result.cover = url
            except SubstackError as e:
                result.warnings.append(f"見出し画像を付けられませんでした（{e}）")
        return result
