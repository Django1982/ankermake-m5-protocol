function watchPrinterCamera(video, printerIndex, setStatus) {
    const url = new URL("/ws/video", location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("printer_index", printerIndex);

    let socket;
    let player;
    let retry;
    let waiting;
    let stopped = false;
    let unauthorized = false;
    let lastPlaybackTime = 0;

    function markWaiting() {
        clearTimeout(waiting);
        waiting = setTimeout(() => setStatus("Waiting for camera"), 8000);
    }

    function markLive() {
        clearTimeout(waiting);
        setStatus("Live");
    }

    function reportPlayback() {
        if (video.currentTime > lastPlaybackTime) markLive();
        lastPlaybackTime = video.currentTime;
    }

    function releasePlayer() {
        player?.destroy();
        player = null;
        lastPlaybackTime = 0;
    }

    function connect() {
        const connection = new WebSocket(url);
        socket = connection;
        socket.binaryType = "arraybuffer";
        setStatus("Connecting…");
        markWaiting();
        socket.onmessage = ({ data }) => {
            if (typeof data === "string") {
                if (JSON.parse(data).error === "unauthorized") {
                    unauthorized = true;
                    socket.close();
                }
                return;
            }
            if (!player) player = new AnkerVideoPlayer(video);
            player.enqueue(data);
        };
        socket.onclose = () => {
            if (socket !== connection) return;
            releasePlayer();
            clearTimeout(waiting);
            if (unauthorized) {
                setStatus("Camera access denied");
            } else if (!stopped) {
                setStatus("Camera disconnected. Reconnecting…");
                retry = setTimeout(connect, 2000);
            }
        };
    }

    video.addEventListener("playing", markLive);
    video.addEventListener("waiting", markWaiting);
    video.addEventListener("timeupdate", reportPlayback);
    connect();

    return () => {
        stopped = true;
        clearTimeout(retry);
        clearTimeout(waiting);
        video.removeEventListener("playing", markLive);
        video.removeEventListener("waiting", markWaiting);
        video.removeEventListener("timeupdate", reportPlayback);
        socket.onmessage = null;
        socket.onclose = null;
        socket.close();
        releasePlayer();
    };
}

function watchExternalCamera(image, tile, setStatus) {
    const printerIndex = tile.dataset.cameraIndex;
    const refreshMs = Number(tile.dataset.refreshSec) * 1000;
    let liveStream = tile.dataset.cameraMode === "stream";
    let retry;

    function load() {
        const endpoint = liveStream ? "stream" : "frame";
        image.src = `/api/camera/${endpoint}?printer_index=${printerIndex}&ts=${Date.now()}`;
    }

    image.onload = () => {
        image.hidden = false;
        setStatus(liveStream ? "Live" : "Snapshot preview");
        if (!liveStream) retry = setTimeout(load, refreshMs);
    };
    image.onerror = () => {
        setStatus("Waiting for camera");
        liveStream = false;
        retry = setTimeout(load, refreshMs);
    };
    load();

    return () => {
        clearTimeout(retry);
        image.onload = null;
        image.onerror = null;
        image.removeAttribute("src");
        image.hidden = true;
    };
}

const printerStateLabels = {
    idle: "Idle",
    preparing: "Preparing",
    pre_print: "Preparing",
    printing: "Printing",
    paused: "Paused",
    failed: "Print failed",
};

function formatCameraTemperature(current, target) {
    if (!Number.isFinite(current)) return "—";
    const measured = Math.round(current);
    return Number.isFinite(target) && target > 0
        ? `${measured} / ${Math.round(target)} °C`
        : `${measured} °C`;
}

function formatCameraTimeLeft(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "Time left unavailable";
    const minutes = Math.ceil(seconds / 60);
    const hours = Math.floor(minutes / 60);
    return `${hours ? `${hours}h ` : ""}${minutes % 60}m left`;
}

function watchPrinterStatus(tile) {
    const stateLabel = tile.querySelector("[data-print-state]");
    const file = tile.querySelector("[data-print-file]");
    const metrics = tile.querySelector("[data-print-metrics]");
    const nozzle = tile.querySelector("[data-nozzle]");
    const bed = tile.querySelector("[data-bed]");
    const progress = tile.querySelector("[data-print-progress]");
    const remaining = tile.querySelector("[data-print-remaining]");
    const params = new URLSearchParams({ printer_index: tile.dataset.cameraIndex });
    let stopped = false;
    let timer;
    let request;

    function render(data) {
        const age = data?.telemetry?.age_seconds;
        const available = data?.print && Number.isFinite(age) && age <= 30;
        const job = available ? data.print : null;
        const jobActive = job && (job.active || job.pending_start);
        const filename = jobActive ? job.last_filename : "";

        stateLabel.textContent = job ? (printerStateLabels[job.print_state] || "Unknown status") : "Printer status unavailable";
        file.textContent = filename || "";
        file.title = file.textContent;
        file.hidden = !filename;
        metrics.hidden = !available;
        if (!available) return;

        const temp = data.temperature || {};
        nozzle.textContent = formatCameraTemperature(temp.nozzle, temp.nozzle_target);
        bed.textContent = formatCameraTemperature(temp.bed, temp.bed_target);
        progress.hidden = !jobActive || !Number.isFinite(job.last_progress);
        progress.textContent = progress.hidden ? "" : `${Math.round(job.last_progress)}% complete`;
        remaining.hidden = !jobActive;
        remaining.textContent = jobActive ? formatCameraTimeLeft(job.remaining_seconds) : "";
    }

    async function refresh() {
        request = new AbortController();
        const timeout = setTimeout(() => request.abort(), 8000);
        try {
            const response = await fetch(`/api/printer/runtime-state?${params}`, {
                cache: "no-store",
                signal: request.signal,
            });
            const data = response.ok ? await response.json() : null;
            if (!stopped) render(data);
        } catch (error) {
            if (!stopped) render(null);
        } finally {
            clearTimeout(timeout);
            if (!stopped) timer = setTimeout(refresh, 5000);
        }
    }

    refresh();
    return () => {
        stopped = true;
        clearTimeout(timer);
        request.abort();
    };
}

let stopWatching = [];
window.addEventListener("pageshow", () => {
    for (const tile of document.querySelectorAll("[data-camera-index]")) {
        const status = tile.querySelector("[data-camera-status]");
        const setStatus = (message) => {
            if (status.textContent !== message) status.textContent = message;
        };
        const video = tile.querySelector("video");
        const image = tile.querySelector("img");
        if (video) stopWatching.push(watchPrinterCamera(video, tile.dataset.cameraIndex, setStatus));
        if (image) stopWatching.push(watchExternalCamera(image, tile, setStatus));
        stopWatching.push(watchPrinterStatus(tile));
    }
});
window.addEventListener("pagehide", () => {
    for (const stop of stopWatching) stop();
    stopWatching = [];
});
