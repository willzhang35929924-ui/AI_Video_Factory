"""Stable TikTok source identity, independent of keyword, country and attempts."""
import re
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit, urlunsplit


def identity_for(video_id=None, url=None):
    native = str(video_id or "").strip()
    native = native if re.fullmatch(r"[0-9]{1,30}", native) else ""
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    if host and host != "tiktok.com" and not host.endswith(".tiktok.com"):
        raise ValueError("COLLECTION_PLATFORM_MISMATCH")
    match = re.search(r"/(?:video|photo|v|embed/v2)/([0-9]{1,30})(?:[/.]|$)", parsed.path)
    query_ids = [value for key, values in parse_qs(parsed.query).items()
                 if key in {"item_id", "aweme_id", "share_item_id"} for value in values
                 if re.fullmatch(r"[0-9]{1,30}", value)]
    identifiers = set(query_ids + ([match.group(1)] if match else []) + ([native] if native else []))
    if len(identifiers) > 1:
        raise ValueError("COLLECTION_IDENTITY_CONFLICT")
    if not identifiers:
        return None
    native = next(iter(identifiers))
    canonical_url = urlunsplit(("https", "www.tiktok.com", parsed.path.rstrip("/"), "", "")) if match else f"https://www.tiktok.com/video/{native}"
    return {"id": "tiktok:" + native, "platform": "tiktok", "native_video_id": native,
            "canonical_url": canonical_url, "confidence": "NATIVE_ID"}


def task_identity(task, state=None):
    if task.get("execution_mode") != "integration" or task.get("context", {}).get("execution_scope") == "TECHNICAL_VALIDATION":
        return None
    values = [task.get("collector_result", {}), task.get("context", {}).get("task_context", {}),
              task.get("context", {}).get("collection_source", {})]
    if state:
        values += [{"video_id": row.get("video_id")} for row in state.get("legacy_bindings", []) if row["task_id"] == task["id"]]
        values += [{"video_id": row.get("legacy_video_id"), "video_url": row.get("source_url")}
                   for row in state.get("assets", []) if row["task_id"] == task["id"] and row["type"] in {"source", "source_candidate"}
                   and row.get("legacy_reference", {}).get("execution_scope") != "TECHNICAL_VALIDATION"]
    identities = [identity for value in values
                  if (identity := identity_for(value.get("video_id"), value.get("video_url") or value.get("url")))]
    if len({row["id"] for row in identities}) > 1:
        raise ValueError("COLLECTION_IDENTITY_CONFLICT")
    return identities[0] if identities else None


def history_groups(state):
    groups, conflicts = {}, []
    evidence = defaultdict(lambda: {"assets": [], "legacy_bindings": []})
    for key in ("assets", "legacy_bindings"):
        for row in state.get(key, []):
            evidence[row["task_id"]][key].append(row)
    for task in state["tasks"]:
        try:
            identity = task_identity(task, evidence[task["id"]])
        except ValueError as exc:
            conflicts.append({"task_id": task["id"], "reason": str(exc)})
            continue
        if identity:
            groups.setdefault(identity["id"], []).append((task, identity))
    return groups, conflicts


def canonical_record(entries):
    task, identity = min(entries, key=lambda value: (
        not bool(value[0].get("collector_result", {}).get("video_id")), value[0]["created_at"], value[0]["id"]))
    return {**identity, "canonical_task_id": task["id"], "first_seen_at": task["created_at"]}


def lookup_identity(state, video_id=None, url=None):
    identity = identity_for(video_id, url)
    if identity is None:
        return None
    existing = next((row for row in state.get("collection_identities", []) if row["id"] == identity["id"]), None)
    if existing:
        return existing
    groups, conflicts = history_groups(state)
    if conflicts:
        # A conflicting native ID cannot be safely treated as a new source.
        for conflict in conflicts:
            task = next(row for row in state["tasks"] if row["id"] == conflict["task_id"])
            if identity["native_video_id"] in str(task.get("collector_result", {})) or identity["native_video_id"] in str(task.get("context", {})):
                raise ValueError("COLLECTION_IDENTITY_CONFLICT")
    if identity["id"] not in groups:
        return None
    existing = canonical_record(groups[identity["id"]])
    state.setdefault("collection_identities", []).append(existing)
    return existing


def claim_identity(state, task_id, video_id=None, url=None):
    identity = identity_for(video_id, url)
    if identity is None:
        return None
    existing = lookup_identity(state, video_id, url)
    task = next(row for row in state["tasks"] if row["id"] == task_id)
    if existing is None:
        existing = {**identity, "canonical_task_id": task_id, "first_seen_at": task["created_at"]}
        state.setdefault("collection_identities", []).append(existing)
    duplicate = existing["canonical_task_id"] != task_id
    task.setdefault("context", {})["collection_identity"] = {
        "source_identity_key": existing["id"], "canonical_task_id": existing["canonical_task_id"],
        "status": "DUPLICATE_SKIPPED" if duplicate else "CANONICAL"}
    return {**existing, "source_identity_key": existing["id"], "duplicate": duplicate}
