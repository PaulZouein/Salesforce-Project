// The page's code. Served from /static/app.js; the page's security policy allows no inline script.
"use strict";
// The page token is in a <meta> tag: no script is inline, so the page can forbid inline scripts.
const TOKEN = document.querySelector('meta[name="sfti-token"]').content;
const $ = (sel, root) => (root || document).querySelector(sel);
const main = $("#main");
let STATUS = null;
let pollTimer = null;
const state = { orgs: [], orgInfo: {}, ticketTab: "cause", loginType: "sandbox", loginAlias: "my-sandbox" };

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
async function api(path, options) {
  const init = Object.assign({ headers: {} }, options || {});
  if (init.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-SFTI-Token"] = TOKEN;
    init.body = JSON.stringify(init.body);
  } else if (init.method && init.method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.headers["X-SFTI-Token"] = TOKEN;
    init.body = "{}";
  }
  let response;
  // Relative to the page ("api/…", not "/api/…"), so the page also works behind a proxy that serves it under a path.
  try { response = await fetch(path.replace(/^\//, ""), init); }
  catch (error) { throw new Error("The interface is not answering. Is 'python -m webapp' still running?"); }
  const data = await response.json().catch(() => ({ ok: false, error: { message: "Unexpected answer from the server." } }));
  if (!data.ok) {
    const error = new Error((data.error && data.error.message) || "Something went wrong.");
    error.hint = data.error && data.error.hint;
    error.code = data.error && data.error.code;
    error.details = data.error && data.error.details;
    throw error;
  }
  return data;
}
function toast(text) {
  const el = $("#toast"); el.textContent = text; el.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove("show"), 1800);
}
async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); toast((what || "Copied") + " to the clipboard"); }
  catch (error) {
    const area = document.createElement("textarea"); area.value = text; document.body.appendChild(area);
    area.select(); document.execCommand("copy"); area.remove(); toast((what || "Copied") + " to the clipboard");
  }
}
function cmd(text) {
  return `<div class="cmd"><span>${esc(text)}</span><button class="btn sm" data-copy="${esc(text)}">Copy</button></div>`;
}
function errorBox(error) {
  return `<div class="banner bad"><b>${esc(error.message)}</b>${error.hint ? "<br>" + esc(error.hint) : ""}</div>`;
}
function when(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  return isNaN(date) ? esc(iso) : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
function classChip(classification) {
  const map = { production: ["bad", "Production · read-only"], sandbox: ["ok", "Sandbox"], scratch: ["ok", "Scratch org"],
                developer: ["info", "Developer Edition"] };
  const [tone, label] = map[classification] || ["", classification || "checking…"];
  return `<span class="chip ${tone}"><span class="dot"></span>${esc(label)}</span>`;
}
const STAGES = { new: ["", "Not started"], running: ["info", "Investigating…"], in_progress: ["warn", "In progress"],
  report: ["info", "Report written"], done: ["ok", "Done"], failed: ["bad", "Stopped with an error"],
  cancelled: ["warn", "Stopped"], interrupted: ["warn", "Interrupted"], incomplete: ["warn", "Stopped before the report"] };
function modelSelect(id, chosen) {
  const models = (STATUS && STATUS.models) || { "": "Claude Code's default" };
  const fallback = STATUS && STATUS.default_model;
  return `<select id="${id}" class="inline" title="Which Claude model investigates">${Object.entries(models).map(([value, label]) =>
    `<option value="${esc(value)}" ${value === (chosen || "") ? "selected" : ""}>${esc(value === "" && fallback ? "Default (" + fallback + ")" : label)}</option>`).join("")}</select>`;
}
function money(value) {
  if (value === null || value === undefined || isNaN(value)) return "–";
  return "$" + (value >= 100 ? value.toFixed(0) : value.toFixed(2));
}
function kTokens(count) {
  count = count || 0;
  return count >= 1e6 ? (count / 1e6).toFixed(1) + "M" : count >= 1e3 ? Math.round(count / 1e3) + "k" : String(count);
}
function runPrefs() {
  try { return JSON.parse(localStorage.getItem("sfti-run-prefs") || "{}") || {}; } catch (error) { return {}; }
}
function keepRunPrefs(prefs) {
  try { localStorage.setItem("sfti-run-prefs", JSON.stringify(prefs)); } catch (error) { /* a convenience only */ }
}
function savingControls(economy, budget) {
  return `<div class="saving">
    <label class="check"><input type="checkbox" id="economy" ${economy ? "checked" : ""}> <b>Use fewer tokens</b></label>
    <span class="small muted" style="margin-left:24px">Sonnet unless you chose a model, less deliberation, no helper agents, a shorter write-up. The evidence rules stay the same.</span>
    <label class="inline small" for="budget">Stop if it costs more than $<input type="number" id="budget" min="0.05" max="100" step="0.05" placeholder="no limit" value="${budget ? esc(budget) : ""}"></label>
  </div>`;
}
function savingChoice() {
  const economy = $("#economy"), budget = $("#budget");
  return { economy: !!(economy && economy.checked), budget_usd: budget && budget.value.trim() ? budget.value.trim() : null };
}

/* ---------- Usage box ---------- */
const meter = { data: null, timer: null, open: false };
function planWindows(plan) {
  if (!plan || !plan.windows) return "";
  const names = { five_hour: "5-hour", seven_day: "Week", seven_day_opus: "Week, Opus", seven_day_sonnet: "Week, Sonnet" };
  const now = Date.now() / 1000;
  const parts = Object.entries(plan.windows).map(([name, w]) => {
    const label = names[name] || name.replace(/_/g, " ");
    if (w.resetsAt && w.resetsAt < now) return `<div class="m-sub">${esc(label)}: reset since</div>`;
    const pct = Math.max(0, Math.min(100, Math.round((w.utilization || 0) * 100)));
    const tone = pct >= 95 ? "bad" : pct >= 80 ? "warn" : "";
    const resets = w.resetsAt ? new Date(w.resetsAt * 1000) : null;
    const at = resets ? (resets - Date.now() < 864e5 ? resets.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
                                                 : resets.toLocaleDateString([], { weekday: "short", day: "numeric" })) : "";
    return `<div title="${esc(label)} limit: ${pct}% used${at ? ", resets " + esc(at) : ""}">
      <div class="m-win-head"><span>${esc(label)}</span><span class="${tone}">${pct}%</span></div>
      <div class="m-bar"><i class="${tone}" style="width:${pct}%"></i></div>
      ${name === "five_hour" && at ? `<div class="m-sub">resets ${esc(at)}</div>` : ""}</div>`;
  });
  return `<div class="m-row"><span class="m-k">Plan</span><div class="m-plan">${parts.join("")}</div></div>`;
}
function paintMeter() {
  const box = $("#meter"), d = meter.data;
  if (!box || !d) return;
  const run = d.running[0];
  const live = run ? run.usage : null;
  const pill = run ? `<span class="spinner"></span>≈ ${money(live.cost_usd)}` : (d.today.runs ? `${money(d.today.cost_usd)} today` : "Usage");
  const now = run ? `<div class="m-row"><span class="m-k">Now</span><span><a href="#/ticket/${encodeURIComponent(run.key)}">${esc(run.key)}</a>${run.options && run.options.economy ? ` <span class="chip ok" style="padding:0 7px">saving</span>` : ""}
      <div class="m-big">${live.basis === "reported" ? "" : "≈ "}${money(live.cost_usd)}</div>
      <div class="m-sub">${kTokens(live.tokens.total)} tokens${run.options && run.options.budget_usd ? ` · limit ${money(run.options.budget_usd)}` : ""}</div></span></div>` : "";
  const last = !run && d.last ? `<div class="m-row"><span class="m-k">Last</span><span><a href="#/ticket/${encodeURIComponent(d.last.key)}">${esc(d.last.key)}</a> <span class="m-big">${d.last.basis === "reported" ? "" : "≈ "}${money(d.last.cost_usd)}</span>
      <div class="m-sub">${kTokens((d.last.tokens || {}).total)} tokens${d.last.economy ? " · saving" : ""}${d.last.status && d.last.status !== "finished" ? " · " + esc(d.last.status) : ""}</div></span></div>` : "";
  const today = `<div class="m-row"><span class="m-k">Today</span><span>${d.today.runs ? `${money(d.today.cost_usd)} · ${d.today.runs} run${d.today.runs === 1 ? "" : "s"}` : `<span class="m-sub">nothing yet</span>`}</span></div>`;
  box.classList.toggle("compact", !meter.open);
  box.innerHTML = `<button class="m-pill" id="meter-open" title="Show what investigations use">${pill}</button>
    <div class="m-head"><b>Claude usage</b><button class="m-toggle" id="meter-close" title="Hide">×</button></div>
    ${now}${last}${today}${planWindows(d.plan)}
    <details><summary>About these figures</summary><div class="m-sub" style="margin-top:4px">${esc(d.note)}${d.plan && d.plan.seen_at ? ` Plan figures as of ${esc(when(d.plan.seen_at))}.` : ""}</div></details>`;
  $("#meter-open").onclick = () => { meter.open = true; paintMeter(); };
  $("#meter-close").onclick = () => { meter.open = false; paintMeter(); };
}
async function refreshMeter() {
  clearTimeout(meter.timer);
  try { meter.data = await api("/api/usage"); paintMeter(); } catch (error) { /* the page shows its own errors */ }
  meter.timer = setTimeout(refreshMeter, meter.data && meter.data.running.length ? 1500 : 12000);
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}
function stageChip(stage) {
  const [tone, label] = STAGES[stage] || ["", stage];
  return `<span class="chip ${tone}">${stage === "running" ? '<span class="spinner"></span>' : '<span class="dot"></span>'}${esc(label)}</span>`;
}
function causeChip(status) {
  if (!status) return "";
  const confirmed = /^confirmed/i.test(status.trim());
  return `<span class="chip ${confirmed ? "ok" : "warn"}"><span class="dot"></span>Root cause: ${esc(status)}</span>`;
}

/* ---------- shell ---------- */
async function refreshStatus() {
  STATUS = await api("/api/status");
  $("#nav-count").textContent = STATUS.tickets;
  $("#nav-count").setAttribute("aria-label", `${STATUS.tickets} ticket${STATUS.tickets === 1 ? "" : "s"}`);
  $("#side-foot").innerHTML = [
    `<span class="chip ${STATUS.mode === "apply" ? "warn" : "ok"}" title="advise: finds the cause and hands you the fix. apply: may also deploy it to a sandbox, asking you each time. Set by you in sfti.config.json.">Mode: ${esc(STATUS.mode)}</span>`,
    STATUS.demo ? `<span class="chip info">Demo orgs (simulated)</span>` : `<span class="chip">Your Salesforce CLI</span>`,
    STATUS.claude.available ? `<span class="chip ok">Claude Code found</span>` : `<span class="chip warn">Claude Code not found</span>`,
  ].join("");
  return STATUS;
}
function route() {
  clearInterval(pollTimer); pollTimer = null;
  const hash = location.hash || "#/start";
  const parts = hash.replace(/^#\//, "").split("/");
  const view = (parts[0] || "start").split("?")[0];
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("on", a.dataset.view === (view === "ticket" ? "tickets" : view)));
  const views = { start: viewStart, new: viewNew, tickets: viewTickets, ticket: () => viewTicket(decodeURIComponent(parts[1] || "")),
                  memory: () => viewMemory(parts[1]), safety: viewSafety };
  (views[view] || viewStart)().catch(error => { main.innerHTML = errorBox(error); });
  window.scrollTo(0, 0);
}
document.addEventListener("click", event => {
  const copy = event.target.closest("[data-copy]");
  if (copy) { copyText(copy.dataset.copy, "Copied"); }
});
window.addEventListener("hashchange", route);
$("#skip").onclick = event => { event.preventDefault(); main.setAttribute("tabindex", "-1"); main.focus(); };

/* ---------- Get started ---------- */
function loginAlias() { return (state.loginAlias || "my-org").replace(/[^A-Za-z0-9._@+-]/g, "-"); }
function loginCommand() {
  const domain = (state.loginDomain || "").trim().replace(/[^A-Za-z0-9.:/-]/g, "");
  const urls = { sandbox: " --instance-url https://test.salesforce.com", production: "", developer: "",
                 custom: ` --instance-url ${domain || "https://YOUR-DOMAIN.my.salesforce.com"}` };
  return `sf org login web --alias ${loginAlias()}${urls[state.loginType]}`;
}
async function viewStart(again) {
  const fresh = again === true ? "?refresh=1" : "";
  main.innerHTML = `
    <h1>Get started</h1>
    <p class="lead">Four steps, once. After that you paste a ticket and read the answer: the root cause with its evidence, the smallest fix, and a comment ready for the ticket.</p>
    <div id="start-steps"><p class="muted"><span class="spinner"></span> Checking this computer…</p></div>`;
  await refreshStatus();
  let pre, orgs;
  try { pre = await api("/api/prereqs" + fresh); } catch (error) { pre = { checks: [], ok: false, error }; }
  try { orgs = await api("/api/orgs" + fresh); } catch (error) { orgs = { orgs: [], error }; }
  state.orgs = orgs.orgs || [];
  if (fresh) state.orgInfo = {};
  const required = pre.checks.filter(c => c.required);
  const toolsOk = required.length > 0 && required.filter(c => c.name !== "Authenticated org").every(c => c.ok);
  const hasOrg = state.orgs.length > 0;

  const checks = pre.checks.filter(c => c.name !== "Authenticated org").map(c => `
    <li><span class="${c.ok ? "mark-ok" : (c.required ? "mark-bad" : "mark-warn")}">${c.ok ? "✓" : (c.required ? "✕" : "!")}</span>
      <div><b>${esc(c.name)}</b> <span class="muted">${esc(c.detail)}</span>
        ${c.hint && !c.ok ? `<div class="small muted">${esc(c.hint)}</div>` : ""}
        ${c.fix ? `<div class="small" style="margin-top:6px">How to fix it on ${esc({windows:"Windows",macos:"macOS",linux:"Linux"}[pre.os])}:</div>${c.fix.map(line => /^(or|Then|sudo|brew|npm|python|winget|xcode)/.test(line) && !/^(or |Then )/.test(line) ? cmd(line) : `<div class="small muted">${esc(line)}</div>`).join("")}` : ""}
      </div></li>`).join("");

  const orgRows = state.orgs.map(o => `
    <tr><td><b>${esc(o.alias || o.username)}</b><div class="small muted">${esc(o.instance_url || "")}</div></td>
        <td id="org-class-${esc(o.alias || o.username)}">${classChip(null)}</td>
        <td class="small muted" id="org-note-${esc(o.alias || o.username)}"></td></tr>`).join("");

  if (!$("#start-steps")) return;  // the user went to another page meanwhile
  $("#start-steps").innerHTML = `
    ${pre.error ? errorBox(pre.error) : ""}
    <div class="card step ${toolsOk ? "done" : "todo"}">
      <div class="num">${toolsOk ? "✓" : "1"}</div>
      <div><h2>This computer has what it needs</h2>
        <ul class="checks">${checks || '<li><span class="mark-wait">…</span><div class="muted">No checks could be run.</div></li>'}</ul>
        <div class="row" style="margin-top:10px"><button class="btn sm" id="recheck">Check again</button></div>
      </div>
    </div>

    <div class="card step ${hasOrg ? "done" : "todo"}">
      <div class="num">${hasOrg ? "✓" : "2"}</div>
      <div><h2>Connect a Salesforce org</h2>
        ${STATUS.demo ? `<div class="banner info">You are using the <b>demo orgs</b>: simulated, with a planted bug. No real Salesforce org is involved, and Apex is not executed in them.</div>` : ""}
        ${orgs.error ? errorBox(orgs.error) : ""}
        ${hasOrg ? `<div class="tablewrap"><table><thead><tr><th>Org</th><th>Kind</th><th>What the investigator may do</th></tr></thead><tbody>${orgRows}</tbody></table></div>`
                 : `<p class="muted">No org is connected yet.</p>`}
        ${STATUS.demo ? "" : `
        <details ${hasOrg ? "" : "open"}><summary>${hasOrg ? "Connect another org" : "How to connect your org"}</summary>
          <p class="small muted" style="margin:10px 0 8px">Connecting goes through the Salesforce CLI on this computer: your browser opens on the Salesforce login page, you log in there, and you can close it again. The investigator never sees your password.</p>
          <div class="row" style="margin-bottom:10px">
            <div style="min-width:180px;flex:1"><label for="alias">A short name for this org</label><input type="text" id="alias" value="${esc(state.loginAlias)}" autocomplete="off"></div>
            <div><label>What kind of org is it?</label>
              <div class="seg" id="login-type">
                <button data-type="sandbox">Sandbox</button><button data-type="production">Production</button>
                <button data-type="developer">Developer Edition</button><button data-type="custom">Custom domain</button>
              </div></div>
          </div>
          <div id="domain-row" style="margin-bottom:10px" hidden><label for="domain">Your My Domain address</label><input type="text" id="domain" placeholder="https://yourcompany.my.salesforce.com" value="${esc(state.loginDomain || "")}" autocomplete="off"></div>
          <button class="btn primary" id="login-web">Log in with your browser</button>
          <div id="login-result"></div>
          <p class="small muted" style="margin-top:8px">A browser window opens on the Salesforce login page. Log in there and allow access; this page shows the org when you are done. Use a login with only the access the investigation needs. Production is always read-only here, whatever the user is allowed to do.</p>
          <details style="margin-top:6px"><summary>Or connect from a terminal</summary>
            <div class="small" style="margin-top:8px">1. Open a terminal and run this:</div>
            <div id="login-cmd"></div>
            <div class="small">2. Log in in the browser window that opens, and allow access.</div>
            <div class="small" style="margin-top:4px">3. Come back here and press <b>I have connected it</b>.</div>
            <button class="btn sm" id="orgs-refresh" style="margin-top:8px">I have connected it</button>
          </details>
          <details id="paste-auth" style="margin-top:14px"><summary>No browser on the computer that runs the investigator? Paste an authorization instead</summary>
            <p class="small muted" style="margin:10px 0 8px">For when this page runs somewhere you cannot log in from, such as a remote computer or a cloud session. On a computer that has the Salesforce CLI and a browser:</p>
            <div class="small">1. Connect the org there, with the command above:</div>
            <div id="login-cmd-2"></div>
            <div class="small">2. Show its authorization:</div>
            <div id="auth-url-cmd"></div>
            <div class="small">3. Copy the line that starts with <code>force://</code> and paste it below. It works like a password: paste it only here, never in a chat, an email or a ticket.</div>
            <div class="row" style="margin-top:8px;align-items:flex-end">
              <div style="flex:1;min-width:220px"><label for="auth-url">Authorization (force://…)</label><input type="password" id="auth-url" autocomplete="off" spellcheck="false" placeholder="force://…"></div>
              <button class="btn sm primary" id="auth-connect">Connect</button>
            </div>
            <div id="auth-result"></div>
            <p class="small muted" style="margin-top:8px">The org gets the short name above. The authorization goes to the Salesforce CLI on the computer that runs this page, which keeps it; the page does not store it, and nothing in the org changes. To end it later: <code>sf org logout --target-org</code> and the name, on that computer, and in Salesforce revoke the Salesforce CLI under your user's OAuth Connected Apps.</p>
          </details>
        </details>`}
        <div class="row" style="margin-top:12px">
          ${STATUS.demo ? `<button class="btn sm" id="demo-off">Use my own Salesforce orgs</button><button class="btn sm" id="demo-reset">Reset the demo orgs</button>`
                        : `<button class="btn sm" id="demo-on">No org at hand? Try with the demo orgs</button>`}
        </div>
      </div>
    </div>

    <div class="card step ${STATUS.claude.available ? "done" : "todo"}">
      <div class="num">${STATUS.claude.available ? "✓" : "3"}</div>
      <div><h2>The investigator itself: Claude Code</h2>
        ${STATUS.claude.available
          ? `<p>Found: <code>${esc(STATUS.claude.version || "claude")}</code>. When you submit a ticket, this page starts Claude Code in this project folder and shows its progress.</p>
             <p class="small">If you have never used Claude Code on this computer, sign in once: run the command below in a terminal, follow the sign-in, then close it.</p>${cmd("claude")}`
          : `<p>Claude Code was not found on this computer. You can still use this page: after you submit a ticket it gives you one line to paste into Claude Code, and the result appears here.</p>`}
        <p class="small muted">Mode is <b>${esc(STATUS.mode)}</b>: ${STATUS.mode === "apply"
          ? "from Claude Code the investigator may deploy to a sandbox, asking you each time. From this page it only advises."
          : "the investigator finds the cause and hands you the fix; you apply it."} The mode is set in <code>sfti.config.json</code>, by you.</p>
      </div>
    </div>

    <div class="card step ${toolsOk && hasOrg ? "done" : ""}">
      <div class="num">4</div>
      <div><h2>Investigate a ticket</h2>
        <p class="muted">Paste the whole ticket, comments included. The more of the history it has, the less it has to assume.</p>
        <a class="btn primary" href="#/new">New ticket</a>
        ${STATUS.demo ? `<a class="btn" href="#/new?sample">Try the sample ticket</a>` : ""}
      </div>
    </div>`;

  $("#recheck").onclick = () => viewStart(true);
  const refresh = $("#orgs-refresh"); if (refresh) refresh.onclick = () => viewStart(true);
  const on = $("#demo-on"); if (on) on.onclick = async () => { on.disabled = true; await api("/api/demo", { method: "POST", body: { enabled: true } }); viewStart(); };
  const off = $("#demo-off"); if (off) off.onclick = async () => { await api("/api/demo", { method: "POST", body: { enabled: false } }); viewStart(); };
  const reset = $("#demo-reset"); if (reset) reset.onclick = async () => { await api("/api/demo", { method: "POST", body: { enabled: true, reset: true } }); toast("Demo orgs reset"); viewStart(); };
  const alias = $("#alias");
  const paintLogin = () => {
    const box = $("#login-cmd"); if (!box) return;
    box.innerHTML = cmd(loginCommand());
    const again = $("#login-cmd-2"); if (again) again.innerHTML = cmd(loginCommand());
    const show = $("#auth-url-cmd"); if (show) show.innerHTML = cmd(`sf org auth show-sfdx-auth-url --target-org ${loginAlias()}`);
    document.querySelectorAll("#login-type button").forEach(b => b.classList.toggle("on", b.dataset.type === state.loginType));
    const domainRow = $("#domain-row"); if (domainRow) domainRow.hidden = state.loginType !== "custom";
  };
  const domainField = $("#domain");
  if (domainField) domainField.oninput = () => { state.loginDomain = domainField.value; paintLogin(); };
  const loginWeb = $("#login-web");
  if (loginWeb) loginWeb.onclick = async () => {
    const out = $("#login-result");
    const body = { alias: loginAlias(), kind: state.loginType, domain: (state.loginDomain || "").trim() };
    if (body.kind === "custom" && !body.domain) {
      out.innerHTML = `<p class="small" style="color:var(--bad)">Type your My Domain address first.</p>`; return;
    }
    loginWeb.disabled = true;
    out.innerHTML = `<p class="small muted"><span class="spinner"></span> A browser window opened on the Salesforce login page. Log in there and allow access. This page waits for you, up to five minutes.</p>`;
    try {
      let got;
      try { got = await api("/api/orgs/login", { method: "POST", body }); }
      catch (error) {
        // Asked before the browser opens: the name already points at a connected org.
        if (!(error.details && error.details.alias_in_use) ||
            !confirm(`"${body.alias}" is already the name of a connected org. Point it at the org you log in to instead?`)) throw error;
        got = await api("/api/orgs/login", { method: "POST", body: { ...body, replace: true } });
      }
      toast(`Connected: ${got.connected.alias}`);
      viewStart(true);
    } catch (error) {
      if (out.isConnected) out.innerHTML = errorBox(error);
    } finally {
      if (loginWeb.isConnected) loginWeb.disabled = false;
    }
  };
  const connect = $("#auth-connect");
  if (connect) connect.onclick = async () => {
    const field = $("#auth-url"), out = $("#auth-result");
    if (!field.value.trim()) { out.innerHTML = `<p class="small" style="color:var(--bad)">Paste the line that starts with force:// first.</p>`; return; }
    connect.disabled = true;
    out.innerHTML = `<p class="small muted">Connecting… This takes a few seconds.</p>`;
    try {
      const body = { alias: loginAlias(), auth_url: field.value };
      let got;
      try { got = await api("/api/orgs/connect", { method: "POST", body }); }
      catch (error) {
        // The name already points at a connected org: replacing it changes what every later command reaches.
        if (!(error.details && error.details.alias_in_use) ||
            !confirm(`"${body.alias}" is already the name of a connected org. Point it at this org instead?`)) throw error;
        got = await api("/api/orgs/connect", { method: "POST", body: { ...body, replace: true } });
      }
      field.value = "";
      toast(`Connected: ${got.connected.alias}`);
      viewStart(true);
    } catch (error) {
      if (out.isConnected) out.innerHTML = errorBox(error);
    } finally {
      if (connect.isConnected) connect.disabled = false;
    }
  };
  if (alias) {
    alias.oninput = () => { state.loginAlias = alias.value; paintLogin(); };
    document.querySelectorAll("#login-type button").forEach(b => b.onclick = () => {
      state.loginType = b.dataset.type;
      const result = $("#login-result"); if (result) result.innerHTML = "";
      const names = { sandbox: "my-sandbox", production: "my-prod", developer: "my-dev", custom: "my-org" };
      if (Object.values(names).includes(state.loginAlias)) { state.loginAlias = names[state.loginType]; alias.value = state.loginAlias; }
      paintLogin();
    });
    paintLogin();
  }
  state.orgs.forEach(async o => {
    const name = o.alias || o.username;
    try {
      const info = await api("/api/orgs/" + encodeURIComponent(name));
      state.orgInfo[name] = info;
      const cell = document.getElementById("org-class-" + name); if (cell) cell.innerHTML = classChip(info.classification);
      const note = document.getElementById("org-note-" + name); if (note) note.textContent = info.what_is_allowed || "";
    } catch (error) {
      const cell = document.getElementById("org-class-" + name); if (cell) cell.innerHTML = `<span class="chip bad">Could not be checked</span>`;
      const note = document.getElementById("org-note-" + name); if (note) note.textContent = error.message;
    }
  });
}

/* ---------- New ticket ---------- */
function detectKey(text) {
  const line = text.match(/^\s*(?:key|ticket|issue)\s*[:#]\s*([A-Za-z][A-Za-z0-9_]{0,19}-\d{1,9})\b/im);
  const any = text.match(/\b([A-Z][A-Z0-9]{1,19}-\d{1,9})\b/);
  return (line && line[1].toUpperCase()) || (any && any[1]) || "";
}
async function viewNew() {
  await refreshStatus();
  let orgs = [];
  let orgError = null;
  try { orgs = (await api("/api/orgs")).orgs; } catch (error) { orgError = error; }
  state.orgs = orgs;
  const options = (orgs.length > 1 ? `<option value="" selected disabled>Choose the org…</option>` : "") +
    orgs.map(o => `<option value="${esc(o.alias || o.username)}">${esc(o.alias || o.username)}</option>`).join("");
  main.innerHTML = `
    <h1>New ticket</h1>
    <p class="lead">Paste the ticket exactly as it is in your ticket system. Nothing is sent back there: you get a draft comment to post yourself.</p>
    ${orgError ? errorBox(orgError) : ""}
    ${!orgs.length && !orgError ? `<div class="banner warn">No org is connected yet. <a href="#/start">Connect one first</a>, or try the demo orgs.</div>` : ""}
    <div class="grid2">
      <div class="card">
        <div class="field">
          <div class="spread"><label for="text">The ticket</label>
            <span class="row">
              <label class="btn sm" for="pdf" style="margin:0" title="A PDF exported or printed from Jira, comments included">Upload a PDF</label><input type="file" id="pdf" accept="application/pdf,.pdf" hidden>
              ${STATUS.demo ? `<button class="btn sm" id="load-sample">Load the sample ticket</button>` : ""}
              ${STATUS.jira && STATUS.jira.enabled ? `<button class="btn sm" id="load-jira">Fetch from Jira</button>` : ""}
            </span></div>
          <textarea id="text" spellcheck="false" placeholder="Paste the ticket here, or upload it as a PDF (exported or printed from Jira, comments included).&#10;&#10;Key: ABC-123&#10;Title: …&#10;&#10;Description&#10;…&#10;&#10;Comments&#10;[date] who: …"></textarea>
          <div id="pdf-note"></div>
        </div>
        <div class="row" style="align-items:flex-start">
          <div class="field" style="flex:1;min-width:150px"><label for="key">Ticket key</label><input type="text" id="key" placeholder="found in the text" autocomplete="off"></div>
          <div class="field" style="flex:1;min-width:170px"><label for="org">Org where it happens</label><select id="org">${options}</select><div class="small" id="org-kind" style="margin-top:6px"></div></div>
          <div class="field" style="flex:1;min-width:170px"><label for="sandbox">Sandbox for tests <span class="muted">(optional)</span></label><select id="sandbox"><option value="">None</option>${options}</select></div>
        </div>
        <div class="row" style="margin-bottom:10px"><label for="model" style="margin:0">Model</label>${modelSelect("model", runPrefs().model || "")}
          <span class="small muted">Sonnet is quicker and uses less of your Claude usage; the default is the most thorough.</span></div>
        ${savingControls(runPrefs().economy, runPrefs().budget_usd)}
        <div id="new-error"></div>
        <div class="row"><button class="btn primary" id="submit" ${orgs.length ? "" : "disabled"}>Save and continue</button>
          <span class="small muted">Nothing runs until you choose how on the next page.</span></div>
      </div>
      <div class="card flat">
        <h2>What happens next</h2>
        <ol class="small" style="padding-left:18px;margin:0;display:grid;gap:8px">
          <li><b>Intake.</b> The ticket is read in full and rewritten in a structured form, with what is unclear listed.</li>
          <li><b>Memory.</b> It recalls what earlier tickets taught it about this org, and checks that it still holds.</li>
          <li><b>Investigation.</b> Hypotheses, then records, source, logs and recent changes from the org. Read-only.</li>
          <li><b>Root cause.</b> One statement, every claim tied to a file and line, a query result or a log line.</li>
          <li><b>Fix.</b> The smallest correct change, as a diff, with a test plan. You apply it.</li>
          <li><b>Comment.</b> A short draft for the ticket, with record Ids and no personal data.</li>
        </ol>
        <p class="small muted" style="margin:14px 0 0">A typical investigation takes a few minutes and uses your Claude Code usage.</p>
      </div>
    </div>`;
  const text = $("#text"), key = $("#key"), org = $("#org"), sandbox = $("#sandbox");
  let keyTouched = false;
  key.oninput = () => { keyTouched = true; };
  text.oninput = () => { if (!keyTouched) key.value = detectKey(text.value); };
  const paintKind = async () => {
    const name = org.value, kind = $("#org-kind"); if (!name || !kind) return;
    kind.innerHTML = classChip(null);
    // The answer can arrive after the user has left this page: write only into the element still shown.
    try { const info = state.orgInfo[name] || await api("/api/orgs/" + encodeURIComponent(name)); state.orgInfo[name] = info;
          if (org.value === name && kind.isConnected) kind.innerHTML = classChip(info.classification) +
            (info.org_memory && info.org_memory.components ? ` <span class="chip">${info.org_memory.components} components remembered</span>` : ""); }
    catch (error) { if (kind.isConnected) kind.innerHTML = `<span class="chip bad">Could not be checked</span>`; }
  };
  org.onchange = paintKind; paintKind();
  const loadSample = async () => {
    const sample = await api("/api/sample");
    text.value = sample.text; key.value = sample.key; keyTouched = true;
    if ([...org.options].some(o => o.value === sample.org)) org.value = sample.org;
    if ([...sandbox.options].some(o => o.value === sample.sandbox)) sandbox.value = sample.sandbox;
    paintKind();
  };
  const sampleBtn = $("#load-sample"); if (sampleBtn) sampleBtn.onclick = loadSample;
  if (location.hash.includes("?sample") && STATUS.demo) loadSample();
  let pdfData = null;
  $("#pdf").onchange = async () => {
    const chosen = $("#pdf").files[0]; if (!chosen) return;
    $("#new-error").innerHTML = ""; $("#pdf-note").innerHTML = `<p class="small muted"><span class="spinner"></span> Reading ${esc(chosen.name)}…</p>`;
    try {
      const content = await readFile(chosen);
      const got = await api("/api/ticket-pdf", { method: "POST", body: { name: chosen.name, content_base64: content } });
      pdfData = content;
      text.value = got.readable ? got.text : "";
      if (got.key && !keyTouched) key.value = got.key;
      $("#pdf-note").innerHTML = `<div class="banner ${got.readable ? "info" : "warn"}" style="margin:8px 0 0"><b>${esc(chosen.name)}</b>: ${got.readable
        ? `${got.pages} page${got.pages === 1 ? "" : "s"} read. Check the text above. The PDF itself is saved with the ticket, and the investigator opens it too for what the text misses (tables, screenshots).`
        : `no text could be read from it (it may be a scan). Type the ticket key; the investigator will read the PDF itself.`}
        <a href="#" id="pdf-remove">Remove</a></div>`;
      $("#pdf-remove").onclick = event => { event.preventDefault(); pdfData = null; $("#pdf-note").innerHTML = ""; };
    } catch (error) { pdfData = null; $("#pdf-note").innerHTML = errorBox(error); }
    $("#pdf").value = "";
  };
  const jiraBtn = $("#load-jira");
  if (jiraBtn) jiraBtn.onclick = async () => {
    const wanted = (key.value || prompt("Jira key, for example ABC-123") || "").trim(); if (!wanted) return;
    try { const got = await api("/api/jira/" + encodeURIComponent(wanted)); text.value = got.text; key.value = got.key; keyTouched = true; }
    catch (error) { $("#new-error").innerHTML = errorBox(error); }
  };
  $("#submit").onclick = async () => {
    $("#new-error").innerHTML = "";
    if (!org.value) { $("#new-error").innerHTML = errorBox(new Error("Choose the org where the problem happens.")); org.focus(); return; }
    try {
      const saving = savingChoice();
      const made = await api("/api/tickets", { method: "POST", body: { text: text.value, key: key.value, org: org.value, sandbox: sandbox.value,
        model: $("#model").value, economy: saving.economy, budget_usd: saving.budget_usd, pdf_base64: pdfData || undefined } });
      keepRunPrefs({ model: $("#model").value, economy: saving.economy, budget_usd: saving.budget_usd });
      location.hash = "#/ticket/" + encodeURIComponent(made.ticket.key);
    } catch (error) {
      $("#new-error").innerHTML = errorBox(error) + (/already exists/.test(error.message) && key.value
        ? `<p><a class="btn sm" href="#/ticket/${encodeURIComponent(key.value.toUpperCase())}">Open ${esc(key.value.toUpperCase())}</a></p>` : "");
    }
  };
}

/* ---------- Tickets ---------- */
async function viewTickets() {
  main.innerHTML = `<h1>Tickets</h1><p class="lead">Every ticket investigated on this computer. They live in the <code>tickets/</code> folder and are never uploaded anywhere.</p><div id="list"><p class="muted"><span class="spinner"></span> Loading…</p></div>`;
  await refreshStatus();
  const data = await api("/api/tickets");
  if (!data.tickets.length) {
    if (!$("#list")) return;
    $("#list").innerHTML = `<div class="card empty"><p>No ticket yet.</p><a class="btn primary" href="#/new">New ticket</a></div>`;
    return;
  }
  if (!$("#list")) return;
  $("#list").innerHTML = `<div class="card" style="padding:6px 10px"><div class="tablewrap"><table>
    <thead><tr><th>Ticket</th><th>Org</th><th>Status</th><th>Root cause</th><th>Evidence check</th><th>Created</th></tr></thead>
    <tbody>${data.tickets.map(t => `
      <tr class="click" data-key="${esc(t.key)}"><td><b>${esc(t.key)}</b><div class="small muted">${esc(t.title || "")}</div></td>
      <td>${esc(t.org || "")}</td><td>${stageChip(t.stage)}</td><td>${causeChip(t.root_cause_status)}</td>
      <td>${t.evidence_check ? (t.evidence_check.ok ? `<span class="chip ok">✓ ${t.evidence_check.citations.valid} citations</span>` : `<span class="chip bad">${t.evidence_check.problems.length} problem(s)</span>`) : ""}</td>
      <td class="small muted">${when(t.created_at)}</td></tr>`).join("")}</tbody></table></div></div>
    <a class="btn primary" href="#/new">New ticket</a>`;
  document.querySelectorAll("tr.click").forEach(row => row.onclick = () => { location.hash = "#/ticket/" + encodeURIComponent(row.dataset.key); });
}

/* ---------- One ticket ---------- */
function feedItem(e) {
  const marks = { tool: ["mark-ok", "›"], info: ["mark-wait", "•"], note: ["mark-wait", "“"], warn: ["mark-warn", "!"],
                  error: ["mark-bad", "✕"], done: ["mark-ok", "✓"] };
  const [tone, mark] = marks[e.kind] || ["mark-wait", "•"];
  const figures = [e.duration_seconds ? Math.max(1, Math.round(e.duration_seconds / 60)) + " min" : "", e.turns ? e.turns + " steps" : "",
                   typeof e.cost_usd === "number" ? money(e.cost_usd) : ""].filter(Boolean);
  const extra = figures.length && (e.kind === "done" || e.kind === "warn" || e.kind === "error") ? ` <span class="muted small">(${figures.join(", ")})</span>` : "";
  return `<li><span class="${tone}">${mark}</span><div><span class="${e.kind === "note" ? "note" : ""}">${esc(e.label)}</span>${extra}${e.detail ? `<div class="d">${esc(e.detail)}</div>` : ""}</div></li>`;
}
function costChip(t) {
  const u = t.run && t.run.usage;
  const total = t.usage_total || { runs: 0 };
  const parts = [];
  if (u && (u.tokens.total || u.cost_usd)) {
    parts.push(t.run.status === "running"
      ? `<span class="chip info" title="${esc(kTokens(u.tokens.total))} tokens so far">≈ ${money(u.cost_usd)} so far</span>`
      : `<span class="chip" title="${esc(kTokens(u.tokens.total))} tokens">Last run ${u.basis === "reported" ? "" : "≈ "}${money(u.cost_usd)}</span>`);
  }
  if (total.runs > 1) parts.push(`<span class="chip" title="Every run of this ticket from this page">${money(total.cost_usd)} in ${total.runs} runs</span>`);
  return parts.join(" ");
}
function renderDiff(text) {
  return `<pre class="diff">${text.split("\n").map(line => {
    const cls = line.startsWith("@@") ? "hunk" : (line.startsWith("+++") || line.startsWith("---")) ? "file"
      : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    return cls ? `<span class="${cls}">${esc(line) || " "}</span>` : esc(line) + "\n";
  }).join("")}</pre>`;
}
async function viewTicket(key) {
  if (!key) { location.hash = "#/tickets"; return; }
  main.innerHTML = `<p class="muted"><span class="spinner"></span> Loading ${esc(key)}…</p>`;
  await refreshStatus();
  let events = [];
  let lastStage = null;

  async function paint(full) {
    const t = await api("/api/tickets/" + encodeURIComponent(key));
    const got = await api(`/api/tickets/${encodeURIComponent(key)}/events?after=0`);
    events = got.events;
    const running = t.stage === "running";
    const hasResult = !!t.report_html;
    if (!full && lastStage === t.stage && running) {
      const feed = $("#feed"); if (feed) { feed.innerHTML = events.map(feedItem).join(""); feed.scrollTop = feed.scrollHeight; }
      const steps = $("#steps"); if (steps) steps.innerHTML = stepsHtml(t);
      const cost = $("#run-cost"); if (cost) cost.innerHTML = costChip(t);
      return t;
    }
    lastStage = t.stage;
    const isSample = key === STATUS.sample_ticket_key && STATUS.demo;
    const tabs = [["cause", "Root cause", !!t.root_cause_html], ["fix", "Fix", !!(t.fix_html || t.fix_diff)], ["report", "Full report", hasResult],
                  ["comment", "Ticket comment", !!t.comment_markdown], ["evidence", "Evidence", t.evidence.length > 0],
                  ["ticket", "Ticket", true], ["activity", "Activity", events.length > 0]].filter(x => x[2]);
    if (!tabs.some(x => x[0] === state.ticketTab)) state.ticketTab = tabs[0][0];
    const sampleRun = t.run && t.run.runner === "sample";

    main.innerHTML = `
      <div class="spread" style="margin-bottom:4px"><a class="small" href="#/tickets">← All tickets</a>
        <span class="row"><button class="btn sm danger" id="delete" ${running ? "disabled" : ""}>Delete</button></span></div>
      <h1>${esc(t.key)}${t.title ? " · " + esc(t.title) : ""}</h1>
      <p class="row" style="margin:0 0 20px">${stageChip(t.stage)} ${causeChip(t.root_cause_status)}
        ${t.evidence_check ? (t.evidence_check.ok ? `<span class="chip ok">✓ Evidence check passed · ${t.evidence_check.citations.valid} citations</span>` : `<span class="chip bad">Evidence check: ${t.evidence_check.problems.length} problem(s)</span>`) : ""}
        <span class="chip">Org: ${esc(t.org || "?")}</span>${t.sandbox ? `<span class="chip">Tests on: ${esc(t.sandbox)}</span>` : ""}
        <span id="run-cost">${costChip(t)}</span></p>
      <div id="ticket-error"></div>

      ${sampleRun && hasResult ? `<div class="banner info"><b>Recorded sample.</b> The evidence below was gathered just now from the simulated org by the real tools. The written analysis is the stored sample, not a fresh AI investigation. The simulated org does not execute Apex.</div>` : ""}

      ${hasResult ? `
        <div class="card verdict">
          <div class="row">${causeChip(t.root_cause_status)}</div>
          <div class="big prose">${t.root_cause_summary_html || esc(t.root_cause_summary || "")}</div>
          <div class="row small muted">Every claim is tied to an artifact. Open <b>Root cause</b> for the chain and <b>Evidence</b> for the files.
            ${t.has_report_html ? `<a class="btn sm" href="tickets/${encodeURIComponent(t.key)}/report.html" target="_blank" rel="noopener">Open printable report</a>` : ""}
            <a class="btn sm" href="tickets/${encodeURIComponent(t.key)}/result.zip" download>Download the result (.zip)</a></div>
          ${t.evidence_check && !t.evidence_check.ok ? `<div class="banner bad" style="margin:0">${t.evidence_check.problems.map(esc).join("<br>")}</div>` : ""}
          ${t.open_questions_html && !/^\s*<p>\s*(none|no open questions?)\.?\s*<\/p>\s*$/i.test(t.open_questions_html) ? `<div class="banner warn" style="margin:0"><b>The investigator had questions it could not ask.</b> Answering them can turn an assumption into a confirmed fact.
            <div class="prose small">${t.open_questions_html}</div><a href="#add-info" id="answer-link">Answer below and investigate again</a></div>` : ""}
        </div>` : ""}

      ${!hasResult || running ? `
      <div class="grid2">
        <div class="card">
          <div class="spread"><h2>${running ? (sampleRun ? "Replaying the recorded sample…" : "Investigating…") : "Start the investigation"}</h2>
            ${running ? `<button class="btn sm" id="cancel">Stop</button>` : ""}</div>
          ${running ? "" : `
            <div class="row" style="margin-bottom:12px">
              <button class="btn primary" id="run-agent" ${STATUS.claude.available ? "" : "disabled"}>${t.stage === "new" ? "Investigate with Claude Code" : "Continue with Claude Code"}</button>
              ${STATUS.claude.available ? modelSelect("model", t.model) : ""}
              ${isSample ? `<button class="btn" id="run-sample">Replay the recorded sample</button>` : ""}
            </div>
            ${STATUS.claude.available ? savingControls(t.economy, t.budget_usd) : ""}
            ${STATUS.claude.available ? `<p class="small muted">Starts Claude Code in this project folder with only the investigator's read-only tools: it reads from the org and writes into this ticket's folder. The tools that change an org are not given to it.</p>`
              : `<p class="small muted">Claude Code was not found, so this page cannot start it for you.</p>`}
            ${isSample ? `<p class="small muted"><b>Replay</b> shows a complete result in seconds without the AI: the real tools gather the evidence from the simulated org and the stored analysis is displayed.</p>` : ""}
            <details><summary>Or run it yourself in Claude Code</summary>
              <p class="small muted">Open Claude Code in this project folder and paste this. The result appears on this page as it is written.</p>
              <pre class="plain">${esc(t.prompt || "")}</pre>
              <button class="btn sm" data-copy="${esc(t.prompt || "")}" style="margin-top:8px">Copy the prompt</button>
            </details>`}
          ${events.length ? `<ul class="feed" id="feed" style="margin-top:8px">${events.map(feedItem).join("")}</ul>` : (running ? `<ul class="feed" id="feed"></ul>` : "")}
        </div>
        <div class="card flat"><h2>Progress</h2><ul class="checks" id="steps">${stepsHtml(t)}</ul></div>
      </div>` : ""}

      ${hasResult || t.ticket_html || events.length ? `
      <div class="card">
        <div class="tabs" id="tabs" role="tablist" aria-label="Result">${tabs.map(([id, label]) => `<button data-tab="${id}" id="tab-${id}" role="tab" aria-controls="tab" aria-selected="${id === state.ticketTab}" tabindex="${id === state.ticketTab ? 0 : -1}" class="${id === state.ticketTab ? "on" : ""}">${esc(label)}</button>`).join("")}</div>
        <div id="tab" role="tabpanel" aria-labelledby="tab-${state.ticketTab}" tabindex="0"></div>
      </div>` : ""}

      ${!running ? `
      <div class="card" id="add-info">
        <h2>Add information</h2>
        <p class="small muted">${hasResult ? "Something to add, or an answer to a question?" : "Anything to add before it starts?"} A record Id, a correction, a screenshot, an exported log. It is saved with the ticket; the next run reads it and continues from what is already there instead of starting again.</p>
        <textarea id="note" class="short" placeholder="For example: it also happens for cases created from email, see case 500…"></textarea>
        <div class="row" style="margin:10px 0"><label class="btn sm" for="file" style="margin:0">Attach a file</label><input type="file" id="file" hidden>
          <span class="files" id="files">${(t.attachments || []).map(name => `<span class="chip">${esc(name)}</span>`).join("")}</span></div>
        <div id="info-error"></div>
        ${hasResult && STATUS.claude.available ? savingControls(t.economy, t.budget_usd) : ""}
        ${hasResult ? `<div class="row"><button class="btn primary sm" id="rerun" ${STATUS.claude.available ? "" : "disabled"}>Investigate again with Claude Code</button>
          ${STATUS.claude.available ? modelSelect("model", t.model) : ""}
          ${isSample ? `<button class="btn sm" id="rerun-sample">Replay the sample again</button>` : ""}
          <a class="btn sm" href="#/memory">See what was learned about the org</a></div>`
          : `<p class="small muted" style="margin:0">Then press <b>${t.stage === "new" ? "Investigate" : "Continue"} with Claude Code</b> above. If you run it yourself in Claude Code, save the note first: <button class="btn sm" id="save-note">Save the note</button></p>`}
      </div>` : ""}`;

    const paintTab = () => {
      const box = $("#tab"); if (!box) return;
      const tab = state.ticketTab;
      if (tab === "cause") box.innerHTML = `<div class="prose">${t.root_cause_html || ""}</div>` +
        (t.open_questions_html ? `<h3>Open questions</h3><div class="prose">${t.open_questions_html}</div>` : "");
      else if (tab === "fix") box.innerHTML = `<div class="prose">${t.fix_html || ""}</div>` +
        (t.fix_diff ? `<div class="spread" style="margin-top:16px"><h3 style="margin:0">Complete diff (fix.diff)</h3><button class="btn sm" data-copy="${esc(t.fix_diff)}">Copy the diff</button></div><div style="margin-top:8px">${renderDiff(t.fix_diff)}</div>` : "") +
        (t.tests_html ? `<h3>Test results and test plan</h3><div class="prose">${t.tests_html}</div>` : "") +
        (t.risks_html ? `<h3>Risks</h3><div class="prose">${t.risks_html}</div>` : "") +
        `<div class="banner info" style="margin-top:16px">Nothing was deployed. Apply the change yourself, or ask the investigator in Claude Code with mode <code>apply</code>, where every deploy asks for your approval and production is refused.</div>`;
      else if (tab === "report") box.innerHTML = `<div class="prose">${t.report_html}</div>`;
      else if (tab === "comment") box.innerHTML = `<div class="banner warn">A draft. Nothing is posted anywhere: read it, then paste it into the ticket yourself.</div>
        <div class="prose">${t.comment_html}</div><button class="btn primary" style="margin-top:12px" data-copy="${esc((t.comment_markdown || "").replace(/^<!--[\s\S]*?-->\s*/, ""))}">Copy the comment</button>`;
      else if (tab === "evidence") box.innerHTML = `<p class="small muted">Files saved in <code>tickets/${esc(t.key)}/</code>. Query results are stored with personal data redacted.</p>
        <div class="tablewrap"><table><thead><tr><th>Kind</th><th>File</th><th>What it holds</th></tr></thead><tbody>${t.evidence.map(a => `
          <tr><td>${esc(String(a.kind).replace(/_/g, " "))}</td><td><code>${esc(a.path)}</code></td><td>${esc(a.description)}</td></tr>`).join("")}</tbody></table></div>`;
      else if (tab === "ticket") box.innerHTML = (t.has_pdf ? `<p><a class="btn sm" href="tickets/${encodeURIComponent(t.key)}/ticket.pdf" target="_blank" rel="noopener">Open the ticket's PDF</a></p>` : "") + (t.ticket_html ? `<div class="prose">${t.ticket_html}</div>` : `<p class="muted small">Not structured yet. This is the text as pasted:</p>`) +
        (t.original_text ? `<details ${t.ticket_html ? "" : "open"}><summary>Original text as pasted</summary><pre class="plain" style="margin-top:8px">${esc(t.original_text)}</pre></details>` : "");
      else if (tab === "activity") box.innerHTML = `<ul class="feed">${events.map(feedItem).join("")}</ul>`;
    };
    const tabList = $("#tabs");
    if (tabList) tabList.onkeydown = event => {
      const all = [...tabList.querySelectorAll("button")], at = all.indexOf(document.activeElement);
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (at < 0 || !step) return;
      event.preventDefault(); const next = all[(at + step + all.length) % all.length]; next.focus(); next.click();
    };
    document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => {
      state.ticketTab = b.dataset.tab;
      document.querySelectorAll("#tabs button").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-selected", x === b); x.tabIndex = x === b ? 0 : -1; });
      const panel = $("#tab"); if (panel) panel.setAttribute("aria-labelledby", b.id); paintTab();
    });
    paintTab();
    const feed = $("#feed"); if (feed) feed.scrollTop = feed.scrollHeight;

    const start = runner => async () => {
      const body = { runner };
      if (runner === "agent") {
        const note = $("#note"), model = $("#model");
        if (note && note.value.trim()) body.note = note.value;
        if (model) body.model = model.value;
        if ($("#economy")) Object.assign(body, savingChoice());
      }
      for (let attempt = 0; attempt < 2; attempt++) {
        try { await api(`/api/tickets/${encodeURIComponent(key)}/run`, { method: "POST", body }); lastStage = null; await paint(true); poll(); refreshMeter(); return; }
        catch (error) {
          // A ticket started in Claude Code has no org recorded: ask once, then send it with the run.
          if (attempt === 0 && error.details && error.details.needs_org) {
            const org = window.prompt(error.message + "\n\nAlias of the org to investigate:", "");
            if (org && org.trim()) { body.org = org.trim(); continue; }
          }
          $("#ticket-error").innerHTML = errorBox(error); window.scrollTo(0, 0); return;
        }
      }
    };
    const file = $("#file");
    if (file) file.onchange = async () => {
      const chosen = file.files[0]; if (!chosen) return;
      $("#info-error").innerHTML = "";
      try {
        const saved = await api(`/api/tickets/${encodeURIComponent(key)}/attachments`, { method: "POST", body: { name: chosen.name, content_base64: await readFile(chosen) } });
        const name = saved.saved.split("/").pop();
        if (![...$("#files").children].some(c => c.textContent === name)) $("#files").insertAdjacentHTML("beforeend", `<span class="chip">${esc(name)}</span>`);
        toast("Attached " + name);
      } catch (error) { $("#info-error").innerHTML = errorBox(error); }
      file.value = "";
    };
    const saveNote = $("#save-note");
    if (saveNote) saveNote.onclick = async () => {
      $("#info-error").innerHTML = "";
      try { const saved = await api(`/api/tickets/${encodeURIComponent(key)}/note`, { method: "POST", body: { text: $("#note").value } });
            $("#note").value = ""; $("#files").insertAdjacentHTML("beforeend", `<span class="chip">${esc(saved.saved.split("/").pop())}</span>`); toast("Note saved with the ticket"); }
      catch (error) { $("#info-error").innerHTML = errorBox(error); }
    };
    const answer = $("#answer-link"); if (answer) answer.onclick = event => { event.preventDefault(); $("#add-info").scrollIntoView({ behavior: "smooth" }); $("#note").focus(); };
    [["#run-agent", "agent"], ["#rerun", "agent"], ["#run-sample", "sample"], ["#rerun-sample", "sample"]].forEach(([sel, runner]) => {
      const button = $(sel); if (button) button.onclick = start(runner);
    });
    const cancel = $("#cancel"); if (cancel) cancel.onclick = async () => { await api(`/api/tickets/${encodeURIComponent(key)}/cancel`, { method: "POST" }); lastStage = null; paint(true); };
    const del = $("#delete"); if (del) del.onclick = async () => {
      if (!confirm(`Delete ${key} from this computer?\n\nThis removes its folder: the report, the saved evidence and the retrieved source. Nothing in Salesforce is touched.`)) return;
      try { await api("/api/tickets/" + encodeURIComponent(key), { method: "DELETE" }); location.hash = "#/tickets"; }
      catch (error) { $("#ticket-error").innerHTML = errorBox(error); }
    };
    return t;
  }
  function stepsHtml(t) {
    const running = t.stage === "running";
    let current = false;
    return t.steps.map(s => {
      const active = running && !s.done && !current; if (active) current = true;
      return `<li><span class="${s.done ? "mark-ok" : "mark-wait"}">${s.done ? "✓" : (active ? '<span class="spinner"></span>' : "○")}</span>
        <div>${esc(s.label)}${s.detail ? `<div class="small muted">${esc(s.detail)}</div>` : ""}</div></li>`;
    }).join("");
  }
  function poll() {
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try { const t = await paint(false); if (t.stage !== "running") { clearInterval(pollTimer); pollTimer = null; state.ticketTab = "cause"; await paint(true); refreshStatus(); } }
      catch (error) { clearInterval(pollTimer); }
    }, 1200);
  }
  const first = await paint(true);
  if (first.stage === "running") poll();
  else if (!first.report_html) {
    // The investigation may be running in Claude Code itself: keep looking for new files.
    pollTimer = setInterval(async () => { try { const t = await api("/api/tickets/" + encodeURIComponent(key));
      if (t.stage !== lastStage || t.report_html) { lastStage = null; await paint(true); if (t.report_html) { clearInterval(pollTimer); } } } catch (error) { clearInterval(pollTimer); } }, 4000);
  }
}

/* ---------- Org memory ---------- */
function describeComponent(c) {
  const o = c.outline || {};
  const bits = [];
  if (o.kind === "trigger" && o.object) bits.push(`Trigger on ${o.object} (${(o.events || []).join(", ")})`);
  if (o.kind === "flow") bits.push(`${o.trigger_type === "RecordAfterSave" ? "After-save" : o.trigger_type === "RecordBeforeSave" ? "Before-save" : "Flow"} on ${o.object || "?"}${o.record_trigger_type ? ", on " + o.record_trigger_type.toLowerCase() : ""}`);
  if (o.is_test) bits.push("Test class");
  if (o.methods && o.methods.length && !o.is_test) bits.push("Methods: " + o.methods.slice(0, 4).map(m => m.name).join(", ") + (o.methods.length > 4 ? "…" : ""));
  if (o.objects_queried && o.objects_queried.length) bits.push("Queries " + o.objects_queried.join(", "));
  if (o.fields_assigned && o.fields_assigned.length && !o.is_test) bits.push("Sets " + o.fields_assigned.slice(0, 5).join(", "));
  if (o.reads && o.reads.length) bits.push("Tests " + o.reads.join(", "));
  if (o.writes && o.writes.length) bits.push("Writes " + o.writes.join(", "));
  if (o.actions && o.actions.length) bits.push("Actions: " + o.actions.join(", "));
  if (o.calls && o.calls.length && !o.is_test) bits.push("Calls " + o.calls.slice(0, 3).join(", "));
  return bits.join(" · ");
}
function freshChip(check) {
  if (!check) return `<span class="chip">Not checked yet</span>`;
  const map = { unchanged: ["ok", "Unchanged in the org"], changed: ["warn", "Changed in the org"],
                not_found_in_org: ["bad", "No longer in the org"], not_checked: ["", "Could not be checked"] };
  const [tone, label] = map[check.status] || ["", check.status];
  return `<span class="chip ${tone}"><span class="dot"></span>${esc(label)}</span>`;
}
async function viewMemory(orgId) {
  main.innerHTML = `<h1>Org memory</h1>
    <p class="lead">What earlier investigations learned about each org: which automation runs on which object, what each class and flow does, how they connect, and the facts worth keeping. The next ticket starts from this instead of from zero.</p>
    <div id="memory"><p class="muted"><span class="spinner"></span> Loading…</p></div>`;
  await refreshStatus();
  const known = (await api("/api/memory")).orgs;
  if (!known.length) {
    if (!$("#memory")) return;
    $("#memory").innerHTML = `<div class="card empty"><p>Nothing remembered yet.</p><p class="small">The memory fills by itself during investigations: every component the investigator retrieves is added to the map.</p><a class="btn primary" href="#/new">Investigate a ticket</a></div>`;
    return;
  }
  const current = known.find(o => o.org_id === orgId) || known[0];
  const map = await api("/api/memory/" + encodeURIComponent(current.org_id));
  const all = Object.values(map.components_by_type).flat();
  const paint = verified => {
    const status = verified ? verified.components : {};
    const staleIds = new Set(verified ? verified.stale_notes : map.notes.filter(n => n.stale).map(n => n.id));
    if (!$("#memory")) return;
    $("#memory").innerHTML = `
      <div class="row" style="margin-bottom:14px">${known.map(o => `<a class="chip ${o.org_id === current.org_id ? "ok" : ""}" style="text-decoration:none" href="#/memory/${esc(o.org_id)}">${esc((o.aliases || []).join(", ") || o.org_id)}</a>`).join("")}</div>
      <div class="tiles">
        <div class="tile"><b>${map.counts.components}</b><span>components known</span></div>
        <div class="tile"><b>${map.counts.objects}</b><span>objects mapped</span></div>
        <div class="tile"><b>${map.counts.links}</b><span>links between them</span></div>
        <div class="tile"><b>${map.counts.notes}</b><span>facts remembered</span></div>
        <div class="tile"><b>${map.tickets.length}</b><span>tickets contributed</span></div>
      </div>
      <div class="card"><h2>Ask the memory</h2>
        <p class="small muted" style="margin:0 0 10px">The same search the investigator runs at the start of a ticket. Type the subject of a ticket to see where it would look first.</p>
        <div class="row"><input type="text" id="mem-q" placeholder="for example: case priority, voice call routing, renewal flow" style="flex:1;min-width:200px" autocomplete="off">
          <button class="btn" id="mem-search">Search</button></div>
        <div id="mem-hits" style="margin-top:10px"></div>
      </div>
      <div class="card">
        <div class="spread"><div><h2 style="margin:0">Is it still true?</h2>
          <p class="small muted" style="margin:4px 0 0">Orgs change. Before the investigator relies on anything here, it asks the org whether each component was modified since it was learned. You can run that check now.</p></div>
          <button class="btn primary" id="verify">Check against the org</button></div>
        <div id="verify-result" style="margin-top:10px">${verified ? `<div class="banner ${verified.counts.changed || verified.counts.not_found_in_org ? "warn" : "ok"}" style="margin:0">
          Checked ${when(verified.checked_at)}: ${Object.entries(verified.counts).map(([k, v]) => `${v} ${esc(k.replace(/_/g, " "))}`).join(", ")}.
          ${verified.stale_notes.length ? ` ${verified.stale_notes.length} remembered fact(s) are now flagged and will be re-read before use.` : ""}</div>` : ""}</div>
      </div>

      <div class="card"><h2>What runs on each object</h2>
        ${Object.keys(map.objects).length ? `<div class="tablewrap"><table><thead><tr><th>Object</th><th>Triggers</th><th>Flows</th><th>Validation rules</th><th>Workflow rules</th></tr></thead><tbody>
        ${Object.entries(map.objects).map(([name, o]) => `<tr><td><b>${esc(name)}</b></td>
          <td>${(o.triggers || []).map(esc).join(", ") || '<span class="muted">none seen</span>'}</td>
          <td>${(o.flows || []).map(esc).join(", ") || '<span class="muted">none seen</span>'}</td>
          <td>${o.validation_rules ? (o.validation_rules.map(esc).join(", ") || "none") : '<span class="muted">not looked at</span>'}</td>
          <td>${o.workflow_rules ? (o.workflow_rules.map(esc).join(", ") || "none") : '<span class="muted">not looked at</span>'}</td></tr>`).join("")}
        </tbody></table></div>` : `<p class="muted">No object mapped yet.</p>`}</div>

      <div class="card"><h2>Components</h2>
        <div class="tablewrap"><table><thead><tr><th>Component</th><th>What it does</th><th>Seen in</th><th>State</th></tr></thead><tbody>
        ${all.map(c => `<tr><td><b>${esc(c.component.split(":")[1])}</b><div class="small muted">${esc(c.component.split(":")[0])}</div></td>
          <td class="small">${esc(describeComponent(c)) || '<span class="muted">—</span>'}</td>
          <td class="small">${(c.seen_in_tickets || []).map(k => `<a href="#/ticket/${encodeURIComponent(k)}">${esc(k)}</a>`).join(", ")}</td>
          <td>${freshChip(status[c.component] || null)}<div class="small muted">last read ${when(c.last_seen)}</div></td></tr>`).join("")}
        </tbody></table></div></div>

      <div class="card"><h2>Facts remembered</h2>
        ${map.notes.length ? map.notes.map(n => `<div class="note-card ${staleIds.has(n.id) ? "stale" : ""}">
          ${staleIds.has(n.id) ? `<div class="chip warn" style="margin-bottom:6px">Needs re-reading: ${esc((n.stale_because || ["a component it is about changed"]).join("; "))}</div>` : ""}
          <div>${esc(n.text)}</div>
          <div class="row small muted" style="margin-top:8px">${(n.topics || []).map(t => `<span class="chip">${esc(t)}</span>`).join("")}
            ${n.ticket_key ? `<span>learned in <a href="#/ticket/${encodeURIComponent(n.ticket_key)}">${esc(n.ticket_key)}</a></span>` : ""}
            ${(n.evidence || []).length ? `<span>from <code>${esc(n.evidence[0])}</code></span>` : ""}</div></div>`).join("")
          : `<p class="muted">No fact noted yet. The map above was built automatically; notes are added by the investigator at the end of a ticket.</p>`}</div>

      ${map.links.length ? `<div class="card"><h2>How they connect</h2><div class="tablewrap"><table><thead><tr><th>From</th><th></th><th>To</th><th>Known from</th></tr></thead><tbody>
        ${map.links.map(l => `<tr><td>${esc(l.from.split(":")[1])}</td><td class="muted">${esc(l.kind)} →</td><td>${esc(l.to.split(":")[1])}</td><td class="small muted">${l.origin === "source" ? "reading the source" : "Salesforce dependency data"}</td></tr>`).join("")}
        </tbody></table></div></div>` : ""}
      <p class="small muted">Memory is a lead, never evidence: a report can only cite what was read from the org during that investigation. Stored in <code>knowledge/</code> on this computer; it holds facts about metadata, no record data, and is never committed or uploaded.</p>`;
    const search = async () => {
      const words = $("#mem-q").value.trim(); if (!words) return;
      const box = $("#mem-hits"); box.innerHTML = '<p class="muted small"><span class="spinner"></span> Searching…</p>';
      try {
        const found = await api(`/api/memory/${encodeURIComponent(current.org_id)}/search?q=${encodeURIComponent(words)}`);
        const hits = [
          ...found.notes.map(n => `<div class="hit"><span class="chip ${n.stale ? "warn" : "ok"}">${n.stale ? "Fact, needs re-reading" : "Fact"}</span> ${esc(n.text)}
            ${n.ticket_key ? `<span class="small muted"> · learned in <a href="#/ticket/${encodeURIComponent(n.ticket_key)}">${esc(n.ticket_key)}</a></span>` : ""}</div>`),
          ...found.components.map(c => `<div class="hit"><span class="chip">${esc(c.component.split(":")[0])}</span> <b>${esc(c.component.split(":")[1])}</b>
            <span class="small muted"> ${esc(describeComponent(c))}</span></div>`),
          ...(found.objects || []).map(o => `<div class="hit"><span class="chip">Object</span> <b>${esc(o.object || o.name || "")}</b>
            <span class="small muted"> ${esc([(o.triggers || []).length ? "triggers: " + o.triggers.join(", ") : "", (o.flows || []).length ? "flows: " + o.flows.join(", ") : ""].filter(Boolean).join(" · "))}</span></div>`),
        ];
        box.innerHTML = hits.length ? hits.join("") : `<p class="muted small">Nothing remembered for these words. That only means no earlier ticket touched it.</p>`;
      } catch (error) { box.innerHTML = errorBox(error); }
    };
    $("#mem-search").onclick = search;
    $("#mem-q").onkeydown = event => { if (event.key === "Enter") search(); };
    $("#verify").onclick = async () => {
      const button = $("#verify"); button.disabled = true; button.innerHTML = '<span class="spinner"></span> Asking the org…';
      try { paint(await api(`/api/memory/${encodeURIComponent(current.org_id)}/verify`, { method: "POST" })); }
      catch (error) { button.disabled = false; button.textContent = "Check against the org"; $("#verify-result").innerHTML = errorBox(error); }
    };
  };
  paint(null);
}

/* ---------- Safety ---------- */
async function viewSafety() {
  await refreshStatus();
  main.innerHTML = `
    <h1>How it stays safe</h1>
    <p class="lead">The investigator is built so that the worst it can do by mistake is be wrong on paper.</p>
    <div class="card"><h2>Production is read-only</h2><p>Before every write, the org is asked what it is. Anything that is not provably a sandbox, a scratch org or a Developer Edition org counts as production, and writes to production are refused in code. There is no setting, option or instruction that changes this.</p></div>
    <div class="card"><h2>Every change needs you</h2><p>In Claude Code, each tool that changes an org asks for your approval with its exact arguments. Investigations started from this page are not given those tools at all, nor a shell or web access: they only read, and hand you the fix as a diff.</p></div>
    <div class="card"><h2>Evidence, or it says so</h2><p>Every claim about the cause must point to a file and line, a saved query result or a debug log line. A checker verifies that each of those exists before a report is shown as passed. What cannot be proven is labelled <b>Hypothesis</b>, and a cause that cannot be confirmed is reported as <b>Not confirmed</b> with what is missing.</p></div>
    <div class="card"><h2>Your data stays here</h2><p>Query results are saved with personal data redacted. Reports and comments use record Ids, not names. Tickets, evidence and the org memory live in folders on this computer that are never committed or uploaded. Nothing is posted to your ticket system: you get a draft.</p></div>
    <div class="card"><h2>This page</h2><p>It answers only on this computer (<code>127.0.0.1</code>). It has no button, and no hidden route, that changes a Salesforce org.</p></div>
    <p class="small muted">Details and the list of what has not yet been verified against a live org are in <code>docs/SAFETY.md</code>, <code>docs/VERIFICATION.md</code> and <code>docs/DECISIONS.md</code>, in the project folder.</p>`;
}

refreshStatus().then(route).catch(error => { main.innerHTML = errorBox(error); });
refreshMeter();
