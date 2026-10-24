"use strict";
/* 会议助手前端（零构建，原生 JS）。
 * 所有后端调用都走 api() 助手（音频上传除外，用 FormData 直发），
 * 路径与 src/api/main.py 的路由一一对应——tests/test_web_ui.py 会把
 * 这里用到的路径抽出来与后端路由表比对，改接口时两边要同步。 */

const $ = (sel, el) => (el || document).querySelector(sel);
/* 会议/任务 id 拼进 URL 路径前必须过 encodeURIComponent：id 是用户
 * 自由输入（新建表单、JSON 导入），含空格、'?'、'#' 等字符时不编码
 * 就会拼出坏路径。后端另外在建会入口校验 id 字符集
 * （^[A-Za-z0-9._-]+$），两端一起防。JSON body 里传的 id 不用编码。 */
const enc = encodeURIComponent;

let state = { meetings: [], selectedId: null, meeting: null };

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(msg, isError) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), isError ? 8000 : 3000);
}

async function api(path, opts) {
  opts = opts || {};
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, init);
  let data = null;
  try { data = await resp.json(); } catch (e) { /* non-JSON body */ }
  if (!resp.ok) {
    const detail = (data && data.detail) ? data.detail : (resp.status + " " + resp.statusText);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

async function call(fn) {
  try { return await fn(); }
  catch (e) { toast("请求失败：" + e.message, true); throw e; }
}

/* ---------------------------------------------------------- 状态徽章 */
function statusClass(status) {
  if (!status) return "st-default";
  if (status.startsWith("dispatched")) return "st-ok";
  if (status.startsWith("awaiting")) return "st-wait";
  if (status.endsWith("rejected") || status === "dispatch_failed" || status === "needs_transcript") return "st-bad";
  if (status.endsWith("draft") || status === "processing") return "st-info";
  if (status === "minutes_confirmed") return "st-info";
  if (status === "tasks_confirmed") return "st-ok";
  return "st-default";
}
function badge(status) {
  return `<span class="badge ${statusClass(status)}">${esc(status)}</span>`;
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString("zh-CN", { hour12: false });
}
function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString("zh-CN", { hour12: false });
}

/* ---------------------------------------------------------- 会议列表 */
async function loadList() {
  state.meetings = await call(() => api(`/meetings`));
  renderList();
  if (state.selectedId && !state.meetings.some(m => m.id === state.selectedId)) {
    state.selectedId = null; state.meeting = null; renderDetail();
  }
}

function renderList() {
  const ul = $("#meeting-list");
  $("#meeting-list-empty").style.display = state.meetings.length ? "none" : "";
  ul.innerHTML = state.meetings.map(m => `
    <li class="meeting-item ${m.id === state.selectedId ? "active" : ""}" data-id="${esc(m.id)}">
      <div class="mi-title">${esc(m.title)}</div>
      <div class="mi-meta">${badge(m.status)}<span>${esc(m.start_at ? fmtTime(m.start_at) : "")}</span></div>
    </li>`).join("");
  ul.querySelectorAll(".meeting-item").forEach(li => {
    li.addEventListener("click", () => selectMeeting(li.dataset.id));
  });
}

async function selectMeeting(id) {
  state.selectedId = id;
  state.meeting = await call(() => api(`/meetings/${enc(id)}`));
  renderList();
  renderDetail();
}

async function refreshDetail() {
  if (!state.selectedId) return;
  state.meeting = await call(() => api(`/meetings/${enc(state.selectedId)}`));
  renderDetail();
}

/* ---------------------------------------------------------- 新建会议 */
function parseParticipants(text) {
  return text.split("\n").map(s => s.trim()).filter(Boolean).map((line, i) => {
    const parts = line.split(/[,，]/).map(s => s.trim());
    if (parts.length >= 2) return { user_id: parts[0], name: parts[1], role: parts[2] || null };
    return { user_id: "u-" + (i + 1) + "-" + parts[0], name: parts[0], role: null };
  });
}

function parseTranscript(text) {
  return text.split("\n").map(s => s.trim()).filter(Boolean).map((line, i) => {
    const m = line.match(/^([^:：]+)[:：]\s*(.*)$/);
    const speaker = m ? m[1].trim() : "发言人";
    const content = m ? m[2] : line;
    return { id: i + 1, speaker, start_sec: i * 30, end_sec: i * 30 + 30, text: content };
  });
}

async function createMeeting(payload) {
  const m = await call(() => api(`/meetings`, { method: "POST", body: payload }));
  toast("会议已创建：" + m.id);
  await loadList();
  await selectMeeting(m.id);
}

const SAMPLE_MEETING = {
  id: "demo-sample",
  title: "示例：支付联调与上线时间评审会",
  start_at: "2026-10-23T14:00:00+08:00",
  timezone: "Asia/Shanghai",
  participants: [
    { user_id: "u-zhangsan", name: "张三", role: "organizer" },
    { user_id: "u-lisi", name: "李四", role: "attendee" },
    { user_id: "u-wangwu", name: "王五", role: "attendee" }
  ],
  transcript: [
    { id: 1, speaker: "张三", start_sec: 0, end_sec: 30, text: "今天主要对一下支付联调和上线时间，目前支付联调还没完成。" },
    { id: 2, speaker: "李四", start_sec: 30, end_sec: 65, text: "支付接口联调还差证书，支付渠道证书11月1日就过期了，这是个风险。" },
    { id: 3, speaker: "张三", start_sec: 65, end_sec: 95, text: "那就决定一下，上线推迟到11月8日吧，大家确认一下。" },
    { id: 4, speaker: "王五", start_sec: 95, end_sec: 125, text: "同意，上线推迟到11月8日，灰度比例还没定，待产品确认。" },
    { id: 5, speaker: "张三", start_sec: 125, end_sec: 160, text: "李四，你来负责支付渠道证书续期，下周三之前完成证书续期并提交联调。" },
    { id: 6, speaker: "李四", start_sec: 160, end_sec: 185, text: "好的，我下周三之前完成证书续期并提交联调。" },
    { id: 7, speaker: "王五", start_sec: 185, end_sec: 215, text: "我来负责灰度方案，明天提交灰度方案给大家评审。" },
    { id: 8, speaker: "张三", start_sec: 215, end_sec: 250, text: "王五负责跟进支付联调测试，11月1日前完成联调测试报告。" },
    { id: 9, speaker: "李四", start_sec: 250, end_sec: 280, text: "下次开会我们下周二下午再对一次进度吧。" },
    { id: 10, speaker: "张三", start_sec: 280, end_sec: 310, text: "好，下周二下午开跟进会，确认证书和联调结果。" }
  ]
};

/* ---------------------------------------------------------- 详情渲染 */
function segChips(ids) {
  if (!ids || !ids.length) return "";
  return `<span class="chips">证据：${ids.map(id =>
    `<button type="button" class="chip" data-seg="${id}">#${id}</button>`).join("")}</span>`;
}

function renderDetail() {
  const empty = $("#detail-empty"), body = $("#detail-body");
  const m = state.meeting;
  if (!m) { empty.classList.remove("hidden"); body.classList.add("hidden"); return; }
  empty.classList.add("hidden"); body.classList.remove("hidden");

  const dispatched = ["dispatched", "dispatched_with_failures", "dispatch_failed"].includes(m.status);
  let html = "";

  html += `<div class="card">
    <div class="detail-head"><h2>${esc(m.title)}</h2>${badge(m.status)}</div>
    <div class="muted">ID：${esc(m.id)}${m.start_at ? " · " + esc(fmtTime(m.start_at)) : ""} · ${esc(m.timezone || "")}</div>
    <div class="chips">参会人：${(m.participants || []).map(p =>
      `<span class="chip static">${esc(p.name)}${p.role ? "（" + esc(p.role) + "）" : ""}</span>`).join("") || "（无）"}</div>
    <div class="row actions">
      ${dispatched ? "" : `<button type="button" id="btn-process" class="primary">处理会议（生成纪要）</button>
      <label class="inline"><input type="checkbox" id="cb-auto-min"> 自动确认纪要</label>
      <label class="inline"><input type="checkbox" id="cb-auto-tasks"> 自动确认任务</label>`}
      <button type="button" id="btn-brief">生成 / 刷新 Brief</button>
      <button type="button" id="btn-propose">预览跟进会候选时间</button>
      <button type="button" id="btn-refresh-detail">刷新详情</button>
    </div>
    ${gatePanel(m)}
    ${(m.warnings && m.warnings.length) ? `<div class="warnings"><strong>提示</strong><ul>${m.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    ${(m.rejections && m.rejections.length) ? `<div class="warnings"><strong>驳回记录</strong><ul>${m.rejections.map(r =>
      `<li>${esc(r.gate)} 闸门被驳回${r.reason ? "：原因「" + esc(r.reason) + "」" : ""}（${esc(fmtTime(r.at))}）</li>`).join("")}</ul></div>` : ""}
  </div>`;

  if (m.brief) {
    html += `<div class="card"><h3>会前 Brief</h3><div class="md">${renderMd(m.brief.content || "")}</div>
      <div class="muted">生成于 ${esc(fmtTime(m.brief.generated_at))}</div></div>`;
  }

  if (m.transcript && m.transcript.length) {
    html += `<div class="card"><h3>逐字稿（${m.transcript.length} 段）</h3>
      ${m.transcript.map(s => `<div class="seg" id="seg-${s.id}">
        <span class="seg-id">#${s.id}</span>
        <span class="seg-speaker">${esc(s.speaker)}</span>
        <span class="seg-time">${s.start_sec}–${s.end_sec}s</span>
        <span class="seg-text">${esc(s.text)}</span></div>`).join("")}
    </div>`;
  }

  html += `<div class="card"><h3>补充 / 替换逐字稿</h3>
    <textarea id="ta-transcript" rows="4" placeholder="每行一段：说话人：内容"></textarea>
    <div class="row"><button type="button" id="btn-transcript">提交逐字稿</button></div>
    <h4>上传录音（ASR 转写，下次「处理会议」时生效）</h4>
    <div class="row"><input type="file" id="file-audio" accept=".mp3,.wav,.m4a,.flac,.ogg,.webm,.mp4">
    <button type="button" id="btn-audio">上传录音</button></div>
  </div>`;

  if (m.minutes || m.executive_summary) {
    html += `<div class="card"><h3>纪要</h3>
      ${m.generation_source ? `<div class="muted">生成来源：${esc(m.generation_source)}${m.generation_model ? " · " + esc(m.generation_model) : ""}</div>` : ""}
      <p>${esc(m.executive_summary || m.summary || "")}</p>
      <h4>决策</h4><ul>${(m.decisions || []).map(d =>
        `<li>${esc(d.text)} ${segChips(d.segment_ids)}</li>`).join("") || "<li class='muted'>（无）</li>"}</ul>
      <h4>风险</h4><ul>${(m.risks || []).map(r =>
        `<li>[${esc(r.severity)}] ${esc(r.text)} ${segChips(r.segment_ids)}</li>`).join("") || "<li class='muted'>（无）</li>"}</ul>
      <h4>未决问题</h4><ul>${(m.open_questions || []).map(q =>
        `<li>${esc(q)}</li>`).join("") || "<li class='muted'>（无）</li>"}</ul>
      ${m.detailed_minutes ? `<details><summary>详细纪要</summary><div class="md">${renderMd(m.detailed_minutes)}</div></details>` : ""}
    </div>`;
  }

  if (m.action_items && m.action_items.length) {
    const canPatch = m.status === "awaiting_tasks_confirmation";
    html += `<div class="card"><h3>任务（${m.action_items.length}）</h3>
      ${m.tasks_generation_source ? `<div class="muted">抽取来源：${esc(m.tasks_generation_source)}</div>` : ""}
      <table class="tasks"><thead><tr>
        <th>任务</th><th>负责人</th><th>置信度</th><th>到期</th><th>优先级</th><th>状态</th><th>外部 ID</th>${canPatch ? "<th>改派</th>" : ""}
      </tr></thead><tbody>
      ${m.action_items.map(a => `<tr>
        <td>${esc(a.title)} ${segChips(a.source_segment_ids)}</td>
        <td>${esc(a.owner_raw || "")}${a.owner_id ? "（" + esc(a.owner_id) + "）" : ""}</td>
        <td>${a.owner_confidence != null ? a.owner_confidence.toFixed(2) : ""}</td>
        <td>${esc(a.due_raw || "")}${a.due_date ? " → " + esc(a.due_date) : ""}</td>
        <td>${esc(a.priority)}</td><td>${esc(a.status)}</td>
        <td>${esc(a.external_task_id || a.dispatch_error || "")}</td>
        ${canPatch ? `<td>${patchForm(m, a)}</td>` : ""}
      </tr>`).join("")}
      </tbody></table></div>`;
  }

  if (m.follow_up_candidate_times && m.follow_up_candidate_times.length) {
    html += `<div class="card"><h3>跟进会候选时间</h3><ul>${m.follow_up_candidate_times.map(t =>
      `<li>${esc(fmtTime(t))}</li>`).join("")}</ul></div>`;
  }

  if (m.events && m.events.length) {
    html += `<div class="card"><h3>事件时间线（消息总线审计日志，共 ${m.events.length} 条）</h3>
      <div class="timeline">${m.events.map(e => `<div class="event">
        <span class="ev-time">${esc(fmtClock(e.at))}</span>
        <span class="ev-type">${esc(e.type)}</span>
        <span class="ev-payload">${esc(JSON.stringify(e.payload || {}))}</span>
      </div>`).join("")}</div></div>`;
  }

  body.innerHTML = html;
  wireDetail(m);
}

function gatePanel(m) {
  if (m.status === "awaiting_minutes_confirmation") {
    return `<div class="gate"><strong>闸门 1 · 纪要待确认</strong>
      <div class="row">
        <button type="button" id="btn-confirm-minutes" class="primary">确认纪要（进入任务抽取）</button>
        <input id="in-reject-minutes" type="text" placeholder="驳回原因（可选）">
        <button type="button" id="btn-reject-minutes" class="danger">驳回纪要</button>
      </div></div>`;
  }
  if (m.status === "minutes_rejected") {
    return `<div class="gate"><strong>纪要已被驳回</strong><span class="muted">修改后重新点「处理会议」生成新纪要。</span></div>`;
  }
  if (m.status === "minutes_confirmed") {
    return `<div class="gate"><strong>纪要已确认</strong><span class="muted">任务抽取已在编排配置中关闭（pipeline.steps.tasks），本次流程到此结束，没有待确认的任务。</span></div>`;
  }
  if (m.status === "awaiting_tasks_confirmation") {
    return `<div class="gate"><strong>闸门 2 · 任务待确认</strong>
      <div class="row">
        <button type="button" id="btn-confirm-tasks" class="primary">确认任务（提议日程并分发）</button>
        <input id="in-reject-tasks" type="text" placeholder="驳回原因（可选）">
        <button type="button" id="btn-reject-tasks" class="danger">驳回任务</button>
      </div><div class="muted">分发前可在下方任务表里改派负责人 / 到期 / 优先级。</div></div>`;
  }
  if (m.status === "tasks_rejected") {
    return `<div class="gate"><strong>任务已被驳回</strong>
      <div class="row"><button type="button" id="btn-reextract" class="primary">重新抽取任务</button></div></div>`;
  }
  return "";
}

function patchForm(m, a) {
  const owners = (m.participants || []).map(p =>
    `<option value="${esc(p.user_id)}" ${p.user_id === a.owner_id ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  return `<form class="patch" data-item="${esc(a.id)}">
    <select name="owner_id"><option value="">（不改负责人）</option>${owners}</select>
    <input name="due_date" type="date" value="${esc(a.due_date || "")}">
    <select name="priority">${["P0", "P1", "P2"].map(p =>
      `<option ${p === a.priority ? "selected" : ""}>${p}</option>`).join("")}</select>
    <button type="submit">保存</button></form>`;
}

function wireDetail(m) {
  const id = m.id;
  const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };

  on("#btn-refresh-detail", "click", async () => { await refreshDetail(); await loadList(); });
  on("#btn-process", "click", async () => {
    await call(() => api(`/meetings/${enc(id)}/process`, {
      method: "POST",
      body: {
        auto_approve_minutes: !!($("#cb-auto-min") && $("#cb-auto-min").checked),
        auto_approve_tasks: !!($("#cb-auto-tasks") && $("#cb-auto-tasks").checked)
      }
    }));
    toast("处理完成"); await refreshDetail(); await loadList();
  });
  on("#btn-brief", "click", async () => {
    state.meeting = await call(() => api(`/meetings/${enc(id)}/brief`, { method: "POST" }));
    toast("Brief 已生成"); renderDetail();
  });
  on("#btn-propose", "click", async () => {
    const r = await call(() => api(`/calendar/propose`, { method: "POST", body: { meeting_id: id } }));
    showToolOutput("跟进会候选时间（预览）", `<ul>${(r.candidates || []).map(c =>
      `<li>${esc(fmtTime(c))}</li>`).join("")}</ul>`);
  });
  on("#btn-confirm-minutes", "click", async () => {
    state.meeting = await call(() => api(`/meetings/${enc(id)}/confirm-minutes`, { method: "POST" }));
    toast("纪要已确认，任务已抽取"); renderDetail(); await loadList();
  });
  on("#btn-reject-minutes", "click", async () => {
    const reason = ($("#in-reject-minutes") || {}).value || "";
    state.meeting = await call(() => api(`/meetings/${enc(id)}/reject-minutes`, { method: "POST", body: { reason } }));
    toast("纪要已驳回"); renderDetail(); await loadList();
  });
  on("#btn-confirm-tasks", "click", async () => {
    state.meeting = await call(() => api(`/meetings/${enc(id)}/confirm-tasks`, { method: "POST" }));
    toast("任务已确认并分发"); renderDetail(); await loadList();
  });
  on("#btn-reject-tasks", "click", async () => {
    const reason = ($("#in-reject-tasks") || {}).value || "";
    state.meeting = await call(() => api(`/meetings/${enc(id)}/reject-tasks`, { method: "POST", body: { reason } }));
    toast("任务已驳回"); renderDetail(); await loadList();
  });
  on("#btn-reextract", "click", async () => {
    state.meeting = await call(() => api(`/meetings/${enc(id)}/reextract-tasks`, { method: "POST" }));
    toast("任务已重新抽取"); renderDetail(); await loadList();
  });
  on("#btn-transcript", "click", async () => {
    const segs = parseTranscript(($("#ta-transcript") || {}).value || "");
    if (!segs.length) { toast("先粘贴逐字稿", true); return; }
    state.meeting = await call(() => api(`/meetings/${enc(id)}/transcript`, { method: "POST", body: { transcript: segs } }));
    toast("逐字稿已提交"); renderDetail();
  });
  on("#btn-audio", "click", async () => {
    const f = ($("#file-audio") || {}).files;
    if (!f || !f.length) { toast("先选择录音文件", true); return; }
    const fd = new FormData();
    fd.append("file", f[0]);
    try {
      const resp = await fetch(`/meetings/${enc(id)}/audio`, { method: "POST", body: fd });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error((data && data.detail) || resp.statusText);
      toast("录音已上传：" + data.filename);
      await refreshDetail();
    } catch (e) { toast("上传失败：" + e.message, true); }
  });

  document.querySelectorAll(".chip[data-seg]").forEach(chip => {
    chip.addEventListener("click", () => {
      const seg = $("#seg-" + chip.dataset.seg);
      document.querySelectorAll(".seg.hl").forEach(s => s.classList.remove("hl"));
      if (seg) { seg.classList.add("hl"); seg.scrollIntoView({ block: "center", behavior: "smooth" }); }
    });
  });

  document.querySelectorAll("form.patch").forEach(form => {
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const itemId = form.dataset.item;
      const fd = new FormData(form);
      const patch = {};
      if (fd.get("owner_id")) patch.owner_id = fd.get("owner_id");
      if (fd.get("due_date")) patch.due_date = fd.get("due_date");
      if (fd.get("priority")) patch.priority = fd.get("priority");
      state.meeting = await call(() => api(`/meetings/${enc(id)}/tasks/${enc(itemId)}`, { method: "PATCH", body: patch }));
      toast("任务已改派"); renderDetail();
    });
  });
}

/* ---------------------------------------------------------- 工具区 */
function showToolOutput(title, html) {
  $("#tool-output-title").textContent = title;
  $("#tool-output-body").innerHTML = html;
  $("#tool-output").classList.remove("hidden");
}

function renderMd(md) {
  const lines = String(md || "").split("\n");
  let html = "", inList = false;
  const inline = s => esc(s).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  for (const line of lines) {
    const t = line.trim();
    if (t.startsWith("- ") || t.startsWith("* ")) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(t.slice(2))}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    if (t.startsWith("### ")) html += `<h5>${inline(t.slice(4))}</h5>`;
    else if (t.startsWith("## ")) html += `<h4>${inline(t.slice(3))}</h4>`;
    else if (t.startsWith("# ")) html += `<h3>${inline(t.slice(2))}</h3>`;
    else if (t) html += `<p>${inline(t)}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
}

/* ---------------------------------------------------------- 启动 */
document.addEventListener("DOMContentLoaded", () => {
  $("#btn-refresh").addEventListener("click", () => loadList());
  $("#tool-output-close").addEventListener("click", () => $("#tool-output").classList.add("hidden"));
  $("#btn-reminders").addEventListener("click", async () => {
    const r = await call(() => api(`/reminders/run`, { method: "POST" }));
    showToolOutput("提醒扫描结果", `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`);
  });
  $("#btn-gate-timeouts").addEventListener("click", async () => {
    const r = await call(() => api(`/gate-timeouts/run`, { method: "POST" }));
    showToolOutput("闸门超时扫描结果", `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`);
  });
  $("#btn-weekly").addEventListener("click", async () => {
    const r = await call(() => api(`/weekly-summary/run?window_days=7`, { method: "POST" }));
    showToolOutput("任务周报（" + esc(r.period_start) + " ~ " + esc(r.period_end) + "）",
      `<div class="muted">投递状态：${r.delivered ? "已投递" : "未投递"}（${esc(r.delivery_note || "")}）</div>` +
      `<div class="md">${renderMd(r.summary || "")}</div>`);
  });
  $("#btn-create").addEventListener("click", async () => {
    const title = $("#f-title").value.trim();
    if (!title) { toast("先填会议标题", true); return; }
    const id = $("#f-id").value.trim() || ("m-" + Date.now());
    const startVal = $("#f-start").value;
    await createMeeting({
      id, title,
      start_at: startVal ? new Date(startVal).toISOString() : null,
      participants: parseParticipants($("#f-participants").value),
      transcript: parseTranscript($("#f-transcript").value)
    });
  });
  $("#btn-sample").addEventListener("click", async () => {
    const exists = state.meetings.some(m => m.id === SAMPLE_MEETING.id);
    if (exists) { await selectMeeting(SAMPLE_MEETING.id); return; }
    await createMeeting(JSON.parse(JSON.stringify(SAMPLE_MEETING)));
  });
  $("#btn-import-json").addEventListener("click", async () => {
    let payload;
    try { payload = JSON.parse($("#f-json").value); }
    catch (e) { toast("JSON 解析失败：" + e.message, true); return; }
    await createMeeting(payload);
  });
  loadList().catch(() => {});
});
