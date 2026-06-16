"""AWS EVS 唯讀除錯 agent（CloudShell 一鍵部署）。

架構：
- 模型：Claude Opus 4.8（Bedrock，跨區 inference profile，開 reasoning 顯示思考）
- 思考/操作即時顯示：Strands 預設 PrintingCallbackHandler（串流文字 + reasoning + 工具呼叫）
- 防 agent：工具白名單（無 raw shell）+ 唯讀 boto3 session（可選 assume readonly role）
- 防幻覺：證據帳本（真實 RequestId）+ 強制查文件 gate + 收尾確定性驗證器
- 文件來源：AWS Knowledge MCP（遠端託管、免認證）+ read_official_doc（VMware/Broadcom）

環境變數（皆有預設，可覆寫）：
  AWS_REGION            EVS / log 所在 region（預設 ap-northeast-1）
  BEDROCK_REGION        呼叫 Bedrock 的 region（預設 us-east-1）
  EVS_DEBUG_MODEL_ID    模型 inference profile id（預設 us.anthropic.claude-opus-4-8）
  EVS_DEBUG_ROLE_ARN    （建議）唯讀角色 ARN，設了就 assume 做縱深防禦
  KNOWLEDGE_MCP_URL     AWS Knowledge MCP 端點（預設官方託管 URL）
"""

from __future__ import annotations

import os
import sys

import tools  # 本地：唯讀工具 + 證據帳本 + 驗證器

# ---- 設定 ----
AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MODEL_ID = os.environ.get("EVS_DEBUG_MODEL_ID", "us.anthropic.claude-opus-4-8")
ROLE_ARN = os.environ.get("EVS_DEBUG_ROLE_ARN")  # None = 用 CloudShell 當前身分
KNOWLEDGE_MCP_URL = os.environ.get("KNOWLEDGE_MCP_URL", "https://knowledge-mcp.global.api.aws")

SYSTEM_PROMPT = """你是 AWS EVS（Amazon Elastic VMware Service）除錯專家，協助排查 EVS / VCF（ESXi、vCenter、NSX）在 AWS 上的問題。

【鐵律｜先查文件再排查】
1. 任何排查動作之前，必須先查官方文件：AWS 用文件查詢工具（search_documentation / read_documentation），
   VMware/NSX/VCF 用 read_official_doc 讀 techdocs.broadcom.com / docs.vmware.com。
2. 沒查文件就呼叫 AWS API，工具會直接拒絕你（REFUSED）。這是硬規則，不要嘗試繞過。

【鐵律｜只憑證據說話，禁止幻覺】
3. 你只能用「唯讀」AWS API（aws_read）查資訊，不能也無法做任何寫入/變更。
4. 每個事實陳述都必須引用對應的證據 ID（格式 [E1]、[E2]…），那是你真實呼叫工具拿到的結果。
5. 嚴禁宣稱你查了某 API、看了某 log，卻沒有對應的證據 ID。沒查到就老實說「沒查/查無資料」。
6. 工具回傳 NO_DATA 代表「真的沒資料」，不可以把它說成有資料或自行腦補內容。

【排查方法】
7. 先釐清症狀 → 查官方文件確認該症狀的已知原因/正確 API → 用 aws_read 蒐證 → 對照文件下判斷。
8. 常見面向：EVS environment/host/VLAN 狀態、VPC/subnet/路由/SG、CloudWatch Logs、NSX-T routing。
9. 結論要分「已由證據確認」與「推測待驗證」，後者明講是推測。

【輸出格式】
- 排查步驟（每步標你查了哪份文件、呼叫了哪個 API → 證據 ID）
- 發現（每條附 [E#]）
- 結論與建議（唯讀環境只能建議，不能代為變更）
"""


def _tool_name(event) -> str | None:
    tu = getattr(event, "tool_use", None)
    if isinstance(tu, dict):
        return tu.get("name")
    if tu is not None:
        return getattr(tu, "name", None)
    return None


def build_hooks():
    """攔截真實工具事件：偵測文件查詢以解除 gate，並把真實操作印出來。"""
    from strands.hooks import HookProvider, HookRegistry
    from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent

    class LedgerHook(HookProvider):
        def register_hooks(self, registry: HookRegistry, **kwargs):
            registry.add_callback(BeforeToolCallEvent, self._before)
            registry.add_callback(AfterToolCallEvent, self._after)

        def _before(self, event):
            name = _tool_name(event) or "?"
            print(f"\n  \033[36m▶ 呼叫工具：{name}\033[0m", flush=True)

        def _after(self, event):
            name = (_tool_name(event) or "").lower()
            # AWS Knowledge MCP 的文件工具被呼叫 → 視為已查文件，解除 gate
            if "documentation" in name or name in ("recommend",):
                tools.mark_doc_consulted(f"AWS Knowledge MCP:{name}")
                print("  \033[32m✓ 已查 AWS 官方文件，AWS API gate 解除\033[0m", flush=True)

    return [LedgerHook()]


def build_model():
    from strands.models import BedrockModel
    return BedrockModel(
        model_id=MODEL_ID,
        region_name=BEDROCK_REGION,
        streaming=True,
        # 開啟 Opus 4.8 延伸思考（reasoning），思考過程會透過 callback 串流顯示
        additional_request_fields={
            "thinking": {"type": "enabled", "budget_tokens": 4000}
        },
    )


def run_repl(agent):
    print("\n" + "=" * 70)
    print(" AWS EVS 唯讀除錯 agent 已就緒（Opus 4.8）")
    print(" 規則：先查文件才能查 API｜只唯讀｜每個結論附證據 ID")
    print(" 輸入問題開始，輸入 exit / quit 離開")
    print("=" * 70)
    while True:
        try:
            user_input = input("\n\033[1m你：\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再見。")
            return
        if user_input.lower() in ("exit", "quit", "q", ""):
            print("再見。")
            return

        # 每輪重置查文件 gate：強制這輪排查也要先查文件
        tools._DOC_CONSULTED["value"] = False

        print("\n\033[1mAgent：\033[0m", flush=True)
        result = agent(user_input)  # 思考/文字/工具呼叫會即時串流印出

        # ---- 收尾：印真實操作帳本 + 跑確定性驗證器 ----
        print("\n\n" + "-" * 70)
        print("📒 真實操作帳本（agent 實際做了什麼，不是它嘴上說的）：")
        print(tools.ledger_table())

        issues = tools.validate_report(str(result))
        print("\n🔍 防幻覺驗證：")
        if issues:
            print("  \033[31m⚠ 偵測到可疑之處：\033[0m")
            for i in issues:
                print(f"    - {i}")
        else:
            print("  \033[32m✓ 報告每個證據引用都對得上真實工具結果\033[0m")
        print("-" * 70)


def main():
    # 1. 建立唯讀 session（可選 assume readonly role）
    print(f"[init] region={AWS_REGION} bedrock={BEDROCK_REGION} model={MODEL_ID}")
    print(f"[init] readonly role: {ROLE_ARN or '（未設，使用 CloudShell 當前身分；建議設 EVS_DEBUG_ROLE_ARN）'}")
    tools.init_session(AWS_REGION, ROLE_ARN)

    from strands import Agent

    base_tools = [tools.read_official_doc, tools.aws_read]
    hooks = build_hooks()
    model = build_model()

    # 2. 連 AWS Knowledge MCP（遠端託管、免認證），把文件查詢工具加進來
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from strands.tools.mcp import MCPClient

        knowledge_mcp = MCPClient(lambda: streamablehttp_client(KNOWLEDGE_MCP_URL))
        with knowledge_mcp:
            mcp_tools = knowledge_mcp.list_tools_sync()
            print(f"[init] 已連上 AWS Knowledge MCP，載入 {len(mcp_tools)} 個文件工具")
            agent = Agent(
                model=model,
                tools=base_tools + mcp_tools,
                hooks=hooks,
                system_prompt=SYSTEM_PROMPT,
            )
            run_repl(agent)
    except Exception as e:  # noqa: BLE001
        # MCP 連不上時降級：仍可用 read_official_doc 查 AWS/VMware 文件網站
        print(f"[warn] AWS Knowledge MCP 連線失敗（{e}），降級為只用 read_official_doc 查文件。")
        agent = Agent(
            model=model,
            tools=base_tools,
            hooks=hooks,
            system_prompt=SYSTEM_PROMPT,
        )
        run_repl(agent)


if __name__ == "__main__":
    sys.exit(main())
