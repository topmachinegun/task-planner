#!/usr/bin/env python3
"""generate_tasks.py — 项目任务自动生成。

两种触发场景：
  场景一：跟进中项目 + 客户/联系人信息不完整 → 创建「客户背景调查」任务
  场景二：项目最后跟进超过 14 天 → 创建「项目搁置跟进」任务

用法：
  python3 generate_tasks.py                        # 全部场景
  python3 generate_tasks.py --dry-run               # 试运行
  python3 generate_tasks.py --project "华北油田"     # 指定项目
  python3 generate_tasks.py --row-id <ROW_ID>       # 指定rowId
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent  # skills/
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from hap_app_access.src.mcp_client import MCPClient
from hap_app_access.src.token_reader import get_mcp_url, TokenNotFoundError

# ---- 常量（Worksheet/Field IDs）----
APP_ID = "49392ae2-6aa0-4d69-b5e7-57d4fe3fc98e"
PROJECT_WS_ID = "69ca1fb1d128aadb0c749d49"
TASK_WS_ID = "69ca1fc12304b0580845c0c2"
CUSTOMER_WS_ID = "69ca1f9f2304b0580845be9d"
CONTACTS_SUB_WS_ID = "69dd91cc924f17c90a943a06"  # 联系人子表

# 项目字段
PROJECT_STATUS_FIELD = "69ca1fb1c2498ad1448fbf3f"
PROJECT_OWNER_FIELD = "69ce1ecd6b2525025fe62c93"
PROJECT_AI_EVAL_FIELD = "69f956419f1956fc0e1867c3"
PROJECT_CUSTOMER_FIELD = "69ca2053da5dfda2243eb5fa"  # Relation -> 客户档案
PROJECT_COMPANY_FIELD = "69ccedaa237158d08688e603"   # 公司名称 (线索信息)
PROJECT_TITLE_FIELD = "69ca1fb1c2498ad1448fbf3b"     # 项目名称
PROJECT_LAST_FOLLOWUP_FIELD = "69d2e3d827546f97d192359f"  # 最后跟进时间

# 搁置阈值（天）
STALE_DAYS_THRESHOLD = 14

# 客户档案字段
CUSTOMER_NAME_FIELD = "69ca1fa0d128aadb0c749bc9"     # 客户全称
CUSTOMER_CONTACTS_FIELD = "69dd91cc924f17c90a943a04"  # 联系人 (SubTable)

# 任务字段
TASK_NAME_FIELD = "69ca1fc2f045950b8020520f"
TASK_TYPE_FIELD = "69ca39d0957a3e800df1f6b1"
TASK_SOURCE_FIELD = "69ca8508957a3e800df207dd"
TASK_STATUS_FIELD = "69ca1fc2f045950b80205213"
TASK_OWNER_FIELD = "69ca1fc2f045950b80205210"
TASK_PROJECT_FIELD = "69ca2059da5dfda2243eb62c"
TASK_END_DATE_FIELD = "69ca2fa2226c409f1db158fe"
TASK_START_DATE_FIELD = "69ca2fa2226c409f1db158fd"
TASK_DESC_FIELD = "69ca1fc2f045950b80205214"      # 任务说明

# 跟进中 option key
STATUS_FOLLOWING_UP = "70f4a77c-f268-4fd7-b6b3-7c12afd0e179"

# 任务状态 key
TASK_STATUS_RECEIVED = "c60b3807-e98f-45e2-a9af-b147b3fc4ea7"  # 待接收

MAX_TASK_NAME_LEN = 100


def diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


def ai_desc(s: str) -> str:
    return s[:180]


def _row_title(r: dict) -> str:
    # 尝试多种 title 字段名（明道云 list/details 返回不同 key）
    title = r.get("project_name") or r.get("title") or r.get("name") or ""
    return str(title) if title else ""


def _safe_list(val: Any) -> list:
    """安全转为 list。"""
    if isinstance(val, list):
        return val
    if val is not None and val != "":
        return [val]
    return []


# Field ID -> common alias mapping (get_record_list returns aliases)
FIELD_ALIAS_LOOKUP: dict[str, list[str]] = {
    PROJECT_STATUS_FIELD: ["project_status"],
    PROJECT_OWNER_FIELD: ["69ce1ecd6b2525025fe62c93"],  # list 里也返回 ID
    PROJECT_AI_EVAL_FIELD: ["69f956419f1956fc0e1867c3"],
    PROJECT_CUSTOMER_FIELD: ["customer"],
    PROJECT_COMPANY_FIELD: ["69ccedaa237158d08688e603"],
    PROJECT_TITLE_FIELD: ["project_name"],
    CUSTOMER_NAME_FIELD: ["customer_name"],
}


def _field_val(record: dict, field_id: str) -> Any:
    """从记录中按 field id 取值，兼容 alias 回退。"""
    # 直接 key 匹配
    if field_id in record:
        return record[field_id]
    # 去掉可能的前缀下划线
    for k, v in record.items():
        if k.lstrip("_") == field_id.lstrip("_"):
            return v
    # 已知 alias
    aliases = FIELD_ALIAS_LOOKUP.get(field_id, [])
    for al in aliases:
        if al in record:
            return record[al]
    return None


def check_customer_info(cli: MCPClient, project: dict) -> dict | None:
    """检查客户信息完整性。返回 None 表示完整，返回 dict 表示需创建的任务描述。"""
    project_name = _row_title(project)
    customer_rel = _field_val(project, PROJECT_CUSTOMER_FIELD)
    company_name = _field_val(project, PROJECT_COMPANY_FIELD)

    # 1. 公司名称检查
    has_customer_record = bool(customer_rel and isinstance(customer_rel, list) and customer_rel)
    has_company_name = bool(company_name and str(company_name).strip())

    if not has_customer_record and not has_company_name:
        return {
            "reason": "公司信息缺失",
            "detail": "项目未关联客户档案，且线索信息中无公司名称",
            "task_name": f"{project_name} - 创建客户档案并补录联系人",
            "task_type": "客户背景调查",
            "task_type_key": "382f2695-5304-4107-88a3-8151ed2a90e3",
            "source_section": "场景一：客户信息缺失（客户档案）",
        }

    # 2. 检查客户档案中的联系人
    if has_customer_record:
        customer_row_id = None
        for item in customer_rel:
            if isinstance(item, dict):
                customer_row_id = item.get("sid") or item.get("rowId")
            elif isinstance(item, str):
                customer_row_id = item
            if customer_row_id:
                break

        if customer_row_id:
            customer = cli.call("get_record_details", {
                "worksheet_id": CUSTOMER_WS_ID,
                "row_id": customer_row_id,
                "appId": APP_ID,
                "ai_description": ai_desc("Get customer details for info completeness check."),
            })
            if isinstance(customer, dict):
                # 检查客户全称
                cust_name = _field_val(customer, CUSTOMER_NAME_FIELD)
                if not cust_name or not str(cust_name).strip():
                    return {
                        "reason": "客户全称缺失",
                        "detail": "客户档案中客户全称为空",
                        "task_name": f"{project_name} - 完善联系人信息",
                        "task_type": "客户背景调查",
                        "task_type_key": "382f2695-5304-4107-88a3-8151ed2a90e3",
                        "source_section": "场景一：客户信息缺失（客户全称）",
                    }
                # 检查联系人子表
                contacts = _field_val(customer, CUSTOMER_CONTACTS_FIELD)
                if isinstance(contacts, list) and contacts:
                    # 联系人存在，检查姓名和职务
                    has_valid_contact = False
                    for c in contacts:
                        if isinstance(c, dict):
                            contact_name = c.get("姓名") or c.get("name") or ""
                            contact_title = c.get("职务") or c.get("title") or ""
                            if contact_name.strip() and contact_title.strip():
                                has_valid_contact = True
                                break
                    if not has_valid_contact:
                        return {
                            "reason": "联系人信息不完整",
                            "detail": "客户档案联系人子表存在，但姓名或职务缺失",
                            "task_name": f"{project_name} - 完善联系人信息",
                            "task_type": "客户背景调查",
                            "task_type_key": "382f2695-5304-4107-88a3-8151ed2a90e3",
                            "source_section": "场景一：客户信息缺失（联系人）",
                        }
                else:
                    return {
                        "reason": "联系人缺失",
                        "detail": "客户档案中无联系人记录",
                        "task_name": f"{project_name} - 完善联系人信息",
                        "task_type": "客户背景调查",
                        "task_type_key": "382f2695-5304-4107-88a3-8151ed2a90e3",
                        "source_section": "场景一：客户信息缺失（联系人）",
                    }
    else:
        # 没有客户档案关联，但可能有公司名称 → 需补录联系人
        if has_company_name:
            return {
                "reason": "联系人缺失",
                "detail": "项目有公司名称但未关联客户档案，无联系人记录",
                "task_name": f"{project_name} - 创建客户档案并补录联系人",
                "task_type": "客户背景调查",
                "task_type_key": "382f2695-5304-4107-88a3-8151ed2a90e3",
                "source_section": "场景一：客户信息缺失（客户档案）",
            }
    return None


def check_stale(project: dict) -> dict | None:
    """检查项目是否长期未跟进（>14天）。返回 None 表示正常，返回 dict 表示需创建的任务描述。"""
    last_followup = _field_val(project, PROJECT_LAST_FOLLOWUP_FIELD)
    if not last_followup:
        return None
    last_str = str(last_followup).strip()
    try:
        last_dt = datetime.strptime(last_str[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            last_dt = datetime.strptime(last_str[:10], "%Y-%m-%d")
        except ValueError:
            return None
    days_since = (datetime.now() - last_dt).days
    if days_since <= STALE_DAYS_THRESHOLD:
        return None
    return {
        "reason": f"项目 {days_since} 天未更新",
        "detail": f"最后跟进时间 {last_str[:10]}，距今 {days_since} 天，已超过 {STALE_DAYS_THRESHOLD} 天阈值",
        "task_name": f"{project_name} - 项目长期未更新（{days_since}天），需跟进",
        "task_type": "客户背景调查",
        "task_type_key": "382f2695-5304-4107-88a3-8151ed2a90e3",
        "source_section": f"场景三：项目搁置（{days_since}天未更新）",
    }


def get_project_owner_ids(project: dict) -> list[str]:
    """从项目记录中提取负责人 ID 列表。"""
    owner = _field_val(project, PROJECT_OWNER_FIELD)
    if not owner:
        return []
    if isinstance(owner, list):
        ids = []
        for o in owner:
            if isinstance(o, dict):
                ids.append(o.get("accountId") or o.get("sid") or o.get("id") or str(o))
            elif isinstance(o, str):
                ids.append(o)
        return ids
    return [str(owner)] if owner else []


def _build_task_description(task_desc: dict) -> str:
    """构建任务说明，解释为何产生此任务。"""
    section = task_desc.get("source_section", "")
    action = task_desc.get("action", "")
    reason = task_desc.get("reason", "")
    detail = task_desc.get("detail", "")
    deadline_raw = task_desc.get("deadline_raw", "")

    if "场景一" in section:
        # 客户信息缺失
        desc = f"因该项目客户信息存在缺失：{detail}"
    elif "场景二" in section or "场景三" in section:
        # 项目搁置
        desc = f"因{reason}：{detail}，需排查原因并推动进展。"
    else:
        desc = f"来源: {section}"
        if detail:
            desc += f" — {detail}"
    return desc


def create_task(cli: MCPClient, project: dict, task_desc: dict, dry_run: bool) -> dict:
    """创建一条任务。"""
    project_name = _row_title(project)
    project_row_id = project.get("rowId") or project.get("rowid") or ""

    owner_ids = get_project_owner_ids(project)

    fields = [
        {"id": TASK_NAME_FIELD, "value": task_desc["task_name"]},
        {"id": TASK_TYPE_FIELD, "value": [task_desc["task_type_key"]]},
        {"id": TASK_SOURCE_FIELD, "value": "AI"},
        {"id": TASK_STATUS_FIELD, "value": [TASK_STATUS_RECEIVED]},
        {"id": TASK_PROJECT_FIELD, "value": [project_row_id]},
    ]

    if owner_ids:
        fields.append({"id": TASK_OWNER_FIELD, "value": owner_ids})

    if task_desc.get("deadline_date"):
        fields.append({"id": TASK_END_DATE_FIELD, "value": task_desc["deadline_date"]})

    if task_desc.get("source_section"):
        # 任务说明：解释为何产生此任务
        desc = _build_task_description(task_desc)
        fields.append({"id": TASK_DESC_FIELD, "value": desc})

    if dry_run:
        return {"status": "dry_run", "task_name": task_desc["task_name"],
                "fields": fields, "project": project_name, "rowId": project_row_id}

    result = cli.call("create_record", {
        "worksheet_id": TASK_WS_ID,
        "appId": APP_ID,
        "fields": fields,
        "triggerWorkflow": True,
        "ai_description": ai_desc(f"Task: {task_desc['task_name']}. Created from {task_desc.get('source_section', 'auto')}"),
    })

    return {"status": "created", "task_name": task_desc["task_name"],
            "result": result, "project": project_name, "rowId": project_row_id}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="试运行，不实际创建任务")
    p.add_argument("--project", help="指定项目名（模糊搜索）")
    p.add_argument("--row-id", help="指定项目 rowId")
    args = p.parse_args()

    # S1 Token
    profile = os.environ.get("HAP_TOKEN_PROFILE", "claw-crm")
    try:
        mcp_url = get_mcp_url(profile, check_expiry=True)
        diag(f"S1 token_reader profile={profile}")
    except TokenNotFoundError as e:
        print(f"ERROR: Token 不可用: {e}", file=sys.stderr)
        return 2

    cli = MCPClient(mcp_url, mode="personal_mcp")
    cli.ensure_initialized()

    # S3 获取跟进中项目
    projects: list[dict] = []

    if args.row_id:
        detail = cli.call("get_record_details", {
            "worksheet_id": PROJECT_WS_ID,
            "row_id": args.row_id,
            "appId": APP_ID,
            "ai_description": ai_desc("Fetch project record for task generation."),
        })
        if isinstance(detail, dict):
            projects.append(detail)
            diag(f"S3 direct rowId={args.row_id} title={_row_title(detail)!r}")
    elif args.project:
        # 模糊搜索
        listing = cli.call("get_record_list", {
            "worksheet_id": PROJECT_WS_ID,
            "pageSize": 20,
            "pageIndex": 1,
            "search": args.project,
            "appId": APP_ID,
            "ai_description": ai_desc(f"Search project '{args.project}' for task generation."),
        })
        rows: list[dict] = []
        if isinstance(listing, dict):
            rows = listing.get("rows") or listing.get("data") or []
        elif isinstance(listing, list):
            rows = [r for r in listing if isinstance(r, dict)]
        diag(f"S3 search={args.project!r} got {len(rows)} rows")
        projects = rows
    else:
        # 全量：用「推进中」视图获取所有活跃项目
        # HAP「全部」视图有默认过滤只返回部分记录，推进中视图包含跟进中/已拜访/新机会等
        all_listing = cli.call("get_record_list", {
            "worksheet_id": PROJECT_WS_ID,
            "pageSize": 500,
            "pageIndex": 1,
            "appId": APP_ID,
            "viewId": "69e184d7f7066f665c4dcf8e",  # 推进中视图
            "ai_description": ai_desc("Fetch all active projects for task generation."),
        })
        all_rows: list[dict] = []
        if isinstance(all_listing, dict):
            all_rows = all_listing.get("rows") or all_listing.get("data") or []
        elif isinstance(all_listing, list):
            all_rows = [r for r in all_listing if isinstance(r, dict)]

        # 过滤跟进中
        for r in all_rows:
            status = _field_val(r, PROJECT_STATUS_FIELD)
            if not status:
                continue
            status_key = ""
            if isinstance(status, list) and status:
                status_key = status[0].get("key", "") if isinstance(status[0], dict) else str(status[0])
            elif isinstance(status, dict):
                status_key = status.get("key", "")
            elif isinstance(status, str):
                status_key = status
            if status_key == STATUS_FOLLOWING_UP:
                projects.append(r)
        diag(f"S3 all projects: {len(all_rows)} total, {len(projects)} in 跟进中")

    if not projects:
        print(json.dumps({"ok": True, "message": "没有符合条件的项目", "tasks_created": []},
                         ensure_ascii=False, indent=2))
        return 0

    # S4 处理每个项目
    all_tasks: list[dict] = []
    skipped: list[dict] = []

    for project in projects:
        project_name = _row_title(project)
        row_id = project.get("rowId") or project.get("rowid") or ""

        diag(f"\nS4 project: {project_name} ({row_id})")

        # 只有跟进中状态的项目才检查
        status = _field_val(project, PROJECT_STATUS_FIELD)
        status_key = ""
        if isinstance(status, list) and status:
            status_key = status[0].get("key", "") if isinstance(status[0], dict) else str(status[0])
        elif isinstance(status, dict):
            status_key = status.get("key", "")
        elif isinstance(status, str):
            status_key = status

        if status_key != STATUS_FOLLOWING_UP:
            diag(f"  skip: 状态非跟进中")
            skipped.append({"project": project_name, "reason": f"状态={status_key}，非跟进中"})
            continue

        # 场景一：客户信息检查
        info_gap = check_customer_info(cli, project)
        if info_gap:
            diag(f"  [info] {info_gap['reason']}: {info_gap['detail']}")
            result = create_task(cli, project, info_gap, args.dry_run)
            all_tasks.append(result)
        else:
            diag(f"  [info] 客户信息完整")
            skipped.append({"project": project_name, "scenario": "info", "reason": "信息完整"})

        # 场景二：项目搁置检测（>14天未更新）
        stale_gap = check_stale(project)
        if stale_gap:
            diag(f"  [stale] {stale_gap['reason']}: {stale_gap['detail']}")
            result = create_task(cli, project, stale_gap, args.dry_run)
            all_tasks.append(result)
        else:
            diag(f"  [stale] 跟进正常")

    # S5 输出
    output = {
        "ok": True,
        "dry_run": args.dry_run,
        "projects_scanned": len(projects),
        "tasks_created": len(all_tasks),
        "tasks": all_tasks,
        "skipped": skipped,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
