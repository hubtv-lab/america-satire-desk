# America Satire Desk — 自動生成バックエンド

毎朝、日本時間の朝5時ごろに、アメリカ関連ニュースをRSSから取得し、Claude AIが風刺向きの5本を選定・初稿生成して、アプリ（index.html）が読み込む `daily.json` を自動更新します。

**Substackへの投稿は自動化されていません。** 毎朝あなたが5本を確認し、必要なら修正して、手動で投稿します。

---

## フォルダ構成

```
（リポジトリのルート）
├── index.html                      ← アプリ本体（既存のものをここに置く）
├── daily.json                      ← 毎朝自動更新される当日データ
├── daily.js                        ← 同内容（file://で開く場合の予備）
├── requirements.txt                ← Pythonの依存ライブラリ
├── README.md                       ← このファイル
├── archive/
│   └── 2026-07-08.json             ← 日付付きの控え（毎日たまっていく）
├── scripts/
│   └── generate_daily.py           ← 生成スクリプト本体
└── .github/
    └── workflows/
        └── daily.yml               ← 毎朝の自動実行設定
```

---

## セットアップ手順（初回のみ・約30分）

### ステップ1: GitHubアカウントを作る

1. https://github.com を開き、「Sign up」からアカウントを作成（無料）

### ステップ2: リポジトリ（プロジェクト置き場）を作る

1. 右上の「＋」→「New repository」
2. Repository name: `america-satire-desk`（好きな名前でOK）
3. **Public** を選択（無料でGitHub Pagesを使うため）
   - 注意: Publicだと `daily.json`（投稿前の下書き）は理論上誰でも閲覧できます。気になる場合は後述の「非公開にしたい場合」を参照
4. 「Create repository」をクリック

### ステップ3: ファイルをアップロードする

1. リポジトリのページで「uploading an existing file」（または Add file → Upload files）
2. このバックエンド一式（上のフォルダ構成のファイルすべて）と、既存の `index.html` をドラッグ＆ドロップ
   - **重要:** `.github/workflows/daily.yml` と `scripts/generate_daily.py` はフォルダ構造ごとアップロードする必要があります。ZIPを解凍したフォルダの中身を、フォルダごとまとめてドラッグすると構造が保たれます
   - もしブラウザでフォルダ構造がうまく上がらない場合: Add file → Create new file で、ファイル名欄に `scripts/generate_daily.py` のように `/` 込みで入力すると、フォルダが自動で作られます。中身をコピペして保存してください
3. 下の「Commit changes」をクリック

### ステップ4: Anthropic APIキーを取得して登録する

1. https://console.anthropic.com でアカウントを作成し、クレジットを購入（最低$5程度でOK。1日1回の生成なら数ヶ月もちます）
2. 「API Keys」→「Create Key」でキーを作成し、表示された `sk-ant-...` をコピー（**この画面でしか見られないので必ず控える**）
3. GitHubのリポジトリに戻り、**Settings → Secrets and variables → Actions → New repository secret**
4. Name: `ANTHROPIC_API_KEY` ／ Secret: コピーしたキーを貼り付け → 「Add secret」

これでキーは暗号化されて保管されます。コードやページには一切表示されません。

### ステップ5: GitHub Pagesでアプリを公開する

1. リポジトリの **Settings → Pages**
2. 「Source」を **Deploy from a branch** に、Branch を **main / (root)** にして Save
3. 数分待つと `https://あなたのユーザー名.github.io/america-satire-desk/` でアプリが開けるようになります
4. このURLをスマホとPCのブックマークに登録してください。**以後、アプリはこのURLで開きます**（daily.jsonが正しく読み込まれます）

### ステップ6: 動作テスト（手動実行）

1. リポジトリの **Actions** タブを開く（初回は「I understand... enable them」を押して有効化）
2. 左の「Generate daily satire candidates」→ 右の「Run workflow」→ 緑のボタン
3. 1〜3分で完了します。緑のチェック✓が付けば成功
4. アプリのURLを開き（更新はスーパーリロード: Cmd+Shift+R）、TODAY'S DESK の説明文に「データ: daily.json」と出ていれば連携成功です
5. 赤い×が付いた場合は、その実行をクリックするとログが読めます（下のトラブルシューティング参照）

---

## 毎朝の使い方

1. 朝、ブックマークからアプリを開く（前夜のうちに5本が自動生成されています）
2. 5本の見出し・要約・風刺コメント・画像プロンプト・Captionを確認
3. 気に入らないCaptionは Regenerate caption で別案に切り替え
4. Generate Today's Article → Copy Full Article
5. Download All 5 Images（ZIP）
6. Substackに貼り付け、画像をアップロードして、**自分の目で最終確認してから**投稿

---

## 仕組みと安全設計

- 毎朝 UTC 19:50（日本時間 4:50）にGitHub Actionsが起動します（GitHub側の混雑で15〜60分遅れることがあります）
- スクリプトは「5本揃わない」「AIの出力が壊れている」「必須項目が欠けている」場合、**何も書き込まずに失敗します**。前日のdaily.jsonはそのまま残るので、朝アプリが壊れていることはありません
- 失敗するとGitHubからメール通知が届きます。Actionsタブから「Run workflow」で手動再実行できます
- 生成された5本は `archive/日付.json` にも毎日保存され、履歴が残ります

## 風刺の編集方針（スクリプトに組み込み済み）

- 分野を混ぜる（政治・企業・テック・裁判・教育・労働・文化・SNS・都市生活）
- 風刺対象は制度・組織・構造・矛盾。実在個人への人格攻撃はしない
- 事実（NEWS/EVENT）と論評（COMMENTARY）を分離する
- 悲劇・真偽不明の話題・陰謀論・扇動的な情報は使わない
- 出典URLはRSSの実データから機械的に埋めるため、AIがURLを捏造することはありません

---

## カスタマイズ

すべて `scripts/generate_daily.py` の冒頭にあります。

- **ニュースソースを変えたい** → `FEEDS` のリストを編集
- **実行時刻を変えたい** → `.github/workflows/daily.yml` の cron を編集（UTC表記。日本時間−9時間）
- **モデルを変えたい** → Settings → Secrets and variables → Actions → Variables に `SATIRE_MODEL` を追加（既定: claude-sonnet-4-6）

## コストの目安

- GitHub（Actions / Pages）: 無料枠内
- Claude API: 1日1回の生成で、入力+出力あわせて概ね1回1〜3円程度 → 月100円前後（為替・記事量により変動）

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| Actionsが赤×で「ANTHROPIC_API_KEY is not set」 | ステップ4のSecret名のスペルを確認（大文字小文字も一致させる） |
| 赤×で「credit balance」系のエラー | Anthropicコンソールでクレジット残高を確認・追加 |
| 赤×で「not enough news items」 | RSS側の一時的な不調。時間をおいて Run workflow で再実行 |
| アプリに「内蔵モックデータ」と出る | daily.jsonがまだ無い/読めていない。ステップ6を実施。PagesのURLで開いているか確認 |
| 朝5時に更新されていない | cronの遅延（最大1時間程度）。急ぐ場合は手動実行 |
| Weekly Archiveが空になった | localStorageはURL単位。file://からPagesのURLに移ると保存は新規になります（仕様） |

## 非公開にしたい場合

無料プランのGitHub PagesはPublicリポジトリが前提です。daily.json（投稿前の下書き）を非公開にしたい場合は、リポジトリをPrivateにして **Cloudflare Pages**（Privateリポジトリでも無料で公開可能）に接続する方法があります。必要になったら移行手順を相談してください。

## 将来の拡張（このリポジトリに追加していける）

- 画像生成API連携: `generate_daily.py` に画像生成の呼び出しを足し、`images/` に保存してJSONにパスを書く
- ナレーション音声: 同様にTTS APIを足して音声ファイルを保存
- 選定スコアリング: Weekly Top 5 用に punchline strength などのスコアをJSONに含める
