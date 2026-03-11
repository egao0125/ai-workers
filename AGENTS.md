# StepAI AI Workers

## プロジェクト概要
StepAIのAIエージェントシステム。5体のエージェントが自律的に連携して動く。

## エージェント一覧
| # | 名前 | ディレクトリ | ポート元 | 状態 |
|---|------|------------|---------|------|
| 1 | 秘書くん | `agents/hisho/` | 新規実装 | 実装済み |
| 2 | セールスくん | `agents/sales/` | maakun (サポ君) | 実装済み |
| 3 | 参謀くん | `agents/sanbou/` | kashikabot (ミル君) | 実装済み |
| 4 | SNSくん | `agents/sns/` | — | 未実装 |
| 5 | コンテンツくん | `agents/content/` | — | 未実装 |

## 技術スタック
- Python 3.11+
- Claude API — Sonnet (分析・推論) + Haiku (分類・感情分析)
- Slack Bot (slack_bolt, Socket Mode)
- Gmail API — OAuth2 (秘書くん) / Service Account (セールスくん)
- Google Calendar API (Service Account)
- Notion API (セールスくん — CRM)
- SQLite (参謀くん — チームデータ)
- APScheduler

## 各エージェントの起動
```bash
# 共通: 依存関係インストール
pip install -e .

# 秘書くん
python -m agents.hisho.main

# 参謀くん
python -m agents.sanbou.main

# セールスくん
python -m agents.sales.main
```

## アーキテクチャ
```
┌─────────────────────────────────────────────────────┐
│                    Slack (共通バス)                    │
│  全エージェントが読み書き。エージェント間通信もSlack経由  │
└──────┬──────────────┬──────────────┬────────────────┘
       ▼              ▼              ▼
┌─────────────┐ ┌──────────────┐ ┌──────────────┐
│  秘書くん    │ │  参謀くん     │ │ セールスくん   │
│  (hisho)    │ │  (sanbou)    │ │  (sales)     │
├─────────────┤ ├──────────────┤ ├──────────────┤
│ Gmail(OAuth)│ │ SQLite DB    │ │ Gmail(SA)    │
│ Calendar    │ │ Team Monitor │ │ Notion CRM   │
│ Email Triage│ │ Profile Build│ │ Pipeline Mgmt│
│ Scheduler   │ │ Reports      │ │ KPI/Bottlnck │
└─────────────┘ └──────────────┘ └──────────────┘
       │              │              │
       └──────────────┴──────────────┘
                      ▼
              Claude API (Anthropic)
              Sonnet + Haiku
```

## 設計原則
- **自律優先**: エージェントが自分で考えて自分で動く
- **安全第一**: メール送信は絶対にえがおの承認が必要（下書きまで）
- **通信バス**: Slack（全エージェントが読み書き）
- **独立デプロイ**: 各エージェントは別プロセスで起動。障害が他に波及しない
