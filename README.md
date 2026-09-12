# 日仏 製菓用語辞典 / Lexique de pâtisserie

フランス菓子の道具・型の名前を、**日本語・フランス語・カタカナのどれからでも**引ける対応表サイトです。

- ビルドツールなし（HTML / CSS / JS だけ）
- GitHub Pages にそのまま置けます
- スマホ優先レイアウト

## 構成

```
.
├── index.html          ページ本体
├── assets/
│   ├── style.css       スタイル（ダークモード対応）
│   └── app.js          検索・絞り込み・描画
├── data/
│   └── terms.json      用語データ ← 更新するのはここだけ
└── README.md
```

サイトの中身は `data/terms.json` を読み込んで描いているので、**用語を増やすときに HTML や JS を触る必要はありません。**

## 用語の追加方法

`data/terms.json` の `terms` 配列に、次の形でオブジェクトを 1 つ足すだけです。

```json
{
  "id": "maryse",
  "ja": "ゴムベラ",
  "fr": "maryse",
  "article": "une",
  "kana": "マリーズ",
  "category": "outil",
  "featured": true,
  "note": "最頻出。spatule とは言わない",
  "amazon_url": ""
}
```

| フィールド | 必須 | 内容 |
| --- | --- | --- |
| `id` | ○ | 英数字とハイフンの識別子。他と重複しないもの |
| `ja` | ○ | 日本語の名前 |
| `fr` | ○ | フランス語の名前（見出しとして大きく出ます） |
| `article` | | 冠詞（`un` / `une` / `du`）。フランス語名の前に小さく添えられます |
| `kana` | | カタカナ読み。検索対象になります |
| `category` | ○ | `categories[]` の `id` のどれか（現在は `outil` / `moule`） |
| `featured` | ○ | `true` にするとトップの「まず揃えたい◯点」枠に出ます |
| `note` | | 補足。あればカード内に小さく表示されます。不要なら `""` |
| `amazon_url` | | 商品リンク。**空 `""` のあいだはボタンが出ません** |

### 「まず揃えたい◯点」の見出し

見出しの数字は `featured: true` の件数から自動で決まります。今は 6 件なので「まず揃えたい**6**点」と出ます。
増減させたいときは `featured` を付け外しするだけで、見出しの数字もついてきます。

### カテゴリーを増やすとき

同じファイルの `categories` 配列に足します。チップ（絞り込みボタン）は自動で増えます。

```json
{ "id": "ingredient", "ja": "材料", "fr": "Ingrédients" }
```

### Amazon リンクを後から埋める運用

`amazon_url` が `""` の用語には、リンクボタンそのものが描画されません。
商品ページの URL が決まったら、その用語の `amazon_url` に貼るだけで、次のデプロイからボタンが出ます。

```json
"amazon_url": "https://www.amazon.fr/dp/XXXXXXXXXX"
```

URL に `tag=` が付いていなければ、`data/terms.json` の先頭にある `affiliate_tag`（現在は `ryotaozawa101-21`）が
表示時に自動で付与されます。すでに `tag=` が入っている URL はそのまま尊重されるので、
どちらの書き方でも大丈夫です。

### 編集したら確認を

JSON はカンマ 1 つで壊れます。追記したら手元で構文チェックしておくと安全です。

```bash
python3 -m json.tool data/terms.json > /dev/null && echo OK
```

## 手元での確認方法

`index.html` をダブルクリックして開くと、ブラウザのセキュリティ制限で `data/terms.json` を読み込めません
（「用語データを読み込めませんでした」と出ます）。簡易サーバー経由で開いてください。

```bash
python3 -m http.server 8000
# → http://localhost:8000 をブラウザで開く
```

## GitHub Pages での公開手順

1. このリポジトリを GitHub に push します。
2. GitHub のリポジトリページで **Settings** → 左メニューの **Pages** を開きます。
3. **Build and deployment** の **Source** で **Deploy from a branch** を選びます。
4. **Branch** で公開したいブランチ（通常は `main`）と、フォルダ **`/ (root)`** を選び、**Save**。
5. 1〜2 分待つと、同じ Pages の画面の上部に公開 URL が表示されます。

```
https://<ユーザー名>.github.io/<リポジトリ名>/
```

以降は、`data/terms.json` を編集して `main` に push するたびに、自動で再デプロイされます。
GitHub の Web エディタ（リポジトリ上でファイルを開いて鉛筆アイコン）から直接編集してコミットしてもかまいません。

> 反映されないときは、ブラウザのキャッシュか、Pages のビルドがまだ終わっていない可能性があります。
> リポジトリの **Actions** タブで `pages build and deployment` の完了を確認してください。

## 検索の仕様（メモ）

入力のたびに絞り込まれます。次はすべて同じ用語にヒットします。

| 入力 | ヒット |
| --- | --- |
| `ゴムベラ` | 日本語 |
| `maryse` / `MARYSE` | 大文字小文字を無視 |
| `マリーズ` / `まりーず` | カタカナ・ひらがなのどちらでも |
| `thermometre` | アクセント記号を無視（`thermomètre`） |
| `cul de poule` | ハイフン・中黒・スペースを無視（`cul-de-poule`） |
| `moule a tarte` | スペース区切りの複数語は AND 検索 |

検索中とカテゴリー絞り込み中は、「まず揃えたい◯点」枠は隠れます（結果と二重に並ばないようにするため）。
検索語とカテゴリーは URL に残るので、絞り込んだ状態のページをそのまま共有できます。

---

Amazonアソシエイトとして、適格販売により収入を得ています。
