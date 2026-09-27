from __future__ import annotations

MEMO_ROOT = "__project_memory__"


def _value(text: str, fallback: str = "未設定") -> str:
    return text.strip() if text and text.strip() else fallback


def write_project_memos(memory, project_id: str) -> None:
    project = memory.get_project(project_id)
    mission = memory.get_mission(project_id, event_limit=500)
    if not project or not mission.get("created_at"):
        return

    goal = (
        f"# {project['name']} — プロジェクト目標\n\n"
        f"- 状態: {mission['status']}\n"
        f"- 計画版: {mission['plan_version']}\n"
        f"- 更新日時: {mission['updated_at']}\n"
        f"- 外部AI調査・計画評価: {'明示許可あり' if mission['allow_external_ai'] else '使用しない'}\n"
        f"- Workspace: /workspace/{project.get('workspace_path') or f'projects/{project_id}'}\n\n"
        f"## 目標\n\n{_value(mission['goal'])}\n\n"
        f"## 達成条件\n\n{_value(mission['success_criteria'])}\n\n"
        f"## 制約・注意事項\n\n{_value(mission['constraints_text'])}\n"
    )
    memory.upsert_context_memo(project_id, f"{MEMO_ROOT}/00_goal.md", goal)

    if mission["tasks"]:
        task_sections = []
        for task in mission["tasks"]:
            task_sections.append(
                f"## 手順 {task['position']}: {task['title']}\n\n"
                f"- 状態: {task['status']}\n"
                f"- タスクキー: {task.get('task_key') or f'task_{task['position']}'}\n"
                f"- 依存: {', '.join(task.get('depends_on', [])) or 'なし'}\n"
                f"- 実行方式: {task['mode']}\n"
                f"- 担当: {task.get('agent_label') or '未割当'}\n"
                f"- 試行回数: {task['attempts']}\n\n"
                f"### 実施内容\n\n{_value(task['description'])}\n\n"
                f"### 完了判定\n\n{_value(task['acceptance_criteria'])}"
            )
        plan = (
            f"# {project['name']} — 実行計画・実施手順\n\n"
            f"- 状態: {mission['status']}\n"
            f"- 計画版: {mission['plan_version']}\n"
            f"- 進捗: {mission['progress']['completed']}/{mission['progress']['total']} "
            f"({mission['progress']['percent']}%)\n"
            f"- 最大同時実行: {mission.get('max_parallel_tasks', 2)}\n"
            f"- 更新日時: {mission['updated_at']}\n\n"
            f"## 計画概要\n\n{_value(mission['plan_summary'])}\n\n"
            + "\n\n".join(task_sections)
            + "\n"
        )
        memory.upsert_context_memo(project_id, f"{MEMO_ROOT}/10_plan_and_procedure.md", plan)
    else:
        memory.delete_context_memo(project_id, f"{MEMO_ROOT}/10_plan_and_procedure.md")

    if mission.get("plan_reviews"):
        review_sections = []
        for review in mission["plan_reviews"]:
            status = "成功" if review.get("ok") else "失敗"
            body = review.get("review") or review.get("error") or "回答なし"
            review_sections.append(
                f"## {review.get('label') or review.get('id') or '評価AI'} — {status}\n\n"
                f"- モデル: {review.get('model') or '不明'}\n\n{body[:16000]}"
            )
        reviews_memo = (
            f"# {project['name']} — 複数AI計画評価\n\n"
            "外部AIは草案を評価し、最終計画の決定と統合はローカル統制LLMが行います。\n\n"
            + "\n\n".join(review_sections) + "\n"
        )
        memory.upsert_context_memo(project_id, f"{MEMO_ROOT}/05_plan_reviews.md", reviews_memo)
    else:
        memory.delete_context_memo(project_id, f"{MEMO_ROOT}/05_plan_reviews.md")
    if mission["events"] or mission["tasks"]:
        event_lines = [
            f"- {event['created_at']} | {event['kind']} | {event['message']}"
            + (f"\n  - 詳細: {event['detail'][:2000]}" if event["detail"] else "")
            for event in reversed(mission["events"])
        ]
        result_sections = []
        for task in mission["tasks"]:
            if task["result"] or task["error"]:
                result_sections.append(
                    f"## {task['position']}. {task['title']} — {task['status']}\n\n"
                    f"{(task['result'] or task['error'])[:10000]}"
                )
        execution = (
            f"# {project['name']} — 実行記録・中途報告\n\n"
            f"- 現在状態: {mission['status']}\n"
            f"- 進捗: {mission['progress']['completed']}/{mission['progress']['total']} "
            f"({mission['progress']['percent']}%)\n"
            f"- 最大同時実行: {mission.get('max_parallel_tasks', 2)}\n"
            f"- 更新日時: {mission['updated_at']}\n\n"
            f"## 実行イベント\n\n{chr(10).join(event_lines) or '- 記録なし'}\n\n"
            f"## タスク成果\n\n{chr(10).join(result_sections) or '成果はまだありません。'}\n"
        )
        memory.upsert_context_memo(project_id, f"{MEMO_ROOT}/20_execution_log.md", execution)
    else:
        memory.delete_context_memo(project_id, f"{MEMO_ROOT}/20_execution_log.md")

    if mission["final_report"]:
        report_label = "未解決事項レポート" if mission["status"] == "failed" else "最終報告"
        final_report = (
            f"# {project['name']} — {report_label}\n\n"
            f"- 状態: {mission['status']}\n"
            f"- 更新日時: {mission['updated_at']}\n\n"
            f"{mission['final_report']}\n"
        )
        memory.upsert_context_memo(project_id, f"{MEMO_ROOT}/90_final_report.md", final_report)
    else:
        memory.delete_context_memo(project_id, f"{MEMO_ROOT}/90_final_report.md")
