import hashlib
import random
import json


def description_pool_hash(descriptions):
    rows = sorted(({key: row.get(key) for key in ("id", "text", "weight", "enabled")} for row in descriptions), key=lambda row: row["id"])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def description_review_ready(product):
    context = product.get("publishing_context", {})
    required = context.get("description_review_required") or context.get("description_generation_id")
    review = context.get("description_review", {})
    return not required or (review.get("source") == "USER_CONFIRMED" and review.get("sha256") == description_pool_hash(product.get("descriptions", [])))


class DescriptionSelector:
    def __init__(self, rng=None):
        self.rng = rng or random.SystemRandom()

    def select(self, descriptions, policy, recent=()):
        count = policy.get("exclude_last_n", 3)
        excluded = list(recent)[:count]
        ids = {row["description_id"] for row in excluded}
        hashes = {row["description_sha256"] for row in excluded}
        available = [{**row, "sha256": hashlib.sha256(row["text"].encode()).hexdigest()} for row in descriptions
            if row.get("enabled", True) and row.get("weight", 1) > 0]
        available = [row for row in available if row["id"] not in ids and row["sha256"] not in hashes]
        if not available:
            raise ValueError("DESCRIPTION_POOL_EXHAUSTED：最近N条排除后无可用文案，请补充文案或调整策略")
        mode = policy.get("mode", "random")
        if mode == "weighted":
            return self.rng.choices(available, weights=[row.get("weight", 1) for row in available], k=1)[0]
        if mode != "random":
            raise ValueError("DESCRIPTION_SELECTION_MODE_INVALID")
        return self.rng.choice(available)
