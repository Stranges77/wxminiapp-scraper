// Agent: hook every function that references applet_index_container.cc,
// plus the 1101-magic callee and the CDP filter candidate.
const OFFSETS = [
    0x2cdb640, 0x2cdbc20, 0x2cdc1a0, 0x2cdc470, 0x2cdc6c0, 0x2cdfee0,
    0x2ce0b10, 0x2ce2340, 0x2ce4890, 0x2ce4af0, 0x2ce4d50, 0x2ce52a0,
    0x2ce6030, 0x2ce66b0, 0x2ce6c20, 0x2ce7690,
    0x2ce7c50, // our LoadStartHookOffset candidate
    0x2ce7ed0, 0x2ce8110, 0x2ce83a0, 0x2ce86f0, 0x2cea730, 0x2ceb3b0,
    0x2cebde0, 0x2cec030, 0x2cec340, 0x2cecbf0, 0x2cece30, 0x2ced600,
    0x2cee010, 0x2cee2a0, 0x2cef040, 0x2cf0670, 0x2cf0930, 0x2cf0d20,
    0x2cf1650, 0x2cf1c00,
    0x363a690, // 1101-magic callee
    0x3978930, // CDP filter candidate
    0x37aa690, // SendToClientFilter xref function
];

const mod = Process.findModuleByName("flue.dll");
send(`flue.dll base = ${mod.base}, size = ${mod.size}`);

let ok = 0;
for (const off of OFFSETS) {
    try {
        Interceptor.attach(mod.base.add(off), {
            onEnter(args) {
                send(`HIT 0x${off.toString(16)} this=${args[0]} a1=${args[1]} a2=${args[2]}`);
            },
        });
        ok++;
    } catch (e) {
        send(`attach failed 0x${off.toString(16)}: ${e}`);
    }
}
send(`installed ${ok}/${OFFSETS.length} hooks`);
