# 目標達成 第3段階-B 変更履歴（2026-09-26）

## 証拠グラフ（読み取り専用）

車両×月×費目のセルから、入力明細・原本位置・配賦適用元を遅延生成する。グラフ全体の事前構築・キャッシュ・永続化はしない。`trace` が呼ばれたときだけ `input.json` から該当明細を集め、その場でノードと辺を返す。

### ノード種別

| kind | 意味 |
|---|---|
| `cell` | 車両×月×費目の金額 |
| `record` | `input.json` の明細1件 |
| `source` | 原本の文脈ファイル + `locator`（ページ/シート/セル/請求番号などをそのまま保持） |
| `allocation` | `allocations` / `allocation_reused` / 適用元IDがある明細だけ |

### status の判定規則

| status | 条件 |
|---|---|
| `traced` | 該当明細がすべて `source_ref` と `source_locator` を持ち、参照先の原本が現存し、原本ハッシュが入力の `source_hash` と矛盾しない |
| `untraceable` | 明細はあるが参照または位置が欠ける、原本が現存しない、あるいは `source_hash` 自体が無い。欠けた明細IDと理由を `issues` に列挙。推測で補完しない |
| `stale` | 原本が入力作成後に変わっている（ハッシュ不一致）。`traced` にしない |
| `no_data` | 該当明細が無い。`amount` は `None`（0円と区別する） |
| `not_applicable` | 車両損益以外。サンプリング対象のため未実装。偽の `traced` は作らない |

`untraceable` は `stale` より優先する。0円の明細があるセルは `amount=0` で追跡できれば `traced`。

### 遅延生成

- `trace(manager, project_id, vehicle_id, month, category)` は対象セルだけを組み立てる。
- `summary` は `input.json` を1回読み、セルごとの件数と `untraceable`/`stale` 一覧（上限付き）だけを返す。ノード展開はしない。
- DB・入力・成果物へは書き込まない。承認・確定・`achieved` の経路は作らない。

### Gate との関係

`check_c12_trace` と `completion_gate` の判定は変えない。証拠グラフは診断用であり、`traced` を PASS の代わりにしない。`summary` の `untraceable`/`stale` は C12 未達の説明情報。

### API（追加のみ）

- `GET /api/projects/{project_id}/evidence/trace?vehicle_id=&month=&category=`
- `GET /api/projects/{project_id}/evidence/summary`
- 不正引数は 4xx。`not_applicable` は 200。出力はデータのみ（HTML として扱わない）。

## 試験

- `tests/test_evidence_graph.py`: EG-01〜EG-08
- 実測結果は作業完了報告に記載する。
