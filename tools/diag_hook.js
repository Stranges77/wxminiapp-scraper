// Diagnostic driver: hook all OnLoadStart candidates in a given PID.
// Usage: node diag_hook.js <pid>
const fs = require("fs");
const path = require("path");
// frida 来自 third_party/WMPFDebugger 的 node_modules (本文件位于 tools/, 上一级即项目根)
const frida = require(path.join(__dirname, "..", "third_party", "WMPFDebugger", "node_modules", "frida"));

const AGENT = fs.readFileSync(__dirname + "/diag_agent.js", "utf8");

async function main() {
    const pid = parseInt(process.argv[2], 10);
    const device = await frida.getLocalDevice();
    const session = await device.attach(pid);
    const script = await session.createScript(AGENT);
    script.message.connect((m) => {
        if (m.type === "send") console.log(`[pid ${pid}]`, m.payload);
        else if (m.type === "error") console.error(`[pid ${pid} err]`, m.description);
    });
    await script.load();
    console.log(`diag hooks installed on pid ${pid}`);
}
main().catch((e) => {
    console.error(e);
    process.exit(1);
});
