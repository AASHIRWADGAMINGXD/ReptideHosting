// ============================================================
// Knowledge Pro Dashboard — Core App
// ============================================================

const SECTIONS = [
  { id: "overview", label: "Overview", icon: "📊" },
  { id: "moderation", label: "Moderation", icon: "🛡️" },
  { id: "admins", label: "Admins", icon: "👑" },
  { id: "antispam", label: "Anti-Spam", icon: "🚫" },
  { id: "blockedwords", label: "Blocked Words", icon: "🔇" },
  { id: "autoreplies", label: "Auto-Replies", icon: "💬" },
  { id: "afk", label: "AFK Users", icon: "💤" },
  { id: "shoutlog", label: "Shout Log", icon: "📢" },
  { id: "aisessions", label: "AI Sessions", icon: "🤖" },
  { id: "premium", label: "Premium Users", icon: "💎" },
  { id: "logs", label: "Activity Logs", icon: "📜" },
  { id: "settings", label: "Settings", icon: "⚙️" },
];

let currentSection = "overview";
let activeChannels = [];

// ---------------------------------------------------------------
// Auth guard
// ---------------------------------------------------------------
async function guardAuth() {
  const { data } = await supabaseClient.auth.getSession();
  if (!data.session) {
    window.location.href = "index.html";
    return null;
  }
  document.getElementById("userEmail").textContent = data.session.user.email;
  return data.session;
}

document.getElementById("logoutBtn").addEventListener("click", async () => {
  await supabaseClient.auth.signOut();
  window.location.href = "index.html";
});

supabaseClient.auth.onAuthStateChange((event) => {
  if (event === "SIGNED_OUT") window.location.href = "index.html";
});

// ---------------------------------------------------------------
// Theme
// ---------------------------------------------------------------
function initTheme() {
  const saved = localStorage.getItem("kp-theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
}
document.getElementById("themeToggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("kp-theme", next);
});

// ---------------------------------------------------------------
// Sidebar / nav
// ---------------------------------------------------------------
function buildNav() {
  const nav = document.getElementById("navLinks");
  nav.innerHTML = SECTIONS.map(s => `
    <div class="sidebar-link" data-section="${s.id}">
      <span>${s.icon}</span><span>${s.label}</span>
    </div>
  `).join("");

  nav.querySelectorAll(".sidebar-link").forEach(el => {
    el.addEventListener("click", () => navigateTo(el.dataset.section));
  });

  document.querySelectorAll(".bn-btn").forEach(el => {
    el.addEventListener("click", () => navigateTo(el.dataset.section));
  });
}

function navigateTo(sectionId) {
  currentSection = sectionId;
  document.querySelectorAll(".sidebar-link").forEach(el => {
    el.classList.toggle("active", el.dataset.section === sectionId);
  });
  document.getElementById("searchInput").value = "";
  teardownRealtime();
  renderSection(sectionId);
}

document.getElementById("searchInput").addEventListener("input", (e) => {
  filterCurrentTable(e.target.value);
});

function filterCurrentTable(query) {
  const rows = document.querySelectorAll("#content tbody tr");
  const q = query.trim().toLowerCase();
  rows.forEach(row => {
    row.style.display = !q || row.textContent.toLowerCase().includes(q) ? "" : "none";
  });
}

// ---------------------------------------------------------------
// Realtime helper — subscribes to a table and re-renders on change
// ---------------------------------------------------------------
function subscribeRealtime(table, onChange) {
  const channel = supabaseClient
    .channel(`kp-${table}-${Date.now()}`)
    .on("postgres_changes", { event: "*", schema: "public", table }, onChange)
    .subscribe();
  activeChannels.push(channel);
  return channel;
}

function teardownRealtime() {
  activeChannels.forEach(ch => supabaseClient.removeChannel(ch));
  activeChannels = [];
}

// ---------------------------------------------------------------
// Modal helper (used by section renderers for confirmations/forms)
// ---------------------------------------------------------------
function showModal(innerHtml) {
  const root = document.getElementById("modalRoot");
  root.innerHTML = `
    <div class="modal-backdrop" id="modalBackdrop">
      <div class="glass glow p-6 w-full max-w-md mx-4">${innerHtml}</div>
    </div>
  `;
  document.getElementById("modalBackdrop").addEventListener("click", (e) => {
    if (e.target.id === "modalBackdrop") closeModal();
  });
}
function closeModal() {
  document.getElementById("modalRoot").innerHTML = "";
}

function confirmModal(title, message, onConfirm) {
  showModal(`
    <h3 class="font-bold text-lg mb-2">${title}</h3>
    <p class="text-sm mb-5" style="color:var(--text-dim)">${message}</p>
    <div class="flex justify-end gap-2">
      <button class="text-xs px-4 py-2 rounded-lg" style="border:1px solid var(--panel-border)" id="cancelBtn">Cancel</button>
      <button class="gradient-btn text-xs" id="confirmBtn">Confirm</button>
    </div>
  `);
  document.getElementById("cancelBtn").addEventListener("click", closeModal);
  document.getElementById("confirmBtn").addEventListener("click", async () => {
    await onConfirm();
    closeModal();
  });
}

// ---------------------------------------------------------------
// Boot
// ---------------------------------------------------------------
(async function boot() {
  initTheme();
  const session = await guardAuth();
  if (!session) return;
  buildNav();
  navigateTo("overview");
})();
