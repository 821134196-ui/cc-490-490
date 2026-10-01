/* 公交场站轮椅坡板点检与车辆放行系统 — 前端逻辑 */
"use strict";

const state = {
  staff: [], routes: [], vehicles: [],
  roleLabels: {}, statusLabels: {}, faultTypes: {}, itemLabels: {},
  actorId: Number(localStorage.getItem("actorId")) || null,
  board: [],
  inspectItems: { appearance: null, deploy: null, locking: null, button: null },
  recheckVehicleId: null,          // 从复检页跳入的车辆级复检
  releaseKeys: {},                 // shift_id -> 幂等键（重试复用）
};

/* ---------------- 基础 ---------------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, kind = "") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3600);
}

async function api(path, opts = {}) {
  opts.headers = Object.assign({ "X-Staff-Id": state.actorId || "" }, opts.headers);
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
  }
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || `请求失败（${resp.status}）`);
    err.status = resp.status;
    throw err;
  }
  return data;
}

function actor() { return state.staff.find(s => s.id === state.actorId); }
function vehicle(id) { return state.vehicles.find(v => v.id === id); }

async function guard(fn, refresh) {
  try {
    await fn();
    if (refresh) await refresh();
  } catch (e) {
    toast(e.message, "error");
  }
}

/* ---------------- 初始化 ---------------- */

async function boot() {
  const b = await api("/api/bootstrap");
  Object.assign(state, {
    staff: b.staff, routes: b.routes, vehicles: b.vehicles,
    roleLabels: b.role_labels, statusLabels: b.vehicle_status_labels,
    faultTypes: b.fault_types, itemLabels: b.item_labels,
  });
  if (!state.actorId || !actor()) {
    const firstDriver = state.staff.find(s => s.role === "driver");
    state.actorId = firstDriver ? firstDriver.id : state.staff[0].id;
  }
  renderActorSelect();
  bindTabs();
  bindInspect();
  bindFaultForm();
  bindDialog();
  await Promise.all([loadBoard(), loadRamp(), loadFaults(), loadSwap(),
                     loadRepairs(), loadRechecks(), loadReleases(), loadEvents()]);
  pollReminders();
  setInterval(pollReminders, 10000);
  setInterval(() => {
    if (document.querySelector('#tab-board').classList.contains('active')) loadBoard();
  }, 15000);
}

function renderActorSelect() {
  const sel = document.getElementById("actor-select");
  const groups = { driver: "驾驶员", dispatcher: "调度员", mechanic: "维修人员" };
  sel.innerHTML = Object.entries(groups).map(([role, label]) =>
    `<optgroup label="${label}">` +
    state.staff.filter(s => s.role === role)
      .map(s => `<option value="${s.id}" ${s.id === state.actorId ? "selected" : ""}>${esc(s.name)}</option>`)
      .join("") + `</optgroup>`).join("");
  updateRoleChip();
  sel.onchange = () => {
    state.actorId = Number(sel.value);
    localStorage.setItem("actorId", state.actorId);
    updateRoleChip();
    toast(`已切换身份：${actor().name}（${state.roleLabels[actor().role]}）`);
  };
}

function updateRoleChip() {
  const a = actor();
  document.getElementById("actor-role").textContent =
    a ? state.roleLabels[a.role] : "";
}

function bindTabs() {
  document.getElementById("tabs").addEventListener("click", e => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t === btn));
    document.querySelectorAll(".tab-page").forEach(p =>
      p.classList.toggle("active", p.id === "tab-" + btn.dataset.tab));
    const loaders = {
      board: loadBoard, ramp: loadRamp, faults: loadFaults, swap: loadSwap,
      repairs: loadRepairs, recheck: loadRechecks, releases: loadReleases,
      events: loadEvents, inspect: loadInspectShifts,
    };
    if (loaders[btn.dataset.tab]) loaders[btn.dataset.tab]();
  });
  document.getElementById("board-refresh").onclick = loadBoard;
  document.getElementById("ramp-refresh").onclick = loadRamp;
  document.getElementById("swap-refresh").onclick = loadSwap;
  document.getElementById("repairs-refresh").onclick = loadRepairs;
  document.getElementById("recheck-refresh").onclick = loadRechecks;
  document.getElementById("releases-refresh").onclick = loadReleases;
  document.getElementById("events-refresh").onclick = loadEvents;
}

/* ---------------- 提醒条 ---------------- */

async function pollReminders() {
  try {
    const { reminders } = await api("/api/reminders");
    const bar = document.getElementById("reminder-bar");
    if (!reminders.length) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.innerHTML = "<strong>⏰ 任务提醒</strong>" +
      reminders.map(r => `<div>· ${esc(r.message)}</div>`).join("");
  } catch (e) { /* 提醒失败不打扰 */ }
}

/* ---------------- 线路待发板 ---------------- */

function rampBadge(status) {
  if (status === "active") return `<span class="ramp-badge ramp-ok">坡板正常</span>`;
  if (status === "access_disabled") return `<span class="ramp-badge ramp-disabled">无障碍停用</span>`;
  return `<span class="ramp-badge ramp-out">整车待修</span>`;
}

function inspChip(result) {
  if (result === "pass") return `<span class="chip pass">点检合格</span>`;
  if (result === "fail") return `<span class="chip fail">点检不合格</span>`;
  return `<span class="chip none">未点检</span>`;
}

async function loadBoard() {
  const { shifts } = await api("/api/board");
  state.board = shifts;
  const byRoute = {};
  for (const s of shifts) (byRoute[s.route_code] ??= { name: s.route_name, rows: [] }).rows.push(s);
  const html = Object.entries(byRoute).map(([code, g]) => `
    <div class="route-block">
      <div class="route-head"><span>${esc(code)}<span class="route-name">${esc(g.name)}</span></span>
      <span>${g.rows.length} 个班次</span></div>
      <table class="board-table">
        <thead><tr>
          <th>发车</th><th>班次</th><th>车辆 / 坡板状态</th><th>驾驶员</th>
          <th>点检</th><th>状态</th><th></th>
        </tr></thead>
        <tbody>${g.rows.map(row => {
          const released = row.status === "released";
          const problem = !released && (row.vehicle_status !== "active" || row.open_faults > 0);
          return `<tr class="${released ? "released" : problem ? "problem" : ""}">
            <td><strong>${esc(row.depart_time)}</strong></td>
            <td>#${row.id}</td>
            <td><span class="fleet-cell">${esc(row.fleet_no)}</span>${rampBadge(row.vehicle_status)}
                <span class="chip none">${esc(row.ramp_type)}</span></td>
            <td>${esc(row.driver_name)}</td>
            <td>${inspChip(row.insp_result)}</td>
            <td>${released ? '<span class="chip done">已放行</span>'
                : problem ? '<span class="chip fail">故障阻断</span>'
                : '<span class="chip pending">待发</span>'}</td>
            <td class="actions">${released ? "" : `
              <button class="btn blue" data-release="${row.id}">放行</button>
              <button class="btn ghost" data-handover="${row.id}">交接</button>`}
            </td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`).join("");
  document.getElementById("board-list").innerHTML = html || `<div class="empty">暂无班次</div>`;

  document.querySelectorAll("[data-release]").forEach(btn => {
    btn.onclick = () => releaseShift(Number(btn.dataset.release), btn);
  });
  document.querySelectorAll("[data-handover]").forEach(btn => {
    btn.onclick = () => openHandover(Number(btn.dataset.handover));
  });
}

async function releaseShift(shiftId, btn) {
  // 幂等：同一班次重试复用同一幂等键，并发放行只会成功一次
  state.releaseKeys[shiftId] ??= crypto.randomUUID();
  btn.disabled = true;
  await guard(async () => {
    const r = await api(`/api/shifts/${shiftId}/release`,
      { json: { idempotency_key: state.releaseKeys[shiftId] } });
    toast(r.idempotent ? `班次#${shiftId} 已放行过（幂等返回记录#${r.release_id}）`
                       : `班次#${shiftId} 放行成功`, "ok");
    delete state.releaseKeys[shiftId];
  }, loadBoard);
  btn.disabled = false;
}

/* ---------------- 交接 ---------------- */

function bindDialog() {
  document.getElementById("handover-cancel").onclick =
    () => document.getElementById("handover-dialog").close();
}

function openHandover(shiftId) {
  const shift = state.board.find(s => s.id === shiftId);
  const dlg = document.getElementById("handover-dialog");
  document.getElementById("handover-shift-label").textContent =
    `班次#${shiftId}（${shift.route_code} ${shift.depart_time}，当前驾驶员 ${shift.driver_name}）`;
  const sel = document.getElementById("handover-driver");
  sel.innerHTML = state.staff.filter(s => s.role === "driver")
    .map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  document.getElementById("handover-ok").onclick = () => guard(async () => {
    await api(`/api/shifts/${shiftId}/handover`, { json: { to_driver_id: Number(sel.value) } });
    dlg.close();
    toast("交接完成", "ok");
    await loadBoard();
  });
  dlg.showModal();
}

/* ---------------- 车辆点检 ---------------- */

function bindInspect() {
  document.getElementById("inspect-shift").onchange = onInspectTargetChange;
  document.getElementById("read-signals").onclick = readSignals;
  document.getElementById("submit-inspection").onclick = submitInspection;
  renderInspectItems();
}

async function loadInspectShifts() {
  const { shifts } = await api("/api/board");
  state.board = shifts;
  const pending = shifts.filter(s => s.status === "pending");
  const sel = document.getElementById("inspect-shift");
  let opts = pending.map(s =>
    `<option value="shift:${s.id}">#${s.id} ${esc(s.route_code)} ${esc(s.depart_time)} — ${esc(s.fleet_no)}（${esc(s.driver_name)}）</option>`);
  if (state.recheckVehicleId) {
    const v = vehicle(state.recheckVehicleId);
    opts.unshift(`<option value="vehicle:${v.id}">车辆复检（无班次）— ${esc(v.fleet_no)}</option>`);
  }
  sel.innerHTML = opts.join("") || `<option value="">暂无待发班次</option>`;
  onInspectTargetChange();
}

function inspectTarget() {
  const raw = document.getElementById("inspect-shift").value;
  if (raw.startsWith("shift:")) {
    const id = Number(raw.slice(6));
    const row = state.board.find(s => s.id === id);
    return { shiftId: id, vehicleId: row ? row.vehicle_id : null };
  }
  if (raw.startsWith("vehicle:")) return { shiftId: null, vehicleId: Number(raw.slice(8)) };
  return { shiftId: null, vehicleId: null };
}

function onInspectTargetChange() {
  const { vehicleId } = inspectTarget();
  const info = document.getElementById("inspect-vehicle-info");
  const v = vehicle(vehicleId);
  info.innerHTML = v
    ? `<strong>${esc(v.fleet_no)}</strong>（${esc(v.plate_no)}） · 坡板类型：<strong>${esc(v.ramp_type)}</strong>
       · 核载 ${v.capacity} 人 · 状态：${esc(state.statusLabels[v.status])}`
    : "请选择班次";
  state.inspectItems = { appearance: null, deploy: null, locking: null, button: null };
  document.getElementById("inspect-result").hidden = true;
  document.getElementById("signal-readout").innerHTML = "";
  renderInspectItems();
}

function renderInspectItems() {
  const box = document.getElementById("inspect-items");
  box.innerHTML = Object.entries(state.itemLabels).map(([key, label]) => `
    <div class="inspect-item">
      <span class="item-name">${esc(label)}</span>
      <span class="item-btns">
        <button class="item-btn ${state.inspectItems[key] === true ? "sel-pass" : ""}"
                data-item="${key}" data-val="1">✔ 合格</button>
        <button class="item-btn ${state.inspectItems[key] === false ? "sel-fail" : ""}"
                data-item="${key}" data-val="0">✘ 不合格</button>
      </span>
    </div>`).join("");
  box.querySelectorAll(".item-btn").forEach(btn => {
    btn.onclick = () => {
      state.inspectItems[btn.dataset.item] = btn.dataset.val === "1";
      renderInspectItems();
    };
  });
}

async function readSignals() {
  const { vehicleId } = inspectTarget();
  if (!vehicleId) return toast("请先选择班次", "error");
  await guard(async () => {
    const { terminal, door } = await api(`/api/vehicles/${vehicleId}/signals`);
    const rampOk = terminal.ramp_position !== "jammed";
    const btnOk = terminal.call_button === "ok";
    state.inspectItems.deploy = rampOk;
    state.inspectItems.button = btnOk;
    renderInspectItems();
    const rampText = { stowed: "收回到位", deployed: "展开到位", jammed: "卡滞" }[terminal.ramp_position];
    document.getElementById("signal-readout").innerHTML = `
      <div>终端在线：${terminal.terminal_online ? '<span class="sig-ok">在线</span>' : '<span class="sig-bad">离线</span>'}
        （信号龄期 ${terminal.signal_age_s}s）</div>
      <div>坡板位置：<span class="${rampOk ? "sig-ok" : "sig-bad"}">${rampText}</span>
        → 展开/收回项已置为「${rampOk ? "合格" : "不合格"}」</div>
      <div>呼叫按钮：<span class="${btnOk ? "sig-ok" : "sig-bad"}">${btnOk ? "响应正常" : "无响应"}</span>
        → 按钮项已置为「${btnOk ? "合格" : "不合格"}」</div>
      <div>门控联锁：${door.interlock === "ok" ? '<span class="sig-ok">正常</span>' : '<span class="sig-bad">故障</span>'}
        · 前门 ${esc(door.front_door)} / 后门 ${esc(door.rear_door)}</div>`;
  });
}

async function submitInspection() {
  const { shiftId, vehicleId } = inspectTarget();
  if (!vehicleId) return toast("请先选择班次", "error");
  const missing = Object.entries(state.inspectItems).filter(([, v]) => v === null);
  if (missing.length) return toast("还有点检项未判定", "error");

  // 可选：先上传测试照片到本地 photos/ 目录
  const photos = [];
  const fileInput = document.getElementById("inspect-photo");
  if (fileInput.files.length) {
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    const up = await fetch("/api/photos", { method: "POST", body: fd }).then(r => r.json());
    if (up.path) photos.push({ item: "", path: up.path });
  }

  await guard(async () => {
    const r = await api("/api/inspections", {
      json: {
        vehicle_id: vehicleId, shift_id: shiftId, items: state.inspectItems,
        notes: document.getElementById("inspect-notes").value, photos,
      },
    });
    const box = document.getElementById("inspect-result");
    box.hidden = false;
    if (r.result === "pass") {
      box.className = "inspect-result pass";
      box.textContent = r.kind === "reinspection" ? "✅ 复检合格，车辆恢复正常" : "✅ 点检合格，可以放行";
      toast("点检合格", "ok");
    } else {
      box.className = "inspect-result fail";
      box.textContent = (r.kind === "reinspection" ? "⛔ 复检不合格，故障已重开" : "⛔ 点检不合格，已自动生成故障与维修单");
      toast("点检不合格，车辆已停用无障碍功能", "error");
    }
    state.recheckVehicleId = null;
    fileInput.value = "";
    document.getElementById("inspect-notes").value = "";
    await Promise.all([loadBoard(), loadInspectShifts()]);
  });
}

/* ---------------- 坡板测试 ---------------- */

async function loadRamp() {
  const grid = document.getElementById("ramp-vehicles");
  const cards = await Promise.all(state.vehicles.map(async v => {
    const { terminal, door } = await api(`/api/vehicles/${v.id}/signals`);
    const cls = v.status === "active" ? "" : v.status === "access_disabled" ? "disabled" : "out";
    const rampText = { stowed: "收回", deployed: "展开", jammed: "卡滞" }[terminal.ramp_position];
    return `<div class="ramp-card ${cls}">
      <h4>${esc(v.fleet_no)} ${v.is_backup ? "（备用）" : ""} ${rampBadge(v.status)}</h4>
      <div class="sig-row"><span>坡板类型</span><strong>${esc(v.ramp_type)}</strong></div>
      <div class="sig-row"><span>终端</span><strong>${terminal.terminal_online ? "在线" : "离线"}</strong></div>
      <div class="sig-row"><span>坡板位置</span><strong>${rampText}</strong></div>
      <div class="sig-row"><span>呼叫按钮</span><strong>${terminal.call_button === "ok" ? "正常" : "无响应"}</strong></div>
      <div class="sig-row"><span>门控联锁</span><strong>${door.interlock === "ok" ? "正常" : "故障"}</strong></div>
      <button class="btn blue" data-test="${v.id}">执行展开/收回测试</button>
    </div>`;
  }));
  grid.innerHTML = cards.join("");
  grid.querySelectorAll("[data-test]").forEach(btn => {
    btn.onclick = () => guard(async () => {
      const r = await api(`/api/vehicles/${btn.dataset.test}/ramp-test`, { json: {} });
      toast(r.result === "pass" ? "展开/收回测试合格" : "展开/收回测试不合格",
            r.result === "pass" ? "ok" : "error");
      await loadRamp();
    });
  });
  const { tests } = await api("/api/ramp-tests");
  document.getElementById("ramp-tests").innerHTML = tests.length ? `<table class="simple">
    <thead><tr><th>时间</th><th>车辆</th><th>项目</th><th>结果</th><th>执行人</th></tr></thead>
    <tbody>${tests.map(t => `<tr>
      <td>${esc(t.created_at)}</td><td>${esc(t.fleet_no)}</td><td>展开/收回</td>
      <td>${t.result === "pass" ? '<span class="chip pass">合格</span>' : '<span class="chip fail">不合格</span>'}</td>
      <td>${esc(t.actor_name)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">暂无测试记录</div>`;
}

/* ---------------- 故障停用 ---------------- */

function bindFaultForm() {
  const vsel = document.getElementById("fault-vehicle");
  vsel.innerHTML = state.vehicles.map(v =>
    `<option value="${v.id}">${esc(v.fleet_no)}（${esc(state.statusLabels[v.status])}）</option>`).join("");
  document.getElementById("fault-type").innerHTML =
    Object.entries(state.faultTypes).map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("");
  document.getElementById("fault-form").onsubmit = e => {
    e.preventDefault();
    guard(async () => {
      await api("/api/faults", {
        json: {
          vehicle_id: Number(vsel.value),
          type: document.getElementById("fault-type").value,
          severity: document.getElementById("fault-severity").value,
          description: document.getElementById("fault-desc").value,
        },
      });
      document.getElementById("fault-desc").value = "";
      toast("故障已登记，车辆已停用并生成维修单", "ok");
      await Promise.all([loadFaults(), loadBoard(), loadSwap(), loadRepairs()]);
    });
  };
}

async function loadFaults() {
  const { faults } = await api("/api/faults");
  const statusChip = { open: '<span class="chip fail">待维修</span>',
                       repaired: '<span class="chip pending">待复检</span>',
                       closed: '<span class="chip done">已闭环</span>' };
  document.getElementById("fault-list").innerHTML = faults.length ? `<table class="simple">
    <thead><tr><th>#</th><th>车辆</th><th>故障</th><th>级别</th><th>描述</th><th>报修人</th><th>状态</th><th>时间</th></tr></thead>
    <tbody>${faults.map(f => `<tr>
      <td>${f.id}</td><td>${esc(f.fleet_no)}</td>
      <td>${esc(state.faultTypes[f.type] || f.type)}</td>
      <td>${f.severity === "vehicle" ? "整车待修" : "无障碍停用"}</td>
      <td>${esc(f.description)}</td><td>${esc(f.reporter_name)}</td>
      <td>${statusChip[f.status]}</td><td>${esc(f.created_at)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">暂无故障</div>`;
}

/* ---------------- 备用车调配 ---------------- */

async function loadSwap() {
  const { shifts } = await api("/api/board");
  const pending = shifts.filter(s => s.status === "pending");
  const backups = state.vehicles.filter(v => v.is_backup);
  const box = document.getElementById("swap-list");
  if (!pending.length) { box.innerHTML = `<div class="empty">暂无待发班次</div>`; return; }
  box.innerHTML = pending.map(s => {
    const route = state.routes.find(r => r.code === s.route_code);
    const needFix = s.vehicle_status !== "active" || s.open_faults > 0;
    const opts = backups.map(b => {
      const disabled = b.status !== "active" || b.capacity < route.min_capacity
        || (route.requires_accessible && !b.accessible) || b.id === s.vehicle_id;
      const why = b.status !== "active" ? state.statusLabels[b.status]
        : b.capacity < route.min_capacity ? `容量不足 ${b.capacity}<${route.min_capacity}` : "";
      return `<option value="${b.id}" ${disabled ? "disabled" : ""}>
        ${esc(b.fleet_no)} · 核载${b.capacity} · ${esc(b.ramp_type)}${why ? `（${why}）` : ""}</option>`;
    }).join("");
    return `<div class="op-card ${needFix ? "warn" : ""}">
      <div class="grow">
        <strong>#${s.id} ${esc(s.route_code)} ${esc(s.depart_time)}</strong>
        当前车辆 <span class="fleet-cell">${esc(s.fleet_no)}</span>${rampBadge(s.vehicle_status)}
        ${needFix ? '<span class="chip fail">需换车</span>' : ""}
        <div class="hint">线路要求：核载 ≥${route.min_capacity}${route.requires_accessible ? " · 无障碍车" : ""}</div>
      </div>
      <select id="swap-sel-${s.id}">${opts}</select>
      <button class="btn orange" data-swap="${s.id}">换车</button>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-swap]").forEach(btn => {
    btn.onclick = () => guard(async () => {
      const sid = Number(btn.dataset.swap);
      const to = Number(document.getElementById(`swap-sel-${sid}`).value);
      await api(`/api/shifts/${sid}/swap`,
        { json: { to_vehicle_id: to, reason: "故障停用，临时换车" } });
      toast("换车完成，新车辆需重新点检", "ok");
      await Promise.all([loadSwap(), loadBoard()]);
    });
  });
}

/* ---------------- 维修进度 ---------------- */

async function loadRepairs() {
  const { repairs } = await api("/api/repairs");
  const me = actor();
  document.getElementById("repair-list").innerHTML = repairs.length ? repairs.map(r => {
    const overdue = r.status === "pending" && new Date(r.deadline.replace(" ", "T")) < new Date();
    return `<div class="op-card ${overdue ? "warn" : ""}">
      <div class="grow">
        <strong>维修单#${r.id}</strong> ${esc(r.fleet_no)} ·
        ${esc(state.faultTypes[r.fault_type] || r.fault_type)} · ${esc(r.description || "")}
        <div class="hint">时限：${esc(r.deadline)}
          ${overdue ? '<span class="chip overdue">已超时</span>' : ""}
          ${r.assignee_name ? ` · 完工：${esc(r.assignee_name)} ${esc(r.completed_at)}` : ""}</div>
      </div>
      ${r.status === "pending"
        ? `<button class="btn orange" data-repair="${r.id}"
             ${me && me.role !== "mechanic" ? 'disabled title="仅维修人员可完工"' : ""}>完成维修</button>`
        : '<span class="chip done">已完工</span>'}
    </div>`;
  }).join("") : `<div class="empty">暂无维修单</div>`;
  document.querySelectorAll("[data-repair]").forEach(btn => {
    btn.onclick = () => guard(async () => {
      await api(`/api/repairs/${btn.dataset.repair}/complete`, { json: {} });
      toast("维修已完工，车辆待复检", "ok");
      await Promise.all([loadRepairs(), loadRechecks(), loadBoard()]);
    });
  });
}

/* ---------------- 复检 ---------------- */

async function loadRechecks() {
  const { pending, history } = await api("/api/rechecks");
  document.getElementById("recheck-pending").innerHTML = pending.length ? pending.map(p => `
    <div class="op-card warn">
      <div class="grow"><strong>${esc(p.fleet_no)}</strong>
        ${rampBadge(p.status)} 维修已完成，等待驾驶员复检</div>
      <button class="btn orange" data-recheck="${p.vehicle_id}">发起复检</button>
    </div>`).join("") : `<div class="empty">暂无待复检车辆</div>`;
  document.querySelectorAll("[data-recheck]").forEach(btn => {
    btn.onclick = () => {
      state.recheckVehicleId = Number(btn.dataset.recheck);
      document.querySelector('[data-tab="inspect"]').click();
    };
  });
  // 复检结论：图标 + 文字 同时显示
  document.getElementById("recheck-history").innerHTML = history.length ? history.map(h => `
    <div class="op-card">
      <div class="grow">
        <strong>${esc(h.fleet_no)}</strong> · 复检人 ${esc(h.inspector_name)} · ${esc(h.created_at)}
        ${h.notes ? `<div class="hint">${esc(h.notes)}</div>` : ""}
      </div>
      ${h.result === "pass"
        ? '<span class="recheck-conclusion pass"><span class="icon">✅</span><span>复检合格</span></span>'
        : '<span class="recheck-conclusion fail"><span class="icon">⛔</span><span>复检不合格</span></span>'}
    </div>`).join("") : `<div class="empty">暂无复检记录</div>`;
}

/* ---------------- 放行记录 ---------------- */

async function loadReleases() {
  const [{ releases }, { events }] = await Promise.all([
    api("/api/releases"), api("/api/events")]);
  const blocked = events.filter(e =>
    ["erroneous_release_blocked", "release_duplicate_blocked"].includes(e.type));
  document.getElementById("release-list").innerHTML = `
    <h3 class="section-title">放行记录</h3>
    ${releases.length ? `<table class="simple">
      <thead><tr><th>#</th><th>线路</th><th>发车</th><th>车辆</th><th>放行人</th><th>时间</th></tr></thead>
      <tbody>${releases.map(r => `<tr><td>${r.id}</td><td>${esc(r.route_code)}</td>
        <td>${esc(r.depart_time)}</td><td>${esc(r.fleet_no)}</td>
        <td>${esc(r.released_by_name)}</td><td>${esc(r.created_at)}</td></tr>`).join("")}</tbody>
    </table>` : '<div class="empty">暂无放行记录</div>'}
    <h3 class="section-title">误放行 / 重复放行拦截留痕</h3>
    ${blocked.length ? blocked.map(e => `
      <div class="event-row"><span class="etime">${esc(e.created_at)}</span>
        <span class="event-type bad">${esc(e.type)}</span>
        <span>${esc(e.message)}</span></div>`).join("")
    : '<div class="empty">暂无拦截记录</div>'}`;
}

/* ---------------- 留痕日志 ---------------- */

const EVENT_CLASS = {
  ramp_jam: "bad", pin_missing: "bad", button_no_response: "bad", label_peeling: "bad",
  inspection_failed: "bad", reinspection_failed: "bad", driver_missed_inspection: "bad",
  repair_delayed: "bad", erroneous_release_blocked: "bad", release_duplicate_blocked: "bad",
  vehicle_swap: "warn", driver_handover: "warn", repair_completed: "warn", ramp_tested: "warn",
  inspection_passed: "good", release_granted: "good", fault_closed: "good",
};
const EVENT_LABEL = {
  ramp_jam: "坡板卡滞", pin_missing: "固定销缺失", button_no_response: "按钮无响应",
  label_peeling: "承重标识脱落", inspection_failed: "点检不合格",
  reinspection_failed: "复检不合格", driver_missed_inspection: "司机漏检",
  repair_delayed: "维修延期", erroneous_release_blocked: "误放行拦截",
  release_duplicate_blocked: "重复放行拦截", vehicle_swap: "临时换车",
  driver_handover: "驾驶员交接", repair_completed: "维修完成", ramp_tested: "坡板测试",
  inspection_passed: "点检合格", release_granted: "放行出场", fault_closed: "故障闭环",
};

async function loadEvents() {
  const { events } = await api("/api/events");
  document.getElementById("event-list").innerHTML = events.length ? events.map(e => `
    <div class="event-row">
      <span class="etime">${esc(e.created_at)}</span>
      <span class="event-type ${EVENT_CLASS[e.type] || ""}">${esc(EVENT_LABEL[e.type] || e.type)}</span>
      <span>${esc(e.message)}</span>
    </div>`).join("") : `<div class="empty">暂无留痕</div>`;
}

boot().catch(e => toast(e.message, "error"));
