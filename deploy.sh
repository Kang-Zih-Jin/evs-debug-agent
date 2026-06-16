#!/usr/bin/env bash
#
# EVS 唯讀除錯 agent — CloudShell 一鍵部署 / 啟動
# 用法：bash deploy.sh
#
# 設計重點：
# - venv 裝在 /tmp（暫存區），不佔 CloudShell 1GB 持久儲存
# - LLM 推理在 Bedrock 遠端跑，本機只當指揮中心，1 vCPU/2GiB 綽綽有餘
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="/tmp/evs-agent-venv"

echo "==> [1/3] 建立虛擬環境（/tmp，不佔持久儲存）"
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip

echo "==> [2/3] 安裝依賴"
pip install --quiet -r "$APP_DIR/requirements.txt"

echo "==> [3/3] 啟動 agent"
# ---- 設定（可在執行前 export 覆寫）----
export AWS_REGION="${AWS_REGION:-ap-northeast-1}"          # 你的 EVS 所在 region
export BEDROCK_REGION="${BEDROCK_REGION:-us-east-1}"       # 呼叫 Bedrock 的 region（須已開通 Opus 4.8）
export EVS_DEBUG_MODEL_ID="${EVS_DEBUG_MODEL_ID:-us.anthropic.claude-opus-4-8}"
# 建議設唯讀角色做縱深防禦（防 agent）；不設則用 CloudShell 當前身分：
# export EVS_DEBUG_ROLE_ARN="arn:aws:iam::<ACCOUNT_ID>:role/EvsDebugReadOnlyRole"

python3 "$APP_DIR/main.py"
