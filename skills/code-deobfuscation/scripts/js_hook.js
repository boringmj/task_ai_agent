#!/usr/bin/env node
/**
 * JS 动态去混淆 harness —— 旁路捕获 eval / Function 构造收到的源码。
 *
 * 为什么这么做:字符串数组混淆、自解密 loader 的明文只在运行时出现,静态看不见。
 * 把样本放进受控上下文跑,在"喂给 eval/Function"的瞬间把源码截下来。
 *
 * 用法:
 *   node js_hook.js <样本.js> [输出文件]
 *
 * 安全:样本会被真实执行,只能在 VM 沙箱内运行,禁止在宿主或容器上跑。
 */
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const samplePath = process.argv[2];
const outPath = process.argv[3] || 'extracted.js';
if (!samplePath) {
  console.error('用法: node js_hook.js <样本.js> [输出文件]');
  process.exit(1);
}

const captured = [];
const RealFunction = Function;

const sandbox = {
  console, Buffer,
  setTimeout, setInterval, clearTimeout, clearInterval,
  Date, Math, JSON, String, Number, Boolean, Array, Object, RegExp, Error, Promise, Symbol,
  eval(src) {
    if (typeof src === 'string') captured.push('/* ---- via eval ---- */\n' + src);
    return vm.runInThisContext(src);
  },
  Function(...args) {
    const body = args[args.length - 1];
    if (typeof body === 'string') captured.push('/* ---- via Function ---- */\n' + body);
    return RealFunction(...args);
  },
};
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
const src = fs.readFileSync(samplePath, 'utf8');

try {
  vm.runInContext(src, ctx, { filename: path.basename(samplePath) });
} catch (e) {
  console.error('[*] 样本执行中断(可能是反调试,或执行到尾部报错):', e && e.message);
}

if (!captured.length) {
  console.error('[!] 未截获 eval/Function。样本可能没走这两个入口;可考虑:');
  console.error('    1) hook 被反复调用的字符串解码函数 2) 改拦 document.write / innerHTML');
  console.error('    3) 绕过反调试后重试 —— 见 references/js.md');
  process.exit(2);
}
fs.writeFileSync(outPath, captured.join('\n\n'));
console.log(`[OK] 截获 ${captured.length} 处明文,已写入 ${outPath}`);
