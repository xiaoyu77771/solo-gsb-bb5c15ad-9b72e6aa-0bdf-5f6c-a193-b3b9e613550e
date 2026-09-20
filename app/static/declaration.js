/* 利益冲突自助申报页（评审人公开页）—— 原生 JS，无框架、无构建 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const token = location.pathname.split("/").filter(Boolean).pop() || "";

let toastTimer = null;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
}

function banner(cls, html) {
  $("#decl-message").innerHTML = `<div class="status-banner ${cls}">${html}</div>`;
}

async function loadDeclaration() {
  let res, data;
  try {
    res = await fetch(`/api/public/declarations/${encodeURIComponent(token)}`);
    data = await res.json().catch(() => ({}));
  } catch (e) {
    banner("bad", "页面加载失败，请检查网络后重试，或联系管理员。");
    return;
  }

  if (!res.ok) {
    banner("bad", esc(data.detail || `请求失败（${res.status}）`));
    return;
  }

  const reviewer = esc(data.reviewer_name || "评审人");
  $("#decl-reviewer").textContent = `${reviewer}，您好`;

  if (data.status !== "pending") {
    if (data.status === "submitted") {
      banner("ok", `✅ 您已于 ${esc(data.submitted_at || "")} 完成利益冲突申报，<strong>该链接已失效，不能重复提交</strong>。`);
    } else {
      banner("warn", `⚠️ 该申报链接已于 ${esc(data.revoked_at || "")} 失效（可能被管理员撤销或由新链接顶替），请联系管理员重新获取。`);
    }
    $("#decl-panel").style.display = "none";
    return;
  }

  $("#decl-panel").style.display = "";
  if (data.stale) {
    $("#decl-stale").innerHTML =
      `<div class="status-banner warn" style="margin-bottom:12px">⚠️ 签发本链接后稿件清单已发生变化
      （有稿件新增、删除或标题修改）。为避免漏报，<strong>提交将被拒绝</strong>，请联系管理员重新生成申报链接。</div>`;
    $("#btn-submit").disabled = true;
  }

  if (!data.manuscripts || !data.manuscripts.length) {
    $("#decl-list").innerHTML = `<div class="empty">签发时暂无稿件。您仍可直接提交（表示无冲突）。</div>`;
  } else {
    $("#decl-list").innerHTML = data.manuscripts.map((m) => `
      <label class="decl-item">
        <input type="checkbox" name="conflict" value="${m.id}" />
        <span class="m-title">《${esc(m.title)}》</span>
      </label>`).join("");
  }
}

$("#btn-submit").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll('input[name="conflict"]:checked')]
    .map((cb) => Number(cb.value));
  if (ids.length === 0) {
    if (!confirm("您未勾选任何稿件，确认您与全部稿件均无利益冲突并提交？提交后链接立即失效。")) return;
  } else {
    if (!confirm(`确认勾选的 ${ids.length} 篇稿件均存在利益冲突？提交后链接立即失效，不能修改。`)) return;
  }
  const btn = $("#btn-submit");
  btn.disabled = true;
  try {
    const res = await fetch(`/api/public/declarations/${encodeURIComponent(token)}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conflict_manuscript_ids: ids }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      banner("bad", `❌ ${esc(data.detail || `提交失败（${res.status}），本次未写入。`)}`);
      // 409（清单变化/重复提交）后按钮保持禁用，避免再次无效提交
      if (res.status === 409) btn.disabled = true;
      else btn.disabled = false;
      return;
    }
    const dup = data.duplicate_count
      ? `（其中 ${data.duplicate_count} 篇此前已登记冲突）` : "";
    banner("ok",
      `✅ 申报已提交：本次合并冲突 <strong>${data.merged_count}</strong> 条${esc(dup)}，
       提交时间 ${esc(data.submitted_at || "")}。<br/><strong>该链接已立即失效，不能重复提交。</strong>`);
    $("#decl-panel").style.display = "none";
    toast("申报提交成功");
  } catch (e) {
    banner("bad", "提交失败，请检查网络后重试；如仍失败请联系管理员。");
    btn.disabled = false;
  }
});

loadDeclaration();
