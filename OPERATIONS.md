# セバス 運用ガイド

## Start

```powershell
Set-Location 'C:\LocalCowork\sebas'
.\scripts\start.ps1
```

初回またはフロント再ビルド時:

```powershell
.\scripts\start.ps1 -BuildFront -Check
```

## Stop

```powershell
.\scripts\stop.ps1
```

AIモデルをアンロードし、マイクブリッジと3コンテナを停止します。Workspaceとnamed volumeは残ります。

## Status and health

```powershell
.\scripts\status.ps1
.\scripts\healthcheck.ps1
```

## URLs

- Integrated Front: http://127.0.0.1:8099
- Open WebUI: http://127.0.0.1:3000
- Computer: http://127.0.0.1:8000
- Ollama: http://127.0.0.1:11434

## Logs

```powershell
docker compose logs --tail 200 web
docker compose logs --tail 200 open-webui
docker compose logs --tail 200 cptr
```

Computer初回setup URL/tokenは `docker compose logs cptr` で確認します。値を文書へ貼り付けません。

## Models

```powershell
.\scripts\pull-models.ps1
.\scripts\smoke-test.ps1
```

16GB VRAMでは8K contextから開始します。モデルがメモリへ載らない場合はContextを下げ、既存の安定モデルへ戻します。

## Project goal execution

1. Integrated Frontで対象プロジェクトを選択する。
2. 目標、達成条件、制約を保存する。
3. ローカルLLMで計画を生成し、人間が内容を確認して承認する。
4. 実行開始後は中途報告とタスク成果を確認する。
5. 必要なら一時停止または中止する。一時停止中は完了済み成果を保持して再開できる。
6. 失敗時はエラー内容を確認し、「失敗タスクを再試行」後に再開する。
7. 完了後は実行報告Markdownを保存する。

実行状態はSQLiteに永続化され、`__project_memory__/00_goal.md`、`10_plan_and_procedure.md`、`20_execution_log.md`、`90_final_report.md` も状態変更ごとに自動更新されます。アプリ再起動で実行中だった計画は、自動続行せず一時停止へ復元されます。AI全体を停止すると、実行中のプロジェクト計画も安全に一時停止します。

### Approval-gated external execution

販売、顧客連絡、商談、PoC、契約などを要求する目標は、資料ファイルだけでは完了になりません。画面の「承認付き外部実行」で対象と内容を登録し、内容確認後に承認してください。

- `email`: SMTP設定済みの場合だけ「実行」で実送信する
- `proposal`、`meeting`、`poc`、`contract`、`manual`: 外部で実施後、「外部実施証拠を登録」で日時・対象・結果・確認記録を保存する
- 承認前のアクションは実行されない
- 実行証拠が0件の販売目標は完了せず、外部実行待ちで停止する

メール自動送信を使う場合は `.env` に `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_FROM` を設定し、必要に応じて `SMTP_SSL` または `SMTP_STARTTLS` を設定します。認証情報や本文はイベント詳細へ保存しません。

### Premarketing and lead capture

画面の「プレマーケティング・リード獲得」でキャンペーン名、対象顧客、オファー、CTAを入力すると、次を自動生成します。

- キャンペーン設計Markdown
- ランディングページ文案
- 4週間のコンテンツカレンダーCSV
- 推測困難な公開トークン付き同意フォーム

フォーム回答はプロジェクト別DBへ保存され、会社、役割、課題の具体性、検討時期、予算感、利用同意から0～100点で評価されます。60点以上は承認待ちメールアクションへ自動登録されます。承認前には送信されません。

標準構成ではサービスは `127.0.0.1` に限定されています。インターネット公開や広告・SNS配信は自動実行しません。公開する場合は、プライバシー表示、利用目的、保存期間、問い合わせ窓口、公開先のアクセス制御を人間が確認してください。

### Google external publication

Google公開は「公開申請 → 人間の公開承認 → Googleフォーム作成・公開 → Google Sites掲載 → 公開URL登録 → 回答同期」の順で行います。公開承認と見込み客への連絡承認は別です。

1. 専用のGoogle Workspaceユーザーを用意し、`.env` の `GOOGLE_MARKETING_ACCOUNT` に設定します。
2. Google CloudでForms APIを有効にし、OAuthクライアントを作成します。
3. 通常の対話ブラウザでOAuth同意を完了し、クライアントID、クライアントシークレット、更新トークンをローカルの `.env` に設定します。値をGit、文書、画面キャプチャへ残しません。
4. `docker compose up -d --build` でWebと `google-publisher-browser` を起動します。専用ブラウザは `127.0.0.1:${GOOGLE_BROWSER_PORT:-8010}` だけで待ち受けます。
5. Google Sitesの初回ログイン、2FA、CAPTCHA、OAuth再同意は人が行います。自動処理はこれらを回避しません。

プロジェクト画面で「Google公開申請」「公開承認」「Googleフォーム公開」を順に実行します。フォーム作成後は次の独立したLP公開フローへ進みます。

1. 「LP資材生成」で、HTMLプレビュー、Google Sites転記原稿、公開検証manifestをプロジェクトWorkspaceへ生成します。
2. LPプレビューでタイトル、対象、オファー、CTA、情報取扱い、免責、問い合わせ先Googleフォームを確認します。
3. 「LP公開申請」を登録し、「LP公開承認」でフォーム公開とは別の人間承認を記録します。
4. 「専用ブラウザ」からGoogle Sitesを開き、`google_sites_copy.md` の承認済み内容だけを反映します。問い合わせボタンまたは埋込み先には既存の `google_form_url` を使用し、別フォームを作りません。
5. デスクトップとモバイルをプレビューしてGoogle Sites側で公開します。
6. 「公開結果を検証・登録」へ編集URLと公開URLを入力します。システムがHTTPS、Google Sitesホスト、HTTP 200、タイトル、CTA、フォームIDを確認できた場合だけ `published` として保存します。

Google Sitesのログイン、編集、公開ボタン操作は人が専用ブラウザで行います。現行Google Sitesの本文編集は公式API経由で自動化せず、認証画面やCAPTCHAを回避しません。

回答は既定で5分ごとに差分取得され、「回答同期」で即時取得もできます。Google回答IDで重複を排除し、同意とメールアドレスが確認できた回答だけをリード化します。60点以上は連絡承認待ちへ入ります。

- `reauth_required`: 人がGoogle認証を更新し、Webサービスを再起動する。
- `failed`: エラー内容を確認して再試行する。記録済みフォームID、公開URL、同期時刻は削除しない。
- APIの429、5xx、ネットワーク障害は最大3回まで指数バックオフする。401、403、2FA、CAPTCHAは自動再試行しない。
- 専用ブラウザの状態は `localsaporter_google_publisher_browser_data` に保存する。このボリュームを共有・公開しない。


## Context folder upload

Integrated Frontの「調査フォルダ追加」は、ブラウザで選んだフォルダ配下の全ファイルを再帰登録します。ホストやサーバーの任意パスを文字列で指定する機能ではありません。相対パスは保持されますが、絶対パス、ドライブ指定、親ディレクトリを示すパスはAPIで拒否します。

テキスト、PDF、DOCX、XLSX、PPTXは抽出本文をAIコンテキストに加えます。画像・音声・動画・圧縮ファイル・未対応バイナリも原本とメタデータを保存しますが、内容の自動解析は行いません。一覧の「原本保存」で再取得できます。

上限は1回200ファイル、1ファイル10 MB、プロジェクト原本合計100 MB、抽出本文合計2,000,000文字です。AIへ一度に渡す抽出本文は60,000文字までで、全ファイルの目録は常に付与します。


## Backup

```powershell
.\scripts\backup.ps1
```

Workspace、統合フロント、Open WebUI、Computerの状態を `backups/YYYYMMDD_HHMMSS` へ保存します。

## Update

1. backup
2. `scripts/collect-versions.ps1`
3. `docker compose pull`
4. `docker compose up -d --build`
5. healthcheck
6. acceptance test
7. VERSIONS.md更新

## Never use casually

```text
docker compose down -v
docker volume prune
docker system prune -a
```

### Google Sites landing-page workflow

Googleフォーム公開後は、次の独立したLP公開フローを使用します。

1. 「LP資材生成」でHTMLプレビュー、Google Sites転記原稿、公開検証manifestをプロジェクトWorkspaceへ生成する。
2. LPプレビューでタイトル、対象、オファー、CTA、情報取扱い、免責、問い合わせ先Googleフォームを確認する。
3. 「LP公開申請」を登録し、「LP公開承認」でフォーム公開とは別の人間承認を記録する。
4. 「専用ブラウザ」からGoogle Sitesを開き、`google_sites_copy.md` の承認済み内容だけを反映する。問い合わせボタンまたは埋込み先には既存の `google_form_url` を使用し、別フォームを作らない。
5. デスクトップとモバイルをプレビューしてGoogle Sites側で公開する。
6. 「公開結果を検証・登録」へ編集URLと公開URLを入力する。システムがHTTPS、Google Sitesホスト、HTTP 200、タイトル、CTA、フォームIDを確認できた場合だけ `published` として保存する。

Google Sitesのログイン、編集、公開ボタン操作は人が専用ブラウザで行います。認証画面、2FA、CAPTCHAを自動回避しません。
