import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from app.integrations.process_runner import BASE_ENV_KEYS

RULE_KEYS = (
    "min_views", "min_like_view_ratio", "product_match_min_confidence",
    "reference_product_match_required", "accepted_product_match", "accepted_ai_origin",
    "accepted_cutout_suitability", "manual_review_on_uncertain",
    "min_likes", "accepted_require_cutout_suitability", "filter_overlay", "filter_watermark",
    "filter_subtitle", "person_integrity", "vidbee_require_watermark_review",
)

FACTORY_RULE_VERSION = "factory-collector-rules-v1"
FACTORY_RULE_INPUT_KEYS = ("min_views", "min_likes", "product_match_min_confidence",
                           "overlay_requirement", "watermark_requirement", "subtitle_requirement")


def factory_rule_values(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - {*FACTORY_RULE_INPUT_KEYS, "min_like_view_ratio"}:
        raise ValueError("FACTORY_RULE_FIELDS_INVALID")
    result = {}
    for key in ("min_views", "min_likes"):
        value = values.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("FACTORY_RULE_NUMBER_INVALID:" + key)
        result[key] = value
    confidence = values.get("product_match_min_confidence", .85)
    if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("FACTORY_RULE_CONFIDENCE_INVALID")
    result["product_match_min_confidence"] = float(confidence)
    for key in ("overlay_requirement", "watermark_requirement", "subtitle_requirement"):
        value = values.get(key, "any")
        if value not in ("any", "present", "absent"):
            raise ValueError("FACTORY_RULE_REQUIREMENT_INVALID:" + key)
        result[key] = value
    result["min_like_view_ratio"] = result["min_likes"] / result["min_views"] if result["min_views"] else 0.0
    if "min_like_view_ratio" in values and values["min_like_view_ratio"] != result["min_like_view_ratio"]:
        raise ValueError("FACTORY_RATIO_IS_DERIVED")
    return result


def factory_rule_snapshot(values: dict) -> dict:
    rules = factory_rule_values(values)
    digest = hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": FACTORY_RULE_VERSION, "sha256": digest, "rules": rules}


def verified_factory_rules(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict) or snapshot != factory_rule_snapshot(snapshot.get("rules")):
        raise ValueError("FACTORY_RULE_FINGERPRINT_INVALID")
    return snapshot["rules"]


def read_factory_rules(settings, legacy_snapshot: dict | None = None) -> dict:
    path = Path(settings.storage_root) / "collector_inputs" / "rules-production.json"
    if path.is_file():
        stored = json.loads(path.read_text(encoding="utf-8"))
        verified_factory_rules(stored["snapshot"])
        return {**stored, "persisted": True}
    legacy = (legacy_snapshot or read_production_rules(settings))["rules"]
    minimum_likes = int(legacy["min_likes"]) if legacy.get("min_likes") is not None else math.ceil(int(legacy["min_views"]) * float(legacy["min_like_view_ratio"]))
    snapshot = factory_rule_snapshot({"min_views": int(legacy["min_views"]), "min_likes": minimum_likes,
        "product_match_min_confidence": legacy["product_match_min_confidence"]})
    return {"snapshot": snapshot, "persisted": False, "updated_at": None, "origin": "legacy-conservative-default"}


def save_factory_rules(settings, values: dict) -> dict:
    stored = {"snapshot": factory_rule_snapshot(values), "updated_at": datetime.now(timezone.utc).isoformat()}
    path = Path(settings.storage_root) / "collector_inputs" / "rules-production.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return {**stored, "persisted": True}


def factory_metric_gate(rules: dict, likes: int, views: int) -> tuple[str, str] | None:
    ratio = likes / views if views else 0.0
    if views < rules["min_views"] or likes < rules["min_likes"] or ratio < rules["min_like_view_ratio"]:
        return "rejected", "factory_metric_threshold"
    return None


def factory_presence_gate(rules: dict, detected: dict) -> tuple[str, str] | None:
    unknown = False
    for subject in ("overlay", "watermark", "subtitle"):
        requirement = rules[subject + "_requirement"]
        actual = detected.get(subject + "_detected")
        if requirement == "any":
            continue
        if not isinstance(actual, bool):
            unknown = True
        elif actual != (requirement == "present"):
            return "rejected", "factory_" + subject + "_requirement"
    return ("manual_review", "factory_detection_unverified") if unknown else None


def read_production_rules(settings) -> dict:
    path = settings.legacy_tiktok_auto_root / "config.yaml"
    script = "import json; from pathlib import Path; from src.config import load_config; config=load_config(Path('config.yaml')); print(json.dumps({k: config.get(k) for k in " + repr(RULE_KEYS) + "}))"
    environment = {key: os.environ[key] for key in BASE_ENV_KEYS if key in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen([str(settings.legacy_tiktok_auto_python), "-B", "-c", script],
                               cwd=settings.legacy_tiktok_auto_root, env=environment, shell=False,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, _stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("LEGACY_CONFIG_TIMEOUT") from None
    if process.returncode:
        raise RuntimeError("LEGACY_CONFIG_READ_FAILED")
    rules = json.loads(stdout)
    if not isinstance(rules, dict) or set(rules) != set(RULE_KEYS):
        raise RuntimeError("LEGACY_CONFIG_INVALID")
    return {"source": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rules": rules, "read_only": True}
