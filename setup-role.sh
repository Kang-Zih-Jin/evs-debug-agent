#!/usr/bin/env bash
#
# 一次性：建立 EvsDebugReadOnlyRole（防 agent 的唯讀角色）
# 由「有 IAM 權限的管理者」執行一次即可，之後 agent 部署都 assume 這個角色。
#
# 用法：bash setup-role.sh
#
set -euo pipefail

ROLE_NAME="EvsDebugReadOnlyRole"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo "==> 帳號：$ACCOUNT_ID  角色：$ROLE_NAME"

# 把 ACCOUNT_ID 套進 trust / policy 範本（輸出到 /tmp，不污染 repo）
sed "s/ACCOUNT_ID/$ACCOUNT_ID/g" "$APP_DIR/iam/trust-policy.json"   > /tmp/evs-trust.json
sed "s/ACCOUNT_ID/$ACCOUNT_ID/g" "$APP_DIR/iam/readonly-policy.json" > /tmp/evs-readonly.json

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "==> 角色已存在，更新信任政策"
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" \
    --policy-document file:///tmp/evs-trust.json
else
  echo "==> 建立角色"
  aws iam create-role --role-name "$ROLE_NAME" \
    --description "Read-only role for EVS debug agent (defense against agent writes)" \
    --max-session-duration 3600 \
    --assume-role-policy-document file:///tmp/evs-trust.json
fi

echo "==> 掛上 AWS 託管 ViewOnlyAccess（基礎唯讀）"
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/job-function/ViewOnlyAccess

echo "==> 掛上補充唯讀政策（EVS / Logs / CloudTrail / Bedrock invoke + 寫入 Deny）"
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name evs-debug-readonly-extra \
  --policy-document file:///tmp/evs-readonly.json

echo
echo "✅ 完成。請把下面這行加到部署環境（或 deploy.sh）："
echo "   export EVS_DEBUG_ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
