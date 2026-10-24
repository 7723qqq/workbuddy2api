#!/usr/bin/env node
/**
 * WorkBuddy auth 文件解密工具（升级后 access token 被 $wbEncrypted 信封加密）
 *
 * 加密方案（从 WorkBuddy.app 逆向得出）：
 *   - 静态密钥 payload 由 Electron Framework 原生绑定 electron_browser_workbuddy_storage
 *     的 loggerGet() 提供：{version:1, atRestSecretKey:"<44位 canonical base64>", ...}
 *   - AES-256-GCM：
 *       key   = sha256( utf8(atRestSecretKey 字符串本身) )
 *       keyId = sha256(key).hex 前 16 位（envelope.keyId 用于校验）
 *   - 字段级（$wbEncrypted:1）信封 = base64(JSON{suite:1,keyId,nonce(12B),authTag(16B),ciphertext})
 *   - AAD（sym-v1 / field framing）二进制拼接：
 *       "WB-AAD\0" | 0x01 | lp("WBEV1") | lp("sym-v1") | u32be(suite=1)
 *                  | lp(keyId) | 0x02(field) | 0x00(无 sequence) | 0x00(无 final)
 *       其中 lp(s) = u32be(len) + utf8(s)
 *
 * 用法：
 *   ELECTRON_RUN_AS_NODE=1 /Applications/WorkBuddy.app/Contents/MacOS/Electron wb_auth_decrypt.js \
 *     "/Users/smite/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
 */
'use strict';

const crypto = require('crypto');

const AUTH_PATH = process.argv[2] ||
  '/Users/smite/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info';

// ---- 1. 取静态密钥 payload ----
// 密钥只从本机客户端的原生绑定取：**不在代码里内置**（内置密钥随客户端版本更新
// 就会失效，而且属于不该入库的凭据材料）。取不到就让脚本明确失败。
function getKeyPayload() {
  const binding = process._linkedBinding('electron_browser_workbuddy_storage');
  return JSON.parse(binding.loggerGet());
}

function deriveKey(payload) {
  const secret = payload.atRestSecretKey;
  const key = crypto.createHash('sha256').update(secret, 'utf8').digest();
  const keyId = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);
  return { key, keyId };
}

// ---- 2. 构造 AAD（sym-v1, field framing） ----
function lp(s) {
  const b = Buffer.from(s, 'utf8');
  const len = Buffer.alloc(4);
  len.writeUInt32BE(b.length);
  return Buffer.concat([len, b]);
}

function buildFieldAad(keyId, suite) {
  return Buffer.concat([
    Buffer.from('WB-AAD\0', 'ascii'),
    Buffer.from([1]),
    lp('WBEV1'),            // field framing 的 framing tag
    lp('sym-v1'),           // scheme
    (() => { const b = Buffer.alloc(4); b.writeUInt32BE(suite); return b; })(),
    lp(keyId),
    Buffer.from([2]),       // framing code: file=1, field=2, record=3, stream=4
    Buffer.from([0]),       // sequence 缺省
    Buffer.from([0]),       // final 缺省
  ]);
}

// ---- 3. 解密单个 $wbEncrypted 字段 ----
function openField(wrapper, key) {
  if (!wrapper || wrapper.$wbEncrypted !== 1) return wrapper; // 未加密，原样返回
  const env = JSON.parse(Buffer.from(wrapper.envelope, 'base64').toString('utf8'));
  const nonce = Buffer.from(env.nonce, 'base64');
  const tag = Buffer.from(env.authTag, 'base64');
  const ct = Buffer.from(env.ciphertext, 'base64');
  const aad = buildFieldAad(env.keyId, env.suite);
  const d = crypto.createDecipheriv('aes-256-gcm', key, nonce, { authTagLength: 16 });
  d.setAAD(aad);
  d.setAuthTag(tag);
  return Buffer.concat([d.update(ct), d.final()]).toString('utf8');
}

// 明文里可能出现的 {"type":"Buffer","data":[...]} 包装 → 还原为 UTF-8 文本
function unwrap(v) {
  if (v && typeof v === 'object' && v.type === 'Buffer' && Array.isArray(v.data)) {
    return Buffer.from(v.data).toString('utf8');
  }
  if (v && typeof v === 'object') {
    for (const [k, x] of Object.entries(v)) v[k] = unwrap(x);
  }
  return v;
}

// ---- main ----
(function main() {
  const payload = getKeyPayload();
  const { key, keyId } = deriveKey(payload);
  console.error(`[wb-decrypt] keyId=${keyId}`);

  const raw = JSON.parse(require('fs').readFileSync(AUTH_PATH, 'utf8'));

  // 解密 + 解包：明文文本通常是 {"type":"Buffer","data":[...]}（UTF-8 字节包装），先 parse 再还原
  function decodeField(wrapper) {
    const plain = openField(wrapper, key);
    if (typeof plain !== 'string') return plain;
    let v = maybeJson(plain);
    v = unwrap(v);
    if (typeof v === 'string') v = maybeJson(v); // 个别字段可能双重 JSON
    return v;
  }

  const out = {
    account: {
      uid: raw.account?.uid,
      uin: raw.account?.uin,
      nickname: decodeField(raw.account?.nickname),
      phoneNumber: decodeField(raw.account?.phoneNumber),
    },
    auth: {},
  };
  for (const [k, v] of Object.entries(raw.auth || {})) {
    out.auth[k] = decodeField(v);
  }

  console.log(JSON.stringify(out, null, 2));
})();

function maybeJson(s) {
  try { return JSON.parse(s); } catch { return s; }
}
