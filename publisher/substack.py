"""Substack（フランス語版の週刊ニュースレター）に記事を作る。

Substack にも公式の投稿 API はないため、ブラウザのエディタと同じ通信を再現している。
本文は Substack のエディタが使う ProseMirror の JSON に変換して送る。

ログインは、ブラウザの Cookie `substack.sid`（SUBSTACK_COOKIE）を優先する。
メールとパスワードでのログインはロボット確認で弾かれることがある。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from publisher.note import IMAGE, LINK, USER_AGENT

SITE = "https://substack.com"


class SubstackError(RuntimeError):
    pass


@dataclass
class SubstackResult:
    draft_id: int
    status: str  # "draft" | "published"
    url: str | None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- Markdown → ProseMirror


INLINE = re.compile(
    LINK.pattern
    + r"|\*\*(.+?)\*\*"
    + r"|(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)"
)


def _inline(text: str, marks: tuple = ()) -> list[dict]:
    """太字（**）・斜体（*）・リンク（[文字](URL)）を ProseMirror の text ノードに分ける。"""
    nodes: list[dict] = []

    def plain(t: str) -> None:
        if t:
            node = {"type": "text", "text": t}
            if marks:
                node["marks"] = [dict(m) for m in marks]
            nodes.append(node)

    pos = 0
    for m in INLINE.finditer(text):
        plain(text[pos : m.start()])
        if m.group(1) is not None:
            nodes += _inline(m.group(1), marks + ({"type": "link", "attrs": {"href": m.group(2)}},))
        elif m.group(3) is not None:
            nodes += _inline(m.group(3), marks + ({"type": "strong"},))
        else:
            nodes += _inline(m.group(4), marks + ({"type": "em"},))
        pos = m.end()
    plain(text[pos:])
    return nodes


def _paragraph(lines: list[str]) -> dict:
    content: list[dict] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hard_break"})
        content += _inline(line)
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def _starts_block(line: str) -> bool:
    s = line.strip()
    return bool(
        re.match(r"^(#{1,6}\s|[-*・]\s|\d+[.)]\s|>|\|)", s)
        or re.match(r"^-{3,}$|^\*{3,}$", s)
        or IMAGE.match(s)
    )


def markdown_to_doc(md: str) -> dict:
    """Cowork の記事で使う範囲の Markdown を Substack のエディタの文書（ProseMirror JSON）にする。"""
    blocks: list[dict] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if re.match(r"^-{3,}$|^\*{3,}$", stripped):
            blocks.append({"type": "horizontal_rule"})
            i += 1
            continue
        m = IMAGE.match(stripped)
        if m:
            width, height = (int(m.group(3)), int(m.group(4))) if m.group(3) else (620, None)
            blocks.append({"type": "captionedImage", "content": [
                {"type": "image2", "attrs": {
                    "src": m.group(2), "fullscreen": False, "imageSize": "normal", "width": width,
                    "height": height, "resizeWidth": width, "alt": m.group(1), "title": None,
                    "type": "image/png", "href": None, "belowTheFold": False, "internalRedirect": None,
                }},
                {"type": "caption", "content": _inline(m.group(1))},
            ]})
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            # 記事の見出しは ### で書かれるので、それを大見出し（h2）にする
            level = 2 if len(m.group(1)) <= 3 else 3
            blocks.append({"type": "heading", "attrs": {"level": level}, "content": _inline(m.group(2))})
            i += 1
            continue
        if re.match(r"^[-*・]\s+", stripped) or re.match(r"^\d+[.)]\s+", stripped):
            ordered = bool(re.match(r"^\d+[.)]\s+", stripped))
            items = []
            while i < len(lines) and (
                re.match(r"^\s*[-*・]\s+", lines[i]) or re.match(r"^\s*\d+[.)]\s+", lines[i])
            ):
                item = re.sub(r"^\s*(?:[-*・]|\d+[.)])\s+", "", lines[i])
                items.append({"type": "list_item", "content": [_paragraph([item])]})
                i += 1
            blocks.append({"type": "ordered_list" if ordered else "bullet_list", "content": items})
            continue
        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "blockquote", "content": [_paragraph(quote)]})
            continue
        if stripped.startswith("|"):
            # 表は使わない約束だが、来てしまったら note と同じく1行ずつの段落にする
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-+:?$", c) for c in cells if c):
                    rows.append(" / ".join(c for c in cells if c))
                i += 1
            blocks.append(_paragraph(rows))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append(_paragraph(para))
    return {"type": "doc", "content": blocks}


# ---------------------------------------------------------------- API


def _sid(raw: str) -> str:
    """Cookie の文字列全体を貼られても、substack.sid の値だけを取り出す。"""
    m = re.search(r"(?:^|;\s*)substack\.sid=([^;]+)", raw)
    return (m.group(1) if m else raw).strip()


class SubstackClient:
    def __init__(self, publication: str, cookie: str | None = None, session: requests.Session | None = None):
        self.base = f"https://{publication.strip()}.substack.com"
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        if cookie:
            self.session.cookies.set("substack.sid", _sid(cookie), domain=".substack.com")
        self._user_id: int | None = None

    @classmethod
    def login(cls, publication: str, email: str, password: str,
              session: requests.Session | None = None) -> "SubstackClient":
        client = cls(publication, session=session)
        client._request("POST", f"{SITE}/api/v1/login", json={
            "captcha_response": None, "email": email, "for_pub": "", "password": password, "redirect": "/",
        }, what="ログイン")
        return client

    def _request(self, method: str, url: str, *, what: str, **kw) -> dict:
        try:
            r = self.session.request(method, url, timeout=30, **kw)
        except requests.RequestException as e:
            raise SubstackError(f"{what}: 通信に失敗しました（{e}）") from e
        if r.status_code in (401, 403):
            hint = "Cookie（SUBSTACK_COOKIE）が切れているか、ロボット確認で弾かれました"
            if "captcha" in r.text.lower():
                hint = "ロボット確認（captcha）で弾かれました。SUBSTACK_COOKIE を使ってください"
            raise SubstackError(f"{what}: HTTP {r.status_code}。{hint}")
        if r.status_code >= 400:
            raise SubstackError(f"{what}: HTTP {r.status_code} {r.text[:200]}")
        try:
            return r.json() if r.content else {}
        except ValueError as e:
            raise SubstackError(f"{what}: 応答が JSON ではありません（{r.text[:120]}）") from e

    def user_id(self) -> int:
        if self._user_id is None:
            profile = self._request("GET", f"{SITE}/api/v1/user/profile/self", what="ログイン確認")
            if not profile.get("id"):
                raise SubstackError("ログイン確認: ユーザー ID が取れません（Cookie が切れている可能性があります）")
            self._user_id = int(profile["id"])
        return self._user_id

    def upload_image(self, image: Path) -> str:
        data = base64.b64encode(Path(image).read_bytes()).decode()
        r = self._request("POST", f"{self.base}/api/v1/image",
                          data={"image": f"data:image/png;base64,{data}"}, what="画像のアップロード")
        if not r.get("url"):
            raise SubstackError(f"画像のアップロード: URL が返ってきません（{str(r)[:120]}）")
        return r["url"]

    def create(self, title: str, subtitle: str, body_md: str, *, publish: bool) -> SubstackResult:
        draft = self._request("POST", f"{self.base}/api/v1/drafts", what="下書きの作成", json={
            "draft_title": title,
            "draft_subtitle": subtitle,
            "draft_body": json.dumps(markdown_to_doc(body_md), ensure_ascii=False),
            "draft_bylines": [{"id": self.user_id(), "is_guest": False}],
            "audience": "everyone",
            "type": "newsletter",
            "section_chosen": True,
            "draft_section_id": None,
            "write_comment_permissions": "everyone",
        })
        draft_id = draft.get("id")
        if not draft_id:
            raise SubstackError(f"下書きの作成: ID が返ってきません（{str(draft)[:120]}）")
        if not publish:
            return SubstackResult(draft_id, "draft", None)
        warnings = []
        try:
            self._request("GET", f"{self.base}/api/v1/drafts/{draft_id}/prepublish", what="公開前の確認")
            post = self._request("POST", f"{self.base}/api/v1/drafts/{draft_id}/publish", what="公開",
                                 json={"send": True, "share_automatically": False})
        except SubstackError as e:
            # 下書きはできているので、記事そのものは失わない
            warnings.append(f"下書きは作れましたが公開できませんでした（{e}）。Substack のアプリで公開してください")
            return SubstackResult(draft_id, "draft", None, warnings)
        slug = post.get("slug")
        url = post.get("canonical_url") or (f"{self.base}/p/{slug}" if slug else None)
        return SubstackResult(draft_id, "published", url, warnings)
