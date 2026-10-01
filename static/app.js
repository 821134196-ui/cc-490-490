/* 公交场站轮椅坡板点检与放行系统 —— 前端 */
"use strict";

const state = {
  board: null,
  role: "driver",
  staffId: null,
  tab: "board",
  pickedTripId: null,
  pickedVehicleId: null,
  check: {},            // vehicleId -> {item: true/false}
  rampForm: {},         // vehicleId -> {deploy_seconds, retract_seconds, cycles}
  note: "",
  photos: [],
};

const ITEM_META = {
  appearance:     { icon: "🪟", name: "坡板外观",     where: "ramp" },
  deploy_retract: { icon: "🔄", name: "展开 / 收回",  where: "hinge" },
  locking:        { icon: "🔒", name: "锁止到位",     where: "lock" },
  call_button:    { icon: "🔔", name: "车内呼叫按钮", where: "button" },
  pin_present:    { icon: "📌", name: "固定销齐全",   where: "pin" },
  load_label:     { icon: "🏷️", name: "承重标识完好", where: "label" },
};
const ITEM_ORDER = ["appearance", "deploy_retract", "locking", "call_button", "pin_present", "load_label"];

const FAULT_META = {
  RAMP_JAM:             { icon: "🔄", label: "坡板卡滞" },
  PIN_MISSING:          { icon: "📌", label: "固定销缺失" },
  BUTTON_DEAD:          { icon: "🔕", label: "呼叫按钮无响应" },
  LOAD_LABEL_LOST:      { icon: "🏷️", label: "承重标识脱落" },
  DRIVER_MISSED_CHECK:  { icon: "⏰", label: "司机漏检" },
  REPAIR_DELAY:         { icon: "🛠️", label: "维修延期" },
  MISRELEASE:           { icon: "🚦", label: "误放行" },
};

/* ---------------- 基础 ---------------- */
const $ = (sel) => document.querySelector(sel);
const view = () => $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, staff_id: state.staffId }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || `请求失败 ${res.status}`);
    err.code = data.error;
    throw err;
  }
  return data;
}

async function loadBoard() {
  const res = await fetch("/api/board");
  state.board = await res.json();
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  t.hidden = false;
  clearTimeout(toast._tm);
  toast._tm = setTimeout(() => { t.hidden = true; }, 3200);
}

function fmtTime(iso) {
  if (!iso) return "—";
  return iso.slice(11, 16);
}
function fmtDateTime(iso) {
  if (!iso) return "—";
  return iso.slice(0, 16).replace("T", " ");
}
function staffName(id) {
  const s = (state.board.staff || []).find((x) => x.id === id);
  return s ? s.name : id;
}
function rampLabel(code) {
  return (state.board.enums.ramp_types[code]) || code;
}
function vehStatusBadge(st) {
  const map = state.board.enums.vehicle_status;
  const cls = st === "IN_SERVICE" ? "b-ok"
    : st === "UNDER_REPAIR" ? "b-bad"
    : st === "RAMP_DISABLED" ? "b-warn" : "b-gray";
  return `<span class="badge ${cls}">${esc(map[st] || st)}</span>`;
}

/* ---------------- 角色 ---------------- */
function setupChrome() {
  document.querySelectorAll("#roleTabs button").forEach((btn) => {
    btn.onclick = () => {
      state.role = btn.dataset.role;
      document.querySelectorAll("#roleTabs button").forEach((b) =>
        b.classList.toggle("active", b === btn));
      fillStaff();
      render();
    };
  });
  document.querySelectorAll("#mainTabs button").forEach((btn) => {
    btn.onclick = () => {
      state.tab = btn.dataset.tab;
      document.querySelectorAll("#mainTabs button").forEach((b) =>
        b.classList.toggle("active", b === btn));
      render();
    };
  });
  $("#staffSelect").onchange = (e) => { state.staffId = e.target.value; render(); };
}

function fillStaff() {
  const sel = $("#staffSelect");
  const list = (state.board.staff || []).filter((s) => s.role === state.role);
  sel.innerHTML = list.map((s) => `<option value="${s.id}">${esc(s.name)}（${s.id}）</option>`).join("");
  if (!list.find((s) => s.id === state.staffId)) state.staffId = list[0]?.id || null;
  sel.value = state.staffId;
}

function roleGuard(allowedRoles) {
  if (!allowedRoles.includes(state.role)) {
    const name = { driver: "驾驶员", dispatcher: "调度员", mechanic: "维修人员" };
    return `<div class="roleguard">⛔ 职责分离：此操作仅限 ${allowedRoles
      .map((r) => name[r]).join(" / ")} 执行。当前为${name[state.role]}身份，后端同样会拒绝。</div>`;
  }
  return "";
}

/* ---------------- 提醒条 ---------------- */
function renderAlerts() {
  const strip = $("#alertStrip");
  const alerts = (state.board.live_reminders && state.board.live_reminders.length)
    ? state.board.live_reminders
    : (state.board.reminders || []);
  if (!alerts.length) { strip.hidden = true; strip.innerHTML = ""; return; }
  strip.innerHTML = alerts.map((a) =>
    `<div class="a-item">${a.kind.includes("OVERDUE") || a.kind.includes("NO_CHECK") || a.kind.includes("MISSED")
      ? "🔴 <b></b>" : "🟠 "}${esc(a.message)}</div>`).join("");
  strip.hidden = false;
}

/* ---------------- 1. 待发板 ---------------- */
function rampDot(trip) {
  const insp = trip.inspection;
  if (!insp) return `<span class="ramp-dot dot-none"><i></i>未检</span>`;
  if (trip.vehicle_status === "UNDER_REPAIR")
    return `<span class="ramp-dot dot-down"><i></i>待修</span>`;
  if (trip.vehicle_status === "RAMP_DISABLED")
    return `<span class="ramp-dot dot-down"><i></i>停用</span>`;
  return insp.result === "PASS"
    ? `<span class="ramp-dot dot-ok"><i></i>合格</span>`
    : `<span class="ramp-dot dot-fail"><i></i>不合格</span>`;
}

function depClass(trip) {
  const dep = new Date(trip.departure_at);
  const mins = (dep - new Date(state.board.now)) / 60000;
  if (mins < 0) return "late";
  if (mins <= 15) return "soon";
  return "";
}

function renderBoard() {
  const b = state.board;
  const trips = b.trips;
  const released = trips.filter((t) => t.release).length;
  const blocked = trips.filter((t) => t.vehicle_status !== "IN_SERVICE" ||
    (t.inspection && t.inspection.result !== "PASS")).length;
  const unchecked = trips.filter((t) => !t.inspection).length;

  return `
  <div class="kpi-row">
    <div class="kpi"><div class="k-num">${b.shift ? esc(b.shift.label) : "—"}</div><div class="k-lbl">当前班次</div></div>
    <div class="kpi"><div class="k-num green">${released}</div><div class="k-lbl">已放行</div></div>
    <div class="kpi"><div class="k-num red">${blocked}</div><div class="k-lbl">故障/不合格阻断</div></div>
    <div class="kpi"><div class="k-num orange">${unchecked}</div><div class="k-lbl">待点检</div></div>
    <div class="kpi"><div class="k-num orange">${b.spares_available.length}/${b.spares_total}</div><div class="k-lbl">可用无障碍备用车</div></div>
  </div>
  <div class="panel">
    <div class="board-head">
      <h2>线路待发板 <span class="tag">按线路与发车时间排序</span></h2>
      <div class="legend">
        <span><i style="background:var(--ok)"></i>坡板合格</span>
        <span><i style="background:var(--bad)"></i>不合格</span>
        <span><i style="background:var(--warn)"></i>停用/待修</span>
        <span><i style="background:#fff;border:2px solid var(--gray)"></i>未点检</span>
      </div>
    </div>
    <table class="dense">
      <thead><tr>
        <th>发车</th><th>线路</th><th>车辆 / 坡板状态</th><th>驾驶员</th>
        <th>外观</th><th>展开</th><th>锁止</th><th>呼叫</th><th>销/标识</th>
        <th>状态</th><th>操作</th>
      </tr></thead>
      <tbody>
      ${trips.map((t) => {
        const i = t.inspection;
        const mk = (k) => !i ? `<span class="muted">·</span>`
          : i[k] ? `<span style="color:var(--ok);font-weight:800">✓</span>`
                 : `<span style="color:var(--bad);font-weight:800">✗</span>`;
        const st = t.release
          ? `<span class="badge b-ok">已放行</span>`
          : t.status === "PLANNED"
            ? (i && i.result === "PASS" && t.vehicle_status === "IN_SERVICE"
                ? `<span class="badge b-blue">待放行</span>`
                : `<span class="badge b-warn">不可放行</span>`)
            : `<span class="badge b-gray">${esc(t.status)}</span>`;
        const canGo = !t.release && i && i.result === "PASS" && t.vehicle_status === "IN_SERVICE";
        return `<tr>
          <td class="dep-time ${depClass(t)}">${fmtTime(t.departure_at)}</td>
          <td><span class="line-chip ${t.line_code === "K3" ? "k3" : ""}">${esc(t.line_code)}</span></td>
          <td><span class="fleet">${esc(t.fleet_no)}<small>${esc(t.plate)} · ${esc(rampLabel(t.ramp_type))}</small></span>
              <span style="margin-left:6px">${rampDot(t)}</span></td>
          <td>${esc(t.driver_name)}</td>
          <td>${mk("appearance")}</td><td>${mk("deploy_retract")}</td>
          <td>${mk("locking")}</td><td>${mk("call_button")}</td>
          <td>${mk("pin_present")}${mk("load_label")}</td>
          <td>${st} ${vehStatusBadge(t.vehicle_status)}</td>
          <td>
            <button class="btn" onclick="pickTripForInspect('${t.id}')">点检</button>
            ${state.role === "dispatcher"
              ? (canGo
                  ? `<button class="btn orange" onclick="doRelease('${t.id}')">放行</button>`
                  : `<button class="btn" onclick="pickSwap('${t.id}')">换车</button>`)
              : ""}
          </td>
        </tr>`;
      }).join("")}
      </tbody>
    </table>
    <p class="muted" style="margin-bottom:0;margin-top:10px">
      规则：外观、展开/收回、锁止、呼叫按钮四项全部合格且车辆在役才可放行；同一车辆同一班次只有一份有效点检；放行按班次任务幂等，重复点击只成功一次，门控只开一次。
    </p>
  </div>`;
}

window.pickTripForInspect = (tripId) => {
  state.pickedTripId = tripId;
  const t = state.board.trips.find((x) => x.id === tripId);
  state.pickedVehicleId = t.vehicle_id;
  switchTab("inspect");
};
window.pickSwap = (tripId) => {
  state.pickedTripId = tripId;
  switchTab("spare");
};
window.doRelease = async (tripId) => {
  try {
    const out = await api("/api/releases", { trip_id: tripId });
    if (out.duplicate) toast("该班次任务已放行 —— 幂等返回，门控未再次开启", "ok");
    else toast(`放行成功：门控 ${out.gate_signal}；终端 ${out.terminal_signal}`, "ok");
    await refresh();
  } catch (e) { toast("⛔ " + e.message, "err"); }
};

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll("#mainTabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  render();
}

/* ---------------- 2. 车辆点检（侧视车辆 + 大字号） ---------------- */
function vehicleSvg() {
  const c = (v) => state.check[state.pickedVehicleId]?.[v];
  const color = (v) => c(v) === true ? "#1a8a4c" : c(v) === false ? "#c62f2f" : "#9aa7b4";
  const hot = (v, x, y, label, dy = -14) => `
    <g class="hotspot" onclick="toggleCheck('${v}')">
      <circle class="hs-ring" cx="${x}" cy="${y}" r="13" fill="#fff" stroke="${color(v)}" stroke-width="3"/>
      <text x="${x}" y="${y + 5}" text-anchor="middle" font-size="14" font-weight="800"
        fill="${color(v)}">${c(v) === true ? "✓" : c(v) === false ? "✗" : "?"}</text>
      <text class="hs-label" x="${x}" y="${y + dy}" text-anchor="middle">${label}</text>
    </g>`;
  return `
  <svg viewBox="0 0 760 250" class="vehicle-stage" width="100%" role="img" aria-label="车辆侧视图">
    <!-- 车身 -->
    <rect x="40" y="40" width="680" height="120" rx="16" fill="#ffffff" stroke="#0b5fa5" stroke-width="3"/>
    <rect x="40" y="40" width="680" height="34" rx="16" fill="#0b5fa5"/>
    <rect x="40" y="60" width="680" height="14" fill="#0b5fa5"/>
    <!-- 车窗 -->
    ${[70, 160, 250, 430, 520, 610].map((x) =>
      `<rect x="${x}" y="86" width="70" height="46" rx="6" fill="#cfe4f6" stroke="#7fb0d8"/>`).join("")}
    <!-- 中门（坡板位） -->
    <rect x="330" y="82" width="86" height="78" rx="4" fill="#e9f1f8" stroke="#0b5fa5" stroke-width="2" stroke-dasharray="5 3"/>
    <line x1="373" y1="82" x2="373" y2="160" stroke="#0b5fa5" stroke-width="2"/>
    <!-- 车轮 -->
    <circle cx="170" cy="170" r="28" fill="#26333f"/><circle cx="170" cy="170" r="13" fill="#8fa0b2"/>
    <circle cx="590" cy="170" r="28" fill="#26333f"/><circle cx="590" cy="170" r="13" fill="#8fa0b2"/>
    <!-- 展开的坡板 -->
    <polygon points="332,160 414,160 470,226 300,226" fill="#f28c28" opacity="0.9" stroke="#d9730f" stroke-width="2"/>
    <line x1="330" y1="162" x2="412" y2="162" stroke="#7a4208" stroke-width="4"/>
    <!-- 路沿 -->
    <line x1="40" y1="228" x2="720" y2="228" stroke="#9aa7b4" stroke-width="3" stroke-dasharray="10 6"/>
    <!-- 热点：板面/铰链/锁销/标识 在坡板，按钮在车内门旁 -->
    ${hot("appearance", 392, 200, "坡板外观")}
    ${hot("deploy_retract", 412, 158, "展开/收回", -16)}
    ${hot("locking", 322, 150, "锁止", -15)}
    ${hot("pin_present", 432, 158, "固定销", -30)}
    ${hot("load_label", 348, 214, "承重标识", 26)}
    ${hot("call_button", 424, 104, "呼叫按钮", -14)}
    <text x="60" y="62" font-size="15" font-weight="700" fill="#fff">♿ 无障碍客车 侧视图（点击坡板与按钮位置逐项点检）</text>
  </svg>`;
}

window.toggleCheck = (item) => {
  const vid = state.pickedVehicleId;
  if (!vid) return toast("请先选择车辆", "err");
  state.check[vid] = state.check[vid] || {};
  const cur = state.check[vid][item];
  state.check[vid][item] = cur === true ? false : true;  // 合格↔不合格
  render();
};

function renderInspect() {
  const guard = roleGuard(["driver"]);
  const b = state.board;
  const trips = b.trips.filter((t) => t.status !== "DEPARTED");
  const pickBtns = trips.map((t) => {
    const active = state.pickedTripId === t.id;
    return `<button class="pick ${active ? "active" : ""}" onclick="pickTrip('${t.id}')">
      <span class="line-chip ${t.line_code === "K3" ? "k3" : ""}">${esc(t.line_code)}</span>
      ${fmtTime(t.departure_at)} · ${esc(t.fleet_no)}
      <small>${esc(t.driver_name)} · ${esc(rampLabel(t.ramp_type))}</small></button>`;
  }).join("");

  let body;
  const t = trips.find((x) => x.id === state.pickedTripId);
  if (!t) {
    body = `<p class="muted">请从上方选择一个待发班次开始点检。也可在“待发板”点击某行的【点检】。</p>`;
  } else {
    const vid = t.vehicle_id;
    state.check[vid] = state.check[vid] || {};
    state.rampForm[vid] = state.rampForm[vid] || { deploy_seconds: "", retract_seconds: "", cycles: 2 };
    const ck = state.check[vid];
    const rf = state.rampForm[vid];
    const existing = t.inspection;
    const cards = ITEM_ORDER.map((k) => {
      const v = ck[k];
      const cls = v === true ? "pass" : v === false ? "fail" : "pending";
      const stTxt = v === true ? "✓ 合格" : v === false ? "✗ 不合格" : "待检查";
      return `<div class="check-card ${cls}" onclick="toggleCheck('${k}')" id="card-${k}">
        <span class="cc-icon">${ITEM_META[k].icon}</span>
        <span class="cc-name">${ITEM_META[k].name}</span>
        <span class="cc-state">${stTxt}</span></div>`;
    }).join("");
    const photos = existing ? JSON.parse(existing.photos || "[]") : [];
    body = `
      ${existing ? `<div class="note-box">📄 本车本班次已存在一份<strong>${existing.result === "PASS" ? "合格" : "不合格"}</strong>点检（${existing.round > 1 ? "第" + existing.round + "次/复检" : "初检"}，${fmtDateTime(existing.finalized_at)}）。
        同车同班只允许一份有效点检；维修后复检由“复检”页发起，系统会自动作废旧的不合格单。</div>` : ""}
      ${vehicleSvg()}
      <div class="check-grid">${cards}</div>
      <div class="row-flex" style="margin-top:14px">
        <div class="panel" style="margin:0">
          <h2>坡板展开/收回计时 <span class="tag">测试数据随点检归档</span></h2>
          <div class="row-flex">
            <div class="field" style="flex:1"><label>展开耗时（秒）</label>
              <input type="number" step="0.1" min="0" value="${rf.deploy_seconds}"
                onchange="setRamp('deploy_seconds',this.value)"></div>
            <div class="field" style="flex:1"><label>收回耗时（秒）</label>
              <input type="number" step="0.1" min="0" value="${rf.retract_seconds}"
                onchange="setRamp('retract_seconds',this.value)"></div>
            <div class="field" style="flex:1"><label>往复次数</label>
              <input type="number" min="1" value="${rf.cycles}" onchange="setRamp('cycles',this.value)"></div>
          </div>
          <p class="hint">电动/半自动坡板参考：≤10 秒为顺畅；超过 15 秒或中途卡滞判不合格。</p>
        </div>
        <div class="panel" style="margin:0">
          <h2>备注与测试照片 <span class="tag">本地目录 data/photos</span></h2>
          <div class="field"><textarea id="inspNote" placeholder="例：展开至 60% 卡滞，需人工助力">${esc(state.note)}</textarea></div>
          <div class="field">
            <input type="file" id="photoInput" accept="image/png,image/jpeg" onchange="uploadPhoto()">
            <div class="hint">已存档照片（演示）：${photos.length ? photos.map((p) => `<span class="sig">${esc(p)}</span>`).join(" ") : "无"}</div>
          </div>
        </div>
      </div>
      <div class="btn-row">
        <button class="btn primary big" onclick="submitInspection()">✔ 提交点检（驾驶员 ${esc(staffName(state.staffId))}）</button>
        <button class="btn" onclick="setAllPass()">全部合格（一键）</button>
      </div>`;
  }
  return `
  <div class="driver-mode">
    ${guard}
    <div class="panel">
      <h2>驾驶员出场前点检 <span class="tag">坡板外观 · 展开收回 · 锁止 · 呼叫按钮 · 固定销 · 承重标识</span></h2>
      <div class="pick-line">${pickBtns || `<span class="muted">暂无待发班次</span>`}</div>
    </div>
    ${t ? `<div class="panel">${body}</div>` : ""}
  </div>`;
}

window.pickTrip = (tripId) => {
  state.pickedTripId = tripId;
  const t = state.board.trips.find((x) => x.id === tripId);
  state.pickedVehicleId = t.vehicle_id;
  state.note = "";
  render();
};
window.setRamp = (k, v) => {
  const vid = state.pickedVehicleId;
  state.rampForm[vid] = state.rampForm[vid] || {};
  state.rampForm[vid][k] = v;
};
window.setAllPass = () => {
  const vid = state.pickedVehicleId;
  const ck = {};
  ITEM_ORDER.forEach((k) => (ck[k] = true));
  state.check[vid] = ck;
  const rf = state.rampForm[vid] || {};
  if (rf.deploy_seconds === "") rf.deploy_seconds = 6.5;
  if (rf.retract_seconds === "") rf.retract_seconds = 5.5;
  render();
};
window.uploadPhoto = async () => {
  const file = $("#photoInput").files[0];
  if (!file) return;
  const data = await new Promise((r) => {
    const fr = new FileReader();
    fr.onload = () => r(fr.result.split(",")[1]);
    fr.readAsDataURL(file);
  });
  try {
    await api("/api/photos", { file: file.name, data });
    toast("照片已存入 data/photos", "ok");
  } catch (e) { toast(e.message, "err"); }
};

window.submitInspection = async () => {
  const t = state.board.trips.find((x) => x.id === state.pickedTripId);
  if (!t) return toast("请先选择班次", "err");
  const vid = t.vehicle_id;
  const ck = state.check[vid] || {};
  const pending = ITEM_ORDER.filter((k) => ck[k] === undefined);
  if (pending.length) return toast("还有未检查项：" + pending.map((k) => ITEM_META[k].name).join("、"), "err");
  const rf = state.rampForm[vid] || {};
  const items = {};
  ITEM_ORDER.forEach((k) => (items[k] = !!ck[k]));
  const ramp_test = {
    deploy_seconds: parseFloat(rf.deploy_seconds) || null,
    retract_seconds: parseFloat(rf.retract_seconds) || null,
    cycles: parseInt(rf.cycles || "1", 10),
    result: items.deploy_retract ? "PASS" : "FAIL",
  };
  try {
    const out = await api("/api/inspections", {
      shift_id: state.board.shift.id, vehicle_id: vid, trip_id: t.id,
      items, ramp_test, note: $("#inspNote")?.value || "",
    });
    toast(out.result === "PASS" ? "点检合格，可由调度员放行" : "点检不合格，车辆将按流程停用/报修",
      out.result === "PASS" ? "ok" : "err");
    await refresh();
  } catch (e) {
    toast("⛔ " + e.message + (e.code === "DUPLICATE_INSPECTION" ? "" : ""), "err");
  }
};

/* ---------------- 3. 坡板测试记录 ---------------- */
function renderRamp() {
  const tests = state.board.ramp_tests;
  const meter = (sec, ok) => {
    const w = Math.max(4, Math.min(100, (Number(sec || 0) / 20) * 100));
    return `<span class="test-meter"><span class="bar ${ok ? "ok" : ""}" style="width:${w}px"></span>${sec ?? "—"} s</span>`;
  };
  return `<div class="panel">
    <h2>坡板展开 / 收回测试记录 <span class="tag">随每次点检与复检归档</span></h2>
    <p class="section-note">电动、半自动坡板须记录展开与收回耗时及往复次数；出现卡滞、异响、超时即判不合格并转故障停用。</p>
    <table class="dense">
      <thead><tr><th>时间</th><th>车辆</th><th>轮次</th><th>展开</th><th>收回</th><th>往复</th><th>结果</th><th>说明</th></tr></thead>
      <tbody>${tests.map((x) => `<tr>
        <td>${fmtDateTime(x.created_at)}</td>
        <td class="fleet">${esc(fleetOf(x.vehicle_id))}</td>
        <td>${x.round > 1 ? `复检#${x.round}` : "初检"}</td>
        <td>${meter(x.deploy_seconds, x.result === "PASS")}</td>
        <td>${meter(x.retract_seconds, x.result === "PASS")}</td>
        <td>${x.cycles} 次</td>
        <td>${x.result === "PASS" ? `<span class="badge b-ok">通过</span>` : `<span class="badge b-bad">失败</span>`}</td>
        <td>${esc(x.detail || "")}</td>
      </tr>`).join("") || `<tr><td colspan="8" class="muted">暂无测试记录</td></tr>`}</tbody>
    </table>
  </div>`;
}
function fleetOf(vid) {
  return state.board.vehicles.find((v) => v.id === vid)?.fleet_no || vid;
}

/* ---------------- 4. 故障停用 ---------------- */
function renderFault() {
  const b = state.board;
  const openFaults = b.faults.filter((f) => f.status !== "RESOLVED" || f.consequence !== "NONE");
  const guard = roleGuard(["driver", "dispatcher"]);
  const trips = b.trips.filter((t) => t.status !== "DEPARTED");
  const faultCards = openFaults.map((f) => {
    const meta = FAULT_META[f.fault_type] || { icon: "❗", label: f.fault_type };
    const cons = f.consequence === "VEHICLE_DOWN"
      ? `<span class="badge b-bad">整车待修</span>`
      : f.consequence === "RAMP_DISABLED"
        ? `<span class="badge b-warn">无障碍功能停用</span>`
        : `<span class="badge b-gray">记录留痕</span>`;
    const st = f.status === "RESOLVED" ? `<span class="badge b-ok">已闭环</span>`
      : f.status === "REPAIRING" ? `<span class="badge b-blue">维修中</span>`
      : `<span class="badge b-orange">待处理</span>`;
    return `<div class="item-card ${f.consequence === "VEHICLE_DOWN" ? "blocked" : "warn"}">
      <div class="ic-top">
        <span class="ic-title">${meta.icon} ${esc(meta.label)} · ${esc(fleetOf(f.vehicle_id))} ${st}</span>
        <span>${cons}</span>
      </div>
      <div class="ic-meta">
        <span>故障单 <span class="sig">${esc(f.id)}</span></span>
        <span>报修：${esc(staffName(f.reported_by))}</span>
        <span>${fmtDateTime(f.created_at)}</span>
        ${f.repair_id ? `<span>维修单 <span class="sig">${esc(f.repair_id)}</span></span>` : ""}
      </div>
      ${f.description ? `<div class="ic-meta"><span>📝 ${esc(f.description)}</span></div>` : ""}
    </div>`;
  }).join("");

  return `
  ${guard}
  <div class="row-flex">
    <div class="panel" style="flex:2">
      <h2>故障与停用记录 <span class="tag">八类情形全部留痕</span></h2>
      ${faultCards || `<p class="muted">暂无故障</p>`}
    </div>
    <div class="panel" style="flex:1;min-width:280px">
      <h2>登记故障</h2>
      <div class="field"><label>所属班次任务</label>
        <select id="fTrip">${trips.map((t) => `<option value="${t.id}">${esc(t.line_code)} ${fmtTime(t.departure_at)} · ${esc(t.fleet_no)}</option>`).join("")}</select></div>
      <div class="field"><label>故障类型</label>
        <select id="fType">${Object.entries(FAULT_META).map(([k, v]) =>
          `<option value="${k}">${v.icon} ${v.label}</option>`).join("")}</select></div>
      <div class="field"><label>处置后果</label>
        <select id="fCons">
          <option value="RAMP_DISABLED">无障碍功能停用（坡板类故障）</option>
          <option value="VEHICLE_DOWN">整车待修（呼叫/结构等）</option>
          <option value="NONE">仅留痕（漏检/误放行/延期）</option>
        </select></div>
      <div class="field"><label>描述</label><textarea id="fDesc" placeholder="故障现象"></textarea></div>
      <button class="btn danger" onclick="reportFault()" ${state.role === "mechanic" ? "disabled" : ""}>登记故障并停用</button>
      <p class="hint">线路要求无障碍时，停用后须到“备用车调配”换车；维修完成后在“复检”页由驾驶员复检。</p>
    </div>
  </div>`;
}

window.reportFault = async () => {
  const tripId = $("#fTrip").value;
  const t = state.board.trips.find((x) => x.id === tripId);
  try {
    await api("/api/faults", {
      vehicle_id: t.vehicle_id, trip_id: tripId,
      fault_type: $("#fType").value, consequence: $("#fCons").value,
      description: $("#fDesc").value,
    });
    toast("故障已登记留痕，车辆状态已更新", "ok");
    await refresh();
  } catch (e) { toast("⛔ " + e.message, "err"); }
};

/* ---------------- 5. 备用车调配 ---------------- */
function renderSpare() {
  const b = state.board;
  const guard = roleGuard(["dispatcher"]);
  const assigned = new Set(b.swaps.map((s) => s.to_vehicle_id));
  const pool = b.vehicles.filter((v) => v.is_spare).map((v) => {
    const avail = b.spares_available.some((s) => s.id === v.id);
    return `<div class="spare-card ${avail ? "" : "used"}">
      <b>${esc(v.fleet_no)}</b>
      <div class="muted">${esc(v.plate)}</div>
      <div class="muted">${esc(rampLabel(v.ramp_type))} · 承重 ${v.load_rating_kg}kg</div>
      <div style="margin-top:6px">${avail ? `<span class="badge b-ok">可调配</span>`
        : `<span class="badge b-gray">已占用/待命</span>`}</div>
    </div>`;
  }).join("");

  const needSwap = b.trips.filter((t) =>
    t.status === "PLANNED" && (!t.inspection || t.inspection.result !== "PASS"
      || t.vehicle_status !== "IN_SERVICE"));
  const rows = needSwap.map((t) => `
    <div class="item-card blocked">
      <div class="ic-top">
        <span class="ic-title"><span class="line-chip ${t.line_code === "K3" ? "k3" : ""}">${esc(t.line_code)}</span>
          ${fmtTime(t.departure_at)} · 原车 ${esc(t.fleet_no)} ${rampDot(t)}</span>
        <span>${t.line_code === "K3" && !t.accessible_required
          ? `<span class="badge b-gray">该线路不强制无障碍</span>` : ""}
          <button class="btn orange" onclick="doSwap('${t.id}')" ${b.spares_available.length === 0 ? "disabled" : ""}>
            调配备用车（剩 ${b.spares_available.length}）</button></span>
      </div>
      <div class="ic-meta"><span>驾驶员 ${esc(t.driver_name)}</span>
        <span>${t.inspection ? "点检不合格" : "尚未点检"} · ${vehStatusBadge(t.vehicle_status)}</span></div>
    </div>`).join("");

  const swapLog = b.swaps.map((s) => `<div class="item-card done">
    <div class="ic-top"><span class="ic-title">🔁 ${esc(fleetOf(s.from_vehicle_id))} → ${esc(fleetOf(s.to_vehicle_id))}</span>
      <span class="badge b-blue">临时换车</span></div>
    <div class="ic-meta"><span>班次任务 <span class="sig">${esc(s.trip_id)}</span></span>
      <span>调度员 ${esc(staffName(s.dispatcher_id))}</span>
      <span>${fmtDateTime(s.created_at)}</span><span>原因：${esc(s.reason)}</span></div>
  </div>`).join("");

  return `${guard}
  <div class="panel">
    <h2>备用车池 <span class="tag">剩余 ${b.spares_available.length} / ${b.spares_total} 台无障碍备用车</span></h2>
    <div class="spare-pool">${pool}</div>
  </div>
  <div class="row-flex">
    <div class="panel" style="flex:1"><h2>需要换车的班次</h2>
      ${rows || `<p class="muted">所有在途班次车辆均合格，无需换车。</p>`}
      ${b.spares_available.length === 0 && needSwap.length
        ? `<div class="roleguard">🧯 备用容量耗尽：已无可调配无障碍备用车，新换车请求将被后端拒绝（SPARE_NONE_AVAILABLE）。</div>` : ""}
    </div>
    <div class="panel" style="flex:1"><h2>换车留痕</h2>${swapLog || `<p class="muted">暂无换车记录</p>`}</div>
  </div>`;
}

window.doSwap = async (tripId) => {
  const reason = prompt("换车原因：", "临时换车：原车无障碍设备故障");
  if (reason === null) return;
  try {
    const out = await api("/api/swaps", { trip_id: tripId, reason });
    toast(`已换乘 ${fleetOf(out.spare_vehicle_id)}，剩余备用车 ${out.spares_remaining} 台`, "ok");
    await refresh();
    switchTab("inspect");
    state.pickedTripId = tripId;
    state.pickedVehicleId = out.spare_vehicle_id;
    render();
  } catch (e) {
    toast("⛔ " + (e.code === "SPARE_NONE_AVAILABLE"
      ? "备用车容量不足，无车可换 —— 已留痕" : e.message), "err");
    await refresh();
  }
};

/* ---------------- 6. 维修进度 ---------------- */
function repairStatusBadge(st) {
  return {
    OPEN: `<span class="badge b-orange">待开工</span>`,
    IN_PROGRESS: `<span class="badge b-blue">维修中</span>`,
    DELAYED: `<span class="badge b-warn">延期/复检退回</span>`,
    DONE: `<span class="badge b-ok">完工待复检</span>`,
    REINSPECTED: `<span class="badge b-ok">复检合格闭环</span>`,
  }[st] || st;
}

function renderRepair() {
  const guard = roleGuard(["mechanic"]);
  const openFaults = state.board.faults.filter((f) =>
    !f.repair_id && f.consequence !== "NONE");
  const repairCards = state.board.repairs.map((r) => {
    const overdue = r.due_at && !r.completed_at && r.due_at < state.board.now;
    return `<div class="item-card ${r.status === "DONE" ? "done" : overdue ? "blocked" : "warn"}">
      <div class="ic-top">
        <span class="ic-title">🛠️ ${esc(r.fleet_no)} · ${esc(FAULT_META[r.fault_type]?.label || r.fault_type)} ${repairStatusBadge(r.status)}</span>
        <span class="muted"><span class="sig">${esc(r.id)}</span></span>
      </div>
      <div class="ic-meta">
        <span>报修 ${fmtDateTime(r.created_at)}</span>
        <span>期限 ${fmtDateTime(r.due_at)} ${overdue ? `<b style="color:var(--bad)">已超时</b>` : ""}</span>
        ${r.mechanic_id ? `<span>维修：${esc(staffName(r.mechanic_id))}</span>` : ""}
        ${r.completed_at ? `<span>完工 ${fmtDateTime(r.completed_at)}</span>` : ""}
      </div>
      ${r.summary ? `<div class="ic-meta"><span>📝 ${esc(r.summary)}（台架测试：${esc(r.bench_test)}）</span></div>` : ""}
      ${r.status !== "DONE" ? `<div class="btn-row" style="margin:6px 0 0">
        <button class="btn primary" onclick="completeRepair('${r.id}')">维修完成（填台架测试）</button>
        <button class="btn danger" onclick="delayRepair('${r.id}')">维修延期（留痕）</button>
      </div>` : `<p class="hint" style="margin:6px 0 0">完工后必须由驾驶员复检合格才能恢复运营，维修人员不能批准复检。</p>`}
    </div>`;
  }).join("");

  return `${guard}
  <div class="row-flex">
    <div class="panel" style="flex:1">
      <h2>维修进度 <span class="tag">完工不等于恢复运营</span></h2>
      ${repairCards || `<p class="muted">暂无维修单</p>`}
    </div>
    <div class="panel" style="flex:1">
      <h2>待开工故障</h2>
      ${openFaults.map((f) => `<div class="item-card warn">
        <div class="ic-top"><span class="ic-title">${FAULT_META[f.fault_type]?.icon} ${esc(FAULT_META[f.fault_type]?.label || f.fault_type)} · ${esc(fleetOf(f.vehicle_id))}</span></div>
        <div class="ic-meta"><span><span class="sig">${esc(f.id)}</span></span><span>${esc(f.description)}</span></div>
        <div class="btn-row" style="margin:6px 0 0">
          <button class="btn primary" onclick="openRepair('${f.id}')">开工（默认 60 分钟期限）</button>
        </div>
      </div>`).join("") || `<p class="muted">没有待开工的故障。</p>`}
    </div>
  </div>`;
}

window.openRepair = async (faultId) => {
  try { await api("/api/repairs/open", { fault_id: faultId, due_minutes: 60 });
    toast("维修单已开工，到期前系统会提醒", "ok"); await refresh();
  } catch (e) { toast("⛔ " + e.message, "err"); }
};
window.completeRepair = async (rid) => {
  const summary = prompt("维修内容与更换件：", "已更换故障部件并润滑，台架测试通过");
  if (summary === null) return;
  try { await api("/api/repairs/complete", { repair_id: rid, summary, bench_test: "PASS" });
    toast("维修完工，等待驾驶员复检（维修人员无权批准复检）", "ok"); await refresh();
  } catch (e) { toast("⛔ " + e.message, "err"); }
};
window.delayRepair = async (rid) => {
  const reason = prompt("延期原因：", "配件待料");
  if (reason === null) return;
  try { await api("/api/repairs/delay", { repair_id: rid, reason });
    toast("已延期并写入“维修延期”留痕", "ok"); await refresh();
  } catch (e) { toast("⛔ " + e.message, "err"); }
};

/* ---------------- 7. 复检（图标 + 文字结论） ---------------- */
function renderReinspect() {
  const b = state.board;
  const guard = roleGuard(["driver"]);
  const awaiting = b.repairs.filter((r) => r.status === "DONE");
  const history = b.inspections.filter((i) => i.round > 1);

  const verdictHtml = history.map((i) => {
    const pass = i.result === "PASS";
    return `<div class="verdict ${pass ? "pass" : "fail"}">
      <span class="v-icon">${pass ? "✅" : "❌"}</span>
      <div><div>${pass ? "复检合格 —— 车辆恢复运营" : "复检不合格 —— 车辆继续停用，退回重新维修"}</div>
        <div class="v-sub">${esc(fleetOf(i.vehicle_id))} · 第 ${i.round} 次点检 ·
          驾驶员 ${esc(staffName(i.driver_id))} · ${fmtDateTime(i.finalized_at)}
          ${i.note ? `· ${esc(i.note)}` : ""}</div></div>
    </div>`;
  }).join("");

  const cards = awaiting.map((r) => {
    const vid = b.vehicles.find((v) => v.fleet_no === r.fleet_no).id;
    state.check[vid] = state.check[vid] || {};
    const ck = state.check[vid];
    const cards2 = ITEM_ORDER.map((k) => {
      const v = ck[k];
      const cls = v === true ? "pass" : v === false ? "fail" : "pending";
      return `<div class="check-card ${cls}" onclick="toggleRetest('${vid}','${k}')">
        <span class="cc-icon">${ITEM_META[k].icon}</span>
        <span class="cc-name">${ITEM_META[k].name}</span>
        <span class="cc-state">${v === true ? "✓ 合格" : v === false ? "✗ 不合格" : "待检查"}</span></div>`;
    }).join("");
    return `<div class="panel">
      <h2>复检：${esc(r.fleet_no)} <span class="tag">维修单 ${esc(r.id)} · ${esc(FAULT_META[r.fault_type]?.label || "")}</span></h2>
      <p class="section-note">维修内容：${esc(r.summary || "—")}；台架测试：${esc(r.bench_test)}。
        台架通过不代表车上合格，须实车重新点检并记录坡板测试。</p>
      <div class="check-grid">${cards2}</div>
      <div class="btn-row">
        <button class="btn primary big" onclick="submitRetest('${r.id}','${vid}')">提交复检结论（驾驶员）</button>
        <button class="btn" onclick="retestAllPass('${vid}')">全部合格</button>
      </div>
    </div>`;
  }).join("");

  return `<div class="driver-mode">
    ${guard}
    ${awaiting.length ? cards : `<div class="panel"><p class="muted">暂无“完工待复检”的维修单。</p></div>`}
    <div class="panel"><h2>复检结论 <span class="tag">图标 + 文字同时呈现</span></h2>${verdictHtml || `<p class="muted">暂无复检记录。</p>`}</div>
  </div>`;
}

window.toggleRetest = (vid, item) => {
  state.check[vid] = state.check[vid] || {};
  const cur = state.check[vid][item];
  state.check[vid][item] = cur === true ? false : true;
  render();
};
window.retestAllPass = (vid) => {
  const ck = {}; ITEM_ORDER.forEach((k) => (ck[k] = true));
  state.check[vid] = ck; render();
};
window.submitRetest = async (rid, vid) => {
  const ck = state.check[vid] || {};
  const pending = ITEM_ORDER.filter((k) => ck[k] === undefined);
  if (pending.length) return toast("还有未检查项", "err");
  const items = {};
  ITEM_ORDER.forEach((k) => (items[k] = !!ck[k]));
  try {
    const out = await api("/api/reinspections", {
      repair_id: rid, items,
      ramp_test: { deploy_seconds: 7.0, retract_seconds: 6.0, cycles: 2,
                   result: items.deploy_retract ? "PASS" : "FAIL" },
    });
    toast(out.result === "PASS" ? "✅ 复检合格，车辆恢复运营" : "❌ 复检失败，车辆继续停用并退回维修",
      out.result === "PASS" ? "ok" : "err");
    await refresh();
  } catch (e) {
    toast("⛔ " + (e.code === "FORBIDDEN_ROLE"
      ? "职责分离：维修人员不能批准复检，请切换驾驶员身份" : e.message), "err");
  }
};

/* ---------------- 8. 放行记录 ---------------- */
function renderRelease() {
  const b = state.board;
  const guard = roleGuard(["dispatcher"]);
  const rows = b.releases.map((r) => `
    <div class="item-card done">
      <div class="ic-top">
        <span class="ic-title">🚦 ${esc(r.line_code)} ${fmtTime(r.departure_at)} · ${esc(r.fleet_no)}
          <span class="badge b-ok">已放行</span></span>
        <span class="muted"><span class="sig">${esc(r.id)}</span></span>
      </div>
      <div class="ic-meta">
        <span>放行：${esc(staffName(r.released_by))}</span><span>${fmtDateTime(r.created_at)}</span>
      </div>
      <div class="ic-meta">
        <span>门控信号 <span class="sig">${esc(r.gate_signal)}</span></span>
        <span>车辆终端 <span class="sig">${esc(r.terminal_signal)}</span></span>
      </div>
    </div>`).join("");

  return `${guard}
  <div class="panel">
    <h2>出场放行记录 <span class="tag">门控 / 终端模拟信号</span></h2>
    <p class="section-note">放行前校验：有效点检存在且六项全合格、车辆在役。同一班次任务只产生一张放行单；
      两人并发或重复点击，只有第一次成功并抬杆，其余请求幂等返回且门控不再动作。</p>
    ${rows || `<p class="muted">暂无放行记录</p>`}
  </div>
  <div class="row-flex">
    <div class="panel" style="flex:1"><h2>🚧 门控适配器日志</h2>
      ${(b.gate_log || []).map((g) => `<div class="ic-meta"><span class="sig">${esc(g.signal)}</span><span>${esc(g.barrier)} @ ${fmtDateTime(g.opened_at)}</span></div>`).join("") || `<p class="muted">无</p>`}
    </div>
    <div class="panel" style="flex:1"><h2>📟 车辆终端适配器日志</h2>
      ${(b.terminal_log || []).map((g) => `<div class="ic-meta"><span class="sig">${esc(g.signal)}</span><span>${esc(g.message)} @ ${fmtDateTime(g.delivered_at)}</span></div>`).join("") || `<p class="muted">无</p>`}
    </div>
  </div>`;
}

/* ---------------- 9. 留痕时间线 ---------------- */
const KIND_STYLE = {
  INSPECTION_PASS: ["k-ok", "初检合格"], INSPECTION_FAIL: ["k-bad", "初检不合格"],
  REINSPECTION_PASS: ["k-ok", "复检合格"], REINSPECTION_FAIL: ["k-bad", "复检失败"],
  REINSPECTION_FAILED_HOLD: ["k-bad", "复检失败停用"],
  FAULT_REPORT: ["k-bad", "故障登记"], VEHICLE_RAMP_DISABLED: ["k-warn", "无障碍停用"],
  VEHICLE_DOWN: ["k-bad", "整车待修"], VEHICLE_BACK_IN_SERVICE: ["k-ok", "恢复运营"],
  SPARE_ASSIGNED: ["k-blue", "备用车调配"], SWAP: ["k-blue", "临时换车"],
  SWAP_DENIED_CAPACITY: ["k-bad", "换车被拒(容量)"],
  REPAIR_START: ["k-blue", "维修开工"], REPAIR_COMPLETE: ["k-blue", "维修完工"],
  REPAIR_DELAY: ["k-warn", "维修延期"], REPAIR_OVERDUE_FLAG: ["k-bad", "维修超时"],
  RELEASE_GRANTED: ["k-ok", "放行成功"], RELEASE_BLOCKED: ["k-bad", "放行阻断"],
  RELEASE_DUPLICATE: ["k-warn", "重复放行拦截"], GATE_SIGNAL: ["k-blue", "门控抬杆"],
  TERMINAL_SIGNAL: ["k-blue", "终端通知"], HANDOVER: ["k-ok", "驾驶员交接"],
  DRIVER_MISSED_CHECK: ["k-bad", "司机漏检"], MISRELEASE_FLAGGED: ["k-bad", "误放行"],
};
function renderLog() {
  const events = state.board.events;
  return `<div class="panel">
    <h2>全程留痕 <span class="tag">${events.length} 条（最近 200）</span></h2>
    <p class="section-note">坡板卡滞、固定销缺失、按钮无响应、承重标识脱落、司机漏检、临时换车、维修延期、误放行八类情形均可在此追溯。</p>
    <div class="timeline">
      ${events.map((e) => {
        const [cls, label] = KIND_STYLE[e.kind] || ["k-blue", e.kind];
        let detail = "";
        if (e.detail_obj && Object.keys(e.detail_obj).length) {
          detail = Object.entries(e.detail_obj).slice(0, 6)
            .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
        }
        return `<div class="tl-item">
          <span class="tl-time">${fmtDateTime(e.at)}</span>
          <span class="tl-kind ${cls}">${label}</span>
          <span class="tl-detail">${esc(detail)}</span>
          <span class="tl-actor">${e.vehicle_id ? esc(e.vehicle_id) + " " : ""}${e.actor_id ? esc(staffName(e.actor_id)) : ""}</span>
        </div>`;
      }).join("")}
    </div>
  </div>`;
}

/* ---------------- 渲染分发 ---------------- */
function render() {
  if (!state.board) return;
  renderAlerts();
  const map = {
    board: renderBoard, inspect: renderInspect, ramp: renderRamp,
    fault: renderFault, spare: renderSpare, repair: renderRepair,
    reinspect: renderReinspect, release: renderRelease, log: renderLog,
  };
  view().innerHTML = (map[state.tab] || renderBoard)();
}

async function refresh() {
  await loadBoard();
  render();
}

(async function init() {
  setupChrome();
  await loadBoard();
  fillStaff();
  render();
  setInterval(refresh, 15000);
})();
