# StepAI AI Workers

## プロジェクト概要
StepAIのAIエージェントシステム。5体のエージェントが自律的に連携して動く。

## エージェント一覧
| # | 名前 | ディレクトリ | 状態 |
|---|------|------------|------|
| 1 | 秘書くん | `agents/hisho/` | Phase 1 実装中 |
| 2 | セールスくん | `agents/sales/` | 未実装 |
| 3 | 参謀くん | `agents/sanbou/` | 未実装 |
| 4 | SNSくん | `agents/sns/` | 未実装 |
| 5 | コンテンツくん | `agents/content/` | 未実装 |

## 技術スタック
- Python 3.11+
- Claude API (Anthropic)
- Slack Bot (slack_bolt, Socket Mode)
- Gmail API (OAuth2)
- Google Calendar API (Service Account)
- APScheduler

## セットアップ

### 1. 依存関係
```bash
pip install -e .
```

### 2. 環境変数
```bash
cp .env.example .env
# .env を編集して API キーを設定
```

### 3. Gmail OAuth (初回のみ)
```bash
# Google Cloud Console で OAuth クライアント ID を作成
# credentials.json をプロジェクトルートに配置
python -m agents.hisho.gmail_client
# ブラウザが開くのでログインして許可
```

### 4. 起動
```bash
python -m agents.hisho.main
```

## アーキテクチャ
```
Slack (Socket Mode) ─┐
APScheduler ─────────┤
                     ▼
               brain.py (Claude API)
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
   gmail_client  calendar_client  slack
   (read/draft)  (read/create)   (notify)
```

## 設計原則
- **自律優先**: エージェントが自分で考えて自分で動く
- **安全第一**: メール送信は絶対にえがおの承認が必要（下書きまで）
- **通信バス**: Slack（全エージェントが読み書き）
