// (tests/test_harness_shell_js.py runs this.) A host simulator for the MCP Apps view: runs OUR shell script in a stub DOM and plays the host by the published protocol.
import fs from "node:fs"; import vm from "node:vm";
const script = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[3] || "normal";            // normal | strip_meta | reject_init | silent
const log = []; const calls = []; const handlers = {}; let root = { innerHTML: "", scrollHeight: 0 };
let docH = 0;                                          // the height the document would have
const parent = { postMessage(m) { if (m.method === "tools/call") calls.push(m.params.name); log.push(["app->host", m.method || ("reply " + m.id), m.params ? JSON.stringify(m.params).slice(0, 90) : ""]); host(m); } };
const win = { innerWidth: 400, addEventListener(t, f) { (handlers[t] ||= []).push(f); }, parent };
const doc = { getElementById: () => root, documentElement: { style: {}, getBoundingClientRect: () => ({ height: root.innerHTML ? 120 : 0 }), get scrollHeight() { return Math.max(docH, root.innerHTML ? 120 : 0); }, scrollWidth: 400 }, addEventListener(t, f) { (handlers["doc:" + t] ||= []).push(f); }, hidden: false };
const ctx = vm.createContext({ window: win, document: doc, parent, setInterval: () => 1, clearInterval: () => {}, console, JSON, Promise, Object, Array, String, Math, Date, setTimeout, clearTimeout });
ctx.window.parent = parent; ctx.self = win;
function toApp(m) { for (const f of handlers.message || []) f({ source: parent, data: m }); }
function host(m) {
  if (mode === "silent") return;
  if (m.method === "ui/initialize") { if (mode === "reject_init") return toApp({ jsonrpc: "2.0", id: m.id, error: { code: -32602, message: "bad params" } });
    return toApp({ jsonrpc: "2.0", id: m.id, result: { protocolVersion: "2025-11-21", hostInfo: { name: "sim", version: "1" }, hostCapabilities: {}, hostContext: { displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"], theme: "light" } } }); }
  if (m.method === "ui/notifications/initialized") {
    const meta = mode === "strip_meta" ? {} : { "mas/cards": [{ slot: "network", intent_id: "n1", html: '<div data-card="network">MAP</div>' }], "mas/live": { active: false, every_s: 3 } };
    setTimeout(() => toApp({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: { content: [{ type: "text", text: "x" }], structuredContent: { intents: [] }, _meta: meta } }), 5); }
  if (m.method === "tools/call") toApp({ jsonrpc: "2.0", id: m.id, result: { content: [] } });
}
vm.runInContext(script, ctx);
await new Promise(r => setTimeout(r, 80));
const sizes = log.filter(l => l[1] === "ui/notifications/size-changed").map(l => l[2]);
const tools = calls;
console.log(JSON.stringify({ mode, tools, messages: log.map(l => l[1]), sizes, rendered: root.innerHTML.replace(/<[^>]+>/g,'').slice(0, 90) }));
process.exit(0);
