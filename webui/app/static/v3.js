(() => {
  "use strict";

  const body = document.body;
  if (!body || !body.classList.contains("ui-v3")) return;
  if (body.dataset.v3Enhanced === "true") return;
  body.dataset.v3Enhanced = "true";

  const icons = {
    overview: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 13h6V4H4v9Zm0 7h6v-4H4v4Zm10 0h6v-9h-6v9Zm0-16v4h6V4h-6Z"/></svg>',
    library: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-13Zm3 1.5v3h3V7H7Zm7 0v3h3V7h-3ZM7 14v3h3v-3H7Zm7 0v3h3v-3h-3Z"/></svg>',
    queue: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v3H5V5Zm0 5.5h14v3H5v-3ZM5 16h9v3H5v-3Zm12 0 4 1.5-4 1.5v-3Z"/></svg>',
    automate: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8 14 7l4.6.7-3.3 3.2.8 4.6-4.1-2.2-4.1 2.2.8-4.6-3.3-3.2L10 7l2-4.2Zm-7 14.7h6V20H5v-2.5Zm9 0h5V20h-5v-2.5Z"/></svg>',
    wizard: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15.7 3.3 5 5-12.4 12.4-5-5L15.7 3.3Zm0 3.5-8.9 8.9 1.5 1.5 8.9-8.9-1.5-1.5ZM5 3l.7 1.8L7.5 5.5l-1.8.7L5 8l-.7-1.8-1.8-.7 1.8-.7L5 3Zm3.5 2.5.5 1.2 1.2.5-1.2.5-.5 1.3L8 7.7l-1.2-.5L8 6.7l.5-1.2Z"/></svg>',
    settings: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.6 2h2.8l.6 2.2c.5.2 1 .5 1.5.9l2.2-.7 1.4 2.4-1.7 1.6c.1.5.1 1.1 0 1.6l1.7 1.6-1.4 2.4-2.2-.7c-.5.4-1 .7-1.5.9l-.6 2.2h-2.8l-.6-2.2c-.5-.2-1-.5-1.5-.9l-2.2.7-1.4-2.4L6.6 10a8.2 8.2 0 0 1 0-1.6L4.9 6.8l1.4-2.4 2.2.7c.5-.4 1-.7 1.5-.9l.6-2.2ZM12 7a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.5 4a6.5 6.5 0 1 0 3.9 11.7l4 4 1.4-1.4-4-4A6.5 6.5 0 0 0 10.5 4Zm0 2a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9Z"/></svg>',
    plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5Z"/></svg>',
    activity: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2.2-6.2 4.1 12.4L16 12h5v2h-3.7l-4 9.2L9.1 10.7 8.4 14H3v-2Z"/></svg>',
    alert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 1.8 20h20.4L12 3Zm0 4 6.7 11H5.3L12 7Zm-1 3v4h2v-4h-2Zm0 5.5v2h2v-2h-2Z"/></svg>',
    switch: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10l-2.5-2.5L16 3l5 5-5 5-1.5-1.5L17 9H7V7Zm10 10H7l2.5 2.5L8 21l-5-5 5-5 1.5 1.5L7 15h10v2Z"/></svg>'
  };

  const routes = [
    { href: "/", label: "Overview", icon: "overview", match: path => path === "/" },
    { href: "/library", label: "Library", icon: "library", match: path => path.startsWith("/library") },
    { href: "/jobs", label: "Queue", icon: "queue", match: path => path.startsWith("/jobs") },
    { href: "/autopilot", label: "Automate", icon: "automate", match: path => path.startsWith("/autopilot") },
    { href: "/size_wizard", label: "Size Wizard", icon: "wizard", match: path => path.startsWith("/size_wizard") },
    { href: "/settings", label: "Settings", icon: "settings", match: path => path.startsWith("/settings") }
  ];

  const currentPath = window.location.pathname;
  const currentRoute = routes.find(route => route.match(currentPath)) || routes[0];
  const release = body.dataset.release || "3.15.4";

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[character]);
  }

  function decorateNavigation() {
    const nav = document.querySelector(".app-nav");
    if (!nav) return;
    nav.classList.add("v3-sidebar");

    const brand = nav.querySelector(".app-brand");
    if (brand) {
      brand.setAttribute("aria-label", "ByteSqueeze overview");
      brand.innerHTML = '<span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="brand-copy"><strong>ByteSqueeze</strong><span>Media operations</span></span>';
    }

    const navLinks = nav.querySelector(".nav-links");
    nav.querySelector(".v3-nav-section")?.remove();
    navLinks?.querySelectorAll(".v3-nav-group").forEach(label => label.remove());

    nav.querySelectorAll(".nav-link").forEach(link => {
      const href = new URL(link.href, window.location.origin).pathname;
      const route = routes.find(item => item.href === href);
      if (!route) return;
      link.classList.remove("nav-library", "nav-jobs", "nav-wizard", "nav-settings");
      link.innerHTML = `<span class="v3-nav-icon">${icons[route.icon]}</span><span class="v3-nav-label">${route.label}</span>`;
      link.title = route.label;
      link.setAttribute("aria-label", route.label);
      link.classList.toggle("nav-link-active", route.match(currentPath));
      if (route.match(currentPath)) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });

    if (navLinks) {
      [
        ["Operate", "/"],
        ["Optimize", "/autopilot"],
        ["System", "/settings"]
      ].forEach(([label, href]) => {
        const target = Array.from(navLinks.querySelectorAll(".nav-link")).find(link => new URL(link.href, window.location.origin).pathname === href);
        if (!target) return;
        const group = document.createElement("span");
        group.className = "v3-nav-group";
        group.textContent = label;
        navLinks.insertBefore(group, target);
      });
    }

    nav.querySelector(".v3-sidebar-footer")?.remove();
    const footer = document.createElement("div");
    footer.className = "v3-sidebar-footer";
    footer.innerHTML = `<span class="v3-release-chip">V3</span><span class="v3-sidebar-release">${release}</span>`;
    nav.appendChild(footer);
  }

  function injectTopbar() {
    const main = document.querySelector("main");
    if (!main) return;
    document.querySelector(".v3-topbar")?.remove();
    const topbar = document.createElement("header");
    topbar.className = "v3-topbar";
    const isLibrary = currentPath.startsWith("/library");
    const isQueue = currentPath.startsWith("/jobs");
    const primaryHref = isLibrary ? "#libraryControls" : isQueue ? "#v3QueueComposer" : "/library#libraryControls";
    const primaryLabel = isLibrary ? "Library controls" : isQueue ? "Add media" : "Queue media";
    topbar.innerHTML = `
      <div class="v3-topbar-title"><span class="v3-topbar-wordmark">ByteSqueeze</span><span class="v3-topbar-divider" aria-hidden="true">/</span><strong>${currentRoute.label}</strong></div>
      <div class="v3-topbar-actions">
        <button id="v3SystemChip" class="v3-system-chip is-loading" type="button" aria-controls="v3OperationsDock" aria-expanded="false"><i></i><span>Checking</span></button>
        <button class="v3-command-trigger" type="button" aria-label="Open command center">
          <span class="v3-command-icon">${icons.search}</span><span>Search and run a command</span><kbd>/</kbd>
        </button>
        <a class="v3-quick-action" href="${primaryHref}"><span>${icons.plus}</span>${primaryLabel}</a>
      </div>`;
    main.parentNode.insertBefore(topbar, main);
    topbar.querySelector(".v3-command-trigger").addEventListener("click", openCommandCenter);
    topbar.querySelector("#v3SystemChip").addEventListener("click", toggleOperationsDetail);
  }

  function formatBytes(value) {
    let size = Number(value || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size >= 100 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
  }

  function fileName(path) {
    return String(path || "").split(/[\\/]/).pop() || "Encode job";
  }

  function injectOperationsDock() {
    document.querySelector(".v3-operations-dock")?.remove();
    const dock = document.createElement("aside");
    dock.id = "v3OperationsDock";
    dock.className = "v3-operations-dock is-loading";
    dock.setAttribute("aria-label", "Encoding operations status");
    dock.hidden = true;
    dock.innerHTML = `
      <a class="v3-ops-primary" href="/jobs">
        <span class="v3-ops-signal">${icons.activity}</span>
        <span class="v3-ops-copy"><strong id="v3OpsState">Checking operations</strong><small id="v3OpsCurrent">Loading queue and worker status…</small><small id="v3OpsDetail"></small></span>
        <b id="v3OpsPercent" class="v3-ops-percent">—</b>
        <span class="v3-ops-progress" aria-hidden="true"><i id="v3OpsProgress"></i></span>
      </a>
      <div class="v3-ops-metrics">
        <a href="/jobs?status=running"><small>Running</small><strong id="v3OpsRunning">—</strong></a>
        <a href="/jobs?status=queued"><small>Queued</small><strong id="v3OpsQueued">—</strong></a>
        <a href="/settings/nodes"><small>Workers</small><strong id="v3OpsWorkers">—</strong></a>
        <a href="/settings"><small>Recovered</small><strong id="v3OpsSaved">—</strong></a>
      </div>
      <a id="v3OpsAlert" class="v3-ops-alert" href="/jobs?status=error" aria-label="No queue errors">${icons.alert}<span>0</span></a>
      <button class="v3-ops-close" type="button" aria-label="Close operations details">×</button>`;
    document.body.appendChild(dock);
    dock.querySelector(".v3-ops-close").addEventListener("click", () => setOperationsDetail(false));
    refreshOperationsDock();
    window.setInterval(refreshOperationsDock, 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshOperationsDock(); });
  }

  let operationsRequestActive = false;

  function setOperationsDetail(open) {
    const dock = document.getElementById("v3OperationsDock");
    const chip = document.getElementById("v3SystemChip");
    if (!dock || !chip) return;
    dock.dataset.userOpen = open ? "true" : "false";
    const contextual = dock.classList.contains("is-running") || dock.classList.contains("is-warning");
    dock.hidden = !open && !contextual;
    dock.classList.toggle("is-expanded", open);
    chip.setAttribute("aria-expanded", open ? "true" : "false");
    document.body.classList.toggle("v3-has-ops-preview", open && !contextual);
  }

  function toggleOperationsDetail() {
    const dock = document.getElementById("v3OperationsDock");
    setOperationsDetail(dock?.dataset.userOpen !== "true");
  }

  function jobRate(job) {
    const value = Number(job?.fps ?? job?.current_fps ?? job?.encode_fps ?? 0);
    return value > 0 ? `${value.toFixed(value >= 100 ? 0 : 1)} FPS` : "";
  }

  function jobEta(job) {
    const seconds = Number(job?.eta_seconds ?? job?.eta ?? 0);
    if (!(seconds > 0)) return "";
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.max(1, Math.round((seconds % 3600) / 60));
    return hours ? `${hours}h ${minutes}m remaining` : `${minutes}m remaining`;
  }

  async function refreshOperationsDock() {
    if (operationsRequestActive || document.hidden) return;
    operationsRequestActive = true;
    const dock = document.querySelector(".v3-operations-dock");
    if (!dock) {
      operationsRequestActive = false;
      return;
    }
    try {
      const response = await fetch("/api/home/summary", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Operations unavailable");
      const summary = data.queue?.summary || {};
      const counts = summary.counts || {};
      const active = Array.isArray(data.queue?.active) ? data.queue.active : [];
      const running = active.filter(job => String(job.status).toLowerCase() === "running");
      const runningCount = Number(counts.running || running.length || 0);
      const queuedCount = Number(counts.queued ?? summary.queued_count ?? 0);
      const errors = Number(counts.error || summary.active_error_count || 0);
      const progressValues = running.map(job => Number(job.progress || 0)).filter(Number.isFinite);
      const progress = progressValues.length ? progressValues.reduce((total, value) => total + value, 0) / progressValues.length : 0;
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      const workersOnline = nodes.filter(node => node.online).length;
      const current = running[0];
      const paused = Boolean(data.queue?.paused || summary.queue_paused);
      const showContext = Boolean(errors || runningCount);
      const chip = document.getElementById("v3SystemChip");

      dock.classList.remove("is-loading", "is-idle", "is-running", "is-warning");
      dock.classList.add(errors ? "is-warning" : runningCount ? "is-running" : "is-idle");
      document.body.classList.toggle("v3-has-active-ops", showContext);
      dock.hidden = !showContext && dock.dataset.userOpen !== "true";
      if (chip) {
        chip.className = `v3-system-chip ${errors ? "is-warning" : runningCount ? "is-running" : "is-ready"}`;
        chip.querySelector("span").textContent = errors
          ? `${errors} error${errors === 1 ? "" : "s"}`
          : runningCount ? `${runningCount} encoding`
          : nodes.length ? `${workersOnline}/${nodes.length} workers` : "Ready";
      }
      document.getElementById("v3OpsState").textContent = errors ? "Queue needs attention" : paused ? "Queue paused" : runningCount ? fileName(current?.src) : "System ready";
      document.getElementById("v3OpsCurrent").textContent = current
        ? [current.node_name || current.worker_name || current.encoder || "Local encoder", jobRate(current), jobEta(current)].filter(Boolean).join(" · ")
        : queuedCount ? `${queuedCount} waiting to start` : "No active transcodes";
      document.getElementById("v3OpsDetail").textContent = current && runningCount > 1 ? `+ ${runningCount - 1} other active job${runningCount === 2 ? "" : "s"}` : "";
      document.getElementById("v3OpsRunning").textContent = String(runningCount);
      document.getElementById("v3OpsQueued").textContent = String(queuedCount);
      document.getElementById("v3OpsWorkers").textContent = nodes.length ? `${workersOnline}/${nodes.length}` : "Local";
      document.getElementById("v3OpsSaved").textContent = formatBytes(data.storage?.saved_bytes || data.storage?.total_saved_bytes || 0);
      document.getElementById("v3OpsProgress").style.width = `${Math.max(0, Math.min(100, progress))}%`;
      document.getElementById("v3OpsPercent").textContent = runningCount ? `${Math.round(progress)}%` : "—";
      const alert = document.getElementById("v3OpsAlert");
      alert.querySelector("span").textContent = String(errors);
      alert.classList.toggle("has-errors", errors > 0);
      alert.setAttribute("aria-label", errors ? `${errors} queue error${errors === 1 ? "" : "s"}` : "No queue errors");
    } catch (error) {
      dock.classList.remove("is-loading", "is-idle", "is-running");
      dock.classList.add("is-warning");
      dock.hidden = false;
      document.body.classList.add("v3-has-active-ops");
      const chip = document.getElementById("v3SystemChip");
      if (chip) { chip.className = "v3-system-chip is-warning"; chip.querySelector("span").textContent = "Status error"; }
      const state = document.getElementById("v3OpsState");
      const current = document.getElementById("v3OpsCurrent");
      if (state) state.textContent = "Status unavailable";
      if (current) current.textContent = "Open Queue to inspect the connection";
    } finally {
      operationsRequestActive = false;
    }
  }

  function enhanceJobsPage() {
    if (!currentPath.startsWith("/jobs")) return;
    const main = document.querySelector("main.jobs-shell");
    const browserCard = main?.querySelector(".browser-card");
    const actionGrid = main?.querySelector(".jobs-action-grid");
    const batchCard = main?.querySelector(".batch-tools-card");
    if (!main || !browserCard || !actionGrid || !batchCard || main.querySelector(".v3-queue-compose")) return;

    const composer = document.createElement("details");
    composer.id = "v3QueueComposer";
    composer.className = "v3-queue-compose";
    composer.innerHTML = `<summary><span class="v3-compose-icon">${icons.plus}</span><span><strong>Add media to the queue</strong><small>Browse a file, use Smart Preset, or queue a complete folder.</small></span><b>Open</b></summary><div class="v3-queue-compose-body"></div>`;
    browserCard.parentNode.insertBefore(composer, browserCard);
    const composerBody = composer.querySelector(".v3-queue-compose-body");
    composerBody.append(browserCard, actionGrid, batchCard);

    const overview = document.getElementById("summaryQueued")?.closest("section");
    const running = document.getElementById("v3RunningSection");
    const history = main.querySelector(".jobs-history-card");
    const nodes = document.getElementById("linkedNodesJobsPanel")?.closest("section");
    overview?.classList.add("v3-queue-overview");
    nodes?.classList.add("v3-worker-overview");
    if (running) main.insertBefore(running, main.querySelector(".page-hero")?.nextSibling || main.firstChild);
    if (overview && running) running.insertAdjacentElement("afterend", overview);
    else if (overview) main.insertBefore(overview, main.querySelector(".page-hero")?.nextSibling || main.firstChild);
    if (history && running) running.insertAdjacentElement("afterend", history);
    if (history) history.insertAdjacentElement("afterend", composer);
    if (nodes) composer.insertAdjacentElement("afterend", nodes);

    const statusSelect = document.getElementById("jobStatusFilter");
    const filterBar = statusSelect?.closest(".jobs-filter-bar");
    if (statusSelect && filterBar && !filterBar.querySelector(".v3-job-tabs")) {
      const tabs = document.createElement("div");
      tabs.className = "v3-job-tabs";
      tabs.setAttribute("role", "tablist");
      [["all", "All"], ["running", "Running"], ["queued", "Queued"], ["done", "Completed"], ["error", "Errors"]].forEach(([value, label]) => {
        const tab = document.createElement("button");
        tab.type = "button";
        tab.dataset.status = value;
        tab.textContent = label;
        tab.className = value === statusSelect.value ? "is-active" : "";
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-controls", "jobsTablePane");
        tab.setAttribute("aria-selected", value === statusSelect.value ? "true" : "false");
        tab.addEventListener("click", () => {
          statusSelect.value = value;
          statusSelect.dispatchEvent(new Event("change", { bubbles: true }));
          tabs.querySelectorAll("button").forEach(button => {
            button.classList.toggle("is-active", button === tab);
            button.setAttribute("aria-selected", button === tab ? "true" : "false");
          });
        });
        tabs.appendChild(tab);
      });
      filterBar.prepend(tabs);
      statusSelect.addEventListener("change", () => tabs.querySelectorAll("button").forEach(button => {
        const active = button.dataset.status === statusSelect.value;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      }));
    }

    const openComposerForHash = () => {
      if (window.location.hash === "#v3QueueComposer") {
        composer.open = true;
        window.setTimeout(() => composer.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
      }
    };
    window.addEventListener("hashchange", openComposerForHash);
    openComposerForHash();
  }

  function enhanceSettingsPage() {
    if (!currentPath.startsWith("/settings")) return;
    const shell = document.querySelector("main.settings-shell");
    const grid = shell?.querySelector(".settings-grid");
    const nav = shell?.querySelector(".settings-section-nav");
    if (!shell || !grid || !nav || shell.querySelector(".v3-settings-search")) return;

    const visibleCards = Array.from(grid.querySelectorAll(":scope > .section-card")).filter(card => getComputedStyle(card).display !== "none");
    const search = document.createElement("div");
    search.className = "v3-settings-search";
    search.innerHTML = `<span>${icons.search}</span><input type="search" placeholder="Find a setting on this page" aria-label="Find a setting"><small>${visibleCards.length} sections</small>`;
    nav.insertAdjacentElement("afterend", search);
    const input = search.querySelector("input");
    const count = search.querySelector("small");
    input.addEventListener("input", () => {
      const query = input.value.trim().toLowerCase();
      let matches = 0;
      visibleCards.forEach(card => {
        const match = !query || card.textContent.toLowerCase().includes(query);
        card.classList.toggle("v3-settings-filtered", !match);
        if (match) matches += 1;
      });
      count.textContent = `${matches} section${matches === 1 ? "" : "s"}`;
    });

    grid.addEventListener("change", event => {
      const card = event.target.closest(".section-card");
      if (!card || !visibleCards.includes(card)) return;
      card.classList.add("v3-settings-modified");
      if (!card.querySelector(":scope > .v3-modified-pill")) {
        const pill = document.createElement("span");
        pill.className = "v3-modified-pill";
        pill.textContent = "Modified";
        card.appendChild(pill);
      }
    });
  }

  function enhanceLibraryPage() {
    if (!currentPath.startsWith("/library")) return;
    const controls = document.getElementById("libraryControls");
    const filterSelect = document.getElementById("quickFilterSelect");
    const filterBar = controls?.querySelector(".library-filterbar");
    if (!controls || !filterSelect || !filterBar || controls.querySelector(".v3-library-filters")) return;
    const chips = document.createElement("div");
    chips.className = "v3-library-filters v3-only";
    [["all", "All"], ["savings", "Largest savings"], ["hdr", "HDR"], ["tracked", "Tracked shows"], ["artwork", "Missing artwork"], ["learning", "Needs learning"]].forEach(([value, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.filter = value;
      button.textContent = label;
      button.className = filterSelect.value === value ? "is-active" : "";
      button.addEventListener("click", () => {
        filterSelect.value = value;
        filterSelect.dispatchEvent(new Event("change", { bubbles: true }));
        chips.querySelectorAll("button").forEach(item => item.classList.toggle("is-active", item === button));
      });
      chips.appendChild(button);
    });
    filterBar.insertAdjacentElement("afterend", chips);
    filterSelect.addEventListener("change", () => chips.querySelectorAll("button").forEach(button => button.classList.toggle("is-active", button.dataset.filter === filterSelect.value)));
  }

  function enhanceAutopilotPage() {
    if (!currentPath.startsWith("/autopilot")) return;
    ["training", "policy", "feedback", "documentation"].forEach((id, index) => {
      const section = document.getElementById(id);
      const head = section?.querySelector(":scope > .auto-card-head");
      const body = section?.querySelector(":scope > .auto-card-body");
      if (!section || !head || !body || head.querySelector(".v3-section-toggle")) return;
      section.classList.add("v3-collapsible");
      section.classList.toggle("is-collapsed", index > 0);
      const toggle = document.createElement("button");
      toggle.className = "v3-section-toggle";
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", index === 0 ? "true" : "false");
      toggle.setAttribute("aria-controls", `${id}Body`);
      toggle.innerHTML = "<span>Toggle section</span><b>⌄</b>";
      body.id = `${id}Body`;
      toggle.addEventListener("click", () => {
        const collapsed = section.classList.toggle("is-collapsed");
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
      head.appendChild(toggle);
    });
    document.addEventListener("bytesqueeze:autopilot-stage", event => {
      const current = event.detail?.stage;
      ["training", "policy"].forEach(id => {
        const section = document.getElementById(id);
        const toggle = section?.querySelector(".v3-section-toggle");
        if (!section || !toggle) return;
        const collapsed = id !== current;
        section.classList.toggle("is-collapsed", collapsed);
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
    });
  }

  function enhanceWizardPage() {
    if (!currentPath.startsWith("/size_wizard")) return;
    const metrics = document.querySelector(".metric-list");
    const source = document.getElementById("metricSourceSize");
    const estimate = document.getElementById("metricEstimatedSize");
    if (!metrics || !source || !estimate || document.querySelector(".v3-size-impact")) return;
    const impact = document.createElement("section");
    impact.className = "v3-size-impact";
    impact.innerHTML = '<div><small>Current</small><strong id="v3ImpactSource">—</strong></div><span>→</span><div><small>Estimated</small><strong id="v3ImpactEstimate">—</strong></div><b id="v3ImpactSaving">Select a source</b>';
    metrics.insertAdjacentElement("beforebegin", impact);
    const update = () => {
      document.getElementById("v3ImpactSource").textContent = source.textContent || "—";
      document.getElementById("v3ImpactEstimate").textContent = estimate.textContent || "—";
      const parse = value => {
        const match = String(value || "").match(/([\d.]+)\s*(TB|GB|MB|KB|B)/i);
        if (!match) return 0;
        const scale = { B: 1, KB: 1024, MB: 1048576, GB: 1073741824, TB: 1099511627776 };
        return Number(match[1]) * scale[match[2].toUpperCase()];
      };
      const before = parse(source.textContent);
      const after = parse(estimate.textContent);
      const savings = before > 0 && after > 0 ? Math.max(0, Math.round((1 - after / before) * 100)) : 0;
      document.getElementById("v3ImpactSaving").textContent = savings ? `Save about ${savings}%` : "Live source-aware estimate";
    };
    const observer = new MutationObserver(update);
    observer.observe(source, { childList: true, characterData: true, subtree: true });
    observer.observe(estimate, { childList: true, characterData: true, subtree: true });
    update();
  }

  const commands = [
    { label: "Open Overview", detail: "System health and recent work", href: "/", icon: "overview" },
    { label: "Browse Library", detail: "Find movies and complete seasons", href: "/library", icon: "library" },
    { label: "Open Queue", detail: "Watch, pause, or reorder encodes", href: "/jobs", icon: "queue" },
    { label: "Open Automate", detail: "Train and manage Autopilot", href: "/autopilot", icon: "automate" },
    { label: "Open Size Wizard", detail: "Fine-tune a one-off plan", href: "/size_wizard", icon: "wizard" },
    { label: "Manage Smart Presets", detail: "Preservation and language safeguards", href: "/settings/smart", icon: "settings" },
    { label: "Open Settings", detail: "Interface, workers, storage, and AI", href: "/settings", icon: "settings" },
    { label: "Switch to V2 Classic", detail: "Use the preserved classic interface", href: "/settings?ui=v2", icon: "switch" }
  ];

  let commandDialog;
  let commandInput;
  let commandList;

  function createCommandCenter() {
    commandDialog = document.createElement("dialog");
    commandDialog.className = "v3-command-dialog";
    commandDialog.setAttribute("aria-label", "Command center");
    commandDialog.innerHTML = `
      <div class="v3-command-head"><span>${icons.search}</span><input class="v3-command-input" type="search" autocomplete="off" placeholder="Where do you want to go?" aria-label="Search commands"><button class="v3-command-close" type="button" aria-label="Close command center">Esc</button></div>
      <div class="v3-command-results" role="listbox"></div>
      <div class="v3-command-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move · <kbd>Enter</kbd> open · Type anything to search the library</span></div>`;
    document.body.appendChild(commandDialog);
    commandInput = commandDialog.querySelector("input");
    commandList = commandDialog.querySelector(".v3-command-results");
    renderCommands("");

    commandInput.addEventListener("input", () => renderCommands(commandInput.value));
    commandInput.addEventListener("keydown", handleCommandKeys);
    commandDialog.querySelector(".v3-command-close").addEventListener("click", () => commandDialog.close());
    commandDialog.addEventListener("click", event => {
      if (event.target === commandDialog) commandDialog.close();
    });
    commandDialog.addEventListener("close", () => { commandInput.value = ""; renderCommands(""); });
  }

  function renderCommands(query) {
    const normalized = query.trim().toLowerCase();
    const visible = commands.filter(command => `${command.label} ${command.detail}`.toLowerCase().includes(normalized));
    commandList.replaceChildren();
    visible.forEach((command, index) => {
      const link = document.createElement("a");
      link.className = `v3-command-item${index === 0 ? " is-selected" : ""}`;
      link.href = command.href;
      link.setAttribute("role", "option");
      link.innerHTML = `<span class="v3-command-item-icon">${icons[command.icon]}</span><span><strong>${command.label}</strong><small>${command.detail}</small></span><span class="v3-command-arrow">→</span>`;
      link.addEventListener("mouseenter", () => selectCommand(link));
      commandList.appendChild(link);
    });
    if (!visible.length && normalized) {
      const link = document.createElement("a");
      link.className = "v3-command-item is-selected";
      link.href = `/library?search=${encodeURIComponent(query.trim())}`;
      link.innerHTML = `<span class="v3-command-item-icon">${icons.search}</span><span><strong>Search the library</strong><small>Find “${escapeHtml(query.trim())}” in movies and shows</small></span><span class="v3-command-arrow">→</span>`;
      commandList.appendChild(link);
    }
  }

  function selectCommand(item) {
    commandList.querySelectorAll(".v3-command-item").forEach(node => node.classList.toggle("is-selected", node === item));
  }

  function handleCommandKeys(event) {
    const items = Array.from(commandList.querySelectorAll(".v3-command-item"));
    if (!items.length) return;
    const current = Math.max(0, items.findIndex(item => item.classList.contains("is-selected")));
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      selectCommand(items[(current + step + items.length) % items.length]);
    } else if (event.key === "Enter") {
      event.preventDefault();
      items[current].click();
    }
  }

  function openCommandCenter() {
    if (!commandDialog) createCommandCenter();
    if (typeof commandDialog.showModal === "function") commandDialog.showModal();
    else commandDialog.setAttribute("open", "");
    window.setTimeout(() => commandInput.focus(), 0);
  }

  function addKeyboardShortcuts() {
    document.addEventListener("keydown", event => {
      const target = event.target;
      const isTyping = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        openCommandCenter();
      } else if (event.key === "/" && !isTyping && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        openCommandCenter();
      }
    });
  }

  function applyLibrarySearch() {
    if (!currentPath.startsWith("/library")) return;
    const query = new URLSearchParams(window.location.search).get("search");
    const input = document.getElementById("searchInput");
    if (!query || !input) return;
    input.value = query;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function addWizardProgress() {
    const heading = document.querySelector(".wizard-heading");
    if (!heading) return;
    const progress = document.createElement("ol");
    progress.className = "v3-workflow-steps";
    progress.setAttribute("aria-label", "Encoding workflow");
    ["Source", "Intent", "Preview", "Queue"].forEach((label, index) => {
      const step = document.createElement("li");
      step.className = "v3-workflow-step";
      step.innerHTML = `<span>${index + 1}</span><strong>${label}</strong>`;
      progress.appendChild(step);
    });
    heading.insertAdjacentElement("afterend", progress);

    const update = () => {
      const steps = Array.from(progress.children);
      const source = document.getElementById("src")?.value || "";
      const previewText = document.getElementById("previewBox")?.textContent || "";
      const compareVisible = document.getElementById("compareWrap")?.style.display !== "none";
      const queueText = `${document.getElementById("queueStatus")?.textContent || ""} ${document.getElementById("simpleQueueStatus")?.textContent || ""}`;
      const sourceReady = Boolean(source);
      const previewReady = sourceReady && !/waiting for a selected file/i.test(previewText);
      const compareReady = previewReady && compareVisible;
      const queueReady = /queued|complete|success/i.test(queueText);
      const complete = [sourceReady, previewReady, compareReady, queueReady];
      const active = queueReady ? 3 : compareReady ? 3 : previewReady ? 2 : sourceReady ? 1 : 0;
      steps.forEach((step, index) => {
        step.classList.toggle("is-complete", complete[index] && index < active);
        step.classList.toggle("is-active", index === active);
      });
    };
    const observer = new MutationObserver(update);
    ["fileNameChip", "previewBox", "compareWrap", "queueStatus", "simpleQueueStatus"].forEach(id => {
      const element = document.getElementById(id);
      if (element) observer.observe(element, { attributes: true, childList: true, characterData: true, subtree: true });
    });
    document.getElementById("src")?.addEventListener("change", update);
    update();
  }

  function initializeInterfaceMotion() {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion) {
      body.classList.add("v3-reduced-motion");
      return;
    }

    body.classList.add("v3-motion-ready");
    const seen = new WeakSet();
    const observer = "IntersectionObserver" in window
      ? new IntersectionObserver(entries => {
          entries.forEach(entry => {
            if (!entry.isIntersecting) return;
            entry.target.classList.add("is-visible");
            observer.unobserve(entry.target);
          });
        }, { rootMargin: "0px 0px -5%", threshold: 0.04 })
      : null;

    const register = (element, delay = 0) => {
      if (!(element instanceof HTMLElement) || seen.has(element)) return;
      seen.add(element);
      element.classList.add("v3-reveal");
      element.style.setProperty("--v3-reveal-delay", `${Math.min(delay, 220)}ms`);
      window.requestAnimationFrame(() => {
        const bounds = element.getBoundingClientRect();
        if (!observer || bounds.top < window.innerHeight * 1.04) element.classList.add("is-visible");
        else observer.observe(element);
      });
    };

    const sectionSelectors = [
      "main > .page-hero", "main > .home-hero", "main > .auto-hero", "main > .wizard-heading",
      "main > .home-metrics", "main > .home-grid", "main > .library-control-card",
      "main > .library-view-tabs", "main > .beta-layout", "main > .library-release-section",
      "main > .v3-running-section", "main > .v3-queue-overview", "main > .jobs-history-card",
      "main > .v3-queue-compose", "main > .v3-worker-overview", "main > .auto-card",
      "main > .auto-layout", "main > .wizard-ai-setup-card", "main > .wizard-mode-card",
      "main > .smart-preset-card", "main > .wizard-layout", "main > .settings-section-nav",
      "main > .v3-settings-search", "main > .settings-grid"
    ];
    document.querySelectorAll(sectionSelectors.join(",")).forEach((element, index) => register(element, index * 32));

    const dynamicSelector = ".beta-card, .v3-running-card, .node-status-box, .linked-node-job-row, .home-job, .home-event";
    const registerDynamic = root => {
      const elements = [];
      if (root instanceof HTMLElement && root.matches(dynamicSelector)) elements.push(root);
      if (root instanceof Element) elements.push(...root.querySelectorAll(dynamicSelector));
      elements.forEach((element, index) => register(element, (index % 8) * 24));
    };
    registerDynamic(document.querySelector("main"));

    const main = document.querySelector("main");
    if (main) {
      new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(registerDynamic)))
        .observe(main, { childList: true, subtree: true });
    }
  }

  decorateNavigation();
  injectTopbar();
  injectOperationsDock();
  enhanceJobsPage();
  enhanceLibraryPage();
  enhanceAutopilotPage();
  enhanceSettingsPage();
  addKeyboardShortcuts();
  applyLibrarySearch();
  addWizardProgress();
  enhanceWizardPage();
  initializeInterfaceMotion();
  document.title = `ByteSqueeze · ${currentRoute.label}`;
  body.classList.add("v3-ready");
})();
