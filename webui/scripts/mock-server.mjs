/**
 * Zero-dependency mock API + static server for WebUI development/screenshots.
 *
 *   node scripts/mock-server.mjs [port]
 *
 * Serves `dist/` with an SPA fallback and stubs every `/api/v1` endpoint the
 * console client (src/lib/api.ts) can call, wrapped in the real Envelope shape.
 * Frames/files are served as generated SVG placeholders so grids and the live
 * view have something to draw. The event WebSocket is intentionally not
 * implemented: the console falls back to polling, which is what we want to see.
 */
import http from "node:http";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DIST = path.join(ROOT, "dist");
const PORT = Number(process.argv[2] ?? 8974);

const VERSION = "0.3.7-mock";
const BOOT_MS = Date.now();
const now = () => Date.now();
const env = (data, extra = {}) => ({ data, server_time_ms: now(), ...extra });
const json = (res, status, body) => {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(text);
};

// --------------------------------------------------------------------- data ---
const hue = (seed) => [...seed].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);

function device(uid, name, host, os, osv, arch, ver, proto, state, transport, opts = {}) {
  const lastSeen = now() - (opts.ageS ?? 4) * 1000;
  return {
    uid, name, hostname: host, os_name: os, os_version: osv, arch,
    agent_version: ver, protocol: proto,
    boot_id: `boot-${uid.slice(0, 6)}`,
    enrolled_at_ms: BOOT_MS - 86400_000 * 26,
    last_seen_ms: lastSeen,
    uninstalled_at_ms: null,
    note: opts.note ?? "",
    presence: {
      state, transport,
      reason: opts.reason ?? "",
      last_seen_ms: lastSeen,
      age_ms: now() - lastSeen,
      channel_since_ms: lastSeen,
      capabilities: {
        control: state === "online",
        files: state === "online" || state === "degraded",
        screenshot: state === "online",
        timeline: state !== "uninstalled",
        update: proto >= 3,
        uninstall: true,
      },
      capture: state === "uninstalled" ? null : {
        state: opts.captureState ?? "ok",
        reason: opts.captureReason ?? "",
        sessions: opts.sessions ?? 2,
        workers: [3, 7],
        at_ms: lastSeen,
      },
      self_update_blocked: proto < 3 ? "agent 2.x 无法自动升级，请手动安装 3.x 安装包" : null,
      protocol: proto,
    },
    sysinfo: {
      cpu_model: opts.cpu ?? "Intel Core i7-1265U",
      cpu_percent: opts.cpuPercent ?? 12 + hue(uid) % 40,
      mem_percent: 34 + hue(uid) % 30,
      mem_total: 17_179_869_184,
      disks: [{ mount: "C:", total: 42_949_672_960, free: 9_663_676_416 }],
      sessions: opts.sessions ?? 2,
      resolution: opts.res ?? "1920x1080",
    },
    settings: {},
    latest_frame_id: opts.noFrames ? null : 4100 + hue(uid) % 40,
    server_time_ms: now(),
  };
}

const DEVICES = [
  device("W-7f3a91", "手腕观测站", "watch-band-01", "Android", "13 (Wear)", "arm64", "0.3.6", 3, "online", "ws", { ageS: 3, res: "454x454", note: "表盘右侧,充电座" }),
  device("D-2c81e4", "主控笔记本", "LAPTOP-TR9", "Windows", "11 23H2", "x64", "0.3.6", 3, "online", "ws", { ageS: 2, note: "日常主力机" }),
  device("D-55aa10", "书房迷你机", "MINI-PC-02", "Windows", "10 22H2", "x64", "0.3.5", 3, "degraded", "poll", { ageS: 95, reason: "WebSocket 不可达,轮询通道正常", cpu: "Intel N100" }),
  device("D-9b0c33", "设计工作站", "MACSTUDIO-X", "macOS", "14.6", "arm64", "0.3.6", 3, "offline", "none", { ageS: 5400, reason: "超过 45 分钟未上报", noFrames: false }),
  device("D-1e77f8", "仓库旧机器", "LEGACY-W7", "Windows", "7 SP1", "x86", "0.2.9", 2, "online", "ws", { ageS: 20 }),
  device("D-cc42d0", "已停用设备", "OLD-DESK", "Windows", "10", "x64", "0.3.4", 3, "uninstalling", "none", { ageS: 3600, reason: "卸载指令已确认,等待回执" }),
];

const deviceByUid = (uid) => DEVICES.find((d) => d.uid === uid);

function frame(id, uid, tsMs, w = 1920, h = 1080) {
  return { id, ts_ms: tsMs, device_time_ms: tsMs - 120, width: w, height: h, size_bytes: 180_000 + (id % 7) * 12_000, has_thumb: true, kind: "screen", session: 1 + (id % 3) };
}

const FRAMES = new Map();
for (const d of DEVICES) {
  if (d.presence.state === "uninstalling") continue;
  const list = [];
  for (let i = 0; i < 60; i += 1) {
    const id = 4000 + hue(d.uid) % 40 + i;
    list.push(frame(id, d.uid, now() - (i + 1) * 240_000, d.os_name === "Android" ? 454 : 1920, d.os_name === "Android" ? 454 : 1080));
  }
  FRAMES.set(d.uid, list);
}

const COMMANDS = [
  { id: "cmd-9012", type: "screenshot", kind: "screenshot", status: "ok", payload: {}, result: { frame_id: 4103 }, error: null, error_code: null, deadline_ms: 20_000, attempts: 1, created_at_ms: now() - 3600_000, dispatched_at_ms: now() - 3599_000, finished_at_ms: now() - 3597_000 },
  { id: "cmd-9013", type: "files.list", kind: "files.list", status: "ok", payload: { path: "C:\\" }, result: { count: 12 }, error: null, error_code: null, deadline_ms: 15_000, attempts: 1, created_at_ms: now() - 2400_000, dispatched_at_ms: now() - 2399_000, finished_at_ms: now() - 2398_000 },
  { id: "cmd-9014", type: "agent.update", kind: "agent.update", status: "error", payload: { version: "0.3.6" }, result: null, error: "设备 45 分钟未上报,调度超时", error_code: "TIMEOUT", deadline_ms: 25_000, attempts: 3, created_at_ms: now() - 1800_000, dispatched_at_ms: now() - 1799_000, finished_at_ms: now() - 1774_000 },
  { id: "cmd-9015", type: "control.grant", kind: "control.grant", status: "pending", payload: { ttl_s: 300 }, result: null, error: null, error_code: null, deadline_ms: 10_000, attempts: 0, created_at_ms: now() - 30_000, dispatched_at_ms: null, finished_at_ms: null },
];

const AUDIT_ACTIONS = ["auth.login", "control.grant", "control.revoke", "device.create_command", "device.patch", "settings.patch", "updates.upload", "backup.retrieve", "install.token_created"];
const AUDIT = Array.from({ length: 46 }, (_, i) => {
  const d = DEVICES[i % DEVICES.length];
  return {
    id: 800 - i,
    ts_ms: now() - i * 1_500_000 - 120_000,
    actor: i % 5 === 0 ? "system" : "operator",
    action: AUDIT_ACTIONS[i % AUDIT_ACTIONS.length],
    device_uid: d.uid,
    device_name: d.name,
    ip: i % 5 === 0 ? "—" : "203.0.113.7",
    detail: { via: i % 2 ? "console" : "api", ttl_s: 300 },
  };
});

const SETTING_SPECS = {
  ws_enabled: { kind: "bool", default: true, min: null, max: null, choices: [], per_device: true, secret: false, description: "启用 WebSocket 实时通道" },
  poll_interval_s: { kind: "int", default: 30, min: 5, max: 600, choices: [], per_device: true, secret: false, description: "轮询间隔(秒)" },
  heartbeat_interval_s: { kind: "int", default: 25, min: 5, max: 300, choices: [], per_device: true, secret: false, description: "心跳间隔(秒)" },
  transport_reprobe_s: { kind: "int", default: 300, min: 30, max: 3600, choices: [], per_device: false, secret: false, description: "传输通道重探测周期(秒)" },
  capture_enabled: { kind: "bool", default: true, min: null, max: null, choices: [], per_device: true, secret: false, description: "允许受控端采集屏幕" },
  capture_interval_s: { kind: "int", default: 900, min: 30, max: 86400, choices: [], per_device: true, secret: false, description: "采集间隔(秒)" },
  control_enabled: { kind: "bool", default: true, min: null, max: null, choices: [], per_device: true, secret: false, description: "允许远程控制会话" },
  file_write_enabled: { kind: "bool", default: false, min: null, max: null, choices: [], per_device: true, secret: false, description: "允许文件写入操作" },
  archive_enabled: { kind: "bool", default: true, min: null, max: null, choices: [], per_device: true, secret: false, description: "启用远端归档" },
  media_retention_interval_s: { kind: "int", default: 3600, min: 600, max: 86400, choices: [], per_device: false, secret: false, description: "媒体清理周期(秒)" },
  install_token_ttl_minutes: { kind: "int", default: 30, min: 5, max: 720, choices: [], per_device: false, secret: false, description: "安装令牌有效期(分钟)" },
  backup_remote_secret: { kind: "str", default: null, min: null, max: null, choices: [], per_device: false, secret: true, description: "远端仓库访问凭据" },
};
const SETTING_VALUES = Object.fromEntries(Object.entries(SETTING_SPECS).map(([k, s]) => [k, s.default]));

const PACKAGES = [
  { version: "0.3.6", filename: "observatory-agent-0.3.6-win64.zip", size_bytes: 48_200_000, sha256: createHash("sha256").update("v0.3.6").digest("hex"), notes: "WS 泵热循环修复;U盘令牌池安装包", created_at_ms: BOOT_MS - 86_400_000 * 2, download_url: "/api/v1/updates/agent/0.3.6/download" },
  { version: "0.3.5", filename: "observatory-agent-0.3.5-win64.zip", size_bytes: 47_900_000, sha256: createHash("sha256").update("v0.3.5").digest("hex"), notes: "文件管理器分块写入", created_at_ms: BOOT_MS - 86_400_000 * 9, download_url: "/api/v1/updates/agent/0.3.5/download" },
  { version: "0.3.4", filename: "observatory-agent-0.3.4-win64.zip", size_bytes: 47_100_000, sha256: createHash("sha256").update("v0.3.4").digest("hex"), notes: "时间线缩略图墙", created_at_ms: BOOT_MS - 86_400_000 * 20, download_url: "/api/v1/updates/agent/0.3.4/download" },
];

const INSTALL_INFO = () => ({
  enabled: true, enroll_required: true, available_tokens: 2, token_ttl_minutes: 30,
  version: VERSION, console_host: "control.tianhw.top", base_urls: ["https://control.tianhw.top", "https://backup.tianhw.top"],
  install_token_endpoint: "/api/v1/install-tokens", script_url: "/api/v1/install/setup.ps1", cmd_url: "/api/v1/install/setup.cmd",
  artifacts: [
    { kind: "cmd", label: "Windows CMD 一键安装", filename: "setup.cmd", url: "/api/v1/install/setup.cmd", hint: "在目标设备以管理员身份运行" },
    { kind: "ps1", label: "PowerShell 安装脚本", filename: "setup.ps1", url: "/api/v1/install/setup.ps1", hint: "支持令牌池参数" },
  ],
  note: "安装前确认目标设备已获得书面授权。",
});

const BACKUP_SUMMARY = {
  statuses: { "待上传": 3, "已上传": 412, "失败": 1, "已删除": 58 },
  pending: 3, failed: 1, uploaded: 412, deleted: 58,
  enqueued: 474, verify_failed: 2, queue_full: 0, remote_deleted: 58, worker_errors: 3,
  queue_size: 3, worker_running: true, last_error: "",
};

function backupEntry(i) {
  const d = DEVICES[i % 3];
  const failed = i === 5;
  return {
    id: 900 - i, device_id: i + 1, device_uid: d.uid, device_name: d.name,
    frame_id: `4${100 + i}`, day: new Date(now() - i * 86_400_000).toISOString().slice(0, 10),
    remote_path: `obs-archive/${d.uid}/${new Date(now() - i * 86_400_000).toISOString().slice(0, 10)}/f${4100 + i}.jpg`,
    sha256: createHash("sha256").update(String(i)).digest("hex"),
    size_bytes: 240_000 + i * 1000, status: failed ? "失败" : i < 3 ? "待上传" : "已上传",
    attempts: failed ? 4 : 1, created_at_ms: now() - i * 86_400_000,
    uploaded_at_ms: failed ? null : now() - i * 86_400_000 + 60_000,
    error: failed ? "远端 503,重试 4 次仍失败" : "",
    local_available: i % 2 === 0, server_time_ms: now(),
  };
}

// ------------------------------------------------------------------- images ---
function svgFrame(uid, w, h) {
  const d = deviceByUid(uid);
  const base = hue(uid);
  const c1 = `hsl(${base} 70% 22%)`;
  const c2 = `hsl(${(base + 40) % 360} 65% 38%)`;
  const label = d ? d.name : uid;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/>
  </linearGradient></defs>
  <rect width="${w}" height="${h}" fill="url(#g)"/>
  <rect x="${w * 0.08}" y="${h * 0.12}" width="${w * 0.84}" height="${h * 0.7}" rx="${Math.max(8, w * 0.02)}" fill="rgba(255,255,255,0.10)"/>
  <rect x="${w * 0.08}" y="${h * 0.12}" width="${w * 0.84}" height="${h * 0.09}" rx="${Math.max(8, w * 0.02)}" fill="rgba(0,0,0,0.25)"/>
  <circle cx="${w * 0.115}" cy="${h * 0.165}" r="${Math.max(3, w * 0.008)}" fill="#fb7185"/>
  <circle cx="${w * 0.145}" cy="${h * 0.165}" r="${Math.max(3, w * 0.008)}" fill="#fbbf24"/>
  <circle cx="${w * 0.175}" cy="${h * 0.165}" r="${Math.max(3, w * 0.008)}" fill="#34d399"/>
  <text x="${w / 2}" y="${h * 0.5}" font-family="sans-serif" font-size="${Math.max(12, w * 0.045)}" fill="rgba(255,255,255,0.92)" text-anchor="middle" font-weight="600">${label}</text>
  <text x="${w / 2}" y="${h * 0.6}" font-family="monospace" font-size="${Math.max(10, w * 0.028)}" fill="rgba(255,255,255,0.55)" text-anchor="middle">${new Date().toISOString().replace("T", " ").slice(0, 19)}</text>
</svg>`;
}

// -------------------------------------------------------------------- files ---
const FILE_TREE = {
  "C:\\": [
    { name: "Users", is_dir: true }, { name: "Program Files", is_dir: true },
    { name: "Windows", is_dir: true }, { name: "pagefile.sys", is_dir: false, size: 8_589_934_592 },
    { name: "bootmgr", is_dir: false, size: 408_768 },
  ],
  "C:\\Users": [{ name: "operator", is_dir: true }, { name: "Public", is_dir: true }],
  "C:\\Users\\operator": [
    { name: "Documents", is_dir: true }, { name: "Downloads", is_dir: true },
    { name: "report-q3.docx", is_dir: false, size: 1_204_224 }, { name: "notes.txt", is_dir: false, size: 2_410 },
  ],
};

function fileListFor(p) {
  const driveEntries = [
    { name: "C:", path: "C:\\", is_dir: true, size: 0, device_time_ms: now(), hidden: false, drive: "C:", free: 128_849_018_880, filesystem: "NTFS" },
    { name: "D:", path: "D:\\", is_dir: true, size: 0, device_time_ms: now(), hidden: false, drive: "D:", free: 51_539_607_552, filesystem: "NTFS" },
  ];
  if (!p || p === "" || p === "drives") {
    return { path: "", parent: null, entries: [], drives: driveEntries, truncated: false, server_time_ms: now() };
  }
  const norm = p.endsWith("\\") && p.length > 3 ? p : p;
  const rows = FILE_TREE[norm] ?? FILE_TREE[`${norm}\\`] ?? [];
  const entries = rows.map((r) => ({
    name: r.name,
    path: norm.endsWith("\\") ? norm + r.name : `${norm}\\${r.name}`,
    is_dir: r.is_dir, size: r.size ?? 0, device_time_ms: now() - 86_400_000, hidden: false,
    drive: "", free: 0, filesystem: "",
  }));
  const parent = norm.includes("\\") && norm.length > 3 ? norm.replace(/\\[^\\]+\\?$/, "") || null : null;
  return { path: norm, parent: norm === "C:\\" ? "" : parent, entries, drives: [], truncated: false, server_time_ms: now() };
}

// -------------------------------------------------------------------- token ---
const TOKENS = new Set();
const INSTALL_TOKENS = [
  { id: 1, created_by: "operator", created_at_ms: BOOT_MS - 3_600_000, expires_at_ms: BOOT_MS + 1_500_000, used_at_ms: null, used_device_id: null, max_uses: 1, expired: false, used: false },
  { id: 2, created_by: "operator", created_at_ms: BOOT_MS - 86_400_000, expires_at_ms: BOOT_MS - 60_000, used_at_ms: BOOT_MS - 80_000, used_device_id: 12, max_uses: 1, expired: true, used: true },
];

// ------------------------------------------------------------------- server ---
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2", ".json": "application/json", ".map": "application/json" };

function serveStatic(req, res, pathname) {
  let file = path.join(DIST, decodeURIComponent(pathname));
  if (!file.startsWith(DIST)) { res.writeHead(403); res.end(); return; }
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(DIST, "index.html");
  }
  const ext = path.extname(file).toLowerCase();
  res.writeHead(200, { "Content-Type": MIME[ext] ?? "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}

const routes = [
  ["POST", /^\/api\/v1\/auth\/login$/, (req, res) => json(res, 200, env({ token: `mock-${Date.now()}`, expires_at_ms: now() + 86_400_000, version: VERSION }))],
  ["POST", /^\/api\/v1\/auth\/logout$/, (req, res) => json(res, 200, env({ ok: true, message: "已退出" }))],
  ["GET", /^\/api\/v1\/auth\/session$/, (req, res) => {
    const authed = Boolean(req.headers.authorization);
    json(res, 200, env({ authenticated: authed, totp_enabled: false, version: VERSION, console_host: "control.tianhw.top", base_urls: ["https://control.tianhw.top"] }));
  }],
  ["POST", /^\/api\/v1\/auth\/(totp\/setup|totp\/enable|totp\/disable|password)$/, (req, res, m) => {
    if (m[1] === "totp/setup") return json(res, 200, env({ secret: "MOCKSECRET234567", uri: "otpauth://totp/Observatory?secret=MOCKSECRET234567" }));
    json(res, 200, env({ ok: true, message: "已完成" }));
  }],
  ["GET", /^\/api\/v1\/public\/bootstrap$/, (req, res) => json(res, 200, env({ base_urls: ["https://control.tianhw.top"] }))],

  ["GET", /^\/api\/v1\/devices$/, (req, res) => json(res, 200, env({ devices: DEVICES, server_version: VERSION, channels: { ws: 3, poll: 1 }, server_time_ms: now() }))],
  ["GET", /^\/api\/v1\/devices\/([^/]+)\/frames\/([^/]+)$/, (req, res, m) => {
    const [, uid, idParam] = m;
    const d = deviceByUid(uid);
    const variant = new URL(req.url, "http://x").searchParams.get("variant") ?? "thumb";
    const isWatch = d?.os_name === "Android";
    const w = variant === "full" ? (isWatch ? 454 : 1920) : isWatch ? 227 : 480;
    const h = isWatch ? w : variant === "full" ? 1080 : 270;
    res.writeHead(200, { "Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=31536000, immutable" });
    res.end(svgFrame(uid, w, h));
  }],
  ["GET", /^\/api\/v1\/devices\/([^/]+)\/frames\/latest$/, (req, res, m) => {
    const list = FRAMES.get(m[1]);
    json(res, 200, env({ frame: list && list.length ? list[0] : null }));
  }],
  ["GET", /^\/api\/v1\/devices\/([^/]+)\/frames$/, (req, res, m) => {
    const list = FRAMES.get(m[1]) ?? [];
    const q = new URL(req.url, "http://x").searchParams;
    const limit = Number(q.get("limit") ?? 24), offset = Number(q.get("offset") ?? 0);
    json(res, 200, env({ frames: list.slice(offset, offset + limit), total: list.length, limit, offset }));
  }],
  ["DELETE", /^\/api\/v1\/devices\/([^/]+)\/frames$/, (req, res) => json(res, 200, env({ ok: true, message: "已清除" }))],
  ["GET", /^\/api\/v1\/devices\/([^/]+)\/timeline$/, (req, res, m) => {
    const list = FRAMES.get(m[1]) ?? [];
    const q = new URL(req.url, "http://x").searchParams;
    const from = Number(q.get("from_ms") ?? now() - 86_400_000), to = Number(q.get("to_ms") ?? now());
    const bucket = Number(q.get("bucket_ms") ?? 3_600_000);
    const buckets = [];
    for (let t = from; t <= to; t += bucket) {
      const inBucket = list.filter((f) => f.ts_ms >= t && f.ts_ms < t + bucket);
      if (inBucket.length) buckets.push({ ts_ms: t, device_time_ms: t, frame_id: inBucket[0].id, frame_count: inBucket.length, session: inBucket[0].session });
    }
    json(res, 200, env({ from_ms: from, to_ms: to, bucket_ms: bucket, buckets, total_frames: list.length }));
  }],

  ["GET", /^\/api\/v1\/devices\/([^/]+)\/files$/, (req, res, m) => {
    const p = new URL(req.url, "http://x").searchParams.get("path") ?? "";
    json(res, 200, env(fileListFor(p)));
  }],
  ["POST", /^\/api\/v1\/devices\/([^/]+)\/files\/op$/, (req, res) => json(res, 200, env({ ok: true, op: "op", data: {}, error: null, elapsed_ms: 42 }))],
  ["GET", /^\/api\/v1\/devices\/([^/]+)\/files\/raw$/, (req, res) => {
    res.writeHead(200, { "Content-Type": "application/octet-stream", "X-File-Size": "2410", "X-Chunk-Offset": "0", "X-Eof": "1" });
    res.end("mock file content: observatory\n");
  }],
  ["PUT", /^\/api\/v1\/devices\/([^/]+)\/files\/raw$/, (req, res) => json(res, 200, env({ ok: true, op: "write", data: {}, error: null, elapsed_ms: 88 }))],

  ["POST", /^\/api\/v1\/devices\/([^/]+)\/control$/, (req, res, m) => json(res, 200, env({
    token: `ctl-${m[1]}`, expires_at_ms: now() + 300_000, fps: 8, quality: 70, ws_path: `/api/v1/devices/${m[1]}/live`, ws_bases: [], server_time_ms: now(),
  }))],
  ["DELETE", /^\/api\/v1\/devices\/([^/]+)\/control\/[^/]+$/, (req, res) => json(res, 200, env({ ok: true, message: "已撤销" }))],

  ["POST", /^\/api\/v1\/devices\/([^/]+)\/commands$/, (req, res) => {
    const body = req.body ?? {};
    json(res, 200, env({ id: `cmd-${Date.now() % 100000}`, type: body.type ?? "unknown", kind: body.type ?? "unknown", status: "pending", payload: body.payload ?? {}, result: null, error: null, error_code: null, deadline_ms: 15_000, attempts: 0, created_at_ms: now(), dispatched_at_ms: null, finished_at_ms: null }));
  }],
  ["GET", /^\/api\/v1\/devices\/([^/]+)\/commands$/, (req, res) => json(res, 200, env({ commands: COMMANDS }))],
  ["POST", /^\/api\/v1\/devices\/([^/]+)\/uninstall$/, (req, res) => json(res, 200, env({ command_id: "cmd-uninst", status: "pending", message: "卸载指令已下发" }))],
  ["GET", /^\/api\/v1\/devices\/([^/]+)$/, (req, res, m) => {
    const d = deviceByUid(m[1]);
    if (!d) return json(res, 404, { code: "GONE", message: "设备不存在" });
    json(res, 200, env({ device: d }));
  }],
  ["PATCH", /^\/api\/v1\/devices\/([^/]+)$/, (req, res, m) => {
    const d = deviceByUid(m[1]);
    if (d && req.body?.name) d.name = req.body.name;
    if (d && req.body?.note !== undefined) d.note = req.body.note;
    json(res, 200, env({ device: d }));
  }],
  ["DELETE", /^\/api\/v1\/devices\/([^/]+)$/, (req, res) => json(res, 200, env({ ok: true, message: "已删除" }))],

  ["GET", /^\/api\/v1\/settings$/, (req, res) => {
    if (req.method === "PATCH") { /* handled below */ }
    json(res, 200, env({ values: SETTING_VALUES, specs: SETTING_SPECS, server_version: VERSION, console_host: "control.tianhw.top", base_urls: ["https://control.tianhw.top"] }));
  }],
  ["PATCH", /^\/api\/v1\/settings$/, (req, res) => {
    Object.assign(SETTING_VALUES, req.body?.values ?? {});
    json(res, 200, env({ values: SETTING_VALUES, specs: SETTING_SPECS, server_version: VERSION, pushed_to: 4 }));
  }],
  ["GET", /^\/api\/v1\/settings\/effective\/([^/]+)$/, (req, res, m) => json(res, 200, env({ uid: m[1], settings: SETTING_VALUES }))],

  ["GET", /^\/api\/v1\/audit$/, (req, res) => {
    const q = new URL(req.url, "http://x").searchParams;
    const limit = Number(q.get("limit") ?? 20), offset = Number(q.get("offset") ?? 0);
    json(res, 200, env({ entries: AUDIT.slice(offset, offset + limit), total: AUDIT.length }));
  }],

  ["GET", /^\/api\/v1\/health$/, (req, res) => json(res, 200, { ok: true, product: "observatory", version: VERSION, server_time_ms: now(), uptime_s: Math.floor((now() - BOOT_MS) / 1000), carriers: { ws: 3, poll: 1 } })],
  ["GET", /^\/api\/v1\/system$/, (req, res) => json(res, 200, env({
    version: VERSION, uptime_s: Math.floor((now() - BOOT_MS) / 1000) + 86_400 * 3,
    devices: DEVICES.length, frames: 41_820, commands: 12_904, pending_commands: 1,
    channels: { ws: 3, poll: 1 }, control_sessions: 0,
    storage: { frames_bytes: 7_516_192_768, thumbs_bytes: 812_000_000, db_bytes: 204_000_000, disk_total: 42_949_672_960, disk_free: 9_663_676_416 },
    base_urls: ["https://control.tianhw.top", "https://backup.tianhw.top"], console_host: "control.tianhw.top",
    install: INSTALL_INFO(),
  }))],

  ["GET", /^\/api\/v1\/updates$/, (req, res) => json(res, 200, env({
    server_version: VERSION, latest_agent_version: "0.3.6",
    outdated: DEVICES.filter((d) => d.agent_version !== "0.3.6" && d.presence.state !== "uninstalling").map((d) => ({ uid: d.uid, name: d.name, version: d.agent_version })),
    packages: PACKAGES,
  }))],
  ["POST", /^\/api\/v1\/updates\/agent$/, (req, res) => json(res, 200, env(PACKAGES[0]))],
  ["DELETE", /^\/api\/v1\/updates\/agent\/([^/]+)$/, (req, res) => json(res, 200, env({ ok: true, message: "已删除" }))],

  ["GET", /^\/api\/v1\/install$/, (req, res) => json(res, 200, env(INSTALL_INFO()))],
  ["GET", /^\/api\/v1\/install-tokens$/, (req, res) => json(res, 200, env({ tokens: INSTALL_TOKENS }))],
  ["POST", /^\/api\/v1\/install-tokens$/, (req, res) => json(res, 200, env({
    id: INSTALL_TOKENS.length + 1, token: `obt-${Date.now().toString(36)}`, expires_at_ms: now() + 1_800_000, max_uses: 1,
    install_cmd_url: "/api/v1/install/setup.cmd", install_ps1_url: "/api/v1/install/setup.ps1",
  }))],
  ["DELETE", /^\/api\/v1\/install-tokens\/(\d+)$/, (req, res) => json(res, 200, env({ ok: true, message: "已撤销" }))],

  ["GET", /^\/api\/v1\/backup$/, (req, res) => json(res, 200, env({
    configured: true, archive_enabled: true, repo: "github.com/H2O-ME/observatory-archive", branch: "main",
    summary: BACKUP_SUMMARY, recent_failures: [backupEntry(5)], statuses: ["待上传", "已上传", "失败", "已删除"],
  }))],
  ["GET", /^\/api\/v1\/backup\/entries\/(\d+)\/file$/, (req, res) => { res.writeHead(200, { "Content-Type": "image/svg+xml" }); res.end(svgFrame("D-2c81e4", 960, 540)); }],
  ["POST", /^\/api\/v1\/backup\/entries\/(\d+)\/retrieve$/, (req, res, m) => json(res, 200, { ok: true, message: "已取回", frame_id: 4100 + Number(m[1]), server_time_ms: now() })],
  ["GET", /^\/api\/v1\/backup\/entries\/(\d+)$/, (req, res, m) => json(res, 200, env({ entry: { ...backupEntry(Number(m[1])), frame: { id: 4102, ts_ms: now() - 86_400_000, device_time_ms: now() - 86_400_000, width: 1920, height: 1080, size_bytes: 242_000, sha256: "aa".repeat(20) } } }))],
  ["DELETE", /^\/api\/v1\/backup\/entries\/(\d+)$/, (req, res) => json(res, 200, env({ ok: true, message: "已删除" }))],
  ["GET", /^\/api\/v1\/backup\/entries$/, (req, res) => {
    const q = new URL(req.url, "http://x").searchParams;
    const limit = Number(q.get("limit") ?? 20), offset = Number(q.get("offset") ?? 0);
    json(res, 200, env({ entries: Array.from({ length: 18 }, (_, i) => backupEntry(offset + i)).filter(Boolean), total: 474, limit, offset, statuses: ["待上传", "已上传", "失败", "已删除"] }));
  }],
];

http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  const pathname = url.pathname;

  if (pathname.startsWith("/api/")) {
    // Collect JSON bodies so PATCH/POST handlers can read them.
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      try { body = raw ? JSON.parse(raw) : null; } catch { body = null; }
      req.body = body;
      if (pathname.endsWith("/ws") || pathname.includes("/live")) {
        return json(res, 400, { code: "UNSUPPORTED", message: "mock: websocket not implemented" });
      }
      for (const [method, pattern, handler] of routes) {
        if (req.method !== method) continue;
        const m = pathname.match(pattern);
        if (m) {
          try {
            return handler(req, res, m);
          } catch (error) {
            console.error(`[mock] handler failed for ${req.method} ${pathname}:`, error);
            return json(res, 500, { code: "INTERNAL", message: `mock handler error: ${error}` });
          }
        }
      }
      json(res, 404, { code: "BAD_REQUEST", message: `mock: no route for ${req.method} ${pathname}` });
    });
    return;
  }

  serveStatic(req, res, pathname === "/" ? "/index.html" : pathname);
}).listen(PORT, () => {
  console.log(`[mock] observatory console on http://127.0.0.1:${PORT}  (dist: ${DIST})`);
});
