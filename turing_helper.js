/**
 * turing_helper.js — 从本机 WorkBuddy 桌面端自带的 Turing Shield SDK 取得设备风控 Token。
 *
 * 用途：workbuddy2api（Python 网关）需要给后端请求注入 `X-Device-Token` 头，
 * 否则敏感请求（签到 / 对话）会被上游风控识别为「非真实客户端」。设备 Token 由桌面端
 * 的 TuringShieldSDK 原生桥接生成，本脚本是 Python 侧调用该原生模块的桥梁。
 *
 * 输出（stdout，单行 JSON）：{"token": "v3:AAAA..."}；失败仅向 stderr 写错误并返回非 0。
 *
 * **SDK 目录自动发现（不写死）**：不同用户把 WorkBuddy 桌面端装在不同位置，本脚本会按
 * 以下顺序查找含 `index.cjs` / `package.json` / `turing_sdk.node` / `TuringShieldSDK.dll`
 * 的 turing-sdk 目录：
 *   1. 环境变量 WORKBUDDY_TURING_SDK_DIR（可指向 turing-sdk 目录，或指向桌面端安装基目录）
 *   1b. 环境变量 WORKBUDDY_INSTALL_DIR（桌面端安装基目录，Python 侧发现后下发）
 *   2. %LOCALAPPDATA% / %APPDATA% / %ProgramFiles% / %ProgramFiles(x86)% / %USERPROFILE% 下的
 *      WorkBuddy 或 workbuddy 目录
 *   3. 各盘根目录（WORKBUDDY_TURING_DRIVES，默认 C,D,E,F）下的 workbuddy / WorkBuddy
 *   4. 仓库内置的 SDK（Windows 用 `turing-sdk/`，macOS 用 `macos-turing-sdk/`）——
 *      桌面端没装时兜底；装了则仍优先用桌面端那份（版本号 / channelId 与本机一致）。
 *
 * **配置自动读取（不写死）**：channelId / 产品名 / 版本号优先读安装包里的
 * `resources/app.asar.unpacked/cli/product.json`（`config.turingSdk.channelId`、
 * `genieVersion`），其次读 `resources/install-manifest.json` 的 `appVersion`。
 * 这样官方发版后本脚本自动跟上，不必改代码。
 *
 * 其余可覆盖的环境变量：
 *   WORKBUDDY_TURING_CHANNEL_ID  channelId（默认读 product.json，最后兜底 109144）
 *   WORKBUDDY_TURING_PRODUCT_NAME 产品名（默认 WorkBuddy）
 *   WORKBUDDY_TURING_VERSION      产品版本（默认读安装包，最后兜底 2.0.0）
 *   WORKBUDDY_TURING_DRIVES       参与扫描的盘符列表，逗号分隔（默认 C,D,E,F）
 *   WORKBUDDY_TURING_DEBUG        置 1 时向 stderr 打印取值来源，便于排障
 *
 * ---------------------------------------------------------------------------
 * 稳定性（取自上游 DistPub/workbuddy2api，2026-09）
 * ---------------------------------------------------------------------------
 * macOS 上 Turing SDK 取设备 token 时会在主队列上 dispatch_sync 等结果，而普通
 * Node 进程的主线程停在 libuv 的 kevent 里、永远不排空主队列 → dispatch_sync 死等
 * → "device token request timed out"，且「缓存命中时」又恰好能成功，所以表现为
 * 「有时能拿到、有时超时」。修法是等待期间在主线程上反复泵主 runloop
 * （伴生模块 mainq_drain.node 的 drainMain），与 GUI App 的行为对齐。
 *
 * 辅助措施（均可用环境变量调）：
 *   TURING_ATTEMPTS=2                进程内重试次数
 *   TURING_ATTEMPT_TIMEOUT_MS=3000   单次尝试看门狗
 *   TURING_TOTAL_BUDGET_MS=6000      总预算（必须小于 Python 侧 subprocess 超时）
 *   TURING_DEADLINE_MS               Python 侧下发的剩余时间，预算取它与配置里的较小值
 *   TURING_TOKEN_CACHE_TTL_MS=600000 磁盘缓存有效期（默认 10 分钟，与 Python 侧一致）
 *   TURING_TOKEN_STALE_MS=86400000   失败兜底：超过 TTL 但小于该窗口的旧 token 也可用
 *   TURING_NO_CACHE=1                跳过磁盘缓存（强制真实拉取，排障用）
 */
"use strict";
const path = require("node:path");
const fs = require("node:fs");

// 已知的 SDK 相对路径（相对于桌面端安装基目录）
const REL_SDK_PATHS = [
  "resources/app.asar.unpacked/native/turing-sdk",
  "resources/native/turing-sdk",
];

// 仓库内置的 SDK：桌面端未安装时的兜底。macOS 用 macos-turing-sdk/。
const BUNDLED_SDK_DIR = path.join(
  __dirname, process.platform === "darwin" ? "macos-turing-sdk" : "turing-sdk");

// 伴生模块：macOS 主队列排空。只有 darwin 加载；加载失败退回旧行为（不泵）。
const DRAIN = (function () {
  if (process.platform !== "darwin") return null;
  try {
    return require(path.join(__dirname, "mainq_drain.node"));
  } catch (e) {
    process.stderr.write(
      "[turing_helper] mainq_drain.node 不可用（死锁兜底未启用）：" +
      (e && e.message ? e.message : String(e)) + "\n");
    return null;
  }
})();

// ===== 可调参数 =====================================================================
// 总预算必须小于 Python 侧 subprocess 的超时（admin/turing_token.py 的 _RUN_TIMEOUT），
// 否则进程会被先杀掉，已经拿到的 token 也白拿。Python 侧通过 TURING_DEADLINE_MS
// 把剩余时间下发下来，这里再与配置值取小。
function numEnv(name, def) {
  const v = parseInt(process.env[name] || "", 10);
  return Number.isFinite(v) ? v : def;
}
const ATTEMPTS = Math.max(1, numEnv("TURING_ATTEMPTS", 2));
const ATTEMPT_TIMEOUT_MS = Math.max(500, numEnv("TURING_ATTEMPT_TIMEOUT_MS", 3000));
const DEADLINE_MS = numEnv("TURING_DEADLINE_MS", 0);
const TOTAL_BUDGET_MS = Math.max(
  ATTEMPT_TIMEOUT_MS,
  Math.min(numEnv("TURING_TOTAL_BUDGET_MS", 6000), DEADLINE_MS || Infinity));

// 磁盘缓存：跨进程复用（Python 侧只有进程内缓存）。设备 token 本身有效期很长，
// 拉不到新的时，过期窗口内的旧 token 依然可用 —— 总比没有强。
const CACHE_FILE = process.env.TURING_TOKEN_CACHE ||
  path.join(__dirname, ".turing_token_cache.json");
const CACHE_TTL_MS = numEnv("TURING_TOKEN_CACHE_TTL_MS", 10 * 60 * 1000);
const CACHE_STALE_MS = numEnv("TURING_TOKEN_STALE_MS", 24 * 60 * 60 * 1000);
const NO_CACHE = process.env.TURING_NO_CACHE === "1";

function readCachedToken(maxAgeMs) {
  if (NO_CACHE || !Number.isFinite(maxAgeMs) || maxAgeMs <= 0) return null;
  try {
    const c = JSON.parse(fs.readFileSync(CACHE_FILE, "utf8"));
    if (c && typeof c.token === "string" && c.token &&
        Number.isFinite(c.ts) && Date.now() - c.ts <= maxAgeMs) {
      return c.token;
    }
  } catch (_) { /* 无缓存或已损坏：忽略 */ }
  return null;
}

function writeCachedToken(token) {
  if (NO_CACHE) return;
  try {
    fs.writeFileSync(CACHE_FILE, JSON.stringify({ token: token, ts: Date.now() }));
  } catch (_) { /* 写失败不影响主流程 */ }
}

// 收尾：先设 exitCode 让 Node 自然退出（保证 stdout 冲刷），3 秒后仍未退出再强杀。
function finish(code) {
  process.exitCode = code;
  const killer = setTimeout(function () { process.exit(code); }, 3000);
  if (typeof killer.unref === "function") killer.unref();
}

// 兜底默认值：只在读不到安装包时使用，不代表真实版本
const FALLBACK_DESKTOP_VERSION = "5.5.6";
const FALLBACK_TURING_CHANNEL_ID = 109144;

// SDK 目录应具备的特征文件（命中其一即视为有效 SDK 目录）
function looksLikeSdk(dir) {
  try {
    const ents = fs.readdirSync(dir);
    return ents.some((n) =>
      /^index\.(cjs|js)$/i.test(n) ||
      n.toLowerCase() === "package.json" ||
      n.toLowerCase() === "turingshieldsdk.dll" ||
      /\.node$/i.test(n)
    );
  } catch (_) {
    return false;
  }
}

// 目录是否是桌面端安装基目录（以存在 resources/app.asar 为准）
function looksLikeInstall(dir) {
  try {
    return fs.statSync(path.join(dir, "resources", "app.asar")).isFile();
  } catch (_) {
    return false;
  }
}

function readJson(p) {
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch (_) {
    return null;
  }
}

// 从已定位的安装基目录读出版本号与 turing 配置；读不到返回空对象。
function readInstallMeta(base) {
  const out = { version: "", channelId: 0, productName: "", source: {} };
  if (!base) return out;
  const unpacked = path.join(base, "resources", "app.asar.unpacked");

  const productJson = readJson(path.join(unpacked, "cli", "product.json"));
  if (productJson) {
    const turing = (productJson.config || {}).turingSdk;
    if (turing && Number.isInteger(turing.channelId) && turing.channelId > 0) {
      out.channelId = turing.channelId;
      out.source.channelId = "cli/product.json";
    }
    if (typeof productJson.genieVersion === "string" && productJson.genieVersion) {
      out.version = productJson.genieVersion;
      out.source.version = "cli/product.json";
    }
    if (typeof productJson.applicationName === "string" && productJson.applicationName) {
      out.productName = productJson.applicationName;
      out.source.productName = "cli/product.json";
    }
  }

  const manifest = readJson(path.join(base, "resources", "install-manifest.json"));
  if (manifest && !out.version &&
      typeof manifest.appVersion === "string" && manifest.appVersion) {
    out.version = manifest.appVersion;
    out.source.version = "install-manifest.json";
  }
  return out;
}

function collectCandidateDirs() {
  const out = [];
  const add = (d) => { if (d && !out.includes(d)) out.push(d); };

  // 1) 显式覆盖（最高优先级）
  //    WORKBUDDY_TURING_SDK_DIR 是历史变量名，继续支持；
  //    WORKBUDDY_INSTALL_DIR 由 Python 侧发现后下发，优先级相同。
  for (const ev of ["WORKBUDDY_TURING_SDK_DIR", "WORKBUDDY_INSTALL_DIR"]) {
    const explicit = process.env[ev];
    if (!explicit) continue;
    const e = path.resolve(explicit);
    if (looksLikeSdk(e)) {
      add(e);
    } else {
      // 当作安装基目录，拼上已知相对路径再试
      for (const rel of REL_SDK_PATHS) add(path.join(e, rel));
    }
  }

  // 2) 常见安装基目录
  //    APP_NAMES：安装目录名候选。WorkBuddyAI 是官方桌面端在新版安装器下的目录名
  //    （2026-10-24 实测本机装在 %LOCALAPPDATA%\Programs\WorkBuddyAI）。
  //    PROGRAMS_SUBROOT：用户级安装根 %LOCALAPPDATA%\Programs\<App> —— 新版安装器
  //    的默认落点，不扫这一层就等于没扫（本机 SDK 就在它下面）。
  const APP_NAMES = ["WorkBuddy", "workbuddy", "WorkBuddyAI"];
  const PROGRAMS_SUBROOT = "Programs";
  const bases = [];
  for (const ev of ["LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "USERPROFILE", "HOME"]) {
    const v = process.env[ev];
    if (!v) continue;
    for (const name of APP_NAMES) {
      bases.push(path.join(v, name));
      if (ev === "LOCALAPPDATA") bases.push(path.join(v, PROGRAMS_SUBROOT, name));
    }
  }

  // 3) 各盘根目录（支持纯盘符 C / 带冒号 C: / 完整根路径 C:\ 三种写法）
  const drives = (process.env.WORKBUDDY_TURING_DRIVES || process.env.WORKBUDDY_DRIVES || "C,D,E,F")
    .split(",").map((s) => s.trim()).filter(Boolean);
  for (const d of drives) {
    let root;
    if (/^[A-Za-z]$/.test(d)) {
      root = d + ":\\";          // 纯盘符 C -> C:\
    } else if (/^[A-Za-z]:$/.test(d)) {
      root = d + "\\";           // C: -> C:\
    } else if (d.endsWith("\\") || d.endsWith("/")) {
      root = d;                  // 已是根路径
    } else {
      root = d + "\\";
    }
    bases.push(path.join(root, "workbuddy"));
    bases.push(path.join(root, "WorkBuddy"));
  }

  for (const base of bases) {
    for (const rel of REL_SDK_PATHS) add(path.join(base, rel));
  }

  // 4) 仓库内置的 SDK（桌面端未安装时的兜底）——放在最后：本机真装了桌面端时
  //    仍然优先用它那份，版本号 / channelId 才能跟客户端对上。
  add(BUNDLED_SDK_DIR);
  return out;
}

function findSdkDir() {
  for (const dir of collectCandidateDirs()) {
    if (looksLikeSdk(dir)) return dir;
  }
  return null;
}

// 反推 SDK 目录所属的安装基目录（…/resources/app.asar.unpacked/native/turing-sdk）
function installBaseOf(sdkDir) {
  let p = sdkDir;
  for (let i = 0; i < 6; i++) {
    p = path.dirname(p);
    if (looksLikeInstall(p)) return p;
  }
  return process.env.WORKBUDDY_INSTALL_DIR
    ? path.resolve(process.env.WORKBUDDY_INSTALL_DIR) : null;
}

const sdkDir = findSdkDir();
if (!sdkDir) {
  process.stderr.write(
    "TuringShield SDK 未找到。本脚本依赖本机已安装的 WorkBuddy 桌面端自带的 TuringShieldSDK 原生模块。\n" +
    "已搜索以下候选目录（设置环境变量 WORKBUDDY_TURING_SDK_DIR 指向含 index.cjs / TuringShieldSDK.dll 的目录即可覆盖）：\n"
  );
  for (const d of collectCandidateDirs()) process.stderr.write("  - " + d + "\n");
  process.stderr.write("\n若你已安装桌面端但目录特殊，请设置 WORKBUDDY_TURING_SDK_DIR 后重试。\n");
  finish(1);
}

// 把 SDK 目录及其 build/Release 加入 DLL 搜索路径，提升 TuringShieldSDK.dll 解析成功率
try {
  const extra = [sdkDir, path.join(sdkDir, "build", "Release")];
  const sep = process.platform === "win32" ? ";" : ":";
  process.env.PATH = extra.join(sep) + sep + (process.env.PATH || "");
} catch (_) {
  /* 忽略：PATH 增强失败不影响主流程，仅作为辅助 */
}

let turing;
try {
  turing = require(sdkDir);
} catch (e) {
  process.stderr.write("require turing sdk failed: " + (e && e.message ? e.message : String(e)) + "\n");
  process.stderr.write("SDK dir: " + sdkDir + "\n");
  finish(1);
}

// 配置：env 覆盖 > 安装包元数据 > 兜底默认
const installBase = installBaseOf(sdkDir);
const meta = readInstallMeta(installBase);

function pickEnv(name, metaVal, fallback) {
  const v = process.env[name];
  if (v !== undefined && String(v).trim() !== "") return String(v).trim();
  if (metaVal !== undefined && metaVal !== null && String(metaVal).trim() !== "") {
    return String(metaVal).trim();
  }
  return String(fallback);
}

// 注意 `|| FALLBACK`：读不到安装包时 meta.channelId 是 0，而 0 会被 SDK 判为
// 「未分配 channelId」直接报错，必须回落到兜底值。
const channelId = parseInt(
  pickEnv("WORKBUDDY_TURING_CHANNEL_ID", meta.channelId, FALLBACK_TURING_CHANNEL_ID), 10)
  || FALLBACK_TURING_CHANNEL_ID;
const productName = pickEnv("WORKBUDDY_TURING_PRODUCT_NAME", meta.productName, "WorkBuddy");
const productVersion = pickEnv("WORKBUDDY_TURING_VERSION", meta.version, FALLBACK_DESKTOP_VERSION);

if (process.env.WORKBUDDY_TURING_DEBUG === "1") {
  process.stderr.write(
    "[turing_helper] sdkDir=" + sdkDir + "\n" +
    "[turing_helper] installBase=" + installBase + "\n" +
    "[turing_helper] channelId=" + channelId +
      " (" + (process.env.WORKBUDDY_TURING_CHANNEL_ID ? "env"
              : (meta.source.channelId || "fallback")) + ")\n" +
    "[turing_helper] version=" + productVersion +
      " (" + (process.env.WORKBUDDY_TURING_VERSION ? "env"
              : (meta.source.version || "fallback")) + ")\n" +
    "[turing_helper] productName=" + productName + "\n"
  );
}

function isSupported() {
  try {
    return !turing.isSupported || turing.isSupported();
  } catch (e) {
    return false;
  }
}

// ===== 核心：带主 runloop 泵的单次取 token ==========================================
async function fetchTokenOnce(attemptTimeoutMs) {
  const pending = Promise.resolve(turing.fetchDeviceToken({
    usingCachedMessage: true,
    includesOutdatedMessage: true,
    includesDeviceInfo: true,
    timeoutMs: Math.min(60000, attemptTimeoutMs + 2000), // SDK 自身超时略晚于我们的看门狗
  }));

  if (!DRAIN || process.platform !== "darwin") {
    return await pending; // 无伴生模块：维持旧行为
  }

  let state = 0; // 0=进行中 1=成功 2=失败
  let value, error;
  pending.then(
    function (v) { value = v; state = 1; },
    function (e) { error = e; state = 2; }
  );

  const deadline = Date.now() + attemptTimeoutMs;
  while (state === 0) {
    if (Date.now() >= deadline) {
      throw new Error("watchdog: fetchDeviceToken 未在 " + attemptTimeoutMs + "ms 内返回");
    }
    try { DRAIN.drainMain(50); } catch (_) { /* 单次泵失败不致命 */ }
    // 转一圈事件循环：SDK 的 Promise 回调（线程安全函数投递到主线程）得以执行
    await new Promise(function (resolve) { setImmediate(resolve); });
  }
  if (state === 2) throw error;
  return value;
}

async function fetchTokenWithRetry() {
  const totalDeadline = Date.now() + TOTAL_BUDGET_MS;
  let lastErr = new Error("unknown fetch failure");
  for (let i = 1; i <= ATTEMPTS; i++) {
    const remain = totalDeadline - Date.now();
    if (remain < 1500) break; // 剩余预算不足以再来一次
    const per = Math.min(ATTEMPT_TIMEOUT_MS, remain);
    try {
      const token = await fetchTokenOnce(per);
      const t = (token || "").toString().trim();
      if (t) return t;
      lastErr = new Error("turing sdk returned empty token");
    } catch (e) {
      lastErr = e;
    }
    process.stderr.write("[turing_helper] attempt " + i + "/" + ATTEMPTS + " failed: " +
      (lastErr && lastErr.message ? lastErr.message : String(lastErr)) + "\n");
  }
  throw lastErr;
}

(async () => {
  if (!isSupported()) {
    const loadErr = (typeof turing.getLoadError === "function") ? turing.getLoadError() : null;
    process.stderr.write("turing sdk not supported" + (loadErr ? (": " + loadErr) : " on this platform") + "\n");
    process.stderr.write("SDK dir: " + sdkDir + "\n");
    finish(1);
    return;
  }

  // 1) 新鲜的磁盘缓存：直接用（fork node 的开销远大于命中判断）
  const cached = readCachedToken(CACHE_TTL_MS);
  if (cached) {
    process.stdout.write(JSON.stringify({ token: cached }));
    finish(0);
    return;
  }

  try {
    turing.configure(channelId, productName, productVersion);
    // configure 后先泵一小会儿：冲掉 SDK 初始化阶段可能投递到主队列的 block
    if (DRAIN) { try { DRAIN.drainMain(100); } catch (_) {} }

    const t = await fetchTokenWithRetry();
    writeCachedToken(t);
    process.stdout.write(JSON.stringify({ token: t }));
    finish(0);
  } catch (e) {
    // 2) 兜底：拉不到新 token 时，过期窗口内的旧设备 token 依然可用
    const stale = readCachedToken(CACHE_STALE_MS);
    if (stale) {
      process.stderr.write("[turing_helper] 获取失败，回退到过期缓存的 token\n");
      process.stdout.write(JSON.stringify({ token: stale }));
      finish(0);
      return;
    }
    process.stderr.write("fetch device token failed: " + (e && e.message ? e.message : String(e)) + "\n");
    process.stderr.write("SDK dir: " + sdkDir + "\n");
    finish(1);
  }
})();
