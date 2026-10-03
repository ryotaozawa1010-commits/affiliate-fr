# マーケットレポート自動投稿（note）

Cowork が毎日作るマーケットレポートの公開版から、note 記事を自動で作るための仕組みです。
X への投稿機能もありますが、**現在は使っていません**（キーを登録したときだけ動きます。下の「X（停止中）」を参照）。

## 全体の流れ

```
Cowork ルーティン（平日 引け後版 / 日曜 週末版）
  │ ① note 記事を書く
  │ ② Gmail コネクタ（ryotaozawa1010@gmail.com）から
  │    受け取り用アドレス（u1132038801@gmail.com）へ件名 [PUBLISH] のメールを送る
  ▼
GitHub Actions「マーケットレポート投稿（note）」（10分おきに受け取り用 Gmail を確認）
  │ ③ 差出人が PUBLISH_SENDER で、合言葉（PUBLISH_TOKEN）が一致するメールだけ受け取る
  │ ④ publish/inbox/ に保存
  │ ⑤ note に記事を作る（下書き or 公開）
  ▼ ⑥ 結果を publish/done/*.json に記録し、メールに「published」ラベル（二重投稿しない）
```

Gmail を「郵便受け」にしているのは、Cowork がもともと Gmail コネクタを持っていて、
途中に AI を挟まずに文章を一字一句そのまま GitHub まで運べるからです。
受け取り用アドレスを分けてあるので、メインの受信箱のパスワードは GitHub に渡しません。

確認する時間帯（UTC）: 平日 20:00〜23:59、日曜 06:00〜14:59。Cowork の実行時刻を変えたら
`.github/workflows/publish.yml` の `cron` も合わせてください。

## 初期設定（最初の1回だけ）

GitHub のリポジトリ画面で **Settings → Secrets and variables → Actions** を開いて登録します
（「Secrets」タブと「Variables」タブがあります）。

### Gmail（必須・郵便受け）

受け取り用アカウント **u1132038801@gmail.com** で次を行います。

1. 2段階認証を有効にする（Google アカウント → セキュリティとログイン → 2段階認証プロセス）
2. https://myaccount.google.com/apppasswords で「アプリ パスワード」を作成（名前は何でも可）
3. 登録

| 種類 | 名前 | 中身 |
|---|---|---|
| Secret | `GMAIL_ADDRESS` | `u1132038801@gmail.com` |
| Secret | `GMAIL_APP_PASSWORD` | 上で作った16文字のアプリ パスワード |
| Secret | `PUBLISH_TOKEN` | Cowork のルーティンの指示文に書いてある `TOKEN:` の値（合言葉。セットアップ時に Claude から伝えた値） |
| Variable | `PUBLISH_SENDER` | `ryotaozawa1010@gmail.com`（Cowork がメールを送るアドレス） |

アプリ パスワードは「受信箱の読み取り＋ラベル付け」にしか使いません（送信や削除はしません）。

### note（必須）

note には公式の投稿 API がないため、**ログイン済みブラウザの Cookie** を使ってエディタと同じ操作を再現します。

1. PC のブラウザで note にログイン
2. 開発者ツール（F12）→ Network タブ → note.com へのリクエストを1つ選ぶ → Request Headers の `cookie:` の値を丸ごとコピー
3. Secret `NOTE_COOKIE` に貼り付け
4. Variable `NOTE_URLNAME` = あなたの note ID（`https://note.com/<ここ>`）

| Variable `NOTE_MODE` | 動き |
|---|---|
| 未設定 / `draft`（既定） | 下書きに保存。note アプリで「公開」を押すだけの状態になる |
| `publish` | そのまま公開する（**実験的**） |

最初の数日は `draft` で見た目（見出し・太字・箇条書きの変換）を確認し、問題なければ `publish` に切り替えるのがおすすめです。

注意:
- Cookie は数週間〜数か月で切れます。切れると Actions が失敗して GitHub からメールが届くので、取り直して貼り直してください。
- note 側の仕様変更やアクセス制限で、ある日突然動かなくなることがあります。
- note は自動投稿を推奨していません。公開まで自動にするかどうかはご自身で判断してください。

### X（停止中・任意）

再開したくなったら、Cowork の指示文に X スレッドの作成を戻し、次を登録すれば動きます。

1. https://developer.x.com でアプリを作成し、権限を **Read and write** にしてからトークンを発行
2. Secrets に `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET`
3. Developer Console でクレジット購入と利用上限の設定（2026年時点で1ポスト 約 $0.015、URL 入りは 約 $0.20）

日本語 140 字を超えるポストは自動で分割してスレッドにつなぎます（X Premium なら Variable `X_PREMIUM` = `true` で分割しない）。

## 動作確認

Actions タブ → 「マーケットレポート投稿（note）」→ **Run workflow**（`dry_run` にチェック）で実行すると、
受け取り用 Gmail の新着を見て、**投稿せずに** note の記事タイトルと文字数を Summary に表示します。

件名が `[PUBLISH] YYYY-MM-DD_test` のメールは常にこの確認モードで処理されます（本番投稿されません）。
セットアップ時に u1132038801@gmail.com へテストメール `[PUBLISH] 2026-10-03_test` を送ってあるので、
Secrets を登録したらこれで受け取りまで確認できます（3日以内のメールだけ見るので、それを過ぎたら同じ形式のメールを送り直してください）。

## 止めたいとき

- 一時停止: Actions タブ → 「マーケットレポート投稿（note）」→ 右上の「…」→ **Disable workflow**
- note だけ止める: Secret `NOTE_COOKIE` を削除

## ファイル構成

| パス | 役割 |
|---|---|
| `publisher/gmail_inbox.py` | 受け取り用 Gmail から投稿用メールを受け取る（なりすまし検査つき） |
| `publisher/parse.py` | 公開版 Markdown を note 記事（と X スレッド）に分解 |
| `publisher/note.py` | Markdown → note 用 HTML 変換、note への下書き保存・公開 |
| `publisher/xpost.py` | X の文字数計算・分割・OAuth 署名・スレッド投稿（停止中） |
| `publisher/main.py` | 全体の流れと二重投稿防止 |
| `publish/inbox/` | 受け取った公開版（Gmail から保存したもの） |
| `publish/done/` | 投稿結果の記録（note の URL など） |
| `publisher/COWORK_PROMPT.md` | Cowork ルーティンに追記した指示の控え |

テスト: `python -m unittest discover -s publisher/tests -t .`
