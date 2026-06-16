"""唯讀 AWS 工具 + 證據帳本 + 防幻覺機制。

核心原則：真相來自這裡記錄的「證據帳本」(EVIDENCE_LEDGER)，不是模型的敘述。
- 所有 AWS 呼叫只允許唯讀動詞（describe/list/get/lookup/...），寫入一律在工具層擋掉（防 agent）。
- 每次呼叫都記錄真實 AWS RequestId、時間、參數、結果狀態（OK / NO_DATA / ERROR / REFUSED）。
- 查無資料回明確 NO_DATA，模型無法把「沒資料」唬成「有資料」。
- 強制查文件 gate：未先查官方文件（AWS doc / VMware docs）前，AWS API 工具一律拒絕執行。
"""

from __future__ import annotations

import datetime
import json
import urllib.request
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

# ---- 唯讀動詞白名單（工具層強制 readonly，第一道防 agent 寫入防線）----
READONLY_PREFIXES = ("describe", "list", "get", "lookup", "head", "search", "batch_get")
# 少數不以唯讀動詞開頭、但確定是唯讀的操作（明確列舉，不開 prefix 後門）
EXTRA_READONLY_OPS = {
    "filter_log_events",     # CloudWatch Logs
    "start_query",           # CloudWatch Logs Insights（只啟動查詢，不改資源）
    "stop_query",
    "select_resource_config",  # AWS Config
}

# ---- 官方文件網域白名單（VMware/Broadcom + AWS）----
DOC_DOMAIN_ALLOWLIST = (
    "docs.aws.amazon.com",
    "aws.amazon.com",
    "techdocs.broadcom.com",
    "knowledge.broadcom.com",
    "docs.vmware.com",
    "blogs.vmware.com",
)

# ---- 全域狀態 ----
EVIDENCE_LEDGER: list[dict] = []          # 證據帳本：每筆工具呼叫的真實紀錄
_DOC_CONSULTED = {"value": False}          # 強制查文件 gate 旗標
_SESSION = {"boto3": None, "region": None}

_MAX_RESULT_CHARS = 8000                   # 回傳給模型的結果上限（保護 2GiB RAM + context）


# =========================================================================
# 基礎工具
# =========================================================================
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _next_eid() -> str:
    return f"E{len(EVIDENCE_LEDGER) + 1}"


def _record(eid, tool_name, service, operation, params, request_id, status, summary):
    """寫入證據帳本（唯一真相來源）。"""
    EVIDENCE_LEDGER.append({
        "evidence_id": eid,
        "ts": _now(),
        "tool": tool_name,
        "service": service,
        "operation": operation,
        "params": params,
        "request_id": request_id,   # AWS 真實 RequestId = 出生證明，可拿去 CloudTrail 對帳
        "status": status,           # OK / NO_DATA / ERROR / REFUSED
        "summary": summary,
    })


def _truncate(text: str) -> str:
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    return text[:_MAX_RESULT_CHARS] + f"\n...[已截斷，原長 {len(text)} 字，請用更精確的參數縮小查詢]"


def init_session(region: str, role_arn: str | None = None,
                 role_session_name: str = "evs-debug-ro"):
    """建立唯讀 boto3 session。若給 role_arn 則 assume（防 agent 縱深防禦）。

    註：CloudShell 多半已是 assumed-role 身分，這裡再 assume = role chaining，
    session 上限 1 小時；boto3 client 由本 session 衍生，逾時請重啟 agent（程式碼仍在）。
    """
    base = boto3.Session()
    if role_arn:
        sts = base.client("sts", region_name=region)
        creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=role_session_name)["Credentials"]
        sess = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region,
        )
    else:
        sess = boto3.Session(region_name=region)
    _SESSION["boto3"] = sess
    _SESSION["region"] = region
    return sess


def mark_doc_consulted(source: str):
    """由 main.py 的 hook 在偵測到『文件工具被呼叫』時呼叫，解除查文件 gate。"""
    _DOC_CONSULTED["value"] = True
    _record(_next_eid(), "doc_consult", "docs", source, {}, None, "OK",
            f"已查官方文件來源：{source}")


# =========================================================================
# 工具 1：強制查文件——讀 VMware/Broadcom/AWS 官方文件頁
# =========================================================================
@tool
def read_official_doc(url: str) -> dict:
    """讀取官方文件頁面內容（VMware/Broadcom 或 AWS 文件網站）。

    除錯前『必須』先用本工具或 AWS 文件查詢工具查官方文件，否則 AWS API 工具會被 gate 擋下。
    只允許官方文件網域，其他網址一律拒絕。

    Args:
        url: 官方文件頁面網址（須屬白名單網域，如 techdocs.broadcom.com / docs.aws.amazon.com）
    """
    eid = _next_eid()
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in DOC_DOMAIN_ALLOWLIST):
        _record(eid, "read_official_doc", "docs", "GET", {"url": url}, None, "REFUSED",
                f"網域不在官方文件白名單：{host}")
        return {"evidence_id": eid, "status": "REFUSED",
                "message": f"只允許官方文件網域 {DOC_DOMAIN_ALLOWLIST}，{host} 被拒。"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "evs-debug-agent/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        _DOC_CONSULTED["value"] = True   # 查了文件 → 解除 gate
        _record(eid, "read_official_doc", "docs", "GET", {"url": url}, None, "OK",
                f"讀到官方文件 {host}（{len(body)} 字）")
        return {"evidence_id": eid, "status": "OK", "source": host,
                "content": _truncate(body)}
    except Exception as e:  # noqa: BLE001
        _record(eid, "read_official_doc", "docs", "GET", {"url": url}, None, "ERROR", str(e))
        return {"evidence_id": eid, "status": "ERROR", "message": str(e)}


# =========================================================================
# 工具 2：通用唯讀 AWS API 呼叫（agent 查任何想查的資訊/log）
# =========================================================================
@tool
def aws_read(service: str, operation: str, parameters: dict | None = None,
             region: str | None = None) -> dict:
    """對 AWS 發出『唯讀』API 呼叫，回傳真實結果（含 AWS RequestId 出生證明）。

    安全規則（強制）：
    1. 使用前必須先查官方文件（read_official_doc 或 AWS 文件查詢工具），否則拒絕（防『沒查就排查』）。
    2. 只允許唯讀操作（describe_/list_/get_/lookup_/head_/search_ + 少數明列唯讀操作），
       任何寫入/變更操作一律在此擋下。

    Args:
        service: boto3 服務名，如 'evs'、'ec2'、'logs'、'cloudtrail'、'elbv2'
        operation: 唯讀操作（snake_case），如 'list_environments'、'describe_vpcs'、'filter_log_events'
        parameters: 該操作的參數 dict（如 {"logGroupName": "...", "limit": 50}）
        region: 覆寫 region（預設用啟動時的 region）
    """
    eid = _next_eid()
    params = parameters or {}
    op = operation.lower().strip()

    # gate 1：強制先查文件
    if not _DOC_CONSULTED["value"]:
        _record(eid, "aws_read", service, op, params, None, "REFUSED",
                "查文件 gate：尚未查官方文件")
        return {"evidence_id": eid, "status": "REFUSED",
                "message": "排查前必須先查官方文件。請先用 read_official_doc 或 AWS 文件查詢工具，"
                           "確認該症狀/API 的官方說明後再呼叫 AWS API。"}

    # gate 2：唯讀動詞檢查（工具層強制 readonly）
    if not (op.startswith(READONLY_PREFIXES) or op in EXTRA_READONLY_OPS):
        _record(eid, "aws_read", service, op, params, None, "REFUSED",
                "非唯讀操作被工具層擋下")
        return {"evidence_id": eid, "status": "REFUSED",
                "message": f"只允許唯讀操作（{READONLY_PREFIXES} 或明列唯讀操作），'{operation}' 被拒。"}

    sess = _SESSION["boto3"]
    if sess is None:
        return {"evidence_id": eid, "status": "ERROR", "message": "boto3 session 未初始化"}

    try:
        client = sess.client(service, region_name=region or _SESSION["region"])
        method = getattr(client, op, None)
        if method is None:
            _record(eid, "aws_read", service, op, params, None, "ERROR", "操作不存在")
            return {"evidence_id": eid, "status": "ERROR",
                    "message": f"{service} 沒有操作 '{operation}'，請查官方文件確認正確操作名。"}

        resp = method(**params)
        request_id = (resp.get("ResponseMetadata", {}) or {}).get("RequestId")
        # 去掉 ResponseMetadata 後判斷是否真的有資料
        payload = {k: v for k, v in resp.items() if k != "ResponseMetadata"}
        is_empty = all(
            (v in (None, [], {}, "", 0)) for v in payload.values()
        ) if payload else True

        if is_empty:
            _record(eid, "aws_read", service, op, params, request_id, "NO_DATA",
                    "查詢成功但無資料")
            return {"evidence_id": eid, "status": "NO_DATA", "request_id": request_id,
                    "message": "查詢成功，但這個條件下沒有任何資料（不是有資料、是真的空）。"}

        body = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        _record(eid, "aws_read", service, op, params, request_id, "OK",
                f"{service}.{op} 回傳 {len(body)} 字")
        return {"evidence_id": eid, "status": "OK", "request_id": request_id,
                "data": _truncate(body)}

    except (ClientError, BotoCoreError) as e:
        _record(eid, "aws_read", service, op, params, None, "ERROR", str(e))
        return {"evidence_id": eid, "status": "ERROR", "message": str(e)}


# =========================================================================
# 確定性驗證器（防幻覺第二層，純程式比對，不靠 LLM）
# =========================================================================
def validate_report(report_text: str) -> list[str]:
    """掃描報告引用的證據 ID，比對證據帳本，找出疑似幻覺。

    回傳問題清單；空清單代表報告每個證據引用都對得上真實工具結果。
    """
    import re
    cited = sorted(set(re.findall(r"\bE\d+\b", report_text)), key=lambda x: int(x[1:]))
    by_id = {r["evidence_id"]: r for r in EVIDENCE_LEDGER}
    issues = []
    for eid in cited:
        rec = by_id.get(eid)
        if rec is None:
            issues.append(f"{eid}：報告引用了不存在的證據 → 疑似幻覺（模型憑空捏造查詢結果）")
        elif rec["status"] in ("NO_DATA", "ERROR", "REFUSED"):
            issues.append(
                f"{eid}：引用的證據實際狀態為 {rec['status']}（{rec['summary']}），"
                f"不應拿來當事實依據"
            )
    if not cited:
        issues.append("報告未引用任何證據 ID [E#]，無法驗證其事實是否來自真實查詢。")
    return issues


def ledger_table() -> str:
    """把證據帳本印成表格（真實操作過程，供人核對）。"""
    if not EVIDENCE_LEDGER:
        return "（本次沒有任何工具呼叫紀錄）"
    lines = ["| 證據 | 時間(UTC) | 工具 | 服務.操作 | 狀態 | RequestId | 摘要 |",
             "|---|---|---|---|---|---|---|"]
    for r in EVIDENCE_LEDGER:
        lines.append(
            f"| {r['evidence_id']} | {r['ts']} | {r['tool']} | "
            f"{r['service']}.{r['operation']} | {r['status']} | "
            f"{r['request_id'] or '-'} | {r['summary']} |"
        )
    return "\n".join(lines)
