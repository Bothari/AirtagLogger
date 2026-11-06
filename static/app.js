// ── State ────────────────────────────────────────────────
let devices = [];
let selectedDevices = new Set();
let activeTimeRange = 24; // hours, 0 = all
let mapLayers = { markers: [], trails: [] };
let clusterGroup = null;
let latestPositions = {}; // deviceId -> {lat, lng}
let locationCache = {}; // deviceId -> [{latitude, longitude, timestamp, _ts}, ...]
let scrubberMin = 0;
let scrubberMax = 0;
let map;
let me = null;          // the signed-in user (from /api/auth/me)
let geofences = [];     // this user's places
let geofenceLayer = null;
let pickingGeofence = false;
let geofenceShape = "circle";   // shape selected in the Place form
let draftPolygon = null;        // [[lat, lon], ...] for the Place form's area
let drawing = null;             // active area-drawing session on the map

// ── Init ─────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
    initMap();
    initNav();
    initConfigPanel();
    initTimeRange();
    initSettingsTabs();
    initGeneralSettings();
    initExportTab();
    initScrubber();
    initAddDeviceModal();
    initAuth();
    initLogin();
    initAccountTab();
    initAlertsTab();
    initUsersTab();
    initAdminSettings();
    await boot();
});

// ── Map ──────────────────────────────────────────────────
function initMap() {
    map = L.map("map", { zoomControl: true }).setView([40, -95], 4);
    // CARTO basemaps now require an API key; Esri Dark Gray Canvas is keyless
    const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
    const tileOpts = { maxNativeZoom: 16, maxZoom: 19 };
    L.tileLayer(`${esri}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, {
        ...tileOpts,
        attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
    }).addTo(map);
    L.tileLayer(`${esri}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, tileOpts).addTo(map);
    map.on("click", onMapPick);
}

// ── Navigation ───────────────────────────────────────────
function initNav() {
    const navItems = document.querySelectorAll(".nav-item");
    navItems.forEach(btn => {
        btn.addEventListener("click", () => {
            const pageId = btn.dataset.page;
            document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
            document.getElementById(pageId).classList.add("active");
            navItems.forEach(n => n.classList.remove("active"));
            btn.classList.add("active");
            if (pageId === "page-map") {
                map.invalidateSize();
            }
        });
    });

    // "Go add" button on map empty state
    document.getElementById("btn-go-add").addEventListener("click", () => {
        navigateTo("page-devices");
        openAddModal();
    });
}

function navigateTo(pageId) {
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    document.getElementById(pageId).classList.add("active");
    document.querySelectorAll(".nav-item").forEach(n => {
        n.classList.toggle("active", n.dataset.page === pageId);
    });
    if (pageId === "page-map") map.invalidateSize();
}

// ── Config Panel (mobile collapse) ───────────────────────
function initConfigPanel() {
    const header = document.getElementById("config-header");
    const panel = document.getElementById("config-panel");
    const toggle = document.getElementById("config-toggle");

    header.addEventListener("click", () => {
        if (window.innerWidth > 768) return;
        const isOpen = panel.classList.toggle("open");
        toggle.classList.toggle("open", isOpen);
    });
}

function updateConfigSummary() {
    const el = document.getElementById("config-summary");
    const count = devices.length;
    el.textContent = count === 0 ? "No devices" : `${count} device${count !== 1 ? "s" : ""} tracked`;
}

// ── Time Range ───────────────────────────────────────────
function initTimeRange() {
    const group = document.getElementById("time-range-group");
    group.addEventListener("click", (e) => {
        const btn = e.target.closest(".time-btn");
        if (!btn) return;
        group.querySelectorAll(".time-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        activeTimeRange = parseInt(btn.dataset.range, 10);
        refreshMap();
    });
}

// ── Settings Tabs ────────────────────────────────────────
// Each page (Devices, Settings) has its own tab bar; switch within that page only.
function initSettingsTabs() {
    document.querySelectorAll(".tab-bar").forEach(bar => {
        const page = bar.closest(".page");
        const tabs = bar.querySelectorAll(".tab-btn");
        tabs.forEach(btn => {
            btn.addEventListener("click", () => {
                tabs.forEach(t => t.classList.remove("active"));
                btn.classList.add("active");
                page.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
                document.getElementById(btn.dataset.tab).classList.add("active");
            });
        });
    });
}

// ── General Settings ─────────────────────────────────────
function initGeneralSettings() {
    const pollInput = document.getElementById("poll-interval");
    const pollBtn = document.getElementById("btn-poll-now");

    // Save poll interval on change
    let saveTimeout;
    pollInput.addEventListener("input", () => {
        clearTimeout(saveTimeout);
        saveTimeout = setTimeout(async () => {
            const val = parseInt(pollInput.value, 10);
            if (!val || val < 1) return;
            try {
                await api("PUT", "/api/settings", { poll_interval: val });
                toast(`Poll interval set to ${val} min`, "success");
            } catch (e) {
                toast("Failed to save interval", "error");
            }
        }, 800);
    });

    // Save retention days on change
    const retentionInput = document.getElementById("retention-days");
    let retentionTimeout;
    retentionInput.addEventListener("input", () => {
        clearTimeout(retentionTimeout);
        retentionTimeout = setTimeout(async () => {
            const val = parseInt(retentionInput.value, 10);
            if (isNaN(val) || val < 0) return;
            try {
                await api("PUT", "/api/settings", { retention_days: val });
                toast(val === 0 ? "Data retention: keep forever" : `Data retention set to ${val} days`, "success");
            } catch (e) {
                toast("Failed to save retention", "error");
            }
        }, 800);
    });

    // Poll now button
    pollBtn.addEventListener("click", async () => {
        pollBtn.disabled = true;
        pollBtn.textContent = "Polling...";
        try {
            const result = await api("POST", "/api/poll");
            if (result.error) throw new Error(result.error);
            toast(`Poll complete: ${result.fetched} location(s) fetched`, "success");
            await loadDevices();
        } catch (e) {
            toast("Poll failed: " + e.message, "error");
        } finally {
            pollBtn.disabled = false;
            pollBtn.textContent = "Poll Now";
        }
    });
}

async function loadSettings() {
    try {
        const settings = await fetchJSON("/api/settings");
        if (settings.poll_interval) {
            document.getElementById("poll-interval").value = settings.poll_interval;
        }
        if (settings.retention_days !== undefined) {
            document.getElementById("retention-days").value = settings.retention_days;
        }
    } catch {}
}

// ── Export Tab ───────────────────────────────────────────
let exportSelectedDevices = new Set();
let exportTimeRange = 720; // hours, 0 = all

function initExportTab() {
    // Time range presets
    const presets = document.querySelector(".export-time-presets");
    presets.addEventListener("click", (e) => {
        const btn = e.target.closest(".time-btn");
        if (!btn) return;
        presets.querySelectorAll(".time-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        exportTimeRange = parseInt(btn.dataset.exportRange, 10);
        updateExportDates();
    });

    // Select all / none
    document.getElementById("export-select-all").addEventListener("click", () => {
        devices.forEach(d => exportSelectedDevices.add(d.id));
        renderExportDevices();
    });
    document.getElementById("export-select-none").addEventListener("click", () => {
        exportSelectedDevices.clear();
        renderExportDevices();
    });

    // Custom date inputs clear the preset
    document.getElementById("export-from").addEventListener("input", () => {
        document.querySelector(".export-time-presets").querySelectorAll(".time-btn").forEach(b => b.classList.remove("active"));
    });
    document.getElementById("export-to").addEventListener("input", () => {
        document.querySelector(".export-time-presets").querySelectorAll(".time-btn").forEach(b => b.classList.remove("active"));
    });

    // Export button
    document.getElementById("btn-export").addEventListener("click", doExport);

    // Set initial dates
    updateExportDates();
}

function updateExportDates() {
    const fromInput = document.getElementById("export-from");
    const toInput = document.getElementById("export-to");
    const now = new Date();
    toInput.value = toLocalDatetimeStr(now);
    if (exportTimeRange > 0) {
        fromInput.value = toLocalDatetimeStr(new Date(now.getTime() - exportTimeRange * 3600 * 1000));
    } else {
        fromInput.value = "";
    }
}

function toLocalDatetimeStr(date) {
    // Format as YYYY-MM-DDTHH:MM for datetime-local input
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    const h = String(date.getHours()).padStart(2, "0");
    const min = String(date.getMinutes()).padStart(2, "0");
    return `${y}-${m}-${d}T${h}:${min}`;
}

function renderExportDevices() {
    const container = document.getElementById("export-device-list");
    container.innerHTML = "";

    devices.forEach(d => {
        const item = document.createElement("label");
        item.className = "export-device-item";

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = exportSelectedDevices.has(d.id);
        cb.addEventListener("change", () => {
            if (cb.checked) exportSelectedDevices.add(d.id);
            else exportSelectedDevices.delete(d.id);
        });

        const dot = document.createElement("span");
        dot.className = "device-dot";
        dot.style.background = d.color;

        const name = document.createElement("span");
        name.className = "device-filter-name";
        name.textContent = d.name;

        item.append(cb, dot, name);
        container.appendChild(item);
    });
}

async function doExport() {
    if (exportSelectedDevices.size === 0) {
        toast("Select at least one device", "error");
        return;
    }

    const format = document.querySelector('input[name="export-format"]:checked').value;
    const fromVal = document.getElementById("export-from").value;
    const toVal = document.getElementById("export-to").value;

    const params = new URLSearchParams();
    params.set("devices", Array.from(exportSelectedDevices).join(","));
    params.set("format", format);
    if (fromVal) params.set("from_ts", new Date(fromVal).toISOString());
    if (toVal) params.set("to_ts", new Date(toVal).toISOString());

    const btn = document.getElementById("btn-export");
    btn.disabled = true;
    btn.textContent = "Exporting...";

    try {
        const res = await fetch(`/api/export?${params}`);
        if (res.status === 401) { showLogin(); return; }
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        // Trigger download
        const blob = await res.blob();
        const disposition = res.headers.get("content-disposition") || "";
        const match = disposition.match(/filename="(.+?)"/);
        const filename = match ? match[1] : `export.${format}`;

        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);

        toast(`Exported ${format.toUpperCase()} file`, "success");
    } catch (e) {
        toast("Export failed: " + e.message, "error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Export";
    }
}

// ── Add Device Modal ─────────────────────────────────────
let selectedColor = "#4f8cff";
let selectedFile = null;

function initAddDeviceModal() {
    const overlay = document.getElementById("modal-add");
    const closeBtn = document.getElementById("modal-close");
    const cancelBtn = document.getElementById("btn-modal-cancel");
    const submitBtn = document.getElementById("btn-modal-submit");
    const addBtn = document.getElementById("btn-add-device");
    const nameInput = document.getElementById("device-name");
    const dropZone = document.getElementById("drop-zone");
    const fileInput = document.getElementById("file-input");
    const swatches = document.getElementById("color-swatches");

    addBtn.addEventListener("click", openAddModal);
    closeBtn.addEventListener("click", closeAddModal);
    cancelBtn.addEventListener("click", closeAddModal);
    overlay.addEventListener("click", (e) => {
        if (e.target === overlay) closeAddModal();
    });

    // Color swatches
    swatches.addEventListener("click", (e) => {
        const swatch = e.target.closest(".color-swatch");
        if (!swatch) return;
        swatches.querySelectorAll(".color-swatch").forEach(s => s.classList.remove("selected"));
        swatch.classList.add("selected");
        selectedColor = swatch.dataset.color;
    });

    // File drop zone
    dropZone.addEventListener("click", () => fileInput.click());
    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener("change", () => {
        if (fileInput.files.length) handleFile(fileInput.files[0]);
    });

    // Validate form
    nameInput.addEventListener("input", validateForm);

    // Submit
    submitBtn.addEventListener("click", submitDevice);
}

function handleFile(file) {
    selectedFile = file;
    const nameEl = document.getElementById("file-name");
    nameEl.textContent = file.name;
    nameEl.style.display = "block";

    // Try to extract device name from the JSON
    file.text().then(text => {
        try {
            const data = JSON.parse(text);
            if (data.name) {
                const nameInput = document.getElementById("device-name");
                if (!nameInput.value.trim()) {
                    nameInput.value = data.name;
                }
            }
        } catch {}
        validateForm();
    });
}

function validateForm() {
    const name = document.getElementById("device-name").value.trim();
    document.getElementById("btn-modal-submit").disabled = !(name && selectedFile);
}

function openAddModal() {
    document.getElementById("modal-add").classList.add("open");
    document.getElementById("device-name").focus();
}

function closeAddModal() {
    const overlay = document.getElementById("modal-add");
    overlay.classList.remove("open");
    // Reset form
    document.getElementById("device-name").value = "";
    selectedFile = null;
    document.getElementById("file-name").style.display = "none";
    document.getElementById("file-input").value = "";
    document.getElementById("btn-modal-submit").disabled = true;
    // Reset color to first
    const swatches = document.getElementById("color-swatches");
    swatches.querySelectorAll(".color-swatch").forEach(s => s.classList.remove("selected"));
    swatches.querySelector(".color-swatch").classList.add("selected");
    selectedColor = swatches.querySelector(".color-swatch").dataset.color;
}

async function submitDevice() {
    const name = document.getElementById("device-name").value.trim();
    if (!name || !selectedFile) return;

    const submitBtn = document.getElementById("btn-modal-submit");
    submitBtn.disabled = true;
    submitBtn.textContent = "Adding...";

    const form = new FormData();
    form.append("name", name);
    form.append("color", selectedColor);
    form.append("key_file", selectedFile);

    try {
        const res = await fetch("/api/devices", { method: "POST", body: form, headers: { "X-Requested-With": "fetch" } });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            toast(err.detail || "Failed to add device", "error");
            return;
        }
        closeAddModal();
        toast(`${name} added`, "success");
        await loadDevices();
    } catch (e) {
        toast("Network error: " + e.message, "error");
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Add";
    }
}

// ── Load Devices ─────────────────────────────────────────
let knownDevices = new Set();

// Devices this user has not hidden from their map.
function mapDevices() {
    return devices.filter(d => !d.hidden);
}

async function loadDevices() {
    try {
        devices = await fetchJSON("/api/devices");
    } catch {
        devices = [];
    }

    // Select every map device by default, and any that appeared since last load
    const ids = new Set(mapDevices().map(d => d.id));
    selectedDevices.forEach(id => { if (!ids.has(id)) selectedDevices.delete(id); });
    mapDevices().forEach(d => { if (!knownDevices.has(d.id)) selectedDevices.add(d.id); });
    devices.forEach(d => knownDevices.add(d.id));

    // Auto-select all for export if first load
    if (exportSelectedDevices.size === 0 && devices.length > 0) {
        devices.forEach(d => exportSelectedDevices.add(d.id));
    }

    renderFilterList();
    renderSettingsDevices();
    renderExportDevices();
    renderRuleDeviceOptions();
    updateConfigSummary();
    updateEmptyStates();
    refreshMap();
    // User cards list every AirTag, so they can only be drawn once devices are in.
    loadUsers();
}

function updateEmptyStates() {
    const hasDevices = devices.length > 0;
    document.getElementById("map-empty").style.display = hasDevices ? "none" : "";
    document.getElementById("settings-empty").style.display = hasDevices ? "none" : "";
}

// ── Filter List (config panel) ───────────────────────────
function renderFilterList() {
    const container = document.getElementById("device-filter-list");
    container.innerHTML = "";

    mapDevices().forEach(d => {
        const item = document.createElement("label");
        item.className = "device-filter-item";
        item.dataset.id = d.id;

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selectedDevices.has(d.id);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) selectedDevices.add(d.id);
            else selectedDevices.delete(d.id);
            refreshMap();
        });

        const dot = document.createElement("span");
        dot.className = "device-dot";
        dot.style.background = d.color;

        const info = document.createElement("div");
        info.className = "device-filter-info";
        info.innerHTML = `<div class="device-filter-name">${esc(d.name)}</div>
            <div class="device-filter-lastseen"></div>`;

        const locateBtn = document.createElement("button");
        locateBtn.className = "device-filter-locate";
        locateBtn.title = `Zoom to ${d.name}`;
        locateBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M12 2v4M12 18v4M2 12h4M18 12h4"></path></svg>';
        locateBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            const pos = latestPositions[d.id];
            if (pos) {
                map.flyTo([pos.lat, pos.lng], 16, { duration: 0.8 });
            } else {
                toast("No location data yet", "info");
            }
        });

        item.append(checkbox, dot, info, locateBtn);
        container.appendChild(item);
    });
}

// ── Settings Device List ─────────────────────────────────
const SWATCH_COLORS = ["#4f8cff","#ff5c5c","#34d399","#fbbf24","#a78bfa","#f472b6","#fb923c","#22d3ee"];
const ACCESS_LABELS = { admin: "", edit: "Can edit", view: "View only" };

function swatchesHtml(selected) {
    return SWATCH_COLORS.map(c =>
        `<button class="color-swatch${c === selected ? " selected" : ""}" data-color="${c}" style="background:${c}"></button>`
    ).join("");
}

function renderSettingsDevices() {
    const container = document.getElementById("settings-device-list");
    const expandedId = container.querySelector(".device-card.expanded")?.dataset.id;
    container.innerHTML = "";

    devices.forEach(d => {
        const card = document.createElement("div");
        card.className = "device-card" + (d.enabled === 0 ? " disabled" : "") + (d.id === expandedId ? " expanded" : "");
        card.dataset.id = d.id;

        const lastFetched = d.last_fetched ? timeAgo(d.last_fetched) : "Never";
        const lastFetchedExact = d.last_fetched ? formatExact(d.last_fetched) : "";
        const badges = [
            d.enabled === 0 ? "Disabled" : "",
            d.hidden ? "Hidden" : "",
            ACCESS_LABELS[d.access],
        ].filter(Boolean).map(b => `<span class="badge">${b}</span>`).join("");

        const header = document.createElement("div");
        header.className = "device-card-header";
        header.innerHTML = `
            <span class="device-card-swatch" style="background:${esc(d.color)}"></span>
            <div class="device-card-info">
                <div class="device-card-name">${esc(d.name)} ${badges}</div>
                <div class="device-card-meta" title="${esc(lastFetchedExact)}">Last polled: ${lastFetched}</div>
            </div>
            <svg class="device-card-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>`;
        header.addEventListener("click", () => {
            const wasExpanded = card.classList.contains("expanded");
            container.querySelectorAll(".device-card.expanded").forEach(c => c.classList.remove("expanded"));
            if (!wasExpanded) {
                card.classList.add("expanded");
                if (d.access === "admin") loadShares(card, d);
            }
        });

        const body = document.createElement("div");
        body.className = "device-card-body";
        const canEdit = d.access === "admin" || d.access === "edit";
        const originalName = d.original_name || d.name;

        body.innerHTML = `
            ${canEdit ? `
            <div class="device-setting">
                <span class="device-setting-label">Name</span>
                <div class="device-setting-value">
                    <input type="text" class="device-name-input" value="${esc(d.name)}">
                    <div class="device-setting-original">Original: ${esc(originalName)}${d.name !== originalName ? ' <button class="btn-revert-name">Reset</button>' : ""}</div>
                </div>
            </div>
            <div class="device-setting">
                <span class="device-setting-label">Color</span>
                <div class="device-setting-value"><div class="device-color-swatches">${swatchesHtml(d.color)}</div></div>
            </div>` : ""}
            <div class="device-setting">
                <span class="device-setting-label">Hide from my map</span>
                <div class="device-setting-value">
                    <label class="toggle"><input type="checkbox" class="pref-hidden" ${d.hidden ? "checked" : ""}><span class="toggle-track"></span></label>
                </div>
            </div>
            ${d.access === "admin" ? `
            <div class="device-setting">
                <span class="device-setting-label">Polling</span>
                <div class="device-setting-value">
                    <label class="toggle"><input type="checkbox" class="toggle-enabled" ${d.enabled !== 0 ? "checked" : ""}><span class="toggle-track"></span></label>
                </div>
            </div>
            <div class="card-section-label">Shared with</div>
            <div class="share-list"><div class="setting-hint">Loading…</div></div>
            <div class="device-card-actions">
                <button class="btn-delete">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path></svg>
                    Delete device
                </button>
            </div>` : ""}`;

        body.querySelector(".pref-hidden").addEventListener("change", (e) => {
            updateDevicePrefs(d.id, { hidden: e.target.checked });
        });

        const nameInput = body.querySelector(".device-name-input");
        if (nameInput) {
            nameInput.addEventListener("blur", () => {
                const value = nameInput.value.trim();
                if (value && value !== d.name) updateDeviceField(d.id, { name: value });
            });
            nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") nameInput.blur(); });
            body.querySelector(".btn-revert-name")?.addEventListener("click", () => updateDeviceField(d.id, { name: originalName }));
            body.querySelector(".device-color-swatches").addEventListener("click", (e) => {
                const swatch = e.target.closest(".color-swatch");
                if (swatch) updateDeviceField(d.id, { color: swatch.dataset.color });
            });
        }

        body.querySelector(".toggle-enabled")?.addEventListener("change", (e) => {
            updateDeviceField(d.id, { enabled: e.target.checked });
        });
        body.querySelector(".btn-delete")?.addEventListener("click", (e) => {
            e.stopPropagation();
            showDeleteConfirm(body, d);
        });

        card.append(header, body);
        container.appendChild(card);
        if (d.id === expandedId && d.access === "admin") loadShares(card, d);
    });
}

async function loadShares(card, device) {
    const list = card.querySelector(".share-list");
    if (!list) return;
    try {
        const shares = await fetchJSON(`/api/admin/devices/${device.id}/shares`);
        const others = shares.filter(s => s.user_id !== me.id);
        if (others.length === 0) {
            list.innerHTML = '<div class="setting-hint">No other users yet. Add some on the Users tab.</div>';
            return;
        }
        list.innerHTML = "";
        others.forEach(s => {
            const row = document.createElement("div");
            row.className = "share-row";
            if (s.role === "admin") {
                row.innerHTML = `<span>${esc(s.username)}</span><span class="badge">Admin, sees everything</span>`;
            } else {
                row.innerHTML = `<span>${esc(s.username)}</span>
                    <select>
                        <option value="">No access</option>
                        <option value="view">View</option>
                        <option value="edit">Edit</option>
                    </select>`;
                const sel = row.querySelector("select");
                sel.value = s.access || "";
                sel.addEventListener("change", async () => {
                    try {
                        await api("PUT", `/api/admin/devices/${device.id}/shares/${s.user_id}`, { access: sel.value || null });
                        toast(sel.value ? `Shared with ${s.username}` : `Unshared from ${s.username}`, "success");
                    } catch (e) {
                        toast(e.message, "error");
                        sel.value = s.access || "";
                    }
                });
            }
            list.appendChild(row);
        });
    } catch (e) {
        list.innerHTML = `<div class="setting-hint">Couldn't load sharing: ${esc(e.message)}</div>`;
    }
}

function replaceDevice(updated) {
    const idx = devices.findIndex(d => d.id === updated.id);
    if (idx !== -1) devices[idx] = updated;
    if (!updated.hidden && !selectedDevices.has(updated.id) && !knownDevices.has(updated.id)) {
        selectedDevices.add(updated.id);
    }
    renderFilterList();
    renderSettingsDevices();
    renderExportDevices();
    renderRuleDeviceOptions();
    refreshMap();
}

async function updateDeviceField(deviceId, fields) {
    try {
        replaceDevice(await api("PATCH", `/api/devices/${deviceId}`, fields));
    } catch (e) {
        toast("Failed to update: " + e.message, "error");
    }
}

async function updateDevicePrefs(deviceId, prefs) {
    try {
        const updated = await api("PUT", `/api/devices/${deviceId}/prefs`, prefs);
        // Un-hiding puts it back on the map
        if (prefs.hidden === false) selectedDevices.add(deviceId);
        replaceDevice(updated);
    } catch (e) {
        toast("Failed to update: " + e.message, "error");
    }
}

function showDeleteConfirm(container, device) {
    const existing = container.querySelector(".confirm-bar");
    if (existing) { existing.remove(); return; }

    const bar = document.createElement("div");
    bar.className = "confirm-bar";
    bar.innerHTML = `<span>Delete "${esc(device.name)}" and all its history, for everyone?</span>
        <button class="btn-confirm-yes">Delete</button>
        <button class="btn-confirm-no">Cancel</button>`;

    bar.querySelector(".btn-confirm-yes").addEventListener("click", async () => {
        try {
            await api("DELETE", `/api/devices/${device.id}`);
            selectedDevices.delete(device.id);
            toast(`${device.name} deleted`, "success");
            await loadDevices();
        } catch (e) {
            toast("Failed to delete: " + e.message, "error");
        }
    });
    bar.querySelector(".btn-confirm-no").addEventListener("click", () => bar.remove());

    container.appendChild(bar);
}

// ── Map Rendering ────────────────────────────────────────
function clearMapLayers() {
    if (clusterGroup) {
        map.removeLayer(clusterGroup);
        clusterGroup = null;
    }
    mapLayers.markers.forEach(m => map.removeLayer(m));
    mapLayers.trails.forEach(t => map.removeLayer(t));
    mapLayers.markers = [];
    mapLayers.trails = [];
}

async function refreshMap() {
    clearMapLayers();
    locationCache = {};

    const visible = mapDevices().filter(d => selectedDevices.has(d.id));
    if (visible.length === 0) {
        updateScrubber();
        renderGeofencesOnMap();
        return;
    }

    // Show loading indicator
    const loadingEl = document.createElement("div");
    loadingEl.className = "map-loading";
    loadingEl.innerHTML = '<div class="spinner"></div> Loading...';
    document.getElementById("page-map").appendChild(loadingEl);

    await Promise.all(visible.map(async (device) => {
        const params = new URLSearchParams();
        if (activeTimeRange > 0) {
            const from = new Date(Date.now() - activeTimeRange * 3600 * 1000);
            params.set("from_ts", from.toISOString());
        }
        params.set("limit", "0"); // no limit — time range already bounds the data

        let locations;
        try {
            locations = await fetchJSON(`/api/locations/${device.id}?${params}`);
        } catch {
            return;
        }
        if (!locations.length) return;

        // Pre-compute timestamps for scrubber performance
        locationCache[device.id] = locations.map(l => ({
            ...l,
            _ts: parseTS(l.timestamp).getTime(),
        }));
    }));

    loadingEl.remove();
    updateScrubber();
    renderMapAtTime(null, true); // Live mode, fit bounds
}

// ── Timeline Scrubber ────────────────────────────────────
function initScrubber() {
    const slider = document.getElementById("scrubber-slider");
    let rafId = null;

    slider.addEventListener("input", () => {
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(() => {
            const val = parseInt(slider.value, 10);
            const label = document.getElementById("scrubber-label");
            if (val >= scrubberMax) {
                label.textContent = "Live";
                renderMapAtTime(null);
            } else {
                label.textContent = formatTimestamp(new Date(val * 1000).toISOString());
                renderMapAtTime(val * 1000);
            }
            rafId = null;
        });
    });
}

function updateScrubber() {
    const bar = document.getElementById("scrubber-bar");
    const slider = document.getElementById("scrubber-slider");
    const label = document.getElementById("scrubber-label");

    let minTs = Infinity;
    let maxTs = -Infinity;

    for (const deviceId in locationCache) {
        for (const loc of locationCache[deviceId]) {
            if (loc._ts < minTs) minTs = loc._ts;
            if (loc._ts > maxTs) maxTs = loc._ts;
        }
    }

    if (minTs === Infinity || minTs === maxTs) {
        bar.style.display = "none";
        return;
    }

    scrubberMin = Math.floor(minTs / 1000);
    scrubberMax = Math.ceil(maxTs / 1000);

    slider.min = scrubberMin;
    slider.max = scrubberMax;
    slider.value = scrubberMax;
    label.textContent = "Live";
    bar.style.display = "flex";
}

function renderMapAtTime(timeMs, fitBounds = false) {
    clearMapLayers();

    renderGeofencesOnMap();
    const visible = mapDevices().filter(d => selectedDevices.has(d.id));
    if (visible.length === 0) return;

    const enabledIds = new Set(devices.filter(d => d.enabled !== 0).map(d => d.id));
    const isLive = timeMs === null;
    const splitTime = isLive ? Infinity : timeMs;

    clusterGroup = L.markerClusterGroup({
        maxClusterRadius: 40,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        iconCreateFunction: function(cluster) {
            const count = cluster.getChildCount();
            return L.divIcon({
                html: `<span>${count}</span>`,
                className: "marker-cluster",
                iconSize: [36, 36],
            });
        },
    });

    const allPoints = [];

    visible.forEach(device => {
        const locations = locationCache[device.id];
        if (!locations || !locations.length) return;

        const isDisabled = !enabledIds.has(device.id);
        const color = isDisabled ? "#555" : device.color;

        // Split locations at scrub time (locations sorted DESC — newest first)
        const before = []; // timestamp <= scrubTime (colored, "past")
        const after = [];  // timestamp > scrubTime (grey, "future")

        for (const loc of locations) {
            if (loc._ts <= splitTime) {
                before.push(loc);
            } else {
                after.push(loc);
            }
        }

        // Colored trail (past / before scrub time)
        if (before.length > 0) {
            const latlngs = before.map(l => [l.latitude, l.longitude]);
            const trail = L.polyline(latlngs, {
                color, weight: 2.5,
                opacity: isDisabled ? 0.25 : 0.5,
                dashArray: isDisabled ? "6 4" : null,
            }).addTo(map);
            mapLayers.trails.push(trail);
            allPoints.push(...latlngs);
        }

        // Grey trail (future / after scrub time)
        if (after.length > 0) {
            const latlngs = after.map(l => [l.latitude, l.longitude]);
            // Connect to nearest "before" point for visual continuity
            if (before.length > 0) {
                latlngs.push([before[0].latitude, before[0].longitude]);
            }
            const trail = L.polyline(latlngs, {
                color: "#555", weight: 2, opacity: 0.25, dashArray: "6 4",
            }).addTo(map);
            mapLayers.trails.push(trail);
        }

        // Marker at scrub position (newest point at or before scrub time)
        const markerLoc = before.length > 0 ? before[0] : null;
        if (markerLoc) {
            const markerColor = isDisabled ? "#555" : device.color;
            const marker = L.marker([markerLoc.latitude, markerLoc.longitude], {
                icon: L.divIcon({
                    className: "device-marker-icon",
                    html: `<div style="background:${markerColor};opacity:${isDisabled ? 0.4 : 0.9}"></div>`,
                    iconSize: [16, 16],
                    iconAnchor: [8, 8],
                    popupAnchor: [0, -10],
                }),
            }).bindPopup(
                `<b>${esc(device.name)}</b>${isDisabled ? " <i>(Disabled)</i>" : ""}<br>
                 ${markerLoc.latitude.toFixed(5)}, ${markerLoc.longitude.toFixed(5)}<br>
                 <small>${formatTimestamp(markerLoc.timestamp)}</small>`
            );
            marker.bindTooltip(device.name + (isDisabled ? " (off)" : ""), {
                permanent: true,
                direction: "top",
                offset: [0, -12],
                className: "device-label" + (isDisabled ? " device-label-disabled" : ""),
            });
            clusterGroup.addLayer(marker);

            latestPositions[device.id] = { lat: markerLoc.latitude, lng: markerLoc.longitude };
            if (isLive) updateLastSeen(device.id, markerLoc.timestamp);
        }

        if (isLive) allPoints.push(...(locations.map(l => [l.latitude, l.longitude])));
    });

    map.addLayer(clusterGroup);

    if (fitBounds && allPoints.length > 0) {
        map.fitBounds(L.latLngBounds(allPoints), { padding: [50, 50], maxZoom: 16 });
    }
}

function updateLastSeen(deviceId, timestamp) {
    const item = document.querySelector(`.device-filter-item[data-id="${CSS.escape(deviceId)}"]`);
    const el = item && item.querySelector(".device-filter-lastseen");
    if (el) {
        el.textContent = timeAgo(timestamp);
        el.title = formatExact(timestamp);
    }
}

// ── Toast Notifications ──────────────────────────────────
function toast(message, type = "info") {
    const container = document.getElementById("toast-container");
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 3200);
}

// ── API helpers ──────────────────────────────────────────
class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
}

// Every API call goes through here: adds the CSRF header, unwraps JSON, and
// bounces to the login page when the session has gone.
async function api(method, url, body) {
    const opts = { method, headers: { "X-Requested-With": "fetch" } };
    if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    if (res.status === 401 && !url.startsWith("/api/auth/")) {
        showLogin();
        throw new ApiError(401, "Signed out");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
        throw new ApiError(res.status, detail);
    }
    return data;
}

function fetchJSON(url) {
    return api("GET", url);
}

function esc(str) {
    const d = document.createElement("div");
    d.textContent = str ?? "";
    return d.innerHTML;
}

function formatDate(iso) {
    return parseTS(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatTimestamp(iso) {
    return parseTS(iso).toLocaleString(undefined, {
        month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
    });
}

// Ensure timestamps without timezone info are treated as UTC
function parseTS(iso) {
    if (!iso) return new Date(NaN);
    const s = iso.trim().replace(" ", "T");
    // If no timezone marker at end (Z, +HH:MM, -HH:MM, +00:00), assume UTC
    if (!/Z$|[+-]\d{2}(:\d{2})?$/.test(s)) return new Date(s + "Z");
    return new Date(s);
}

function timeAgo(iso) {
    const seconds = Math.floor((Date.now() - parseTS(iso).getTime()) / 1000);
    if (seconds < 60) return "<1m ago";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

function formatExact(iso) {
    return parseTS(iso).toLocaleString(undefined, {
        year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
}

function isAdmin() {
    return me && me.role === "admin";
}

// ── Boot / app login ─────────────────────────────────────
let authConfig = { setup_required: false, oidc_enabled: false };

const SSO_ERRORS = {
    failed: "Single sign-on failed. Try again, or sign in with a password.",
    expired: "The sign-in took too long. Please try again.",
    disabled: "Single sign-on is turned off.",
    not_provisioned: "Your SSO account isn't set up here yet. Ask an admin to create it.",
    already_linked: "That SSO account is already linked to a different user.",
};

async function boot() {
    const params = new URLSearchParams(location.search);
    const ssoError = params.get("sso_error");
    const ssoLinked = params.get("sso_linked");
    if (ssoError || ssoLinked) history.replaceState(null, "", location.pathname);

    try {
        authConfig = await api("GET", "/api/auth/config");
    } catch {}

    try {
        me = await api("GET", "/api/auth/me");
    } catch {
        me = null;
    }

    if (!me) {
        showLogin(ssoError ? (SSO_ERRORS[ssoError] || SSO_ERRORS.failed) : null);
        return;
    }
    if (ssoError) toast(SSO_ERRORS[ssoError] || SSO_ERRORS.failed, "error");
    if (ssoLinked) toast("Single sign-on linked to your account", "success");
    await enterApp();
}

function hideAllPages() {
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    document.getElementById("bottom-nav").style.display = "none";
}

function showLogin(notice = null) {
    me = null;
    hideAllPages();
    document.getElementById("page-login").classList.add("active");
    const noticeEl = document.getElementById("login-notice");
    noticeEl.hidden = !notice;
    noticeEl.textContent = notice || "";

    const setup = authConfig.setup_required;
    document.getElementById("setup-form").hidden = !setup;
    document.getElementById("login-form").hidden = setup;
    document.getElementById("pwchange-form").hidden = true;
    document.getElementById("login-subtitle").textContent = setup ? "Welcome! Let's get you set up." : "Sign in to continue";

    const sso = document.getElementById("btn-sso");
    sso.hidden = !authConfig.oidc_enabled;
    sso.textContent = authConfig.oidc_button_label || "Sign in with SSO";
    document.getElementById("login-divider").hidden = !authConfig.oidc_enabled;
}

function showPasswordChange() {
    hideAllPages();
    document.getElementById("page-login").classList.add("active");
    document.getElementById("login-notice").hidden = true;
    document.getElementById("setup-form").hidden = true;
    document.getElementById("login-form").hidden = true;
    document.getElementById("pwchange-form").hidden = false;
    document.getElementById("login-subtitle").textContent = `Hi ${me.username}`;
    document.getElementById("pwchange-current").focus();
}

async function enterApp() {
    if (me.force_password_change) {
        showPasswordChange();
        return;
    }
    document.body.classList.toggle("is-admin", isAdmin());
    document.body.classList.toggle("is-user", !isAdmin());

    const range = me.prefs.default_range_hours;
    if (range !== undefined) {
        activeTimeRange = range;
        document.querySelectorAll("#time-range-group .time-btn").forEach(b =>
            b.classList.toggle("active", parseInt(b.dataset.range, 10) === range));
    }
    renderAccount();

    let apple = { authenticated: true };
    try { apple = await api("GET", "/api/apple/status"); } catch {}
    if (!apple.authenticated) {
        showSetup();
        return;
    }
    showApp();
}

function initLogin() {
    document.getElementById("login-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const btn = e.submitter;
        btn.disabled = true;
        try {
            me = await api("POST", "/api/auth/login", {
                username: document.getElementById("login-username").value.trim(),
                password: document.getElementById("login-password").value,
            });
            document.getElementById("login-password").value = "";
            await enterApp();
        } catch (err) {
            showLogin(err.message);
        } finally {
            btn.disabled = false;
        }
    });

    document.getElementById("setup-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const btn = e.submitter;
        btn.disabled = true;
        try {
            await api("POST", "/api/auth/setup", {
                token: document.getElementById("setup-token").value.trim(),
                username: document.getElementById("setup-username").value.trim(),
                password: document.getElementById("setup-password").value,
            });
            authConfig.setup_required = false;
            me = await api("GET", "/api/auth/me");
            await enterApp();
        } catch (err) {
            const notice = document.getElementById("login-notice");
            notice.textContent = err.message;
            notice.hidden = false;
        } finally {
            btn.disabled = false;
        }
    });

    document.getElementById("pwchange-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
            await api("POST", "/api/auth/change-password", {
                current_password: document.getElementById("pwchange-current").value,
                new_password: document.getElementById("pwchange-new").value,
            });
            document.getElementById("pwchange-current").value = "";
            document.getElementById("pwchange-new").value = "";
            me = await api("GET", "/api/auth/me");
            await enterApp();
        } catch (err) {
            const notice = document.getElementById("login-notice");
            notice.textContent = err.message;
            notice.hidden = false;
        }
    });

    document.getElementById("btn-sso").addEventListener("click", () => {
        location.href = "/api/auth/oidc/start";
    });

    document.querySelectorAll(".btn-logout").forEach(btn => btn.addEventListener("click", async () => {
        try { await api("POST", "/api/auth/logout"); } catch {}
        // Drop everything the previous user could see.
        devices = [];
        geofences = [];
        selectedDevices.clear();
        knownDevices.clear();
        clearMapLayers();
        if (geofenceLayer) { map.removeLayer(geofenceLayer); geofenceLayer = null; }
        showLogin();
    }));
}

// ── Account tab ──────────────────────────────────────────
function renderAccount() {
    document.getElementById("account-name").textContent = `${me.username}${isAdmin() ? " (admin)" : ""}`;
    document.getElementById("pref-range").value = String(me.prefs.default_range_hours ?? 24);
    const hasKey = !!me.prefs.pushover_user_key;
    document.getElementById("pushover-status").textContent = hasKey ? "Set up" : "Not set up";
    document.getElementById("btn-test-push").hidden = !hasKey;
    document.getElementById("pushover-hint").textContent = hasKey
        ? "Alerts are sent to your Pushover account."
        : isAdmin()
            ? "Add your Pushover user key on your card in the Users tab to get alerts."
            : "Alerts are recorded below but not sent. Ask an admin to add your Pushover key.";

    const ssoRow = document.getElementById("sso-row");
    ssoRow.hidden = !authConfig.oidc_enabled && !me.sso_linked;
    document.getElementById("sso-status").textContent = me.sso_linked ? "Linked" : "Not linked";
    const linkBtn = document.getElementById("btn-sso-link");
    linkBtn.textContent = me.sso_linked ? "Unlink" : "Link";
    linkBtn.hidden = !me.sso_linked && !authConfig.oidc_enabled;

    // SSO-only accounts set a first password without a current one.
    document.getElementById("account-pw-current-group").hidden = !me.has_password;
    document.getElementById("pw-title").textContent = me.has_password ? "Change password" : "Set a password";
}

async function savePrefs(prefs, message) {
    try {
        me.prefs = await api("PATCH", "/api/auth/me/prefs", prefs);
        if (message) toast(message, "success");
        return true;
    } catch (e) {
        toast(e.message, "error");
        return false;
    }
}

function initAccountTab() {
    document.getElementById("pref-range").addEventListener("change", (e) => {
        savePrefs({ default_range_hours: parseInt(e.target.value, 10) }, "Default range saved");
    });

    document.getElementById("btn-sso-link").addEventListener("click", async () => {
        if (!me.sso_linked) {
            location.href = "/api/auth/oidc/start?link=1";
            return;
        }
        try {
            await api("POST", "/api/auth/unlink-sso");
            me.sso_linked = false;
            renderAccount();
            toast("Single sign-on unlinked", "success");
        } catch (e) {
            toast(e.message, "error");
        }
    });

    document.getElementById("account-pw-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const current = document.getElementById("account-pw-current");
        const next = document.getElementById("account-pw-new");
        try {
            await api("POST", "/api/auth/change-password", {
                current_password: current.value || null,
                new_password: next.value,
            });
            current.value = "";
            next.value = "";
            me.has_password = true;
            renderAccount();
            toast("Password updated. Other devices have been signed out.", "success");
        } catch (err) {
            toast(err.message, "error");
        }
    });
}

// ── Alerts tab: places, rules, events ────────────────────
const KIND_LABELS = { enter: "arrives at", exit: "leaves", both: "arrives at or leaves" };

function initAlertsTab() {
    document.getElementById("btn-test-push").addEventListener("click", async () => {
        try {
            await api("POST", "/api/alerts/test");
            toast("Test notification sent", "success");
        } catch (e) {
            toast(e.message, "error");
        }
    });

    // Places
    const gForm = document.getElementById("geofence-form");
    document.getElementById("btn-add-geofence").addEventListener("click", () => openGeofenceForm());
    document.getElementById("btn-geofence-cancel").addEventListener("click", closeGeofenceForm);
    document.getElementById("btn-geofence-pick").addEventListener("click", startPicking);
    document.getElementById("btn-pick-cancel").addEventListener("click", stopPicking);
    document.getElementById("btn-geofence-draw").addEventListener("click", startDrawing);
    document.getElementById("btn-draw-undo").addEventListener("click", () => {
        if (drawing && drawing.points.length) { drawing.points.pop(); renderDrawing(); }
    });
    document.getElementById("btn-draw-cancel").addEventListener("click", () => stopDrawing(false));
    document.getElementById("btn-draw-done").addEventListener("click", () => stopDrawing(true));
    document.getElementById("geofence-shape").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-shape]");
        if (btn) setGeofenceShape(btn.dataset.shape);
    });
    gForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const id = document.getElementById("geofence-id").value;
        const body = { name: document.getElementById("geofence-name").value.trim(), shape: geofenceShape };
        if (geofenceShape === "polygon") {
            if (!draftPolygon || draftPolygon.length < 3) { toast("Draw the area on the map first", "error"); return; }
            body.polygon = draftPolygon;
        } else {
            body.latitude = parseFloat(document.getElementById("geofence-lat").value);
            body.longitude = parseFloat(document.getElementById("geofence-lon").value);
            body.radius_m = parseFloat(document.getElementById("geofence-radius").value);
        }
        try {
            await api(id ? "PUT" : "POST", id ? `/api/geofences/${id}` : "/api/geofences", body);
            closeGeofenceForm();
            toast(`${body.name} saved`, "success");
            await loadAlerts();
        } catch (err) {
            toast(err.message, "error");
        }
    });

    // Rules
    const rForm = document.getElementById("rule-form");
    const kind = document.getElementById("rule-kind");
    kind.addEventListener("change", () => {
        const stale = kind.value === "stale";
        document.getElementById("rule-geofence-group").hidden = stale;
        document.getElementById("rule-stale-group").hidden = !stale;
    });
    document.getElementById("btn-add-rule").addEventListener("click", () => {
        if (!devices.length) { toast("No AirTags to watch yet", "info"); return; }
        rForm.hidden = false;
        document.getElementById("btn-add-rule").hidden = true;
    });
    document.getElementById("btn-rule-cancel").addEventListener("click", () => {
        rForm.hidden = true;
        document.getElementById("btn-add-rule").hidden = false;
    });
    rForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const body = {
            device_id: document.getElementById("rule-device").value,
            kind: kind.value,
        };
        if (kind.value === "stale") {
            body.stale_hours = parseFloat(document.getElementById("rule-stale").value);
        } else {
            body.geofence_id = document.getElementById("rule-geofence").value;
            if (!body.geofence_id) { toast("Add a place first", "error"); return; }
        }
        try {
            await api("POST", "/api/alerts", body);
            rForm.hidden = true;
            document.getElementById("btn-add-rule").hidden = false;
            toast("Alert added", "success");
            await loadAlerts();
        } catch (err) {
            toast(err.message, "error");
        }
    });
}

function renderRuleDeviceOptions() {
    const sel = document.getElementById("rule-device");
    const current = sel.value;
    sel.innerHTML = devices.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
    if (current) sel.value = current;
}

async function loadAlerts() {
    let rules = [], events = [];
    try {
        [geofences, rules, events] = await Promise.all([
            fetchJSON("/api/geofences"), fetchJSON("/api/alerts"), fetchJSON("/api/alerts/events"),
        ]);
    } catch {
        return;
    }
    renderGeofenceList();
    renderRuleList(rules);
    renderEventList(events);
    renderGeofencesOnMap();
}

function renderGeofenceList() {
    const list = document.getElementById("geofence-list");
    document.getElementById("rule-geofence").innerHTML =
        geofences.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("");
    if (!geofences.length) {
        list.innerHTML = '<div class="setting-hint">No places yet. Add one, like Home or School.</div>';
        return;
    }
    list.innerHTML = "";
    geofences.forEach(g => {
        const row = document.createElement("div");
        row.className = "item-row";
        const meta = g.shape === "polygon"
            ? `Area · ${g.polygon.length} points · about ${formatDistance(g.radius_m * 2)} across`
            : `${g.radius_m.toFixed(0)} m around ${g.latitude.toFixed(5)}, ${g.longitude.toFixed(5)}`;
        row.innerHTML = `<div class="item-main"><div class="item-title">${esc(g.name)}</div>
            <div class="item-meta">${meta}</div></div>
            <button class="btn-small btn-show">Show</button>
            <button class="btn-small btn-edit">Edit</button>
            <button class="btn-small btn-danger btn-del">Delete</button>`;
        row.querySelector(".btn-show").addEventListener("click", () => {
            navigateTo("page-map");
            if (g.shape === "polygon") map.flyToBounds(L.latLngBounds(g.polygon), { padding: [40, 40], duration: 0.8 });
            else map.flyTo([g.latitude, g.longitude], 16, { duration: 0.8 });
        });
        row.querySelector(".btn-edit").addEventListener("click", () => openGeofenceForm(g));
        row.querySelector(".btn-del").addEventListener("click", () => confirmInline(row, `Delete ${g.name} and its alerts?`));
        row.querySelector(".btn-del").addEventListener("confirm", async () => {
            try {
                await api("DELETE", `/api/geofences/${g.id}`);
                await loadAlerts();
            } catch (e) { toast(e.message, "error"); }
        });
        list.appendChild(row);
    });
}

function renderRuleList(rules) {
    const list = document.getElementById("rule-list");
    if (!rules.length) {
        list.innerHTML = '<div class="setting-hint">No alerts yet.</div>';
        return;
    }
    const names = Object.fromEntries(devices.map(d => [d.id, d.name]));
    list.innerHTML = "";
    rules.forEach(r => {
        const what = r.kind === "stale"
            ? `stops reporting for ${r.stale_hours}h`
            : `${KIND_LABELS[r.kind]} ${esc(r.geofence_name)}`;
        const state = r.last_state === "in" ? "inside now" : r.last_state === "out" ? "outside now"
                    : r.last_state === "stale" ? "quiet now" : r.last_state === "fresh" ? "reporting" : "waiting for a fix";
        const row = document.createElement("div");
        row.className = "item-row" + (r.enabled ? "" : " muted");
        row.innerHTML = `<div class="item-main"><div class="item-title">${esc(names[r.device_id] || "Unknown")} ${what}</div>
            <div class="item-meta">${state}${r.last_fired_at ? ` · last alert ${timeAgo(r.last_fired_at)}` : ""}</div></div>
            <label class="toggle"><input type="checkbox" ${r.enabled ? "checked" : ""}><span class="toggle-track"></span></label>
            <button class="btn-small btn-danger btn-del">Delete</button>`;
        row.querySelector("input").addEventListener("change", async (e) => {
            try {
                await api("PUT", `/api/alerts/${r.id}`, {
                    device_id: r.device_id, kind: r.kind, geofence_id: r.geofence_id,
                    stale_hours: r.stale_hours, enabled: e.target.checked,
                });
                row.classList.toggle("muted", !e.target.checked);
            } catch (err) { toast(err.message, "error"); }
        });
        row.querySelector(".btn-del").addEventListener("click", () => confirmInline(row, "Delete this alert?"));
        row.querySelector(".btn-del").addEventListener("confirm", async () => {
            try {
                await api("DELETE", `/api/alerts/${r.id}`);
                await loadAlerts();
            } catch (e) { toast(e.message, "error"); }
        });
        list.appendChild(row);
    });
}

function renderEventList(events) {
    const list = document.getElementById("event-list");
    if (!events.length) {
        list.innerHTML = '<div class="setting-hint">Nothing yet.</div>';
        return;
    }
    list.innerHTML = events.map(e => `<div class="item-row"><div class="item-main">
        <div class="item-title">${esc(e.message)}</div>
        <div class="item-meta" title="${esc(formatExact(e.created_at))}">${timeAgo(e.created_at)}${e.delivered ? "" : " · not delivered (no Pushover key)"}</div>
        </div></div>`).join("");
}

// Inline "are you sure?" bar. Fires a `confirm` event on the row's delete button.
function confirmInline(row, message) {
    const existing = row.nextElementSibling;
    if (existing && existing.classList.contains("confirm-bar")) { existing.remove(); return; }
    const btn = row.querySelector(".btn-del");
    const bar = document.createElement("div");
    bar.className = "confirm-bar";
    bar.innerHTML = `<span>${esc(message)}</span><button class="btn-confirm-yes">Delete</button><button class="btn-confirm-no">Cancel</button>`;
    bar.querySelector(".btn-confirm-yes").addEventListener("click", () => { bar.remove(); btn.dispatchEvent(new Event("confirm")); });
    bar.querySelector(".btn-confirm-no").addEventListener("click", () => bar.remove());
    row.after(bar);
}

function openGeofenceForm(g = null) {
    const form = document.getElementById("geofence-form");
    document.getElementById("geofence-id").value = g ? g.id : "";
    document.getElementById("geofence-name").value = g ? g.name : "";
    const center = map.getCenter();
    document.getElementById("geofence-lat").value = (g ? g.latitude : center.lat).toFixed(6);
    document.getElementById("geofence-lon").value = (g ? g.longitude : center.lng).toFixed(6);
    document.getElementById("geofence-radius").value = g && g.shape !== "polygon" ? g.radius_m : 150;
    draftPolygon = g && g.polygon ? g.polygon.map(p => [...p]) : null;
    setGeofenceShape(g ? g.shape : "circle");
    form.querySelector('button[type="submit"]').textContent = g ? "Save" : "Add Place";
    form.hidden = false;
    document.getElementById("btn-add-geofence").hidden = true;
    document.getElementById("geofence-name").focus();
}

function closeGeofenceForm() {
    document.getElementById("geofence-form").hidden = true;
    document.getElementById("btn-add-geofence").hidden = false;
}

function startPicking() {
    pickingGeofence = true;
    navigateTo("page-map");
    document.getElementById("pick-banner").hidden = false;
    document.getElementById("map").classList.add("picking");
}

function stopPicking() {
    pickingGeofence = false;
    document.getElementById("pick-banner").hidden = true;
    document.getElementById("map").classList.remove("picking");
    navigateTo("page-settings");
}

function onMapPick(e) {
    if (drawing) {
        drawing.points.push([e.latlng.lat, e.latlng.lng]);
        renderDrawing();
        return;
    }
    if (!pickingGeofence) return;
    document.getElementById("geofence-lat").value = e.latlng.lat.toFixed(6);
    document.getElementById("geofence-lon").value = e.latlng.lng.toFixed(6);
    stopPicking();
}

function setGeofenceShape(shape) {
    geofenceShape = shape;
    const area = shape === "polygon";
    document.querySelectorAll("#geofence-shape button").forEach(b => b.classList.toggle("active", b.dataset.shape === shape));
    document.getElementById("geofence-circle-fields").hidden = area;
    document.getElementById("geofence-area-fields").hidden = !area;
    // Disabled, not just hidden: hidden `required` fields would block submit.
    document.querySelectorAll("#geofence-circle-fields input").forEach(i => { i.disabled = area; });
    document.getElementById("btn-geofence-pick").hidden = area;
    document.getElementById("btn-geofence-draw").hidden = !area;
    updateAreaSummary();
}

function updateAreaSummary() {
    const el = document.getElementById("geofence-area-summary");
    const n = draftPolygon ? draftPolygon.length : 0;
    el.textContent = n >= 3 ? `Area with ${n} points.` : "No area drawn yet.";
    document.getElementById("btn-geofence-draw").textContent = n >= 3 ? "Edit on map" : "Draw on map";
}

function formatDistance(m) {
    return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

// Area drawing: tap the map to add points; drag a point to move it; Undo removes
// the last one. The draft only replaces the form's area when Done is pressed.
function startDrawing() {
    drawing = { points: draftPolygon ? draftPolygon.map(p => [...p]) : [], layer: L.layerGroup().addTo(map) };
    navigateTo("page-map");
    document.getElementById("draw-banner").hidden = false;
    document.getElementById("map").classList.add("picking");
    if (drawing.points.length >= 3) map.fitBounds(L.latLngBounds(drawing.points), { padding: [60, 60] });
    renderDrawing();
}

function stopDrawing(save) {
    if (!drawing) return;
    if (save) {
        if (drawing.points.length < 3) { toast("An area needs at least 3 points", "error"); return; }
        draftPolygon = drawing.points;
    }
    map.removeLayer(drawing.layer);
    drawing = null;
    document.getElementById("draw-banner").hidden = true;
    document.getElementById("map").classList.remove("picking");
    updateAreaSummary();
    navigateTo("page-settings");
}

function renderDrawing() {
    const { points, layer } = drawing;
    layer.clearLayers();
    const style = { color: "#4f8cff", weight: 2, fillOpacity: 0.12, interactive: false };
    const outline = points.length >= 3 ? L.polygon(points, style) : L.polyline(points, style);
    layer.addLayer(outline);
    points.forEach((p, i) => {
        const marker = L.marker(p, {
            draggable: true,
            icon: L.divIcon({ className: "draw-vertex" + (i === 0 ? " first" : ""), iconSize: [16, 16], iconAnchor: [8, 8] }),
        });
        marker.on("drag", (e) => {
            const ll = e.target.getLatLng();
            points[i] = [ll.lat, ll.lng];
            outline.setLatLngs(points);
        });
        layer.addLayer(marker);
    });
    const n = points.length;
    document.getElementById("draw-hint").textContent =
        n === 0 ? "Tap the map to start the area" :
        n < 3 ? `Tap to add points (${n} so far, need 3)` :
        `${n} points · tap to add, drag to move`;
    document.getElementById("btn-draw-undo").disabled = n === 0;
    document.getElementById("btn-draw-done").disabled = n < 3;
}

const GEOFENCE_COLOR = "#e2725b";  // terracotta, the same for every place

// Where a place's name badge sits: the top of a circle, or the northernmost
// vertex of an area (an area's centre can fall outside a concave shape).
function geofenceBadgePoint(g) {
    if (g.shape === "polygon") return g.polygon.reduce((top, p) => (p[0] > top[0] ? p : top));
    return [g.latitude + g.radius_m / 111320, g.longitude];
}

function renderGeofencesOnMap() {
    if (geofenceLayer) map.removeLayer(geofenceLayer);
    if (!map.getPane("geofenceBadges")) {
        // Above shapes and trails, below AirTag markers so a badge never hides a tag.
        map.createPane("geofenceBadges").style.zIndex = 550;
    }
    const style = { color: GEOFENCE_COLOR, weight: 2, opacity: 0.9, dashArray: "6 4",
                    fillColor: GEOFENCE_COLOR, fillOpacity: 0.08, interactive: false };
    const layers = [];
    geofences.forEach(g => {
        layers.push(g.shape === "polygon"
            ? L.polygon(g.polygon, style)
            : L.circle([g.latitude, g.longitude], { ...style, radius: g.radius_m }));
        layers.push(L.marker(geofenceBadgePoint(g), {
            pane: "geofenceBadges",
            interactive: false,
            keyboard: false,
            icon: L.divIcon({ className: "geofence-badge-anchor", html: `<span class="geofence-badge">${esc(g.name)}</span>`,
                              iconSize: null, iconAnchor: [0, 0] }),
        }));
    });
    geofenceLayer = L.layerGroup(layers).addTo(map);
}

// ── Users tab (admin) ────────────────────────────────────
function initUsersTab() {
    const form = document.getElementById("user-form");
    document.getElementById("btn-add-user").addEventListener("click", () => {
        form.hidden = false;
        document.getElementById("btn-add-user").hidden = true;
        document.getElementById("new-username").focus();
    });
    document.getElementById("btn-user-cancel").addEventListener("click", () => {
        form.hidden = true;
        document.getElementById("btn-add-user").hidden = false;
    });
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
            const u = await api("POST", "/api/admin/users", {
                username: document.getElementById("new-username").value.trim(),
                password: document.getElementById("new-password").value,
                role: document.getElementById("new-role").value,
            });
            form.reset();
            form.hidden = true;
            document.getElementById("btn-add-user").hidden = false;
            toast(`${u.username} created. Share some AirTags with them below.`, "success");
            await loadUsers(u.id);
        } catch (err) {
            toast(err.message, "error");
        }
    });
}

// User cards whose AirTag access list is expanded; survives re-renders.
const openAccess = new Set();

async function loadUsers(expandId = null) {
    if (!isAdmin()) return;
    let users;
    try { users = await fetchJSON("/api/admin/users"); } catch { return; }
    const list = document.getElementById("user-list");
    const openId = expandId || list.querySelector(".device-card.expanded")?.dataset.id;
    list.innerHTML = "";

    users.forEach(u => {
        const card = document.createElement("div");
        card.className = "device-card" + (u.id === openId ? " expanded" : "");
        card.dataset.id = u.id;
        const isSelf = u.id === me.id;
        const methods = [u.has_password ? "password" : "", u.sso_linked ? "SSO" : ""].filter(Boolean).join(" + ") || "no sign-in method";
        const shareCount = u.role === "admin" ? "all AirTags" : `${u.shares.length} AirTag${u.shares.length === 1 ? "" : "s"}`;
        card.innerHTML = `
            <div class="device-card-header">
                <span class="avatar">${esc(u.username[0].toUpperCase())}</span>
                <div class="device-card-info">
                    <div class="device-card-name">${esc(u.username)} ${u.role === "admin" ? '<span class="badge">Admin</span>' : ""} ${isSelf ? '<span class="badge">You</span>' : ""}</div>
                    <div class="device-card-meta">${methods} · ${shareCount} · ${u.last_login_at ? "last seen " + timeAgo(u.last_login_at) : "never signed in"}</div>
                </div>
                <svg class="device-card-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </div>
            <div class="device-card-body">
                <div class="device-setting">
                    <span class="device-setting-label">Role</span>
                    <div class="device-setting-value">
                        <select class="user-role" ${isSelf ? "disabled" : ""}>
                            <option value="user">User</option><option value="admin">Admin</option>
                        </select>
                    </div>
                </div>
                <div class="device-setting">
                    <span class="device-setting-label">Pushover key</span>
                    <div class="device-setting-value inline-controls">
                        <input type="text" class="pushover-key" value="${esc(u.pushover_user_key)}" placeholder="30-character user key" autocomplete="off" spellcheck="false">
                        <button class="btn-small btn-save-pushover">Save</button>
                    </div>
                </div>
                ${u.role === "admin" ? "" : `
                <button class="access-toggle${openAccess.has(u.id) ? " open" : ""}">
                    <span>AirTag access</span>
                    <span class="access-count">${u.shares.length} of ${devices.length}</span>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </button>
                <div class="share-list access-list"${openAccess.has(u.id) ? "" : " hidden"}>${devices.length ? devices.map(d => {
                    const s = u.shares.find(x => x.device_id === d.id);
                    const v = s ? (s.can_edit ? "edit" : "view") : "";
                    return `<div class="share-row"><span><span class="device-dot" style="background:${esc(d.color)}"></span> ${esc(d.name)}</span>
                        <select data-device="${esc(d.id)}">
                            <option value="" ${v === "" ? "selected" : ""}>No access</option>
                            <option value="view" ${v === "view" ? "selected" : ""}>View</option>
                            <option value="edit" ${v === "edit" ? "selected" : ""}>Edit</option>
                        </select></div>`;
                }).join("") : '<div class="setting-hint">No AirTags yet.</div>'}</div>`}
                <div class="device-setting">
                    <span class="device-setting-label">Password</span>
                    <div class="device-setting-value inline-controls">
                        <input type="text" class="reset-pw" placeholder="New temporary password" autocomplete="off">
                        <button class="btn-small btn-reset-pw">Reset</button>
                    </div>
                </div>
                ${isSelf ? "" : `<div class="device-card-actions"><button class="btn-delete">Delete user</button></div>`}
            </div>`;

        card.querySelector(".device-card-header").addEventListener("click", () => {
            const was = card.classList.contains("expanded");
            list.querySelectorAll(".device-card.expanded").forEach(c => c.classList.remove("expanded"));
            if (!was) card.classList.add("expanded");
        });

        const role = card.querySelector(".user-role");
        role.value = u.role;
        role.addEventListener("change", async () => {
            try {
                await api("PATCH", `/api/admin/users/${u.id}`, { role: role.value });
                toast(`${u.username} is now ${role.value === "admin" ? "an admin" : "a user"}`, "success");
                await loadUsers(u.id);
            } catch (e) { toast(e.message, "error"); role.value = u.role; }
        });

        card.querySelector(".access-toggle")?.addEventListener("click", (e) => {
            const btn = e.currentTarget;
            const open = btn.classList.toggle("open");
            card.querySelector(".access-list").hidden = !open;
            if (open) openAccess.add(u.id); else openAccess.delete(u.id);
        });

        const keyInput = card.querySelector(".pushover-key");
        const saveKey = async () => {
            const key = keyInput.value.trim();
            if (key === (u.pushover_user_key || "")) return;
            try {
                const updated = await api("PATCH", `/api/admin/users/${u.id}`, { pushover_user_key: key });
                u.pushover_user_key = updated.pushover_user_key;
                if (u.id === me.id) { me.prefs.pushover_user_key = key || undefined; renderAccount(); }
                toast(key ? `Pushover key saved for ${u.username}` : `Pushover key removed for ${u.username}`, "success");
            } catch (e) { toast(e.message, "error"); }
        };
        card.querySelector(".btn-save-pushover").addEventListener("click", saveKey);
        keyInput.addEventListener("keydown", (e) => { if (e.key === "Enter") saveKey(); });

        card.querySelectorAll(".share-list select").forEach(sel => sel.addEventListener("change", async () => {
            try {
                await api("PUT", `/api/admin/devices/${sel.dataset.device}/shares/${u.id}`, { access: sel.value || null });
                const s = u.shares.find(x => x.device_id === sel.dataset.device);
                if (sel.value) {
                    if (s) s.can_edit = sel.value === "edit";
                    else u.shares.push({ device_id: sel.dataset.device, can_edit: sel.value === "edit" });
                } else {
                    u.shares = u.shares.filter(x => x.device_id !== sel.dataset.device);
                }
                card.querySelector(".access-count").textContent = `${u.shares.length} of ${devices.length}`;
                card.querySelector(".device-card-meta").textContent =
                    `${methods} · ${u.shares.length} AirTag${u.shares.length === 1 ? "" : "s"} · ${u.last_login_at ? "last seen " + timeAgo(u.last_login_at) : "never signed in"}`;
            } catch (e) { toast(e.message, "error"); }
        }));

        card.querySelector(".btn-reset-pw").addEventListener("click", async () => {
            const input = card.querySelector(".reset-pw");
            try {
                await api("POST", `/api/admin/users/${u.id}/reset-password`, { new_password: input.value });
                input.value = "";
                toast(`Password reset. ${u.username} will choose a new one at next sign-in.`, "success");
            } catch (e) { toast(e.message, "error"); }
        });

        card.querySelector(".btn-delete")?.addEventListener("click", () => {
            const body = card.querySelector(".device-card-body");
            if (body.querySelector(".confirm-bar")) return;
            const bar = document.createElement("div");
            bar.className = "confirm-bar";
            bar.innerHTML = `<span>Delete ${esc(u.username)}? Their places and alerts go too.</span>
                <button class="btn-confirm-yes">Delete</button><button class="btn-confirm-no">Cancel</button>`;
            bar.querySelector(".btn-confirm-yes").addEventListener("click", async () => {
                try {
                    await api("DELETE", `/api/admin/users/${u.id}`);
                    toast(`${u.username} deleted`, "success");
                    await loadUsers();
                } catch (e) { toast(e.message, "error"); }
            });
            bar.querySelector(".btn-confirm-no").addEventListener("click", () => bar.remove());
            body.appendChild(bar);
        });

        list.appendChild(card);
    });
}

// ── General tab extras (admin) ───────────────────────────
function initAdminSettings() {
    document.getElementById("btn-save-pushover").addEventListener("click", async () => {
        const input = document.getElementById("pushover-app-token");
        try {
            const r = await api("PUT", "/api/admin/notifications", { pushover_app_token: input.value.trim() });
            input.value = "";
            input.placeholder = r.pushover_app_token_set ? "set (unchanged)" : "not set";
            toast(r.pushover_app_token_set ? "Pushover token saved" : "Pushover token cleared", "success");
        } catch (e) { toast(e.message, "error"); }
    });

    document.getElementById("sso-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const body = {
            oidc_enabled: document.getElementById("sso-enabled").checked,
            public_url: document.getElementById("sso-public-url").value.trim(),
            oidc_provider_url: document.getElementById("sso-provider").value.trim(),
            oidc_client_id: document.getElementById("sso-client-id").value.trim(),
            oidc_client_secret: document.getElementById("sso-client-secret").value,
            oidc_button_label: document.getElementById("sso-label").value.trim(),
            oidc_scopes: document.getElementById("sso-scopes").value.trim(),
            oidc_auto_provision: document.getElementById("sso-auto-provision").checked,
        };
        try {
            renderSso(await api("PUT", "/api/admin/sso", body));
            authConfig = await api("GET", "/api/auth/config");
            renderAccount();
            toast("SSO settings saved", "success");
        } catch (err) { toast(err.message, "error"); }
    });
}

function renderSso(cfg) {
    document.getElementById("sso-enabled").checked = cfg.oidc_enabled;
    document.getElementById("sso-public-url").value = cfg.public_url || "";
    document.getElementById("sso-provider").value = cfg.oidc_provider_url || "";
    document.getElementById("sso-client-id").value = cfg.oidc_client_id || "";
    document.getElementById("sso-client-secret").value = "";
    document.getElementById("sso-client-secret").placeholder = cfg.oidc_client_secret_set ? "set (unchanged)" : "not set";
    document.getElementById("sso-label").value = cfg.oidc_button_label || "";
    document.getElementById("sso-scopes").value = cfg.oidc_scopes || "";
    document.getElementById("sso-auto-provision").checked = cfg.oidc_auto_provision;
    document.getElementById("sso-redirect-hint").textContent = `Redirect URI to register with your provider: ${cfg.redirect_uri_hint}`;
}

async function loadAdminSettings() {
    if (!isAdmin()) return;
    try {
        const [sso, notif, apple] = await Promise.all([
            fetchJSON("/api/admin/sso"), fetchJSON("/api/admin/notifications"), fetchJSON("/api/apple/status"),
        ]);
        renderSso(sso);
        document.getElementById("pushover-app-token").placeholder = notif.pushover_app_token_set ? "set (unchanged)" : "not set";
        document.getElementById("apple-account").textContent = apple.account || (apple.authenticated ? "Connected" : "Not connected");
    } catch {}
}

// ── Apple account setup (admin) ──────────────────────────
let selected2faMethod = null;

function showSetup() {
    hideAllPages();
    document.getElementById("page-setup").classList.add("active");
    document.getElementById("setup-not-admin").hidden = isAdmin();
}

function showApp() {
    document.getElementById("page-setup").classList.remove("active");
    document.getElementById("page-login").classList.remove("active");
    document.getElementById("bottom-nav").style.display = "";
    navigateTo("page-map");
    loadDevices();
    loadSettings();
    loadAlerts();
    loadAdminSettings();
}

function initAuth() {
    const loginBtn = document.getElementById("btn-login");
    const submitBtn = document.getElementById("btn-2fa-submit");

    loginBtn.addEventListener("click", handleLogin);
    submitBtn.addEventListener("click", handle2fa);

    // Enter key support
    document.getElementById("apple-password").addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleLogin();
    });
    document.getElementById("2fa-code").addEventListener("keydown", (e) => {
        if (e.key === "Enter") handle2fa();
    });
}

async function handleLogin() {
    const email = document.getElementById("apple-email").value.trim();
    const password = document.getElementById("apple-password").value;
    const errorEl = document.getElementById("login-error");
    const btn = document.getElementById("btn-login");

    if (!email || !password) return;

    errorEl.style.display = "none";
    btn.disabled = true;
    btn.textContent = "Signing in...";

    try {
        const data = await api("POST", "/api/apple/login", { email, password });

        if (data.status === "ok") {
            showApp();
        } else if (data.status === "2fa") {
            show2faStep(data.methods);
        } else {
            errorEl.textContent = data.message || "Login failed";
            errorEl.style.display = "";
        }
    } catch (e) {
        errorEl.textContent = e.message;
        errorEl.style.display = "";
    } finally {
        btn.disabled = false;
        btn.textContent = "Sign In";
    }
}

function show2faStep(methods) {
    document.getElementById("setup-login").style.display = "none";
    document.getElementById("setup-2fa").style.display = "";

    const container = document.getElementById("2fa-methods");
    container.innerHTML = "";

    methods.forEach(m => {
        const btn = document.createElement("button");
        btn.className = "method-btn" + (methods.length === 1 ? " selected" : "");
        btn.dataset.id = m.id;

        if (m.type === "trusted_device") {
            btn.textContent = "Trusted Device";
        } else {
            btn.textContent = "SMS to " + m.phone;
        }

        btn.addEventListener("click", () => {
            container.querySelectorAll(".method-btn").forEach(b => b.classList.remove("selected"));
            btn.classList.add("selected");
            selected2faMethod = m.id;
        });

        container.appendChild(btn);
    });

    if (methods.length === 1) {
        selected2faMethod = methods[0].id;
    }

    document.getElementById("2fa-code").focus();
}

async function handle2fa() {
    const code = document.getElementById("2fa-code").value.trim();
    const errorEl = document.getElementById("2fa-error");
    const btn = document.getElementById("btn-2fa-submit");

    if (!code || selected2faMethod === null) return;

    errorEl.style.display = "none";
    btn.disabled = true;
    btn.textContent = "Verifying...";

    try {
        const data = await api("POST", "/api/apple/2fa", { method_id: selected2faMethod, code });

        if (data.status === "ok") {
            showApp();
        } else {
            errorEl.textContent = data.message || "Verification failed";
            errorEl.style.display = "";
        }
    } catch (e) {
        errorEl.textContent = e.message;
        errorEl.style.display = "";
    } finally {
        btn.disabled = false;
        btn.textContent = "Verify";
    }
}
