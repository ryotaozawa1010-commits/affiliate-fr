"""Cowork が出力する「公開・投稿用」Markdown を X スレッドと note 記事に分解する。

想定フォーマット（週末版の 公開版_YYYY-MM-DD.md と同じ）:

    # ① X投稿スレッド（5ポスト構成）
    ### 1/5
    本文...
    ### 2/5
    ...
    # ② note記事
    ## タイトル
    **【米国株】2026/09/27 今週の振り返りと週明けの展望**
    ## 本文
    本文...

X ポスト内の `---` だけの行は区切りとして無視する（note 本文内のものは残す）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

X_HEADER = re.compile(r"^#\s*①?\s*X投稿")
NOTE_HEADER = re.compile(r"^#\s*②?\s*note記事")
POST_HEADER = re.compile(r"^###\s*(\d+)\s*/\s*(\d+)\s*$")
RULE = re.compile(r"^\s*-{3,}\s*$")
TOP_HEADER = re.compile(r"^#\s")


class ParseError(ValueError):
    pass


@dataclass
class Payload:
    x_posts: list[str] = field(default_factory=list)
    note_title: str = ""
    note_body: str = ""  # Markdown


def _strip_rules(lines: list[str]) -> list[str]:
    return [l for l in lines if not RULE.match(l)]


def _clean_block(lines: list[str]) -> str:
    return "\n".join(_strip_rules(lines)).strip()


def parse(text: str) -> Payload:
    lines = text.replace("\r\n", "\n").split("\n")

    x_start = next((i for i, l in enumerate(lines) if X_HEADER.match(l)), None)
    note_start = next((i for i, l in enumerate(lines) if NOTE_HEADER.match(l)), None)
    if x_start is None and note_start is None:
        raise ParseError("「# ① X投稿」も「# ② note記事」も見つかりません")

    payload = Payload()

    if x_start is not None:
        end = note_start if note_start is not None and note_start > x_start else len(lines)
        current: list[str] | None = None
        for line in lines[x_start + 1 : end]:
            if POST_HEADER.match(line):
                if current is not None:
                    payload.x_posts.append(_clean_block(current))
                current = []
            elif current is not None:
                current.append(line)
        if current is not None:
            payload.x_posts.append(_clean_block(current))
        payload.x_posts = [p for p in payload.x_posts if p]
        if not payload.x_posts:
            raise ParseError("X投稿セクションに「### 1/N」形式のポストがありません")

    if note_start is not None:
        # note 記事は次の大見出し（「# ③ Substack記事」など）の手前まで
        end = next((i for i in range(note_start + 1, len(lines)) if TOP_HEADER.match(lines[i])), len(lines))
        section = lines[note_start + 1 : end]
        title_i = next((i for i, l in enumerate(section) if re.match(r"^##\s*タイトル", l)), None)
        body_i = next((i for i, l in enumerate(section) if re.match(r"^##\s*本文", l)), None)
        if title_i is None or body_i is None or body_i < title_i:
            raise ParseError("note記事セクションに「## タイトル」「## 本文」がありません")
        title = _clean_block(section[title_i + 1 : body_i])
        payload.note_title = re.sub(r"\*\*(.+?)\*\*", r"\1", title).strip()
        body = section[body_i + 1 :]
        # 記事内の区切り線は残し、末尾にぶら下がった区切り線だけ落とす
        while body and (not body[-1].strip() or RULE.match(body[-1])):
            body.pop()
        payload.note_body = "\n".join(body).strip()
        if not payload.note_title or not payload.note_body:
            raise ParseError("note記事のタイトルか本文が空です")

    return payload
