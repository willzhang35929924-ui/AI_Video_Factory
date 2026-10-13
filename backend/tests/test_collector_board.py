import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.integrations.collector_board import CollectorBoard, create_collector_board_router
from app.repository import StateRepository


class CollectorBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = StateRepository("sqlite:///" + str(Path(self.temp.name) / "board.db"),
            Path(__file__).resolve().parents[1] / "app/seed.json")
        self.board = CollectorBoard(self.repository)
        original = self.repository.state()["tasks"][0]
        created = datetime(2026, 10, 9, tzinfo=timezone.utc)
        statuses = ["CREATED"] * 41 + ["RUNNING", "WAITING_REVIEW", "WAITING_REVIEW", "WAITING_REVIEW",
            "REJECTED", "COLLECTED", "FAILED", "CANCELLED", "PAUSED", "SKIPPED"]
        def populate(state):
            for index, status in enumerate(statuses):
                task = {**original, "id": f"board-{index:03}", "execution_mode": "integration", "status": status,
                    "country": "墨西哥" if index % 2 else "法国", "keyword": "real keyword" if index % 2 else "other keyword",
                    "created_at": (created + timedelta(seconds=index)).isoformat(), "updated_at": (created + timedelta(seconds=index)).isoformat(),
                    "source_video_id": None, "current_stage": "collector", "context": {"production_rules": {"sha256": "rules-hash"}, "task_context": {"context_id": f"ctx-{index}", "video_id": str(700000 + index)}},
                    "collector_result": {"title": f"video title {index}", "author": f"author_{index}", "video_id": str(700000 + index)}}
                if index == 43:
                    task["collector_result"]["error_code"] = "EXTERNAL_BUDGET_EXHAUSTED"
                if index == 44:
                    task["context"]["collector_candidate_decision"] = {"decision": "ACCEPTED"}
                state["tasks"].append(task)
            state["tasks"].append({**original, "id": "local-cutout-only", "execution_mode": "integration", "context": {"local_cutout": {"status": "READY"}}})
            for index in range(3):
                state["runs"].append({"id": f"board-run-{index}", "task_id": "board-043", "stage": "collector", "provider": "LegacyCollectorAdapter",
                    "status": "success", "progress": 100, "logs": ["old log"], "created_at": (created + timedelta(minutes=index)).isoformat(),
                    "error_code": "EXTERNAL_BUDGET_EXHAUSTED"})
        self.repository.mutate(populate)
        app = FastAPI()
        app.include_router(create_collector_board_router(self.repository))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.repository.engine.dispose()
        self.temp.cleanup()

    def test_classifications_are_disjoint_and_counted_by_database(self):
        result = self.board.list()
        self.assertEqual(result["total"], 51)
        counts = {row["status"]: row["count"] for row in result["categories"]}
        self.assertEqual(sum(counts.values()), 51)
        self.assertEqual(counts["PENDING"], 41)
        for key in ("PROCESSING", "WAITING_REVIEW", "WAITING_RESOURCE", "ACCEPTED_QUEUED", "REJECTED", "COMPLETED", "FAILED", "CANCELLED", "PAUSED", "SKIPPED"):
            self.assertEqual(counts[key], 1)
            page = self.board.list(status=key)
            self.assertEqual(page["total"], 1)
            self.assertEqual(len(page["items"]), 1)
        self.assertEqual(result["matching_total"], 51)

    def test_page_sizes_offset_and_out_of_range_correction(self):
        for size, pages in ((10, 6), (30, 2), (50, 2)):
            first = self.board.list(page_size=size)
            self.assertEqual(len(first["items"]), size)
            self.assertEqual(first["total_pages"], pages)
            last = self.board.list(page=999, page_size=size)
            self.assertEqual(last["page"], pages)
            self.assertTrue(last["items"])
        one = self.board.list(page=1)
        two = self.board.list(page=2)
        self.assertFalse({row["id"] for row in one["items"]} & {row["id"] for row in two["items"]})
        empty = self.board.list(search="no matching row", page=9)
        self.assertEqual((empty["items"], empty["total_pages"], empty["page"]), ([], 0, 1))

    def test_search_country_platform_and_sort_use_real_fields(self):
        for search in ("700043", "video title 43", "author_43", "board-043"):
            result = self.board.list(search=search)
            self.assertEqual([row["id"] for row in result["items"]], ["board-043"])
        result = self.board.list(search="real keyword", country="墨西哥", page_size=50)
        self.assertEqual(result["total"], 25)
        self.assertTrue(all(row["country"] == "墨西哥" for row in result["items"]))
        self.assertEqual(self.board.list(platform="TikTok")["total"], 51)
        self.assertEqual(self.board.list(platform="other")["total"], 0)
        self.assertEqual(self.board.list(sort="created_asc")["items"][0]["id"], "board-000")
        self.assertEqual(self.board.list(sort="created_desc")["items"][0]["id"], "board-050")
        self.assertEqual(self.board.list(search="%")["total"], 0)

    def test_no_full_state_read_and_only_latest_page_run_is_returned(self):
        statements = []
        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(self.repository.engine, "before_cursor_execute", capture)
        try:
            with patch.object(self.repository, "state", side_effect=AssertionError("full state is forbidden")):
                result = self.board.list(status="WAITING_RESOURCE")
        finally:
            event.remove(self.repository.engine, "before_cursor_execute", capture)
        self.assertEqual([row["id"] for row in result["runs"]], ["board-run-2"])
        self.assertEqual(result["runs"][0]["logs"], ["old log"])
        self.assertTrue(any("LIMIT" in statement and "OFFSET" in statement for statement in statements))
        self.assertTrue(any("GROUP BY" in statement for statement in statements))

    def test_http_contract_rejects_invalid_paging_filters(self):
        response = self.client.get("/api/collector/tasks", params={"page_size": 30, "page": 2, "status": "PENDING"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["page_size"], response.json()["total"]), (30, 41))
        for params in ({"page_size": 20}, {"page": 0}, {"status": "made-up"}, {"sort": "raw-sql"}):
            self.assertEqual(self.client.get("/api/collector/tasks", params=params).status_code, 422)

    def test_downstream_review_is_not_counted_as_collector_manual_review(self):
        self.repository.mutate(lambda state: next(value for value in state["tasks"] if value["id"] == "board-042").update(current_stage="background"))
        result = self.board.list(status="DOWNSTREAM_REVIEW")
        self.assertEqual([value["id"] for value in result["items"]], ["board-042"])
        self.assertEqual(self.board.list(status="WAITING_REVIEW")["total"], 0)

