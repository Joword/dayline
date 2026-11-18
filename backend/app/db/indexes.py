"""Index definitions (plan §11.2). Idempotent; run at API start-up and by `dl db init`."""

from __future__ import annotations

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase


def index_models(activity_ttl_days: int) -> dict[str, list[IndexModel]]:
    return {
        "reports": [
            IndexModel(
                [("user_id", ASCENDING), ("date", ASCENDING), ("version", ASCENDING)],
                unique=True,
                name="user_date_version",
            ),
            IndexModel([("date", ASCENDING), ("status", ASCENDING)], name="date_status"),
        ],
        "users": [
            IndexModel([("org_path", ASCENDING)], name="org_path"),
            IndexModel([("manager_id", ASCENDING)], name="manager_id"),
            IndexModel([("api_tokens.hash", ASCENDING)], name="api_token_hash", sparse=True),
            IndexModel([("dingtalk_id", ASCENDING)], name="dingtalk_id", sparse=True),
        ],
        "reviews": [
            IndexModel(
                [("subject_id", ASCENDING), ("reviewer_id", ASCENDING), ("created_at", DESCENDING)],
                name="subject_reviewer_created",
            ),
        ],
        "audit_logs": [
            IndexModel([("actor_id", ASCENDING), ("ts", DESCENDING)], name="actor_ts"),
            IndexModel([("target", ASCENDING), ("ts", DESCENDING)], name="target_ts"),
        ],
        "escalations": [
            IndexModel([("current_holder_id", ASCENDING), ("status", ASCENDING)], name="holder_status"),
            IndexModel(
                [("participants", ASCENDING), ("updated_at", DESCENDING)], name="participants_updated"
            ),
            IndexModel([("created_by", ASCENDING), ("origin.date", ASCENDING)], name="creator_date"),
        ],
        "notifications": [
            IndexModel(
                [("user_id", ASCENDING), ("read_at", ASCENDING), ("created_at", DESCENDING)],
                name="user_unread",
            ),
            # partial, not sparse: a sparse index would still index explicit nulls and collide
            IndexModel(
                [("dedupe_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"dedupe_key": {"$type": "string"}},
                name="dedupe",
            ),
        ],
        "activity_cache": [
            IndexModel(
                [("user_id", ASCENDING), ("date", ASCENDING), ("source", ASCENDING)],
                unique=True,
                name="user_date_source",
            ),
            IndexModel(
                [("fetched_at", ASCENDING)],
                expireAfterSeconds=activity_ttl_days * 86400,
                name="ttl_fetched_at",
            ),
        ],
        "integrations": [
            IndexModel([("user_id", ASCENDING), ("type", ASCENDING)], name="user_type"),
        ],
        "outbox": [
            IndexModel([("status", ASCENDING), ("next_retry_at", ASCENDING)], name="status_retry"),
            IndexModel(
                [("dedupe_key", ASCENDING)],
                unique=True,
                partialFilterExpression={"dedupe_key": {"$type": "string"}},
                name="dedupe",
            ),
        ],
        # DingTalk Stream may redeliver a bot message: remember handled message ids for 2 days
        "dingtalk_events": [
            IndexModel([("created_at", ASCENDING)], expireAfterSeconds=2 * 86400, name="ttl"),
        ],
        "tools": [
            IndexModel([("status", ASCENDING)], name="status"),
            IndexModel([("owner", ASCENDING)], name="owner"),
        ],
        "skill_runs": [
            IndexModel([("skill", ASCENDING), ("created_at", DESCENDING)], name="skill_created"),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_created"),
        ],
    }


async def ensure_indexes(db: AsyncDatabase, activity_ttl_days: int = 7) -> None:
    for coll, models in index_models(activity_ttl_days).items():
        await db[coll].create_indexes(models)
