"""Tickets have their own storage, tools and router; never the life list."""
import asyncio
import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def store(tmp_path, monkeypatch):
    import brain_todos
    from g_todos import tickets
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    monkeypatch.setattr(tickets, "TICKETS_DIR", tmp_path / "tickets")
    return tickets, brain_todos


def test_ticket_lifecycle_and_closed_idempotency(store):
    tickets, todos = store
    item = tickets.create_ticket("Fix sample workflow", "Investigate the failure.", project="sample",
                                  source_key="workflow:fake", requester="agent")
    assert item["kind"] == "ticket" and item["status"] == "open"
    assert tickets.list_tickets(project="other") == []
    assert tickets.list_tickets(project="sample") == [item]
    closed = tickets.close_ticket(item["slug"], note="Fixed the configuration.")
    assert closed["closed"] == dt.date.today().isoformat()
    assert closed["provenance"] == item["provenance"]
    assert "Fixed the configuration." in closed["body"]
    path = tickets.TICKETS_DIR / (item["slug"] + ".md")
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert tickets.create_ticket("Retry", source_key="workflow:fake")["deduped"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert tickets.list_tickets() == []
    assert tickets.list_tickets(status="done")[0]["slug"] == item["slug"]
    assert tickets.close_ticket(item["slug"], status="dropped")["status"] == "dropped"


def test_ticket_rest_and_isolation(store, tmp_path, monkeypatch):
    tickets, todos = store
    from g_pulse import pulse
    from outlets import remind
    app = FastAPI()
    app.include_router(todos.create_router(), prefix="/api")
    app.include_router(tickets.create_tickets_router(), prefix="/api")
    client = TestClient(app)
    payload = {"title": "Refactor sample auth", "source_key": "ticket:fake", "project": "sample"}
    item = client.post("/api/tickets", json=payload).json()
    assert client.post("/api/tickets", json=payload).json()["deduped"]
    assert item["provenance"]["requester"] == "unknown"
    assert len(client.get("/api/tickets?project=sample").json()) == 1
    assert client.get("/api/todos").json() == []
    assert client.get("/api/todos/calendar").json() == []
    assert todos.list_todos(status="all") == []
    monkeypatch.setattr(remind, "STATE", str(tmp_path / "reminders.json"))
    monkeypatch.setattr(remind.alerts, "send", lambda *a, **k: pytest.fail("ticket reached reminders"))
    assert remind.run() == 0
    assert pulse._todos_soon() == []
    assert item["title"] not in pulse.composed_payload()
    assert client.patch("/api/tickets/" + item["slug"], json={"status": "done"}).json()["status"] == "done"
    assert client.patch("/api/tickets/missing", json={}).status_code == 404
    assert client.patch("/api/tickets/" + item["slug"], json={"status": "open"}).status_code == 400
    assert client.get("/api/tickets?status=invalid").status_code == 400


def test_ticket_mcp_registration(store):
    tickets, todos = store
    tools = asyncio.run(todos.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert {"create_ticket", "list_tickets", "close_ticket"} <= names


def test_ticket_routes_are_mounted_by_create_api(store):
    from g_brain.api import create_api
    _, todos = store
    app = FastAPI()
    app.mount("/api", create_api())
    with TestClient(app) as client:
        assert client.get("/api/tickets").json() == []
        response = client.post("/api/tickets", json={"title": "Repair fixture workflow", "project": "fixture"})
        assert response.status_code == 200
        item = response.json()
        assert client.get("/api/tickets?project=fixture").json() == [item]
        closed = client.patch("/api/tickets/" + item["slug"], json={"status": "done"})
        assert closed.status_code == 200 and closed.json()["status"] == "done"
        assert client.get("/api/tickets").json() == []
        assert client.get("/api/tickets?status=done").json() == [closed.json()]
        assert client.get("/api/todos").json() == []
    assert todos.list_todos(status="all") == []


def test_ticket_tools_reach_the_host_mcp(store, monkeypatch):
    from fastmcp import Client, FastMCP
    from g_brain import surface
    monkeypatch.setattr(surface, "proxy_basic_memory", lambda target: FastMCP("fixture-memory"))
    from g_brain import server
    _, todos = store
    assert server.todos_mcp is todos.mcp

    async def exercise():
        async with Client(server.mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"create_ticket", "list_tickets", "close_ticket"} <= names
            result = await client.call_tool("create_ticket", {"title": "Repair fixture transport"})
            assert result.data["kind"] == "ticket"
            listed = await client.call_tool("list_tickets", {})
            assert listed.data[0]["slug"] == result.data["slug"]
            closed = await client.call_tool("close_ticket", {"slug": result.data["slug"]})
            assert closed.data["status"] == "done"
    asyncio.run(exercise())


def test_ticket_validation(store):
    tickets, todos = store
    with pytest.raises(ValueError):
        tickets.create_ticket(" ")
    with pytest.raises(ValueError):
        tickets.create_ticket("Example", body="a" * 801)
    with pytest.raises(ValueError):
        tickets.close_ticket("../outside")
