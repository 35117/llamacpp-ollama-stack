// 从 ui/index.html 里抽出真实函数来跑，避免测到"副本"。
// 需要 node（不需要浏览器、不需要服务）。
const fs = require("fs");
const path = require("path");
const UI = path.join(__dirname, "..", "ui", "index.html");
const html = fs.readFileSync(UI, "utf8");

function grab(name) {
  const i = html.indexOf("function " + name + "(");
  if (i < 0) throw new Error("找不到 " + name);
  // 从 function 开头配平大括号
  let d = 0, j = html.indexOf("{", i);
  for (let k = j; k < html.length; k++) {
    if (html[k] === "{") d++;
    else if (html[k] === "}") { d--; if (d === 0) return html.slice(i, k + 1); }
  }
  throw new Error("括号不配平: " + name);
}

const src = grab("makeTitle") + "\n" + grab("buildPayloadMessages");
const state = { messages: [] };
const VISION_KEEP_TURNS = 2;
const f = new Function("state", "VISION_KEEP_TURNS",
  src + "\nreturn {makeTitle, buildPayloadMessages};");
const api = f(state, VISION_KEEP_TURNS);

let bad = 0;
function eq(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  console.log((ok ? "  PASS  " : "  FAIL  ") + name + (ok ? "" : "\n         got=" + JSON.stringify(got) + "\n        want=" + JSON.stringify(want)));
  if (!ok) bad++;
}

// ---- makeTitle
eq("去掉「帮我」并取第一句",
   api.makeTitle("帮我写一个 Python 脚本，把文件夹里的图片批量转成 webp。要保留原文件名。", []),
   "写一个 Python 脚本，把文件夹…");
eq("短句原样", api.makeTitle("什么是 KV 缓存", []), "什么是 KV 缓存");
eq("纯图片消息", api.makeTitle("", [{name: "shot.png"}]), "［图片］shot.png");
eq("无文字无图", api.makeTitle("", []), "［图片］");
eq("超长截断到 18 字 + 省略号",
   api.makeTitle("一二三四五六七八九十一二三四五六七八九十一二三四五", []),
   "一二三四五六七八九十一二三四五六七八…");
eq("「请问」被去掉", api.makeTitle("请问 llama.cpp 的 -fa 是什么", []), "llama.cpp 的 -fa 是什…");

// ---- buildPayloadMessages：只保留最近 2 条带图消息
state.messages = [
  {role: "user", content: "图1", images: [{b64: "AAA"}]},
  {role: "assistant", content: "嗯"},
  {role: "user", content: "图2", images: [{b64: "BBB"}]},
  {role: "assistant", content: "嗯2"},
  {role: "user", content: "图3", images: [{b64: "CCC"}]},
  {role: "assistant", content: ""},          // 空回复要被过滤
];
const out = api.buildPayloadMessages();
eq("空 assistant 被过滤", out.length, 5);
eq("最早的图1不再上传", out[0].images, undefined);
eq("图2 仍上传", out[2].images, ["BBB"]);
eq("图3 仍上传", out[4].images, ["CCC"]);
eq("只有 2 条带图", out.filter(m => m.images).length, 2);
eq("图片是裸 base64 而非 dataUrl", out[4].images[0], "CCC");

console.log(bad ? "\n" + bad + " 项失败" : "\n全部通过");
process.exit(bad ? 1 : 0);
