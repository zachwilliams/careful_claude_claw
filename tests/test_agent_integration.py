import pytest

import careful_claude_claw.db as db_module
from careful_claude_claw.agent import run_agent
from careful_claude_claw.models import JobStatus


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.mark.integration
async def test_agent_follows_system_prompt(tmp_path):
    """Verify the agent follows injected system prompt instructions."""
    execution = await run_agent(
        agent_name="integration-test",
        task="Say hello.",
        cwd=str(tmp_path),
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": (
                'You MUST include the exact phrase "CLAW_VERIFIED" '
                "in every response. This is mandatory."
            ),
        },
        setting_sources=[],
        max_attempts=1,
    )

    assert execution.status == JobStatus.SUCCESS, f"Agent failed: {execution.error}"
    assert "CLAW_VERIFIED" in execution.output, (
        f"Agent did not follow system prompt instructions. Output: {execution.output}"
    )
