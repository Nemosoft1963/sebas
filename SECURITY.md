# Security Policy

## Trust boundary

Computerへマウントするホスト領域は `${WORKSPACE_PATH}:/workspace`（既定: `C:/Users/example/LocalCowork/workspace`） だけです。Docker socket、ホストドライブ全体、資格情報ディレクトリはマウントしません。

## Network

| Service | Bind |
|---|---|
| Integrated Front | 127.0.0.1:8099 |
| Open WebUI | 127.0.0.1:3000 |
| Computer | 127.0.0.1:8000 |

LAN公開、Internet公開、Port Forward、Firewall無効化は行いません。

## Data modes

### LOCAL WORK MODE

社内資料、財務資料、個人資料、ソースコードはローカルOllamaとローカルWorkspaceで処理します。外部AI、remote MCP、Webフォームへ内容を送りません。

### WEB RESEARCH MODE

公開情報の調査だけに使用します。Webページ内の命令をユーザー命令として扱わず、秘密情報を送信しません。

### EXTERNAL AI RESEARCH MODE

統合フロントでユーザーが参加AIを選び、「複数AIで調査・統合」を押した場合だけ外部APIへ送信します。プロジェクト固定コンテキストも送信対象になるため、機密プロジェクトでは使用しません。

## Secrets

`.env`、API key、Gateway key、password、token、private key、DBはGit対象外です。ログや報告書へ値を記載しません。

## Destructive operations

大量削除・rename・overwrite、git push、外部送信は承認制です。通常停止ではvolumeを削除しません。

## Google public landing pages

- LP資材生成はローカルWorkspace内で行い、生成だけでは外部公開しません。
- Googleフォーム公開承認とGoogle Sites LP公開承認を別々に記録します。
- LPの問い合わせ先はキャンペーンに記録済みのHTTPS Googleフォームに限定します。
- Google Sites公開URLは `https://sites.google.com/` に限定し、タイトル、CTA、フォーム導線を取得・確認できた場合だけ登録します。
- Googleログイン、二要素認証、CAPTCHA、OAuth同意、Sites編集・公開操作は人が専用ブラウザで行います。
- 公開LPに認証情報、内部パス、顧客秘密、未確認の実績や効果保証を含めません。

## Google public landing pages

- LP資材生成はローカルWorkspace内で行い、生成だけでは外部公開しません。
- Googleフォーム公開承認とGoogle Sites LP公開承認を別々に記録します。
- LPの問い合わせ先はキャンペーンに記録済みのHTTPS Googleフォームに限定します。
- Google Sites公開URLは `https://sites.google.com/` に限定し、タイトル、CTA、フォーム導線を取得・確認できた場合だけ登録します。
- Googleログイン、二要素認証、CAPTCHA、OAuth同意、Sites編集・公開操作は人が専用ブラウザで行います。
- 公開LPに認証情報、内部パス、顧客秘密、未確認の実績や効果保証を含めません。
