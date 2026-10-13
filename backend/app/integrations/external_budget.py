"""Reserve authorized paid calls, preserving unknown requests across approvals."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.integrations.file_lock import lock_file


class ExternalBudgetExceeded(RuntimeError):
    pass


def reserve(storage_root, family, request_key):
    directory = Path(storage_root).resolve() / "external_budget"
    authorization = directory / "active.json"
    if not authorization.is_file():
        raise ExternalBudgetExceeded("EXTERNAL_AUTHORIZATION_REQUIRED")
    with (directory / "ledger.lock").open("a") as lock:
        lock_file(lock)
        approved = json.loads(authorization.read_text())
        if approved.get("status") != "APPROVED" or family not in approved.get("limits", {}):
            raise ExternalBudgetExceeded("EXTERNAL_AUTHORIZATION_REQUIRED")
        ledger = directory / (approved["id"] + ".json")
        state = json.loads(ledger.read_text()) if ledger.is_file() else {"authorization_id": approved["id"], "requests": []}
        for path in directory.glob("*.json"):
            if path.name == "active.json":
                continue
            history = json.loads(path.read_text())
            if any(row["family"] == family and row["request_key"] == request_key for row in history.get("requests", [])):
                raise ExternalBudgetExceeded("UNKNOWN_EXTERNAL_REQUEST: reservation already consumed; verify the saved result before retry")
        limit = approved["limits"][family]
        if limit is None and approved.get("limit_policy") != "UNLIMITED_USER_AUTHORIZED":
            raise ExternalBudgetExceeded("EXTERNAL_AUTHORIZATION_REQUIRED")
        if limit is not None and sum(row["family"] == family for row in state["requests"]) >= limit:
            raise ExternalBudgetExceeded("EXTERNAL_BUDGET_EXHAUSTED:" + family)
        record = {"family": family, "request_key": request_key, "reserved_at": datetime.now(timezone.utc).isoformat(), "status": "RESERVED"}
        state["requests"].append(record)
        temporary = ledger.with_suffix(".pending")
        with temporary.open("w") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(ledger)
        return record
