// ============================================================
// Knowledge Pro Dashboard — Section Renderers
// Each function fetches data, renders it, and wires realtime.
// ============================================================

function panelHeader(title, subtitle) {
  return `
    <div class="mb-1">
      <h2 class="text-xl font-bold">${title}</h2>
      ${subtitle ? `<p class="text-xs" style="color:var(--text-dim)">${subtitle}</p>` : ""}
    </div>
  `;
}

function emptyState(msg) {
  return `<div class="text-center py-10 text-sm" style="color:var(--text-dim)">${msg}</div>`;
}

function fmtDate(ts) {
  if (!ts) return "—";
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}

async function renderSection(id) {
  const content = document.getElementById("content");
  content.innerHTML = `<div class="glass glow p-6">${emptyState("Loading...")}</div>`;
  try {
    switch (id) {
      case "overview": return renderOverview(content);
      case "moderation": return renderModeration(content);
      case "admins": return renderAdmins(content);
      case "antispam": return renderAntiSpam(content);
      case "blockedwords": return renderBlockedWords(content);
      case "autoreplies": return renderAutoReplies(content);
      case "afk": return renderAFK(content);
      case "shoutlog": return renderShoutLog(content);
      case "aisessions": return renderAISessions(content);
      case "premium": return renderPremium(content);
      case "logs": return renderLogs(content);
      case "settings": return renderSettings(content);
      default: content.innerHTML = emptyState("Unknown section");
    }
  } catch (err) {
    console.error(err);
    content.innerHTML = `<div class="glass glow p-6">${emptyState("⚠️ Failed to load: " + err.message)}</div>`;
  }
}

// ---------------------------------------------------------------
// 1. Overview
// ---------------------------------------------------------------
async function renderOverview(content) {
  const counts = await Promise.all([
    supabaseClient.from("bans").select("*", { count: "exact", head: true }),
    supabaseClient.from("warnings").select("*", { count: "exact", head: true }),
    supabaseClient.from("afk_users").select("*", { count: "exact", head: true }),
    supabaseClient.from("premium_users").select("*", { count: "exact", head: true }),
    supabaseClient.from("ai_sessions").select("*", { count: "exact", head: true }),
    supabaseClient.from("blocked_words").select("*", { count: "exact", head: true }),
  ]);
  const [bans, warns, afk, premium, ai, blocked] = counts.map(c => c.count ?? 0);

  const { data: recentLogs } = await supabaseClient
    .from("logs").select("*").order("timestamp", { ascending: false }).limit(8);

  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("📊 Overview", "Live snapshot of your bot & database")}</div>
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      ${statCard("Bans", bans, "#FC7683")}
      ${statCard("Warnings", warns, "#FACD68")}
      ${statCard("AFK", afk, "#93c5fd")}
      ${statCard("Premium", premium, "#6ee7b7")}
      ${statCard("AI Sessions", ai, "#c4b5fd")}
      ${statCard("Blocked Words", blocked, "#fca5a5")}
    </div>
    <div class="glass glow p-6">
      ${panelHeader("Recent Activity")}
      <div id="overviewLogFeed" class="mt-3 space-y-2"></div>
    </div>
  `;
  renderLogFeed(recentLogs || [], "overviewLogFeed");

  subscribeRealtime("logs", async () => {
    const { data } = await supabaseClient.from("logs").select("*").order("timestamp", { ascending: false }).limit(8);
    renderLogFeed(data || [], "overviewLogFeed");
  });
}

function statCard(label, value, color) {
  return `
    <div class="glass glow p-4">
      <div class="text-xs" style="color:var(--text-dim)">${label}</div>
      <div class="text-2xl font-bold mono mt-1" style="color:${color}">${value}</div>
    </div>
  `;
}

function renderLogFeed(logs, targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  el.innerHTML = logs.length ? logs.map(l => `
    <div class="flex items-center justify-between text-xs py-2 border-b" style="border-color:var(--panel-border)">
      <span><span class="badge badge-warn mono">${l.action_type}</span> actor:${l.actor_id ?? "—"} ${l.target_id ? "→ " + l.target_id : ""}</span>
      <span style="color:var(--text-dim)">${fmtDate(l.timestamp)}</span>
    </div>
  `).join("") : emptyState("No activity yet");
}

// ---------------------------------------------------------------
// 2. Moderation (bans/kicks + warnings)
// ---------------------------------------------------------------
async function renderModeration(content) {
  const { data: bans } = await supabaseClient.from("bans").select("*").order("timestamp", { ascending: false }).limit(100);
  const { data: warns } = await supabaseClient.from("warnings").select("*").order("timestamp", { ascending: false }).limit(100);

  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("🛡️ Moderation", "Bans, kicks & warnings")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <h3 class="font-semibold mb-2 text-sm">Bans / Kicks</h3>
      <table><thead><tr><th>User</th><th>Type</th><th>Reason</th><th>Admin</th><th>When</th><th></th></tr></thead>
      <tbody id="bansTable"></tbody></table>
    </div>
    <div class="glass glow p-4 overflow-x-auto">
      <h3 class="font-semibold mb-2 text-sm">Warnings</h3>
      <table><thead><tr><th>User</th><th>Reason</th><th>Admin</th><th>When</th><th></th></tr></thead>
      <tbody id="warnsTable"></tbody></table>
    </div>
  `;

  const renderBans = (rows) => {
    document.getElementById("bansTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.user_id}</td>
        <td><span class="badge badge-danger">${r.action_type}</span></td>
        <td>${r.reason || "—"}</td>
        <td class="mono">${r.admin_id ?? "—"}</td>
        <td style="color:var(--text-dim)">${fmtDate(r.timestamp)}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('bans','${r.id}')">Remove</button></td>
      </tr>`).join("") : `<tr><td colspan="6">${emptyState("No bans/kicks yet")}</td></tr>`;
  };
  const renderWarns = (rows) => {
    document.getElementById("warnsTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.user_id}</td>
        <td>${r.reason || "—"}</td>
        <td class="mono">${r.admin_id ?? "—"}</td>
        <td style="color:var(--text-dim)">${fmtDate(r.timestamp)}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('warnings','${r.id}')">Remove</button></td>
      </tr>`).join("") : `<tr><td colspan="5">${emptyState("No warnings yet")}</td></tr>`;
  };

  renderBans(bans || []);
  renderWarns(warns || []);

  subscribeRealtime("bans", async () => {
    const { data } = await supabaseClient.from("bans").select("*").order("timestamp", { ascending: false }).limit(100);
    renderBans(data || []);
  });
  subscribeRealtime("warnings", async () => {
    const { data } = await supabaseClient.from("warnings").select("*").order("timestamp", { ascending: false }).limit(100);
    renderWarns(data || []);
  });
}

// Generic row deleter — handles tables whose primary key isn't "id"
async function deleteRow(table, id) {
  const pk = (table === "afk_users" || table === "premium_users" || table === "ai_sessions") ? "user_id" : "id";
  confirmModal("Remove entry?", "This cannot be undone.", async () => {
    const { error } = await supabaseClient.from(table).delete().eq(pk, id);
    if (error) alert("Failed: " + error.message);
  });
}

// ---------------------------------------------------------------
// 3. Admins
// ---------------------------------------------------------------
async function renderAdmins(content) {
  const { data } = await supabaseClient.from("admins").select("*").order("promoted_at", { ascending: false });
  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("👑 Admins", "Promote/demote log & current admins")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>User</th><th>Chat</th><th>Promoted By</th><th>Reason</th><th>When</th><th></th></tr></thead>
      <tbody id="adminsTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("adminsTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.user_id}</td>
        <td class="mono">${r.chat_id}</td>
        <td class="mono">${r.promoted_by ?? "—"}</td>
        <td>${r.reason || "—"}</td>
        <td style="color:var(--text-dim)">${fmtDate(r.promoted_at)}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('admins','${r.id}')">Revoke</button></td>
      </tr>`).join("") : `<tr><td colspan="6">${emptyState("No admins recorded")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("admins", async () => {
    const { data } = await supabaseClient.from("admins").select("*").order("promoted_at", { ascending: false });
    render(data || []);
  });
}

// ---------------------------------------------------------------
// 4. Anti-Spam
// ---------------------------------------------------------------
async function renderAntiSpam(content) {
  const { data } = await supabaseClient.from("groups").select("*").order("chat_id");
  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("🚫 Anti-Spam", "Toggle per-group anti-spam protection")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>Chat ID</th><th>Status</th><th></th></tr></thead>
      <tbody id="antispamTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("antispamTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.chat_id}</td>
        <td>${r.anti_spam ? '<span class="badge badge-ok">Enabled</span>' : '<span class="badge badge-danger">Disabled</span>'}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="toggleAntiSpam('${r.chat_id}', ${!r.anti_spam})">Toggle</button></td>
      </tr>`).join("") : `<tr><td colspan="3">${emptyState("No groups registered yet — use /antispam in a group to register it")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("groups", async () => {
    const { data } = await supabaseClient.from("groups").select("*").order("chat_id");
    render(data || []);
  });
}
async function toggleAntiSpam(chatId, newState) {
  const { error } = await supabaseClient.from("groups").update({ anti_spam: newState }).eq("chat_id", chatId);
  if (error) alert("Failed: " + error.message);
}

// ---------------------------------------------------------------
// 5. Blocked Words
// ---------------------------------------------------------------
async function renderBlockedWords(content) {
  const { data } = await supabaseClient.from("blocked_words").select("*").order("created_at", { ascending: false });
  content.innerHTML = `
    <div class="glass glow p-6 flex items-center justify-between">
      ${panelHeader("🔇 Blocked Words", "Words that trigger automatic punishment")}
      <button class="gradient-btn text-xs" onclick="openAddBlockedWord()">+ Add Word</button>
    </div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>Word</th><th>Reason</th><th>Punishment</th><th>Chat</th><th></th></tr></thead>
      <tbody id="blockedTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("blockedTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.word}</td>
        <td>${r.reason || "—"}</td>
        <td><span class="badge badge-warn">${r.punishment}</span></td>
        <td class="mono">${r.chat_id}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('blocked_words','${r.id}')">Remove</button></td>
      </tr>`).join("") : `<tr><td colspan="5">${emptyState("No blocked words yet")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("blocked_words", async () => {
    const { data } = await supabaseClient.from("blocked_words").select("*").order("created_at", { ascending: false });
    render(data || []);
  });
}
function openAddBlockedWord() {
  showModal(`
    <h3 class="font-bold text-lg mb-4">Add Blocked Word</h3>
    <div class="space-y-3">
      <input id="bwChat" placeholder="Chat ID" class="w-full" />
      <input id="bwWord" placeholder="Word" class="w-full" />
      <input id="bwReason" placeholder="Reason" class="w-full" />
      <select id="bwPunishment" class="w-full">
        <option value="warn">Warn</option><option value="kick">Kick</option>
        <option value="ban">Ban</option><option value="mute">Mute</option>
      </select>
    </div>
    <div class="flex justify-end gap-2 mt-5">
      <button class="text-xs px-4 py-2 rounded-lg" style="border:1px solid var(--panel-border)" onclick="closeModal()">Cancel</button>
      <button class="gradient-btn text-xs" onclick="submitBlockedWord()">Add</button>
    </div>
  `);
}
async function submitBlockedWord() {
  const chat_id = document.getElementById("bwChat").value.trim();
  const word = document.getElementById("bwWord").value.trim();
  const reason = document.getElementById("bwReason").value.trim();
  const punishment = document.getElementById("bwPunishment").value;
  if (!chat_id || !word) return alert("Chat ID and word are required.");
  const { error } = await supabaseClient.from("blocked_words").insert({ chat_id, word, reason, punishment });
  if (error) return alert("Failed: " + error.message);
  closeModal();
}

// ---------------------------------------------------------------
// 6. Auto-Replies
// ---------------------------------------------------------------
async function renderAutoReplies(content) {
  const { data } = await supabaseClient.from("auto_replies").select("*").order("created_at", { ascending: false });
  content.innerHTML = `
    <div class="glass glow p-6 flex items-center justify-between">
      ${panelHeader("💬 Auto-Replies", "Trigger → response mappings")}
      <button class="gradient-btn text-xs" onclick="openAddAutoReply()">+ Add Reply</button>
    </div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>Trigger</th><th>Response</th><th>Type</th><th>Chat</th><th></th></tr></thead>
      <tbody id="autoreplyTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("autoreplyTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.trigger_text}</td>
        <td>${r.response_text || "—"}</td>
        <td><span class="badge badge-ok">${r.response_type}</span></td>
        <td class="mono">${r.chat_id}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('auto_replies','${r.id}')">Remove</button></td>
      </tr>`).join("") : `<tr><td colspan="5">${emptyState("No auto-replies yet")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("auto_replies", async () => {
    const { data } = await supabaseClient.from("auto_replies").select("*").order("created_at", { ascending: false });
    render(data || []);
  });
}
function openAddAutoReply() {
  showModal(`
    <h3 class="font-bold text-lg mb-4">Add Auto-Reply</h3>
    <div class="space-y-3">
      <input id="arChat" placeholder="Chat ID" class="w-full" />
      <input id="arTrigger" placeholder="Trigger text" class="w-full" />
      <input id="arResponse" placeholder="Response text" class="w-full" />
    </div>
    <div class="flex justify-end gap-2 mt-5">
      <button class="text-xs px-4 py-2 rounded-lg" style="border:1px solid var(--panel-border)" onclick="closeModal()">Cancel</button>
      <button class="gradient-btn text-xs" onclick="submitAutoReply()">Add</button>
    </div>
  `);
}
async function submitAutoReply() {
  const chat_id = document.getElementById("arChat").value.trim();
  const trigger_text = document.getElementById("arTrigger").value.trim();
  const response_text = document.getElementById("arResponse").value.trim();
  if (!chat_id || !trigger_text) return alert("Chat ID and trigger are required.");
  const { error } = await supabaseClient.from("auto_replies").insert({ chat_id, trigger_text, response_text, response_type: "text" });
  if (error) return alert("Failed: " + error.message);
  closeModal();
}

// ---------------------------------------------------------------
// 7. AFK Users
// ---------------------------------------------------------------
async function renderAFK(content) {
  const { data } = await supabaseClient.from("afk_users").select("*").order("since_timestamp", { ascending: false });
  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("💤 AFK Users", "Currently away")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>User</th><th>Reason</th><th>Since</th><th></th></tr></thead>
      <tbody id="afkTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("afkTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.user_id}</td>
        <td>${r.reason || "—"}</td>
        <td style="color:var(--text-dim)">${fmtDate(r.since_timestamp)}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('afk_users','${r.user_id}')">Clear</button></td>
      </tr>`).join("") : `<tr><td colspan="4">${emptyState("No one is AFK")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("afk_users", async () => {
    const { data } = await supabaseClient.from("afk_users").select("*").order("since_timestamp", { ascending: false });
    render(data || []);
  });
}
// ---------------------------------------------------------------
// 8. Shout Log (pulled from logs table filtered by action_type)
// ---------------------------------------------------------------
async function renderShoutLog(content) {
  const { data } = await supabaseClient.from("logs").select("*")
    .in("action_type", ["shout", "shout_blocked"]).order("timestamp", { ascending: false }).limit(100);
  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("📢 Shout Log", "History + blocked-shout attempts")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>Type</th><th>Actor</th><th>Chat</th><th>Details</th><th>When</th></tr></thead>
      <tbody id="shoutTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("shoutTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td>${r.action_type === "shout_blocked" ? '<span class="badge badge-danger">Blocked</span>' : '<span class="badge badge-ok">Sent</span>'}</td>
        <td class="mono">${r.actor_id ?? "—"}</td>
        <td class="mono">${r.chat_id ?? "—"}</td>
        <td>${r.details || "—"}</td>
        <td style="color:var(--text-dim)">${fmtDate(r.timestamp)}</td>
      </tr>`).join("") : `<tr><td colspan="5">${emptyState("No shouts yet")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("logs", async () => {
    const { data } = await supabaseClient.from("logs").select("*")
      .in("action_type", ["shout", "shout_blocked"]).order("timestamp", { ascending: false }).limit(100);
    render(data || []);
  });
}

// ---------------------------------------------------------------
// 9. AI Sessions
// ---------------------------------------------------------------
async function renderAISessions(content) {
  const { data } = await supabaseClient.from("ai_sessions").select("*").order("last_active", { ascending: false });
  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("🤖 AI Sessions", "Per-user persona & activity")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>User</th><th>Persona</th><th>Last Active</th><th></th></tr></thead>
      <tbody id="aiTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("aiTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td class="mono">${r.user_id}</td>
        <td><span class="badge badge-warn">Prompt ${r.selected_prompt}</span></td>
        <td style="color:var(--text-dim)">${fmtDate(r.last_active)}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('ai_sessions','${r.user_id}')">Reset</button></td>
      </tr>`).join("") : `<tr><td colspan="4">${emptyState("No AI sessions yet")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("ai_sessions", async () => {
    const { data } = await supabaseClient.from("ai_sessions").select("*").order("last_active", { ascending: false });
    render(data || []);
  });
}

// ---------------------------------------------------------------
// 10. Premium Users
// ---------------------------------------------------------------
async function renderPremium(content) {
  const { data } = await supabaseClient.from("premium_users").select("*").order("expiry", { ascending: true });
  content.innerHTML = `
    <div class="glass glow p-6 flex items-center justify-between">
      ${panelHeader("💎 Premium Users", "Manage premium access & expiry")}
      <button class="gradient-btn text-xs" onclick="openAddPremium()">+ Add User</button>
    </div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>User</th><th>Expiry</th><th></th></tr></thead>
      <tbody id="premiumTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("premiumTable").innerHTML = rows.length ? rows.map(r => {
      const expired = r.expiry && new Date(r.expiry) < new Date();
      return `
      <tr>
        <td class="mono">${r.user_id}</td>
        <td>${r.expiry ? fmtDate(r.expiry) : "Never"} ${expired ? '<span class="badge badge-danger">Expired</span>' : ""}</td>
        <td><button class="text-xs gradient-text font-semibold" onclick="deleteRow('premium_users','${r.user_id}')">Remove</button></td>
      </tr>`;
    }).join("") : `<tr><td colspan="3">${emptyState("No premium users yet")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("premium_users", async () => {
    const { data } = await supabaseClient.from("premium_users").select("*").order("expiry", { ascending: true });
    render(data || []);
  });
}
function openAddPremium() {
  showModal(`
    <h3 class="font-bold text-lg mb-4">Add Premium User</h3>
    <div class="space-y-3">
      <input id="pmUser" placeholder="User ID" class="w-full" />
      <input id="pmExpiry" type="datetime-local" class="w-full" />
    </div>
    <div class="flex justify-end gap-2 mt-5">
      <button class="text-xs px-4 py-2 rounded-lg" style="border:1px solid var(--panel-border)" onclick="closeModal()">Cancel</button>
      <button class="gradient-btn text-xs" onclick="submitPremium()">Add</button>
    </div>
  `);
}
async function submitPremium() {
  const user_id = document.getElementById("pmUser").value.trim();
  const expiryRaw = document.getElementById("pmExpiry").value;
  if (!user_id) return alert("User ID is required.");
  const expiry = expiryRaw ? new Date(expiryRaw).toISOString() : null;
  const { error } = await supabaseClient.from("premium_users").upsert({ user_id, expiry }, { onConflict: "user_id" });
  if (error) return alert("Failed: " + error.message);
  closeModal();
}

// ---------------------------------------------------------------
// 11. Activity Logs (global feed)
// ---------------------------------------------------------------
async function renderLogs(content) {
  const { data } = await supabaseClient.from("logs").select("*").order("timestamp", { ascending: false }).limit(200);
  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("📜 Activity Logs", "Global realtime feed of every bot action")}</div>
    <div class="glass glow p-4 overflow-x-auto">
      <table><thead><tr><th>Action</th><th>Actor</th><th>Target</th><th>Chat</th><th>Details</th><th>When</th></tr></thead>
      <tbody id="logsTable"></tbody></table>
    </div>
  `;
  const render = (rows) => {
    document.getElementById("logsTable").innerHTML = rows.length ? rows.map(r => `
      <tr>
        <td><span class="badge badge-warn mono">${r.action_type}</span></td>
        <td class="mono">${r.actor_id ?? "—"}</td>
        <td class="mono">${r.target_id ?? "—"}</td>
        <td class="mono">${r.chat_id ?? "—"}</td>
        <td>${r.details || "—"}</td>
        <td style="color:var(--text-dim)">${fmtDate(r.timestamp)}</td>
      </tr>`).join("") : `<tr><td colspan="6">${emptyState("No activity yet")}</td></tr>`;
  };
  render(data || []);
  subscribeRealtime("logs", async () => {
    const { data } = await supabaseClient.from("logs").select("*").order("timestamp", { ascending: false }).limit(200);
    render(data || []);
  });
}

// ---------------------------------------------------------------
// 12. Settings
// ---------------------------------------------------------------
async function renderSettings(content) {
  const { data: session } = await supabaseClient.auth.getSession();
  const { data: settingsRow } = await supabaseClient.from("bot_settings").select("*").eq("id", 1).single();
  const maintenanceOn = settingsRow?.maintenance_mode ?? false;

  content.innerHTML = `
    <div class="glass glow p-6">${panelHeader("⚙️ Settings", "Environment & account")}</div>
    <div class="glass glow p-6 space-y-3">
      <div class="flex justify-between items-center text-sm">
        <div>
          <div>Maintenance Mode</div>
          <div class="text-xs" style="color:var(--text-dim)">When ON, the bot replies "can't run" to every command (owner is exempt)</div>
        </div>
        <button id="maintenanceToggle" class="gradient-btn text-xs">
          ${maintenanceOn ? "🔴 Turn OFF" : "🟢 Turn ON"}
        </button>
      </div>
      <div class="text-xs" id="maintenanceStatus">Status: <span class="badge ${maintenanceOn ? "badge-danger" : "badge-ok"}">${maintenanceOn ? "Maintenance ON" : "Live"}</span></div>
    </div>
    <div class="glass glow p-6 space-y-3">
      <div class="flex justify-between text-sm"><span style="color:var(--text-dim)">Logged in as</span><span class="mono">${session.session?.user?.email ?? "—"}</span></div>
      <div class="flex justify-between text-sm"><span style="color:var(--text-dim)">Supabase URL</span><span class="mono">${SUPABASE_URL}</span></div>
      <div class="flex justify-between text-sm"><span style="color:var(--text-dim)">Realtime</span><span class="badge badge-ok">Connected</span></div>
    </div>
    <div class="glass glow p-6">
      <h3 class="font-semibold mb-2 text-sm">Theme</h3>
      <p class="text-xs mb-3" style="color:var(--text-dim)">Toggle dark/light using the button in the top bar. Your preference is saved locally.</p>
    </div>
  `;

  document.getElementById("maintenanceToggle").addEventListener("click", async () => {
    const { error } = await supabaseClient.from("bot_settings")
      .update({ maintenance_mode: !maintenanceOn, updated_at: new Date().toISOString() })
      .eq("id", 1);
    if (error) return alert("Failed: " + error.message);
    renderSettings(content);
  });

  subscribeRealtime("bot_settings", () => renderSettings(content));
}
