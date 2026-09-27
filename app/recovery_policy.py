"""Per-attempt budgets shared with actual Ollama requests through ContextVar."""
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from app.upgrade_store import canonical, digest

CURRENT_ATTEMPT = ContextVar('local_cowork_upgrade_attempt', default=None)


class RecoveryStopped(RuntimeError):
    pass


@dataclass
class RecoveryBudget:
    llm_limit: int = 3
    tool_limit: int = 6
    cycle_limit: int = 2
    recovery_seconds: float = 300
    llm_calls: int = 0
    tool_calls: int = 0
    cycles: int = 0
    started: float | None = None
    fingerprints: set = field(default_factory=set)

    def remaining(self):
        return self.recovery_seconds if self.started is None else max(0, self.recovery_seconds - (time.monotonic() - self.started))

    def consume(self, kind):
        if self.remaining() <= 0:
            raise RecoveryStopped('回復の時間予算に達しました')
        name, limit = ('llm_calls', self.llm_limit) if kind == 'llm' else ('tool_calls', self.tool_limit)
        if getattr(self, name) >= limit:
            raise RecoveryStopped('回復の呼出し予算に達しました: ' + kind)
        setattr(self, name, getattr(self, name) + 1)

    def recover(self, category, identity, method):
        if category in {'authentication_error', 'policy_denied', 'approval_required', 'configuration_missing', 'input_missing', 'unknown'}:
            raise RecoveryStopped('自動回復しない停止理由: ' + category)
        fingerprint = digest(canonical([category, identity, method]))
        if self.cycles >= self.cycle_limit or fingerprint in self.fingerprints:
            raise RecoveryStopped('同じ失敗の反復または回復回数上限')
        self.started = self.started or time.monotonic()
        self.fingerprints.add(fingerprint)
        self.cycles += 1
        return fingerprint


def next_action(category):
    return {'not_presented': 'read_source_units', 'extraction_failed': 'read_source_units',
            'capability_misjudged': 'clarify_capability', 'generation_defect': 'regenerate_sections',
            'connection_error': 'retry_connection'}.get(category, 'stop')

class PersistentRecoveryBudget(RecoveryBudget):
    def __init__(self, store, project_id, key):
        super().__init__()
        self.store, self.project_id, self.key = store, project_id, key

    def consume(self, kind):
        if self.remaining() <= 0:
            raise RecoveryStopped('回復の時間予算に達しました')
        limit = self.llm_limit if kind == 'llm' else self.tool_limit
        used = self.store.consume_execution_budget(self.project_id, self.key, kind, limit)
        setattr(self, 'llm_calls' if kind == 'llm' else 'tool_calls', used)
