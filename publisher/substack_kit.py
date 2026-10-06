"""Substack（仏語記事）の「投稿セット」をメールで届ける。

Substack は GitHub Actions のようなデータセンターからの通信を Cloudflare のロボット確認で止めるため
（2026-10-06 に substack.com・marchsenbref.substack.com とも「Just a moment...」で確認）、自動投稿はしない。
代わりに、スマホの Substack アプリでコピペして公開できるよう、次の一式を 1 通のメールにまとめて送る。

- タイトル・サブタイトル
- 本文（見出し・太字・箇条書きの書式つき。グラフを入れる場所に目印）
- 見出し画像（cover.png）とグラフ画像（chart_N.png）の添付

送信には受け取り用 Gmail（GMAIL_ADDRESS / GMAIL_APP_PASSWORD）を使い、宛先は Ryota 本人。
"""

from __future__ import annotations

import html
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from publisher.note import markdown_to_note_html

KIT_HOST = "https://substack-kit.invalid/"  # 本文中の画像の仮の置き場所（メールでは添付画像に差し替える）


class KitError(RuntimeError):
    pass


def kit_upload(path: Path) -> str:
    """charts.embed に渡す「アップロード」。実際には送らず、仮の URL を返す。"""
    return KIT_HOST + path.name


def _body_html(body_md: str) -> tuple[str, list[str]]:
    """本文を HTML にし、グラフの位置に目印と画像（メール内に表示）を入れる。使った画像のファイル名も返す。"""
    body = markdown_to_note_html(body_md)
    names: list[str] = []

    def figure(m: re.Match) -> str:
        name, caption = m.group(1), m.group(2)
        names.append(name)
        n = len(names)
        return (
            f'<p style="background:#fff4e5;border-left:4px solid #c41e24;padding:8px 12px;font-family:sans-serif">'
            f"<b>【グラフ{n}】ここに添付の {html.escape(name)} を入れる</b><br>"
            f"キャプション: {caption}</p>"
            f'<p><img src="cid:{html.escape(name)}" alt="" width="560" style="max-width:100%"></p>'
        )

    body = re.sub(
        r'<figure[^>]*><img src="' + re.escape(KIT_HOST) + r'([^"]+)"[^>]*><figcaption>(.*?)</figcaption></figure>',
        figure, body,
    )
    # note 用の name / id 属性はメールには要らない
    body = re.sub(r' name="[0-9a-f-]{36}" id="[0-9a-f-]{36}"', "", body)
    return body, names


def build(title: str, subtitle: str, body_md: str, *, cover: Path | None, media_dir: Path,
          sender: str, to: str, date_label: str = "") -> EmailMessage:
    body_html, chart_names = _body_html(body_md)
    images = ([cover] if cover else []) + [media_dir / n for n in chart_names]
    missing = [p.name for p in images if not p.exists()]
    if missing:
        raise KitError(f"画像が見つかりません: {', '.join(missing)}")

    steps = [
        "Substack アプリで新しい記事（Article）を開く",
        "下の Titre・Sous-titre をそれぞれコピーして貼る",
        "「本文ここから」〜「本文ここまで」の間を選んでコピーし、本文に貼る",
    ]
    if chart_names:
        steps.append("【グラフN】の目印の行を消し、そこに添付の chart_N.png を入れる（キャプションは目印に書いた文）")
    if cover:
        steps.append("公開設定の「Cover image」（サムネ）に添付の cover.png を入れる")
    steps.append("プレビューを確認して公開（今すぐ全員に送信）")

    box = "font-family:sans-serif;background:#f6f6f6;padding:10px 14px;border-radius:6px"
    html_doc = (
        '<div style="max-width:680px">'
        f'<div style="{box}"><b>Substack 投稿セット{(" " + html.escape(date_label)) if date_label else ""}</b>'
        "<ol>" + "".join(f"<li>{html.escape(s)}</li>" for s in steps) + "</ol></div>"
        '<p style="font-family:sans-serif;color:#777">Titre</p>'
        f"<h1>{html.escape(title)}</h1>"
        '<p style="font-family:sans-serif;color:#777">Sous-titre</p>'
        f"<p><i>{html.escape(subtitle)}</i></p>"
        '<p style="font-family:sans-serif;color:#c41e24">――― 本文ここから ―――</p>'
        f"{body_html}"
        '<p style="font-family:sans-serif;color:#c41e24">――― 本文ここまで ―――</p>'
        "</div>"
    )
    plain = "\n".join(
        [f"Substack 投稿セット {date_label}".strip(), ""]
        + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
        + ["", "Titre:", title, "", "Sous-titre:", subtitle, "", "――― 本文ここから ―――", body_md,
           "――― 本文ここまで ―――"]
    )

    msg = EmailMessage()
    msg["Subject"] = f"[Substack] 投稿セット: {title}"
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(plain)
    msg.add_alternative(html_doc, subtype="html")
    rich = msg.get_payload()[1]
    for path in images:
        if path.name in chart_names:
            rich.add_related(path.read_bytes(), "image", "png", cid=f"<{path.name}>")
    for path in images:
        msg.add_attachment(path.read_bytes(), maintype="image", subtype="png", filename=path.name)
    return msg


def send(msg: EmailMessage, *, address: str, app_password: str, smtp=smtplib.SMTP_SSL) -> None:
    try:
        with smtp("smtp.gmail.com", 465, timeout=60) as server:
            server.login(address, app_password.replace(" ", ""))
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise KitError(f"メールを送れませんでした: {e}") from e
