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
    }
});
window.addEventListener("pagehide", () => {
    for (const stop of stopWatching) stop();
    stopWatching = [];
});
