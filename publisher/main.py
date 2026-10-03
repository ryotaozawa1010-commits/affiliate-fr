"""publish/inbox/*.md を読んで note と X に投稿し、結果を publish/done/*.json に残す。

使い方:
    python -m publisher.main publish/inbox/2026-10-04_weekend.md [--dry-run]

環境変数（GitHub の Secrets / Variables から渡す）:
    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   … X 投稿用。無ければ X はスキップ
    X_PREMIUM=true      … X Premium で長文ポストが使える場合（分割しない）
    NOTE_COOKIE         … note にログインしたブラウザの Cookie。無ければ note はスキップ
    NOTE_URLNAME        … note のユーザー名（https://note.com/<ここ>）
    NOTE_MODE=draft|publish   … 既定は draft（下書き保存）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from publisher.note import NoteClient, NoteError
from publisher.parse import ParseError, parse
from publisher.xpost import XCredentials, XPostError, build_thread, post_thread, weighted_length

DONE_DIR = Path("publish/done")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _x_creds() -> XCredentials | None:
    values = [_env(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    return XCredentials(*values) if all(values) else None


def _summary(lines: list[str]) -> None:
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def process(path: Path, *, dry_run: bool) -> bool:
    """1 ファイル分を処理する。失敗があれば False。"""
    result_path = DONE_DIR / f"{path.stem}.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    result.setdefault("source", str(path))
    out = [f"## {path.name}"]
    ok = True

    if path.stem.endswith("_test"):
        dry_run = True
        out.append("ファイル名が `_test` で終わるため、テストとして下書きプレビューだけ行います。")

    try:
        payload = parse(path.read_text(encoding="utf-8"))
    except ParseError as e:
        _summary(out + [f"❌ 読み取り失敗: {e}"])
        return False

    premium = _env("X_PREMIUM").lower() == "true"
    publish_note = _env("NOTE_MODE").lower() == "publish"

    # ---- note（X の最後のポストに記事リンクを付けるため先に処理する）
    note_url = (result.get("note") or {}).get("url")
    if payload.note_title:
        if result.get("note"):
            out.append(f"note: 処理済みのためスキップ（{result['note']['status']}）")
        elif dry_run:
            out.append(f"note（プレビュー）: 「{payload.note_title}」 本文 {len(payload.note_body)} 字 / "
                       f"{'公開' if publish_note else '下書き'}予定")
        elif not _env("NOTE_COOKIE"):
            out.append("note: NOTE_COOKIE が未設定のためスキップ")
        else:
            try:
                r = NoteClient(_env("NOTE_COOKIE"), _env("NOTE_URLNAME") or None).create(
                    payload.note_title, payload.note_body, publish=publish_note
                )
                result["note"] = {"id": r.note_id, "key": r.key, "status": r.status, "url": r.url}
                note_url = r.url
                out.append(f"✅ note: {'公開しました ' + r.url if r.url else '下書きに保存しました（note アプリで公開してください）'}")
            except NoteError as e:
                ok = False
                out.append(f"❌ note: {e}")

    # ---- X
    if payload.x_posts:
        thread = build_thread(payload.x_posts, link=note_url, premium=premium)
        if (result.get("x") or {}).get("ids"):
            out.append(f"X: 処理済みのためスキップ（{len(result['x']['ids'])} 本）")
        elif dry_run:
            out.append(f"X（プレビュー）: {len(thread)} ポスト")
            for i, t in enumerate(thread, 1):
                out.append(f"\n**{i}/{len(thread)}**（{weighted_length(t)}/{25000 if premium else 280}）\n```\n{t}\n```")
        elif not _x_creds():
            out.append("X: API キーが未設定のためスキップ")
        else:
            try:
                ids = post_thread(thread, _x_creds())
                result["x"] = {"ids": ids}
                out.append(f"✅ X: {len(ids)} ポストのスレッドを投稿しました https://x.com/i/status/{ids[0]}")
            except XPostError as e:
                ok = False
                # 途中まで出たポストは記録して、再実行で二重投稿しないようにする
                if e.posted_ids:
                    result["x"] = {"ids": e.posted_ids, "partial": True}
                out.append(f"❌ X: {e}")

    if not dry_run:
        result["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        DONE_DIR.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    _summary(out)
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="投稿せずに、投稿される内容だけ表示する")
    args = ap.parse_args(argv)
    ok = True
    for f in args.files:
        if f.exists():
            ok = process(f, dry_run=args.dry_run) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
