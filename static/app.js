const map = L.map("map").setView([40, -95], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap contributors",
    maxZoom: 19,
}).addTo(map);

const markers = {};

async function fetchJSON(url) {
    const res = await fetch(url);
    return res.json();
}

async function loadDevices() {
    const devices = await fetchJSON("/api/devices");
    const list = document.getElementById("device-list");
    list.innerHTML = "";
    devices.forEach((d) => {
        const li = document.createElement("li");
        li.innerHTML = `<span class="device-dot" style="background:${d.color}"></span>${d.name}`;
        li.onclick = () => showDeviceHistory(d.id);
        list.appendChild(li);
    });
}

async function loadLatest() {
    const locations = await fetchJSON("/api/locations/latest");
    // Clear old markers
    Object.values(markers).forEach((m) => map.removeLayer(m));

    locations.forEach((loc) => {
        const marker = L.circleMarker([loc.latitude, loc.longitude], {
            radius: 8,
            color: loc.device_color || "#3388ff",
            fillOpacity: 0.8,
        })
            .addTo(map)
            .bindPopup(`<b>${loc.device_name}</b><br>${loc.timestamp}`);
        markers[loc.device_id] = marker;
    });

    if (locations.length > 0) {
        const bounds = L.latLngBounds(
            locations.map((l) => [l.latitude, l.longitude])
        );
        map.fitBounds(bounds, { padding: [50, 50] });
    }
}

async function loadStatus() {
    const status = await fetchJSON("/api/status");
    document.getElementById("status").textContent =
        `${status.devices} devices | ${status.locations} locations logged` +
        (status.last_fetch ? ` | Last: ${status.last_fetch}` : "");
}

async function showDeviceHistory(deviceId) {
    const locations = await fetchJSON(`/api/locations/${deviceId}?limit=500`);
    if (locations.length === 0) return;

    const latlngs = locations.map((l) => [l.latitude, l.longitude]);
    L.polyline(latlngs, { color: "#ff6600", weight: 2, opacity: 0.7 }).addTo(map);
    map.fitBounds(L.latLngBounds(latlngs), { padding: [50, 50] });
}

// Initial load
loadDevices();
loadLatest();
loadStatus();
