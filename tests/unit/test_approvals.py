from cuckoo.codex.approvals import (
    APPROVE_ALL,
    DENY_ALL,
    RuleBasedApprovals,
    chain,
    evaluate,
)
from cuckoo.codex.protocol.common import Decision
from cuckoo.codex.protocol.server_requests import (
    ApprovalRequest,
    CommandApproval,
    FileChangeApproval,
)


def command(text: str) -> CommandApproval:
    return CommandApproval(thread_id="t1", command=text)


async def test_builtin_handlers():
    assert await evaluate(APPROVE_ALL, command("ls")) is Decision.APPROVED
    assert await evaluate(DENY_ALL, command("ls")) is Decision.DENIED
    assert await evaluate(None, command("ls")) is None


async def test_chain_first_decision_wins():
    async def abstain(request):
        return None

    handler = chain(abstain, lambda r: Decision.APPROVED, DENY_ALL)
    assert await evaluate(handler, command("ls")) is Decision.APPROVED

    all_abstain = chain(abstain, lambda r: None)
    assert await evaluate(all_abstain, command("ls")) is None


async def test_rule_based_commands():
    rules = RuleBasedApprovals(
        allow_commands=[r"^git (status|diff)", r"^ls\b"],
        deny_commands=[r"rm\s+-rf"],
        default=None,
    )
    assert rules(command("git status")) is Decision.APPROVED
    assert rules(command("ls -la")) is Decision.APPROVED
    assert rules(command("rm -rf /")) is Decision.DENIED
    assert rules(command("cargo build")) is None  # abstains -> falls through


async def test_rule_based_deny_wins_over_allow():
    rules = RuleBasedApprovals(
        allow_commands=[r".*"], deny_commands=[r"curl"], default=Decision.DENIED
    )
    assert rules(command("curl http://x")) is Decision.DENIED


async def test_rule_based_legacy_command_list():
    rules = RuleBasedApprovals(allow_commands=[r"^git push"])
    request = CommandApproval(thread_id="t1", command=["git", "push", "origin"])
    assert rules(request) is Decision.APPROVED


async def test_rule_based_write_paths():
    rules = RuleBasedApprovals(
        allow_write_paths=["/repo/src"], default=Decision.DENIED
    )
    inside = FileChangeApproval(
        thread_id="t1", changes={"/repo/src/a.py": {"kind": "update"}}
    )
    outside = FileChangeApproval(
        thread_id="t1",
        changes={"/repo/src/a.py": {"kind": "update"}, "/etc/passwd": {"kind": "update"}},
    )
    item_generation = FileChangeApproval(
        thread_id="t1", changes=[{"path": "/repo/src/b.py", "kind": "add"}]
    )
    assert rules(inside) is Decision.APPROVED
    assert rules(outside) is Decision.DENIED
    assert rules(item_generation) is Decision.APPROVED


async def test_rule_based_unknown_request_gets_default():
    rules = RuleBasedApprovals(default=Decision.DENIED)
    assert rules(ApprovalRequest(thread_id="t1")) is Decision.DENIED
