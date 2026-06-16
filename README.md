# AWS EVS 唯讀除錯 Agent

在 **AWS CloudShell（標準版，非 VPC）** 一鍵部署的 EVS（Amazon Elastic VMware Service）除錯助手。
用 **Claude Opus 4.8** 思考，**只用唯讀權限**查 AWS 環境，**強制先查官方文件才排查**，並用**證據帳本 + 確定性驗證器**防止 agent 唬爛。

---

## 為什麼這樣設計（決策脈絡）

| 需求 | 怎麼解 |
|------|--------|
| 客戶 VPC 全私有、不通 internet | 跑在**標準 CloudShell**（不綁 VPC，自帶 internet）→ 同時解掉「查文件」「呼叫 EVS API」「呼叫 Bedrock」三塊網路需求；私有 VPC 限制咬不到它 |
| 只能唯讀（防 agent 誤操作/幻覺寫入） | ①工具白名單，**不給 raw shell**；②`aws_read` 工具層只放行唯讀動詞；③（建議）assume `EvsDebugReadOnlyRole`，IAM 層再鎖一道 |
| 強制先查官方文件 | `aws_read` 有 **gate**：本輪沒查過文件就拒絕呼叫任何 AWS API |
| 怕它說查了其實沒查 / 說看了 log 其實沒看 | **證據帳本**記錄每次真實工具呼叫（含 AWS `RequestId`）＋查無資料回 `NO_DATA`＋收尾**確定性驗證器**比對報告引用的證據 ID |
| 顯示思考與操作過程 | Opus 4.8 開 reasoning，Strands 串流印出思考＋工具呼叫；收尾再印「真實操作帳本」 |
| 效能 | LLM 在 Bedrock 遠端跑，CloudShell（1 vCPU/2GiB）只當指揮中心，輕量 I/O 工作，足夠 |

> 防護強度：工具白名單 + `aws_read` 唯讀檢查是主防線；assume readonly role 是縱深防禦。
> 唯讀其實已由「角色只給讀的 Allow（其餘 implicit deny）」保證，policy 裡的 explicit Deny 只是再加一道保險。

---

## 快速開始（CloudShell）

### 0.（建議，一次性）建立唯讀角色 — 由管理者執行
```bash
bash setup-role.sh
# 完成後它會印出 EVS_DEBUG_ROLE_ARN，記下來
```

### 1. 開通模型
到 Bedrock console（`BEDROCK_REGION` 那個 region，預設 us-east-1）→ Model access → 開通 **Claude Opus 4.8**。

### 2. 一鍵部署 / 啟動
```bash
# 取得程式碼（git clone 或上傳這個資料夾），然後：
cd evs-debug-agent

# （建議）帶入唯讀角色做縱深防禦
export EVS_DEBUG_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/EvsDebugReadOnlyRole
# 視情況覆寫 region
export AWS_REGION=ap-northeast-1        # 你的 EVS 所在 region
export BEDROCK_REGION=us-east-1         # 已開通 Opus 4.8 的 region

bash deploy.sh
```
就進入互動模式，直接問例如：「EVS environment xxx 的 host 一直卡在某狀態，幫我查」。

---

## 設定（環境變數）

| 變數 | 預設 | 說明 |
|------|------|------|
| `AWS_REGION` | `ap-northeast-1` | EVS / CloudWatch Logs 所在 region |
| `BEDROCK_REGION` | `us-east-1` | 呼叫 Bedrock 的 region（須已開通 Opus 4.8） |
| `EVS_DEBUG_MODEL_ID` | `us.anthropic.claude-opus-4-8` | **跨區 inference profile id**（Opus 4.8 的 in-region 端點為 N/A，必須用 profile）。其他 geo：`eu.` / `jp.` / `au.` |
| `EVS_DEBUG_ROLE_ARN` | （無） | 唯讀角色 ARN；設了就 assume，縱深防禦 |
| `KNOWLEDGE_MCP_URL` | `https://knowledge-mcp.global.api.aws` | AWS Knowledge MCP（遠端託管、免認證） |

---

## 檔案結構
```
evs-debug-agent/
├── main.py                 # agent 組裝：Opus 4.8 + reasoning + MCP + hooks + 驗證器 + REPL
├── tools.py                # 唯讀工具(read_official_doc / aws_read) + 證據帳本 + 確定性驗證器
├── requirements.txt
├── deploy.sh               # CloudShell 一鍵：venv(/tmp) → 裝依賴 → 啟動
├── setup-role.sh           # 一次性：建立 EvsDebugReadOnlyRole
├── iam/
│   ├── trust-policy.json   # 角色信任政策（限本帳號）
│   └── readonly-policy.json# 補充唯讀 + Bedrock invoke + 寫入 Deny（搭配 ViewOnlyAccess）
└── README.md
```

## 防幻覺三層（對應你的擔憂）

| 擔憂 | 擋它的機制 |
|------|-----------|
| 說查了文件，根本沒查 | 查文件 gate：沒查文件 → `aws_read` 回 `REFUSED`；hook 偵測文件工具被呼叫才解除 |
| 說打 API 看 log，其實沒打 | 證據帳本記錄每次真實呼叫 + AWS `RequestId`；收尾驗證器比對報告引用的 `[E#]`，引用不存在或 `NO_DATA` 的證據 → 標記疑似幻覺 |
| 把「沒資料」講成「有資料」 | 工具查無資料回明確 `NO_DATA`，system prompt 禁止腦補 |
| （選配最強）連程式都不信 | 用帳本的 `RequestId` 到 **CloudTrail `LookupEvents`** 對帳：模型說了、帳本有、CloudTrail 也有，三方一致才算數 |

---

## 注意事項 / 已知邊界

- **角色 chaining 1 小時**：CloudShell 多半已是 assumed-role 身分，再 assume 唯讀角色屬 role chaining，session 上限 1 小時；逾時重跑 `deploy.sh` 即可（程式碼還在）。
- **CloudShell 20 分鐘閒置斷線**：跑的時候不會斷；若跑完慢慢讀導致斷線，重連後重啟即可。
- **成本**：Opus 4.8 為 pay-per-token，計入執行帳號。
- **VMware 文件**：`read_official_doc` 只允許官方文件網域（techdocs.broadcom.com / docs.vmware.com / docs.aws.amazon.com 等）。
- **套件 API 版本**：本程式針對 `strands-agents` 1.x 撰寫（`BedrockModel`、`MCPClient`、`BeforeToolCallEvent/AfterToolCallEvent`、`additional_request_fields` 開 reasoning）。若安裝到的版本 API 有差異，請依該版文件微調 `main.py` 對應處——**這份程式我尚未在 CloudShell 實機跑過，第一次部署請留意啟動訊息**。
