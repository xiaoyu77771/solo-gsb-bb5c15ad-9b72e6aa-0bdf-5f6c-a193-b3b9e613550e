/* 匿名评审分配系统 —— 原生 JS 前端（无框架、无构建） */
"use strict";

let state = null;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* --------------------------------------------------------------- API */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `请求失败（${res.status}）`);
  }
  return data;
}

async function refresh() {
  state = await api("/api/state");
  render();
}

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function runAction(promise, okMsg) {
  try {
    const data = await promise;
    state = data;
    render();
    if (okMsg) toast(okMsg);
    return data;
  } catch (e) {
    toast(e.message, true);
    throw e;
  }
}

/* --------------------------------------------------------------- 渲染：总览 */
function indexMaps() {
  state._mById = Object.fromEntries(state.manuscripts.map((m) => [m.id, m]));
  state._rById = Object.fromEntries(state.reviewers.map((r) => [r.id, r]));
  state._assignByM = {};
  for (const a of state.assignments) {
    (state._assignByM[a.manuscript_id] ||= []).push(a);
  }
  state._conflictSet = new Set(state.conflicts.map((c) => `${c.manuscript_id}:${c.reviewer_id}`));
  // 实际主审（稿件:评审人）集合，由最近一次求解结果给出
  state._leadSet = new Set((state.solution?.lead_pairs || []).map(([m, r]) => `${m}:${r}`));
}

function renderSummary() {
  const sol = state.solution;
  const cards = [];
  if (sol) {
    cards.push(
      stat(sol.total_assigned, `已满足评审数（共需 ${sol.total_required}）`,
        sol.total_assigned >= sol.total_required ? "ok" : "bad"),
      stat(sol.fully_satisfied ? "完整" : `${sol.unmet.length} 篇未满足`,
        "分配完整性", sol.fully_satisfied ? "ok" : "bad"),
      stat(`${sol.total_lead_assigned ?? 0} / ${sol.total_lead_required ?? 0}`, "主审已认定 / 共需",
        (sol.total_lead_assigned ?? 0) >= (sol.total_lead_required ?? 0) ? "ok" : "bad"),
      stat(sol.total_cost, "目标函数总费用（越小越优）"),
      stat(sol.locked_count, "锁定的既有分配"),
      stat(sol.total_capacity, "评审人总容量"),
    );
  } else {
    cards.push(stat(state.manuscripts.length, "稿件数"));
    cards.push(stat(state.reviewers.length, "评审人数"));
    cards.push(stat(state.assignments.filter((a) => a.locked).length, "锁定分配"));
  }
  $("#summary-cards").innerHTML = cards.join("");
}

function stat(num, label, cls = "") {
  return `<div class="stat ${cls}"><div class="num">${esc(num)}</div><div class="label">${esc(label)}</div></div>`;
}

function renderAlerts() {
  const sol = state.solution;
  if (!sol) {
    $("#warnings-box").innerHTML = "";
    $("#unmet-box").innerHTML =
      `<div class="alert warn">尚未求解，点击右上角「重新求解分配」生成完整分配。</div>`;
    return;
  }

  $("#warnings-box").innerHTML = sol.warnings.length
    ? `<div class="alert warn"><strong>⚠️ 求解警告（${sol.warnings.length}）</strong><ul>` +
      sol.warnings.map((w) => `<li>${esc(w.message)}</li>`).join("") + `</ul></div>`
    : (sol.fully_satisfied
      ? `<div class="alert success">✅ 全部 ${sol.total_required} 个评审需求均已满足，且未使用任何冲突边。</div>`
      : "");

  if (!sol.unmet.length) {
    $("#unmet-box").innerHTML = "";
    return;
  }

  $("#unmet-box").innerHTML = sol.unmet.map((u) => {
    const detailRows = u.details.candidates
      .map((c) => `<tr class="${c.blocked_by_institution ? "row-blocked" : ""}">
          <td>${esc(c.name)}</td>
          <td>${c.can_lead
            ? '<span class="tag lead-tag">⭐ 可任主审</span>'
            : '<span class="hint">—</span>'}</td>
          <td>${c.institution ? `🏛 ${esc(c.institution)}` : '<span class="hint">未填写</span>'}</td>
          <td>${c.remaining_capacity > 0
            ? `剩余容量 ${c.remaining_capacity}`
            : '<span style="color:#c63b3b">容量已满</span>'}</td>
          <td>${c.uncovered_topics === 0
            ? '<span style="color:#178a52">主题全覆盖</span>'
            : `未覆盖主题 ${c.uncovered_topics} 个`}</td>
          <td>${c.blocked_by_institution
            ? '<span class="excl">被机构上限排除</span>' : ""}</td>
        </tr>`).join("");
    const instBlocked = u.details.blocked_by_institution || 0;
    const headParts = [];
    if (u.deficit > 0) headParts.push(`需要 ${u.required} 名评审，仅安排 ${u.assigned} 名，缺 ${u.deficit} 名`);
    if (u.lead_deficit > 0) headParts.push(`需主审 ${u.lead_required} 名，实际 ${u.lead_assigned} 名，主审缺口 ${u.lead_deficit} 名`);
    return `<div class="alert danger">
      <strong>⛔ 《${esc(u.title)}》未满足：${headParts.join("；")}。</strong>
      <ul>${u.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <div class="reason-detail">
        <strong>可复核明细</strong>（共 ${u.details.reviewer_count} 名评审，
        冲突排除 ${u.details.blocked_by_conflict} 名${
          u.details.assigned_here ? `，已评审本稿 ${u.details.assigned_here} 名（不可重复）` : ""}${
          instBlocked ? `，机构上限排除 ${instBlocked} 名` : ""}，
        其余剩余容量合计 ${u.details.candidate_remaining_capacity}${
          instBlocked ? `；扣除机构上限后实际可用 ${u.details.effective_remaining_capacity}` : ""}${
          u.lead_required > 0 ? `；可任主审者 ${u.details.lead_pool} 名、当前可补充主审 ${u.details.lead_available} 名` : ""}）
        <table>
          <tr><th>评审人</th><th>主审资格</th><th>机构</th><th>剩余容量</th><th>主题匹配</th><th>排除原因</th></tr>
          ${detailRows}
        </table>
      </div>
    </div>`;
  }).join("");
}

function costInfo(mId, rId) {
  return state.solution?.edge_costs?.[`${mId}:${rId}`] || null;
}

function renderAssignments() {
  if (!state.manuscripts.length) {
    $("#assignment-list").innerHTML = `<div class="empty">暂无稿件，请先到「稿件」页录入。</div>`;
    return;
  }
  $("#assignment-list").innerHTML = state.manuscripts.map((m) => {
    const assigns = state._assignByM[m.id] || [];
    const short = assigns.length < m.required_reviews;
    const leadGot = assigns.filter((a) => state._leadSet.has(`${m.id}:${a.reviewer_id}`)).length;
    const leadShort = (m.required_leads || 0) > leadGot;
    const chips = assigns
      .slice()
      .sort((a, b) => (b.locked - a.locked) || a.reviewer_id - b.reviewer_id)
      .map((a) => {
        const r = state._rById[a.reviewer_id];
        const isLead = state._leadSet.has(`${m.id}:${a.reviewer_id}`);
        const ci = costInfo(m.id, a.reviewer_id);
        const miss = ci && ci.uncovered_topics.length
          ? `<span class="tag miss">未覆盖：${ci.uncovered_topics.map(esc).join("、")}</span>`
          : "";
        const instTxt = r && r.institution ? `<span class="tag inst">🏛 ${esc(r.institution)}</span>` : "";
        const costTxt = ci ? `费用 ${ci.mismatch_cost + ci.load_cost}` : "锁定";
        return `<span class="chip ${a.locked ? "locked" : ""} ${isLead ? "lead" : ""} ${ci && ci.uncovered_topics.length ? "miss-penalty" : ""}">
          ${a.locked ? "🔒" : "👤"} ${esc(r ? r.name : `#${a.reviewer_id}`)}
          ${isLead ? '<span class="tag lead-tag" title="实际主审">⭐ 主审</span>' : ""}
          ${instTxt}
          <span class="cost">${costTxt}</span>
          ${miss}
          <button title="${a.locked ? "解锁并重新求解" : "锁定该既有分配并重新求解"}"
            data-lock="${m.id}" data-reviewer="${a.reviewer_id}"
            data-locked="${a.locked ? "0" : "1"}">${a.locked ? "🔓" : "🔒"}</button>
        </span>`;
      }).join("");

    // 可手工锁定的候选：尚未分配、无冲突
    const usedIds = new Set(assigns.map((a) => a.reviewer_id));
    const options = state.reviewers
      .filter((r) => !usedIds.has(r.id) && !state._conflictSet.has(`${m.id}:${r.id}`))
      .map((r) => `<option value="${r.id}">${esc(r.name)}（${r.institution ? esc(r.institution) + " · " : ""}剩余 ${Math.max(0, r.capacity - r.assigned)}）</option>`)
      .join("");
    const manualLock = options
      ? `<div class="manual-lock">
          <span class="hint">手工锁定既有分配：</span>
          <select id="lock-select-${m.id}"><option value="">选择评审人…</option>${options}</select>
          <button class="btn small" data-manual-lock="${m.id}">🔒 锁定并重算</button>
        </div>`
      : "";

    return `<div class="assignment-card ${short || leadShort ? "short" : "satisfied"}">
      <div class="assignment-head">
        <div>
          <div class="assignment-title">《${esc(m.title)}》</div>
          <div class="entity-sub">主题：${m.topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "（未标注）"}</div>
        </div>
        <div>
          <span class="badge-count ${short ? "no" : "ok"}">${assigns.length} / ${m.required_reviews} 名评审</span>
          ${(m.required_leads || 0) > 0
            ? `<span class="badge-count ${leadShort ? "no" : "ok"}" title="实际主审 / 需主审">⭐ ${leadGot} / ${m.required_leads} 主审</span>`
            : ""}
        </div>
      </div>
      <div class="reviewer-chips">${chips || '<span class="hint">尚未分配</span>'}</div>
      ${compositionLine(m.id, assigns)}
      ${manualLock}
    </div>`;
  }).join("");
}

function compositionLine(mId, assigns) {
  const cap = state.settings.institution_cap || 0;
  const counts = new Map(); // institution -> {count, locked}
  for (const a of assigns) {
    const inst = (state._rById[a.reviewer_id] || {}).institution || "";
    const g = counts.get(inst) || { count: 0, locked: 0 };
    g.count += 1;
    if (a.locked) g.locked += 1;
    counts.set(inst, g);
  }
  const entries = [...counts.entries()].sort((x, y) => {
    if (!x[0] && !y[0]) return 0;
    if (!x[0]) return 1;
    if (!y[0]) return -1;
    return x[0].localeCompare(y[0], "zh");
  });
  const chipsHtml = entries.map(([inst, g]) => {
    const over = cap > 0 && inst && g.count > cap;
    const lockTxt = g.locked ? ` · 锁 ${g.locked}` : "";
    return `<span class="inst-chip ${over ? "over" : ""}" title="${over ? "历史锁定已超过每篇同机构人数上限" : ""}">${
      inst ? `🏛 ${esc(inst)}` : "未填写机构"} × ${g.count}${lockTxt}${over ? " ⚠️" : ""}</span>`;
  }).join("");
  const capTxt = cap > 0
    ? `<span class="hint">机构多样性上限：每篇同机构最多 ${cap} 人</span>`
    : `<span class="hint">机构多样性约束已关闭（设置中填 &gt;0 的上限可启用）</span>`;
  return `<div class="composition">
    <span class="hint">机构构成：</span>${chipsHtml || '<span class="hint">—</span>'} ${capTxt}
  </div>`;
}

function renderReviewerLoad() {
  if (!state.reviewers.length) {
    $("#reviewer-load").innerHTML = `<div class="empty">暂无评审人。</div>`;
    return;
  }
  $("#reviewer-load").innerHTML = state.reviewers.map((r) => {
    const load = r.assigned || 0;
    const pct = r.capacity > 0 ? Math.min(100, Math.round((load / r.capacity) * 100)) : (load > 0 ? 100 : 0);
    const over = load > r.capacity;
    return `<div class="load-row">
      <div class="load-name">${esc(r.name)}
        <div class="entity-sub" style="width:130px">${
          (r.institution ? `🏛 ${esc(r.institution)}` : "") || r.topics.slice(0, 2).map(esc).join(" / ")}</div>
      </div>
      <div class="load-bar-wrap ${over ? "over" : ""}">
        <div class="load-bar ${over ? "over" : ""}" style="width:${pct}%"></div>
      </div>
      <div class="load-text" style="color:${over ? "var(--red)" : ""}">
        ${load} / ${r.capacity}${r.locked_assigned ? `（锁 ${r.locked_assigned}）` : ""}
      </div>
    </div>`;
  }).join("");
}

/* --------------------------------------------------------------- 渲染：稿件/评审人 */
function renderManuscripts() {
  if (!state.manuscripts.length) {
    $("#manuscript-list").innerHTML = `<div class="empty">还没有稿件，使用上方表单录入。</div>`;
    return;
  }
  $("#manuscript-list").innerHTML = state.manuscripts.map((m) => `
    <div class="entity-row" data-mrow="${m.id}">
      <div class="entity-main">
        <div class="entity-title">《${esc(m.title)}》</div>
        <div class="entity-sub">
          需求 ${m.required_reviews} 名评审${m.required_leads ? `（含主审 ${m.required_leads} 名）` : ""} · 已安排 ${m.assigned || 0} ·
          主题：${m.topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "（未标注）"}
        </div>
      </div>
      <div class="entity-actions">
        <button class="btn small" data-edit-m="${m.id}">编辑</button>
        <button class="btn small danger-ghost" data-del-m="${m.id}">删除</button>
      </div>
    </div>`).join("");

  // 表单默认需求
  const defReq = state.settings.default_required_reviews;
  const reqInput = $('form#form-manuscript [name="required_reviews"]');
  if (reqInput && !reqInput.dataset.touched) reqInput.placeholder = `所需评审数（默认 ${defReq}）`;
  const leadsInput = $('form#form-manuscript [name="required_leads"]');
  if (leadsInput && !leadsInput.dataset.touched) leadsInput.placeholder = "需主审人数 0-2（默认 0）";
}

function renderReviewers() {
  if (!state.reviewers.length) {
    $("#reviewer-list").innerHTML = `<div class="empty">还没有评审人，使用上方表单录入。</div>`;
    return;
  }
  $("#reviewer-list").innerHTML = state.reviewers.map((r) => `
    <div class="entity-row">
      <div class="entity-main">
        <div class="entity-title">${esc(r.name)}
          ${r.can_lead ? '<span class="tag lead-tag">⭐ 可任主审</span>' : ""}
          ${r.locked_assigned ? '<span class="tag locked-tag">🔒 ' + r.locked_assigned + ' 锁定</span>' : ""}
        </div>
        <div class="entity-sub">
          容量 ${r.capacity} · 已安排 ${r.assigned || 0}${r.assigned > r.capacity ? " <b style='color:var(--red)'>(超容)</b>" : ""} ·
          机构：${r.institution ? `<span class="tag inst">${esc(r.institution)}</span>` : "（未填写，不计入机构上限）"} ·
          擅长：${r.topics.map((t) => `<span class="tag">${esc(t)}</span>`).join("") || "（未标注）"}
        </div>
      </div>
      <div class="entity-actions">
        <button class="btn small" data-edit-r="${r.id}">编辑</button>
        <button class="btn small danger-ghost" data-del-r="${r.id}">删除</button>
      </div>
    </div>`).join("");

  const defCap = state.settings.default_capacity;
  const capInput = $('form#form-reviewer [name="capacity"]');
  if (capInput && !capInput.dataset.touched) capInput.placeholder = `个人容量（默认 ${defCap}）`;
}

/* --------------------------------------------------------------- 渲染：冲突矩阵 */
function renderConflicts() {
  if (!state.manuscripts.length || !state.reviewers.length) {
    $("#conflict-matrix").innerHTML =
      `<div class="empty">需要至少一篇稿件和一名评审人才能编辑冲突。</div>`;
    return;
  }
  const head = `<tr><th>稿件 ＼ 评审人</th>${
    state.reviewers.map((r) => `<th>${esc(r.name)}</th>`).join("")}</tr>`;
  const rows = state.manuscripts.map((m) => `<tr>
    <td class="mtitle">《${esc(m.title)}》</td>
    ${state.reviewers.map((r) => {
      const checked = state._conflictSet.has(`${m.id}:${r.id}`);
      const locked = state.assignments.some(
        (a) => a.manuscript_id === m.id && a.reviewer_id === r.id && a.locked);
      return `<td><input type="checkbox" data-conflict="${m.id}" data-reviewer="${r.id}"
        ${checked ? "checked" : ""} ${locked ? "disabled" : ""}
        title="${locked ? "该分配已锁定，请先解锁" : "勾选表示存在作者冲突"}"></td>`;
    }).join("")}
  </tr>`).join("");
  $("#conflict-matrix").innerHTML = `<table class="matrix">${head}${rows}</table>`;
}

/* --------------------------------------------------------------- 渲染：自助申报链接 */
const DECL_STATUS = {
  none: { label: "未签发", cls: "decl-none" },
  pending: { label: "待提交", cls: "decl-pending" },
  stale: { label: "待提交 · 清单已变化", cls: "decl-stale" },
  submitted: { label: "已提交", cls: "decl-submitted" },
  revoked: { label: "已失效", cls: "decl-revoked" },
};

function latestDeclarationByReviewer() {
  // 每位评审人取最新一条申报（id 最大）
  const latest = new Map();
  for (const d of state.declarations || []) {
    const cur = latest.get(d.reviewer_id);
    if (!cur || d.id > cur.id) latest.set(d.reviewer_id, d);
  }
  return latest;
}

function declStatusOf(d) {
  if (!d) return "none";
  if (d.status === "pending") return d.stale ? "stale" : "pending";
  return d.status; // submitted / revoked
}

function renderDeclarations() {
  if (!state.reviewers.length) {
    $("#declaration-list").innerHTML =
      `<div class="empty">暂无评审人，请先到「评审人」页录入。</div>`;
    return;
  }
  const latest = latestDeclarationByReviewer();
  $("#declaration-list").innerHTML = state.reviewers.map((r) => {
    const d = latest.get(r.id);
    const status = declStatusOf(d);
    const meta = DECL_STATUS[status];
    let detail = "";
    let actions = `<button class="btn small primary" data-decl-issue="${r.id}">
      ${d ? "重新生成链接" : "生成申报链接"}</button>`;
    if (d) {
      if (d.status === "pending") {
        detail = `签发于 ${d.created_at} · 清单 ${d.manifest_count} 篇` +
          (d.stale ? ` · <span class="excl">稿件已增删或标题变化，评审人提交将返回 409，请重新生成</span>` : "");
        actions += ` <button class="btn small danger-ghost" data-decl-revoke="${d.id}">撤销链接</button>`;
      } else if (d.status === "submitted") {
        const n = (d.submitted_conflicts || []).length;
        detail = `提交于 ${d.submitted_at || "—"} · 勾选冲突 ${n} 篇（已合并进冲突矩阵）`;
      } else if (d.status === "revoked") {
        detail = `失效于 ${d.revoked_at || "—"} · ${esc(d.revoked_reason || "已撤销")}`;
      }
    } else {
      detail = "尚未生成过申报链接";
    }
    return `<div class="entity-row" data-decl-row="${r.id}">
      <div class="entity-main">
        <div class="entity-title">${esc(r.name)}</div>
        <div class="entity-sub">
          <span class="decl-badge ${meta.cls}">${meta.label}</span>
          ${detail}
        </div>
      </div>
      <div class="entity-actions">${actions}</div>
    </div>`;
  }).join("");
}

/* --------------------------------------------------------------- 退出补位 */
let backfillPreview = null;

function renderBackfillSelect() {
  const sel = $("#bf-reviewer");
  if (!sel) return;
  const prev = sel.value;
  const options = state.reviewers
    .slice()
    .sort((a, b) => a.id - b.id)
    .map((r) => {
      const inst = r.institution ? ` · 🏛 ${r.institution}` : "";
      const lead = r.can_lead ? " · ⭐ 可任主审" : "";
      const loadTxt = `已安排 ${r.assigned || 0}（锁 ${r.locked_assigned || 0}）/ 容量 ${r.capacity}`;
      return `<option value="${r.id}">${esc(r.name)}（${loadTxt}${inst}${lead}）</option>`;
    })
    .join("");
  sel.innerHTML = state.reviewers.length
    ? options
    : '<option value="">（暂无评审人）</option>';
  if (prev && state.reviewers.some((r) => String(r.id) === prev)) sel.value = prev;
  // 当前预览指向的评审人已被删除 → 丢弃预览
  if (backfillPreview && !state.reviewers.some((r) => r.id === backfillPreview.reviewer_id)) {
    backfillPreview = null;
  }
}

function gapDeltaCell(after, before, goodWhenShrink = true) {
  const d = after - before;
  if (d === 0) return `<span class="delta-same">${before} → ${after}（±0）</span>`;
  const better = goodWhenShrink ? d < 0 : d > 0;
  const sign = d > 0 ? `+${d}` : `${d}`;
  return `<span class="${better ? "delta-down" : "delta-up"}">${before} → ${after}（${sign}，${better ? "更优" : "更差"}）</span>`;
}

function renderBackfill() {
  const box = $("#backfill-result");
  if (!box) return;
  const adoptBtn = $("#btn-backfill-adopt");
  const clearBtn = $("#btn-backfill-clear");
  renderBackfillSelect();
  if (!backfillPreview) {
    box.innerHTML = "";
    adoptBtn.disabled = true;
    clearBtn.disabled = true;
    return;
  }
  const p = backfillPreview;
  adoptBtn.disabled = !!p._stale || !state.reviewers.some((r) => r.id === p.reviewer_id);
  clearBtn.disabled = false;

  const cur = p.current_metrics;
  const rows = [
    ["目标函数总费用（原求解成本）", cur ? cur.total_cost : "—", p.metrics.total_cost,
      p.delta ? deltaCell(p.delta.total_cost) : '<span class="delta-same">—</span>'],
    ["已满足评审数", cur ? `${cur.total_assigned} / ${cur.total_required}` : "—",
      `${p.metrics.total_assigned} / ${p.metrics.total_required}`,
      p.delta ? deltaCell(p.delta.total_assigned, false) : '<span class="delta-same">—</span>'],
    ["未满足人数", cur ? cur.unmet_people : "—", p.metrics.unmet_people,
      p.delta ? deltaCell(p.delta.unmet_people) : '<span class="delta-same">—</span>'],
    ["主审缺口", cur ? cur.lead_deficit : "—", p.metrics.lead_deficit,
      p.delta ? deltaCell(p.delta.lead_deficit) : '<span class="delta-same">—</span>'],
  ].map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td><td>${r[3]}</td></tr>`).join("");

  const pairLine = (x, sign) =>
    `<li>${sign} 《${esc(x.manuscript_title)}》 ← ${esc(x.reviewer_name)}` +
    (x.is_lead ? ' <span class="tag lead-tag">⭐ 主审</span>' : "") + `</li>`;
  const diffList = (items, sign, emptyText) =>
    items.length ? `<ul>${items.map((x) => pairLine(x, sign)).join("")}</ul>`
                 : `<div class="hint">${emptyText}</div>`;

  const exitingHtml = p.exiting.pairs.length
    ? `<div class="sandbox-unmet">
         <strong>退出者移除配对（${p.exiting.pair_count}${
           p.exiting.locked_pair_count ? `，含锁定 ${p.exiting.locked_pair_count}` : ""}）：</strong>
         <ul>${p.exiting.pairs.map((x) => pairLine(x, "🚪")).join("")}</ul>
       </div>`
    : `<div class="sandbox-unmet ok">该评审人当前没有任何分配。</div>`;

  const chainHtml = p.chain_moves.length
    ? `<div class="sandbox-diff" style="display:block">
         <h4>🔁 连锁换位（${p.chain_moves.length} 名评审人）</h4>
         ${p.chain_moves.map((c) => `
           <div class="chain-row">
             <strong>${esc(c.reviewer_name)}</strong>：
             离开 ${c.vacates.map((x) => `《${esc(x.manuscript_title)}》`).join("、") || "—"}
             → 补位 ${c.fills.map((x) => `《${esc(x.manuscript_title)}》`).join("、")}
           </div>`).join("")}
       </div>`
    : "";

  const gapRows = p.gap_changes.map((g) => {
    const worse = g.deficit_after > g.deficit_before || g.lead_deficit_after > g.lead_deficit_before;
    const better = g.deficit_after < g.deficit_before || g.lead_deficit_after < g.lead_deficit_before;
    const cls = worse ? "delta-up" : better ? "delta-down" : "delta-same";
    return `<tr class="${worse ? "row-blocked" : ""}">
      <td>《${esc(g.manuscript_title)}》</td>
      <td>${g.assigned_before} → ${g.assigned_after}（需求 ${g.required}）</td>
      <td class="${cls}">${g.deficit_before} → ${g.deficit_after}</td>
      <td>${g.lead_before} → ${g.lead_after}（需 ${g.lead_required}）</td>
      <td class="${cls}">${g.lead_deficit_before} → ${g.lead_deficit_after}</td>
    </tr>`;
  }).join("");
  const gapHtml = gapRows
    ? `<table class="sandbox-metrics">
         <tr><th>受影响稿件</th><th>已安排人数（前→后）</th><th>人数缺口（前→后）</th>
         <th>主审数（前→后）</th><th>主审缺口（前→后）</th></tr>
         ${gapRows}
       </table>`
    : '<div class="hint">没有稿件的安排发生变化。</div>';

  const noticesHtml = p.notices.length
    ? `<ul class="bf-notices">${p.notices.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>`
    : "";

  const unmetHtml = p.unmet.length
    ? `<div class="sandbox-unmet"><strong>修复后仍有 ${p.unmet.length} 篇未满足：</strong><ul>` +
      p.unmet.map((u) =>
        `<li>《${esc(u.title)}》：缺评审 ${u.deficit} 名` +
        (u.lead_deficit > 0 ? `，主审缺口 ${u.lead_deficit} 名` : "") + `</li>`).join("") +
      `</ul></div>`
    : `<div class="sandbox-unmet ok">✅ 修复方案可满足全部评审需求（含主审）。</div>`;

  const staleHtml = p._stale
    ? `<div class="alert danger" style="margin-top:10px">⚠️ 采用失败：预览生成后基线数据已被修改，
       该预览已过期（服务端返回 409，未写入任何内容）。请「清除预览」后重新「生成修复预览」。</div>`
    : "";

  box.innerHTML = `<div class="sandbox-result-inner ${p._stale ? "stale" : ""}">
    ${staleHtml}
    <div class="sandbox-meta">
      退出评审人：<strong>${esc(p.reviewer_name)}</strong> ｜
      保留既有非锁定配对 <strong>${p.retained_pair_count}</strong> 个、
      新增配对 <strong>${p.new_pair_count}</strong> 个 ｜
      生成于 ${esc(p.created_at)} ｜ 版本指纹 <code>${esc(p.fingerprint.slice(0, 12))}…</code>
    </div>
    ${noticesHtml}
    <table class="sandbox-metrics">
      <tr><th>指标</th><th>当前方案</th><th>修复预览</th><th>变化</th></tr>
      ${rows}
    </table>
    ${exitingHtml}
    <div class="sandbox-diff">
      <div>
        <h4>➕ 新增评审配对（${p.diff.added.length}）</h4>
        ${diffList(p.diff.added, "➕", "无新增配对")}
      </div>
      <div>
        <h4>➖ 移除评审配对（${p.diff.removed.length}）</h4>
        ${diffList(p.diff.removed, "➖", "无移除配对")}
      </div>
    </div>
    ${chainHtml}
    <h4 style="margin:10px 0 4px;font-size:13px">📉 各稿缺口变化</h4>
    ${gapHtml}
    ${unmetHtml}
    <div class="sandbox-meta">采用后：退出者配对（含锁定）全部移除，其余锁定保留，非锁定分配替换为修复结果，求解参数不变。</div>
  </div>`;
}

/* --------------------------------------------------------------- 缺口解困建议 */
let reliefData = null;

function currentHasGap() {
  const sol = state.solution;
  if (!sol) return false;
  const peopleGap = (sol.unmet || []).reduce((s, u) => s + (u.deficit || 0), 0);
  const leadGap = (sol.unmet || []).reduce((s, u) => s + (u.lead_deficit || 0), 0);
  return peopleGap > 0 || leadGap > 0;
}

function renderRelief() {
  const panel = $("#relief-panel");
  const box = $("#relief-result");
  const genBtn = $("#btn-relief-generate");
  const clearBtn = $("#btn-relief-clear");
  if (!panel) return;

  const hasGap = currentHasGap();
  genBtn.disabled = !hasGap;
  genBtn.title = hasGap ? "" : "当前分配不存在人数或主审缺口";
  clearBtn.disabled = !reliefData;
  if (!reliefData) {
    box.innerHTML = hasGap
      ? ""
      : '<div class="empty">当前分配不存在人数缺口或主审缺口，无需解困建议。</div>';
    return;
  }

  const d = reliefData;
  const base = d.baseline_metrics || {};
  const head = `<div class="sandbox-meta">
    基线（当前数据只读重算）：未满足人数 <strong>${base.unmet_people ?? "—"}</strong>、
    主审缺口 <strong>${base.lead_deficit ?? "—"}</strong>、总费用 ${base.total_cost ?? "—"}
    ｜枚举单步调整 <strong>${d.candidate_count}</strong> 个、有效建议 <strong>${d.suggestions.length}</strong> 个
    ｜生成于 ${esc(d.created_at)}
  </div>`;

  if (!d.suggestions.length) {
    box.innerHTML = `<div class="sandbox-result-inner">
      ${head}
      <div class="alert warn" style="margin-top:10px">${esc(d.message)}</div>
    </div>`;
    return;
  }

  const pairLine = (x, sign) =>
    `<li>${sign} 《${esc(x.manuscript_title)}》 ← ${esc(x.reviewer_name)}` +
    (x.is_lead ? ' <span class="tag lead-tag">⭐ 主审</span>' : "") +
    (x.is_locked ? ' <span class="tag locked-tag">🔒 锁定</span>' : "") + `</li>`;

  const cards = d.suggestions.map((s, idx) => {
    const resolveBadge = s.fully_resolves
      ? '<span class="tag" style="background:var(--green-soft);color:var(--green)">✅ 可补齐全部缺口</span>'
      : "";
    const gapBadges = [];
    if (s.deficit_reduction > 0)
      gapBadges.push(`<span class="relief-badge good">人数缺口 −${s.deficit_reduction}</span>`);
    if (s.lead_deficit_reduction > 0)
      gapBadges.push(`<span class="relief-badge lead">主审缺口 −${s.lead_deficit_reduction}</span>`);
    gapBadges.push(`<span class="relief-badge">改动配对 ${s.changed_pair_count}</span>`);
    const costTxt = s.cost_delta === 0
      ? '<span class="delta-same">成本 ±0</span>'
      : s.cost_delta > 0
        ? `<span class="delta-up">成本 +${s.cost_delta}</span>`
        : `<span class="delta-down">成本 ${s.cost_delta}</span>`;

    const affHtml = s.affected_manuscripts.map((g) => {
      const worse = g.deficit_after > g.deficit_before || g.lead_deficit_after > g.lead_deficit_before;
      const better = g.deficit_after < g.deficit_before || g.lead_deficit_after < g.lead_deficit_before;
      const cls = worse ? "delta-up" : better ? "delta-down" : "delta-same";
      const oldLis = g.old_pairs.map((x) => pairLine(x, "➖")).join("");
      const newLis = g.new_pairs.map((x) => pairLine(x, "➕")).join("");
      const keepTxt = g.kept_pairs.length
        ? `<div class="hint">保留配对（${g.kept_pairs.length}）：${
            g.kept_pairs.map((x) => esc(x.reviewer_name) +
              (x.is_lead ? " ⭐" : "") + (x.is_locked ? " 🔒" : "")).join("、")}</div>`
        : "";
      return `<tr class="${worse ? "row-blocked" : ""}">
        <td>《${esc(g.manuscript_title)}》
          <div class="relief-pairs">
            ${g.old_pairs.length ? `<ul>${oldLis}</ul>` : '<span class="hint">无移除配对</span>'}
            ${g.new_pairs.length ? `<ul>${newLis}</ul>` : '<span class="hint">无新增配对</span>'}
            ${keepTxt}
          </div>
        </td>
        <td>${g.assigned_before} → ${g.assigned_after}（需 ${g.required}）</td>
        <td class="${cls}">${g.deficit_before} → ${g.deficit_after}</td>
        <td>${g.lead_before} → ${g.lead_after}（需 ${g.lead_required}）</td>
        <td class="${cls}">${g.lead_deficit_before} → ${g.lead_deficit_after}</td>
      </tr>`;
    }).join("");

    const warnHtml = s.warnings.length
      ? `<details><summary class="hint">求解警告（${s.warnings.length}）</summary>
         <ul class="bf-notices">${s.warnings.map((w) => `<li>${esc(w.message)}</li>`).join("")}</ul></details>`
      : "";

    return `<div class="relief-card">
      <div class="relief-card-head">
        <div><strong>#${idx + 1} ${esc(s.adjustment.description)}</strong> ${resolveBadge}</div>
        <div class="relief-score">${gapBadges.join(" ")} ${costTxt}</div>
      </div>
      <table class="sandbox-metrics relief-table">
        <tr><th>受影响稿件（旧配对 ➖ / 新配对 ➕）</th><th>人数（前→后）</th><th>人数缺口</th>
        <th>主审（前→后）</th><th>主审缺口</th></tr>
        ${affHtml}
      </table>
      ${warnHtml}
    </div>`;
  }).join("");

  box.innerHTML = `<div class="sandbox-result-inner">
    ${head}
    <div class="sandbox-meta">${esc(d.message)}</div>
    ${cards}
  </div>`;
}

/* --------------------------------------------------------------- 设置 */
function renderSettings() {
  $("#set-default-req").value = state.settings.default_required_reviews;
  $("#set-default-cap").value = state.settings.default_capacity;
  $("#set-beta").value = state.settings.mismatch_weight;
  $("#set-alpha").value = state.settings.load_weight;
  $("#set-inst-cap").value = state.settings.institution_cap ?? 0;
  // 沙盒临时参数：用户未手动改过（或采用后重置）时跟随当前设置
  const sbDefaults = {
    "#sb-beta": state.settings.mismatch_weight,
    "#sb-alpha": state.settings.load_weight,
    "#sb-inst-cap": state.settings.institution_cap ?? 0,
  };
  for (const [sel, val] of Object.entries(sbDefaults)) {
    const el = $(sel);
    if (el && !el.dataset.touched) el.value = val;
  }
}

/* --------------------------------------------------------------- 方案沙盒 */
let sandboxPreview = null;

function deltaCell(value, goodWhenNegative = true) {
  if (value === null || value === undefined) return '<span class="delta-same">—</span>';
  if (value === 0) return '<span class="delta-same">±0</span>';
  const better = goodWhenNegative ? value < 0 : value > 0;
  const sign = value > 0 ? `+${value}` : `${value}`;
  return `<span class="${better ? "delta-down" : "delta-up"}">${sign}（${better ? "更优" : "更差"}）</span>`;
}

function renderSandbox() {
  const box = $("#sandbox-result");
  const adoptBtn = $("#btn-sandbox-adopt");
  const clearBtn = $("#btn-sandbox-clear");
  if (!sandboxPreview) {
    box.innerHTML = "";
    adoptBtn.disabled = true;
    clearBtn.disabled = true;
    return;
  }
  const p = sandboxPreview;
  adoptBtn.disabled = !!p._stale;
  clearBtn.disabled = false;

  const cur = p.current_metrics;
  const rows = [
    ["目标函数总费用", cur ? cur.total_cost : "—", p.metrics.total_cost,
      p.delta ? deltaCell(p.delta.total_cost) : '<span class="delta-same">—</span>'],
    ["已满足评审数", cur ? `${cur.total_assigned} / ${cur.total_required}` : "—",
      `${p.metrics.total_assigned} / ${p.metrics.total_required}`,
      p.delta ? deltaCell(p.delta.total_assigned, false) : '<span class="delta-same">—</span>'],
    ["未满足人数", cur ? cur.unmet_people : "—", p.metrics.unmet_people,
      p.delta ? deltaCell(p.delta.unmet_people) : '<span class="delta-same">—</span>'],
    ["主审缺口", cur ? cur.lead_deficit : "—", p.metrics.lead_deficit,
      p.delta ? deltaCell(p.delta.lead_deficit) : '<span class="delta-same">—</span>'],
  ].map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td><td>${r[3]}</td></tr>`).join("");

  const pairLine = (x, sign) =>
    `<li>${sign} 《${esc(x.manuscript_title)}》 ← ${esc(x.reviewer_name)}` +
    (x.is_lead ? ' <span class="tag lead-tag">⭐ 主审</span>' : "") + `</li>`;
  const diffList = (items, sign, emptyText) =>
    items.length ? `<ul>${items.map((x) => pairLine(x, sign)).join("")}</ul>`
                 : `<div class="hint">${emptyText}</div>`;

  const unmetHtml = p.unmet.length
    ? `<div class="sandbox-unmet"><strong>预览中仍有 ${p.unmet.length} 篇未满足：</strong><ul>` +
      p.unmet.map((u) =>
        `<li>《${esc(u.title)}》：缺评审 ${u.deficit} 名` +
        (u.lead_deficit > 0 ? `，主审缺口 ${u.lead_deficit} 名` : "") + `</li>`).join("") +
      `</ul></div>`
    : `<div class="sandbox-unmet ok">✅ 预览方案可满足全部评审需求（含主审）。</div>`;

  const staleHtml = p._stale
    ? `<div class="alert danger" style="margin-top:10px">⚠️ 采用失败：预览生成后数据已被修改，
       该预览已过期（服务端返回 409，未写入任何内容）。请「清除预览」后重新「生成预览」。</div>`
    : "";

  box.innerHTML = `<div class="sandbox-result-inner ${p._stale ? "stale" : ""}">
    ${staleHtml}
    <div class="sandbox-meta">
      临时参数：β=${p.params.mismatch_weight} · α=${p.params.load_weight} · 机构上限=${p.params.institution_cap}
      （当前设置：β=${p.current_params.mismatch_weight} · α=${p.current_params.load_weight} · 机构上限=${p.current_params.institution_cap}）
      ｜ 生成于 ${esc(p.created_at)} ｜ 版本指纹 <code>${esc(p.fingerprint.slice(0, 12))}…</code>
      ${cur ? "" : "｜ 当前尚无求解结果，仅展示预览指标"}
    </div>
    <table class="sandbox-metrics">
      <tr><th>指标</th><th>当前方案</th><th>沙盒预览</th><th>变化</th></tr>
      ${rows}
    </table>
    <div class="sandbox-diff">
      <div>
        <h4>➕ 新增评审配对（${p.diff.added.length}）</h4>
        ${diffList(p.diff.added, "➕", "无新增配对")}
      </div>
      <div>
        <h4>➖ 移除评审配对（${p.diff.removed.length}）</h4>
        ${diffList(p.diff.removed, "➖", "无移除配对")}
      </div>
    </div>
    ${unmetHtml}
    <div class="sandbox-meta">采用后：临时参数写入设置，预览分配替换当前全部非锁定分配（锁定保留），并更新求解结果。</div>
  </div>`;
}

function clearSandboxTouched() {
  for (const sel of ["#sb-beta", "#sb-alpha", "#sb-inst-cap"]) {
    const el = $(sel);
    if (el) delete el.dataset.touched;
  }
}

function render() {
  indexMaps();
  renderSummary();
  renderAlerts();
  renderAssignments();
  renderReviewerLoad();
  renderManuscripts();
  renderReviewers();
  renderConflicts();
  renderDeclarations();
  renderSettings();
  renderSandbox();
  renderBackfill();
  renderRelief();
}

/* --------------------------------------------------------------- 通用编辑弹窗 */
function openModal(title, fields, onOk) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML =
    `<div class="modal-body-row">${fields.map((f) => f.type === "checkbox" ? `
      <label class="checkbox-label">${esc(f.label)}
        <input type="checkbox" id="mf-${f.key}" ${f.value ? "checked" : ""} />
      </label>` : `
      <label>${esc(f.label)}
        <input type="${f.type || "text"}" id="mf-${f.key}" value="${esc(f.value ?? "")}"
          min="${f.min ?? ""}" max="${f.max ?? ""}" />
      </label>`).join("")}</div>`;
  $("#modal").classList.remove("hidden");

  const cleanup = () => {
    $("#modal").classList.add("hidden");
    $("#modal-ok").onclick = null;
    $("#modal-cancel").onclick = null;
  };
  $("#modal-cancel").onclick = cleanup;
  $("#modal-ok").onclick = async () => {
    const values = {};
    for (const f of fields) {
      const el = $(`#mf-${f.key}`);
      values[f.key] = f.type === "checkbox" ? el.checked
        : f.type === "number" ? Number(el.value) : el.value;
    }
    try {
      await onOk(values);
      cleanup();
    } catch (e) {
      toast(e.message, true);
    }
  };
}

/* ------------------------------------------------- 申报链接弹窗（一次性明文展示） */
function openLinkModal(reviewerName, url, manifestCount) {
  $("#link-modal-desc").textContent =
    `评审人：${reviewerName} · 绑定稿件清单 ${manifestCount} 篇（提交时清单若已变化将返回 409）。`;
  $("#link-modal-input").value = url;
  $("#link-modal").classList.remove("hidden");
}

function closeLinkModal() {
  $("#link-modal").classList.add("hidden");
}

/* --------------------------------------------------------------- 事件绑定 */
function bindEvents() {
  // 标签页
  $("#tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".tab-panel").forEach((p) =>
      p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
  });

  // 顶栏操作
  $("#btn-solve").addEventListener("click", async () => {
    const btn = $("#btn-solve");
    btn.disabled = true;
    try {
      await runAction(api("/api/solve", { method: "POST" }), "已用最小费用最大流完成分配");
    } catch (e) { /* toast handled */ } finally {
      btn.disabled = false;
    }
  });
  $("#btn-demo").addEventListener("click", async () => {
    if (!confirm("载入演示数据将覆盖现有稿件/评审人/冲突/分配，是否继续？")) return;
    await runAction(api("/api/demo", { method: "POST" }), "已载入演示数据并完成求解");
  });
  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("将清空所有稿件、评审人、冲突与分配，是否继续？")) return;
    await runAction(api("/api/reset", { method: "POST" }), "已清空");
  });

  // 新增稿件（required_reviews / required_leads 留空时用默认值）
  $("#form-manuscript").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const req = f.required_reviews.value.trim();
    const leads = f.required_leads.value.trim();
    await runAction(api("/api/manuscripts", {
      method: "POST",
      body: JSON.stringify({
        title: f.title.value,
        topics: f.topics.value,
        required_reviews: req ? Number(req) : state.settings.default_required_reviews,
        required_leads: leads ? Number(leads) : 0,
      }),
    }), "稿件已添加");
    f.reset();
  });

  // 新增评审人
  $("#form-reviewer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const cap = f.capacity.value.trim();
    await runAction(api("/api/reviewers", {
      method: "POST",
      body: JSON.stringify({
        name: f.name.value,
        topics: f.topics.value,
        institution: f.institution.value,
        capacity: cap ? Number(cap) : state.settings.default_capacity,
        can_lead: f.can_lead.checked,
      }),
    }), "评审人已添加");
    f.reset();
  });

  // 列表中的编辑/删除（事件委托）
  document.body.addEventListener("click", async (e) => {
    const editM = e.target.closest("[data-edit-m]");
    const delM = e.target.closest("[data-del-m]");
    const editR = e.target.closest("[data-edit-r]");
    const delR = e.target.closest("[data-del-r]");
    const lockBtn = e.target.closest("[data-lock]");
    const manualBtn = e.target.closest("[data-manual-lock]");
    const issueBtn = e.target.closest("[data-decl-issue]");
    const revokeBtn = e.target.closest("[data-decl-revoke]");

    if (editM) {
      const m = state._mById[editM.dataset.editM];
      openModal("编辑稿件", [
        { key: "title", label: "稿件主题/标题", value: m.title },
        { key: "topics", label: "主题（逗号分隔）", value: m.topics.join(", ") },
        { key: "required_reviews", label: "所需评审数", value: m.required_reviews, type: "number", min: 1, max: 20 },
        { key: "required_leads", label: "需主审人数（0-2，不超过所需评审数）", value: m.required_leads ?? 0, type: "number", min: 0, max: 2 },
      ], async (v) => {
        if (!v.title.trim()) throw new Error("标题不能为空");
        await runAction(api(`/api/manuscripts/${m.id}`, {
          method: "PATCH",
          body: JSON.stringify(v),
        }), "稿件已更新");
      });
    } else if (delM) {
      const m = state._mById[delM.dataset.delM];
      if (!confirm(`确定删除稿件《${m.title}》？相关分配将一并删除。`)) return;
      await runAction(api(`/api/manuscripts/${m.id}`, { method: "DELETE" }), "稿件已删除");
    } else if (editR) {
      const r = state._rById[editR.dataset.editR];
      openModal("编辑评审人", [
        { key: "name", label: "姓名/代号", value: r.name },
        { key: "topics", label: "擅长主题（逗号分隔）", value: r.topics.join(", ") },
        { key: "institution", label: "所属机构（可选，留空不计入机构上限）", value: r.institution || "" },
        { key: "capacity", label: "个人容量", value: r.capacity, type: "number", min: 0, max: 50 },
        { key: "can_lead", label: "可任主审（主审资格硬约束）", value: !!r.can_lead, type: "checkbox" },
      ], async (v) => {
        if (!v.name.trim()) throw new Error("姓名不能为空");
        await runAction(api(`/api/reviewers/${r.id}`, {
          method: "PATCH",
          body: JSON.stringify(v),
        }), "评审人已更新");
      });
    } else if (delR) {
      const r = state._rById[delR.dataset.delR];
      if (!confirm(`确定删除评审人 ${r.name}？相关分配将一并删除。`)) return;
      await runAction(api(`/api/reviewers/${r.id}`, { method: "DELETE" }), "评审人已删除");
    } else if (lockBtn) {
      const mId = Number(lockBtn.dataset.lock);
      const rId = Number(lockBtn.dataset.reviewer);
      const toLocked = lockBtn.dataset.locked === "1";
      if (toLocked && !confirm("锁定该既有分配？锁定后它将占用容量并参与负载均衡，随后自动重新求解。")) return;
      await runAction(api("/api/assignments/lock", {
        method: "POST",
        body: JSON.stringify({ manuscript_id: mId, reviewer_id: rId, locked: toLocked }),
      }), toLocked ? "已锁定并重新求解" : "已解锁并重新求解");
    } else if (manualBtn) {
      const mId = Number(manualBtn.dataset.manualLock);
      const sel = $(`#lock-select-${mId}`);
      if (!sel.value) return toast("请先选择一名评审人", true);
      await runAction(api("/api/assignments/lock", {
        method: "POST",
        body: JSON.stringify({
          manuscript_id: mId, reviewer_id: Number(sel.value), locked: true,
        }),
      }), "已锁定该既有分配并重新求解");
    } else if (issueBtn) {
      const rId = Number(issueBtn.dataset.declIssue);
      const r = state._rById[rId];
      if (!confirm(`为评审人 ${r.name} 生成一次性申报链接？其已有的待提交链接将立即失效。`)) return;
      const btn = issueBtn;
      btn.disabled = true;
      try {
        const data = await api(`/api/reviewers/${rId}/declarations`, { method: "POST" });
        state = data.state;
        render();
        openLinkModal(r.name, data.url, data.declaration.manifest_count);
      } catch (err) { toast(err.message, true); } finally {
        btn.disabled = false;
      }
    } else if (revokeBtn) {
      const declId = Number(revokeBtn.dataset.declRevoke);
      if (!confirm("确定撤销该待提交申报链接？撤销后评审人将无法打开/提交，需要重新生成。")) return;
      await runAction(api(`/api/declarations/${declId}/revoke`, { method: "POST" }),
        "申报链接已撤销");
    }
  });

  // 冲突矩阵
  $("#conflict-matrix").addEventListener("change", async (e) => {    const cb = e.target.closest("input[data-conflict]");
    if (!cb) return;
    const mId = Number(cb.dataset.conflict);
    const rId = Number(cb.dataset.reviewer);
    try {
      if (cb.checked) {
        await runAction(api("/api/conflicts", {
          method: "POST",
          body: JSON.stringify({ manuscript_id: mId, reviewer_id: rId }),
        }), "已登记冲突");
      } else {
        await runAction(api(`/api/conflicts/${mId}/${rId}`, { method: "DELETE" }), "已移除冲突");
      }
    } catch (err) {
      cb.checked = !cb.checked; // 回滚 UI
    }
  });

  // 设置保存
  $("#btn-save-settings").addEventListener("click", async () => {
    await runAction(api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        default_required_reviews: Number($("#set-default-req").value),
        default_capacity: Number($("#set-default-cap").value),
        mismatch_weight: Number($("#set-beta").value),
        load_weight: Number($("#set-alpha").value),
        institution_cap: Number($("#set-inst-cap").value),
      }),
    }), "设置已保存（点击“重新求解分配”可按新权重/机构上限计算）");
  });

  // 方案沙盒：生成预览（不改动任何数据）
  $("#btn-sandbox-preview").addEventListener("click", async () => {
    const btn = $("#btn-sandbox-preview");
    btn.disabled = true;
    try {
      sandboxPreview = await api("/api/sandbox/preview", {
        method: "POST",
        body: JSON.stringify({
          mismatch_weight: Number($("#sb-beta").value),
          load_weight: Number($("#sb-alpha").value),
          institution_cap: Number($("#sb-inst-cap").value),
        }),
      });
      renderSandbox();
      toast("已生成沙盒预览（未改动设置与分配）");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 方案沙盒：采用（指纹一致时单事务写入；409 则提示重新预览）
  $("#btn-sandbox-adopt").addEventListener("click", async () => {
    if (!sandboxPreview) return;
    if (!confirm("将采用临时参数，并以预览分配替换当前全部非锁定分配（锁定保留），是否继续？")) return;
    const btn = $("#btn-sandbox-adopt");
    btn.disabled = true;
    try {
      const data = await api("/api/sandbox/adopt", {
        method: "POST",
        body: JSON.stringify({
          ...sandboxPreview.params,
          fingerprint: sandboxPreview.fingerprint,
        }),
      });
      state = data;
      sandboxPreview = null;
      clearSandboxTouched();
      render();
      toast("已采用沙盒方案：临时参数与预览分配已写入");
    } catch (e) {
      // 409：预览已过期，标记并禁用采用，引导重新预览
      if (sandboxPreview) {
        sandboxPreview._stale = true;
        renderSandbox();
      }
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 方案沙盒：清除预览
  $("#btn-sandbox-clear").addEventListener("click", () => {
    sandboxPreview = null;
    renderSandbox();
  });

  // 沙盒输入框手动编辑后不再跟随设置自动填充
  $$("#sb-beta, #sb-alpha, #sb-inst-cap").forEach((i) =>
    i.addEventListener("input", () => (i.dataset.touched = "1")));

  // 退出补位：生成只读修复预览
  $("#btn-backfill-preview").addEventListener("click", async () => {
    const reviewerId = Number($("#bf-reviewer").value);
    if (!reviewerId) return toast("请先选择一名临时退出的评审人", true);
    const reviewer = state._rById[reviewerId];
    const lockN = reviewer?.locked_assigned || 0;
    const msg = lockN
      ? `将排除评审人 ${reviewer.name} 并生成只读修复预览。注意：其 ${lockN} 个锁定配对会随退出一并移除（无法跨人保留），其余锁定保留。是否继续？`
      : `将排除评审人 ${reviewer.name} 并生成只读修复预览（不写入任何数据）。是否继续？`;
    if (!confirm(msg)) return;
    const btn = $("#btn-backfill-preview");
    btn.disabled = true;
    try {
      backfillPreview = await api("/api/backfill/preview", {
        method: "POST",
        body: JSON.stringify({ reviewer_id: reviewerId }),
      });
      renderBackfill();
      toast("已生成退出补位修复预览（未改动任何数据）");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 退出补位：采用（指纹一致时单事务写入；409 则提示重新预览）
  $("#btn-backfill-adopt").addEventListener("click", async () => {
    if (!backfillPreview) return;
    const p = backfillPreview;
    if (!confirm(`确认采用修复方案？\n\n· ${p.reviewer_name} 的全部配对（含锁定 ${p.exiting.locked_pair_count} 个）将移除；\n· 其余评审人的锁定保留；\n· 非锁定分配将被修复结果替换；\n· 求解参数不变。\n\n该操作在单个事务内完成，指纹过期将返回 409 且不写入。`)) return;
    const btn = $("#btn-backfill-adopt");
    btn.disabled = true;
    try {
      const data = await api("/api/backfill/adopt", {
        method: "POST",
        body: JSON.stringify({
          reviewer_id: p.reviewer_id,
          fingerprint: p.fingerprint,
        }),
      });
      state = data;
      backfillPreview = null;
      render();
      toast("已采用退出补位修复方案");
    } catch (e) {
      if (backfillPreview) {
        backfillPreview._stale = true;
        renderBackfill();
      }
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 退出补位：清除预览
  $("#btn-backfill-clear").addEventListener("click", () => {
    backfillPreview = null;
    renderBackfill();
  });

  // 缺口解困：生成只读单步调整建议（不写入任何数据）
  $("#btn-relief-generate").addEventListener("click", async () => {
    const btn = $("#btn-relief-generate");
    btn.disabled = true;
    try {
      reliefData = await api("/api/relief/suggestions", { method: "POST" });
      renderRelief();
      toast(reliefData.suggestions.length
        ? `已生成 ${reliefData.suggestions.length} 条缺口解困建议（只读试算）`
        : "没有能改善缺口的单步调整");
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 缺口解困：清除结果（仅前端丢弃，本就未写入任何数据）
  $("#btn-relief-clear").addEventListener("click", () => {
    reliefData = null;
    renderRelief();
  });

  // 申报链接弹窗：关闭 / 复制（明文链接仅此一次展示）
  $("#link-modal-close").addEventListener("click", closeLinkModal);
  $("#link-modal").addEventListener("click", (e) => {
    if (e.target === $("#link-modal")) closeLinkModal();
  });
  $("#link-modal-copy").addEventListener("click", async () => {
    const input = $("#link-modal-input");
    try {
      await navigator.clipboard.writeText(input.value);
      toast("链接已复制，请发送给评审人");
    } catch (e) {
      input.select();
      document.execCommand("copy");
      toast("链接已复制，请发送给评审人");
    }
  });

  // 记录用户是否手动改过默认输入框
  $$('form input[name="required_reviews"], form input[name="capacity"]').forEach((i) =>
    i.addEventListener("input", () => (i.dataset.touched = "1")));
}

/* --------------------------------------------------------------- 启动 */
(async function init() {
  bindEvents();
  try {
    state = await api("/api/state");
    indexMaps();
    render();
    // 首次进入（如全新数据库）自动求解一次
    if (!state.solution && state.manuscripts.length) {
      state = await api("/api/solve", { method: "POST" });
      render();
    }
  } catch (e) {
    toast("初始化失败：" + e.message, true);
  }
})();
