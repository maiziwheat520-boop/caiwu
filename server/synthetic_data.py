from __future__ import annotations

import hashlib
from copy import deepcopy


SYNTHETIC_CANDIDATES: list[dict[str, object]] = [
    {
        "id": "2d0d0cb9-d4ab-4e3f-9879-7812882b8f21",
        "short_id": "C-8F21",
        "revision": 1,
        "status": "PENDING",
        "source_channel": "telegram",
        "source_message_id": "synthetic-tg-1001",
        "received_at": "2026-08-24T01:42:00+08:00",
        "business_unit": "城南店",
        "category": "布草",
        "amount_minor": 638000,
        "currency": "CNY",
        "accounting_month": "2026-08",
        "summary": "城南店 8 月布草清洗费用，供应商月结单",
        "confidence_basis_points": 9600,
        "evidence": [
            {"id": "1dedc967-753a-4c02-8409-e51c02e6cc18", "kind": "message", "media_type": "text/plain; charset=utf-8", "sha256": "1" * 64, "original_filename": None},
            {"id": "5715f313-93d0-4b3d-8f58-180d14ba5a7a", "kind": "attachment", "media_type": "application/pdf", "sha256": "2" * 64, "original_filename": "城南店_8月布草账单.pdf"},
        ],
        "blockers": [],
    },
    {
        "id": "cf8efc6d-5955-4f48-b52c-6bfa2e547a64",
        "short_id": "C-7A64",
        "revision": 1,
        "status": "PENDING",
        "source_channel": "dingtalk",
        "source_message_id": "synthetic-dd-1002",
        "received_at": "2026-08-24T00:16:00+08:00",
        "business_unit": "江景店",
        "category": "瓶装水",
        "amount_minor": 214800,
        "currency": "CNY",
        "accounting_month": "2026-08",
        "summary": "江景店瓶装水采购，数量与单价已从附件提取",
        "confidence_basis_points": 9100,
        "evidence": [
            {"id": "cb8b64bc-67f9-42d7-92a3-30c024ca5ab5", "kind": "message", "media_type": "text/plain; charset=utf-8", "sha256": "3" * 64, "original_filename": None},
            {"id": "ee52399e-1b4b-4a10-8534-a6f30809bdb6", "kind": "attachment", "media_type": "image/jpeg", "sha256": "4" * 64, "original_filename": "送货单_0822.jpg"},
        ],
        "blockers": [],
    },
    {
        "id": "430d322d-461d-41e9-89ba-7e8ed04d62d9",
        "short_id": "C-62D9",
        "revision": 1,
        "status": "INCOMPLETE",
        "source_channel": "weixin",
        "source_message_id": "synthetic-wx-1003",
        "received_at": "2026-08-23T17:35:00+08:00",
        "business_unit": "机场店",
        "category": "水费",
        "amount_minor": 483260,
        "currency": "CNY",
        "accounting_month": None,
        "summary": "机场店水费，原消息未说明归属月份",
        "confidence_basis_points": 8800,
        "evidence": [
            {"id": "525408c5-0424-42cc-89d2-5d8d331ed029", "kind": "message", "media_type": "text/plain; charset=utf-8", "sha256": "5" * 64, "original_filename": None},
            {"id": "5cec777e-3472-4127-84d1-4f867520ac20", "kind": "attachment", "media_type": "image/png", "sha256": "6" * 64, "original_filename": "水费回单.png"},
        ],
        "blockers": [{"code": "MISSING_ACCOUNTING_MONTH", "message": "原始消息未提供归属月份，系统建议值不能直接进入报表。"}],
    },
    {
        "id": "d92f2482-d0a6-46de-809c-e68f9c735b17",
        "short_id": "C-5B17",
        "revision": 1,
        "status": "CONFLICTED",
        "source_channel": "telegram",
        "source_message_id": "synthetic-tg-1004",
        "received_at": "2026-08-23T14:02:00+08:00",
        "business_unit": "城南店",
        "category": "银行收款",
        "amount_minor": 1268000,
        "currency": "CNY",
        "accounting_month": "2026-08",
        "summary": "城南店银行收款，与另一条候选金额和凭证号冲突",
        "confidence_basis_points": 9400,
        "evidence": [{"id": "57fcbe13-73d0-40c0-94ad-7358e7a71e32", "kind": "message", "media_type": "text/plain; charset=utf-8", "sha256": "7" * 64, "original_filename": None}],
        "blockers": [{"code": "BUSINESS_KEY_CONFLICT", "message": "相同流水尾号存在另一条金额不同的合成候选。"}],
    },
    {
        "id": "f16cef2e-321f-431d-b73c-e865ae2249e3",
        "short_id": "C-49E3",
        "revision": 2,
        "status": "CONFIRMED",
        "source_channel": "dingtalk",
        "source_message_id": "synthetic-dd-1005",
        "received_at": "2026-08-21T11:28:00+08:00",
        "business_unit": "江景店",
        "category": "税费",
        "amount_minor": 924050,
        "currency": "CNY",
        "accounting_month": "2026-08",
        "summary": "江景店本月税费缴款",
        "confidence_basis_points": 9800,
        "evidence": [
            {"id": "ef5e4308-b01a-4d05-b53c-403ad3857fc8", "kind": "message", "media_type": "text/plain; charset=utf-8", "sha256": "8" * 64, "original_filename": None},
            {"id": "7c92cd2b-12bf-4141-b849-58f87859dc5d", "kind": "attachment", "media_type": "application/pdf", "sha256": "9" * 64, "original_filename": "电子缴款书.pdf"},
        ],
        "blockers": [],
    },
]


SYNTHETIC_REVIEW_EVENTS: dict[str, list[dict[str, object]]] = {
    "f16cef2e-321f-431d-b73c-e865ae2249e3": [{
        "id": "428cf469-f596-4716-af00-b910552a3021",
        "candidate_id": "f16cef2e-321f-431d-b73c-e865ae2249e3",
        "sequence": 1,
        "from_revision": 1,
        "to_revision": 2,
        "decision": "CONFIRM",
        "actor": "prototype-single-user",
        "reason": "合成数据预置确认事件",
        "changes": [{"field": "status", "previous_value": "PENDING", "new_value": "CONFIRMED"}],
        "conflict_resolution": None,
        "created_at": "2026-08-21T11:35:00+08:00",
    }]
}


SYNTHETIC_RECONCILIATION: dict[str, object] = {
    "accounting_month": "2026-08",
    "revision": 7,
    "ready": False,
    "blockers": [
        {"code": "BUSINESS_KEY_CONFLICT", "message": "城南店银行收款候选存在金额冲突。"},
        {"code": "MISSING_ACCOUNTING_MONTH", "message": "机场店水费候选尚未确认归属月份。"},
    ],
    "business_units": [
        {"name": "城南店", "amounts_minor": {"水费": 512080, "税费": 1134000, "布草": 638000, "瓶装水": 187600, "银行收款": 4286000}},
        {"name": "江景店", "amounts_minor": {"水费": 609420, "税费": 924050, "布草": 742000, "瓶装水": 214800, "银行收款": 5162000}},
        {"name": "机场店", "amounts_minor": {"水费": 0, "税费": 1068000, "布草": 589000, "瓶装水": 236000, "银行收款": 3924000}},
    ],
}


SYNTHETIC_CONNECTIONS: list[dict[str, str]] = [
    {"id": "hermes_ingress", "state": "NOT_CONFIGURED", "checked_at": "2026-08-24T02:00:00+08:00", "detail": "合成预览未启用真实消息入口。"},
    {"id": "ledgerbridge_core", "state": "NOT_CONFIGURED", "checked_at": "2026-08-24T02:00:00+08:00", "detail": "合成预览未连接 LedgerBridge 数据库。"},
    {"id": "onedrive_appfolder", "state": "NOT_CONFIGURED", "checked_at": "2026-08-24T02:00:00+08:00", "detail": "未配置 OneDrive App Folder；没有保存任何令牌。"},
    {"id": "libreoffice_worker", "state": "DISCONNECTED", "checked_at": "2026-08-24T02:00:00+08:00", "detail": "合成预览不执行工作簿重算。"},
]


SYNTHETIC_EVIDENCE_CONTENT: dict[str, dict[str, object]] = {
    "1dedc967-753a-4c02-8409-e51c02e6cc18": {
        "content": "【合成原文】财务 城南店8月布草清洗费6380元，账单见附件。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "inline",
        "filename": "synthetic-message-1dedc967.txt",
    },
    "5715f313-93d0-4b3d-8f58-180d14ba5a7a": {
        "content": "【合成附件占位】城南店 8 月布草账单。本文件不含真实财务数据。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "attachment",
        "filename": "synthetic-attachment-5715f313.txt",
    },
    "cb8b64bc-67f9-42d7-92a3-30c024ca5ab5": {
        "content": "【合成原文】江景店这批水已到，财务记在八月份，合计2148。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "inline",
        "filename": "synthetic-message-cb8b64bc.txt",
    },
    "ee52399e-1b4b-4a10-8534-a6f30809bdb6": {
        "content": "【合成附件占位】瓶装水送货单 0822。本文件不含真实财务数据。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "attachment",
        "filename": "synthetic-attachment-ee52399e.txt",
    },
    "525408c5-0424-42cc-89d2-5d8d331ed029": {
        "content": "【合成原文】机场店这次水费4832.60，缴费回单发你了。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "inline",
        "filename": "synthetic-message-525408c5.txt",
    },
    "5cec777e-3472-4127-84d1-4f867520ac20": {
        "content": "【合成附件占位】机场店水费回单。本文件不含真实财务数据。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "attachment",
        "filename": "synthetic-attachment-5cec777e.txt",
    },
    "57fcbe13-73d0-40c0-94ad-7358e7a71e32": {
        "content": "【合成原文】城南店收款12680元，流水尾号9321。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "inline",
        "filename": "synthetic-message-57fcbe13.txt",
    },
    "ef5e4308-b01a-4d05-b53c-403ad3857fc8": {
        "content": "【合成原文】江景店本月税费9240.50，电子缴款书已上传。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "inline",
        "filename": "synthetic-message-ef5e4308.txt",
    },
    "7c92cd2b-12bf-4141-b849-58f87859dc5d": {
        "content": "【合成附件占位】江景店电子缴款书。本文件不含真实财务数据。\n".encode(),
        "content_type": "text/plain; charset=utf-8",
        "disposition": "attachment",
        "filename": "synthetic-attachment-7c92cd2b.txt",
    },
}


# Keep every evidence reference digest consistent with the bytes served by the
# synthetic BFF. These are process constants and never contain real evidence.
for _candidate in SYNTHETIC_CANDIDATES:
    for _reference in _candidate["evidence"]:
        _evidence_id = str(_reference["id"])
        _reference["sha256"] = hashlib.sha256(SYNTHETIC_EVIDENCE_CONTENT[_evidence_id]["content"]).hexdigest()


def initial_candidates() -> list[dict[str, object]]:
    return deepcopy(SYNTHETIC_CANDIDATES)


def initial_review_events() -> dict[str, list[dict[str, object]]]:
    return deepcopy(SYNTHETIC_REVIEW_EVENTS)
