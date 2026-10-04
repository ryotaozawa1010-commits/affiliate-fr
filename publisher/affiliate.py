"""note 記事に広告（アフィリエイト）枠を足す。

`publisher/affiliate.md` に書いた Markdown を記事の末尾に付け、冒頭に「PR」の表記を入れる。
ファイルが空（コメントだけ）のあいだは記事に何も足さない。

ステルスマーケティング規制（2023年10月〜）で、広告を含む記事は読者にそれと分かる表記が要るため、
末尾に広告を付けるときは必ず冒頭の表記もセットで入れる。
"""

from __future__ import annotations

import re
from pathlib import Path

FOOTER = Path(__file__).with_name("affiliate.md")
DISCLOSURE = "*※この記事にはプロモーション（アフィリエイトリンク）を含みます。*"
COMMENT = re.compile(r"<!--.*?-->", re.S)


def footer(path: Path | None = None) -> str:
    path = path or FOOTER
    if not path.exists():
        return ""
    return COMMENT.sub("", path.read_text(encoding="utf-8")).strip()


def apply(body: str, path: Path | None = None) -> str:
    extra = footer(path)
    if not extra:
        return body
    return f"{DISCLOSURE}\n\n{body.rstrip()}\n\n---\n\n{extra}\n"
