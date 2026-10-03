# マーケットレポート自動投稿（note / X）

Cowork が毎日作る「公開版」を、そのまま note と X に出すための仕組みです。

## 全体の流れ

```
Cowork ルーティン（平日 引け後版 / 日曜 週末版）
  │ ① 公開版（X スレッド + note 記事）を作る
  │ ② Gmail コネクタで「自分宛て」に件名 [PUBLISH] のメールを送る
  ▼
GitHub Actions「マーケットレポート投稿」（10分おきに Gmail を確認）
  │ ③ 差出人が自分 & 合言葉（PUBLISH_TOKEN）一致のメールだけ受け取る
  │ ④ publish/inbox/ に保存
  │ ⑤ note に記事を作る（下書き or 公開）
  │ ⑥ X にスレッドを投稿（公開した場合だけ最後のポストに note のリンクを付ける）
  ▼ ⑦ 結果を publish/done/*.json に記録し、メールに「published」ラベル（二重投稿しない）
```

Gmail を「郵便受け」にしているのは、Cowork がもともと Gmail コネクタを持っていて、
途中に AI を挟まずに文章を一字一句そのまま GitHub まで運べるからです。
X や note のキーは GitHub の Secrets にだけ置き、Cowork にも Claude にも渡りません。

確認する時間帯（UTC）: 平日 20:00〜23:59、日曜 06:00〜14:59。Cowork の実行時刻を変えたら
`.github/workflows/publish.yml` の `cron` も合わせてください。

## 初期設定（最初の1回だけ）

GitHub のリポジトリ画面で **Settings → Secrets and variables → Actions** を開いて登録します。

### Gmail（必須・郵便受け）

1. Google アカウントで2段階認証を有効にする（済みなら不要）
2. https://myaccount.google.com/apppasswords で「アプリ パスワード」を作成（名前は何でも可）
3. Secrets に登録

| Secret 名 | 中身 |
|---|---|
| `GMAIL_ADDRESS` | Cowork が送信に使う Gmail アドレス |
| `GMAIL_APP_PASSWORD` | 上で作った16文字のアプリ パスワード |
| `PUBLISH_TOKEN` | Cowork のルーティンの指示文に書いてある `TOKEN:` の値（合言葉。セットアップ時に Claude から伝えた値） |

アプリ パスワードは「読み取り＋ラベル付け」にしか使いません（メールの送信や削除はしません）。

### X（必須）

1. https://developer.x.com でアカウントを作り、アプリ（Project / App）を作成
2. アプリの **User authentication settings** で権限を **Read and write** にする
   （先に Read だけで作ったトークンは書き込みできないので、権限変更後にトークンを作り直す）
3. **Keys and tokens** から4つを取得し、Secrets に登録

| Secret 名 | 中身 |
|---|---|
| `X_API_KEY` | API Key（Consumer Key） |
| `X_API_SECRET` | API Key Secret |
| `X_ACCESS_TOKEN` | Access Token |
| `X_ACCESS_SECRET` | Access Token Secret |

4. Developer Console でクレジットを購入し、使いすぎ防止の上限（Spending limit）を設定

**料金の目安**（2026年時点の従量課金。最新は X の Developer Console で確認してください）:
1ポスト 約 $0.015、URL 入りのポストは 約 $0.20。
平日 3 ポスト × 月22回 + 週末 5 ポスト × 月4回（計 約86ポスト）で、リンクなしなら月 約 $1.5、毎回 note のリンクを付けると月 約 $6。

X Premium に入っていて長文ポストを使えるなら、Variables に `X_PREMIUM` = `true` を入れると分割しません。
入れない場合は、140字（全角）を超えたポストは自動で複数ポストに分けて同じスレッドにつなぎます。

### note（任意）

note には公式の投稿 API がないため、**ログイン済みブラウザの Cookie** を使ってエディタと同じ操作を再現します。

1. PC のブラウザで note にログイン
2. 開発者ツール（F12）→ Network タブ → note.com へのリクエストを1つ選ぶ → Request Headers の `cookie:` の値を丸ごとコピー
3. Secret `NOTE_COOKIE` に貼り付け
4. Variables に `NOTE_URLNAME` = あなたの note ID（`https://note.com/<ここ>`）

| Variable `NOTE_MODE` | 動き |
|---|---|
| 未設定 / `draft`（既定） | 下書きに保存。note アプリで「公開」を押すだけの状態になる。X には note のリンクを付けない |
| `publish` | そのまま公開して、X の最後のポストに記事リンクを付ける（**実験的**） |

注意:
- Cookie は数週間〜数か月で切れます。切れると Actions が失敗して GitHub からメールが届くので、取り直して貼り直してください。
- note 側の仕様変更やアクセス制限で、ある日突然動かなくなることがあります。そのときも X の投稿は止まりません（note だけ失敗扱いになります）。
- note は自動投稿を推奨していません。公開まで自動にするかどうかはご自身で判断してください。

## 動作確認

Actions タブ → 「マーケットレポート投稿」→ **Run workflow**（`dry_run` にチェック）で実行すると、
Gmail の新着を見て、**投稿せずに**「X にどう分割されて何文字で出るか」「note の記事タイトル」を Summary に表示します。

件名が `[PUBLISH] YYYY-MM-DD_test` のメールは常にこの確認モードで処理されます（本番投稿されません）。
セットアップ時に送ったテストメール `[PUBLISH] 2026-10-03_test` が受信箱にあるので、
Secrets を登録したらこれで受け取りまで確認できます（3日以内のメールだけ見るので、それを過ぎたら自分で同じ形式のメールを送ってください）。

## 止めたいとき

- 一時停止: Actions タブ → 「マーケットレポート投稿」→ 右上の「…」→ **Disable workflow**
- X だけ止める: Secret `X_API_KEY` を削除
- note だけ止める: Secret `NOTE_COOKIE` を削除

## ファイル構成

| パス | 役割 |
|---|---|
| `publisher/parse.py` | 公開版 Markdown を X スレッドと note 記事に分解 |
| `publisher/xpost.py` | X の文字数計算・分割・OAuth 署名・スレッド投稿 |
| `publisher/note.py` | Markdown → note 用 HTML 変換、note への下書き保存・公開 |
| `publisher/main.py` | 全体の流れと二重投稿防止 |
| `publisher/gmail_inbox.py` | Gmail から投稿用メールを受け取る（なりすまし検査つき） |
| `publish/inbox/` | 受け取った公開版（Gmail から保存したもの） |
| `publish/done/` | 投稿結果の記録（ポスト ID、note の URL） |
| `publisher/COWORK_PROMPT.md` | Cowork ルーティンに追記した指示の控え |

テスト: `python -m unittest discover -s publisher/tests -t .`
