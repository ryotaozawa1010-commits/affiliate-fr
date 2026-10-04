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
  │ ⑤ タイトル入りの見出し画像（フクロウの TRADING 画像）と、本文中の ```chart から
  │    出典付きのグラフ画像を作る
  │ ⑥ note に記事を作る（下書き or 公開）。見出し画像を設定し、グラフを本文に差し込む
  ▼ ⑦ 結果を publish/done/*.json に記録し、メールに「published」ラベル（二重投稿しない）
```

Gmail を「郵便受け」にしているのは、Cowork がもともと Gmail コネクタを持っていて、
途中に AI を挟まずに文章を一字一句そのまま GitHub まで運べるからです。
受け取り用アドレスを分けてあるので、メインの受信箱のパスワードは GitHub に渡しません。

確認する時間帯（UTC）: 平日 20:00〜23:59、日曜 06:00〜14:59。Cowork の実行時刻を変えたら
`.github/workflows/publish.yml` の `cron` も合わせてください。

## 初期設定（最初の1回だけ）

受け取り用アドレス（u1132038801@gmail.com）と差出人（ryotaozawa1010@gmail.com）は
`.github/workflows/publish.yml` に書いてあるので登録不要です。登録するのはパスワード類の4つだけです。

### 1. Gmail のアプリ パスワードを作る

受け取り用アカウント **u1132038801@gmail.com** で、2段階認証を有効にしたうえで
https://myaccount.google.com/apppasswords を開き、名前（例: github）を入れて「作成」。
表示された16文字（スペースは入っていてもOK）を控えます。

アプリ パスワードは「受信箱の読み取り＋ラベル付け」にしか使いません（送信や削除はしません）。

### 2. GitHub に Secret を4つ登録する

https://github.com/ryotaozawa1010-commits/affiliate-fr/settings/secrets/actions/new を開き、
「Name」と「Secret」を入れて **Add secret** を押す、を4回繰り返します。

| Name | Secret に入れるもの |
|---|---|
| `GMAIL_APP_PASSWORD` | 1 で作った16文字 |
| `PUBLISH_TOKEN` | Cowork のルーティンの指示文にある `TOKEN:` の値（セットアップ時に Claude から伝えた値） |
| `NOTE_EMAIL` | note にログインするメールアドレス |
| `NOTE_PASSWORD` | note のパスワード |

Secret は登録した本人にも二度と表示されません（上書きはできます）。リポジトリが公開でも、中身は外から見えません。

### 3.（任意）設定の上書き

https://github.com/ryotaozawa1010-commits/affiliate-fr/settings/variables/actions/new で Variable を追加すると動きを変えられます。

| Name | 動き |
|---|---|
| `NOTE_MODE` | 未設定 / `draft`: 下書きに保存（既定）。`publish`: そのまま公開（**実験的**） |
| `NOTE_URLNAME` | note の ID。通常はログイン時に自動で取れるので不要 |
| `GMAIL_ADDRESS` / `PUBLISH_SENDER` | 受け取り用・差出人のアドレスを変えたいとき |

最初の数日は `draft` で見た目（見出し・太字・箇条書きの変換）を確認し、問題なければ `publish` に切り替えるのがおすすめです。

### note についての注意

- note には公式の投稿 API がないため、ブラウザのエディタと同じ通信を再現しています。
  note 側の仕様変更やロボット対策で、ある日突然動かなくなることがあります（失敗すると GitHub からメールが届きます）。
- メールとパスワードでのログインが弾かれる場合は、代わりに PC のブラウザから Cookie を取り出して Secret `NOTE_COOKIE` に入れる方法もあります
  （開発者ツール → Network → note.com へのリクエスト → Request Headers の `cookie:` の値）。
- note は自動投稿を推奨していません。公開まで自動にするかどうかはご自身で判断してください。

### X（停止中・任意）

再開したくなったら、Cowork の指示文に X スレッドの作成を戻し、次を登録すれば動きます。

1. https://developer.x.com でアプリを作成し、権限を **Read and write** にしてからトークンを発行
2. Secrets に `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_SECRET`
3. Developer Console でクレジット購入と利用上限の設定（2026年時点で1ポスト 約 $0.015、URL 入りは 約 $0.20）

日本語 140 字を超えるポストは自動で分割してスレッドにつなぎます（X Premium なら Variable `X_PREMIUM` = `true` で分割しない）。

## 見出し画像とグラフ

- **見出し画像**: `publisher/assets/thumbnail_base.jpg`（TRADING のフクロウ）を右に、記事タイトルを
  「【タグ】・日付・見出し」に分けて左に描いた 1280x670 の画像を毎回作り、note の見出し画像に設定します。
  手元で試すとき: `python -m publisher.thumbnail "【米国株】2026/10/04 今週の振り返りと週明けの展望" out.png`
  デザインを変えたいときは `publisher/thumbnail.py`、元の画像を変えたいときは `thumbnail_base.jpg` を差し替えます。
- **グラフ**: 本文に次の形のブロックがあると、出典付きのグラフ画像にして本文に差し込みます
  （書き方の詳細は `publisher/charts.py` の先頭と `COWORK_PROMPT.md`）。
  ````
  ```chart
  {"type": "bar", "title": "今週のセクター別騰落率", "unit": "%", "labels": ["情報技術", "ヘルスケア"], "values": [1.80, -2.65], "source": "stockanalysis.com（10/2 終値）"}
  ```
  ````
- どちらかがうまくいかなくても記事そのものは作ります（グラフは同じ数値の箇条書きに置き換え、Summary に ⚠️ で理由を出します）。
- 作った画像は Actions の実行結果ページの「Artifacts」→ `note-images` からダウンロードして確認できます（30日間）。

## Substack（フランス語版・週刊）

日曜の週末版だけ、Cowork が note 記事の後ろにフランス語版（`# ③ Substack記事`）を書き、同じメールで送ってきます。
GitHub の仕組みはそれを Substack「Marchés en bref」（https://marchsenbref.substack.com）に投稿します。
平日版のメールには `# ③` が無いので、Substack には何もしません。

### 初期設定（最初の1回だけ）

1. **Substack の入館証（Cookie）を取る**（PC の Chrome で）
   1. https://marchsenbref.substack.com を開き、ログインした状態にする
   2. 開発者ツール（F12）→ **Application** → 左の **Cookies** → `https://substack.com`（無ければ `https://marchsenbref.substack.com`）
   3. 名前が `substack.sid` の行の **Value** をコピー
2. https://github.com/ryotaozawa1010-commits/affiliate-fr/settings/secrets/actions/new で Secret `SUBSTACK_COOKIE` に貼る
3. 最初の1回は下書きで届くので、Substack で見た目を確認する。問題なければ
   https://github.com/ryotaozawa1010-commits/affiliate-fr/settings/variables/actions/new で Variable `SUBSTACK_MODE` = `publish` を追加。
   以後は毎週そのまま公開され、購読者にメールが届きます

| Name | 種類 | 動き |
|---|---|---|
| `SUBSTACK_COOKIE` | Secret | ログイン用（必須）。切れたら同じ手順で取り直す |
| `SUBSTACK_MODE` | Variable | 未設定 / `draft`: 下書き。`publish`: そのまま公開してメール配信 |
| `SUBSTACK_PUBLICATION` | Variable | サブドメイン。未設定なら `marchsenbref` |
| `SUBSTACK_EMAIL` / `SUBSTACK_PASSWORD` | Secret | Cookie の代わり。Google で登録したアカウントにはパスワードが無いので通常は使わない |

- グラフは note と同じ ```chart から作り、出典は「Source : …」とフランス語で入ります
- Substack にも公式の投稿 API はなく、エディタと同じ通信を再現しています。仕様変更で突然動かなくなることがあります（失敗すると GitHub からメールが届きます）
- 公開に失敗しても下書きは残るので、Substack のアプリから手で公開できます

## 広告枠（アフィリエイト）

`publisher/affiliate.md` に Markdown を書くと、すべての note 記事の末尾にその内容が付き、
冒頭に「※この記事にはプロモーション（アフィリエイトリンク）を含みます。」が自動で入ります
（ステマ規制で広告の表記が必要なため、末尾だけ付けることはできないようにしています）。

- 今はコメント（`<!-- -->`）の中に例が入っているだけなので、記事には何も付きません
- ASP（A8.net・もしもアフィリエイトなど）で発行したリンクを `[表示する文字](URL)` の形で書けば有効になります
- 外したいときはコメントの外を空にするだけ
- Substack（フランス語版）には `publisher/affiliate_fr.md` が同じように付き、冒頭には「Publicité : …」が入ります
  （フランスのインフルエンサー法で広告の明示が必要なため）。Trade Republic の紹介リンクが届いたらここに書きます
- リンクは note に `nofollow` 付きで入ります。最初は `_drafttest` で下書きを作り、リンクが押せるか確認してください


Actions タブ → 「マーケットレポート投稿（note）」→ **Run workflow**（`dry_run` にチェック）で実行すると、
受け取り用 Gmail の新着を見て、**投稿せずに** note の記事タイトルと文字数を Summary に表示します。

件名が `[PUBLISH] YYYY-MM-DD_test` のメールは常にこの確認モードで処理されます（本番投稿されません）。
セットアップ時に u1132038801@gmail.com へテストメール `[PUBLISH] 2026-10-03_test` を送ってあるので、
Secrets を登録したらこれで受け取りまで確認できます（3日以内のメールだけ見るので、それを過ぎたら同じ形式のメールを送り直してください）。

## 止めたいとき

- 一時停止: Actions タブ → 「マーケットレポート投稿（note）」→ 右上の「…」→ **Disable workflow**
- note だけ止める: Secret `NOTE_PASSWORD` を削除

## ファイル構成

| パス | 役割 |
|---|---|
| `publisher/gmail_inbox.py` | 受け取り用 Gmail から投稿用メールを受け取る（なりすまし検査つき） |
| `publisher/parse.py` | 公開版 Markdown を note 記事・Substack 記事（と X スレッド）に分解 |
| `publisher/note.py` | Markdown → note 用 HTML 変換、note への下書き保存・公開、画像のアップロード |
| `publisher/thumbnail.py` | タイトル入りの見出し画像を作る |
| `publisher/charts.py` | 本文の ```chart ブロックをグラフ画像にする |
| `publisher/xpost.py` | X の文字数計算・分割・OAuth 署名・スレッド投稿（停止中） |
| `publisher/affiliate.py` / `affiliate.md` / `affiliate_fr.md` | 記事の末尾の広告枠と冒頭の PR 表記（日本語 / フランス語） |
| `publisher/substack.py` | Markdown → Substack の文書形式への変換、Substack への下書き保存・公開、画像のアップロード |
| `publisher/main.py` | 全体の流れと二重投稿防止 |
| `publish/inbox/` | 受け取った公開版（Gmail から保存したもの） |
| `publish/done/` | 投稿結果の記録（note の URL など） |
| `publisher/COWORK_PROMPT.md` | Cowork ルーティンに追記した指示の控え |

テスト: `python -m unittest discover -s publisher/tests -t .`
