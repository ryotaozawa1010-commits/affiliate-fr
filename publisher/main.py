"""publish/inbox/*.md（Gmail から受け取った公開版）を読んで note と X に投稿し、結果を publish/done/*.json に残す。

使い方:
    python -m publisher.main publish/inbox/2026-10-04_weekend.md [--dry-run]

環境変数（GitHub の Secrets / Variables から渡す）:
    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   … X 投稿用。無ければ X はスキップ
    X_PREMIUM=true      … X Premium で長文ポストが使える場合（分割しない）
    NOTE_COOKIE         … note にログインしたブラウザの Cookie（優先）
    NOTE_EMAIL, NOTE_PASSWORD … Cookie が無いときのログイン情報（note が reCAPTCHA を求めると失敗する）
    NOTE_URLNAME        … note のユーザー名（https://note.com/<ここ>）
    NOTE_MODE=draft|publish   … 既定は draft（下書き保存）
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD … Substack の投稿セットをメールで送るのに使う（受け取り用 Gmail）
    SUBSTACK_KIT_TO     … 投稿セットの宛先（既定は ryotaozawa1010@gmail.com）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from publisher.charts import embed
from publisher.note import NoteClient, NoteError
from publisher.parse import ParseError, Payload, parse
from publisher import substack_kit
from publisher.thumbnail import make_thumbnail
from publisher.xpost import XCredentials, XPostError, build_thread, post_thread, weighted_length

DONE_DIR = Path("publish/done")
MEDIA_DIR = Path("publish/media")  # 見出し画像・グラフ画像の控え


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _x_creds() -> XCredentials | None:
    values = [_env(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")]
    return XCredentials(*values) if all(values) else None


def _note_client() -> NoteClient:
    urlname = _env("NOTE_URLNAME") or None
    # note はパスワードだけのログインに reCAPTCHA を求めるため、ブラウザの Cookie があればそちらを優先する
    if _env("NOTE_COOKIE"):
        return NoteClient(_env("NOTE_COOKIE"), urlname)
    return NoteClient.login(_env("NOTE_EMAIL"), _env("NOTE_PASSWORD"), urlname)


def _thumbnail(title: str, stem: str, out: list[str], *, lang: str = "ja") -> Path | None:
    """記事タイトル入りの見出し画像を作る。作れなくても記事は出すので None を返すだけにする。"""
    kind = "WEEKLY REVIEW" if stem.endswith("_weekend") else "MARKET CLOSE" if stem.endswith("_weekday") else ""
    try:
        if lang == "fr":
            # 仏語記事のタイトルには【タグ】や日付が入らないので、ファイル名の日付から足す
            y, m, d = (stem.split("_", 1)[0].split("-") + ["", "", ""])[:3]
            return make_thumbnail(title, MEDIA_DIR / stem / "substack" / "eyecatch.png", kind=kind,
                                  tag="Marchés US", date=f"{d}/{m}/{y}" if d else "")
        return make_thumbnail(title, MEDIA_DIR / stem / "eyecatch.png", kind=kind)
    except Exception as e:  # noqa: BLE001
        out.append(f"⚠️ 見出し画像を作れませんでした（{e}）")
        return None


def _substack(payload: Payload, stem: str, result: dict, out: list[str], *, dry_run: bool) -> bool:
    """Substack（仏語記事）の投稿セット（本文・サムネ・グラフ画像）を Ryota にメールで送る。失敗したら False。

    Substack はデータセンターからの通信をロボット確認で止めるので、投稿そのものは Ryota がアプリで行う。
    """
    if result.get("substack"):
        out.append("Substack: 投稿セットは送信済みのためスキップ")
        return True
    media = MEDIA_DIR / stem / "substack"
    cover = _thumbnail(payload.substack_title, stem, out, lang="fr")
    charts = embed(payload.substack_body, media, substack_kit.kit_upload, lang="fr")
    to = _env("SUBSTACK_KIT_TO") or "ryotaozawa1010@gmail.com"
    y, m, d = (stem.split("_", 1)[0].split("-") + ["", "", ""])[:3]
    try:
        msg = substack_kit.build(payload.substack_title, payload.substack_subtitle, charts.body, cover=cover,
                                 media_dir=media, sender=_env("GMAIL_ADDRESS") or to, to=to,
                                 date_label=f"{d}/{m}/{y}" if d else "")
        if dry_run:
            media.mkdir(parents=True, exist_ok=True)
            (media / "kit.eml").write_bytes(bytes(msg))
            out.append(f"Substack（プレビュー）: 「{payload.substack_title}」の投稿セットを作成（{media / 'kit.eml'}）")
            out.extend(charts.notes + [f"⚠️ {w}" for w in charts.warnings])
            return True
        if not (_env("GMAIL_ADDRESS") and _env("GMAIL_APP_PASSWORD")):
            out.append("Substack: GMAIL_ADDRESS / GMAIL_APP_PASSWORD が未設定のため投稿セットを送れません")
            return True
        substack_kit.send(msg, address=_env("GMAIL_ADDRESS"), app_password=_env("GMAIL_APP_PASSWORD"))
    except substack_kit.KitError as e:
        out.append(f"❌ Substack 投稿セット: {e}")
        return False
    result["substack"] = {"status": "kit_sent", "to": to, "cover": bool(cover), "charts": len(charts.notes)}
    out.append(f"✅ Substack: 投稿セットをメールで送りました（サムネ{'あり' if cover else 'なし'}・グラフ {len(charts.notes)} 枚）")
    out.extend(charts.notes + [f"⚠️ {w}" for w in charts.warnings])
    return True


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
    if path.stem.endswith("_drafttest"):
        # 接続確認用: note には必ず下書きで作り、X には出さない
        publish_note = False
        payload.x_posts = []
        out.append("ファイル名が `_drafttest` で終わるため、note に下書きだけ作ります（公開・X 投稿はしません）。")

    # ---- note（X の最後のポストに記事リンクを付けるため先に処理する）
    note_url = (result.get("note") or {}).get("url")
    if payload.note_title:
        if result.get("note"):
            out.append(f"note: 処理済みのためスキップ（{result['note']['status']}）")
        elif dry_run:
            eyecatch = _thumbnail(payload.note_title, path.stem, out)
            charts = embed(payload.note_body, MEDIA_DIR / path.stem, None)
            out.append(f"note（プレビュー）: 「{payload.note_title}」 本文 {len(payload.note_body)} 字 / "
                       f"{'公開' if publish_note else '下書き'}予定")
            if eyecatch:
                out.append(f"見出し画像を作成（{eyecatch}）")
            out += charts.notes + [f"⚠️ {w}" for w in charts.warnings]
        elif not (_env("NOTE_EMAIL") and _env("NOTE_PASSWORD")) and not _env("NOTE_COOKIE"):
            out.append("note: NOTE_COOKIE（または NOTE_EMAIL / NOTE_PASSWORD）が未設定のためスキップ")
        else:
            try:
                client = _note_client()
                eyecatch = _thumbnail(payload.note_title, path.stem, out)
                charts = embed(payload.note_body, MEDIA_DIR / path.stem, client.upload_body_image)
                r = client.create(payload.note_title, charts.body, publish=publish_note, eyecatch=eyecatch)
                result["note"] = {"id": r.note_id, "key": r.key, "status": r.status, "url": r.url,
                                  "eyecatch": bool(r.eyecatch), "charts": len(charts.notes)}
                note_url = r.url
                out.append(f"✅ note: {'公開しました ' + r.url if r.url else '下書きに保存しました（note アプリで公開してください）'}")
                if r.eyecatch:
                    out.append("見出し画像: 設定しました")
                out += charts.notes + [f"⚠️ {w}" for w in charts.warnings + r.warnings]
            except NoteError as e:
                ok = False
                out.append(f"❌ note: {e}")

    # ---- Substack（仏語記事。投稿セットをメールで届ける）
    if payload.substack_title:
        ok = _substack(payload, path.stem, result, out, dry_run=dry_run) and ok

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
