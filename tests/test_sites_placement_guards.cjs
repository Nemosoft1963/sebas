const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../docker/google-sites-extension/content.js"), "utf8");
const section = source.slice(source.indexOf("  const visible ="), source.indexOf("  async function waitFor"));
let markers = [];
let frames = [];
const doc = {querySelectorAll: s => s === "iframe" ? frames : markers};
const marker = (version, dialog = false) => ({
  hidden: false, getClientRects: () => [1],
  getAttribute: () => version, closest: () => dialog ? {} : null
});
class FakeInput {}
class FakeTextarea {}
const factory = new Function(
  "document", "getComputedStyle", "panel", "location", "status", "window",
  "HTMLInputElement", "HTMLTextAreaElement",
  section + ";return {placed,enabled,unique,textEditable};"
);
const guards = factory(
  doc, () => ({visibility:"visible"}), {contains: () => false}, {pathname:"/test"}, {},
  {addEventListener:()=>{}}, FakeInput, FakeTextarea
);
assert.equal(guards.placed("new"), false, "no insertion is not success");
markers = [marker("old")];
assert.equal(guards.placed("new"), false, "old content is not current content");
markers = [marker("new", true)];
assert.equal(guards.placed("new"), false, "dialog preview is not page insertion");
markers = [marker("new")];
assert.equal(guards.placed("new"), true);
markers[0].hidden = true;
assert.equal(guards.placed("new"), false);
markers = [];
frames = [{closest:()=>null, getClientRects:()=>[1],
  get contentDocument() { throw Error("cross origin"); }}];
assert.equal(guards.placed("new"), false, "inaccessible frame must not imply success");
const disabled = marker("new");
disabled.getAttribute = () => "true";
assert.equal(guards.enabled(disabled), false);
assert.equal(guards.textEditable({isContentEditable:false, getAttribute:()=> "false"}), false);
assert.equal(guards.textEditable({isContentEditable:true, getAttribute:()=> "true"}), true);
assert.throws(() => guards.unique([marker("x"),marker("x")], "insert"), /複数/);
assert.match(source, /旧Local Supporter埋込み.*削除/);
assert.match(source, /replacement_confirmed/);
assert.match(source, /replacement_required/);
assert.match(source, /ensurePreviousVersionRemoved/);
assert.doesNotMatch(source, /旧版を削除済みの場合だけOK/);
assert.match(source, /requiredTexts\.every/);
assert.match(source, /requiredLinks\.every/);
assert.doesNotMatch(source, /activate\(insert\)/);
assert.match(source, /awaiting_user_insert/);
assert.match(source, /Googleダイアログ右下の「挿入」を1回押してください/);
assert.match(source, /inserted_pending_preview/);
assert.match(source, /挿入とGoogle Drive保存は完了しています/);
assert.match(source, /await waitFor\(driveSaved/);
assert.match(source, /function previewOpen\(\)/);
assert.match(source, /プレビューを終了して編集画面へ戻ってから配置してください/);
assert.match(source, /panel\.style\.display = previewOpen\(\) \? "none" : ""/);
assert.match(source, /デスクトップ表示で縦にスクロール/);
assert.match(source, /const textEditable =/);
assert.match(source, /activate\(pageTitleAnchor\)/);
assert.match(source, /ページタイトル編集欄/);
console.log("PASS: absent, stale, preview-only, hidden, cross-origin, disabled and ambiguous placement guards");
