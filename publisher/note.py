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
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

BASE = "https://note.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- Markdown → note HTML


LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _inline(text: str) -> str:
    text = html.escape(text)
    # [文字](URL) はリンクに（広告枠のアフィリエイトリンク用。nofollow は検索エンジン向けの広告の作法）
    text = LINK.sub(r'<a href="\2" target="_blank" rel="nofollow noopener noreferrer">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # note のエディタに斜体はないので記号だけ外す
    text = re.sub(r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)", r"\1", text)
    return text


IMAGE = re.compile(r'^!\[([^\]]*)\]\((https://\S+?)(?:\s+"(\d+)x(\d+)")?\)$')


def _attrs() -> str:
    uid = str(uuid.uuid4())
    return f' name="{uid}" id="{uid}"'


def markdown_to_note_html(md: str) -> str:
    """Cowork の記事で使う範囲の Markdown（見出し・段落・太字・リンク・箇条書き・引用・区切り線・表）を変換する。"""
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
        m = IMAGE.match(stripped)
        if m:
            # グラフ等の画像（アップロード済みの URL）。下の説明文に出典を載せる
            caption, src = html.escape(m.group(1)), html.escape(m.group(2))
            width, height = (m.group(3), m.group(4)) if m.group(3) else ("620", "auto")
            uid = str(uuid.uuid4())
            blocks.append(
                f'<figure name="{uid}" id="{uid}"><img src="{src}" alt="" width="{width}" height="{height}" '
                f'contenteditable="false" draggable="false"><figcaption>{caption}</figcaption></figure>'
            )
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
                # note のエディタは箇条書きの中身が段落（<p>）でないと文字を捨てるので包む
                items.append(f"<li{_attrs()}><p{_attrs()}>{_inline(item)}</p></li>")
                i += 1
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}{_attrs()}>{''.join(items)}</{tag}>")
            continue
        if stripped.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(_inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            blocks.append(f"<blockquote{_attrs()}><p{_attrs()}>{'<br>'.join(quote)}</p></blockquote>")
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
        or IMAGE.match(s)
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
    eyecatch: str | None = None  # 見出し画像の URL（付けられた場合）
    warnings: list[str] = field(default_factory=list)  # 記事は作れたが一部うまくいかなかったこと


def _xsrf_from_cookie(cookie: str) -> str | None:
    m = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie)
    return m.group(1) if m else None


def _curl(run, method: str, path: str, body: dict | None, *, cookie: str | None,
          dump_headers: bool = False) -> tuple[int, str, str]:
    """ブラウザに近いヘッダで note の API を呼び、(HTTP ステータス, 応答ヘッダ, 本文) を返す。"""
    headers = [
        f"User-Agent: {USER_AGENT}",
        "Accept: application/json, text/plain, */*",
        "Accept-Language: ja,en-US;q=0.9,en;q=0.8",
        "Content-Type: application/json",
        "Origin: https://editor.note.com",
        "Referer: https://editor.note.com/",
        "X-Requested-With: XMLHttpRequest",
    ]
    if cookie:
        headers.append(f"Cookie: {cookie}")
        xsrf = _xsrf_from_cookie(cookie)
        if xsrf:
            headers.append(f"X-XSRF-TOKEN: {xsrf}")
    cmd = ["curl", "-sS", "--compressed", "-X", method, f"{BASE}{path}", "-w", "\n%{http_code}"]
    if dump_headers:
        cmd += ["-D", "-"]
    for h in headers:
        cmd += ["-H", h]
    if body is not None:
        # 本文（パスワードを含むことがある）はコマンドラインに載せず標準入力で渡す
        cmd += ["--data-binary", "@-"]
    proc = run(
        cmd,
        input=json.dumps(body, ensure_ascii=False) if body is not None else None,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise NoteError(f"curl が失敗しました: {proc.stderr.strip()[:300]}")
    out, _, code = proc.stdout.rpartition("\n")
    head = ""
    if dump_headers:
        # -D - は本文の前に「ヘッダ + 空行」を出す（リダイレクト等で複数回出ることもある）
        parts = re.split(r"\r?\n\r?\n", out)
        while len(parts) > 1 and re.match(r"HTTP/", parts[0]):
            head += parts.pop(0) + "\n"
        out = "\n\n".join(parts)
    return (int(code) if code.isdigit() else 0), head, out


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def _curl_form(run, url: str, fields: dict[str, str], file: tuple[str, Path] | None, *,
               cookie: str | None = None, xsrf: str | None = None) -> tuple[int, str]:
    """multipart/form-data で送る（画像のアップロード用）。cookie が None なら note 以外（S3）宛て。

    Content-Type は境界（boundary）付きで curl に付けさせる。ファイルは最後に置く（S3 の決まり）。
    """
    cmd = ["curl", "-sS", "-X", "POST", url, "-w", "\n%{http_code}"]
    if cookie is not None:
        cmd.insert(2, "--compressed")
        for h in (
            f"User-Agent: {USER_AGENT}",
            "Accept: application/json, text/plain, */*",
            "Accept-Language: ja,en-US;q=0.9,en;q=0.8",
            "Origin: https://editor.note.com",
            "Referer: https://editor.note.com/",
            "X-Requested-With: XMLHttpRequest",
            f"Cookie: {cookie}",
        ) + ((f"X-XSRF-TOKEN: {xsrf}",) if xsrf else ()):
            cmd += ["-H", h]
    for k, v in fields.items():
        # --form-string は値の先頭が @ や < でもファイル扱いしない
        cmd += ["--form-string", f"{k}={v}"]
    if file:
        name, path = file
        mime = IMAGE_TYPES.get(path.suffix.lower())
        if not mime:
            raise NoteError(f"画像の形式が未対応です: {path.name}")
        cmd += ["-F", f'{name}=@"{path}";type={mime};filename={path.name}']
    proc = run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise NoteError(f"curl が失敗しました: {proc.stderr.strip()[:300]}")
    out, _, code = proc.stdout.rpartition("\n")
    return (int(code) if code.isdigit() else 0), out


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001  寸法は無くても送れる
        return 0, 0


def _shape(text: str) -> str:
    """公開ログに個人情報を出さないよう、JSON はキー名だけ、それ以外は種類と長さだけを返す。"""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return f"JSONではない（{len(text)}文字、先頭: {text[:15]!r}）"
    if isinstance(obj, dict):
        inner = obj.get("data")
        sub = f" data={sorted(inner)[:20]}" if isinstance(inner, dict) else ""
        # エラー内容（コードとメッセージ）は個人情報を含まないので表示する
        err = f" error={json.dumps(obj['error'], ensure_ascii=False)[:200]}" if obj.get("error") else ""
        return f"keys={sorted(obj)}{sub}{err}"
    return type(obj).__name__


# 送る Cookie はログインの本体（数か月有効）と XSRF 対策だけに絞る。
# ブラウザの Cookie には note_gql_auth_token など数分で切れる札も混ざっており、
# 期限切れの札を一緒に送ると note は「未ログイン」と判定してしまう。
KEEP_COOKIES = ("_note_session_v5", "XSRF-TOKEN")


def _session_cookie(raw: str) -> str:
    # 開発者ツールから「cookie: ...」の行ごとコピーされた場合に備えて見出しと改行を外す
    raw = re.sub(r"(?i)^\s*cookie\s*:\s*", "", raw.strip()).replace("\n", "").replace("\r", "")
    if "=" not in raw:
        # _note_session_v5 の値だけが貼られた場合
        return f"_note_session_v5={raw}"
    pairs = [p.strip() for p in raw.split(";") if "=" in p]
    kept = [p for p in pairs if p.split("=", 1)[0].strip() in KEEP_COOKIES]
    return "; ".join(kept) if any(p.startswith("_note_session_v5=") for p in kept) else "; ".join(pairs)


class NoteClient:
    def __init__(self, cookie: str, urlname: str | None = None, runner=subprocess.run):
        if not cookie:
            raise NoteError("note のログイン情報（Cookie）が空です")
        self.cookie = _session_cookie(cookie)
        self.urlname = urlname
        self._run = runner

    @classmethod
    def login(cls, email: str, password: str, urlname: str | None = None, runner=subprocess.run) -> "NoteClient":
        """メールアドレスとパスワードでログインし、その Cookie を使うクライアントを返す。"""
        if not email or not password:
            raise NoteError("NOTE_EMAIL / NOTE_PASSWORD が空です")
        code, headers, text = _curl(
            runner, "POST", "/api/v1/sessions/sign_in", {"login": email, "password": password}, cookie=None,
            dump_headers=True,
        )
        if not 200 <= code < 300:
            raise NoteError(
                f"note へのログインに失敗しました（HTTP {code}）。メールアドレス・パスワードを確認してください。"
                f"正しいのに失敗する場合は note 側のロボット対策の可能性があります: {text[:200]}"
            )
        cookies = re.findall(r"(?im)^set-cookie:\s*([^=;\s]+=[^;\r\n]*)", headers)
        if not cookies:
            # 原因調査用に、値を伏せたヘッダ名と本文の先頭だけを出す
            names = sorted({l.split(":", 1)[0].strip().lower() for l in headers.splitlines() if ":" in l})
            raise NoteError(
                f"note にログインできましたが、Cookie が返ってきませんでした（HTTP {code}）。"
                f"応答ヘッダ: {', '.join(names) or 'なし'} / 本文の形: {_shape(text)}"
            )
        try:
            user = (json.loads(text).get("data") or {}) if text.strip() else {}
        except json.JSONDecodeError:
            user = {}
        return cls("; ".join(cookies), urlname or user.get("urlname"), runner)

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        code, _, text = _curl(self._run, method, path, body, cookie=self.cookie)
        if not 200 <= code < 300:
            hint = ""
            if code in (401, 403):
                hint = "（ログインの期限切れか、note 側のアクセス制限の可能性があります）"
            raise NoteError(f"{method} {path} が HTTP {code} で失敗しました{hint}: {text[:300]}")
        try:
            return json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as e:
            raise NoteError(f"{method} {path} の応答が JSON ではありません: {text[:200]}") from e

    def logged_in(self) -> bool:
        """この Cookie で note にログインできているか。"""
        code, _, text = _curl(self._run, "GET", "/api/v2/current_user", None, cookie=self.cookie)
        if code != 200:
            return False
        try:
            return bool((json.loads(text).get("data") or {}).get("urlname"))
        except (json.JSONDecodeError, AttributeError):
            return False

    def _whoami(self) -> str:
        """同じ Cookie でログイン中のユーザーを問い合わせ、結果の要点だけを返す（個人情報は出さない）。"""
        try:
            code, _, text = _curl(self._run, "GET", "/api/v2/current_user", None, cookie=self.cookie)
        except NoteError as e:
            return f"問い合わせ失敗 {e}"
        return f"HTTP {code} {_shape(text)}"

    def _xsrf(self) -> str | None:
        """画像アップロードに添える XSRF トークン。Cookie に無ければ note に発行してもらう。"""
        if getattr(self, "_xsrf_token", None) is None:
            token = _xsrf_from_cookie(self.cookie)
            if not token:
                try:
                    _, head, _ = _curl(self._run, "GET", "/api/v2/current_user", None, cookie=self.cookie,
                                       dump_headers=True)
                    m = re.search(r"(?im)^set-cookie:\s*XSRF-TOKEN=([^;\r\n]+)", head)
                    token = m.group(1) if m else ""
                except NoteError:
                    token = ""
            self._xsrf_token = urllib.parse.unquote(token)
        return self._xsrf_token or None

    def _upload(self, path: str, fields: dict[str, str], file: Path | None) -> dict:
        code, text = _curl_form(self._run, f"{BASE}{path}", fields, ("file", file) if file else None,
                                cookie=self.cookie, xsrf=self._xsrf())
        if not 200 <= code < 300:
            raise NoteError(f"POST {path} が HTTP {code} で失敗しました: {_shape(text)}")
        try:
            return json.loads(text).get("data") or {}
        except (json.JSONDecodeError, AttributeError) as e:
            raise NoteError(f"POST {path} の応答が JSON ではありません: {_shape(text)}") from e

    def upload_eyecatch(self, note_id: str, image: Path) -> str:
        """記事の見出し画像を設定する（1280x670 推奨）。送るだけで記事に紐づく。"""
        width, height = _image_size(image)
        fields = {"note_id": str(note_id)}
        if width and height:
            fields.update(width=str(width), height=str(height))
        data = self._upload("/api/v1/image_upload/note_eyecatch", fields, image)
        url = data.get("url") or data.get("eyecatch") or data.get("eyecatch_url") or ""
        if not url:
            raise NoteError(f"見出し画像の URL が返ってきませんでした: keys={sorted(data)}")
        return url

    def upload_body_image(self, image: Path) -> str:
        """本文に貼る画像を note の保管場所（S3）に置き、表示用の URL を返す。"""
        data = self._upload("/api/v3/images/upload/presigned_post", {"filename": image.name}, None)
        action, url, post = data.get("action"), data.get("url"), data.get("post")
        if not (str(action).startswith("https://") and url and isinstance(post, dict)):
            raise NoteError(f"画像の置き場所を受け取れませんでした: keys={sorted(data)}")
        code, text = _curl_form(self._run, action, {k: str(v) for k, v in post.items() if v not in (None, "")}, ("file", image))
        if not 200 <= code < 300:
            raise NoteError(f"画像のアップロードが HTTP {code} で失敗しました: {text[:200]}")
        return url

    def create(self, title: str, body_md: str, *, publish: bool, eyecatch: Path | None = None) -> NoteResult:
        body_html = markdown_to_note_html(body_md)
        body_length = len(re.sub(r"<[^>]+>", "", body_html))

        created = self._request("POST", "/api/v1/text_notes", {"template_key": None})
        data = created.get("data") or {}
        note_id, key = str(data.get("id", "")), str(data.get("key", ""))
        if not note_id or not key:
            # 原因調査用に Cookie の「名前」だけを出す（値は出さない）
            names = [c.split("=", 1)[0].strip() for c in self.cookie.split(";") if "=" in c]
            raise NoteError(
                f"記事の枠を作れませんでした: {json.dumps(created, ensure_ascii=False)[:300]} "
                f"/ 渡した Cookie の名前: {', '.join(names) or 'なし'}（{len(self.cookie)}文字）"
                f" / ログイン確認: {self._whoami()}"
            )
        urlname = self.urlname or (data.get("user") or {}).get("urlname")

        self._request(
            "POST",
            f"/api/v1/text_notes/draft_save?id={note_id}&is_temp_saved=true",
            {"body": body_html, "body_length": body_length, "name": title, "index": False, "is_lead_form": False},
        )
        eyecatch_url, warnings = None, []
        if eyecatch:
            # 見出し画像が付けられなくても記事そのものは残す
            try:
                eyecatch_url = self.upload_eyecatch(note_id, eyecatch)
            except NoteError as e:
                warnings.append(f"見出し画像を付けられませんでした（{e}）")
        if not publish:
            return NoteResult(note_id, key, "draft", None, eyecatch_url, warnings)

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
        return NoteResult(note_id, key, "published", url, eyecatch_url, warnings)
