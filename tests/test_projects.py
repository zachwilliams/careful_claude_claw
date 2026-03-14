import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.models import Project, ProjectStatus


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


def test_insert_and_list_projects():
    p = Project(name="myproj", path="/tmp/myproj", description="Test project")
    db_module.insert_project(p)

    rows = db_module.list_projects()
    assert len(rows) == 1
    assert rows[0]["name"] == "myproj"
    assert rows[0]["path"] == "/tmp/myproj"
    assert rows[0]["status"] == "active"
    assert rows[0]["description"] == "Test project"


def test_get_project():
    p = Project(name="proj1", path="/tmp/proj1")
    db_module.insert_project(p)

    result = db_module.get_project("proj1")
    assert result is not None
    assert result["name"] == "proj1"

    assert db_module.get_project("nonexistent") is None


def test_update_project():
    p = Project(name="proj1", path="/tmp/proj1", description="old")
    db_module.insert_project(p)

    p.description = "new description"
    p.status = ProjectStatus.PAUSED
    db_module.update_project(p)

    result = db_module.get_project("proj1")
    assert result["description"] == "new description"
    assert result["status"] == "paused"


def test_list_projects_empty():
    assert db_module.list_projects() == []


def test_project_defaults():
    p = Project(name="test", path="/tmp/test")
    assert p.status == ProjectStatus.ACTIVE
    assert p.description == ""
    assert p.created_at is not None
