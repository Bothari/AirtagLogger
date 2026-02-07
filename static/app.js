// ── State ────────────────────────────────────────────────
let devices = [];
let selectedDevices = new Set();
let activeTimeRange = 24; // hours, 0 = all
let mapLayers = { markers: [], trails: [] };
let map;

// ── Init ─────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    initMap();
    initNav();
    initConfigPanel();
    initTimeRange();
    initSettingsTabs();
    initAddDeviceModal();
    loadDevices();
});

// ── Map ──────────────────────────────────────────────────
function initMap() {
    map = L.map("map", { zoomControl: true }).setView([40, -95], 4);
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
        maxZoom: 19,
    }).addTo(map);
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
        navigateTo("page-settings");
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
function initSettingsTabs() {
    const tabs = document.querySelectorAll(".tab-btn");
    tabs.forEach(btn => {
        btn.addEventListener("click", () => {
            tabs.forEach(t => t.classList.remove("active"));
            btn.classList.add("active");
            document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
            document.getElementById(btn.dataset.tab).classList.add("active");
        });
    });
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
    validateForm();
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
        const res = await fetch("/api/devices", { method: "POST", body: form });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            alert(err.detail || "Failed to add device");
            return;
        }
        closeAddModal();
        await loadDevices();
    } catch (e) {
        alert("Network error: " + e.message);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = "Add";
    }
}

// ── Load Devices ─────────────────────────────────────────
async function loadDevices() {
    try {
        devices = await fetchJSON("/api/devices");
    } catch {
        devices = [];
    }

    // Enable all by default
    if (selectedDevices.size === 0 && devices.length > 0) {
        devices.forEach(d => selectedDevices.add(d.id));
    }
    // Remove stale selections
    const ids = new Set(devices.map(d => d.id));
    selectedDevices.forEach(id => { if (!ids.has(id)) selectedDevices.delete(id); });
    // Add new devices
    devices.forEach(d => { if (!ids.has(d.id)) selectedDevices.add(d.id); });

    renderFilterList();
    renderSettingsDevices();
    updateConfigSummary();
    updateEmptyStates();
    refreshMap();
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

    devices.forEach(d => {
        const item = document.createElement("label");
        item.className = "device-filter-item";

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

        item.append(checkbox, dot, info);
        container.appendChild(item);
    });
}

// ── Settings Device List ─────────────────────────────────
function renderSettingsDevices() {
    const container = document.getElementById("settings-device-list");
    container.innerHTML = "";

    devices.forEach(d => {
        const card = document.createElement("div");
        card.className = "device-card";
        card.dataset.id = d.id;

        const created = d.created_at ? formatDate(d.created_at) : "";

        card.innerHTML = `
            <span class="device-card-swatch" style="background:${esc(d.color)}"></span>
            <div class="device-card-info">
                <div class="device-card-name">${esc(d.name)}</div>
                <div class="device-card-meta">${created ? "Added " + created : ""}</div>
            </div>
            <button class="btn-delete" title="Delete device">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path></svg>
            </button>`;

        const delBtn = card.querySelector(".btn-delete");
        delBtn.addEventListener("click", () => showDeleteConfirm(card, d));

        container.appendChild(card);
    });
}

function showDeleteConfirm(card, device) {
    // Remove any existing confirm bar
    const existing = card.querySelector(".confirm-bar");
    if (existing) { existing.remove(); return; }

    const bar = document.createElement("div");
    bar.className = "confirm-bar";
    bar.innerHTML = `<span>Delete "${esc(device.name)}"?</span>
        <button class="btn-confirm-yes">Delete</button>
        <button class="btn-confirm-no">Cancel</button>`;

    bar.querySelector(".btn-confirm-yes").addEventListener("click", async () => {
        try {
            const res = await fetch(`/api/devices/${device.id}`, { method: "DELETE" });
            if (!res.ok) throw new Error("Delete failed");
            selectedDevices.delete(device.id);
            await loadDevices();
        } catch (e) {
            alert("Failed to delete: " + e.message);
        }
    });
    bar.querySelector(".btn-confirm-no").addEventListener("click", () => bar.remove());

    // Insert after the card content
    card.appendChild(bar);
}

// ── Map Rendering ────────────────────────────────────────
function clearMapLayers() {
    mapLayers.markers.forEach(m => map.removeLayer(m));
    mapLayers.trails.forEach(t => map.removeLayer(t));
    mapLayers.markers = [];
    mapLayers.trails = [];
}

async function refreshMap() {
    clearMapLayers();

    const visible = devices.filter(d => selectedDevices.has(d.id));
    if (visible.length === 0) return;

    const allPoints = [];

    await Promise.all(visible.map(async (device) => {
        const params = new URLSearchParams({ limit: "1000" });
        if (activeTimeRange > 0) {
            const from = new Date(Date.now() - activeTimeRange * 3600 * 1000);
            params.set("from_ts", from.toISOString());
        }

        let locations;
        try {
            locations = await fetchJSON(`/api/locations/${device.id}?${params}`);
        } catch {
            return;
        }
        if (!locations.length) return;

        // Trail polyline
        const latlngs = locations.map(l => [l.latitude, l.longitude]);
        const trail = L.polyline(latlngs, {
            color: device.color,
            weight: 2.5,
            opacity: 0.5,
        }).addTo(map);
        mapLayers.trails.push(trail);

        // Latest position marker
        const latest = locations[0];
        const marker = L.circleMarker([latest.latitude, latest.longitude], {
            radius: 8,
            color: device.color,
            fillColor: device.color,
            fillOpacity: 0.9,
            weight: 2,
        }).addTo(map).bindPopup(
            `<b>${esc(device.name)}</b><br>
             ${latest.latitude.toFixed(5)}, ${latest.longitude.toFixed(5)}<br>
             <small>${formatTimestamp(latest.timestamp)}</small>`
        );
        mapLayers.markers.push(marker);

        // Update last-seen in filter panel
        updateLastSeen(device.id, latest.timestamp);

        allPoints.push(...latlngs);
    }));

    if (allPoints.length > 0) {
        map.fitBounds(L.latLngBounds(allPoints), { padding: [50, 50], maxZoom: 16 });
    }
}

function updateLastSeen(deviceId, timestamp) {
    const idx = devices.findIndex(d => d.id === deviceId);
    if (idx === -1) return;
    const items = document.querySelectorAll(".device-filter-item");
    if (items[idx]) {
        const el = items[idx].querySelector(".device-filter-lastseen");
        if (el) el.textContent = timeAgo(timestamp);
    }
}

// ── Helpers ──────────────────────────────────────────────
async function fetchJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
}

function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}

function formatDate(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatTimestamp(iso) {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
        month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
    });
}

function timeAgo(iso) {
    const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 60) return "just now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}
