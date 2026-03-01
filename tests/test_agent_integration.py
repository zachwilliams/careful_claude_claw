import pytest

from careful_claude_claw.agent import run_agent
from careful_claude_claw.models import JobStatus


@pytest.mark.integration
async def test_agent_reads_claude_md(tmp_path):
    """Verify the agent reads and follows CLAUDE.md instructions in its workspace."""
    claude_md = (
        "# Test Workspace\n"
        'When responding to any task, you MUST include the exact phrase "CLAW_VERIFIED" '
        "somewhere in your response.\n"
    )

    job = await run_agent(
        agent_name="integration-test",
        task="Say hello and confirm you read the workspace instructions.",
        cwd=str(tmp_path),
        claude_md=claude_md,
        max_attempts=1,
    )

    assert job.status == JobStatus.SUCCESS, f"Agent failed: {job.error}"
    assert "CLAW_VERIFIED" in job.output, (
        f"Agent did not follow CLAUDE.md instructions. Output: {job.output}"
    )
