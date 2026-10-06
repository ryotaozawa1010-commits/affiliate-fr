"""note と Substack の入館証（NOTE_COOKIE / SUBSTACK_COOKIE）がまだ有効かを確かめる。記事は作らない。

切れていれば終了コード 1 で終わるので、GitHub から失敗通知メールが届く。
レポートが届く前に気づいて貼り直せるよう、毎日の投稿時間帯より前に動かす。
"""

from __future__ import annotations

import os
import sys

from publisher.note import NoteClient, NoteError
from publisher.substack import SubstackClient, SubstackError


def check_substack() -> int:
    cookie = os.environ.get("SUBSTACK_COOKIE", "").strip()
    if not cookie:
        print("SUBSTACK_COOKIE は未設定です（Substack は使っていない扱い）")
        return 0
    try:
        ok = SubstackClient(cookie, os.environ.get("SUBSTACK_URL", "").strip() or None).logged_in()
    except SubstackError as e:
        print(f"Substack の確認に失敗しました: {e}")
        return 1
    if ok:
        print("✅ Substack の入館証は有効です")
        return 0
    print("❌ Substack の入館証が切れています。PC の Chrome（Ryota のプロフィール）で Substack を開き、"
          "connect.sid の値を GitHub の SUBSTACK_COOKIE に貼り直してください。")
    return 1


def main() -> int:
    return max(check_note(), check_substack())


def check_note() -> int:
    cookie = os.environ.get("NOTE_COOKIE", "").strip()
    if not cookie:
        print("NOTE_COOKIE が未設定です")
        return 1
    try:
        client = NoteClient(cookie)
        ok = client.logged_in()
    except NoteError as e:
        print(f"確認に失敗しました: {e}")
        return 1
    if ok:
        print("✅ note の入館証は有効です")
        return 0
    print("❌ note の入館証が切れています。PC の Chrome（Ryota のプロフィール）で note を開き、"
          "_note_session_v5 の値を GitHub の NOTE_COOKIE に貼り直してください。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
