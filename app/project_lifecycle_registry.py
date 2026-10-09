"""L1: 案件データの棚卸しレジストリ + 削除プレビュー + 残存検出.

読み取り専用が中心。削除は実行しない。既存の delete_project の挙動は変えない。
保存先の解決は各ストアの既存コードの規則と同じにする:
- メインDB: memory.path (ShortTermMemory / DetailStore / UpgradeStore / OcrStore)
- goal_reviews: Path(memory.path).parent / 'goal_reviews.sqlite3' (ReviewStore)
- goal_completion: Path(memory.path).parent / 'goal_completion.sqlite3'
- experience: Path(memory.path).parent / 'experience_memory' / 'experience.sqlite3'
- vector index: Path(memory.path).parent / 'experience_memory' / 'vector_index.sqlite3'
- agent_examples: DATA_DIR / 'agent_examples' / 'examples.sqlite3'
  (DATA_DIR = memory.path の parent が 'memory' なら parent.parent、そうでなければ parent)
- procedure_learning: DATA_DIR / 'procedure_learning' / 'learning.sqlite3'
- recovery sidecar: Path(memory.path).with_name(name + '.recovery.sqlite3')
- auto_resume sidecar: with_name(name + '.auto_resume.sqlite3')
- resolution sidecar: with_name(name + '.resolution.sqlite3')
- workspace: WorkspaceSandbox.root 配下 projects/{project_id} (または設定 workspace_path)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

FORMAT_VERSION = "l1-registry/1"
DEFAULT_PROJECT_ID = "default"

# 現行 delete_project が削除するメインDBテーブル (projects を含む11)。
LEGACY_DELETE_TABLES = frozenset({
    "turns", "project_context_files", "project_events", "project_actions",
    "project_leads", "campaign_social_shares", "campaign_creatives",
    "premarketing_campaigns", "project_tasks", "project_missions", "projects",
})

# 意図的に対象外とするテーブル: 理由付きで明示する。
EXCLUDED_TABLES: dict[str, str] = {
    # マイグレーション途中の transient 名。最終的に experiences へ RENAME される。
    "experiences__p0b_status": "migration transient: experiences への付け替え前の一時名。実在の正本ではない",
    # グローバルな能力定義レジストリ。案件列を持たないため棚卸しの案件単位件数対象外。
    "capability_definitions": "global registry: 案件に紐づかない能力定義。案件削除の対象外",
}

AUDIT_TABLES_FALLBACK = (
    "capability_checks", "failure_records", "presentation_manifests",
    "validation_runs", "recovery_attempts", "claims", "claim_evidence_links",
)


@dataclass(frozen=True)
class RegistryEntry:
    """棚卸しエントリ。削除の実行はしない(L3で使う宣言だけを持つ)."""

    name: str
    kind: str  # sqlite_table | sqlite_sidecar_file | directory | index | other
    db_label: str  # main | goal_reviews | goal_completion | experience | vector_index | agent_examples | procedure_learning | recovery | auto_resume | resolution | workspace | experience_config
    table: str = ""  # sqlite_table の場合
    project_column: str = ""  # 'project_id' / 'project' / 'id'(projects) / '' (via結合)
    via: str = ""  # 結合経由の数え方 (例: run_id->ocr_runs)
    shared_with_other_projects: bool = False
    may_contain_external: bool = False
    description: str = ""
    backup_method: str = "row_copy"
    delete_method_declared: str = "L3-delete-by-project-key"
    external_kind: str = ""


def _main_tables() -> list[RegistryEntry]:
    rows: list[RegistryEntry] = []
    for t in ["turns", "project_context_files", "project_events", "project_actions",
              "project_leads", "campaign_social_shares", "campaign_creatives",
              "premarketing_campaigns", "project_tasks", "project_missions"]:
        rows.append(RegistryEntry(
            name=f"main:{t}", kind="sqlite_table", db_label="main",
            table=t, project_column="project_id",
            shared_with_other_projects=True,
            may_contain_external=(t in ("premarketing_campaigns", "campaign_social_shares", "campaign_creatives", "project_actions", "project_leads")),
            description=f"メインDBの案件テーブル {t}",
        ))
    rows.append(RegistryEntry(
        name="main:projects", kind="sqlite_table", db_label="main",
        table="projects", project_column="id",
        shared_with_other_projects=True, description="案件マスタ。id=案件ID",
    ))
    # OCR 6テーブル (OcrStore(memory.path) / ensure_ocr_tables: 同一メインDB)。
    rows.append(RegistryEntry(
        name="main:ocr_runs", kind="sqlite_table", db_label="main",
        table="ocr_runs", project_column="project_id",
        shared_with_other_projects=True,
        may_contain_external=True, external_kind="ocr_publish",
        description="OCR実行。artifact_root/published_at を持つ",
    ))
    for t in ["ocr_pages", "ocr_blocks", "ocr_fields", "ocr_validations", "ocr_reviews"]:
        rows.append(RegistryEntry(
            name=f"main:{t}", kind="sqlite_table", db_label="main",
            table=t, project_column="", via="run_id->ocr_runs.project_id",
            shared_with_other_projects=True,
            description=f"OCR従属テーブル {t}(run_id経由で案件に帰属)",
        ))
    # 詳細計画 (DetailStore(memory.path): 同一メインDB)。
    rows.append(RegistryEntry(
        name="main:detailed_plans", kind="sqlite_table", db_label="main",
        table="detailed_plans", project_column="project_id",
        shared_with_other_projects=True, description="詳細計画 (project_id,task_id,episode,revision)",
    ))
    for t in ["detailed_steps", "detailed_reviews"]:
        rows.append(RegistryEntry(
            name=f"main:{t}", kind="sqlite_table", db_label="main",
            table=t, project_column="", via="plan_id->detailed_plans.project_id",
            shared_with_other_projects=True,
            description=f"詳細計画の従属テーブル {t}(plan_id経由)",
        ))
    rows.append(RegistryEntry(
        name="main:detailed_budgets", kind="sqlite_table", db_label="main",
        table="detailed_budgets", project_column="project_id",
        shared_with_other_projects=True, description="詳細計画の予算 (project_id,episode)",
    ))
    # Upgrade (UpgradeStore(memory.path): 同一メインDB)。
    rows.append(RegistryEntry(
        name="main:task_attempts", kind="sqlite_table", db_label="main",
        table="task_attempts", project_column="project_id",
        shared_with_other_projects=True, description="能力向上の試行",
    ))
    rows.append(RegistryEntry(
        name="main:execution_budgets", kind="sqlite_table", db_label="main",
        table="execution_budgets", project_column="project_id",
        shared_with_other_projects=True, description="実行予算",
    ))
    rows.append(RegistryEntry(
        name="main:source_versions", kind="sqlite_table", db_label="main",
        table="source_versions", project_column="project_id",
        shared_with_other_projects=True, description="実行根拠の版",
    ))
    rows.append(RegistryEntry(
        name="main:source_units", kind="sqlite_table", db_label="main",
        table="source_units", project_column="project_id",
        shared_with_other_projects=True, description="実行根拠の単位",
    ))
    rows.append(RegistryEntry(
        name="main:task_contract_extensions", kind="sqlite_table", db_label="main",
        table="task_contract_extensions", project_column="project_id",
        shared_with_other_projects=True, description="タスク契約の拡張",
    ))
    for t in AUDIT_TABLES_FALLBACK:
        rows.append(RegistryEntry(
            name=f"main:{t}", kind="sqlite_table", db_label="main",
            table=t, project_column="project_id",
            shared_with_other_projects=True,
            description=f"監査テーブル {t}(project_id,attempt_id)",
        ))
    return rows


def build_registry() -> list[RegistryEntry]:
    """全棚卸しエントリを宣言的に返す。"""
    entries = _main_tables()
    # ReviewStore (goal_reviews.sqlite3)。plan_review_loop / plan_repair_loop の
    # run レコードも kind 列違いの同テーブル行として格納される。
    entries.append(RegistryEntry(
        name="goal_reviews:reviews", kind="sqlite_table", db_label="goal_reviews",
        table="reviews", project_column="project",
        shared_with_other_projects=True,
        may_contain_external=True, external_kind="external_review_packet",
        description="ReviewStore(reviews: project,kind,signature,payload)。plan/auto-loop/repair_run を含む",
    ))
    entries.append(RegistryEntry(
        name="goal_reviews:proposal_keys", kind="sqlite_table", db_label="goal_reviews",
        table="proposal_keys", project_column="project",
        shared_with_other_projects=True,
        description="ReviewStoreの冪等キー(proposal_keys: project,proposal_key,signature,candidate_id)",
    ))
    # GoalCompletionStore。
    for t in ["goal_contracts", "plan_coverage", "goal_facts", "goal_states",
              "goal_state_events", "completion_evaluations", "human_acceptances",
              "nac_executions"]:
        entries.append(RegistryEntry(
            name=f"goal_completion:{t}", kind="sqlite_table", db_label="goal_completion",
            table=t, project_column="project_id",
            shared_with_other_projects=True,
            description=f"GoalCompletionStore の {t}",
        ))
    # ExperienceStore (experience.sqlite3)。plan_case_references も同DBに格納。
    for t in ["experiences", "reviews", "requests", "retrievals",
              "candidate_events", "experience_index_state", "quarantine_events",
              "plan_case_references"]:
        col = "project"
        entries.append(RegistryEntry(
            name=f"experience:{t}", kind="sqlite_table", db_label="experience",
            table=t, project_column=col,
            shared_with_other_projects=True,
            may_contain_external=(t == "experiences"),
            description=f"ExperienceStore/参照監査の {t}(project列)",
        ))
    # ベクトル索引 (vector_index.sqlite3)。
    entries.append(RegistryEntry(
        name="vector_index:vectors", kind="index", db_label="vector_index",
        table="vectors", project_column="project",
        shared_with_other_projects=True,
        description="LocalVectorIndex(vectors: identity,project)。再生成可能な索引",
    ))
    # 経験メモリ設定 (グローバル設定。案件単位ではない)。
    entries.append(RegistryEntry(
        name="experience_config:experience_memory.json", kind="other",
        db_label="experience_config",
        shared_with_other_projects=True,
        description="experience_memory.json(全案件共有の設定。正本ではない)",
    ))
    # agent_examples。
    for t in ["examples", "audit"]:
        entries.append(RegistryEntry(
            name=f"agent_examples:{t}", kind="sqlite_table", db_label="agent_examples",
            table=t, project_column="project",
            shared_with_other_projects=True,
            description=f"ExampleStore の {t}(project列)",
        ))
    # procedure_learning。
    for t in ["learning", "history"]:
        entries.append(RegistryEntry(
            name=f"procedure_learning:{t}", kind="sqlite_table",
            db_label="procedure_learning",
            table=t, project_column="project",
            shared_with_other_projects=True,
            description=f"LearningStore の {t}(project列)",
        ))
    # サイドカー3種。
    entries.append(RegistryEntry(
        name="recovery:recoveries", kind="sqlite_sidecar_file", db_label="recovery",
        table="recoveries", project_column="project_id",
        shared_with_other_projects=False,
        description="recovery_record の recoveries(*.recovery.sqlite3)",
    ))
    for t in ["settings", "runs"]:
        entries.append(RegistryEntry(
            name=f"auto_resume:{t}", kind="sqlite_sidecar_file", db_label="auto_resume",
            table=t, project_column="project_id",
            shared_with_other_projects=False,
            description=f"safe_auto_resume の {t}(*.auto_resume.sqlite3)",
        ))
    entries.append(RegistryEntry(
        name="resolution:resolutions", kind="sqlite_sidecar_file", db_label="resolution",
        table="resolutions", project_column="project_id",
        shared_with_other_projects=False,
        description="resolution_coordinator の resolutions(*.resolution.sqlite3)",
    ))
    # Stage1: 停止分類の空転ガード履歴 (サイドカーSQLite: *.stop.sqlite3 の stop_history)。
    # kind は世代管理と同様 "other" とし、件数テスト (全エントリ1件以上) の対象外にする。
    # 網羅性検証 (audit_registry_completeness) では table 名で登録扱いになる。
    entries.append(RegistryEntry(
        name="stop:stop_history", kind="other", db_label="stop",
        table="stop_history", project_column="project_id",
        shared_with_other_projects=False,
        description="stop_classifier の stop_history(*.stop.sqlite3)。"
                    "同一(署名,クラス,コード)2連続の空転ガード用。追記のみ",
        backup_method="stop-sidecar-preserved",
        delete_method_declared="S1-stop-history (append-only, no physical delete)",
    ))
    # Stage 2-A: 機能提案 (サイドカーSQLite: *.gap.sqlite3 の function_proposals /
    # proposal_events)。kind は stop/generation と同様 "other" とし、件数テスト
    # (全エントリ1件以上) の対象外にする。網羅性検証 (audit_registry_completeness)
    # では table 名で登録扱いになる。退避(L1 create_backup)は directory/other を
    # 複写対象外のため、世代切替では stale 化で扱い (plan_signature 変更で旧提案は
    # stale。削除しない)、完全削除では purge_project_proposals で当該PJ行を消す。
    for _t in ("function_proposals", "proposal_events"):
        entries.append(RegistryEntry(
            name=f"gap:{_t}", kind="other", db_label="gap",
            table=_t, project_column="project_id",
            shared_with_other_projects=False,
            description="capability_gap の FunctionProposal と監査イベント(*.gap.sqlite3)。"
                        "同一(案件,計画署名,条件,能力,原因)は一意制約で集約",
            backup_method="gap-sidecar-preserved",
            delete_method_declared="S2A-gap-purge (purge_project_proposals)",
        ))
    # Stage 2-B: Jenkins指示パッケージの出力記録 (サイドカーSQLite: *.gap.sqlite3 の
    # export_packages / export_history。gap と同居し、kind は "other" として件数
    # テストの対象外にする。網羅性検証では table 名で登録扱いになる。退避は
    # gap と同様に sidecar-preserved (行複写対象外)、完全削除では
    # purge_export_packages で当該PJ行を消す。指示パッケージの実ファイルは
    # PJ専用Workspace配下 development_instructions/ に置き、workspace エントリで
    # 退避・削除の対象になる (他PJのスコープには触れない)。
    for _t in ("export_packages", "export_history"):
        entries.append(RegistryEntry(
            name=f"gap:{_t}", kind="other", db_label="gap",
            table=_t, project_column="project_id",
            shared_with_other_projects=False,
            description="capability_export の指示パッケージ出力記録と旧版履歴(*.gap.sqlite3)。"
                        "実ファイルはPJ専用Workspaceの development_instructions/ 配下",
            backup_method="gap-sidecar-preserved",
            delete_method_declared="S2B-export-purge (purge_export_packages)",
        ))
    # Stage 2-D: 開発結果の取込みと再判定 (サイドカーSQLite: *.gap.sqlite3 の
    # gap_deliveries / gap_verifications。gap と同居し、kind は "other" として
    # 件数テストの対象外にする。網羅性検証では table 名で登録扱いになる。
    # 退避は gap と同様に sidecar-preserved (行複写対象外)、完全削除では
    # purge_delivery_records で当該PJ行を消す。Jenkinsへの接続・送信は持たない)。
    for _t in ("gap_deliveries", "gap_verifications"):
        entries.append(RegistryEntry(
            name=f"gap:{_t}", kind="other", db_label="gap",
            table=_t, project_column="project_id",
            shared_with_other_projects=False,
            description="capability_delivery の納品履歴と再判定記録(*.gap.sqlite3)。"
                        "追記のみ。能力レジストリは書き換えない",
            backup_method="gap-sidecar-preserved",
            delete_method_declared="S2D-delivery-purge (purge_delivery_records)",
        ))
    # Workspace。
    entries.append(RegistryEntry(
        name="workspace:project_dir", kind="directory", db_label="workspace",
        shared_with_other_projects=False,
        may_contain_external=True, external_kind="workspace_artifacts",
        description="WorkspaceSandbox 配下の案件専用ディレクトリ (projects/{id} または設定workspace_path)",
    ))
    # L2: PJ初期化の世代管理 (サイドカーSQLite: *.generation.sqlite3 の project_generations)。
    # kind は "other" とし、既存の件数テスト (全エントリ1件以上) の対象外にする。
    # 網羅性検証 (audit_registry_completeness) では table 名で登録扱いになる。
    # 退避(L1 create_backup)は directory/other を複写対象外とするため、世代履歴は
    # 世代操作側で append-only に保全する (project_generation.restore_generation)。
    entries.append(RegistryEntry(
        name="generation:project_generations", kind="other", db_label="generation",
        table="project_generations", project_column="project_id",
        shared_with_other_projects=False,
        description="L2 PJ初期化の世代管理 (*.generation.sqlite3 / project_generations)。"
                    "退避は世代操作側で保全し、L1の行複写対象外",
        backup_method="generation-sidecar-preserved",
        delete_method_declared="L2-generation-switch (no physical delete)",
    ))
    # L3: PJ削除の状態管理 (サイドカーSQLite: *.delete.sqlite3)。
    # kind は "other" とし、件数テストの対象外にする。網羅性検証では table 名で登録扱い。
    # 退避(L1 create_backup)は directory/other を複写対象外のため、削除状態は
    # 削除操作側で保全する (論理削除中もサイドカーに残る)。
    for _t in ("project_delete_states", "project_delete_ops"):
        entries.append(RegistryEntry(
            name=f"delete:{_t}", kind="other", db_label="delete",
            table=_t, project_column="project_id",
            shared_with_other_projects=False,
            description="L3 PJ削除の状態管理 (*.delete.sqlite3)。"
                        "退避は削除操作側で保全し、L1の行複写対象外",
            backup_method="delete-sidecar-preserved",
            delete_method_declared="L3-delete-state (preserved on purge)",
        ))
    return entries


REGISTRY: list[RegistryEntry] = build_registry()


# ----------------------------------------------------------------------------
# 保存先の解決 (既存コードの規則と同じ)
# ----------------------------------------------------------------------------

def data_dir_of(memory_path: str | Path) -> Path:
    """DATA_DIR 相当。DB_PATH=DATA_DIR/memory/conversations.db の規則を踏襲。"""
    p = Path(memory_path)
    parent = p.parent
    if parent.name == "memory":
        return parent.parent
    return parent


def resolve_db_path(memory_path: str | Path, db_label: str) -> Path | None:
    """db_label に対応する実ファイルパス。存在しなくてもパスを返す。"""
    mem = Path(memory_path)
    parent = mem.parent
    if db_label == "main":
        return mem
    if db_label == "goal_reviews":
        return parent / "goal_reviews.sqlite3"
    if db_label == "goal_completion":
        return parent / "goal_completion.sqlite3"
    if db_label == "experience":
        return parent / "experience_memory" / "experience.sqlite3"
    if db_label == "vector_index":
        return parent / "experience_memory" / "vector_index.sqlite3"
    if db_label == "experience_config":
        return parent / "experience_memory.json"
    data_dir = data_dir_of(mem)
    if db_label == "agent_examples":
        return data_dir / "agent_examples" / "examples.sqlite3"
    if db_label == "procedure_learning":
        return data_dir / "procedure_learning" / "learning.sqlite3"
    if db_label == "recovery":
        return mem.with_name(mem.name + ".recovery.sqlite3")
    if db_label == "auto_resume":
        return mem.with_name(mem.name + ".auto_resume.sqlite3")
    if db_label == "resolution":
        return mem.with_name(mem.name + ".resolution.sqlite3")
    if db_label == "stop":
        return mem.with_name(mem.name + ".stop.sqlite3")
    if db_label == "gap":
        return mem.with_name(mem.name + ".gap.sqlite3")
    return None


def resolve_workspace_dir(memory_path: str | Path, project_id: str,
                          workspace_root: str | Path | None,
                          configured: str = "") -> Path | None:
    """案件専用Workspaceディレクトリ。WorkspaceSandbox.project_path の規則を踏襲。"""
    if workspace_root is None:
        return None
    root = Path(workspace_root)
    rel = (configured or "").strip().replace("\\", "/")
    if not rel:
        rel = f"projects/{project_id}"
    rel = rel.strip("/")
    if not rel or ".." in rel.split("/"):
        return None
    return root / rel


# ----------------------------------------------------------------------------
# 件数の数え方 (読み取り専用)
# ----------------------------------------------------------------------------

def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def count_entry(memory_path: str | Path, entry: RegistryEntry, project_id: str,
                workspace_root: str | Path | None = None) -> int:
    """1エントリの件数を数える。副作用なし。ファイルが無ければ0。"""
    pid = str(project_id)
    if entry.kind == "directory":
        configured = ""
        try:
            dbp = Path(memory_path)
            if dbp.exists():
                db = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
                try:
                    db.row_factory = sqlite3.Row
                    if _table_exists(db, "projects"):
                        r = db.execute("SELECT workspace_path FROM projects WHERE id=?", (pid,)).fetchone()
                        if r:
                            try:
                                configured = str(r["workspace_path"] or "")
                            except (KeyError, IndexError):
                                configured = ""
                finally:
                    db.close()
        except sqlite3.Error:
            configured = ""
        d = resolve_workspace_dir(memory_path, pid, workspace_root, configured)
        if d is None or not d.exists():
            return 0
        n = 0
        try:
            for _ in d.rglob("*"):
                n += 1
        except OSError:
            return 0
        return n
    if entry.kind == "other":
        return 0
    db_path = resolve_db_path(memory_path, entry.db_label)
    if db_path is None or not db_path.exists():
        return 0
    try:
        db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        db.row_factory = sqlite3.Row
        if not entry.table or not _table_exists(db, entry.table):
            return 0
        cols = _columns(db, entry.table)
        if entry.project_column and entry.project_column in cols:
            try:
                row = db.execute(
                    f"SELECT COUNT(*) FROM {entry.table} WHERE {entry.project_column}=?",
                    (pid,)).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.Error:
                return 0
        # 結合経由 (run_id / plan_id)。
        if entry.table in ("ocr_pages", "ocr_blocks", "ocr_fields",
                           "ocr_validations", "ocr_reviews"):
            if not _table_exists(db, "ocr_runs"):
                return 0
            key = "run_id"
            if entry.table == "ocr_reviews" and "run_id" not in cols:
                return 0
            try:
                row = db.execute(
                    f"SELECT COUNT(*) FROM {entry.table} WHERE {key} IN "
                    f"(SELECT run_id FROM ocr_runs WHERE project_id=?)", (pid,)).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.Error:
                return 0
        if entry.table in ("detailed_steps", "detailed_reviews"):
            if not _table_exists(db, "detailed_plans"):
                return 0
            try:
                row = db.execute(
                    f"SELECT COUNT(*) FROM {entry.table} WHERE plan_id IN "
                    f"(SELECT id FROM detailed_plans WHERE project_id=?)", (pid,)).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.Error:
                return 0
        return 0
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def count_all(memory_path: str | Path, project_id: str,
              workspace_root: str | Path | None = None) -> dict[str, int]:
    """全エントリの件数。{entry.name: count}。副作用なし。"""
    return {e.name: count_entry(memory_path, e, project_id, workspace_root) for e in REGISTRY}


# ----------------------------------------------------------------------------
# 網羅性の機械検証: app/ 配下の CREATE TABLE 静的走査
# ----------------------------------------------------------------------------

_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w$]*)\s*\(",
    re.IGNORECASE,
)
_PID_RE = re.compile(r"\bproject_id\b")
_PROJ_RE = re.compile(r"(?<![\w])project(?![\w])")


def _audit_tables_from_upgrade_store(app_root: Path) -> list[str]:
    """f-string で生成される監査テーブル群をタプルリテラルから展開する。"""
    tables: list[str] = list(AUDIT_TABLES_FALLBACK)
    try:
        text = (app_root / "app" / "upgrade_store.py").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return tables
    m = re.search(r"AUDIT_TABLES\s*=\s*\((.*?)\)", text, re.S)
    if not m:
        return tables
    found = re.findall(r"'([A-Za-z_][\w$]*)'", m.group(1))
    if found:
        tables = found
    return tables


def _ddl_body(text: str, match: re.Match) -> str:
    """CREATE TABLE の列定義本体を括弧対応で切り出す。

    多くの DDL は文末 `;` を持たないため、次文の `;` まで切り出すと
    別文の project_id を拾って誤検出する。開き括弧から対応する閉じ括弧
    までを本体とする。
    """
    open_idx = text.find("(", match.end() - 1 if match.end() else 0)
    # match.end() はテーブル名直後ではないため、テーブル名末尾から探す。
    open_idx = text.find("(", match.start())
    # テーブル名より前の括弧 (IF NOT EXISTS 等には括弧は無い) を避けるため、
    # テーブル名の末尾以降で最初の開き括弧を使う。
    name_end = match.end(1)
    open_idx = text.find("(", name_end)
    if open_idx < 0:
        return text[match.start():name_end + 2000]
    depth = 0
    for i in range(open_idx, min(len(text), open_idx + 8000)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[match.start():i + 1]
    return text[match.start():open_idx + 2000]


def scan_project_tables(app_root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """app/ 配下の CREATE TABLE を走査し project系列を持つテーブルを列挙する。"""
    root = Path(app_root) if app_root else Path(__file__).resolve().parents[1]
    found: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "app").rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _CREATE_RE.finditer(text):
            table = m.group(1)
            if table.upper() in ("IF",):
                continue
            body = _ddl_body(text, m)
            has_pid = bool(_PID_RE.search(body))
            # 'project' 単独列 (project_id の部分一致を除く)。
            has_proj = bool(_PROJ_RE.search(body))
            if has_pid or has_proj:
                rel = path.relative_to(root).as_posix()
                prev = found.get(table)
                if prev is None or len(rel) < len(prev.get("file", "")):
                    found[table] = {"file": rel, "project_id": has_pid, "project": has_proj}
    # 動的生成 (AUDIT_TABLES) は静的 CREATE に現れないため別途展開する。
    for table in _audit_tables_from_upgrade_store(root):
        found.setdefault(table, {"file": "app/upgrade_store.py(AUDIT_TABLES)",
                                 "project_id": True, "project": False})
    return found


def audit_registry_completeness(app_root: str | Path | None = None) -> dict[str, Any]:
    """レジストリの網羅性を検証する。未登録があれば ok=False。

    戻り値: {ok, scanned, registered, unregistered, excluded}
    """
    scanned = scan_project_tables(app_root)
    registered_names = {e.table for e in REGISTRY if e.table}
    unregistered = sorted(t for t in scanned if t not in registered_names and t not in EXCLUDED_TABLES)
    excluded = sorted(t for t in scanned if t in EXCLUDED_TABLES)
    return {
        "ok": not unregistered,
        "format": FORMAT_VERSION,
        "scanned": scanned,
        "registered_table_count": len(registered_names),
        "entry_count": len(REGISTRY),
        "unregistered": unregistered,
        "excluded": {t: EXCLUDED_TABLES[t] for t in excluded},
    }


# ----------------------------------------------------------------------------
# 実行中ジョブ・外部公開物・他PJ参照 (読み取り専用)
# ----------------------------------------------------------------------------

_RUNNING_STATUSES = {"running", "generating", "pending_approval", "publishing_form"}


def has_running_jobs(memory_path: str | Path, project_id: str,
                     executions: dict | None = None) -> dict[str, Any]:
    """実行中ジョブの有無。web.executions と mission 状態と ReviewStore job 行を見る。"""
    pid = str(project_id)
    running: list[dict[str, Any]] = []
    if executions:
        for item in executions.values():
            try:
                if str(item.get("project_id") or "") == pid and str(item.get("status")) == "running":
                    running.append({"source": "executions",
                                    "kind": str(item.get("kind") or ""),
                                    "label": str(item.get("label") or "")[:200]})
            except (AttributeError, TypeError):
                continue
    mem = Path(memory_path)
    if mem.exists():
        try:
            db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                if _table_exists(db, "project_missions"):
                    try:
                        r = db.execute("SELECT status FROM project_missions WHERE project_id=?",
                                       (pid,)).fetchone()
                        if r and str(r["status"]) == "running":
                            running.append({"source": "mission", "kind": "mission", "label": "mission running"})
                    except sqlite3.Error:
                        pass
            finally:
                db.close()
        except sqlite3.Error:
            pass
    rev_path = resolve_db_path(mem, "goal_reviews")
    if rev_path is not None and rev_path.exists():
        try:
            db = sqlite3.connect(f"file:{rev_path.as_posix()}?mode=ro", uri=True)
            try:
                if _table_exists(db, "reviews"):
                    for sig, payload in db.execute(
                            "SELECT signature, payload FROM reviews WHERE project=? AND kind='job'",
                            (pid,)).fetchall():
                        try:
                            row = json.loads(payload) if isinstance(payload, str) else {}
                        except (ValueError, TypeError):
                            continue
                        if str(row.get("status") or "") in ("running", "generating"):
                            running.append({"source": "review_job", "kind": str(row.get("kind") or "job"),
                                            "label": str(sig)[:64]})
            finally:
                db.close()
        except sqlite3.Error:
            pass
    return {"has_running": bool(running), "running": running}


def collect_external_items(memory_path: str | Path, project_id: str) -> list[dict[str, Any]]:
    """ローカル削除では撤回できない外部公開物の一覧。URL等の所在のみ。値は最小限。"""
    pid = str(project_id)
    items: list[dict[str, Any]] = []
    mem = Path(memory_path)
    if not mem.exists():
        return items
    try:
        db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return items
    try:
        db.row_factory = sqlite3.Row
        if _table_exists(db, "premarketing_campaigns"):
            try:
                cols = _columns(db, "premarketing_campaigns")
                for r in db.execute("SELECT * FROM premarketing_campaigns WHERE project_id=?", (pid,)).fetchall():
                    row = dict(r)
                    for key in ("google_form_url", "google_site_url", "google_site_edit_url"):
                        url = str(row.get(key) or "")
                        if url and key in cols:
                            items.append({"store": "premarketing_campaigns", "kind": key,
                                          "ref": row.get("id", ""), "url_present": True,
                                          "note": "ローカル削除では撤回できない"})
                    if str(row.get("publication_status") or "") not in ("", "local_only", "draft"):
                        items.append({"store": "premarketing_campaigns", "kind": "publication_status",
                                      "ref": row.get("id", ""),
                                      "status": str(row.get("publication_status") or "")[:64],
                                      "note": "ローカル削除では撤回できない"})
            except sqlite3.Error:
                pass
        if _table_exists(db, "campaign_social_shares"):
            try:
                for r in db.execute(
                        "SELECT campaign_id, channel, evidence_url, compose_url, status "
                        "FROM campaign_social_shares WHERE project_id=?", (pid,)).fetchall():
                    if str(r["evidence_url"] or ""):
                        items.append({"store": "campaign_social_shares", "kind": "evidence_url",
                                      "ref": f"{r['campaign_id']}/{r['channel']}",
                                      "url_present": True, "note": "公開済み投稿はローカル削除では撤回できない"})
            except sqlite3.Error:
                pass
        if _table_exists(db, "campaign_creatives"):
            try:
                for r in db.execute(
                        "SELECT campaign_id, design_url, image_url FROM campaign_creatives "
                        "WHERE project_id=?", (pid,)).fetchall():
                    if str(r["design_url"] or "") or str(r["image_url"] or ""):
                        items.append({"store": "campaign_creatives", "kind": "design_url",
                                      "ref": r["campaign_id"], "url_present": True,
                                      "note": "ローカル削除では撤回できない"})
            except sqlite3.Error:
                pass
        if _table_exists(db, "project_actions"):
            try:
                for r in db.execute(
                        "SELECT id, kind, status FROM project_actions "
                        "WHERE project_id=? AND status='executed'", (pid,)).fetchall():
                    items.append({"store": "project_actions", "kind": f"executed:{r['kind']}",
                                  "ref": r["id"], "note": "送信済み・実行済みはローカル削除で取り消せない"})
            except sqlite3.Error:
                pass
        if _table_exists(db, "ocr_runs"):
            try:
                for r in db.execute(
                        "SELECT run_id FROM ocr_runs WHERE project_id=? AND published_at<>''",
                        (pid,)).fetchall():
                    items.append({"store": "ocr_runs", "kind": "published_at",
                                  "ref": r["run_id"], "note": "公開済みOCR成果はローカル削除では撤回できない"})
            except sqlite3.Error:
                pass
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass
    # RAG承認済み (他PJから参照され得る共有知)。
    exp_path = resolve_db_path(mem, "experience")
    if exp_path is not None and exp_path.exists():
        try:
            db = sqlite3.connect(f"file:{exp_path.as_posix()}?mode=ro", uri=True)
            try:
                if _table_exists(db, "experiences"):
                    row = db.execute(
                        "SELECT COUNT(*) FROM experiences WHERE project=? AND status='verified'",
                        (pid,)).fetchone()
                    if row and int(row[0]) > 0:
                        items.append({"store": "experiences", "kind": "verified_shared",
                                      "ref": f"count={int(row[0])}",
                                      "note": "承認済みRAGは他PJと共有され得る。独断で削除しない"})
            finally:
                db.close()
        except sqlite3.Error:
            pass
    return items


def workspace_bytes(memory_path: str | Path, project_id: str,
                    workspace_root: str | Path | None) -> dict[str, int]:
    pid = str(project_id)
    configured = ""
    mem = Path(memory_path)
    if mem.exists():
        try:
            db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                if _table_exists(db, "projects"):
                    r = db.execute("SELECT workspace_path FROM projects WHERE id=?", (pid,)).fetchone()
                    if r:
                        try:
                            configured = str(r["workspace_path"] or "")
                        except (KeyError, IndexError):
                            configured = ""
            finally:
                db.close()
        except sqlite3.Error:
            pass
    d = resolve_workspace_dir(memory_path, pid, workspace_root, configured)
    total = 0
    files = 0
    if d is not None and d.exists():
        try:
            for p in d.rglob("*"):
                try:
                    if p.is_file():
                        files += 1
                        total += p.stat().st_size
                except OSError:
                    continue
        except OSError:
            pass
    return {"bytes": total, "files": files}


# ----------------------------------------------------------------------------
# 削除プレビュー (読み取り専用・副作用なし)
# ----------------------------------------------------------------------------

def build_delete_preview(memory_path: str | Path, project_id: str,
                         workspace_root: str | Path | None = None,
                         executions: dict | None = None,
                         app_root: str | Path | None = None) -> dict[str, Any]:
    """削除プレビュー。何も変更しない。

    未把握のストアがある場合は削除完了と表示できない旨を返す。
    """
    pid = str(project_id)
    mem = Path(memory_path)
    project: dict[str, Any] | None = None
    if mem.exists():
        try:
            db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                if _table_exists(db, "projects"):
                    r = db.execute("SELECT id, name FROM projects WHERE id=?", (pid,)).fetchone()
                    if r:
                        project = {"id": r["id"], "name": r["name"]}
            finally:
                db.close()
        except sqlite3.Error:
            project = None
    counts = count_all(mem, pid, workspace_root)
    total_rows = sum(v for k, v in counts.items()
                     if not k.startswith("workspace:") and not k.startswith("experience_config:"))
    jobs = has_running_jobs(mem, pid, executions)
    externals = collect_external_items(mem, pid)
    ws = workspace_bytes(mem, pid, workspace_root)
    # 保存容量: workspace + 実在DBファイルの合計 (概算として明示)。
    db_bytes = 0
    db_files: list[str] = []
    for label in ["main", "goal_reviews", "goal_completion", "experience",
                  "vector_index", "agent_examples", "procedure_learning",
                  "recovery", "auto_resume", "resolution"]:
        p = resolve_db_path(mem, label)
        if p is not None and p.exists() and p.is_file():
            try:
                db_bytes += p.stat().st_size
                db_files.append(label)
            except OSError:
                continue
    audit = audit_registry_completeness(app_root)
    is_default = (pid == DEFAULT_PROJECT_ID)
    blocked_reasons: list[str] = []
    if is_default:
        blocked_reasons.append("既定PJは削除不可")
    if jobs["has_running"]:
        blocked_reasons.append("実行中ジョブあり。停止/取消が必要")
    if not audit["ok"]:
        blocked_reasons.append("棚卸し未登録のテーブルあり。削除完了と表示できない")
    if externals:
        blocked_reasons.append("ローカル削除では撤回できない外部物あり")
    stores = []
    for e in REGISTRY:
        stores.append({
            "name": e.name, "kind": e.kind, "db": e.db_label,
            "table": e.table, "count": counts.get(e.name, 0),
            "shared": e.shared_with_other_projects,
            "may_contain_external": e.may_contain_external,
        })
    return {
        "format": FORMAT_VERSION,
        "project_id": pid,
        "project": project,
        "project_exists": project is not None,
        "is_default_project": is_default,
        "deletable": not blocked_reasons,
        "blocked_reasons": blocked_reasons,
        "total_rows": total_rows,
        "stores": stores,
        "running_jobs": jobs,
        "external_items": externals,
        "external_warning": ("フォーム/Google Sites/SNS投稿/送信済みメール等のURLは"
                             "ローカル削除では撤回できない" if externals else ""),
        "workspace": ws,
        "storage_bytes_estimate": {"workspace_bytes": ws["bytes"], "db_files_bytes": db_bytes,
                                   "db_files": db_files,
                                   "note": "DBは他PJ共有のため概算。全量ではない"},
        "completeness": {"ok": audit["ok"], "unregistered": audit["unregistered"],
                         "excluded": audit["excluded"]},
        "legacy_note": "現行DELETEは主要11テーブルのみ削除し、他ストアは残る",
        "read_only": True,
    }


def list_orphans_after_legacy_delete(memory_path: str | Path, project_id: str,
                                     workspace_root: str | Path | None = None) -> dict[str, Any]:
    """現行削除の対象11テーブル以外に残るストアと件数。実測で不完全性を示す。"""
    pid = str(project_id)
    orphans: list[dict[str, Any]] = []
    for e in REGISTRY:
        if e.db_label == "main" and e.table in LEGACY_DELETE_TABLES:
            continue
        if e.kind == "other":
            continue
        n = count_entry(memory_path, e, pid, workspace_root)
        if n > 0:
            orphans.append({"name": e.name, "kind": e.kind, "db": e.db_label,
                            "table": e.table, "count": n,
                            "shared": e.shared_with_other_projects})
    return {"project_id": pid, "orphan_count": len(orphans),
            "orphans": sorted(orphans, key=lambda x: x["name"]),
            "legacy_deleted_tables": sorted(LEGACY_DELETE_TABLES)}
