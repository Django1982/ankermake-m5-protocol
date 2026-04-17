$(function () {
    /**
     * Updates the Copywrite year on document ready
     */
    $("#copyYear").text(new Date().getFullYear());

    /**
     * Redirect page when modal dialog is shown
     */
    var popupModal = document.getElementById("popupModal");

    if (popupModal) {
        popupModal.addEventListener("shown.bs.modal", function (e) {
            window.location.href = $("#reload").data("href");
        });
    }

    /**
     * On click of an element with attribute "data-clipboard-src", updates clipboard with text from that element
     */
    if (navigator.clipboard) {
        /* Clipboard support present: link clipboard icons to source object */
        $("[data-clipboard-src]").each(function (i, elm) {
            $(elm).on("click", function () {
                const src = $(elm).attr("data-clipboard-src");
                if (!/^#[A-Za-z][A-Za-z0-9_:.~-]*$/.test(src || "")) {
                    console.warn("Ignored invalid clipboard source");
                    return;
                }
                const source = document.getElementById(src.slice(1));
                if (!source) {
                    console.warn("Clipboard source not found");
                    return;
                }
                const value = source.textContent || "";
                navigator.clipboard.writeText(value);
                console.log("Copied value to clipboard");
            });
        });
    } else {
        /* Clipboard support missing: remove clipboard icons to minimize confusion */
        $("[data-clipboard-src]").remove();
    };

    /**
     * Initializes bootstrap alerts and sets a timeout for when they should automatically close
     */
    $(".alert").each(function (i, alert) {
        var bsalert = new bootstrap.Alert(alert);
        setTimeout(() => {
            bsalert.close();
        }, +alert.getAttribute("data-timeout"));
    });

    /**
     * Get temperature from input
     * @param {number} temp Temperature in 1/100 °C
     * @returns {number} temperature in °C, null if temp is not a number
     */
    function getTemp(temp) {
        return (typeof(temp) === "number") ? (temp / 100) : null;
    }

    /**
     * Get rounded temperature from input
     * @param {number} temp Temperature in 1/100 °C
     * @returns {number} Rounded temperature in °C, null if temp is not a number
     */
    function getTempRounded(temp) {
        return (typeof(temp) === "number") ? Math.round(temp / 100) : null;
    }

    function formatServiceTempValue(temp) {
        return Number.isFinite(Number(temp)) ? `${Math.round(Number(temp))}°C` : "--";
    }

    /**
     * Calculate the percentage between two numbers
     * @param {number} layer
     * @param {number} total
     * @returns {number} percentage
     */
    function getPercentage(progress) {
        return Math.round(progress);
    }

    /**
     * Normalize a time value in seconds for display.
     * @param {number|string|null|undefined} value
     * @returns {number|null} Whole seconds, or null when the input is not usable
     */
    function normalizeTimeSeconds(value) {
        if (typeof(value) === "number") {
            return Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;
        }
        if (typeof(value) === "string") {
            const trimmed = value.trim();
            if (!trimmed) {
                return null;
            }
            const parsed = Number(trimmed);
            return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : null;
        }
        return null;
    }

    /**
     * Convert time in seconds to hours, minutes, and seconds format
     * @param {number} totalseconds
     * @returns {string|null} Formatted time string, or null when the input is invalid
     */
    function getTime(totalseconds) {
        const total = normalizeTimeSeconds(totalseconds);
        if (total === null) {
            return null;
        }

        const hours = Math.floor(total / 3600);
        const minutes = Math.floor((total % 3600) / 60);
        const seconds = total % 60;

        const timeString =
            `${hours.toString().padStart(2, "0")}:` +
            `${minutes.toString().padStart(2, "0")}:` +
            `${seconds.toString().padStart(2, "0")}`;

        return timeString;
    }

    /**
     * Convert bytes to a human readable string.
     * @param {number} bytes
     * @returns {string}
     */
    function formatBytes(bytes) {
        if (!bytes) {
            return "0 B";
        }
        const units = ["B", "KB", "MB", "GB", "TB"];
        let size = bytes;
        let unit = 0;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit++;
        }
        const precision = size >= 10 || unit === 0 ? 0 : 1;
        return `${size.toFixed(precision)} ${units[unit]}`;
    }

    function flash_message(message, category = "info", timeout = 7500, options = {}) {
        const messages = $("#messages");
        if (!messages.length) {
            console.log(`[${category}] ${message}`);
            return;
        }
        const sticky = timeout === 0 || options.sticky === true;
        const stickyKey = sticky ? String(options.key || "") : "";
        if (stickyKey && _activeStickyAlerts.has(stickyKey)) {
            return;
        }
        const alert = $("<div>");
        alert.addClass(`alert alert-${category} alert-dismissible fade show`);
        alert.attr("role", "alert");
        if (!sticky) {
            alert.attr("data-timeout", timeout);
        } else if (stickyKey) {
            _activeStickyAlerts.add(stickyKey);
            alert.attr("data-sticky-key", stickyKey);
        }

        const body = $("<div>");
        body.addClass("d-flex align-items-center justify-content-between gap-3");

        const messageText = $("<div>");
        messageText.addClass("flex-grow-1");
        messageText.text(String(message || ""));
        body.append(messageText);

        if (sticky) {
            const ackBtn = $("<button>");
            ackBtn.attr("type", "button");
            ackBtn.addClass("btn btn-sm btn-light flex-shrink-0");
            ackBtn.attr("data-bs-dismiss", "alert");
            ackBtn.text("OK");
            body.append(ackBtn);
        } else {
            const closeBtn = $("<button>");
            closeBtn.attr("type", "button");
            closeBtn.addClass("btn-close btn-sm btn-close-white flex-shrink-0");
            closeBtn.attr("data-bs-dismiss", "alert");
            closeBtn.attr("aria-label", "Close");
            body.append(closeBtn);
        }

        alert.append(body);
        messages.append(alert);

        const bsalert = new bootstrap.Alert(alert[0]);
        if (stickyKey) {
            alert.on("closed.bs.alert", function () {
                _activeStickyAlerts.delete(stickyKey);
            });
        }
        if (!sticky && timeout > 0) {
            setTimeout(() => {
                bsalert.close();
            }, timeout);
        }
    }

    /**
     * Escape a string for safe insertion into HTML to prevent XSS.
     * @param {string} str
     * @returns {string} HTML-escaped string
     */
    function escapeHtml(str) {
        const node = document.createTextNode(String(str));
        const div = document.createElement("div");
        div.appendChild(node);
        return div.innerHTML;
    }

    const HOME_CONSOLE_INITIAL_LIMIT = 200;
    const HOME_CONSOLE_MAX_LINES = 400;
    const HOME_CONSOLE_POLL_MS = 2000;
    const PRINTER_ALERT_POLL_MS = 4000;
    const PRINTER_RUNTIME_POLL_MS = 5000;

    let _homeConsoleEntries = [];
    let _homeConsoleLastId = 0;
    let _homeConsoleLoading = false;
    let _homeConsoleInterval = null;
    let _homeConsoleFilter = "all";
    let _homeConsoleAutoScrollPaused = false;
    let _homeConsoleClearedBeforeId = 0;
    let _homeConsoleWasCleared = false;
    let _printerAlertLastId = 0;
    let _printerAlertPollStarted = false;
    let _printerRuntimeLoading = false;
    let _printerRuntimePollInterval = null;
    const _activeStickyAlerts = new Set();

    function setHomeConsoleStatus(message) {
        const status = $("#home-console-status");
        if (status.length) {
            status.text(message);
        }
    }

    function getHomeConsoleVisibleEntries() {
        return _homeConsoleEntries.filter(entry => {
            if (!entry || typeof entry.id !== "number" || entry.id <= _homeConsoleClearedBeforeId) {
                return false;
            }
            return homeConsoleMatchesFilter(entry, _homeConsoleFilter);
        });
    }

    function homeConsoleMatchesFilter(entry, filter) {
        if (!entry) {
            return false;
        }

        const text = String(entry.text || "").toLowerCase();
        if (filter === "all") {
            return true;
        }
        if (filter === "mqtt") {
            return text.includes("mqtt") || text.includes("mqttqueue");
        }
        if (filter === "pppp") {
            return text.includes("pppp");
        }
        if (filter === "video") {
            return text.includes("video")
                || text.includes("/ws/video")
                || text.includes("snapshot")
                || text.includes("jmuxer");
        }
        if (filter === "upload") {
            return text.includes("upload")
                || text.includes("filetransfer")
                || text.includes("transfer")
                || text.includes("gcode file");
        }
        if (filter === "error") {
            return /^\[(e|w|!)\]/i.test(text)
                || text.includes("error")
                || text.includes("exception")
                || text.includes("traceback");
        }
        return true;
    }

    function updateHomeConsoleStatus() {
        const visibleEntries = getHomeConsoleVisibleEntries();
        const totalEntries = _homeConsoleEntries.filter(entry => entry && entry.id > _homeConsoleClearedBeforeId).length;
        const filterLabel = _homeConsoleFilter.toUpperCase();
        const scrollLabel = _homeConsoleAutoScrollPaused ? "auto-scroll paused" : "auto-scroll live";
        setHomeConsoleStatus(`Showing ${visibleEntries.length} of ${totalEntries} line(s) - ${filterLabel} - ${scrollLabel}`);
    }

    function updateHomeConsoleFilterButtons() {
        $("[data-home-console-filter]").each(function () {
            const button = $(this);
            const isActive = button.data("home-console-filter") === _homeConsoleFilter;
            button.toggleClass("active", isActive);
            button.attr("aria-pressed", isActive ? "true" : "false");
        });
    }

    function updateHomeConsoleAutoScrollButton() {
        const button = $("#home-console-autoscroll-toggle");
        if (!button.length) {
            return;
        }
        const label = _homeConsoleAutoScrollPaused ? "Resume Auto-Scroll" : "Pause Auto-Scroll";
        const icon = _homeConsoleAutoScrollPaused ? "play-circle" : "pause-circle";
        button
            .toggleClass("btn-outline-warning", _homeConsoleAutoScrollPaused)
            .toggleClass("btn-outline-secondary", !_homeConsoleAutoScrollPaused)
            .attr("aria-pressed", _homeConsoleAutoScrollPaused ? "true" : "false")
            .html(`<i class="bi bi-${icon}"></i> ${label}`);
    }

    function mergeHomeConsoleEntries(entries, replace = false) {
        if (replace) {
            _homeConsoleEntries = Array.isArray(entries) ? entries.slice(-HOME_CONSOLE_MAX_LINES) : [];
        } else if (Array.isArray(entries) && entries.length) {
            _homeConsoleEntries = _homeConsoleEntries.concat(entries).slice(-HOME_CONSOLE_MAX_LINES);
        }

        if (_homeConsoleClearedBeforeId > 0) {
            _homeConsoleEntries = _homeConsoleEntries.filter(entry => entry && entry.id > _homeConsoleClearedBeforeId);
        }
    }

    function renderHomeConsoleEntries() {
        const pre = document.getElementById("home-console-pre");
        const content = document.getElementById("home-console-content");
        if (!content) {
            return;
        }

        const wasNearBottom = !pre
            || (pre.scrollHeight - pre.scrollTop - pre.clientHeight) < 40;

        const visibleEntries = getHomeConsoleVisibleEntries();
        const totalEntries = _homeConsoleEntries.filter(entry => entry && entry.id > _homeConsoleClearedBeforeId).length;

        if (!visibleEntries.length) {
            if (_homeConsoleWasCleared && totalEntries === 0) {
                content.innerHTML = "Console cleared. New output will appear here.";
            } else if (totalEntries > 0) {
                content.innerHTML = "No console lines match the current filter.";
            } else {
                content.innerHTML = "No console output captured yet.";
            }
        } else {
            content.innerHTML = visibleEntries
                .map(entry => escapeHtml(entry && entry.text ? entry.text : ""))
                .join("\n");
        }

        updateHomeConsoleStatus();

        if (pre && !_homeConsoleAutoScrollPaused && wasNearBottom) {
            pre.scrollTop = pre.scrollHeight;
        }
    }

    async function fetchHomeConsoleLogs(afterId = null, limit = HOME_CONSOLE_INITIAL_LIMIT) {
        const params = new URLSearchParams({ limit: String(limit) });
        if (afterId !== null && afterId !== undefined) {
            params.set("after", String(afterId));
        }
        const resp = await fetch(`/api/console/logs?${params.toString()}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        return data;
    }

    async function loadHomeConsoleHistory() {
        if (_homeConsoleLoading) {
            return;
        }
        _homeConsoleLoading = true;
        try {
            const data = await fetchHomeConsoleLogs(null, HOME_CONSOLE_INITIAL_LIMIT);
            mergeHomeConsoleEntries(data.entries || [], true);
            _homeConsoleLastId = data.last_id || 0;
            renderHomeConsoleEntries();
        } catch (err) {
            setHomeConsoleStatus(`Console viewer error: ${err.message}`);
        } finally {
            _homeConsoleLoading = false;
        }
    }

    async function pollHomeConsoleUpdates() {
        if (_homeConsoleLoading) {
            return;
        }
        _homeConsoleLoading = true;
        try {
            const data = await fetchHomeConsoleLogs(_homeConsoleLastId, HOME_CONSOLE_INITIAL_LIMIT);
            if (data.truncated) {
                mergeHomeConsoleEntries(data.entries || [], true);
                renderHomeConsoleEntries();
            } else if (Array.isArray(data.entries) && data.entries.length) {
                mergeHomeConsoleEntries(data.entries, false);
                _homeConsoleWasCleared = false;
                renderHomeConsoleEntries();
            }
            _homeConsoleLastId = data.last_id || _homeConsoleLastId;
        } catch (err) {
            setHomeConsoleStatus(`Console viewer error: ${err.message}`);
        } finally {
            _homeConsoleLoading = false;
        }
    }

    function startHomeConsoleViewer() {
        if (!document.getElementById("home-console-content")) {
            return;
        }
        updateHomeConsoleFilterButtons();
        updateHomeConsoleAutoScrollButton();
        loadHomeConsoleHistory();
        if (_homeConsoleInterval) {
            return;
        }
        _homeConsoleInterval = setInterval(pollHomeConsoleUpdates, HOME_CONSOLE_POLL_MS);
    }

    function stopHomeConsoleViewer() {
        if (_homeConsoleInterval) {
            clearInterval(_homeConsoleInterval);
            _homeConsoleInterval = null;
        }
    }

    $(document).on("click", "#home-console-clear", function () {
        _homeConsoleClearedBeforeId = _homeConsoleLastId;
        _homeConsoleWasCleared = true;
        _homeConsoleEntries = _homeConsoleEntries.filter(entry => entry && entry.id > _homeConsoleClearedBeforeId);
        renderHomeConsoleEntries();
    });

    $(document).on("click", "#home-console-autoscroll-toggle", function () {
        _homeConsoleAutoScrollPaused = !_homeConsoleAutoScrollPaused;
        updateHomeConsoleAutoScrollButton();
        renderHomeConsoleEntries();
    });

    $(document).on("click", "[data-home-console-filter]", function () {
        _homeConsoleFilter = String($(this).data("home-console-filter") || "all");
        updateHomeConsoleFilterButtons();
        renderHomeConsoleEntries();
    });

    /**
     * Calculates the AnkerMake M5 Speed ratio ("X-factor")
     * @param {number} speed - The speed value in mm/s
     * @return {number} The speed factor in units of "X" (50mm/s)
     */
    function getSpeedFactor(speed) {
        return `X${speed / 50}`;
    }

    function getFilamentStateLabel(value, progress, stepLen) {
        const parsedValue = Number(value);
        const parsedProgress = Number(progress);
        const parsedStepLen = Number(stepLen);
        const hasValue = Number.isFinite(parsedValue);
        const hasProgress = Number.isFinite(parsedProgress);
        const hasStepLen = Number.isFinite(parsedStepLen);

        if ((hasProgress && parsedProgress > 0) || (hasStepLen && parsedStepLen > 0)) {
            return "Changing";
        }
        if (hasValue && parsedValue === 0) {
            return "Loaded";
        }
        if (!hasValue) {
            return "Unknown";
        }
        return "Changing";
    }

    function getFilamentStateDetail(label, progress) {
        if (label !== "Changing") {
            return null;
        }
        const parsedProgress = Number(progress);
        if (Number.isFinite(parsedProgress) && parsedProgress > 0 && parsedProgress < 100) {
            return `Filament swap in progress (${Math.round(parsedProgress)}%)`;
        }
        return "Filament swap in progress";
    }

    function isFilamentRunoutEvent(data) {
        if (!data || typeof data !== "object") {
            return false;
        }
        if (data.commandType === 1085 && String(data.errorCode || "") === "0xFF01030001") {
            return true;
        }
        return data.commandType === 1000
            && Number(data.subType) === 2
            && Number(data.value) === 6;
    }

    const _filamentStatus = {
        label: "Unknown",
        detail: null,
        issue: null,
        pauseReason: null,
        pendingRunout: false,
    };
    const _timelapseRuntime = {
        capturing: false,
        activeCapture: false,
        paused: false,
        pauseReason: null,
        manualPaused: false,
        recovering: false,
        detail: null,
        promptStart: false,
        promptFilename: null,
        resumeAvailable: false,
        resumeFrameCount: 0,
    };
    const _cameraState = {
        source: "printer",
        effectiveSource: null,
        printerSupported: false,
        featureAvailable: false,
        detail: null,
        externalName: null,
        externalConfigured: false,
        externalRefreshSec: 3,
        externalStreamPreview: false,
        previewError: null,
    };
    let _cameraSettingsLoading = false;
    let _externalCameraPreviewTimer = null;
    let _externalCameraPreviewToken = 0;
    let _externalCameraPreviewObjectUrl = null;
    let _externalCameraPreviewEnabled = false;
    let _externalCameraPreviewContextKey = null;
    let _externalCameraPreviewStreamActive = false;
    let videoEnabled = false;

    function setFilamentState(label, detail = null, detailTone = null) {
        const el = $("#filament-state");
        const detailEl = $("#filament-state-detail");
        if (!el.length) {
            return;
        }

        const value = String(label || "Unknown");
        el.text(value);
        el.removeClass("text-success text-warning text-danger text-muted");
        if (value === "Loaded") {
            el.addClass("text-success");
        } else if (value === "Not Loaded") {
            el.addClass("text-danger");
        } else if (value === "Changing") {
            el.addClass("text-warning");
        } else {
            el.addClass("text-muted");
        }

        if (detailEl.length) {
            const detailText = String(detail || "").trim();
            detailEl.removeClass("d-none text-danger text-warning text-muted text-success");
            detailEl.text(detailText);
            detailEl.toggleClass("d-none", !detailText);
            if (detailText) {
                detailEl.addClass(`text-${detailTone || "muted"}`);
            }
        }
    }

    function renderFilamentStatus() {
        const pausedForFilament = _currentPrintState === PRINT_STATE.PAUSED && !!_filamentStatus.pauseReason;
        const unverifiedFilamentState = _currentPrintState !== PRINT_STATE.PRINTING
            && _currentPrintState !== PRINT_STATE.PAUSED
            && _filamentStatus.label === "Loaded"
            && !_filamentStatus.issue;
        let label = _filamentStatus.label || "Unknown";
        let detail = _filamentStatus.detail || null;
        let detailTone = "muted";

        if (unverifiedFilamentState) {
            label = "Unknown";
            detail = detail || "Filament presence cannot be confirmed until the printer starts printing.";
            detailTone = "muted";
        }
        if (!detail && pausedForFilament && _filamentStatus.label === "Loaded") {
            detail = "Filament loaded. Resume the print when ready.";
            detailTone = "success";
        }
        if (!detail && pausedForFilament) {
            detail = `Paused: ${_filamentStatus.pauseReason}`;
            detailTone = "warning";
        }
        if (_filamentStatus.issue === "runout") {
            detailTone = pausedForFilament ? "warning" : "danger";
        } else if (label === "Changing") {
            detailTone = "warning";
        } else if (label === "Loaded" && detail) {
            detailTone = "success";
        }

        setFilamentState(label, detail, detailTone);
    }

    function renderTimelapseRuntimeStatus() {
        const el = $("#timelapse-runtime-detail");
        if (!el.length) {
            return;
        }

        const detailText = String(_timelapseRuntime.detail || "").trim();
        el.removeClass("d-none text-warning text-muted text-success");
        el.text(detailText);

        if (!detailText) {
            el.addClass("d-none");
            return;
        }

        if (_timelapseRuntime.recovering) {
            el.addClass("text-warning");
        } else if (_timelapseRuntime.capturing) {
            el.addClass("text-success");
        } else {
            el.addClass("text-muted");
        }
    }

    function renderTimelapseControls() {
        const startBtn = $("#timelapse-control-start");
        const pauseBtn = $("#timelapse-control-pause");
        const resumeBtn = $("#timelapse-control-resume");
        const stopBtn = $("#timelapse-control-stop");
        const statusEl = $("#timelapse-control-status");

        if (!startBtn.length || !pauseBtn.length || !resumeBtn.length || !stopBtn.length || !statusEl.length) {
            return;
        }

        const printActive = _currentPrintState === PRINT_STATE.CALIBRATING
            || _currentPrintState === PRINT_STATE.PENDING_START
            || _currentPrintState === PRINT_STATE.PRINTING
            || _currentPrintState === PRINT_STATE.PAUSED;
        const canStart = printActive && !_timelapseRuntime.capturing;
        const canPause = _timelapseRuntime.capturing && !_timelapseRuntime.paused;
        const canResume = _timelapseRuntime.capturing && _timelapseRuntime.manualPaused;
        const canStop = _timelapseRuntime.capturing || _timelapseRuntime.activeCapture;

        startBtn.prop("disabled", !canStart);
        pauseBtn.prop("disabled", !canPause);
        resumeBtn.prop("disabled", !canResume);
        stopBtn.prop("disabled", !canStop);

        let message = "Use these controls to start, pause, resume, or stop timelapse capture for the active print.";
        if (_timelapseRuntime.promptStart && !_timelapseRuntime.capturing) {
            message = "An active print is waiting for timelapse confirmation. Use Start or the action card to begin capture.";
        } else if (_timelapseRuntime.recovering) {
            message = "Timelapse is recovering the camera stream.";
        } else if (_timelapseRuntime.capturing && _timelapseRuntime.paused) {
            message = _timelapseRuntime.pauseReason === "manual"
                ? "Timelapse is paused manually."
                : `Timelapse is paused: ${_timelapseRuntime.detail || "capture is waiting to resume."}`;
        } else if (_timelapseRuntime.capturing) {
            message = "Timelapse capture is running for the active print.";
        } else if (_timelapseRuntime.activeCapture) {
            message = "Timelapse frames are still saved for this print, but capture is not actively running.";
        } else if (!printActive) {
            message = "Start a print to enable manual timelapse controls.";
        }

        statusEl.text(message);
    }

    function renderTimelapseActionCard() {
        const card = document.getElementById("timelapse-action-card");
        const title = document.getElementById("timelapse-action-title");
        const detail = document.getElementById("timelapse-action-detail");
        const startBtn = document.getElementById("timelapse-action-start");
        const dismissBtn = document.getElementById("timelapse-action-dismiss");
        if (!card || !title || !detail || !startBtn || !dismissBtn) {
            return;
        }

        if (!_timelapseRuntime.promptStart || _timelapseRuntime.capturing) {
            card.style.display = "none";
            return;
        }

        const fileName = String(_timelapseRuntime.promptFilename || "this print").trim() || "this print";
        const frameCount = Number(_timelapseRuntime.resumeFrameCount || 0);
        const canResume = !!_timelapseRuntime.resumeAvailable && frameCount > 0;
        title.textContent = canResume
            ? "Resume timelapse for active print"
            : "Start timelapse for active print";
        detail.textContent = canResume
            ? `${fileName} already has ${frameCount} saved frame${frameCount === 1 ? "" : "s"}. Continue or dismiss this pending capture.`
            : `${fileName} is already printing. Continue or dismiss timelapse capture for this print.`;
        startBtn.textContent = canResume ? "Continue Timelapse" : "Start Timelapse";
        card.style.display = "";
    }

    async function sendTimelapseCurrentAction(endpoint, successMessage) {
        const resp = await fetch(withActivePrinterQuery(endpoint), { method: "POST" });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        applyRuntimeState(data);
        if (successMessage) {
            flash_message(successMessage, "success", 4000);
        }
    }

    function applyCameraRuntimeState(camera) {
        if (!camera || typeof camera !== "object") {
            return;
        }
        _cameraState.source = String(camera.source || _cameraState.source || "printer");
        _cameraState.effectiveSource = camera.effective_source || null;
        _cameraState.printerSupported = !!camera.printer_supported;
        _cameraState.featureAvailable = !!camera.feature_available;
        _cameraState.detail = camera.detail || null;
        _cameraState.externalName = camera.external_name || null;
        _cameraState.externalConfigured = !!camera.external_configured;
        _cameraState.externalRefreshSec = Math.max(1, Number(camera.external_refresh_sec || 3));
        _cameraState.externalStreamPreview = !!camera.external_stream_preview;
        _cameraState.previewError = null;
        renderCameraUi();
    }

    function renderCameraStatusText() {
        let detail = String(_cameraState.detail || "").trim();
        if (_cameraState.effectiveSource === "external" && !_externalCameraPreviewEnabled) {
            const disabledDetail = "External camera preview is disabled. Click Enable Preview to start live view.";
            detail = detail ? `${detail} ${disabledDetail}` : disabledDetail;
        }
        if (_cameraState.previewError && _cameraState.effectiveSource === "external") {
            const previewDetail = `Preview error: ${_cameraState.previewError}`;
            detail = detail ? `${detail} ${previewDetail}` : previewDetail;
        }
        if (_cameraState.effectiveSource === "printer" && typeof videoEnabled !== "undefined" && !videoEnabled) {
            detail = detail ? `${detail} Enable printer video to start live view.` : "Enable printer video to start live view.";
        }

        const status = $("#camera-source-status");
        status
            .text(detail || "No camera source selected.")
            .toggleClass("text-danger", !!_cameraState.previewError && _cameraState.effectiveSource === "external")
            .toggleClass("text-muted", !(_cameraState.previewError && _cameraState.effectiveSource === "external"));
    }

    function stopExternalCameraPreview(resetImage = true) {
        _externalCameraPreviewToken += 1;
        if (_externalCameraPreviewTimer) {
            clearTimeout(_externalCameraPreviewTimer);
            _externalCameraPreviewTimer = null;
        }
        if (_externalCameraPreviewObjectUrl) {
            URL.revokeObjectURL(_externalCameraPreviewObjectUrl);
            _externalCameraPreviewObjectUrl = null;
        }
        const img = document.getElementById("external-camera-preview");
        if (img) {
            img.onload = null;
            img.onerror = null;
            if (_externalCameraPreviewStreamActive) {
                img.src = "";
                _externalCameraPreviewStreamActive = false;
            } else if (resetImage) {
                img.src = "/static/img/load-screen.svg";
            }
        }
    }

    function setExternalCameraPreviewEnabled(enabled) {
        _externalCameraPreviewEnabled = !!enabled;
        if (!_externalCameraPreviewEnabled) {
            _cameraState.previewError = null;
        }
        renderCameraUi();
    }

    function ensureExternalCameraControls() {
        const printerControls = $("#printer-camera-controls");
        let controls = $("#external-camera-controls");

        if (!controls.length && printerControls.length) {
            controls = $(`
                <div id="external-camera-controls" class="d-none mt-2">
                    <div class="row g-2"></div>
                </div>
            `);
            controls.insertAfter(printerControls);
        }

        if (!controls.length) {
            return $();
        }

        let row = controls.find(".row.g-2").first();
        if (!row.length) {
            row = $('<div class="row g-2"></div>');
            controls.append(row);
        }

        if (!controls.find("#external-preview-toggle").length) {
            const toggleCol = $(`
                <div class="col-6">
                    <button class="w-100 btn btn-secondary camera-control-btn external-preview-toggle" id="external-preview-toggle" aria-pressed="false">
                        <i class="bi bi-camera-video"></i> Enable Preview
                    </button>
                </div>
            `);
            const snapshotCol = row.find("#snapshot-btn-secondary").closest(".col-6");
            if (snapshotCol.length) {
                toggleCol.insertBefore(snapshotCol);
            } else {
                row.append(toggleCol);
            }
        }

        if (!controls.find("#snapshot-btn-secondary").length) {
            row.append(`
                <div class="col-6">
                    <button class="w-100 btn btn-secondary camera-control-btn" id="snapshot-btn-secondary" title="Download Snapshot">
                        <i class="bi bi-camera"></i> Snapshot
                    </button>
                </div>
            `);
        }

        return controls;
    }

    function renderExternalCameraPreviewToggle() {
        ensureExternalCameraControls();
        const buttons = $(".external-preview-toggle");
        if (!buttons.length) {
            return;
        }
        if (_externalCameraPreviewEnabled) {
            buttons
                .html('<i class="bi bi-camera-video-off"></i> Disable Preview')
                .attr("aria-pressed", "true");
        } else {
            buttons
                .html('<i class="bi bi-camera-video"></i> Enable Preview')
                .attr("aria-pressed", "false");
        }
    }

    function scheduleExternalCameraPreview(delayMs) {
        if (_cameraState.effectiveSource !== "external" || !_externalCameraPreviewEnabled) {
            return;
        }
        _externalCameraPreviewStreamActive = false;
        if (_externalCameraPreviewTimer) {
            clearTimeout(_externalCameraPreviewTimer);
        }
        _externalCameraPreviewTimer = setTimeout(function () {
            const img = document.getElementById("external-camera-preview");
            if (!img || _cameraState.effectiveSource !== "external") {
                return;
            }
            const token = _externalCameraPreviewToken;
            const refreshMs = Math.max(1000, Math.round(_cameraState.externalRefreshSec * 1000));
            const startedAt = Date.now();
            const scheduleNext = function () {
                const elapsedMs = Date.now() - startedAt;
                scheduleExternalCameraPreview(Math.max(0, refreshMs - elapsedMs));
            };
            fetch(`/api/camera/frame?printer_index=${encodeURIComponent(getActivePrinterIndex())}&ts=${Date.now()}`, {
                cache: "no-store",
            })
                .then(async (resp) => {
                    if (!resp.ok) {
                        const data = await resp.json().catch(() => ({}));
                        throw new Error(data.error || `External camera preview failed (HTTP ${resp.status})`);
                    }
                    return resp.blob();
                })
                .then((blob) => {
                    if (token !== _externalCameraPreviewToken) {
                        return;
                    }
                    const nextUrl = URL.createObjectURL(blob);
                    img.onload = function () {
                        if (token !== _externalCameraPreviewToken) {
                            URL.revokeObjectURL(nextUrl);
                            return;
                        }
                        if (_externalCameraPreviewObjectUrl) {
                            URL.revokeObjectURL(_externalCameraPreviewObjectUrl);
                        }
                        _externalCameraPreviewObjectUrl = nextUrl;
                        _cameraState.previewError = null;
                        renderCameraStatusText();
                        scheduleNext();
                    };
                    img.onerror = function () {
                        URL.revokeObjectURL(nextUrl);
                        if (token !== _externalCameraPreviewToken) {
                            return;
                        }
                        _cameraState.previewError = "The external camera returned a frame that could not be displayed.";
                        renderCameraStatusText();
                        scheduleNext();
                    };
                    img.src = nextUrl;
                })
                .catch((err) => {
                    if (token !== _externalCameraPreviewToken) {
                        return;
                    }
                    _cameraState.previewError = err && err.message ? err.message : String(err);
                    renderCameraStatusText();
                    scheduleNext();
                });
        }, Math.max(0, delayMs || 0));
    }

    function startExternalCameraStreamPreview() {
        const img = document.getElementById("external-camera-preview");
        if (!img || _cameraState.effectiveSource !== "external" || !_externalCameraPreviewEnabled) {
            return;
        }
        const token = _externalCameraPreviewToken;
        const streamUrl = `/api/camera/stream?printer_index=${encodeURIComponent(getActivePrinterIndex())}&ts=${Date.now()}`;
        _externalCameraPreviewStreamActive = true;
        img.onload = function () {
            if (token !== _externalCameraPreviewToken) {
                return;
            }
            _cameraState.previewError = null;
            renderCameraStatusText();
        };
        img.onerror = function () {
            if (token !== _externalCameraPreviewToken) {
                return;
            }
            _externalCameraPreviewStreamActive = false;
            _cameraState.previewError = "External camera live stream ended; falling back to snapshot preview.";
            renderCameraStatusText();
            scheduleExternalCameraPreview(1000);
        };
        img.src = streamUrl;
    }

    function renderCameraUi() {
        const select = $("#camera-source-select");
        if (select.length) {
            const printerOption = select.find('option[value="printer"]');
            printerOption.prop("disabled", !_cameraState.printerSupported && _cameraState.source !== "printer");
            select.val(_cameraState.source || "printer");
        }

        renderExternalCameraPreviewToggle();
        renderCameraStatusText();

        const printerMode = _cameraState.effectiveSource === "printer";
        const externalMode = _cameraState.effectiveSource === "external";
        const externalPreviewKey = externalMode
            ? `${getActivePrinterIndex()}:${_cameraState.externalName || ""}:${_cameraState.externalStreamPreview ? "stream" : "frames"}`
            : null;

        if (!printerMode) {
            $("#vplayer").hide();
        }
        $("#external-camera-container").toggle(externalMode);
        const externalPreviewVisible = externalMode && _externalCameraPreviewEnabled;
        $("#external-camera-preview")
            .toggleClass("d-block", externalPreviewVisible)
            .toggleClass("d-none", !externalPreviewVisible);
        $("#camera-unavailable").toggle(!printerMode && !externalMode);

        $("#printer-camera-controls").toggle(printerMode || externalMode);
        $("#light-on-col, #light-off-col")
            .toggle(printerMode || externalMode)
            .toggleClass("col-6", printerMode || externalMode);
        $("#printer-video-toggle-col, #printer-snapshot-col").toggle(printerMode);
        $("#printer-camera-quality-wrap").toggle(printerMode);
        ensureExternalCameraControls().toggleClass("d-none", !externalMode);

        if (externalMode && _externalCameraPreviewEnabled) {
            if (_externalCameraPreviewContextKey !== externalPreviewKey) {
                stopExternalCameraPreview(false);
                _externalCameraPreviewContextKey = externalPreviewKey;
                if (_cameraState.externalStreamPreview) {
                    startExternalCameraStreamPreview();
                } else {
                    scheduleExternalCameraPreview(0);
                }
            }
        } else {
            stopExternalCameraPreview(!externalMode);
            _externalCameraPreviewContextKey = null;
        }
    }

    function applyRuntimeState(data) {
        if (!data || typeof data !== "object") {
            return;
        }

        const filament = data.filament || {};
        _filamentStatus.label = String(filament.label || "Unknown");
        _filamentStatus.detail = filament.detail || null;
        _filamentStatus.issue = filament.issue || null;
        _filamentStatus.pauseReason = filament.pause_reason_label || null;
        _filamentStatus.pendingRunout = false;
        const timelapse = data.timelapse || {};
        _timelapseRuntime.capturing = !!timelapse.capturing;
        _timelapseRuntime.activeCapture = !!timelapse.active_capture;
        _timelapseRuntime.paused = !!timelapse.paused;
        _timelapseRuntime.pauseReason = timelapse.pause_reason || null;
        _timelapseRuntime.manualPaused = !!timelapse.manual_paused;
        _timelapseRuntime.recovering = !!timelapse.recovering;
        _timelapseRuntime.detail = timelapse.detail || null;
        _timelapseRuntime.promptStart = !!timelapse.prompt_start;
        _timelapseRuntime.promptFilename = timelapse.prompt_filename || null;
        _timelapseRuntime.resumeAvailable = !!timelapse.resume_available;
        _timelapseRuntime.resumeFrameCount = Number(timelapse.resume_frame_count || 0);
        if (Object.prototype.hasOwnProperty.call(data, "temperature")) {
            const temperature = data.temperature || {};
            updateFilamentServiceTemps({
                nozzleCurrent: temperature.nozzle,
                nozzleTarget: temperature.nozzle_target,
                bedCurrent: temperature.bed,
                bedTarget: temperature.bed_target,
            });
        }
        applyCameraRuntimeState(data.camera || {});

        if (data.print && data.print.state !== undefined) {
            _updatePrintControlButtons(_normalizePrintStateValue(data.print.state));
        }
        renderFilamentStatus();
        renderTimelapseRuntimeStatus();
        renderTimelapseActionCard();
        renderTimelapseControls();
    }

    async function loadPrinterRuntimeState() {
        if (_printerRuntimeLoading) {
            return;
        }
        _printerRuntimeLoading = true;
        try {
            const resp = await fetch(withActivePrinterQuery("/api/printer/runtime-state"));
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            applyRuntimeState(data);
        } finally {
            _printerRuntimeLoading = false;
        }
    }

    function startPrinterRuntimePolling() {
        loadPrinterRuntimeState().catch(function (err) {
            console.warn("Failed to load printer runtime state", err);
        });
        if (_printerRuntimePollInterval) {
            return;
        }
        _printerRuntimePollInterval = setInterval(function () {
            loadPrinterRuntimeState().catch(function (err) {
                console.warn("Failed to load printer runtime state", err);
            });
        }, PRINTER_RUNTIME_POLL_MS);
    }

    function getActivePrinterIndex() {
        const raw = document.body ? document.body.dataset.activePrinterIndex : null;
        const parsed = Number(raw);
        if (Number.isFinite(parsed)) {
            return parsed;
        }
        return 0;
    }

    function withActivePrinterQuery(url) {
        const activePrinterIndex = String(getActivePrinterIndex());
        try {
            const resolved = new URL(url, window.location.origin);
            resolved.searchParams.set("printer_index", activePrinterIndex);
            if (resolved.origin === window.location.origin) {
                return `${resolved.pathname}${resolved.search}${resolved.hash}`;
            }
            return resolved.toString();
        } catch (_err) {
            const separator = String(url).includes("?") ? "&" : "?";
            return `${url}${separator}printer_index=${encodeURIComponent(activePrinterIndex)}`;
        }
    }

    function withHistoryScopeQuery(url) {
        const base = withActivePrinterQuery(url);
        if (!historyAllPrinters) return base;
        try {
            const resolved = new URL(base, window.location.origin);
            resolved.searchParams.set("all_printers", "1");
            if (resolved.origin === window.location.origin) {
                return `${resolved.pathname}${resolved.search}${resolved.hash}`;
            }
            return resolved.toString();
        } catch (_err) {
            return `${base}${base.includes("?") ? "&" : "?"}all_printers=1`;
        }
    }

    function processPrinterAlertEntries(entries, notifyUser) {
        if (!Array.isArray(entries) || !entries.length) {
            return;
        }
        const activePrinterIndex = getActivePrinterIndex();
        entries.forEach(function (entry) {
            if (!notifyUser || !entry || Number(entry.printer_index) === activePrinterIndex) {
                return;
            }
            const printerName = String(entry.printer_name || `Printer ${Number(entry.printer_index || 0) + 1}`);
            const title = String(entry.title || "Printer alert");
            const message = String(entry.message || title);
            flash_message(`${printerName}: ${message}`, entry.level || "warning", 0, {
                sticky: true,
                key: `printer-alert:${Number(entry.printer_index || 0)}:${String(entry.type || title)}`,
            });
        });
    }

    async function pollPrinterAlerts() {
        const params = new URLSearchParams({ limit: "20" });
        if (_printerAlertLastId > 0) {
            params.set("after", String(_printerAlertLastId));
        }
        const resp = await fetch(`/api/printer/alerts?${params.toString()}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }

        const notifyUser = _printerAlertPollStarted;
        processPrinterAlertEntries(data.entries || [], notifyUser);
        _printerAlertLastId = data.last_id || _printerAlertLastId;
        _printerAlertPollStarted = true;
    }

    function startPrinterAlertPolling() {
        if (!document.getElementById("messages")) {
            return;
        }
        pollPrinterAlerts().catch(function (err) {
            console.warn("Failed to poll printer alerts", err);
        });
        setInterval(function () {
            pollPrinterAlerts().catch(function (err) {
                console.warn("Failed to poll printer alerts", err);
            });
        }, PRINTER_ALERT_POLL_MS);
    }

    /**
     * Highlight active video profile button.
     * @param {string} profileId
     */
    function setVideoProfileActive(profileId) {
        if (!profileId) {
            return;
        }
        const profileKey = String(profileId).toLowerCase();
        const buttons = $(".video-profile-btn");
        if (!buttons.length) {
            return;
        }
        buttons.each(function () {
            const btn = $(this);
            const isActive = btn.data("video-profile") === profileKey;
            btn.toggleClass("active", isActive);
            btn.attr("aria-pressed", isActive ? "true" : "false");
        });
    }

    /**
     * AutoWebSocket class
     *
     * This class wraps a WebSocket, and makes it automatically reconnect if the
     * connection is lost.
     */
    function setConnectionBadge(selector, tone) {
        $(selector)
            .removeClass("text-bg-success text-bg-danger text-bg-secondary text-bg-warning")
            .addClass(`text-bg-${tone}`);
    }

    class AutoWebSocket {
        constructor({
            name,
            url,
            badge = null,
            open = null,
            opened = null,
            close = null,
            error = null,
            message = null,
            binary = false,
            reconnect = 1000,
        }) {
            this.name = name;
            this.url = url;
            this.badge = badge;
            this.reconnect = reconnect;
            this.open = open;
            this.opened = opened;
            this.close = close;
            this.error = error;
            this.message = message;
            this.binary = binary;
            this.ws = null;
            this.is_open = false;
            this.autoReconnect = reconnect !== false;
        }

        _open() {
            setConnectionBadge(this.badge, "warning");
            if (this.open)
                this.open(this.ws);
        }

        _close() {
            setConnectionBadge(this.badge, "danger");
            console.log(`${this.name} close`);
            this.is_open = false;
            const old = this.ws;
            this.ws = null;
            if (this.autoReconnect) {
                setTimeout(() => this.connect(), this.reconnect);
            }
            if (this.close)
                this.close(old);
        }

        _error() {
            console.log(`${this.name} error`);
            const old = this.ws;
            this.ws = null;
            this.is_open = false;
            try {
                if (old) {
                    old.close();
                }
            } catch (_) {}
            if (this.error)
                this.error(old);
        }

        _message(event) {
            // Check for server-side auth rejection before processing
            if (typeof event.data === "string") {
                try {
                    const parsed = JSON.parse(event.data);
                    if (parsed.error === "unauthorized") {
                        console.warn(`${this.name}: server rejected connection (unauthorized)`);
                        this.autoReconnect = false;
                        if (this.ws) this.ws.close();
                        return;
                    }
                } catch (_) {
                    // Not JSON — continue to normal message handling
                }
            }
            if (!this.is_open) {
                setConnectionBadge(this.badge, "success");
                this.is_open = true;
                if (this.opened)
                    this.opened(event);
            }
            if (this.message)
                this.message(event);
        }

        connect() {
            if (this.reconnect !== false && !this.autoReconnect) {
                return;
            }
            if (this.ws) {
                return;
            }
            var ws = this.ws = new WebSocket(this.url);
            if (this.binary)
                ws.binaryType = "arraybuffer";
            ws.addEventListener("open", this._open.bind(this));
            ws.addEventListener("close", this._close.bind(this));
            ws.addEventListener("error", this._error.bind(this));
            ws.addEventListener("message", this._message.bind(this));
        }
    }

    const uploadBar = $("#upload-progressbar");
    const uploadLabel = $("#upload-progress");
    const uploadMeta = $("#upload-progress-meta");
    let uploadName = "";
    let uploadSize = 0;

    function setUploadProgress(percent) {
        if (!uploadBar.length) {
            return;
        }
        const pct = Math.max(0, Math.min(100, percent));
        uploadBar.attr("aria-valuenow", pct);
        uploadBar.attr("style", `width: ${pct}%`);
        uploadLabel.text(`${pct}%`);
    }

    function resetUploadProgress(message) {
        if (!uploadBar.length) {
            return;
        }
        uploadBar.removeClass("bg-danger");
        setUploadProgress(0);
        uploadMeta.text(message || "Idle");
        uploadName = "";
        uploadSize = 0;
    }

    const Z_OFFSET_STEP_MM = 0.01;
    let _zOffsetCurrentMm = null;

    function setZOffsetControlsEnabled(enabled) {
        $("#z-offset-set-btn").prop("disabled", !enabled);
        $("#z-offset-minus-btn").prop("disabled", !enabled);
        $("#z-offset-plus-btn").prop("disabled", !enabled);
        $("#z-offset-target").prop("disabled", !enabled);
    }

    function normalizeZOffsetMm(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) {
            return null;
        }
        return Math.round(number * 100) / 100;
    }

    function extractZOffsetMm(payload) {
        if (!payload || typeof payload !== "object") {
            return null;
        }
        const keys = ["value", "zAxisRecoup", "z_axis_recoup", "zOffset", "z_offset"];
        for (const key of keys) {
            if (!(key in payload)) {
                continue;
            }
            const steps = Number(payload[key]);
            if (Number.isFinite(steps)) {
                return normalizeZOffsetMm(steps * Z_OFFSET_STEP_MM);
            }
        }
        if ("mm" in payload) {
            return normalizeZOffsetMm(payload.mm);
        }
        return null;
    }

    function formatZOffsetMm(value) {
        const mm = normalizeZOffsetMm(value);
        return mm === null ? "unknown" : `${mm.toFixed(2)} mm`;
    }

    function setZOffsetStatus(message, category = "secondary") {
        const el = document.getElementById("z-offset-status");
        if (!el) {
            return;
        }
        if (!message) {
            el.innerHTML = "";
            return;
        }
        el.innerHTML =
            `<div class="alert alert-${category} py-2 small mb-0">${escapeHtml(message)}</div>`;
    }

    function applyZOffsetState(zOffset, options = {}) {
        const currentEl = document.getElementById("z-offset-current");
        const targetEl = document.getElementById("z-offset-target");
        if (!currentEl || !targetEl || !zOffset) {
            return;
        }

        const mm = extractZOffsetMm(zOffset);
        if (mm === null) {
            currentEl.textContent = "unknown";
            setZOffsetControlsEnabled(false);
            return;
        }

        _zOffsetCurrentMm = mm;
        currentEl.textContent = formatZOffsetMm(mm);
        setZOffsetControlsEnabled(true);

        if (options.populateTarget || !String(targetEl.value || "").trim()) {
            targetEl.value = mm.toFixed(2);
        }

        if (options.statusMessage) {
            setZOffsetStatus(options.statusMessage, options.statusCategory || "secondary");
        }
    }

    async function zOffsetRequest(url, payload = null) {
        const resp = await fetch(withActivePrinterQuery(url), {
            method: payload ? "POST" : "GET",
            headers: payload ? { "Content-Type": "application/json" } : undefined,
            body: payload ? JSON.stringify(payload) : undefined,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        return data;
    }

    async function loadZOffset(refresh = false, options = {}) {
        const data = refresh
            ? await zOffsetRequest("/api/printer/z-offset/refresh", {})
            : await zOffsetRequest("/api/printer/z-offset");

        applyZOffsetState(data.z_offset, {
            populateTarget: options.populateTarget === true,
            statusMessage: options.statusMessage ? (data.message || options.statusMessage) : null,
            statusCategory: options.statusCategory || "secondary",
        });
        return data;
    }

    /**
     * Auto web sockets
     */
    const sockets = {};

    sockets.mqtt = new AutoWebSocket({
        name: "mqtt socket",
        url: `${location.protocol.replace("http", "ws")}//${location.host}/ws/mqtt?printer_index=${encodeURIComponent(getActivePrinterIndex())}`,
        badge: "#badge-mqtt",

        message: function (ev) {
            let data = null;
            try {
                data = JSON.parse(ev.data);
            } catch (err) {
                console.warn("mqtt socket: failed to parse message", err);
                return;
            }
            if (data.commandType == 1000) {
                if (Number(data.subType) === 2 && Number(data.value) === 6) {
                    _filamentStatus.pendingRunout = false;
                    _filamentStatus.label = "Not Loaded";
                    _filamentStatus.detail = _currentPrintState === PRINT_STATE.PAUSED
                        ? "Paused: Filament runout. Reload filament to continue."
                        : "Filament runout or break detected.";
                    _filamentStatus.issue = "runout";
                    _filamentStatus.pauseReason = "Filament runout";
                    renderFilamentStatus();
                    return;
                }
                // Printer state machine: normalize firmware resume acknowledgements for UI controls.
                const normalizedState = _normalizePrintStateValue(data.value);
                const wasPausedForFilament = _currentPrintState === PRINT_STATE.PAUSED
                    && !!_filamentStatus.pauseReason
                    && _filamentStatus.issue === "runout";
                if (normalizedState === PRINT_STATE.PAUSED && _filamentStatus.pendingRunout && _filamentStatus.issue !== "runout") {
                    _filamentStatus.pendingRunout = false;
                    _filamentStatus.label = "Not Loaded";
                    _filamentStatus.detail = "Paused: Filament runout. Reload filament to continue.";
                    _filamentStatus.issue = "runout";
                    _filamentStatus.pauseReason = "Filament runout";
                }
                if (normalizedState === PRINT_STATE.PRINTING && wasPausedForFilament) {
                    _filamentStatus.label = "Loaded";
                    _filamentStatus.detail = null;
                    _filamentStatus.issue = null;
                }
                if (normalizedState !== PRINT_STATE.PAUSED) {
                    _filamentStatus.pauseReason = null;
                    if (_filamentStatus.issue !== "runout") {
                        _filamentStatus.pendingRunout = false;
                    }
                }
                _updatePrintControlButtons(normalizedState);
                if (typeof _onMqttStateChange === "function") {
                    _onMqttStateChange(data.value);
                }
            } else if (data.commandType == 1001) {
                // ZZ_MQTT_CMD_PRINT_SCHEDULE: time=remaining, totalTime=elapsed, progress=0-10000
                if (data.name && _isPrintStateActive()) {
                    $("#print-name").text(data.name);
                }
                const remainingText = getTime(data.time);
                if (remainingText !== null) {
                    $("#time-remain").text(remainingText);
                }
                if (data.totalTime !== undefined) {
                    const elapsedText = getTime(data.totalTime);
                    if (elapsedText !== null) {
                        $("#time-elapsed").text(elapsedText);
                    }
                }
                if (data.progress !== undefined) {
                    const progress = Math.min(100, Math.round(data.progress / 100));
                    $("#progressbar").attr("aria-valuenow", progress);
                    $("#progressbar").attr("style", `width: ${progress}%`);
                    $("#progress").text(`${progress}%`);
                    document.title = progress > 0 && progress < 100
                        ? `\u{1F5A8}\uFE0F ${progress}% | ankerctl`
                        : "ankerctl";
                }
            } else if (data.commandType == 1003) {
                // Returns Nozzle Temp
                const current = getTempRounded(data.currentTemp);
                $("#nozzle-temp").text(`${current}°C`);
                if (data.hasOwnProperty('targetTemp')) {
                    const target = getTempRounded(data.targetTemp);
                    if (!$("#set-nozzle-temp").is(":focus")) {
                        $("#set-nozzle-temp").val(target);
                    }
                    updateFilamentServiceTemps({
                        nozzleCurrent: current,
                        nozzleTarget: target,
                    });
                } else {
                    updateFilamentServiceTemps({
                        nozzleCurrent: current,
                    });
                }
                pushTempData("nozzle", getTemp(data.currentTemp), getTemp(data.targetTemp));
            } else if (data.commandType == 1004) {
                // Returns Bed Temp
                const current = getTempRounded(data.currentTemp);
                $("#bed-temp").text(`${current}°C`);
                if (data.hasOwnProperty('targetTemp')) {
                    const target = getTempRounded(data.targetTemp);
                    if (!$("#set-bed-temp").is(":focus")) {
                        $("#set-bed-temp").val(target);
                    }
                    updateFilamentServiceTemps({
                        bedCurrent: current,
                        bedTarget: target,
                    });
                } else {
                    updateFilamentServiceTemps({
                        bedCurrent: current,
                    });
                }
                pushTempData("bed", getTemp(data.currentTemp), getTemp(data.targetTemp));
            } else if (data.commandType == 1006) {
                // Returns Print Speed
                const X = getSpeedFactor(data.value);
                $("#print-speed").text(`${data.value}mm/s ${X}`);
            } else if (data.commandType == 1007) {
                // auto_leveling: value = current probe point (1 center + 7×7 = 50 points total)
                const point = data.value;
                const total = 50;
                const pct = Math.min(100, Math.round(point / total * 100));
                const statusEl = document.getElementById("bed-level-status");
                if (statusEl) {
                    statusEl.innerHTML =
                        `<div class="alert alert-info py-1 small mb-0">` +
                        `<div class="d-flex justify-content-between mb-1">` +
                        `<span>Auto-Leveling… Punkt ${escapeHtml(point)} / ${total}</span>` +
                        `<span>${pct}%</span></div>` +
                        `<div class="progress" style="height:6px;">` +
                        `<div class="progress-bar progress-bar-striped progress-bar-animated" ` +
                        `style="width:${pct}%" aria-valuenow="${pct}"></div></div></div>`;
                }
            } else if (data.commandType == 1021) {
                applyZOffsetState(data, { populateTarget: false });
            } else if (data.commandType == 1085 && String(data.errorCode || "") === "0xFF01030001") {
                _filamentStatus.pendingRunout = true;
            } else if (data.commandType == 1086 && String(data.errorCode || "") === "0xFF01030001") {
                if (_filamentStatus.issue !== "runout") {
                    _filamentStatus.pendingRunout = false;
                }
            } else if (isFilamentRunoutEvent(data)) {
                _filamentStatus.pendingRunout = false;
                _filamentStatus.label = "Not Loaded";
                _filamentStatus.detail = _currentPrintState === PRINT_STATE.PAUSED
                    ? "Paused: Filament runout. Reload filament to continue."
                    : "Filament runout or break detected.";
                _filamentStatus.issue = "runout";
                _filamentStatus.pauseReason = "Filament runout";
                renderFilamentStatus();
            } else if (data.commandType == 1023) {
                const label = getFilamentStateLabel(data.value, data.progress, data.stepLen);
                const inFilamentRunoutPause = _currentPrintState === PRINT_STATE.PAUSED
                    && !!_filamentStatus.pauseReason
                    && _filamentStatus.issue === "runout";
                if (inFilamentRunoutPause && label === "Loaded") {
                    _filamentStatus.label = "Not Loaded";
                    _filamentStatus.detail = null;
                } else {
                    _filamentStatus.label = label;
                    _filamentStatus.detail = getFilamentStateDetail(label, data.progress);
                }
                if (!inFilamentRunoutPause && (label === "Changing" || label === "Loaded")) {
                    _filamentStatus.issue = null;
                    _filamentStatus.pendingRunout = false;
                }
                renderFilamentStatus();
            } else if (data.commandType == 1044) {
                // Print start notification or storage preview; only active prints should rename Home.
                const filePath = data.filePath || "";
                const baseName = filePath.split("/").pop().split("\\").pop();
                if (baseName && _isPrintStateActive() && !_isStoredFileSourcePath(filePath)) {
                    $("#print-name").text(baseName);
                }
            } else if (data.commandType == 1052) {
                // Returns Layer Info — layer display only; progress comes from ct=1001
                const layer = `${data.real_print_layer} / ${data.total_layer}`;
                $("#print-layer").text(layer);
            } else {
                console.log("Unhandled mqtt message:", data);
            }
        },

        close: function () {
            $("#print-name").text("");
            $("#time-elapsed").text("00:00:00");
            $("#time-remain").text("00:00:00");
            $("#progressbar").attr("aria-valuenow", 0);
            $("#progressbar").attr("style", "width: 0%");
            $("#progress").text("0%");
            $("#nozzle-temp").text("0°C");
            $("#set-nozzle-temp").val(0);
            $("#bed-temp").text("0°C");
            $("#set-bed-temp").val(0);
            updateFilamentServiceTemps({
                nozzleCurrent: null,
                nozzleTarget: null,
                bedCurrent: null,
                bedTarget: null,
            });
            $("#print-speed").text("0mm/s");
            $("#print-layer").text("0 / 0");
            _filamentStatus.label = "Unknown";
            _filamentStatus.detail = null;
            _filamentStatus.issue = null;
            _filamentStatus.pauseReason = null;
            _filamentStatus.pendingRunout = false;
            renderFilamentStatus();
            document.title = "ankerctl";
            _updatePrintControlButtons(PRINT_STATE.IDLE);
            _zOffsetCurrentMm = null;
            $("#z-offset-current").text("unknown");
            setZOffsetControlsEnabled(false);
        },
    });

    /**
     * Initializing a new instance of JMuxer for video playback
     */
    sockets.video = new AutoWebSocket({
        name: "Video socket",
        url: `${location.protocol.replace("http", "ws")}//${location.host}/ws/video?printer_index=${encodeURIComponent(getActivePrinterIndex())}`,
        badge: "#badge-video",
        binary: true,
        reconnect: 2000,

        open: function () {
            this.videoQueue = [];
            this.videoBufferMinPackets = 2;
            this.videoBufferDelayMs = 120;
            this.videoBufferMaxPackets = 120;
            this.videoBufferCatchupPackets = 24;
            this.videoPumpMaxPacketsPerTick = 24;
            this.videoBuffering = true;
            this.jmuxer = new JMuxer({
                node: "player",
                mode: "video",
                flushingTime: 0,
                fps: 15,
                // debug: true,
                onError: function (data) {
                    console.warn("JMuxer video error", data);
                },
            });
            this.videoPump = window.setInterval(() => {
                if (!this.jmuxer)
                    return;

                const now = (window.performance && window.performance.now) ? window.performance.now() : Date.now();
                if (this.videoBuffering) {
                    const firstPacket = this.videoQueue[0];
                    const firstPacketAge = firstPacket ? now - firstPacket.receivedAt : 0;
                    if (this.videoQueue.length < this.videoBufferMinPackets && firstPacketAge < this.videoBufferDelayMs) {
                        return;
                    }
                    this.videoBuffering = false;
                }

                let fedPackets = 0;
                while (this.videoQueue.length && fedPackets < this.videoPumpMaxPacketsPerTick) {
                    const packet = this.videoQueue[0];
                    const packetAge = now - packet.receivedAt;
                    if (packetAge < this.videoBufferDelayMs && this.videoQueue.length <= this.videoBufferMinPackets) {
                        break;
                    }
                    this.videoQueue.shift();
                    try {
                        this.jmuxer.feed({
                            video: new Uint8Array(packet.data),
                        });
                    } catch (err) {
                        console.log({ type: "video", name: err?.name || "feed", error: err?.message || String(err) });
                        this.videoBuffering = true;
                        return;
                    }
                    fedPackets++;
                }

                if (!fedPackets && !this.videoQueue.length) {
                    this.videoBuffering = true;
                }
            }, 16);
        },

        message: function (event) {
            if (!(event.data instanceof ArrayBuffer) || event.data.byteLength === 0) {
                return;
            }
            if (!this.videoQueue) {
                this.videoQueue = [];
            }
            const now = (window.performance && window.performance.now) ? window.performance.now() : Date.now();
            this.videoQueue.push({
                data: event.data.slice(0),
                receivedAt: now,
            });
            if (this.videoQueue.length > this.videoBufferMaxPackets) {
                const keepPackets = Math.max(this.videoBufferCatchupPackets, this.videoBufferMinPackets);
                this.videoQueue.splice(0, this.videoQueue.length - keepPackets);
                this.videoBuffering = true;
            }
        },

        close: function () {
            if (this.videoPump) {
                window.clearInterval(this.videoPump);
                this.videoPump = null;
            }
            this.videoQueue = [];
            this.videoBuffering = true;

            if (!this.jmuxer)
                return;

            this.jmuxer.destroy();
            this.jmuxer = null;

            /* Clear video source (to show loading animation) */
            $("#player").attr("src", "");
            $("#video-resolution").text("Current: -");

            $(this.badge).removeClass("text-bg-warning text-bg-success").addClass("text-bg-danger");
        },
    });

    const videoPlayer = document.getElementById("player");
    const updateVideoResolution = () => {
        if (!videoPlayer) {
            return;
        }
        const width = videoPlayer.videoWidth;
        const height = videoPlayer.videoHeight;
        if (width && height) {
            $("#video-resolution").text(`Current: ${width}x${height}`);
        }
    };
    if (videoPlayer) {
        videoPlayer.addEventListener("loadedmetadata", updateVideoResolution);
        videoPlayer.addEventListener("loadeddata", updateVideoResolution);
        videoPlayer.addEventListener("resize", updateVideoResolution);
    }

    const ctrlPendingMessages = [];
    function sendCtrlMessage(payload) {
        const message = JSON.stringify(payload);
        if (sockets.ctrl.ws && sockets.ctrl.ws.readyState === WebSocket.OPEN) {
            try {
                sockets.ctrl.ws.send(message);
                return true;
            } catch (err) {
                console.warn("ctrl socket: send failed, queueing message", err);
            }
        }
        ctrlPendingMessages.push(message);
        return false;
    }

    function flushCtrlMessages() {
        if (!sockets.ctrl.ws || sockets.ctrl.ws.readyState !== WebSocket.OPEN) {
            return;
        }
        while (ctrlPendingMessages.length) {
            const message = ctrlPendingMessages.shift();
            try {
                sockets.ctrl.ws.send(message);
            } catch (err) {
                ctrlPendingMessages.unshift(message);
                console.warn("ctrl socket: failed to flush queued message", err);
                return;
            }
        }
    }

    sockets.ctrl = new AutoWebSocket({
        name: "Control socket",
        url: `${location.protocol.replace("http", "ws")}//${location.host}/ws/ctrl?printer_index=${encodeURIComponent(getActivePrinterIndex())}`,
        badge: "#badge-ctrl",
        opened: flushCtrlMessages,
        message: function (event) {
            let data = null;
            try {
                data = JSON.parse(event.data);
            } catch (err) {
                return;
            }
            flushCtrlMessages();
            if (data.ankerctl) {
                setConnectionBadge("#badge-ctrl", "warning");
            }
            if (data.video_profile) {
                setVideoProfileActive(data.video_profile);
            }
        },
    });

    sockets.pppp_state = new AutoWebSocket({
        name: "PPPP socket",
        url: `${location.protocol.replace("http", "ws")}//${location.host}/ws/pppp-state?printer_index=${encodeURIComponent(getActivePrinterIndex())}`,
        badge: "#badge-pppp",
        reconnect: 5000,

        message: function (event) {
            let data = null;
            try {
                data = JSON.parse(event.data);
            } catch (err) {
                console.warn("pppp socket: failed to parse message", err);
                return;
            }
            if (data.status === "connected") {
                setConnectionBadge(this.badge, "success");
                if (data.source === "service") {
                    setConnectionBadge("#badge-ctrl", "success");
                } else {
                    setConnectionBadge("#badge-ctrl", "warning");
                }
            } else if (data.status === "disconnected") {
                setConnectionBadge(this.badge, "warning");
                setConnectionBadge("#badge-ctrl", "warning");
            } else if (data.status === "dormant") {
                setConnectionBadge(this.badge, "secondary");
                setConnectionBadge("#badge-ctrl", "warning");
            }
        },
    });

    sockets.upload = new AutoWebSocket({
        name: "Upload socket",
        url: `${location.protocol.replace("http", "ws")}//${location.host}/ws/upload`,
        reconnect: 2000,
        message: function (event) {
            let data = null;
            try {
                data = JSON.parse(event.data);
            } catch (err) {
                return;
            }
            if (!data) {
                return;
            }
            if (data.name) {
                uploadName = data.name;
            }
            if (typeof data.size === "number") {
                uploadSize = data.size;
            }
            if (data.status === "start") {
                uploadBar.removeClass("bg-danger");
                setUploadProgress(0);
                const sizeText = uploadSize ? ` (${formatBytes(uploadSize)})` : "";
                uploadMeta.text(uploadName ? `Starting upload: ${uploadName}${sizeText}` : "Starting upload");
            } else if (data.status === "progress") {
                const total = data.size || uploadSize;
                const sent = data.sent || 0;
                const percent = total ? Math.round((sent / total) * 100) : 0;
                setUploadProgress(percent);
                const metaName = uploadName ? `Uploading ${uploadName}` : "Uploading";
                const metaSize = total ? ` (${formatBytes(sent)} / ${formatBytes(total)})` : "";
                uploadMeta.text(`${metaName}${metaSize}`);
            } else if (data.status === "done") {
                uploadBar.removeClass("bg-danger");
                setUploadProgress(100);
                const total = data.size || uploadSize;
                const sizeText = total ? ` (${formatBytes(total)})` : "";
                if (data.start_print === true) {
                    uploadMeta.text(uploadName ? `Upload complete, printer preparing: ${uploadName}${sizeText}` : "Upload complete, printer preparing");
                    if (_currentPrintState === PRINT_STATE.IDLE) {
                        if (uploadName) {
                            $("#print-name").text(uploadName);
                        }
                        _updatePrintControlButtons(PRINT_STATE.PENDING_START);
                    }
                } else {
                    uploadMeta.text(uploadName ? `Upload complete: ${uploadName}${sizeText}` : "Upload complete");
                }
            } else if (data.status === "error") {
                uploadBar.addClass("bg-danger");
                setUploadProgress(0);
                const errorText = data.error ? `: ${data.error}` : "";
                uploadMeta.text(`Upload failed${errorText}`);
            }
        },
        close: function () {
            resetUploadProgress("Idle");
        },
    });

    if ($("#badge-mqtt").length) {
        sockets.mqtt.connect();
    }
    if ($("#badge-ctrl").length) {
        sockets.ctrl.connect();
    }
    if ($("#badge-pppp").length) {
        sockets.pppp_state.connect();
    }
    if ($("#upload-progressbar").length) {
        sockets.upload.connect();
    }
    startPrinterRuntimePolling();

    sockets.video.autoReconnect = false;

    function setPrinterVideoEnabled(enabled) {
        videoEnabled = !!enabled;
        if (videoEnabled) {
            $("#vplayer").show();
            $("#video-toggle").html('<i class="bi bi-camera-video-off"></i> Disable Video');
            sendCtrlMessage({ video_enabled: true });
            sockets.video.autoReconnect = true;
            if (!sockets.video.ws) {
                sockets.video.connect();
            }
        } else {
            $("#vplayer").hide();
            $("#video-toggle").html('<i class="bi bi-camera-video"></i> Enable Video');
            sendCtrlMessage({ video_enabled: false });
            sockets.video.autoReconnect = false;
            if (sockets.video.ws) {
                try {
                    sockets.video.ws.close();
                } catch (_) {}
                sockets.video.ws = null;
            }
            $("#video-resolution").text("Current: -");
        }
        renderCameraUi();
    }

    $("#video-toggle").on("click", function () {
        setPrinterVideoEnabled(!videoEnabled);
    });

    $(document).on("click", ".external-preview-toggle, #external-preview-toggle", function () {
        setExternalCameraPreviewEnabled(!_externalCameraPreviewEnabled);
    });

    /**
     * Highlight the active light button.
     * @param {boolean|null} on - true = light on, false = light off, null = unknown
     */
    function setLightActive(on) {
        $("#light-on").toggleClass("active", on === true).attr("aria-pressed", on === true ? "true" : "false");
        $("#light-off").toggleClass("active", on === false).attr("aria-pressed", on === false ? "true" : "false");
    }

    /**
     * On click of element with id "light-on", sends JSON data to wsctrl to turn light on
     */
    $("#light-on").on("click", function () {
        sendCtrlMessage({ light: true });
        setLightActive(true);
        return false;
    });

    /**
     * On click of element with id "light-off", sends JSON data to wsctrl to turn light off
     */
    $("#light-off").on("click", function () {
        sendCtrlMessage({ light: false });
        setLightActive(false);
        return false;
    });

    /**
     * On click of video profile buttons, sends JSON data to wsctrl to set video profile
     */
    $(".video-profile-btn").on("click", function () {
        const profile = $(this).data("video-profile");
        setVideoProfileActive(profile);
        sendCtrlMessage({ video_profile: profile });
        return false;
    });

    const appriseForm = $("#apprise-form");
    if (appriseForm.length) {
        const appriseFields = {
            enabled: $("#apprise-enabled"),
            serverUrl: $("#apprise-server-url"),
            key: $("#apprise-key"),
            tag: $("#apprise-tag"),
            progressInterval: $("#apprise-progress-interval"),
            snapshotQuality: $("#apprise-snapshot-quality"),
            snapshotFallback: $("#apprise-snapshot-fallback"),
            snapshotLight: $("#apprise-snapshot-light"),
            progressIncludeImage: $("#apprise-progress-image"),
            events: {
                print_started: $("#apprise-event-print-started"),
                print_finished: $("#apprise-event-print-finished"),
                print_failed: $("#apprise-event-print-failed"),
                gcode_uploaded: $("#apprise-event-gcode-uploaded"),
                print_progress: $("#apprise-event-print-progress"),
            },
        };
        const appriseButtons = {
            save: $("#apprise-save"),
            test: $("#apprise-test"),
        };

        const setAppriseBusy = (busy) => {
            appriseButtons.save.prop("disabled", busy);
            appriseButtons.test.prop("disabled", busy);
        };

        const buildAppriseConfig = () => {
            const interval = parseInt(appriseFields.progressInterval.val(), 10);
            const snapshotQuality = appriseFields.snapshotQuality.val().trim().toLowerCase();
            return {
                enabled: appriseFields.enabled.is(":checked"),
                server_url: appriseFields.serverUrl.val().trim(),
                key: appriseFields.key.val().trim(),
                tag: appriseFields.tag.val().trim(),
                events: {
                    print_started: appriseFields.events.print_started.is(":checked"),
                    print_finished: appriseFields.events.print_finished.is(":checked"),
                    print_failed: appriseFields.events.print_failed.is(":checked"),
                    gcode_uploaded: appriseFields.events.gcode_uploaded.is(":checked"),
                    print_progress: appriseFields.events.print_progress.is(":checked"),
                },
                progress: {
                    interval_percent: Number.isNaN(interval) ? 25 : interval,
                    include_image: appriseFields.progressIncludeImage.is(":checked"),
                    snapshot_quality: snapshotQuality || "hd",
                    snapshot_fallback: appriseFields.snapshotFallback.is(":checked"),
                    snapshot_light: appriseFields.snapshotLight.is(":checked"),
                },
            };
        };

        const applyAppriseSettings = (apprise) => {
            const settings = apprise || {};
            const events = settings.events || {};
            const progress = settings.progress || {};
            appriseFields.enabled.prop("checked", Boolean(settings.enabled));
            appriseFields.serverUrl.val(settings.server_url || "");
            appriseFields.key.val(settings.key || "");
            appriseFields.tag.val(settings.tag || "");
            appriseFields.events.print_started.prop("checked", Boolean(events.print_started));
            appriseFields.events.print_finished.prop("checked", Boolean(events.print_finished));
            appriseFields.events.print_failed.prop("checked", Boolean(events.print_failed));
            appriseFields.events.gcode_uploaded.prop("checked", Boolean(events.gcode_uploaded));
            appriseFields.events.print_progress.prop("checked", Boolean(events.print_progress));
            if (progress.interval_percent !== undefined && progress.interval_percent !== null) {
                appriseFields.progressInterval.val(progress.interval_percent);
            } else {
                appriseFields.progressInterval.val("");
            }
            appriseFields.progressIncludeImage.prop("checked", Boolean(progress.include_image));
            appriseFields.snapshotQuality.val(progress.snapshot_quality || "hd");
            appriseFields.snapshotFallback.prop("checked", progress.snapshot_fallback !== false);
            appriseFields.snapshotLight.prop("checked", Boolean(progress.snapshot_light));
        };

        const loadAppriseSettings = async () => {
            setAppriseBusy(true);
            try {
                const resp = await fetch("/api/notifications/settings");
                if (resp.ok) {
                    const data = await resp.json();
                    applyAppriseSettings(data.apprise || {});
                } else {
                    const data = await resp.json().catch(() => ({}));
                    const msg = data.error ? data.error : `HTTP ${resp.status}`;
                    flash_message(`Failed to load notifications: ${msg}`, "danger");
                }
            } catch (err) {
                flash_message(`Failed to load notifications: ${err}`, "danger");
            } finally {
                setAppriseBusy(false);
            }
        };

        appriseButtons.save.on("click", async function () {
            setAppriseBusy(true);
            const payload = { apprise: buildAppriseConfig() };
            try {
                const resp = await fetch("/api/notifications/settings", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                if (resp.ok) {
                    const data = await resp.json().catch(() => ({}));
                    if (data.apprise) {
                        applyAppriseSettings(data.apprise);
                    }
                    flash_message("Notification settings saved", "success");
                } else {
                    const data = await resp.json().catch(() => ({}));
                    const msg = data.error ? data.error : `HTTP ${resp.status}`;
                    flash_message(`Failed to save notifications: ${msg}`, "danger");
                }
            } catch (err) {
                flash_message(`Failed to save notifications: ${err}`, "danger");
            } finally {
                setAppriseBusy(false);
            }
        });

        appriseButtons.test.on("click", async function () {
            setAppriseBusy(true);
            const payload = { apprise: buildAppriseConfig() };
            try {
                const resp = await fetch("/api/notifications/test", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                const data = await resp.json().catch(() => ({}));
                if (resp.ok) {
                    flash_message(data.message || "Test notification sent", "success");
                } else {
                    const msg = data.error ? data.error : `HTTP ${resp.status}`;
                    flash_message(`Test notification failed: ${msg}`, "danger");
                }
            } catch (err) {
                flash_message(`Test notification failed: ${err}`, "danger");
            } finally {
                setAppriseBusy(false);
            }
        });

        loadAppriseSettings();
    }

    (function (selectElement) {
        if (!selectElement.length) return;
        const countryCodes = selectElement.data("countrycodes");
        const currentCountry = selectElement.data("country");
        countryCodes.forEach((item) => {
            const opt = document.createElement("option");
            opt.value = item.c;
            opt.textContent = item.n;
            opt.selected = (currentCountry == item.c);
            selectElement[0].appendChild(opt);
        });
    })($("#loginCountry"));

    $("#captchaRow").hide();
    $("#loginCaptchaId").val("");

    $("#config-login-form").on("submit", function (e) {
        e.preventDefault();

        (async () => {
            const form = $("#config-login-form");
            const url = form.attr("action");
            const submitBtn = $("#login");
            const originalButtonHtml = submitBtn.html();

            const form_data = new URLSearchParams();
            for (const pair of new FormData(form.get(0))) {
                form_data.append(pair[0], pair[1]);
            }

            submitBtn.prop("disabled", true);
            submitBtn.html('<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Working...');

            try {
                const resp = await fetch(url, {
                    method: 'POST',
                    body: form_data
                });

                if (resp.status < 300) {
                    const data = await resp.json();
                    const input = $("#loginCaptchaText");
                    if ("redirect" in data) {
                        document.location = data["redirect"];
                        return;
                    }
                    else if ("error" in data) {
                        flash_message(data["error"], "danger");
                        input.get(0).focus();
                    }
                    else if ("captcha_id" in data) {
                        input.val("");
                        input.attr("aria-required", "true");
                        input.prop("required", true);
                        input.get(0).focus();
                        $("#loginCaptchaId").val(data["captcha_id"]);
                        $("#loginCaptchaImg").attr("src", data["captcha_url"]);
                        $("#captchaRow").show();
                    }
                }
                else {
                    flash_message(`HTTP Error ${resp.status}: ${resp.statusText}`, "danger")
                }
            }
            finally {
                submitBtn.prop("disabled", false);
                submitBtn.html(originalButtonHtml);
            }
        })();
    });

    $("#upload-rate").on("change", function () {
        const rate = $(this).val();
        const form_data = new URLSearchParams();
        form_data.append("upload_rate_mbps", rate);

        (async () => {
            const resp = await fetch("/api/ankerctl/config/upload-rate", {
                method: "POST",
                body: form_data,
            });
            if (resp.ok) {
                const data = await resp.json().catch(() => ({}));
                const effectiveRate = data.effective_upload_rate_mbps ?? rate;
                const effectiveSource = data.effective_upload_rate_source || "config";
                if (effectiveSource === "config") {
                    flash_message(`Upload rate set to ${effectiveRate} Mbps`, "success");
                } else {
                    flash_message(`Saved ${rate} Mbps, but effective upload rate is ${effectiveRate} Mbps from ${effectiveSource}`, "warning");
                }
            } else {
                const data = await resp.json().catch(() => ({}));
                const msg = data.error ? data.error : `HTTP ${resp.status}`;
                flash_message(`Failed to update upload rate: ${msg}`, "danger");
            }
        })();
    });

    $("#printer-lan-search-btn").on("click", async function () {
        const btn = $(this);
        const status = $("#printer-lan-search-result");
        btn.prop("disabled", true);
        status.text("Searching...");

        try {
            const resp = await fetch("/api/printers/lan-search", { method: "POST" });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                status.text("");
                flash_message(`LAN search failed: ${data.error || `HTTP ${resp.status}`}`, "danger");
                return;
            }

            const active = data.active_printer || {};
            const savedIp = active.ip_addr || "not set";
            $("#printer-ip-display").text(savedIp);

            const discovered = Array.isArray(data.discovered) ? data.discovered : [];
            const summary = discovered
                .map((item) => `${item.duid} -> ${item.ip_addr}${item.persisted ? " (saved)" : ""}`)
                .join(", ");
            status.text(summary || "No matching printers saved.");

            if (active.updated) {
                flash_message(
                    `LAN search updated ${active.name || "the active printer"} to ${savedIp}. Reload services to reconnect.`,
                    "success",
                );
            } else if (data.saved_count > 0) {
                flash_message(`LAN search saved ${data.saved_count} printer IP entr${data.saved_count === 1 ? "y" : "ies"} to default.json.`, "success");
            } else {
                flash_message("LAN search found printers, but none matched the configured DUIDs.", "warning");
            }
        } catch (err) {
            status.text("");
            flash_message(`LAN search failed: ${err}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    function titleCaseWords(text) {
        return String(text || "")
            .split("_")
            .filter(Boolean)
            .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
            .join(" ");
    }

    function renderPrinterSettingsSummary(data) {
        const statusEl = document.getElementById("printer-settings-summary-status");
        const highlightsEl = document.getElementById("printer-settings-highlights");
        const groupsEl = document.getElementById("printer-settings-groups");
        if (!statusEl || !highlightsEl || !groupsEl) {
            return;
        }

        const reportErrors = Object.values(data.reports || {})
            .filter((report) => report && report.available === false && report.error)
            .map((report) => report.label || report.name);

        const updated = new Date().toLocaleTimeString();
        statusEl.className = `mb-3 small ${reportErrors.length ? "text-warning" : "text-muted"}`;
        statusEl.textContent = reportErrors.length
            ? `Updated ${updated}. Partial data only: ${reportErrors.join(", ")}.`
            : `Updated ${updated}.`;

        const highlights = Array.isArray(data.highlights) ? data.highlights : [];
        if (!highlights.length) {
            highlightsEl.innerHTML = '<div class="text-muted small">No stable highlights available.</div>';
        } else {
            highlightsEl.innerHTML = highlights.map((item) => `
                <div class="border rounded p-2">
                    <div class="text-muted small">${escapeHtml(item.label || item.command || "Value")}</div>
                    <div class="fw-semibold">${escapeHtml(item.value || "unknown")}</div>
                    <div class="small font-monospace text-body-secondary">${escapeHtml(item.command || "")}</div>
                </div>
            `).join("");
        }

        const groups = data.groups || {};
        const groupHtml = Object.entries(groups)
            .filter(([, entries]) => Array.isArray(entries) && entries.length)
            .map(([name, entries]) => `
                <div>
                    <div class="text-muted small mb-1">${escapeHtml(titleCaseWords(name))}</div>
                    <div class="vstack gap-1">
                        ${entries.map((entry) => `
                            <div class="border rounded px-2 py-1 font-monospace small">
                                <span class="text-body-secondary me-2">${escapeHtml(entry.command || "")}</span>
                                <span>${escapeHtml(entry.value || "")}</span>
                            </div>
                        `).join("")}
                    </div>
                </div>
            `).join("");

        groupsEl.innerHTML = groupHtml || '<div class="text-muted small">No grouped settings available.</div>';
    }

    async function loadPrinterSettingsSummary() {
        const statusEl = document.getElementById("printer-settings-summary-status");
        if (!statusEl) {
            return;
        }
        statusEl.className = "mb-3 text-muted small";
        statusEl.textContent = "Reading printer settings...";

        const resp = await fetch(withActivePrinterQuery("/api/printer/settings-summary"));
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        renderPrinterSettingsSummary(data);
    }

    $("#printer-settings-refresh-btn").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await loadPrinterSettingsSummary();
        } catch (err) {
            const statusEl = document.getElementById("printer-settings-summary-status");
            if (statusEl) {
                statusEl.className = "mb-3 text-danger small";
                statusEl.textContent = `Refresh failed: ${err.message}`;
            }
        } finally {
            btn.prop("disabled", false);
        }
    });

    if (document.getElementById("printer-settings-summary-status")) {
        loadPrinterSettingsSummary().catch(function (err) {
            const statusEl = document.getElementById("printer-settings-summary-status");
            if (statusEl) {
                statusEl.className = "mb-3 text-warning small";
                statusEl.textContent = `Initial read failed: ${err.message}`;
            }
        });
    }

    $("#z-offset-refresh-btn").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        setZOffsetControlsEnabled(false);
        setZOffsetStatus("Reading live Z-offset from MQTT 1021...", "info");
        try {
            const data = await loadZOffset(true, {
                populateTarget: true,
                statusMessage: true,
                statusCategory: "success",
            });
            applyZOffsetState(data.z_offset, { populateTarget: true });
        } catch (err) {
            setZOffsetStatus(`Refresh failed: ${err.message}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#z-offset-set-btn").on("click", async function () {
        const btn = $(this);
        const targetRaw = $("#z-offset-target").val();
        const targetMm = normalizeZOffsetMm(targetRaw);
        if (targetMm === null) {
            setZOffsetStatus("Target Z-offset must be a valid number.", "warning");
            return;
        }

        btn.prop("disabled", true);
        setZOffsetStatus(`Setting Z-offset to ${targetMm.toFixed(2)} mm...`, "info");
        try {
            const data = await zOffsetRequest("/api/printer/z-offset", { target_mm: targetMm });
            applyZOffsetState(data.confirmed || data.target || data.current, {
                populateTarget: true,
                statusMessage: data.message,
                statusCategory: data.changed === false ? "secondary" : "success",
            });
        } catch (err) {
            setZOffsetStatus(`Set failed: ${err.message}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    async function nudgeZOffset(deltaMm) {
        setZOffsetStatus(`Nudging Z-offset by ${deltaMm > 0 ? "+" : ""}${deltaMm.toFixed(2)} mm...`, "info");
        const data = await zOffsetRequest("/api/printer/z-offset/nudge", { delta_mm: deltaMm });
        applyZOffsetState(data.confirmed || data.target || data.current, {
            populateTarget: true,
            statusMessage: data.message,
            statusCategory: "success",
        });
    }

    $("#z-offset-minus-btn").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await nudgeZOffset(-0.01);
        } catch (err) {
            setZOffsetStatus(`Nudge failed: ${err.message}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#z-offset-plus-btn").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await nudgeZOffset(0.01);
        } catch (err) {
            setZOffsetStatus(`Nudge failed: ${err.message}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    setZOffsetControlsEnabled(false);
    setZOffsetStatus("Reading live Z-offset from MQTT 1021...", "info");
    loadZOffset(true, {
        populateTarget: true,
        statusMessage: true,
        statusCategory: "secondary",
    }).catch(function (err) {
        setZOffsetControlsEnabled(false);
        setZOffsetStatus(`Initial read failed: ${err.message}`, "warning");
    });

    /**
     * Printer Control Logic
     */
    function sendPrinterGCode(gcode) {
        if (!gcode) return;
        fetch(withActivePrinterQuery("/api/printer/gcode"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ gcode: gcode })
        }).catch(err => console.error("Failed to send GCode:", err));
    }

    function sendPrinterHome(axis) {
        const targetAxis = axis || "all";
        fetch(withActivePrinterQuery("/api/printer/home"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ axis: targetAxis })
        }).catch(err => console.error("Failed to send home command:", err));
    }

    function sendPrintControl(value) {
        fetch(withActivePrinterQuery("/api/printer/control"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value })
        }).catch(err => console.error("Failed to send print control:", err));
    }

    const PRINT_CONTROL = {
        PAUSE: 2,
        RESUME: 3,
        STOP: 4,
    };

    // ct=1000 state values
    const PRINT_STATE = { IDLE: 0, PRINTING: 1, PAUSED: 2, CALIBRATING: 8, STOPPING: 9, PENDING_START: 10 };

    let _currentPrintState = PRINT_STATE.IDLE;
    let _lastStopCommandAt = 0;

    function _normalizePrintStateValue(value) {
        // Some firmware reports resume as ct=1000 value=3, while others report printing (1).
        if (value === 3) {
            return PRINT_STATE.PRINTING;
        }
        return value;
    }

    function _isPrintStateActive(state = _currentPrintState) {
        const normalizedState = _normalizePrintStateValue(state);
        return normalizedState === PRINT_STATE.PRINTING
            || normalizedState === PRINT_STATE.PAUSED
            || normalizedState === PRINT_STATE.CALIBRATING
            || normalizedState === PRINT_STATE.STOPPING
            || normalizedState === PRINT_STATE.PENDING_START;
    }

    function _isStoredFileSourcePath(filePath) {
        const path = String(filePath || "");
        return path.startsWith("/tmp/udisk/")
            || path.startsWith("/usr/data/local/model/");
    }

    function _updatePrintControlButtons(state) {
        const normalizedState = _normalizePrintStateValue(state);
        _currentPrintState = normalizedState;
        const printing = normalizedState === PRINT_STATE.PRINTING;
        const paused = normalizedState === PRINT_STATE.PAUSED;
        const stopping = normalizedState === PRINT_STATE.STOPPING;
        const preparing = normalizedState === PRINT_STATE.CALIBRATING;
        const pendingStart = normalizedState === PRINT_STATE.PENDING_START;
        const active = printing || paused || preparing || stopping || pendingStart;
        $("#print-pause").toggleClass("d-none", !printing || stopping);
        $("#print-resume").toggleClass("d-none", !paused || stopping);
        $("#print-stop").toggleClass("d-none", !active);
        $("#print-pause").prop("disabled", stopping);
        $("#print-resume").prop("disabled", stopping);
        $("#print-stop").prop("disabled", stopping);
        updateGCodeStorageControls();
        if (!isGCodeStorageLocked()) {
            maybeRefreshGCodeStorageAfterUnlock();
        }
        renderFilamentStatus();
    }

    const getStepDist = () => $('input[name="step-dist"]:checked').val() || "1";
    let _lastHomeCommandAt = 0;
    let _lastHomeCommand = null;

    function sendHomeCommand(axis) {
        const now = Date.now();
        if (_lastHomeCommand === axis && now - _lastHomeCommandAt < 1000) {
            return;
        }
        _lastHomeCommandAt = now;
        _lastHomeCommand = axis;
        sendPrinterHome(axis);
    }

    $("#move-x-plus").on("click", function () { sendPrinterGCode(`G91\nG0 X${getStepDist()} F3000\nG90`); return false; });
    $("#move-x-minus").on("click", function () { sendPrinterGCode(`G91\nG0 X-${getStepDist()} F3000\nG90`); return false; });
    $("#move-y-plus").on("click", function () { sendPrinterGCode(`G91\nG0 Y${getStepDist()} F3000\nG90`); return false; });
    $("#move-y-minus").on("click", function () { sendPrinterGCode(`G91\nG0 Y-${getStepDist()} F3000\nG90`); return false; });
    $("#move-z-plus").on("click", function () { sendPrinterGCode(`G91\nG0 Z${getStepDist()} F600\nG90`); return false; });
    $("#move-z-minus").on("click", function () { sendPrinterGCode(`G91\nG0 Z-${getStepDist()} F600\nG90`); return false; });

    $("#control-home-xy").on("click", function () { sendHomeCommand("xy"); return false; });
    $("#control-home-z").on("click", function () { sendHomeCommand("z"); return false; });
    $("#control-home-all").on("click", function () { sendHomeCommand("all"); return false; });

    // ------------------------------------------------------------------
    // Bed Level Map — shared rendering utilities
    // (defined at outer scope so they work with or without the debug tab)
    // ------------------------------------------------------------------

    /**
     * Map a deviation value to an RGB colour.
     * Negative values shade from blue (most negative) to white (zero).
     * Positive values shade from white (zero) to red (most positive).
     * The scale is symmetric: the larger absolute extreme defines ±range.
     *
     * @param {number} val   - cell value in mm
     * @param {number} range - symmetric range (Math.max(|min|, |max|))
     * @returns {string} CSS rgb(...) colour string
     */
    function bedLevelValueToColor(val, range) {
        if (range === 0) return "rgb(255,255,255)";
        const norm = Math.max(-1, Math.min(1, val / range)); // clamp to [-1, 1]
        if (norm < 0) {
            // blue → white  (t goes 0→1 as norm goes -1→0)
            const t = 1 + norm;
            const c = Math.round(t * 255);
            return `rgb(${c},${c},255)`;
        } else {
            // white → red  (t goes 0→1 as norm goes 0→1)
            const t = norm;
            const c = Math.round((1 - t) * 255);
            return `rgb(255,${c},${c})`;
        }
    }

    /**
     * Render the bed leveling heatmap into the specified wrapper element.
     * Draws column indices across the top and row indices down the left.
     *
     * @param {number[][]} grid     - 2-D array of mm deviation values
     * @param {number}     min      - global minimum value
     * @param {number}     max      - global maximum value
     * @param {string}     targetId - ID of wrapper element (default: "dbg-bedlevel-map-wrap")
     * @param {object}     [opts]   - optional settings
     * @param {boolean}    [opts.compact] - use smaller cells for side-by-side compare layout
     */
    function bedLevelRenderGrid(grid, min, max, targetId, opts) {
        const wrapId = targetId || "dbg-bedlevel-map-wrap";
        const compact = opts && opts.compact;
        const range = Math.max(Math.abs(min), Math.abs(max));
        const rows = grid.length;
        const cols = rows > 0 ? grid[0].length : 0;

        // Build a table: header row + one row per grid row
        const table = document.createElement("table");
        const fontSize = compact ? "0.65em" : "0.75em";
        const spacing = compact ? "2px" : "3px";
        table.style.cssText = `border-collapse:separate; border-spacing:${spacing}; font-size:${fontSize}; font-family:monospace;`;

        // Column header row
        const thead = document.createElement("thead");
        const headerRow = document.createElement("tr");
        const hdrPad = compact ? "1px 3px" : "2px 6px";
        // Empty corner cell above the row-label column
        const cornerTh = document.createElement("th");
        cornerTh.style.cssText = `padding:${hdrPad}; color:#6c757d; text-align:center;`;
        headerRow.appendChild(cornerTh);
        for (let c = 0; c < cols; c++) {
            const th = document.createElement("th");
            th.style.cssText = `padding:${hdrPad}; color:#6c757d; text-align:center;`;
            th.textContent = c;
            headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Data rows — rendered bottom-to-top so Row 0 (front of printer) appears
        // at the bottom of the table and Row N-1 (back) at the top, matching
        // the view when standing in front of the printer.
        const tbody = document.createElement("tbody");
        const cellPad = compact ? "2px 3px" : "5px 8px";
        const cellRadius = compact ? "2px" : "3px";
        for (let r = rows - 1; r >= 0; r--) {
            const tr = document.createElement("tr");

            // Row label
            const rowTh = document.createElement("th");
            rowTh.style.cssText = `padding:${hdrPad}; color:#6c757d; text-align:right; white-space:nowrap;`;
            rowTh.textContent = r;
            tr.appendChild(rowTh);

            for (let c = 0; c < grid[r].length; c++) {
                const val = grid[r][c];
                const td = document.createElement("td");
                const bg = bedLevelValueToColor(val, range);
                // Choose dark or light text based on perceived luminance of background
                // For a blue-white-red palette the midpoints are light, extremes need contrast.
                const normAbs = range > 0 ? Math.abs(val) / range : 0;
                const textColor = normAbs > 0.65 ? "#ffffff" : "#212529";
                td.style.cssText = [
                    `background:${bg}`,
                    `color:${textColor}`,
                    `padding:${cellPad}`,
                    `border-radius:${cellRadius}`,
                    "text-align:center",
                    "white-space:nowrap",
                    "cursor:default",
                ].join(";");
                const display = val >= 0 ? `+${val.toFixed(3)}` : val.toFixed(3);
                td.textContent = display;
                td.title = `Row ${r}, Col ${c}: ${display} mm`;
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);

        const wrap = document.getElementById(wrapId);
        if (wrap) {
            wrap.innerHTML = "";
            wrap.appendChild(table);
        }
    }

    // ------------------------------------------------------------------
    // Bed Level Map — Setup > Tools card
    // ------------------------------------------------------------------

    /**
     * localStorage key and cap for bed level snapshots.
     */
    const BED_SNAP_KEY = "ankerctl_bed_snapshots";
    const BED_SNAP_MAX = 10;

    /**
     * Currently loaded bed level data (set by bedLevelRead()).
     * Shape: {grid, min, max, rows, cols} or null.
     */
    let _currentBedData = null;

    /** Load snapshots array from localStorage. */
    function bedSnapLoad() {
        try {
            return JSON.parse(localStorage.getItem(BED_SNAP_KEY) || "[]");
        } catch (_) {
            return [];
        }
    }

    /** Persist snapshots array to localStorage. */
    function bedSnapSave(snaps) {
        localStorage.setItem(BED_SNAP_KEY, JSON.stringify(snaps));
    }

    /**
     * Add a new snapshot from bed data. Enforces BED_SNAP_MAX limit.
     * @param {{grid, min, max, rows, cols}} data
     */
    function bedSnapAdd(data) {
        if (!data) return;
        const snaps = bedSnapLoad();
        const now = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        const label =
            `${now.getFullYear()}/${pad(now.getMonth() + 1)}/${pad(now.getDate())} ` +
            `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
        snaps.push({ id: "snap_" + Date.now(), label, data });
        while (snaps.length > BED_SNAP_MAX) {
            snaps.shift();
        }
        bedSnapSave(snaps);
        bedSnapRefreshUI();
        flash_message("Bed map saved.", "success", 3000);
    }

    /**
     * Delete a snapshot by id and refresh UI.
     * @param {string} id
     */
    function bedSnapDelete(id) {
        const snaps = bedSnapLoad().filter(s => s.id !== id);
        bedSnapSave(snaps);
        bedSnapRefreshUI();
    }

    /**
     * Refresh both compare selects and the saved-snapshots list.
     */
    function bedSnapRefreshUI() {
        const snaps = bedSnapLoad();

        // Rebuild Snapshot A select
        const selA = document.getElementById("bed-snap-a-select");
        if (selA) {
            const prevA = selA.value;
            selA.innerHTML = "";
            if (snaps.length === 0) {
                selA.innerHTML = '<option value="" disabled selected>No bed maps saved yet</option>';
            } else {
                snaps.forEach(s => {
                    const opt = document.createElement("option");
                    opt.value = s.id;
                    opt.textContent = s.label;
                    if (s.id === prevA) opt.selected = true;
                    selA.appendChild(opt);
                });
            }
        }

        // Rebuild Snapshot B select (always has "live" option first)
        const selB = document.getElementById("bed-snap-b-select");
        if (selB) {
            const prevB = selB.value;
            selB.innerHTML = '<option value="live">Read live from printer</option>';
            snaps.forEach(s => {
                const opt = document.createElement("option");
                opt.value = s.id;
                opt.textContent = s.label;
                if (s.id === prevB) opt.selected = true;
                selB.appendChild(opt);
            });
        }

        // Rebuild saved-snapshots list
        const listEl = document.getElementById("bed-snap-list");
        if (listEl) {
            if (snaps.length === 0) {
                listEl.innerHTML = '<span class="text-muted small">No bed maps saved yet.</span>';
            } else {
                listEl.innerHTML = "";
                snaps.forEach(s => {
                    const row = document.createElement("div");
                    row.className = "d-flex justify-content-between align-items-center border-bottom py-1";
                    row.innerHTML =
                        `<span class="small">${escapeHtml(s.label)}</span>` +
                        `<button class="btn btn-sm btn-outline-danger bed-snap-delete-btn" ` +
                        `data-snap-id="${escapeHtml(s.id)}">` +
                        `<i class="bi bi-trash"></i></button>`;
                    listEl.appendChild(row);
                });
            }
        }
    }

    /**
     * Compute cell-wise diff grid: B minus A.
     * Returns null if grids have mismatched dimensions.
     * @param {number[][]} gridA
     * @param {number[][]} gridB
     * @returns {number[][]|null}
     */
    function bedLevelDiffGrid(gridA, gridB) {
        if (!gridA || !gridB || gridA.length !== gridB.length) return null;
        const result = [];
        for (let r = 0; r < gridA.length; r++) {
            if (gridA[r].length !== gridB[r].length) return null;
            result.push(gridA[r].map((v, c) => gridB[r][c] - v));
        }
        return result;
    }

    /**
     * Fetch bed level from printer and display in the Setup > Tools card.
     * Sets _currentBedData on success and enables the Save Snapshot button.
     */
    async function bedLevelRead() {
        const statusEl = document.getElementById("bed-level-status");
        const gridEl = document.getElementById("bed-level-grid");
        const statsEl = document.getElementById("bed-level-stats");
        const saveBtn = document.getElementById("bed-level-save-btn");
        const readBtn = document.getElementById("bed-level-read-btn");

        if (!statusEl) return;

        statusEl.innerHTML =
            '<div class="alert alert-info py-2 small mb-0">' +
            '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
            'Sending M420 V \u2014 waiting for printer response (up to 15 s)...</div>';
        if (gridEl) gridEl.style.display = "none";
        if (readBtn) readBtn.prop ? $(readBtn).prop("disabled", true) : (readBtn.disabled = true);

        try {
            const resp = await fetch(withActivePrinterQuery("/api/printer/bed-leveling"));
            const data = await resp.json();

            if (!resp.ok) {
                statusEl.innerHTML =
                    `<div class="alert alert-danger py-2 small mb-0">` +
                    `Error ${resp.status}: ${escapeHtml(data.error || "Unknown error")}</div>`;
                return;
            }

            _currentBedData = data;

            if (statsEl) {
                statsEl.innerHTML =
                    `<span><strong>Min:</strong> ${data.min.toFixed(3)} mm</span>` +
                    `<span><strong>Max:</strong> +${data.max.toFixed(3)} mm</span>` +
                    `<span><strong>Range:</strong> ${(data.max - data.min).toFixed(3)} mm</span>` +
                    `<span class="text-muted">(${data.rows}&times;${data.cols} grid)</span>`;
            }

            bedLevelRenderGrid(data.grid, data.min, data.max, "bed-level-map-wrap");

            statusEl.innerHTML = "";
            if (gridEl) gridEl.style.display = "block";
            if (saveBtn) saveBtn.disabled = false;
        } catch (err) {
            statusEl.innerHTML =
                `<div class="alert alert-danger py-2 small mb-0">` +
                `Request failed: ${escapeHtml(String(err))}</div>`;
        } finally {
            if (readBtn) readBtn.disabled = false;
        }
    }

    /**
     * Compare two bed level grids and render a 3-panel diff view.
     */
    async function bedLevelCompare() {
        const statusEl = document.getElementById("bed-compare-status");
        const resultEl = document.getElementById("bed-compare-result");
        const selA = document.getElementById("bed-snap-a-select");
        const selB = document.getElementById("bed-snap-b-select");
        const diffStatsEl = document.getElementById("bed-compare-diff-stats");

        if (!statusEl) return;
        statusEl.innerHTML = "";
        if (resultEl) resultEl.style.display = "none";

        const snapIdA = selA ? selA.value : "";
        if (!snapIdA) {
            statusEl.innerHTML = '<div class="alert alert-warning py-2 small mb-0">Please select Bed Map A first.</div>';
            return;
        }

        const snaps = bedSnapLoad();
        const snapA = snaps.find(s => s.id === snapIdA);
        if (!snapA) {
            statusEl.innerHTML = '<div class="alert alert-danger py-2 small mb-0">Bed Map A not found.</div>';
            return;
        }

        const snapBId = selB ? selB.value : "live";
        let dataB = null;

        if (snapBId === "live") {
            statusEl.innerHTML =
                '<div class="alert alert-info py-2 small mb-0">' +
                '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
                'Reading live data from printer...</div>';
            try {
                const resp = await fetch(withActivePrinterQuery("/api/printer/bed-leveling"));
                const parsed = await resp.json();
                if (!resp.ok) {
                    statusEl.innerHTML =
                        `<div class="alert alert-danger py-2 small mb-0">` +
                        `Printer error: ${escapeHtml(parsed.error || "Unknown error")}</div>`;
                    return;
                }
                dataB = parsed;
            } catch (err) {
                statusEl.innerHTML =
                    `<div class="alert alert-danger py-2 small mb-0">` +
                    `Request failed: ${escapeHtml(String(err))}</div>`;
                return;
            }
        } else {
            const snapB = snaps.find(s => s.id === snapBId);
            if (!snapB) {
                statusEl.innerHTML = '<div class="alert alert-danger py-2 small mb-0">Bed Map B not found.</div>';
                return;
            }
            dataB = snapB.data;
        }

        const dataA = snapA.data;
        const diffGrid = bedLevelDiffGrid(dataA.grid, dataB.grid);

        if (!diffGrid) {
            statusEl.innerHTML =
                '<div class="alert alert-warning py-2 small mb-0">' +
                'Cannot compare: grids have different dimensions.</div>';
            return;
        }

        // Render grids — compact mode for side-by-side compare layout
        const cmpOpts = { compact: true };
        bedLevelRenderGrid(dataA.grid, dataA.min, dataA.max, "bed-compare-a-wrap", cmpOpts);
        bedLevelRenderGrid(dataB.grid, dataB.min, dataB.max, "bed-compare-b-wrap", cmpOpts);

        const diffFlat = diffGrid.flat();
        const diffMin = Math.min(...diffFlat);
        const diffMax = Math.max(...diffFlat);
        bedLevelRenderGrid(diffGrid, diffMin, diffMax, "bed-compare-diff-wrap", cmpOpts);

        // Diff stats
        if (diffStatsEl) {
            const avg = diffFlat.reduce((a, b) => a + b, 0) / diffFlat.length;
            const maxImprovement = -diffMin; // most negative diff = biggest improvement (lower deviation)
            const maxRegression = diffMax;   // most positive diff = biggest regression
            diffStatsEl.innerHTML =
                `<div><strong>Avg shift:</strong> ${avg >= 0 ? "+" : ""}${avg.toFixed(3)} mm</div>` +
                `<div><strong>Max improvement:</strong> ${maxImprovement.toFixed(3)} mm</div>` +
                `<div><strong>Max regression:</strong> +${maxRegression.toFixed(3)} mm</div>`;
        }

        statusEl.innerHTML = "";
        if (resultEl) resultEl.style.display = "block";
    }

    async function bedLevelLoadLast() {
        const statusEl = document.getElementById("bed-level-status");
        const gridEl = document.getElementById("bed-level-grid");
        const statsEl = document.getElementById("bed-level-stats");
        const saveBtn = document.getElementById("bed-level-save-btn");

        if (!statusEl) return;
        statusEl.innerHTML =
            '<div class="alert alert-info py-2 small mb-0">' +
            '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
            'Loading last saved map\u2026</div>';
        if (gridEl) gridEl.style.display = "none";

        try {
            const resp = await fetch(withActivePrinterQuery("/api/printer/bed-leveling/last"));
            const data = await resp.json();

            if (!resp.ok) {
                statusEl.innerHTML =
                    `<div class="alert alert-warning py-2 small mb-0">` +
                    `${escapeHtml(data.error || "No saved map found")}</div>`;
                return;
            }

            _currentBedData = data;

            if (statsEl) {
                const ts = data.saved_at
                    ? ` &mdash; saved ${data.saved_at.replace(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/, "$1-$2-$3 $4:$5:$6")}`
                    : "";
                statsEl.innerHTML =
                    `<span><strong>Min:</strong> ${data.min.toFixed(3)} mm</span>` +
                    `<span><strong>Max:</strong> +${data.max.toFixed(3)} mm</span>` +
                    `<span><strong>Range:</strong> ${(data.max - data.min).toFixed(3)} mm</span>` +
                    `<span class="text-muted">(${data.rows}&times;${data.cols} grid${ts})</span>`;
            }

            bedLevelRenderGrid(data.grid, data.min, data.max, "bed-level-map-wrap");
            statusEl.innerHTML = "";
            if (gridEl) gridEl.style.display = "block";
            if (saveBtn) saveBtn.disabled = false;
        } catch (err) {
            statusEl.innerHTML =
                `<div class="alert alert-danger py-2 small mb-0">` +
                `Request failed: ${escapeHtml(String(err))}</div>`;
        }
    }

    // Wire up Setup > Tools bed level buttons
    $("#bed-level-read-btn").on("click", function () { bedLevelRead(); });
    $("#bed-level-load-last-btn").on("click", function () { bedLevelLoadLast(); });
    $("#bed-level-save-btn").on("click", function () {
        bedSnapAdd(_currentBedData);
    });
    $("#bed-compare-btn").on("click", function () { bedLevelCompare(); });

    // Delegate delete buttons in snapshot list
    $(document).on("click", ".bed-snap-delete-btn", function () {
        const id = $(this).data("snap-id");
        if (id) bedSnapDelete(id);
    });

    // Initialize snapshot UI on page load
    bedSnapRefreshUI();

    /**
     * Auto-Leveling — state machine for polling after bed level command.
     * We listen on the MQTT WebSocket (commandType 1000) to detect completion.
     *
     * State:
     *   _waitingForBedLevel: false = idle, "heating" = saw active, "idle" = done
     */
    let _waitingForBedLevel = false;
    let _bedLevelPollTimeout = null;
    const BED_LEVEL_TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes

    /**
     * Called by the MQTT message handler when commandType 1000 arrives.
     * value=0 → idle/finished, value=1 → active.
     */
    function _onMqttStateChange(value) {
        if (!_waitingForBedLevel) return;

        if (value === 1) {
            // Printer became active (heating / probing)
            _waitingForBedLevel = "active";
        } else if (value === 0 && _waitingForBedLevel === "active") {
            // Printer returned to idle after being active → leveling done
            _cancelBedLevelWait();
            const statusEl = document.getElementById("bed-level-status");
            if (statusEl) {
                statusEl.innerHTML =
                    '<div class="alert alert-success py-2 small mb-0">' +
                    '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
                    'Bed leveling complete — reading grid...</div>';
            }
            bedLevelRead();
        }
    }

    function _cancelBedLevelWait() {
        _waitingForBedLevel = false;
        if (_bedLevelPollTimeout) {
            clearTimeout(_bedLevelPollTimeout);
            _bedLevelPollTimeout = null;
        }
    }

    /**
     * Auto-Leveling
     */
    $("#auto-level-btn").on("click", async function () {
        if (!confirm("Start Auto-Leveling? Make sure the print bed is clear.")) return;
        const btn = $(this);
        btn.prop("disabled", true).html('<i class="bi bi-hourglass-split"></i> Leveling...');
        try {
            const resp = await fetch(withActivePrinterQuery("/api/printer/autolevel"), { method: "POST" });
            if (resp.ok) {
                flash_message("Auto-Leveling started — the printer will now probe the bed.", "success");

                // Start waiting for bed leveling to complete via MQTT state changes
                _waitingForBedLevel = true;
                const statusEl = document.getElementById("bed-level-status");
                const gridEl = document.getElementById("bed-level-grid");
                if (statusEl) {
                    statusEl.innerHTML =
                        '<div class="alert alert-info py-2 small mb-0">' +
                        '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
                        'Waiting for bed leveling to complete\u2026</div>';
                }
                if (gridEl) gridEl.style.display = "none";

                // Timeout after 10 minutes
                _bedLevelPollTimeout = setTimeout(function () {
                    if (_waitingForBedLevel) {
                        _cancelBedLevelWait();
                        if (statusEl) {
                            statusEl.innerHTML =
                                '<div class="alert alert-warning py-2 small mb-0">' +
                                'Bed leveling timed out (10 min). Click "Read" to check manually.</div>';
                        }
                    }
                }, BED_LEVEL_TIMEOUT_MS);
            } else {
                const data = await resp.json().catch(() => ({}));
                const msg = data.error ? data.error : `HTTP ${resp.status}`;
                flash_message(`Auto-Leveling failed: ${msg}`, "danger");
            }
        } catch (err) {
            flash_message(`Auto-Leveling failed: ${err}`, "danger");
        } finally {
            btn.prop("disabled", false).html('<i class="bi bi-rulers"></i> Start Auto-Level');
        }
    });

    /**
     * Temperature Control Logic
     */
    $("#set-nozzle-temp").on("change", function () {
        const raw = parseInt($(this).val(), 10);
        if (isNaN(raw)) return;
        const max = parseInt($(this).attr("max"), 10) || 260;
        const temp = Math.max(0, Math.min(max, raw));
        $(this).val(temp);
        sendPrinterGCode(`M104 S${temp}`);
    });

    $("#set-bed-temp").on("change", function () {
        const raw = parseInt($(this).val(), 10);
        if (isNaN(raw)) return;
        const max = parseInt($(this).attr("max"), 10) || 100;
        const temp = Math.max(0, Math.min(max, raw));
        $(this).val(temp);
        sendPrinterGCode(`M140 S${temp}`);
    });

    $(".preheat-preset").on("click", function () {
        const nozzle = $(this).attr("data-nozzle");
        const bed = $(this).attr("data-bed");
        sendPrinterGCode(`M104 S${nozzle}\nM140 S${bed}`);
        return false;
    });

    /**
     * Snapshot Button
     */
    $(document).on("click", "#snapshot-btn, #snapshot-btn-secondary", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            const resp = await fetch(`/api/snapshot?printer_index=${encodeURIComponent(getActivePrinterIndex())}`);
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                const msg = data.error || `HTTP ${resp.status}`;
                const banner = /^snapshot\b/i.test(msg) ? msg : `Snapshot failed: ${msg}`;
                flash_message(banner, "warning");
                return;
            }

            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `ankerctl_snapshot_${Date.now()}.jpg`;
            a.click();
            URL.revokeObjectURL(url);
            flash_message("Snapshot downloaded and saved to Snapshots.", "success", 4000);
            loadTimelapseSnapshots();
        } catch (err) {
            const msg = err.message || String(err);
            const banner = /^snapshot\b/i.test(msg) ? msg : `Snapshot failed: ${msg}`;
            flash_message(banner, "warning");
        } finally {
            btn.prop("disabled", false);
        }
    });

    function applyCameraSettingsToForm(camera) {
        $("#camera-external-name").val(camera && camera.external ? camera.external.name || "" : "");
        $("#camera-external-stream-url").val(camera && camera.external ? camera.external.stream_url || "" : "");
        $("#camera-external-snapshot-url").val(camera && camera.external ? camera.external.snapshot_url || "" : "");
    }

    async function loadCameraSettings() {
        if (_cameraSettingsLoading) {
            return;
        }
        _cameraSettingsLoading = true;
        try {
            const resp = await fetch(withActivePrinterQuery("/api/settings/camera"));
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            applyCameraRuntimeState(data.camera || {});
            applyCameraSettingsToForm(data.camera || {});
        } catch (err) {
            flash_message(`Failed to load camera settings: ${err.message || err}`, "danger");
        } finally {
            _cameraSettingsLoading = false;
        }
    }

    async function saveCameraSettings(payload, successMessage = null) {
        const resp = await fetch(withActivePrinterQuery("/api/settings/camera"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ camera: payload }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        applyCameraRuntimeState(data.camera || {});
        applyCameraSettingsToForm(data.camera || {});
        if (_cameraState.effectiveSource !== "printer" && videoEnabled) {
            setPrinterVideoEnabled(false);
        }
        if (successMessage) {
            flash_message(successMessage, "success");
        }
        return data.camera || {};
    }

    $("#camera-save").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await saveCameraSettings({
                external: {
                    name: $("#camera-external-name").val().trim(),
                    stream_url: $("#camera-external-stream-url").val().trim(),
                    snapshot_url: $("#camera-external-snapshot-url").val().trim(),
                },
            }, "External camera settings saved");
        } catch (err) {
            flash_message(`Failed to save camera settings: ${err.message || err}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#camera-source-select").on("change", async function () {
        const select = $(this);
        const selected = select.val() || "printer";
        const previousExternalPreviewEnabled = _externalCameraPreviewEnabled;
        const previousPrinterVideoEnabled = videoEnabled;
        select.prop("disabled", true);
        try {
            if (selected === "external") {
                _externalCameraPreviewEnabled = false;
                _cameraState.previewError = null;
            } else {
                setPrinterVideoEnabled(false);
            }
            const successMessage = selected === "external"
                ? (_cameraState.externalConfigured
                    ? "Camera source switched to external camera."
                    : "External camera selected. Finish setup in Setup -> Camera to use it.")
                : "Camera source switched to printer camera.";
            await saveCameraSettings({ source: selected }, successMessage);
        } catch (err) {
            _externalCameraPreviewEnabled = previousExternalPreviewEnabled;
            if (previousPrinterVideoEnabled !== videoEnabled) {
                setPrinterVideoEnabled(previousPrinterVideoEnabled);
            }
            flash_message(`Failed to switch camera source: ${err.message || err}`, "danger");
            await loadCameraSettings();
        } finally {
            select.prop("disabled", false);
        }
    });

    $("#launcher-download-btn").on("click", async function () {
        const btn = $(this);
        const installDir = ($("#launcher-install-dir").val() || "").trim();
        if (!installDir) {
            flash_message("Enter the Ankerctl folder before downloading the launcher.", "warning");
            return;
        }
        btn.prop("disabled", true);
        try {
            const resp = await fetch("/api/settings/launcher-bat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ install_dir: installDir }),
            });
            const data = await resp.blob();
            if (!resp.ok) {
                const text = await data.text().catch(() => "");
                let errorMessage = `HTTP ${resp.status}`;
                try {
                    const parsed = JSON.parse(text);
                    errorMessage = parsed.error || errorMessage;
                } catch (_err) {
                    if (text) {
                        errorMessage = text;
                    }
                }
                throw new Error(errorMessage);
            }

            const url = URL.createObjectURL(data);
            const link = document.createElement("a");
            link.href = url;
            link.download = "ankerctl-launcher.bat";
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
            flash_message("Windows launcher downloaded.", "success");
        } catch (err) {
            flash_message(`Failed to download launcher: ${err.message || err}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    loadCameraSettings();

    /**
     * GCode Console
     */
    function gcodeLog(msg) {
        const log = $("#gcode-log");
        const logEl = log.get(0);
        if (!logEl) {
            return;
        }
        const ts = new Date().toLocaleTimeString();
        const line = document.createTextNode(`[${ts}] ${msg}\n`);
        log.append(line);
        logEl.scrollTop = logEl.scrollHeight;
    }

    function normalizeGCodeText(gcode) {
        if (!gcode) {
            return "";
        }
        return gcode
            .split(/\r?\n/)
            .map(line => line.split(";", 1)[0].trim())
            .filter(line => line.length > 0)
            .join("\n");
    }

    function looksLikeGCodeJob(gcode) {
        if (!gcode) {
            return false;
        }
        const nonEmptyLines = gcode.split(/\r?\n/).filter(line => line.trim().length > 0).length;
        return nonEmptyLines >= 100
            || /(^|\n)\s*;LAYER_COUNT:/i.test(gcode)
            || /(^|\n)\s*; estimated printing time/i.test(gcode)
            || /(^|\n)\s*; generated by /i.test(gcode);
    }

    function gcodeStorageSourceLabel(source) {
        return source === "usb" ? "thumb drive" : "printer storage";
    }

    let _selectedGCodeStorageFile = null;
    let _gcodeStorageLoading = false;
    let _gcodeStorageRefreshDeferred = false;

    function isGCodeStorageLocked() {
        const normalizedState = _normalizePrintStateValue(_currentPrintState);
        return normalizedState === PRINT_STATE.PRINTING
            || normalizedState === PRINT_STATE.PAUSED
            || normalizedState === PRINT_STATE.CALIBRATING
            || normalizedState === PRINT_STATE.STOPPING
            || normalizedState === PRINT_STATE.PENDING_START;
    }

    function isGCodeTabVisible() {
        const pane = document.getElementById("gcode");
        return !!(pane && pane.classList.contains("show"));
    }

    function buildGCodeThumbnail(url, altText) {
        const safeAlt = escapeHtml(altText || "GCode thumbnail");
        const safeUrl = url ? escapeHtml(url) : "";
        const img = safeUrl
            ? `<img src="${safeUrl}" alt="${safeAlt}" class="gcode-thumbnail-image" loading="lazy" onerror="this.remove()">`
            : "";
        return (
            '<div class="gcode-thumbnail-shell">' +
            '<div class="gcode-thumbnail-fallback"><i class="bi bi-card-image"></i></div>' +
            img +
            "</div>"
        );
    }

    function formatStorageTimestamp(timestamp) {
        const value = Number(timestamp);
        if (!Number.isFinite(value) || value <= 0) {
            return "-";
        }
        return new Date(value * 1000).toLocaleString();
    }

    function setGCodeStoragePrintEnabled(enabled) {
        $("#gcode-storage-print").prop("disabled", !enabled || _gcodeStorageLoading || isGCodeStorageLocked());
    }

    function setGCodeStorageBusy(busy) {
        _gcodeStorageLoading = busy;
        updateGCodeStorageControls();
    }

    function updateGCodeStorageControls() {
        const busy = _gcodeStorageLoading;
        const locked = isGCodeStorageLocked();
        $("#gcode-storage-refresh").prop("disabled", busy || locked);
        $("#gcode-storage-source").prop("disabled", busy || locked);
        $("#gcode-storage-print").prop("disabled", busy || locked || !_selectedGCodeStorageFile);
        $("#gcode-storage-lock-note").toggleClass("d-none", !locked);
    }

    function deferGCodeStorageRefresh(message) {
        _gcodeStorageRefreshDeferred = true;
        updateGCodeStorageControls();
        if (message) {
            $("#gcode-storage-status").text(message);
        }
    }

    function maybeRefreshGCodeStorageAfterUnlock() {
        if (!_gcodeStorageRefreshDeferred || _gcodeStorageLoading || isGCodeStorageLocked() || !isGCodeTabVisible()) {
            return;
        }
        _gcodeStorageRefreshDeferred = false;
        loadGCodeStorageFiles();
    }

    function renderGCodeStorageSelection(file) {
        const selected = $("#gcode-storage-selected");
        _selectedGCodeStorageFile = file || null;
        if (!selected.length) {
            return;
        }
        if (!file) {
            setGCodeStoragePrintEnabled(false);
            selected.hide().empty();
            return;
        }

        const source = gcodeStorageSourceLabel(file.source || $("#gcode-storage-source").val() || "onboard");
        selected
            .html(
                '<div class="d-flex align-items-start gap-3">' +
                buildGCodeThumbnail(file.thumbnail_url, file.name || "Stored file thumbnail") +
                '<div class="flex-grow-1 min-w-0">' +
                `<div class="fw-semibold">${escapeHtml(file.name || "Unnamed file")}</div>` +
                `<div class="text-break"><code>${escapeHtml(file.path || "-")}</code></div>` +
                `<div class="text-muted">Modified: ${escapeHtml(formatStorageTimestamp(file.timestamp))} · Source: ${escapeHtml(source)}</div>` +
                "</div>" +
                "</div>"
            )
            .show();
        setGCodeStoragePrintEnabled(true);
    }

    function renderGCodeStorageFiles(source, files) {
        const list = $("#gcode-storage-list");
        const status = $("#gcode-storage-status");
        if (!list.length) {
            return;
        }

        list.empty();
        renderGCodeStorageSelection(null);

        if (!Array.isArray(files) || files.length === 0) {
            list.append(
                `<div class="list-group-item text-muted small">No files found on ${escapeHtml(gcodeStorageSourceLabel(source))}.</div>`
            );
            status.text(`No files found on ${gcodeStorageSourceLabel(source)}.`);
            return;
        }

        let firstItem = null;
        let firstFile = null;
        files.forEach((file) => {
            const normalizedFile = Object.assign({}, file || {}, { source: source });
            const name = normalizedFile && normalizedFile.name ? String(normalizedFile.name) : "Unnamed file";
            const path = normalizedFile && normalizedFile.path ? String(normalizedFile.path) : "-";
            const item = $(`
                <button type="button" class="list-group-item list-group-item-action text-start">
                    <div class="d-flex align-items-start gap-3">
                        ${buildGCodeThumbnail(normalizedFile.thumbnail_url, name)}
                        <div class="flex-grow-1 min-w-0">
                            <div class="fw-semibold text-truncate">${escapeHtml(name)}</div>
                            <div class="small text-muted text-truncate"><code>${escapeHtml(path)}</code></div>
                            <div class="small text-muted">Modified: ${escapeHtml(formatStorageTimestamp(file.timestamp))}</div>
                        </div>
                    </div>
                </button>
            `);
            item.on("click", function () {
                $("#gcode-storage-list .list-group-item").removeClass("active");
                item.addClass("active");
                renderGCodeStorageSelection(normalizedFile);
            });
            list.append(item);
            if (!firstItem) {
                firstItem = item;
                firstFile = normalizedFile;
            }
        });

        if (firstItem && firstFile) {
            firstItem.addClass("active");
            renderGCodeStorageSelection(firstFile);
        }

        status.text(`Loaded ${files.length} file(s) from ${gcodeStorageSourceLabel(source)}.`);
    }

    async function loadGCodeStorageFiles() {
        const source = $("#gcode-storage-source").val() || "onboard";
        const status = $("#gcode-storage-status");
        const lockedMessage = `File list is paused while the printer is busy. It will refresh after the print stops.`;

        if (isGCodeStorageLocked()) {
            deferGCodeStorageRefresh(lockedMessage);
            return;
        }

        setGCodeStorageBusy(true);
        _gcodeStorageRefreshDeferred = false;
        status.text(`Loading ${gcodeStorageSourceLabel(source)} files...`);
        try {
            const resp = await fetch(withActivePrinterQuery(`/api/files/printer?source=${encodeURIComponent(source)}`));
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            if (isGCodeStorageLocked()) {
                deferGCodeStorageRefresh(lockedMessage);
                return;
            }
            renderGCodeStorageFiles(source, data.files || []);
        } catch (err) {
            if (isGCodeStorageLocked()) {
                deferGCodeStorageRefresh(lockedMessage);
                return;
            }
            $("#gcode-storage-list").html(
                `<div class="list-group-item text-danger small">Failed to load ${escapeHtml(gcodeStorageSourceLabel(source))}: ${escapeHtml(err.message)}</div>`
            );
            renderGCodeStorageSelection(null);
            status.text(`Failed to load ${gcodeStorageSourceLabel(source)}.`);
            gcodeLog(`Error loading ${gcodeStorageSourceLabel(source)} files: ${err.message}`);
        } finally {
            setGCodeStorageBusy(false);
        }
    }

    function setGCodeConsoleBusy(busy) {
        $("#gcode-file-send").prop("disabled", busy);
        $("#gcode-text-send").prop("disabled", busy);
        $("#gcode-file").prop("disabled", busy);
        $("#gcode-input").prop("disabled", busy);
    }

    async function sendGCodeWithLog(gcode) {
        const normalized = normalizeGCodeText(gcode);
        if (!normalized) {
            gcodeLog("✗ No executable GCode found");
            return false;
        }

        gcodeLog(`» ${normalized.replace(/\n/g, " | ")}`);
        const resp = await fetch(withActivePrinterQuery("/api/printer/gcode"), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ gcode: normalized })
        });

        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            gcodeLog("✓ Sent successfully");
            return true;
        }

        gcodeLog(`✗ Error ${resp.status}: ${data.error || "Unknown error"}`);
        return false;
    }

    async function uploadGCodeFileWithLog(file, startPrint = true) {
        if (!file) {
            gcodeLog("✗ No file selected");
            return false;
        }

        const formData = new FormData();
        formData.append("file", file, file.name);
        formData.append("print", startPrint ? "true" : "false");

        const action = startPrint ? "Uploading print job" : "Uploading file";
        gcodeLog(`» ${action}: ${file.name} (${formatBytes(file.size)})`);

        const resp = await fetch(withActivePrinterQuery("/api/files/local"), {
            method: "POST",
            body: formData,
        });

        if (resp.ok) {
            const data = await resp.json().catch(() => ({}));
            const rate = data.upload_rate_mbps;
            const source = data.upload_rate_source;
            const rateText = rate ? ` using ${rate} Mbps (${source})` : "";
            gcodeLog(startPrint
                ? `✓ Upload started${rateText}, printer should begin after transfer completes`
                : `✓ Upload started${rateText}`);
            return true;
        }

        const text = (await resp.text()).trim();
        gcodeLog(`✗ Error ${resp.status}: ${text || "Upload failed"}`);
        return false;
    }

    // File upload
    $("#gcode-file-send").on("click", async function () {
        const fileInput = document.getElementById("gcode-file");
        if (!fileInput.files.length) {
            gcodeLog("✗ No file selected");
            return;
        }

        setGCodeConsoleBusy(true);
        try {
            const ok = await uploadGCodeFileWithLog(fileInput.files[0], true);
            if (ok) {
                fileInput.value = "";
            }
        } catch (err) {
            gcodeLog(`✗ Failed: ${err.message}`);
        } finally {
            setGCodeConsoleBusy(false);
        }
    });

    // Custom text input
    $("#gcode-text-send").on("click", async function () {
        const input = $("#gcode-input");
        const raw = input.val();
        if (!raw || !raw.trim()) {
            gcodeLog("✗ No GCode entered");
            return;
        }

        setGCodeConsoleBusy(true);
        try {
            let ok = false;
            if (looksLikeGCodeJob(raw)) {
                const filename = `custom-gcode-${Date.now()}.gcode`;
                const file = new File([raw], filename, { type: "text/plain" });
                gcodeLog("Detected slicer-style GCode job, using file upload path");
                ok = await uploadGCodeFileWithLog(file, true);
            } else {
                ok = await sendGCodeWithLog(raw);
            }

            if (ok) {
                input.val("");
            }
        } catch (err) {
            gcodeLog(`✗ Failed: ${err.message}`);
        } finally {
            setGCodeConsoleBusy(false);
        }
    });

    // Enter key in textarea sends
    $("#gcode-input").on("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            $("#gcode-text-send").click();
        }
    });

    $(document).on("click", "#gcode-storage-refresh", function () {
        loadGCodeStorageFiles();
    });

    $(document).on("change", "#gcode-storage-source", function () {
        loadGCodeStorageFiles();
    });

    $(document).on("click", "#gcode-storage-print", async function () {
        if (!_selectedGCodeStorageFile || !_selectedGCodeStorageFile.path) {
            gcodeLog("No stored file selected");
            flash_message("Select a stored file before printing.", "warning");
            return;
        }
        if (isGCodeStorageLocked()) {
            gcodeLog("Stored file actions are paused while the printer is busy");
            flash_message("The printer is busy right now. Wait for it to return to idle before browsing or starting another stored file.", "warning");
            deferGCodeStorageRefresh("File list is paused while the printer is busy. It will refresh after the print stops.");
            return;
        }

        const source = _selectedGCodeStorageFile.source || $("#gcode-storage-source").val() || "onboard";
        const fileName = _selectedGCodeStorageFile.name || "stored file";

        setGCodeStorageBusy(true);
        try {
            gcodeLog(`Starting ${fileName} from ${gcodeStorageSourceLabel(source)}`);
            const resp = await fetch(withActivePrinterQuery("/api/files/printer/print"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    source: source,
                    path: _selectedGCodeStorageFile.path,
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            const confirmedName = data.name || fileName;
            $("#print-name").text(confirmedName);
            if (_currentPrintState === PRINT_STATE.IDLE) {
                _updatePrintControlButtons(PRINT_STATE.PENDING_START);
            }
            gcodeLog(`Printer confirmed stored file start for ${confirmedName}`);
            flash_message(`Printer confirmed ${confirmedName} from ${gcodeStorageSourceLabel(source)}.`, "success", 4000);
        } catch (err) {
            gcodeLog(`Failed to start stored file: ${err.message}`);
            flash_message(`Failed to start stored file: ${err.message}`, "danger");
        } finally {
            setGCodeStorageBusy(false);
        }
    });

    const homeTabBtn = document.querySelector('button[data-bs-target="#home"]');
    if (homeTabBtn) {
        homeTabBtn.addEventListener("shown.bs.tab", function () {
            startHomeConsoleViewer();
        });
        homeTabBtn.addEventListener("hidden.bs.tab", function () {
            stopHomeConsoleViewer();
        });
    }

    const homeTabPane = document.getElementById("home");
    if (homeTabPane && homeTabPane.classList.contains("show")) {
        startHomeConsoleViewer();
    }

    const gcodeTabBtn = document.querySelector('button[data-bs-target="#gcode"]');
    if (gcodeTabBtn) {
        gcodeTabBtn.addEventListener("shown.bs.tab", function () {
            loadGCodeStorageFiles();
        });
    }

    updateGCodeStorageControls();
    startPrinterAlertPolling();

    $("#print-pause").on("click", function () {
        sendPrintControl(PRINT_CONTROL.PAUSE);
        _updatePrintControlButtons(PRINT_STATE.PAUSED);
        return false;
    });
    $("#print-resume").on("click", function () {
        sendPrintControl(PRINT_CONTROL.RESUME);
        _updatePrintControlButtons(PRINT_STATE.PRINTING);
        return false;
    });
    $("#print-stop").on("click", function () {
        const now = Date.now();
        if (now - _lastStopCommandAt < 1000) {
            return false;
        }
        const preparing = _currentPrintState === PRINT_STATE.CALIBRATING;
        const pendingStart = _currentPrintState === PRINT_STATE.PENDING_START;
        const confirmText = preparing
            ? "Cancel the printer prepare phase before the print starts?"
            : pendingStart
                ? "Cancel the pending print before it starts?"
                : "Are you sure you want to stop the print?";
        if (confirm(confirmText)) {
            _lastStopCommandAt = now;
            sendPrintControl(PRINT_CONTROL.STOP);
        }
        return false;
    });

    /**
     * Temperature Graph — client‑side ring buffer + Chart.js
     */
    const TEMP_BUFFER_MAX = 3600;  // 1h at 1 sample/sec
    let tempWindowSec = 300;       // default 5m
    const tempData = [];           // [{t: Date, nC, nT, bC, bT}]
    let lastTempPush = 0;
    let _pendingNozzle = { c: null, t: null };
    let _pendingBed = { c: null, t: null };

    function pushTempData(type, current, target) {
        if (type === "nozzle") {
            _pendingNozzle.c = current;
            if (target !== null) { _pendingNozzle.t = target; }
        }
        else if (type === "bed") {
            _pendingBed.c = current;
            if (target !== null) { _pendingBed.t = target; }
        }

        const now = Date.now();
        if (now - lastTempPush < 1000) return; // 1s throttle
        lastTempPush = now;

        if (_pendingNozzle.c === null && _pendingBed.c === null) return;

        tempData.push({
            t: new Date(),
            nC: _pendingNozzle.c, nT: _pendingNozzle.t,
            bC: _pendingBed.c, bT: _pendingBed.t,
        });
        if (tempData.length > TEMP_BUFFER_MAX) tempData.shift();
    }

    // Initialize Chart.js (only if available)
    let tempChart = null;
    const chartCanvas = document.getElementById("temp-chart");

    if (typeof Chart !== "undefined" && chartCanvas) {
        const ctx = chartCanvas.getContext("2d");
        tempChart = new Chart(ctx, {
            type: "line",
            data: {
                labels: [],
                datasets: [
                    {
                        label: "Nozzle",
                        borderColor: "#ff6384",
                        backgroundColor: "rgba(255,99,132,0.1)",
                        data: [], fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2,
                    },
                    {
                        label: "Nozzle Target",
                        borderColor: "#ff6384",
                        borderDash: [5, 5],
                        data: [], fill: false, tension: 0, pointRadius: 0, borderWidth: 1,
                    },
                    {
                        label: "Bed",
                        borderColor: "#36a2eb",
                        backgroundColor: "rgba(54,162,235,0.1)",
                        data: [], fill: false, tension: 0.3, pointRadius: 0, borderWidth: 2,
                    },
                    {
                        label: "Bed Target",
                        borderColor: "#36a2eb",
                        borderDash: [5, 5],
                        data: [], fill: false, tension: 0, pointRadius: 0, borderWidth: 1,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: {
                        ticks: { color: "#aaa", maxTicksLimit: 8 },
                        grid: { color: "rgba(255,255,255,0.05)" },
                    },
                    y: {
                        beginAtZero: true,
                        title: { display: true, text: "°C", color: "#aaa" },
                        ticks: { color: "#aaa" },
                        grid: { color: "rgba(255,255,255,0.08)" },
                    },
                },
                plugins: {
                    legend: { labels: { color: "#ccc", usePointStyle: true } },
                },
            },
        });

        // Refresh chart every 2s
        setInterval(function () {
            if (!tempChart || tempData.length === 0) return;
            const cutoff = Date.now() - tempWindowSec * 1000;
            const visible = tempData.filter(d => d.t.getTime() >= cutoff);
            tempChart.data.labels = visible.map(d => d.t.toLocaleTimeString());
            tempChart.data.datasets[0].data = visible.map(d => d.nC);
            tempChart.data.datasets[1].data = visible.map(d => d.nT);
            tempChart.data.datasets[2].data = visible.map(d => d.bC);
            tempChart.data.datasets[3].data = visible.map(d => d.bT);
            tempChart.update();
        }, 2000);
    }

    // Time window selector
    $(".temp-window").on("click", function () {
        $(".temp-window").removeClass("active");
        $(this).addClass("active");
        tempWindowSec = parseInt($(this).data("window"), 10) || 300;
    });

    /**
     * Print History Tab
     */
    let historyOffset = 0;
    let historyAllPrinters = false;
    const HISTORY_LIMIT = 25;
    const selectedHistoryIds = new Set();

    function formatDuration(sec) {
        if (!sec) return "-";
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
    }

    function statusBadge(status) {
        const map = {
            started: '<span class="badge bg-primary">In Progress</span>',
            finished: '<span class="badge bg-success">Finished</span>',
            failed: '<span class="badge bg-danger">Failed</span>',
        };
        return map[status] || `<span class="badge bg-secondary">${escapeHtml(status)}</span>`;
    }

    function updateHistorySelectionUi() {
        const deleteButton = $("#history-delete-selected");
        const count = selectedHistoryIds.size;
        deleteButton
            .prop("disabled", count === 0)
            .html(`<i class="bi bi-trash3"></i> Delete Selected${count > 0 ? ` (${count})` : ""}`);

        const checkboxes = $(".history-select-checkbox").filter(function () {
            return !$(this).prop("disabled");
        });
        const checked = checkboxes.filter(function () {
            return $(this).prop("checked");
        });
        const selectAll = $("#history-select-all");
        if (selectAll.length) {
            const selectAllEl = selectAll.get(0);
            selectAll.prop("checked", checkboxes.length > 0 && checked.length === checkboxes.length);
            if (selectAllEl) {
                selectAllEl.indeterminate = checked.length > 0 && checked.length < checkboxes.length;
            }
        }
    }

    function loadHistory(append) {
        fetch(withHistoryScopeQuery(`/api/history?limit=${HISTORY_LIMIT}&offset=${historyOffset}`))
            .then(r => r.json())
            .then(data => {
                const tbody = $("#history-tbody");
                if (!append) {
                    tbody.empty();
                    selectedHistoryIds.clear();
                }
                if (data.entries.length === 0 && !append) {
                    const emptyMsg = historyAllPrinters ? "No history yet across all printers" : "No history yet";
                    tbody.html(`<tr><td colspan="6" class="text-center text-muted py-4">${emptyMsg}</td></tr>`);
                }
                data.entries.forEach(e => {
                    const started = e.started_at ? new Date(e.started_at + "Z").toLocaleString() : "-";
                    const safeFilename = escapeHtml(e.filename);
                    const safePrinterName = escapeHtml(e.printer_name || "Unknown");
                    const thumbnail = buildGCodeThumbnail(e.thumbnail_url, e.filename || "History thumbnail");
                    const canDelete = e.status !== "started";
                    const isChecked = selectedHistoryIds.has(e.id);
                    const checkboxCell = canDelete
                        ? `<input type="checkbox" class="form-check-input history-select-checkbox" data-history-id="${e.id}" ${isChecked ? "checked" : ""} aria-label="Select ${safeFilename}">`
                        : '<input type="checkbox" class="form-check-input" disabled aria-label="Cannot delete in-progress history entry">';
                    const printerAttr = (e.printer_index !== null && e.printer_index !== undefined)
                        ? ` data-history-printer-index="${e.printer_index}"`
                        : "";
                    const actionCell = e.can_reprint
                        ? `<button class="btn btn-sm btn-outline-primary history-reprint-btn" data-history-id="${e.id}" data-history-name="${safeFilename}"${printerAttr}>Reprint</button>`
                        : '<span class="text-muted small">-</span>';
                    // Show printer attribution below the filename when viewing all printers
                    const printerMeta = historyAllPrinters
                        ? `<div class="text-muted small">${safePrinterName}</div>`
                        : "";
                    const row = `<tr>
                        <td class="text-center align-middle">${checkboxCell}</td>
                        <td class="history-file-cell" title="${safeFilename}">
                            <div class="d-flex align-items-center gap-2">
                                ${thumbnail}
                                <div>
                                    <div class="text-truncate" style="max-width:200px;">${safeFilename}</div>
                                    ${printerMeta}
                                </div>
                            </div>
                        </td>
                        <td>${statusBadge(e.status)}</td>
                        <td class="small">${started}</td>
                        <td>${formatDuration(e.duration_sec)}</td>
                        <td class="text-end">${actionCell}</td>
                    </tr>`;
                    tbody.append(row);
                });
                const countSuffix = historyAllPrinters ? " across all printers" : "";
                $("#history-count").text(`${Math.min(historyOffset + data.entries.length, data.total)} / ${data.total} entries${countSuffix}`);
                if (historyOffset + data.entries.length < data.total) {
                    $("#history-load-more").show();
                } else {
                    $("#history-load-more").hide();
                }
                updateHistorySelectionUi();
            })
            .catch(err => console.error("History load failed:", err));
    }

    // Load on tab switch — use native addEventListener because Cash.js splits
    // "shown.bs.tab" at the dot and registers on event type "shown" instead of
    // the full Bootstrap event type "shown.bs.tab".
    const historyTabBtn = document.querySelector('button[data-bs-target="#history"]');
    if (historyTabBtn) {
        historyTabBtn.addEventListener("shown.bs.tab", function () {
            historyOffset = 0;
            loadHistory(false);
        });
    }

    $("#history-load-more").on("click", function () {
        historyOffset += HISTORY_LIMIT;
        loadHistory(true);
    });

    $("#history-all-printers").on("change", function () {
        historyAllPrinters = !!$(this).prop("checked");
        // Update Clear button label to reflect the current scope
        const clearBtn = $("#history-clear");
        if (historyAllPrinters) {
            clearBtn.html('<i class="bi bi-trash"></i> Clear All Printers').attr("title", "Clear history across all printers");
        } else {
            clearBtn.html('<i class="bi bi-trash"></i> Clear').removeAttr("title");
        }
        historyOffset = 0;
        selectedHistoryIds.clear();
        $("#history-tbody").empty();
        loadHistory(false);
    });

    $(document).on("change", ".history-select-checkbox", function () {
        const id = parseInt($(this).attr("data-history-id"), 10);
        if (!Number.isFinite(id)) {
            return;
        }
        if ($(this).prop("checked")) {
            selectedHistoryIds.add(id);
        } else {
            selectedHistoryIds.delete(id);
        }
        updateHistorySelectionUi();
    });

    $("#history-select-all").on("change", function () {
        const checked = $(this).prop("checked");
        $(".history-select-checkbox").each(function () {
            const checkbox = $(this);
            const id = parseInt(checkbox.attr("data-history-id"), 10);
            if (!Number.isFinite(id)) {
                return;
            }
            checkbox.prop("checked", checked);
            if (checked) {
                selectedHistoryIds.add(id);
            } else {
                selectedHistoryIds.delete(id);
            }
        });
        updateHistorySelectionUi();
    });

    $("#history-delete-selected").on("click", async function () {
        const ids = Array.from(selectedHistoryIds);
        if (!ids.length) {
            flash_message("Select one or more history entries first.", "warning");
            return;
        }
        if (!confirm(`Delete ${ids.length} selected history entr${ids.length === 1 ? "y" : "ies"}?`)) {
            return;
        }
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            const resp = await fetch(withActivePrinterQuery("/api/history/delete"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ ids }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            selectedHistoryIds.clear();
            historyOffset = 0;
            loadHistory(false);
            flash_message(`Deleted ${data.deleted || 0} history entr${(data.deleted || 0) === 1 ? "y" : "ies"}.`, "success");
        } catch (err) {
            flash_message(`Delete failed: ${err.message || err}`, "danger");
        } finally {
            btn.prop("disabled", false);
            updateHistorySelectionUi();
        }
    });

    $("#history-clear").on("click", function () {
        const confirmMsg = historyAllPrinters
            ? "Clear history across all printers?"
            : "Clear all print history?";
        if (!confirm(confirmMsg)) return;
        fetch(withHistoryScopeQuery("/api/history"), { method: "DELETE" })
            .then(() => {
                selectedHistoryIds.clear();
                historyOffset = 0;
                loadHistory(false);
                updateHistorySelectionUi();
            });
    });

    $(document).on("click", ".history-reprint-btn", async function () {
        const btn = $(this);
        const entryId = btn.attr("data-history-id");
        const entryName = btn.attr("data-history-name") || "selected file";
        const entryPrinterIndexAttr = btn.attr("data-history-printer-index");
        if (!entryId) {
            return;
        }
        if (!confirm(`Reprint ${entryName}?`)) {
            return;
        }
        btn.prop("disabled", true);
        try {
            // When viewing all-printers history, route the reprint to the printer that
            // originally ran the job (from data-history-printer-index), not the active one.
            let reprintUrl;
            if (historyAllPrinters && entryPrinterIndexAttr !== undefined && entryPrinterIndexAttr !== "") {
                const pi = parseInt(entryPrinterIndexAttr, 10);
                const base = `/api/history/${encodeURIComponent(entryId)}/reprint`;
                reprintUrl = Number.isFinite(pi)
                    ? `${base}?printer_index=${encodeURIComponent(pi)}`
                    : withActivePrinterQuery(base);
            } else {
                reprintUrl = withActivePrinterQuery(`/api/history/${encodeURIComponent(entryId)}/reprint`);
            }
            const resp = await fetch(reprintUrl, { method: "POST" });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            flash_message(`Reprint upload started for ${entryName}`, "success");
        } catch (err) {
            flash_message(`Reprint failed: ${err.message || err}`, "danger");
        } finally {
            btn.prop("disabled", false);
        }
    });

    /**
     * Timelapse — list + player layout
     */
    function formatSize(bytes) {
        if (!bytes) return "-";
        const mb = bytes / (1024 * 1024);
        return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
    }

    let _timelapseSnapshotCollections = [];
    let _timelapseSelectedCollectionId = null;
    let _timelapseSelectedFrameName = null;
    const selectedTimelapseFiles = new Set();

    function getTimelapseSnapshotCollection(id) {
        return _timelapseSnapshotCollections.find(collection => collection.id === id) || null;
    }

    function configureTimelapseSnapshotDeleteButton(mode, options = {}) {
        const deleteBtn = document.getElementById("timelapse-snapshot-delete");
        if (!deleteBtn) return;

        const enabled = options.enabled === true;
        deleteBtn.disabled = !enabled;
        deleteBtn.dataset.mode = mode || "frame";
        deleteBtn.innerHTML = mode === "collection"
            ? '<i class="bi bi-trash"></i> Discard Capture'
            : '<i class="bi bi-trash"></i> Delete';

        if (options.collectionId) {
            deleteBtn.dataset.collection = options.collectionId;
        } else {
            deleteBtn.removeAttribute("data-collection");
        }

        if (options.filename) {
            deleteBtn.dataset.file = options.filename;
        } else {
            deleteBtn.removeAttribute("data-file");
        }
    }

    function clearTimelapseSnapshotPreview(message) {
        const imageEl = document.getElementById("timelapse-snapshot-image");
        const placeholder = document.getElementById("timelapse-snapshot-placeholder");
        const placeholderText = document.getElementById("timelapse-snapshot-placeholder-text");
        const titleEl = document.getElementById("timelapse-snapshot-title");
        const subtitleEl = document.getElementById("timelapse-snapshot-subtitle");
        const metaEl = document.getElementById("timelapse-snapshot-meta");
        const downloadEl = document.getElementById("timelapse-snapshot-download");
        const deleteBtn = document.getElementById("timelapse-snapshot-delete");

        if (imageEl) {
            imageEl.onload = null;
            imageEl.onerror = null;
            imageEl.removeAttribute("src");
            imageEl.style.display = "none";
        }
        if (placeholderText && message) {
            placeholderText.textContent = message;
        }
        if (placeholder) {
            placeholder.style.display = "";
        }
        if (titleEl) titleEl.textContent = "Snapshot Viewer";
        if (subtitleEl) {
            subtitleEl.textContent = "Select a saved snapshot to preview.";
            subtitleEl.style.display = "";
        }
        if (metaEl) {
            metaEl.textContent = "";
            metaEl.style.display = "none";
        }
        if (downloadEl) {
            downloadEl.classList.add("disabled");
            downloadEl.setAttribute("href", "#");
            downloadEl.removeAttribute("download");
        }
        configureTimelapseSnapshotDeleteButton("frame", { enabled: false });
    }

    function getSnapshotCollectionStateSuffix(collection) {
        if (!collection) return "";
        if (collection.state === "capturing") return " (active)";
        if (collection.state === "resume_pending") return " (paused)";
        if (collection.state === "manual") return " (manual)";
        return "";
    }

    function getSnapshotCollectionSubtitle(collection) {
        if (!collection) return "";
        if (collection.state === "manual") {
            return collection.source_label
                ? `Manual snapshot from ${collection.source_label}.`
                : "Manual snapshot.";
        }
        if (collection.state === "capturing") {
            return "Timelapse capture in progress.";
        }
        if (collection.state === "resume_pending") {
            return "Timelapse capture is paused and still resumable.";
        }
        return collection.video_filename
            ? `Saved from timelapse ${collection.video_filename}.`
            : "Saved timelapse snapshot.";
    }

    function timelapseSelectSnapshot(collectionId, frameName) {
        const collection = getTimelapseSnapshotCollection(collectionId);
        const frame = collection ? (collection.frames || []).find(item => item.filename === frameName) : null;
        const collectionSelect = document.getElementById("timelapse-snapshot-collection");
        const frameSelect = document.getElementById("timelapse-snapshot-select");
        const imageEl = document.getElementById("timelapse-snapshot-image");
        const placeholder = document.getElementById("timelapse-snapshot-placeholder");
        const titleEl = document.getElementById("timelapse-snapshot-title");
        const subtitleEl = document.getElementById("timelapse-snapshot-subtitle");
        const metaEl = document.getElementById("timelapse-snapshot-meta");
        const statusEl = document.getElementById("timelapse-snapshot-status");
        const downloadEl = document.getElementById("timelapse-snapshot-download");
        const deleteBtn = document.getElementById("timelapse-snapshot-delete");

        if (!collection || !frame) {
            _timelapseSelectedCollectionId = null;
            _timelapseSelectedFrameName = null;
            clearTimelapseSnapshotPreview("Select a saved snapshot to preview");
            if (statusEl && !_timelapseSnapshotCollections.length) {
                statusEl.textContent = "Saved timelapse and manual snapshots will appear here.";
            }
            return;
        }

        _timelapseSelectedCollectionId = collection.id;
        _timelapseSelectedFrameName = frame.filename;

        if (collectionSelect) collectionSelect.value = collection.id;
        if (frameSelect) frameSelect.value = frame.filename;

        if (titleEl) titleEl.textContent = collection.label || collection.id;
        if (subtitleEl) {
            const subtitle = getSnapshotCollectionSubtitle(collection);
            subtitleEl.textContent = subtitle;
            subtitleEl.style.display = subtitle ? "" : "none";
        }
        if (metaEl) {
            const created = frame.created_at ? new Date(frame.created_at).toLocaleString() : "-";
            metaEl.textContent = `${frame.filename} · ${created} · ${formatSize(frame.size_bytes)}`;
            metaEl.style.display = "";
        }
        if (statusEl) {
            if (collection.state === "manual") {
                statusEl.textContent = collection.source_label
                    ? `Manual snapshot saved from ${collection.source_label}.`
                    : "Manual snapshot saved.";
            } else if (collection.allow_delete) {
                statusEl.textContent = `${collection.frame_count} frame(s) available in this capture.`;
            } else if (collection.state === "capturing") {
                statusEl.textContent = "This capture is still running. Frames are view-only until the timelapse finishes.";
            } else if (collection.state === "resume_pending") {
                statusEl.textContent = "This paused capture is still resumable. Use Discard Capture if you want to remove it.";
            } else {
                statusEl.textContent = "This capture is still resumable. Frames are view-only until the timelapse is finalized.";
            }
        }
        if (imageEl) {
            imageEl.onload = function () {
                imageEl.style.display = "";
                if (placeholder) {
                    placeholder.style.display = "none";
                }
            };
            imageEl.onerror = function () {
                clearTimelapseSnapshotPreview("Unable to load snapshot preview");
            };
            imageEl.src = withActivePrinterQuery(
                `/api/timelapse-snapshot/${encodeURIComponent(collection.id)}/${encodeURIComponent(frame.filename)}`
            );
        }
        if (downloadEl) {
            downloadEl.classList.remove("disabled");
            downloadEl.href = withActivePrinterQuery(
                `/api/timelapse-snapshot/${encodeURIComponent(collection.id)}/${encodeURIComponent(frame.filename)}?download=1`
            );
            downloadEl.setAttribute("download", frame.filename);
        }
        if (collection.state === "resume_pending") {
            configureTimelapseSnapshotDeleteButton("collection", {
                enabled: true,
                collectionId: collection.id,
            });
        } else {
            configureTimelapseSnapshotDeleteButton("frame", {
                enabled: !!collection.allow_delete,
                collectionId: collection.id,
                filename: frame.filename,
            });
        }
    }

    function renderTimelapseSnapshots() {
        const collectionSelect = document.getElementById("timelapse-snapshot-collection");
        const frameSelect = document.getElementById("timelapse-snapshot-select");
        const statusEl = document.getElementById("timelapse-snapshot-status");

        if (!collectionSelect || !frameSelect) return;

        collectionSelect.innerHTML = "";
        frameSelect.innerHTML = "";

        if (!_timelapseSnapshotCollections.length) {
            collectionSelect.innerHTML = '<option value="" selected>No snapshots available</option>';
            collectionSelect.disabled = true;
            frameSelect.innerHTML = '<option value="" selected>No snapshots available</option>';
            frameSelect.disabled = true;
            if (statusEl) {
                statusEl.textContent = "Saved timelapse and manual snapshots will appear here.";
            }
            clearTimelapseSnapshotPreview("Select a saved snapshot to preview");
            return;
        }

        collectionSelect.disabled = false;
        _timelapseSnapshotCollections.forEach(collection => {
            const option = document.createElement("option");
            option.value = collection.id;
            const stateSuffix = getSnapshotCollectionStateSuffix(collection);
            option.textContent = `${collection.label || collection.id} · ${collection.frame_count} frame(s)${stateSuffix}`;
            collectionSelect.appendChild(option);
        });

        let selectedCollection = getTimelapseSnapshotCollection(_timelapseSelectedCollectionId);
        if (!selectedCollection) {
            selectedCollection = _timelapseSnapshotCollections[0];
            _timelapseSelectedCollectionId = selectedCollection.id;
        }
        collectionSelect.value = selectedCollection.id;

        const frames = Array.isArray(selectedCollection.frames) ? selectedCollection.frames : [];
        if (!frames.length) {
            frameSelect.innerHTML = '<option value="" selected>No snapshots available</option>';
            frameSelect.disabled = true;
            clearTimelapseSnapshotPreview("No snapshots available in this collection");
            return;
        }

        frameSelect.disabled = false;
        frames.forEach(frame => {
            const option = document.createElement("option");
            option.value = frame.filename;
            const created = frame.created_at ? new Date(frame.created_at).toLocaleTimeString() : "-";
            option.textContent = `${frame.filename} · ${created}`;
            frameSelect.appendChild(option);
        });

        const selectedFrame = frames.find(frame => frame.filename === _timelapseSelectedFrameName) || frames[frames.length - 1];
        timelapseSelectSnapshot(selectedCollection.id, selectedFrame.filename);
    }

    function loadTimelapseSnapshots() {
        return fetch(withActivePrinterQuery("/api/timelapse-snapshots"))
            .then(r => r.json())
            .then(data => {
                _timelapseSnapshotCollections = Array.isArray(data.collections) ? data.collections : [];
                renderTimelapseSnapshots();
            })
            .catch(err => console.error("Timelapse snapshot load failed:", err));
    }

    function clearTimelapseVideoPreview(message) {
        const headerEl = document.getElementById("timelapse-player-header");
        const placeholderEl = document.getElementById("timelapse-player-placeholder");
        const placeholderTextEl = document.getElementById("timelapse-player-placeholder-text");
        const videoEl = document.getElementById("timelapse-player");
        const titleEl = document.getElementById("timelapse-player-title");
        const metaEl = document.getElementById("timelapse-player-meta");
        const deleteBtn = document.getElementById("timelapse-player-delete");

        if (videoEl) {
            try {
                videoEl.pause();
            } catch (_err) {}
            videoEl.removeAttribute("src");
            videoEl.removeAttribute("data-file");
            videoEl.style.display = "none";
            videoEl.load();
        }
        if (headerEl) headerEl.style.display = "none";
        if (titleEl) titleEl.textContent = "";
        if (metaEl) {
            metaEl.textContent = "";
            metaEl.style.display = "none";
        }
        if (placeholderTextEl) {
            placeholderTextEl.textContent = message || "Select a video to play";
        }
        if (placeholderEl) placeholderEl.style.display = "";
        if (deleteBtn) deleteBtn.removeAttribute("data-file");

        document.querySelectorAll("#timelapse-list .list-group-item").forEach(el => {
            el.classList.remove("active");
        });
    }

    function timelapseSelectVideo(v) {
        const headerEl = document.getElementById("timelapse-player-header");
        const placeholderEl = document.getElementById("timelapse-player-placeholder");
        const videoEl = document.getElementById("timelapse-player");
        const titleEl = document.getElementById("timelapse-player-title");
        const metaEl = document.getElementById("timelapse-player-meta");
        const deleteBtn = document.getElementById("timelapse-player-delete");
        if (!videoEl) return;

        document.querySelectorAll("#timelapse-list .list-group-item").forEach(el => {
            el.classList.toggle("active", el.dataset.file === v.filename);
        });

        if (titleEl) titleEl.textContent = v.filename;
        if (metaEl) {
            metaEl.textContent = `${v.created_at ? new Date(v.created_at).toLocaleString() : "-"} · ${formatSize(v.size_bytes)}`;
            metaEl.style.display = "";
        }

        if (deleteBtn) deleteBtn.dataset.file = v.filename;
        videoEl.dataset.file = v.filename;
        videoEl.src = withActivePrinterQuery(`/api/timelapse/${encodeURIComponent(v.filename)}`);
        videoEl.style.display = "";
        videoEl.load();

        if (headerEl) headerEl.style.display = "";
        if (placeholderEl) placeholderEl.style.display = "none";
    }

    function updateTimelapseSelectionUi() {
        const count = selectedTimelapseFiles.size;
        const deleteButton = $("#timelapse-delete-selected");
        deleteButton
            .prop("disabled", count === 0)
            .html(`<i class="bi bi-trash3"></i> Delete Selected${count > 0 ? ` (${count})` : ""}`);

        const checkboxes = $(".timelapse-select-checkbox");
        const checked = checkboxes.filter(function () {
            return $(this).prop("checked");
        });
        const selectAll = $("#timelapse-select-all");
        if (selectAll.length) {
            const selectAllEl = selectAll.get(0);
            selectAll.prop("checked", checkboxes.length > 0 && checked.length === checkboxes.length);
            selectAll.prop("disabled", checkboxes.length === 0);
            if (selectAllEl) {
                selectAllEl.indeterminate = checked.length > 0 && checked.length < checkboxes.length;
            }
        }
    }

    async function deleteTimelapseFile(file) {
        const resp = await fetch(withActivePrinterQuery(`/api/timelapse/${encodeURIComponent(file)}`), { method: "DELETE" });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `HTTP ${resp.status}`);
        }
        selectedTimelapseFiles.delete(file);
        const videoEl = document.getElementById("timelapse-player");
        if (videoEl && (videoEl.dataset.file || "") === file) {
            clearTimelapseVideoPreview("Select a video to play");
        }
        return data;
    }

    function loadTimelapses() {
        fetch(withActivePrinterQuery("/api/timelapses"))
            .then(r => r.json())
            .then(data => {
                const banner = document.getElementById("timelapse-disabled-banner");
                const list = document.getElementById("timelapse-list");
                const videoEl = document.getElementById("timelapse-player");
                const currentFile = videoEl ? (videoEl.dataset.file || "") : "";

                if (banner) banner.style.display = data.enabled ? "none" : "";
                if (!list) return;

                list.innerHTML = "";

                if (!Array.isArray(data.videos) || data.videos.length === 0) {
                    selectedTimelapseFiles.clear();
                    list.innerHTML = '<div class="text-center text-muted py-4">No timelapse videos yet</div>';
                    clearTimelapseVideoPreview("Select a video to play");
                    updateTimelapseSelectionUi();
                    return;
                }

                let currentFileStillExists = false;
                const visibleFiles = new Set(data.videos.map(v => v.filename));
                selectedTimelapseFiles.forEach(file => {
                    if (!visibleFiles.has(file)) {
                        selectedTimelapseFiles.delete(file);
                    }
                });

                data.videos.forEach(v => {
                    const created = v.created_at ? new Date(v.created_at).toLocaleString() : "-";
                    const safeFilename = escapeHtml(v.filename);
                    const isChecked = selectedTimelapseFiles.has(v.filename);
                    const item = document.createElement("div");
                    item.className = "list-group-item list-group-item-action d-flex align-items-center py-2 px-3";
                    item.dataset.file = v.filename;
                    item.innerHTML = `
                        <div class="form-check me-2 flex-shrink-0">
                            <input type="checkbox" class="form-check-input timelapse-select-checkbox">
                        </div>
                        <div class="overflow-hidden me-2" style="cursor:pointer; flex:1; min-width:0;">
                            <div class="text-truncate fw-semibold small">${safeFilename}</div>
                            <div class="text-muted" style="font-size:0.75em;">${created} · ${formatSize(v.size_bytes)}</div>
                        </div>
                        <div class="d-flex gap-1 flex-shrink-0">
                            <a href="${withActivePrinterQuery(`/api/timelapse/${encodeURIComponent(v.filename)}`)}" class="btn btn-sm btn-outline-secondary" download title="Download">
                                <i class="bi bi-download"></i>
                            </a>
                            <button type="button" class="btn btn-sm btn-outline-danger timelapse-delete" title="Delete">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>`;

                    item.querySelector(".timelapse-delete").dataset.file = v.filename;
                    const checkbox = item.querySelector(".timelapse-select-checkbox");
                    if (checkbox) {
                        checkbox.dataset.file = v.filename;
                        checkbox.checked = isChecked;
                        checkbox.setAttribute("aria-label", `Select ${v.filename}`);
                        checkbox.addEventListener("click", event => event.stopPropagation());
                    }

                    if (currentFile && currentFile === v.filename) {
                        item.classList.add("active");
                        currentFileStillExists = true;
                    }

                    item.querySelector(".overflow-hidden").addEventListener("click", () => timelapseSelectVideo(v));
                    list.appendChild(item);
                });

                if (currentFile && !currentFileStillExists) {
                    clearTimelapseVideoPreview("Select a video to play");
                }
                updateTimelapseSelectionUi();
            })
            .catch(err => console.error("Timelapse load failed:", err));
    }

    // Load on tab show; auto-refresh every 15 s while active.
    const timelapseTabBtn = document.querySelector('button[data-bs-target="#timelapse"]');
    let _timelapseVideoInterval = null;
    if (timelapseTabBtn) {
        timelapseTabBtn.addEventListener("shown.bs.tab", function () {
            loadPrinterRuntimeState().catch(function (err) {
                console.warn("Failed to refresh timelapse runtime state", err);
            });
            loadTimelapses();
            if (!_timelapseVideoInterval) {
                _timelapseVideoInterval = setInterval(function () {
                    loadTimelapses();
                }, 15000);
            }
        });
        timelapseTabBtn.addEventListener("hidden.bs.tab", function () {
            if (_timelapseVideoInterval) {
                clearInterval(_timelapseVideoInterval);
                _timelapseVideoInterval = null;
            }
        });
    }

    const snapshotsTabBtn = document.querySelector('button[data-bs-target="#snapshots"]');
    let _timelapseSnapshotInterval = null;
    if (snapshotsTabBtn) {
        snapshotsTabBtn.addEventListener("shown.bs.tab", function () {
            loadPrinterRuntimeState().catch(function (err) {
                console.warn("Failed to refresh snapshot runtime state", err);
            });
            loadTimelapseSnapshots();
            if (!_timelapseSnapshotInterval) {
                _timelapseSnapshotInterval = setInterval(function () {
                    loadTimelapseSnapshots();
                }, 15000);
            }
        });
        snapshotsTabBtn.addEventListener("hidden.bs.tab", function () {
            if (_timelapseSnapshotInterval) {
                clearInterval(_timelapseSnapshotInterval);
                _timelapseSnapshotInterval = null;
            }
        });
    }

    $("#timelapse-action-start").on("click", async function () {
        const btn = $(this);
        const fileName = String(_timelapseRuntime.promptFilename || "this print").trim() || "this print";
        btn.prop("disabled", true);
        try {
            await sendTimelapseCurrentAction("/api/timelapse/current/start", `Timelapse started for ${fileName}.`);
            loadTimelapseSnapshots();
        } catch (err) {
            flash_message(`Timelapse start failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#timelapse-control-start").on("click", async function () {
        const btn = $(this);
        const fileName = String(_timelapseRuntime.promptFilename || "this print").trim() || "this print";
        btn.prop("disabled", true);
        try {
            await sendTimelapseCurrentAction("/api/timelapse/current/start", `Timelapse started for ${fileName}.`);
            loadTimelapseSnapshots();
        } catch (err) {
            flash_message(`Timelapse start failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#timelapse-control-pause").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await sendTimelapseCurrentAction("/api/timelapse/current/pause", "Timelapse paused.");
        } catch (err) {
            flash_message(`Timelapse pause failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#timelapse-control-resume").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await sendTimelapseCurrentAction("/api/timelapse/current/resume", "Timelapse resumed.");
        } catch (err) {
            flash_message(`Timelapse resume failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#timelapse-control-stop").on("click", async function () {
        const btn = $(this);
        if (!confirm("Stop timelapse capture for the current print?")) return;
        btn.prop("disabled", true);
        try {
            await sendTimelapseCurrentAction("/api/timelapse/current/stop", "Timelapse stopped.");
            loadTimelapseSnapshots();
            loadTimelapses();
        } catch (err) {
            flash_message(`Timelapse stop failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#timelapse-action-dismiss").on("click", async function () {
        const btn = $(this);
        btn.prop("disabled", true);
        try {
            await sendTimelapseCurrentAction("/api/timelapse/current/dismiss", "Pending timelapse capture dismissed.");
            loadTimelapseSnapshots();
        } catch (err) {
            flash_message(`Dismiss failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $("#timelapse-snapshot-collection").on("change", function () {
        _timelapseSelectedCollectionId = this.value || null;
        _timelapseSelectedFrameName = null;
        renderTimelapseSnapshots();
    });

    $("#timelapse-snapshot-select").on("change", function () {
        if (!_timelapseSelectedCollectionId) return;
        timelapseSelectSnapshot(_timelapseSelectedCollectionId, this.value || "");
    });

    $("#timelapse-snapshot-delete").on("click", async function () {
        const btn = $(this);
        const mode = String(btn.data("mode") || "frame");
        const collectionId = btn.data("collection");
        const filename = btn.data("file");
        const collection = getTimelapseSnapshotCollection(collectionId);
        if (!collectionId || !collection) return;

        let requestUrl = null;
        let successMessage = null;
        let confirmMessage = null;

        if (mode === "collection") {
            confirmMessage = `Discard paused capture ${collection.label || collection.id}?`;
            requestUrl = withActivePrinterQuery(
                `/api/timelapse-snapshot/${encodeURIComponent(collectionId)}`
            );
            successMessage = `Discarded paused capture ${collection.label || collection.id}.`;
        } else {
            if (!filename || !collection.allow_delete) {
                return;
            }
            confirmMessage = `Delete snapshot ${filename}?`;
            requestUrl = withActivePrinterQuery(
                `/api/timelapse-snapshot/${encodeURIComponent(collectionId)}/${encodeURIComponent(filename)}`
            );
            successMessage = `Deleted snapshot ${filename}.`;
        }

        if (!confirm(confirmMessage)) return;
        btn.prop("disabled", true);
        try {
            const resp = await fetch(requestUrl, { method: "DELETE" });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }
            flash_message(successMessage, "success", 4000);
            await loadTimelapseSnapshots();
        } catch (err) {
            flash_message(`Snapshot delete failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
        }
    });

    $(document).on("change", ".timelapse-select-checkbox", function () {
        const file = $(this).attr("data-file") || $(this).data("file");
        if (!file) {
            return;
        }
        if ($(this).prop("checked")) {
            selectedTimelapseFiles.add(file);
        } else {
            selectedTimelapseFiles.delete(file);
        }
        updateTimelapseSelectionUi();
    });

    $("#timelapse-select-all").on("change", function () {
        const checked = $(this).prop("checked");
        $(".timelapse-select-checkbox").each(function () {
            const checkbox = $(this);
            const file = checkbox.attr("data-file") || checkbox.data("file");
            if (!file) {
                return;
            }
            checkbox.prop("checked", checked);
            if (checked) {
                selectedTimelapseFiles.add(file);
            } else {
                selectedTimelapseFiles.delete(file);
            }
        });
        updateTimelapseSelectionUi();
    });

    $("#timelapse-delete-selected").on("click", async function () {
        const files = Array.from(selectedTimelapseFiles);
        if (!files.length) {
            flash_message("Select one or more timelapse videos first.", "warning");
            return;
        }
        if (!confirm(`Delete ${files.length} selected timelapse video${files.length === 1 ? "" : "s"}?`)) {
            return;
        }
        const btn = $(this);
        btn.prop("disabled", true);
        const failures = [];
        let deleted = 0;
        try {
            for (const file of files) {
                try {
                    await deleteTimelapseFile(file);
                    deleted += 1;
                } catch (err) {
                    failures.push(`${file}: ${err.message || err}`);
                }
            }
            await loadTimelapses();
            await loadTimelapseSnapshots();
            if (deleted > 0) {
                flash_message(`Deleted ${deleted} timelapse video${deleted === 1 ? "" : "s"}.`, "success", 4000);
            }
            if (failures.length) {
                flash_message(`Some timelapse videos could not be deleted: ${failures.join("; ")}`, "danger", 8000);
            }
        } finally {
            btn.prop("disabled", false);
            updateTimelapseSelectionUi();
        }
    });

    // Delete timelapse (list button or player delete button)
    $(document).on("click", ".timelapse-delete", async function () {
        const btn = $(this);
        const file = btn.attr("data-file") || btn.data("file");
        if (!file || !confirm(`Delete timelapse ${file}?`)) return;
        btn.prop("disabled", true);
        try {
            await deleteTimelapseFile(file);
            flash_message(`Deleted timelapse ${file}.`, "success", 4000);
            await loadTimelapses();
            await loadTimelapseSnapshots();
        } catch (err) {
            flash_message(`Timelapse delete failed: ${err.message || err}`, "danger", 6000);
        } finally {
            btn.prop("disabled", false);
            updateTimelapseSelectionUi();
        }
    });

    /**
     * Timelapse Settings
     */
    const timelapseForm = $("#timelapse-form");
    if (timelapseForm.length) {
        const tlFields = {
            enabled: $("#timelapse-enabled"),
            interval: $("#timelapse-interval"),
            maxVideos: $("#timelapse-max-videos"),
            persistent: $("#timelapse-persistent"),
            cameraSource: $("#timelapse-camera-source"),
            light: $("#timelapse-light"),
        };
        const tlSaveBtn = $("#timelapse-save");

        const loadTimelapseSettings = async () => {
            try {
                const resp = await fetch(withActivePrinterQuery("/api/settings/timelapse"));
                if (resp.ok) {
                    const data = await resp.json();
                    const cfg = data.timelapse || {};
                    tlFields.enabled.prop("checked", Boolean(cfg.enabled));
                    tlFields.interval.val(cfg.interval || 30);
                    tlFields.maxVideos.val(cfg.max_videos || 10);
                    tlFields.persistent.prop("checked", cfg.save_persistent !== false);
                    tlFields.cameraSource.val(cfg.camera_source || "follow");
                    tlFields.light.val(cfg.light || "");
                }
            } catch (err) {
                console.error("Failed to load timelapse settings:", err);
            }
        };

        tlSaveBtn.on("click", async function () {
            const btn = $(this);
            btn.prop("disabled", true);
            const payload = {
                timelapse: {
                    enabled: tlFields.enabled.is(":checked"),
                    interval: parseInt(tlFields.interval.val(), 10) || 30,
                    max_videos: parseInt(tlFields.maxVideos.val(), 10) || 10,
                    save_persistent: tlFields.persistent.is(":checked"),
                    camera_source: tlFields.cameraSource.val() || "follow",
                    light: tlFields.light.val() || null
                }
            };
            try {
                const resp = await fetch(withActivePrinterQuery("/api/settings/timelapse"), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                if (resp.ok) {
                    flash_message("Timelapse settings saved", "success");
                    loadTimelapseSettings(); // Reload to confirm
                } else {
                    const data = await resp.json().catch(() => ({}));
                    flash_message(`Failed to save: ${data.error || resp.statusText}`, "danger");
                }
            } catch (err) {
                flash_message(`Error: ${err.message}`, "danger");
            } finally {
                btn.prop("disabled", false);
            }
        });

        // Load on tab show or init
        loadTimelapseSettings();
    }

    /**
     * MQTT Settings
     */
    const mqttForm = $("#mqtt-form");
    if (mqttForm.length) {
        const mqttFields = {
            enabled: $("#mqtt-enabled"),
            host: $("#mqtt-host"),
            port: $("#mqtt-port"),
            user: $("#mqtt-user"),
            password: $("#mqtt-password"),
            prefix: $("#mqtt-prefix"),
        };
        const mqttSaveBtn = $("#mqtt-save");

        const loadMqttSettings = async () => {
            try {
                const resp = await fetch("/api/settings/mqtt");
                if (resp.ok) {
                    const data = await resp.json();
                    const cfg = data.home_assistant || {};
                    mqttFields.enabled.prop("checked", Boolean(cfg.enabled));
                    mqttFields.host.val(cfg.mqtt_host || "");
                    mqttFields.port.val(cfg.mqtt_port || 1883);
                    mqttFields.user.val(cfg.mqtt_username || "");
                    mqttFields.password.val(cfg.mqtt_password || "");
                    mqttFields.prefix.val(cfg.discovery_prefix || "homeassistant");
                }
            } catch (err) {
                console.error("Failed to load MQTT settings:", err);
            }
        };

        mqttSaveBtn.on("click", async function () {
            const btn = $(this);
            btn.prop("disabled", true);
            const payload = {
                home_assistant: {
                    enabled: mqttFields.enabled.is(":checked"),
                    mqtt_host: mqttFields.host.val().trim(),
                    mqtt_port: parseInt(mqttFields.port.val(), 10) || 1883,
                    mqtt_username: mqttFields.user.val().trim(),
                    mqtt_password: mqttFields.password.val().trim(),
                    discovery_prefix: mqttFields.prefix.val().trim(),
                }
            };
            try {
                const resp = await fetch("/api/settings/mqtt", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                if (resp.ok) {
                    flash_message("MQTT settings saved. Service restarting...", "success");
                    setTimeout(loadMqttSettings, 1000);
                } else {
                    const data = await resp.json().catch(() => ({}));
                    flash_message(`Failed to save: ${data.error || resp.statusText}`, "danger");
                }
            } catch (err) {
                flash_message(`Error: ${err.message}`, "danger");
            } finally {
                btn.prop("disabled", false);
            }
        });

        loadMqttSettings();
    }

    /**
     * Debug Tab Logic
     * Only initialised when the debug tab element is present (ANKERCTL_DEV_MODE=true).
     */
    if ($("#debug").length) {

        // ------------------------------------------------------------------
        // Helpers
        // ------------------------------------------------------------------

        /**
         * Build a Bootstrap table.table-sm.table-dark with key-value rows.
         * Values are colour-coded: true=success, false=danger, null=muted.
         * @param {string} title
         * @param {Object} obj
         * @returns {HTMLElement} card element
         */
        function renderSection(title, obj) {
            const card = document.createElement("div");
            card.className = "card border-secondary mb-3";

            const header = document.createElement("div");
            header.className = "card-header small fw-semibold";
            header.textContent = title;
            card.appendChild(header);

            const table = document.createElement("table");
            table.className = "table table-sm table-dark mb-0";

            const tbody = document.createElement("tbody");
            Object.entries(obj).forEach(([key, value]) => {
                const tr = document.createElement("tr");

                const tdKey = document.createElement("td");
                tdKey.className = "text-muted small w-50";
                tdKey.textContent = key;

                const tdVal = document.createElement("td");
                tdVal.className = "small font-monospace";

                if (value === true) {
                    tdVal.innerHTML = '<span class="text-success">true</span>';
                } else if (value === false) {
                    tdVal.innerHTML = '<span class="text-danger">false</span>';
                } else if (value === null || value === undefined) {
                    tdVal.innerHTML = '<span class="text-muted">null</span>';
                } else {
                    tdVal.textContent = String(value);
                }

                tr.appendChild(tdKey);
                tr.appendChild(tdVal);
                tbody.appendChild(tr);
            });

            table.appendChild(tbody);
            card.appendChild(table);
            return card;
        }

        // ------------------------------------------------------------------
        // State Inspector
        // ------------------------------------------------------------------

        async function dbgRefreshState() {
            try {
                const resp = await fetch("/api/debug/state");
                if (!resp.ok) {
                    document.getElementById("dbg-state-tables").textContent = `Error: HTTP ${resp.status}`;
                    return;
                }
                const data = await resp.json();

                const container = document.getElementById("dbg-state-tables");
                container.innerHTML = "";

                // Top-level scalar values (e.g. debug_logging)
                const scalars = {};
                Object.entries(data).forEach(([key, val]) => {
                    if (typeof val !== "object" || val === null) {
                        scalars[key] = val;
                    }
                });
                if (Object.keys(scalars).length > 0) {
                    container.appendChild(renderSection("General", scalars));
                }

                // Nested objects rendered as separate tables
                Object.entries(data).forEach(([key, val]) => {
                    if (typeof val === "object" && val !== null) {
                        container.appendChild(renderSection(key.charAt(0).toUpperCase() + key.slice(1), val));
                    }
                });

                // Sync controls checkbox
                if (data.debug_logging !== undefined) {
                    $("#dbg-log-mqtt").prop("checked", data.debug_logging);
                }
            } catch (err) {
                document.getElementById("dbg-state-tables").textContent = "Error fetching state: " + err;
            }
        }

        document.getElementById("dbg-refresh-state").addEventListener("click", dbgRefreshState);

        // ------------------------------------------------------------------
        // Printer Reports
        // ------------------------------------------------------------------

        async function dbgLoadPrinterReport(name, buttonEl = null) {
            const metaEl = document.getElementById("dbg-printer-report-meta");
            const contentEl = document.getElementById("dbg-printer-report-content");
            if (!metaEl || !contentEl) {
                return;
            }

            $(".dbg-printer-report-btn").removeClass("active");
            if (buttonEl) {
                $(buttonEl).addClass("active");
            }

            metaEl.textContent = `Loading ${name}...`;
            contentEl.textContent = "Reading printer report...";

            const resp = await fetch(`/api/debug/printer-report/${encodeURIComponent(name)}`);
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(data.error || `HTTP ${resp.status}`);
            }

            const reportText = data.cleaned_output || data.raw_output || "(empty report)";
            contentEl.textContent = reportText;
            metaEl.textContent = `${data.label || name} · ${data.gcode || ""} · ${data.chunk_count || 0} chunk(s)`;
        }

        $(".dbg-printer-report-btn").on("click", async function () {
            const btn = this;
            const reportName = btn.getAttribute("data-report");
            try {
                await dbgLoadPrinterReport(reportName, btn);
            } catch (err) {
                const metaEl = document.getElementById("dbg-printer-report-meta");
                const contentEl = document.getElementById("dbg-printer-report-content");
                if (metaEl) {
                    metaEl.textContent = `Error loading ${reportName}`;
                }
                if (contentEl) {
                    contentEl.textContent = String(err.message || err);
                }
            }
        });

        const dbgReportsTab = document.getElementById("dbg-reports-tab");
        if (dbgReportsTab) {
            dbgReportsTab.addEventListener("shown.bs.tab", function () {
                const activeBtn = document.querySelector(".dbg-printer-report-btn.active");
                const firstBtn = activeBtn || document.querySelector(".dbg-printer-report-btn[data-report='settings']");
                if (firstBtn) {
                    firstBtn.click();
                }
            });
        }

        // Auto-refresh state while the inspector sub-tab is active
        const dbgInspectorTab = document.getElementById("dbg-inspector-tab");
        let dbgStateInterval = null;
        if (dbgInspectorTab) {
            dbgInspectorTab.addEventListener("shown.bs.tab", function () {
                dbgRefreshState();
                dbgStateInterval = setInterval(dbgRefreshState, 3000);
            });
            dbgInspectorTab.addEventListener("hidden.bs.tab", function () {
                if (dbgStateInterval) { clearInterval(dbgStateInterval); dbgStateInterval = null; }
            });
        }

        // Also refresh when the top-level Debug tab itself is shown
        const mainDebugTabBtn = document.getElementById("debug-tab");
        if (mainDebugTabBtn) {
            mainDebugTabBtn.addEventListener("shown.bs.tab", function () {
                dbgRefreshState();
            });
        }

        // ------------------------------------------------------------------
        // Controls
        // ------------------------------------------------------------------

        $("#dbg-log-mqtt").on("change", async function () {
            const enabled = $(this).is(":checked");
            await fetch("/api/debug/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ debug_logging: enabled })
            });
            dbgRefreshState();
        });

        // ------------------------------------------------------------------
        // Simulation
        // ------------------------------------------------------------------

        async function dbgSimEvent(type, payload) {
            try {
                await fetch("/api/debug/simulate", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ type: type, payload: payload })
                });
                dbgRefreshState();
            } catch (err) {
                flash_message("Sim failed: " + err, "danger");
            }
        }

        document.getElementById("dbg-sim-start").addEventListener("click", function () {
            dbgSimEvent("start", { filename: "debug_test.gcode" });
        });
        document.getElementById("dbg-sim-finish").addEventListener("click", function () {
            dbgSimEvent("finish", { filename: "debug_test.gcode" });
        });
        document.getElementById("dbg-sim-fail").addEventListener("click", function () {
            dbgSimEvent("fail", { filename: "debug_test.gcode" });
        });

        // Progress slider
        const progressSlider = document.getElementById("dbg-sim-progress-slider");
        const progressValue = document.getElementById("dbg-sim-progress-value");
        if (progressSlider) {
            progressSlider.addEventListener("input", function () {
                progressValue.textContent = this.value + "%";
            });
        }
        document.getElementById("dbg-sim-progress-btn").addEventListener("click", function () {
            const pct = progressSlider ? parseInt(progressSlider.value, 10) : 50;
            dbgSimEvent("progress", {
                progress: pct,
                filename: "debug_test.gcode",
                elapsed: 120,
                remaining: 60,
            });
        });

        // Temperature buttons
        $(".dbg-sim-temp").on("click", function () {
            const btn = $(this);
            dbgSimEvent("temperature", {
                temp_type: btn.data("temp-type"),
                current: parseInt(btn.data("current"), 10),
                target: parseInt(btn.data("target"), 10),
            });
        });

        // Speed button
        document.getElementById("dbg-sim-speed").addEventListener("click", function () {
            dbgSimEvent("speed", { speed: 250 });
        });

        // Layer button
        document.getElementById("dbg-sim-layer").addEventListener("click", function () {
            dbgSimEvent("layer", { current_layer: 42, total_layers: 200 });
        });

        // ------------------------------------------------------------------
        // Services Health Dashboard
        // ------------------------------------------------------------------

        /**
         * Return a Bootstrap badge colour class for a service state name.
         * @param {string} state
         * @returns {string}
         */
        function serviceStateClass(state) {
            switch (state) {
                case "Running": return "bg-success";
                case "Starting":
                case "Stopping": return "bg-warning text-dark";
                default: return "bg-secondary";
            }
        }

        async function dbgRefreshServices() {
            try {
                const resp = await fetch("/api/debug/services");
                if (!resp.ok) {
                    $("#dbg-services-grid").html(`<div class="col-12 text-danger small">Error: HTTP ${resp.status}</div>`);
                    return;
                }
                const data = await resp.json();
                const grid = $("#dbg-services-grid");
                grid.empty();

                // Determine if a print is currently active (for restart warning)
                let isPrinting = false;
                try {
                    const stateResp = await fetch("/api/debug/state");
                    if (stateResp.ok) {
                        const stateData = await stateResp.json();
                        isPrinting = !!(stateData.print && stateData.print.active);
                    }
                } catch (_) { /* ignore */ }

                Object.entries(data.services).forEach(([name, svc]) => {
                    const badgeClass = serviceStateClass(svc.state);
                    let savedTestHtml = "";
                    if (name === "pppp") {
                        const saved = JSON.parse(localStorage.getItem("pppp_test_result") || "null");
                        if (saved) {
                            const ok = saved.result === "ok";
                            const secs = Math.round((Date.now() - saved.ts) / 1000);
                            const agoStr = secs < 60 ? `${secs}s` : secs < 3600 ? `${Math.round(secs / 60)}m` : `${Math.round(secs / 3600)}h`;
                            savedTestHtml = `<span class="${ok ? "text-success" : "text-danger"}">
                                <i class="bi-${ok ? "check-circle" : "x-circle"}"></i>
                                Last result: ${ok ? "ok" : "fail"} <span class="text-muted">(${agoStr} ago)</span>
                            </span>`;
                        }
                    }
                    const card = $(`<div class="col-md-6 col-lg-4">
                        <div class="card border-secondary h-100">
                            <div class="card-header d-flex justify-content-between align-items-center small">
                                <strong>${escapeHtml(name)}</strong>
                                <span class="badge ${badgeClass}">${escapeHtml(svc.state)}</span>
                            </div>
                            <div class="card-body p-2">
                                <div class="small text-muted mb-1">
                                    <span class="me-2">Type: <code>${escapeHtml(svc.type)}</code></span>
                                </div>
                                <div class="small text-muted mb-2">
                                    <span class="me-2">Refs: ${svc.refs}</span>
                                    <span>Wanted: <span class="${svc.wanted ? 'text-success' : 'text-danger'}">${svc.wanted}</span></span>
                                </div>
                                <div class="d-grid gap-1">
                                    <button class="btn btn-sm btn-outline-warning w-100 dbg-restart-svc"
                                        data-svc-name="${escapeHtml(name)}"
                                        data-is-printing="${isPrinting}">
                                        <i class="bi-arrow-clockwise"></i> Restart
                                    </button>
                                    ${name === "pppp" ? `<button class="btn btn-sm btn-outline-info w-100 dbg-test-svc"
                                        data-svc-name="${escapeHtml(name)}">
                                        <i class="bi-wifi"></i> Test
                                    </button>
                                    <div class="dbg-test-result small text-center" data-svc-name="${escapeHtml(name)}">${savedTestHtml}</div>` : ""}
                                </div>
                            </div>
                        </div>
                    </div>`);
                    grid.append(card);
                });

                const ts = new Date().toLocaleTimeString();
                $("#dbg-services-refresh-indicator").text(`Last updated: ${ts}`);
            } catch (err) {
                $("#dbg-services-grid").html(`<div class="col-12 text-danger small">Error: ${escapeHtml(String(err))}</div>`);
            }
        }

        // Restart button handler (delegated)
        $(document).on("click", ".dbg-restart-svc", async function () {
            const name = $(this).data("svc-name");
            const isPrinting = $(this).data("is-printing");
            const printWarning = isPrinting
                ? "\n\nWarning: A print is currently active. Restarting may interrupt it."
                : "";
            if (!confirm(`Restart service "${name}"?${printWarning}`)) return;

            try {
                const resp = await fetch(`/api/debug/services/${encodeURIComponent(name)}/restart`, {
                    method: "POST",
                });
                if (resp.ok) {
                    flash_message(`Service "${name}" restarting...`, "info");
                    setTimeout(dbgRefreshServices, 1500);
                    setTimeout(dbgRefreshServices, 3500);
                } else {
                    const data = await resp.json().catch(() => ({}));
                    flash_message(`Restart failed: ${data.error || resp.statusText}`, "danger");
                }
            } catch (err) {
                flash_message(`Restart failed: ${err}`, "danger");
            }
        });

        // Test button handler (delegated) — currently only supports "pppp"
        $(document).on("click", ".dbg-test-svc", async function () {
            const name = $(this).data("svc-name");
            const resultDiv = $(`.dbg-test-result[data-svc-name="${name}"]`);
            $(this).prop("disabled", true).html('<i class="bi-hourglass-split"></i> Testing...');
            resultDiv.html('<span class="text-muted">running...</span>');
            try {
                const resp = await fetch(`/api/debug/services/${encodeURIComponent(name)}/test`, {
                    method: "POST",
                });
                const data = await resp.json();
                if (resp.ok) {
                    const ok = data.result === "ok";
                    localStorage.setItem("pppp_test_result", JSON.stringify({ result: ok ? "ok" : "fail", ts: Date.now() }));
                    resultDiv.html(`<span class="${ok ? "text-success" : "text-danger"}">
                        <i class="bi-${ok ? "check-circle" : "x-circle"}"></i>
                        Last result: ${ok ? "ok" : "fail"} <span class="text-muted">(just now)</span>
                    </span>`);
                    // Immediately reflect result in the main PPPP badge
                    if (ok) {
                        $("#badge-pppp").removeClass("text-bg-danger text-bg-warning text-bg-secondary").addClass("text-bg-success");
                    } else {
                        $("#badge-pppp").removeClass("text-bg-success text-bg-warning text-bg-secondary").addClass("text-bg-danger");
                    }
                } else {
                    resultDiv.html(`<span class="text-danger small">${escapeHtml(data.error || "Error")}</span>`);
                }
            } catch (err) {
                resultDiv.html(`<span class="text-danger small">${escapeHtml(String(err))}</span>`);
            } finally {
                $(this).prop("disabled", false).html('<i class="bi-wifi"></i> Test');
            }
        });

        document.getElementById("dbg-refresh-services").addEventListener("click", dbgRefreshServices);

        // Auto-refresh when the services pill is active
        const dbgServicesTab = document.getElementById("dbg-services-tab");
        let dbgServicesInterval = null;
        if (dbgServicesTab) {
            dbgServicesTab.addEventListener("shown.bs.tab", function () {
                dbgRefreshServices();
                dbgServicesInterval = setInterval(dbgRefreshServices, 5000);
            });
            dbgServicesTab.addEventListener("hidden.bs.tab", function () {
                if (dbgServicesInterval) { clearInterval(dbgServicesInterval); dbgServicesInterval = null; }
            });
        }

        // ------------------------------------------------------------------
        // Log Viewer (enhanced)
        // ------------------------------------------------------------------

        let _rawLogLines = [];

        const dbgLogFileSelect = $("#dbg-log-file");
        const dbgLogContent = document.getElementById("dbg-log-content");
        const dbgLogPre = document.getElementById("dbg-log-pre");
        const dbgLogLevelFilter = document.getElementById("dbg-log-level");
        const dbgLogSearch = document.getElementById("dbg-log-search");
        const dbgLogCount = document.getElementById("dbg-log-count");
        const dbgLogAutoRefresh = document.getElementById("dbg-log-autorefresh");
        const dbgLogLinesInput = document.getElementById("dbg-log-lines");
        let dbgLogRefreshInterval = null;

        // Restore persisted viewer height from localStorage
        const _savedLogHeight = localStorage.getItem("dbg_log_height");
        if (_savedLogHeight && dbgLogPre) {
            dbgLogPre.style.height = _savedLogHeight;
        }

        // Persist viewer height on resize via ResizeObserver
        if (dbgLogPre && typeof ResizeObserver !== "undefined") {
            new ResizeObserver(function () {
                localStorage.setItem("dbg_log_height", dbgLogPre.style.height || dbgLogPre.offsetHeight + "px");
            }).observe(dbgLogPre);
        }

        // Restore and persist the lines-to-fetch setting
        if (dbgLogLinesInput) {
            const _savedLines = localStorage.getItem("dbg_log_lines");
            if (_savedLines) {
                dbgLogLinesInput.value = _savedLines;
            }
            dbgLogLinesInput.addEventListener("change", function () {
                localStorage.setItem("dbg_log_lines", this.value);
            });
        }

        async function dbgRefreshLogList() {
            try {
                const resp = await fetch("/api/debug/logs");
                if (!resp.ok) return;
                const data = await resp.json();
                const currentVal = dbgLogFileSelect.val();
                dbgLogFileSelect.empty();
                $('<option value="" disabled selected>Select log file...</option>').appendTo(dbgLogFileSelect);
                data.files.forEach(file => {
                    const opt = $(`<option value="${escapeHtml(file)}">${escapeHtml(file)}</option>`);
                    if (file === currentVal) opt.prop("selected", true);
                    dbgLogFileSelect.append(opt);
                });
            } catch (err) {
                console.error("Failed to list logs:", err);
            }
        }

        /**
         * Render filtered log lines into the DOM, applying level filter,
         * text search with <mark> highlighting, and updating the line counter.
         */
        function dbgApplyLogFilters() {
            const levelFilter = dbgLogLevelFilter ? dbgLogLevelFilter.value.trim().toUpperCase() : "";
            const searchTerm = dbgLogSearch ? dbgLogSearch.value.trim() : "";
            const searchLower = searchTerm.toLowerCase();

            let filtered = _rawLogLines;

            if (levelFilter) {
                filtered = filtered.filter(line => line.toUpperCase().includes(levelFilter));
            }
            if (searchTerm) {
                filtered = filtered.filter(line => line.toLowerCase().includes(searchLower));
            }

            dbgLogCount.textContent = `${filtered.length} / ${_rawLogLines.length} lines`;

            if (!searchTerm) {
                // No search — just escape and join
                dbgLogContent.innerHTML = filtered.map(l => escapeHtml(l)).join("\n");
            } else {
                // Highlight search term with <mark>
                const escapedSearch = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                const re = new RegExp(`(${escapedSearch})`, "gi");
                dbgLogContent.innerHTML = filtered
                    .map(l => escapeHtml(l).replace(re, "<mark>$1</mark>"))
                    .join("\n");
            }

            // Auto-scroll to bottom only when the user is already near the bottom,
            // so that manually scrolling up to read earlier lines is not interrupted.
            if (dbgLogPre) {
                const atBottom = dbgLogPre.scrollHeight - dbgLogPre.scrollTop - dbgLogPre.clientHeight < 40;
                if (atBottom) {
                    dbgLogPre.scrollTop = dbgLogPre.scrollHeight;
                }
            }
        }

        async function dbgLoadLogContent() {
            const filename = dbgLogFileSelect.val();
            if (!filename) return;
            try {
                const lines = dbgLogLinesInput ? (parseInt(dbgLogLinesInput.value, 10) || 500) : 500;
                const resp = await fetch(`/api/debug/logs/${encodeURIComponent(filename)}?lines=${lines}`);
                if (resp.ok) {
                    const data = await resp.json();
                    _rawLogLines = data.content.split("\n");
                    dbgApplyLogFilters();
                } else {
                    dbgLogContent.textContent = `Error loading log: ${resp.status}`;
                }
            } catch (err) {
                dbgLogContent.textContent = `Error loading log: ${err}`;
            }
        }

        dbgLogFileSelect.on("change", dbgLoadLogContent);
        document.getElementById("dbg-log-refresh-btn").addEventListener("click", dbgLoadLogContent);

        if (dbgLogLevelFilter) {
            dbgLogLevelFilter.addEventListener("change", dbgApplyLogFilters);
        }
        if (dbgLogSearch) {
            dbgLogSearch.addEventListener("input", dbgApplyLogFilters);
        }

        const dbgLogsTab = document.getElementById("dbg-logs-tab");
        if (dbgLogsTab) {
            dbgLogsTab.addEventListener("shown.bs.tab", dbgRefreshLogList);
        }

        if (dbgLogAutoRefresh) {
            dbgLogAutoRefresh.addEventListener("change", function () {
                if (this.checked) {
                    dbgLoadLogContent();
                    dbgLogRefreshInterval = setInterval(dbgLoadLogContent, 5000);
                } else {
                    if (dbgLogRefreshInterval) { clearInterval(dbgLogRefreshInterval); dbgLogRefreshInterval = null; }
                }
            });
        }

        // Clean up intervals when leaving the Debug tab
        if (mainDebugTabBtn) {
            mainDebugTabBtn.addEventListener("hidden.bs.tab", function () {
                if (dbgStateInterval) { clearInterval(dbgStateInterval); dbgStateInterval = null; }
                if (dbgServicesInterval) { clearInterval(dbgServicesInterval); dbgServicesInterval = null; }
                if (dbgLogRefreshInterval) {
                    clearInterval(dbgLogRefreshInterval);
                    dbgLogRefreshInterval = null;
                    if (dbgLogAutoRefresh) dbgLogAutoRefresh.checked = false;
                }
            });
        }

        async function dbgRefreshBedLevel() {
            const statusEl = document.getElementById("dbg-bedlevel-status");
            const gridEl = document.getElementById("dbg-bedlevel-grid");
            const statsEl = document.getElementById("dbg-bedlevel-stats");
            const btn = document.getElementById("dbg-bedlevel-refresh");

            if (!statusEl || !gridEl) return;

            // Show loading state
            statusEl.innerHTML =
                '<div class="alert alert-info py-2 small mb-0">' +
                '<span class="spinner-border spinner-border-sm me-2" role="status"></span>' +
                'Sending M420 V — waiting for printer response (up to 15 s)...</div>';
            gridEl.style.display = "none";
            if (btn) btn.disabled = true;

            try {
                const resp = await fetch("/api/debug/bed-leveling");
                const data = await resp.json();

                if (!resp.ok) {
                    statusEl.innerHTML =
                        `<div class="alert alert-danger py-2 small mb-0">` +
                        `Error ${resp.status}: ${escapeHtml(data.error || "Unknown error")}</div>`;
                    return;
                }

                // Render stats bar
                if (statsEl) {
                    statsEl.innerHTML =
                        `<span><strong>Min:</strong> ${data.min.toFixed(3)} mm</span>` +
                        `<span><strong>Max:</strong> +${data.max.toFixed(3)} mm</span>` +
                        `<span><strong>Range:</strong> ${(data.max - data.min).toFixed(3)} mm</span>` +
                        `<span class="text-muted">(${data.rows}&times;${data.cols} grid)</span>`;
                }

                bedLevelRenderGrid(data.grid, data.min, data.max);

                statusEl.innerHTML = "";
                gridEl.style.display = "block";
            } catch (err) {
                statusEl.innerHTML =
                    `<div class="alert alert-danger py-2 small mb-0">` +
                    `Request failed: ${escapeHtml(String(err))}</div>`;
            } finally {
                if (btn) btn.disabled = false;
            }
        }

    } // end debug tab block

    /**
     * Filament Profiles
     */
    const filamentModal = document.getElementById("filamentModal");
    const bsFilamentModal = filamentModal ? new bootstrap.Modal(filamentModal) : null;

    function filamentToggleScarf() {
        const enabled = document.getElementById("filament-scarf-enabled");
        const opts    = document.getElementById("filament-scarf-opts");
        if (enabled && opts) opts.style.display = enabled.checked ? "" : "none";
    }

    function filamentToggleWipe() {
        const enabled = document.getElementById("filament-wipe-enabled");
        const opts    = document.getElementById("filament-wipe-opts");
        if (enabled && opts) opts.style.display = enabled.checked ? "" : "none";
    }

    function filamentReadForm() {
        return {
            name:                    document.getElementById("filament-name").value.trim(),
            brand:                   document.getElementById("filament-brand").value.trim(),
            material:                document.getElementById("filament-material").value.trim(),
            color:                   document.getElementById("filament-color").value,
            nozzle_temp_other_layer: parseInt(document.getElementById("filament-nozzle-temp-other").value, 10) || 0,
            nozzle_temp_first_layer: parseInt(document.getElementById("filament-nozzle-temp-first").value, 10) || 0,
            bed_temp_other_layer:    parseInt(document.getElementById("filament-bed-temp-other").value, 10) || 0,
            bed_temp_first_layer:    parseInt(document.getElementById("filament-bed-temp-first").value, 10) || 0,
            flow_rate:               parseFloat(document.getElementById("filament-flow-rate").value) || 1.0,
            filament_diameter:       parseFloat(document.getElementById("filament-diameter").value) || 1.75,
            pressure_advance:        parseFloat(document.getElementById("filament-pressure-advance").value) || 0,
            max_volumetric_speed:    parseFloat(document.getElementById("filament-max-vol-speed").value) || 0,
            travel_speed:            parseInt(document.getElementById("filament-travel-speed").value, 10) || 0,
            perimeter_speed:         parseInt(document.getElementById("filament-perimeter-speed").value, 10) || 0,
            infill_speed:            parseInt(document.getElementById("filament-infill-speed").value, 10) || 0,
            cooling_enabled:         document.getElementById("filament-cooling-enabled").checked ? 1 : 0,
            cooling_min_fan_speed:   parseInt(document.getElementById("filament-cooling-min").value, 10) || 0,
            cooling_max_fan_speed:   parseInt(document.getElementById("filament-cooling-max").value, 10) || 100,
            seam_position:           document.getElementById("filament-seam-position").value,
            seam_gap:                parseFloat(document.getElementById("filament-seam-gap").value) || 0,
            scarf_enabled:           document.getElementById("filament-scarf-enabled").checked ? 1 : 0,
            scarf_conditional:       document.getElementById("filament-scarf-conditional").checked ? 1 : 0,
            scarf_angle_threshold:   parseInt(document.getElementById("filament-scarf-angle").value, 10) || 155,
            scarf_length:            parseFloat(document.getElementById("filament-scarf-length").value) || 20.0,
            scarf_steps:             parseInt(document.getElementById("filament-scarf-steps").value, 10) || 10,
            scarf_speed:             parseInt(document.getElementById("filament-scarf-speed").value, 10) || 100,
            retract_length:          parseFloat(document.getElementById("filament-retract-length").value) || 0,
            retract_speed:           parseInt(document.getElementById("filament-retract-speed").value, 10) || 45,
            retract_lift_z:          parseFloat(document.getElementById("filament-retract-lift-z").value) || 0,
            wipe_enabled:            document.getElementById("filament-wipe-enabled").checked ? 1 : 0,
            wipe_distance:           parseFloat(document.getElementById("filament-wipe-distance").value) || 1.5,
            wipe_speed:              parseInt(document.getElementById("filament-wipe-speed").value, 10) || 40,
            wipe_retract_before:     document.getElementById("filament-wipe-retract-before").checked ? 1 : 0,
            notes:                   document.getElementById("filament-notes").value.trim(),
        };
    }

    function filamentFillForm(p) {
        document.getElementById("filament-id").value                       = p.id || "";
        document.getElementById("filament-name").value                     = p.name || "";
        document.getElementById("filament-brand").value                    = p.brand || "";
        document.getElementById("filament-material").value                 = p.material || "";
        document.getElementById("filament-color").value                    = p.color || "#FFFFFF";
        document.getElementById("filament-nozzle-temp-other").value        = p.nozzle_temp_other_layer ?? p.nozzle_temp ?? 220;
        document.getElementById("filament-nozzle-temp-first").value        = p.nozzle_temp_first_layer ?? (p.nozzle_temp_other_layer ?? p.nozzle_temp ?? 220) + 5;
        document.getElementById("filament-bed-temp-other").value           = p.bed_temp_other_layer ?? p.bed_temp ?? 60;
        document.getElementById("filament-bed-temp-first").value           = p.bed_temp_first_layer ?? (p.bed_temp_other_layer ?? p.bed_temp ?? 60) + 5;
        document.getElementById("filament-flow-rate").value                = p.flow_rate ?? 1.0;
        document.getElementById("filament-diameter").value                 = p.filament_diameter ?? 1.75;
        document.getElementById("filament-pressure-advance").value         = p.pressure_advance ?? 0;
        document.getElementById("filament-max-vol-speed").value            = p.max_volumetric_speed ?? 15;
        document.getElementById("filament-travel-speed").value             = p.travel_speed ?? 120;
        document.getElementById("filament-perimeter-speed").value          = p.perimeter_speed ?? 60;
        document.getElementById("filament-infill-speed").value             = p.infill_speed ?? 80;
        document.getElementById("filament-cooling-enabled").checked        = !!p.cooling_enabled;
        document.getElementById("filament-cooling-min").value              = p.cooling_min_fan_speed ?? 0;
        document.getElementById("filament-cooling-max").value              = p.cooling_max_fan_speed ?? 100;
        document.getElementById("filament-seam-position").value            = p.seam_position || "aligned";
        document.getElementById("filament-seam-gap").value                 = p.seam_gap ?? 0;
        document.getElementById("filament-scarf-enabled").checked          = !!p.scarf_enabled;
        document.getElementById("filament-scarf-conditional").checked      = !!p.scarf_conditional;
        document.getElementById("filament-scarf-angle").value              = p.scarf_angle_threshold ?? 155;
        document.getElementById("filament-scarf-length").value             = p.scarf_length ?? 20;
        document.getElementById("filament-scarf-steps").value              = p.scarf_steps ?? 10;
        document.getElementById("filament-scarf-speed").value              = p.scarf_speed ?? 100;
        document.getElementById("filament-retract-length").value           = p.retract_length ?? 0.8;
        document.getElementById("filament-retract-speed").value            = p.retract_speed ?? 45;
        document.getElementById("filament-retract-lift-z").value           = p.retract_lift_z ?? 0;
        document.getElementById("filament-wipe-enabled").checked           = !!p.wipe_enabled;
        document.getElementById("filament-wipe-distance").value            = p.wipe_distance ?? 1.5;
        document.getElementById("filament-wipe-speed").value               = p.wipe_speed ?? 40;
        document.getElementById("filament-wipe-retract-before").checked    = !!p.wipe_retract_before;
        document.getElementById("filament-notes").value                    = p.notes || "";
        // Sync conditional sub-section visibility
        filamentToggleScarf();
        filamentToggleWipe();
    }

    function filamentOpenNew() {
        filamentFillForm({});
        document.getElementById("filamentModalLabel").textContent = "New Filament Profile";
        if (bsFilamentModal) bsFilamentModal.show();
    }

    function filamentOpenEdit(profile) {
        filamentFillForm(profile);
        document.getElementById("filamentModalLabel").textContent = "Edit Filament Profile";
        if (bsFilamentModal) bsFilamentModal.show();
    }

    let _filamentSortAsc = true;
    let _filamentAllProfiles = [];
    let _filamentSwapToken = null;
    let _filamentSwapMode = null;
    let _filamentSwapPollHandle = null;
    let _filamentServiceTemps = {
        nozzleCurrent: null,
        nozzleTarget: null,
        bedCurrent: null,
        bedTarget: null,
    };
    let _filamentSwapSettings = {
        allow_legacy_swap: false,
        manual_swap_preheat_temp_c: 180,
        quick_move_length_mm: 40,
        swap_prime_length_mm: 10,
        swap_unload_length_mm: 60,
        swap_load_length_mm: 120,
        swap_home_pause_s: 55,
    };

    function filamentFindProfileById(profileId) {
        const id = parseInt(profileId, 10);
        if (!Number.isFinite(id)) return null;
        return _filamentAllProfiles.find(p => parseInt(p.id, 10) === id) || null;
    }

    function renderFilamentServiceTemps() {
        const nozzleCurrentEl = document.getElementById("filament-service-nozzle-current");
        const nozzleTargetEl = document.getElementById("filament-service-nozzle-target");
        const bedCurrentEl = document.getElementById("filament-service-bed-current");
        const bedTargetEl = document.getElementById("filament-service-bed-target");
        if (!nozzleCurrentEl || !nozzleTargetEl || !bedCurrentEl || !bedTargetEl) {
            return;
        }
        nozzleCurrentEl.textContent = formatServiceTempValue(_filamentServiceTemps.nozzleCurrent);
        nozzleTargetEl.textContent = `Target: ${formatServiceTempValue(_filamentServiceTemps.nozzleTarget)}`;
        bedCurrentEl.textContent = formatServiceTempValue(_filamentServiceTemps.bedCurrent);
        bedTargetEl.textContent = `Target: ${formatServiceTempValue(_filamentServiceTemps.bedTarget)}`;
    }

    function updateFilamentServiceTemps(partial = {}) {
        if (!partial || typeof partial !== "object") {
            return;
        }
        const keys = ["nozzleCurrent", "nozzleTarget", "bedCurrent", "bedTarget"];
        keys.forEach((key) => {
            if (Object.prototype.hasOwnProperty.call(partial, key)) {
                _filamentServiceTemps[key] = partial[key];
            }
        });
        renderFilamentServiceTemps();
    }

    function filamentServiceTemp(profile) {
        if (!profile) return "";
        return profile.nozzle_temp_other_layer ?? profile.nozzle_temp_first_layer ?? profile.nozzle_temp ?? "";
    }

    function filamentSetServiceStatus(message, level = "secondary") {
        const statusEl = document.getElementById("filament-service-status");
        if (!statusEl) return;
        statusEl.className = `alert alert-${level} py-2 small mb-3`;
        statusEl.textContent = message;
    }

    function filamentSetSwapSettingsStatus(message, level = "muted") {
        const el = document.getElementById("filament-swap-settings-status");
        if (!el) return;
        el.className = level === "muted" ? "text-muted small" : `text-${level} small`;
        el.textContent = message || "";
    }

    function filamentSetTableMessage(message, tone = "muted") {
        const tbody = document.getElementById("filaments-tbody");
        if (!tbody) return;
        const textClass = tone === "danger" ? "text-danger" : "text-muted";
        tbody.innerHTML = `<tr><td colspan="8" class="text-center ${textClass} py-4">${escapeHtml(message)}</td></tr>`;
    }

    function filamentSetSwapStateMessage(message) {
        const stateEl = document.getElementById("filament-swap-state");
        if (!stateEl) return;
        stateEl.textContent = message || "";
    }

    async function filamentJsonRequest(url, options = {}, fallbackMessage = "Request failed") {
        const resp = await fetch(url, options);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.error || `${fallbackMessage} (HTTP ${resp.status})`);
        }
        return data;
    }

    function filamentPopulateSelect(selectId, selectedValue = "") {
        const select = document.getElementById(selectId);
        if (!select) return;
        const previous = String(selectedValue || select.value || "");
        select.innerHTML = '<option value="">Select profile...</option>';
        _filamentAllProfiles.forEach(p => {
            const option = document.createElement("option");
            option.value = String(p.id);
            const temp = filamentServiceTemp(p);
            option.textContent = temp ? `${p.name} (${temp}°C)` : p.name;
            if (option.value === previous) option.selected = true;
            select.appendChild(option);
        });
    }

    function filamentSyncQuickServiceTemp() {
        const profile = filamentFindProfileById(document.getElementById("filament-service-profile")?.value);
        const tempEl = document.getElementById("filament-service-temp");
        if (!tempEl) return;
        tempEl.value = profile ? filamentServiceTemp(profile) : "";
    }

    function filamentUpdateSwapModeUi() {
        const legacyEnabled = !!_filamentSwapSettings.allow_legacy_swap;
        [
            "filament-swap-unload-profile",
            "filament-swap-load-profile",
            "filament-swap-prime-length",
            "filament-swap-unload-length",
            "filament-swap-load-length",
            "filament-swap-home-pause",
        ].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.disabled = !legacyEnabled;
        });

        const stateEl = document.getElementById("filament-swap-state");
        if (!stateEl || _filamentSwapToken) return;

        if (legacyEnabled) {
            stateEl.textContent = "Guided automatic swap enabled. Start Swap will heat, home, raise Z, prime, retract, and then wait for you to load the new filament.";
        } else {
            stateEl.textContent =
                `Recommended guided swap enabled. Start Swap will preheat to ${_filamentSwapSettings.manual_swap_preheat_temp_c}°C and wait for a manual filament change.`;
        }
    }

    async function filamentLoadSwapSettings() {
        try {
            const resp = await fetch("/api/settings/filament-service");
            const data = await resp.json();
            if (!resp.ok) {
                filamentSetSwapSettingsStatus(data.error || `Failed to load swap settings (HTTP ${resp.status})`, "danger");
                return;
            }
            _filamentSwapSettings = data.filament_service || _filamentSwapSettings;
            const quickLengthEl = document.getElementById("filament-service-length");
            const tempEl = document.getElementById("filament-manual-swap-temp");
            const legacyEl = document.getElementById("filament-allow-legacy-swap");
            const primeLengthEl = document.getElementById("filament-swap-prime-length");
            const unloadLengthEl = document.getElementById("filament-swap-unload-length");
            const loadLengthEl = document.getElementById("filament-swap-load-length");
            const homePauseEl = document.getElementById("filament-swap-home-pause");
            if (quickLengthEl) quickLengthEl.value = _filamentSwapSettings.quick_move_length_mm ?? 40;
            if (tempEl) tempEl.value = _filamentSwapSettings.manual_swap_preheat_temp_c ?? 180;
            if (legacyEl) legacyEl.checked = !!_filamentSwapSettings.allow_legacy_swap;
            if (primeLengthEl) primeLengthEl.value = _filamentSwapSettings.swap_prime_length_mm ?? 10;
            if (unloadLengthEl) unloadLengthEl.value = _filamentSwapSettings.swap_unload_length_mm ?? 60;
            if (loadLengthEl) loadLengthEl.value = _filamentSwapSettings.swap_load_length_mm ?? 120;
            if (homePauseEl) homePauseEl.value = _filamentSwapSettings.swap_home_pause_s ?? 55;
            filamentUpdateSwapModeUi();
            filamentSetSwapSettingsStatus(
                _filamentSwapSettings.allow_legacy_swap
                    ? "Guided automatic swap is enabled."
                    : "Recommended manual swap is enabled.",
                "muted"
            );
        } catch (err) {
            filamentSetSwapSettingsStatus(`Failed to load swap settings: ${err}`, "danger");
        }
    }

    function filamentStartSwapPolling() {
        if (_filamentSwapPollHandle) return;
        _filamentSwapPollHandle = window.setInterval(() => {
            filamentRefreshSwapState();
        }, 2000);
    }

    function filamentStopSwapPolling() {
        if (_filamentSwapPollHandle) {
            window.clearInterval(_filamentSwapPollHandle);
            _filamentSwapPollHandle = null;
        }
    }

    function filamentUpdateSwapState(data) {
        const stateEl = document.getElementById("filament-swap-state");
        const startBtn = document.getElementById("filament-swap-start-btn");
        const confirmBtn = document.getElementById("filament-swap-confirm-btn");
        const cancelBtn = document.getElementById("filament-swap-cancel-btn");
        const swap = data && data.pending ? data.swap : null;
        const previousSwapToken = _filamentSwapToken;
        const previousSwapMode = _filamentSwapMode;
        const running = swap && ["homing", "heating_unload", "priming_unload", "unloading", "heating_load", "loading", "cooling_down"].includes(swap.phase);

        _filamentSwapToken = swap ? swap.token : null;
        _filamentSwapMode = swap ? swap.mode : null;

        if (startBtn) startBtn.disabled = !!swap;
        if (confirmBtn) confirmBtn.disabled = !swap || running;
        if (cancelBtn) cancelBtn.disabled = !swap;

        if (swap) {
            filamentStartSwapPolling();
        } else {
            filamentStopSwapPolling();
        }

        if (!stateEl) return;
        if (!swap) {
            if (previousSwapToken) {
                const message = data?.message || (
                    previousSwapMode === "legacy"
                        ? "Filament changed. Nozzle heater turned off."
                        : "Manual filament swap complete."
                );
                filamentSetSwapStateMessage(message);
                filamentSetServiceStatus(message, previousSwapMode === "legacy" ? "success" : "secondary");
            } else {
                filamentUpdateSwapModeUi();
            }
            return;
        }

        if (swap.mode === "manual") {
            stateEl.textContent = swap.message ||
                `Manual swap pending. Nozzle preheating to ${swap.manual_swap_preheat_temp_c}°C.`;
            return;
        }

        stateEl.textContent = swap.message ||
            `Pending swap: unload ${swap.unload_profile_name} (${swap.unload_length_mm} mm @ ${swap.unload_temp_c}°C), ` +
            `then load ${swap.load_profile_name} (${swap.load_length_mm} mm @ ${swap.load_temp_c}°C).`;
    }

    async function filamentRefreshSwapState() {
        try {
            const data = await filamentJsonRequest(withActivePrinterQuery("/api/filaments/service/swap"), {}, "Failed to load swap state");
            filamentUpdateSwapState(data);
        } catch (err) {
            console.warn("Filament swap state refresh failed:", err);
            if (_filamentSwapToken) {
                filamentSetSwapStateMessage(`Swap status refresh failed: ${err.message}. The last known state may be stale.`);
            }
        }
    }

    async function filamentServiceRequest(url, payload) {
        return filamentJsonRequest(withActivePrinterQuery(url), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {}),
        }, "Filament service request failed");
    }

    function _renderFilaments() {
        const tbody = document.getElementById("filaments-tbody");
        if (!tbody) return;
        const query = (document.getElementById("filament-search")?.value || "").toLowerCase().trim();
        let profiles = _filamentAllProfiles.slice();
        if (query) {
            profiles = profiles.filter(p =>
                (p.name || "").toLowerCase().includes(query) ||
                (p.material || "").toLowerCase().includes(query) ||
                (p.brand || "").toLowerCase().includes(query)
            );
        }
        profiles.sort((a, b) => {
            const cmp = (a.name || "").localeCompare(b.name || "");
            return _filamentSortAsc ? cmp : -cmp;
        });
        if (profiles.length === 0) {
            filamentSetTableMessage("No filament profiles found.");
            return;
        }
        tbody.innerHTML = "";
        profiles.forEach(p => {
                    const safeName     = escapeHtml(p.name);
                    const safeMaterial = escapeHtml(p.material || "");
                    const safeBrand    = escapeHtml(p.brand || "");
                    const safeId       = parseInt(p.id, 10);
                    const dotColor     = escapeHtml(p.color || "#FFFFFF");
                    const colorDot     = `<span style="display:inline-block;width:1.1rem;height:1.1rem;border-radius:50%;background:${dotColor};border:1px solid #aaa;vertical-align:middle;box-shadow:inset 0 0 0 1px rgba(0,0,0,0.08);"></span>`;
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td class="text-center">${colorDot}</td>
                        <td class="fw-semibold">${safeName}</td>
                        <td>${safeMaterial}</td>
                        <td class="text-muted small">${safeBrand}</td>
                        <td>${p.nozzle_temp_other_layer ?? p.nozzle_temp ?? "-"}&thinsp;°C</td>
                        <td>${p.bed_temp_other_layer ?? p.bed_temp ?? "-"}&thinsp;°C</td>
                        <td>${p.filament_diameter}&thinsp;mm</td>
                        <td class="text-end" style="white-space:nowrap;">
                            <div class="d-flex gap-1 justify-content-end">
                                <button class="btn btn-sm btn-outline-secondary filament-edit" data-id="${safeId}" title="Edit">
                                    <i class="bi bi-pencil"></i>
                                </button>
                                <button class="btn btn-sm btn-outline-info filament-duplicate" data-id="${safeId}" title="Duplicate">
                                    <i class="bi bi-files"></i>
                                </button>
                                <button class="btn btn-sm btn-outline-warning filament-preheat" data-id="${safeId}" title="Preheat printer to these temperatures">
                                    <i class="bi bi-thermometer-half"></i>
                                </button>
                                <button class="btn btn-sm btn-outline-danger filament-delete" data-id="${safeId}" title="Delete">
                                    <i class="bi bi-trash"></i>
                                </button>
                            </div>
                        </td>`;
                    tr.querySelector(".filament-edit").addEventListener("click", () => filamentOpenEdit(p));
                    tr.querySelector(".filament-duplicate").addEventListener("click", async () => {
                        try {
                            await filamentJsonRequest(`/api/filaments/${safeId}/duplicate`, { method: "POST" }, "Failed to duplicate filament profile");
                            await loadFilaments();
                            flash_message(`Created a copy of "${p.name}".`, "success");
                        } catch (err) {
                            flash_message(`Duplicate failed: ${err.message}`, "danger");
                        }
                    });
                    tr.querySelector(".filament-preheat").addEventListener("click", async () => {
                        const nozzle = p.nozzle_temp_first_layer ?? p.nozzle_temp_other_layer ?? p.nozzle_temp ?? "?";
                        const bed    = p.bed_temp_first_layer ?? p.bed_temp_other_layer ?? p.bed_temp ?? "?";
                        if (!confirm(`Preheat printer for ${p.name}?\nNozzle: ${nozzle}°C, Bed: ${bed}°C`)) return;
                        try {
                            await filamentJsonRequest(withActivePrinterQuery(`/api/filaments/${safeId}/apply`), { method: "POST" }, "Failed to preheat filament profile");
                            filamentSetServiceStatus(`Preheating ${p.name}: nozzle ${nozzle}\u00B0C, bed ${bed}\u00B0C.`, "warning");
                        } catch (err) {
                            filamentSetServiceStatus(`Preheat failed: ${err.message}`, "danger");
                            flash_message(`Preheat failed: ${err.message}`, "danger");
                        }
                    });
                    tr.querySelector(".filament-delete").addEventListener("click", async () => {
                        if (!confirm(`Delete filament profile "${p.name}"?`)) return;
                        try {
                            await filamentJsonRequest(`/api/filaments/${safeId}`, { method: "DELETE" }, "Failed to delete filament profile");
                            await loadFilaments();
                            flash_message(`Deleted filament profile "${p.name}".`, "success");
                        } catch (err) {
                            flash_message(`Delete failed: ${err.message}`, "danger");
                        }
                    });
                    tbody.appendChild(tr);
                });
    }

    async function loadFilaments() {
        filamentSetTableMessage("Loading filament profiles...");
        try {
            const data = await filamentJsonRequest("/api/filaments", {}, "Failed to load filament profiles");
            _filamentAllProfiles = data.filaments || [];
            filamentPopulateSelect("filament-service-profile");
            filamentPopulateSelect("filament-swap-unload-profile");
            filamentPopulateSelect("filament-swap-load-profile");
            filamentSyncQuickServiceTemp();
            _renderFilaments();
        } catch (err) {
            _filamentAllProfiles = [];
            filamentPopulateSelect("filament-service-profile");
            filamentPopulateSelect("filament-swap-unload-profile");
            filamentPopulateSelect("filament-swap-load-profile");
            filamentSyncQuickServiceTemp();
            filamentSetTableMessage(`Failed to load filament profiles. ${err.message}`, "danger");
            flash_message(`Failed to load filament profiles: ${err.message}`, "danger");
        }
    }

    // Sort button
    const filamentSortBtn = document.getElementById("filament-sort-btn");
    if (filamentSortBtn) {
        filamentSortBtn.addEventListener("click", function () {
            _filamentSortAsc = !_filamentSortAsc;
            const icon = document.getElementById("filament-sort-icon");
            if (icon) {
                icon.className = _filamentSortAsc ? "bi bi-sort-alpha-down" : "bi bi-sort-alpha-up";
            }
            _renderFilaments();
        });
    }

    // Search input
    const filamentSearch = document.getElementById("filament-search");
    if (filamentSearch) {
        filamentSearch.addEventListener("input", function () { _renderFilaments(); });
    }

    const filamentServiceProfile = document.getElementById("filament-service-profile");
    if (filamentServiceProfile) {
        filamentServiceProfile.addEventListener("change", filamentSyncQuickServiceTemp);
    }

    const filamentServicePreheatBtn = document.getElementById("filament-service-preheat-btn");
    if (filamentServicePreheatBtn) {
        filamentServicePreheatBtn.addEventListener("click", async function () {
            const profileId = document.getElementById("filament-service-profile")?.value;
            if (!profileId) {
                filamentSetServiceStatus("Select a filament profile first.", "warning");
                return;
            }
            try {
                const res = await filamentServiceRequest("/api/filaments/service/preheat", {
                    profile_id: parseInt(profileId, 10),
                });
                filamentSetServiceStatus(
                    `Preheating ${res.profile_name} to ${res.target_temp_c}°C.`,
                    "warning"
                );
            } catch (err) {
                filamentSetServiceStatus(`Preheat failed: ${err.message}`, "danger");
            }
        });
    }

    const filamentServiceExtrudeBtn = document.getElementById("filament-service-extrude-btn");
    if (filamentServiceExtrudeBtn) {
        filamentServiceExtrudeBtn.addEventListener("click", async function () {
            const profileId = document.getElementById("filament-service-profile")?.value;
            const lengthMm = parseFloat(document.getElementById("filament-service-length")?.value || "0");
            if (!profileId) {
                filamentSetServiceStatus("Select a filament profile first.", "warning");
                return;
            }
            try {
                const res = await filamentServiceRequest("/api/filaments/service/move", {
                    profile_id: parseInt(profileId, 10),
                    action: "extrude",
                    length_mm: lengthMm,
                });
                filamentSetServiceStatus(
                    `Extruding ${res.length_mm} mm with ${res.profile_name} at ${res.target_temp_c}°C.`,
                    "success"
                );
            } catch (err) {
                filamentSetServiceStatus(`Extrude failed: ${err.message}`, "danger");
            }
        });
    }

    const filamentServiceRetractBtn = document.getElementById("filament-service-retract-btn");
    if (filamentServiceRetractBtn) {
        filamentServiceRetractBtn.addEventListener("click", async function () {
            const profileId = document.getElementById("filament-service-profile")?.value;
            const lengthMm = parseFloat(document.getElementById("filament-service-length")?.value || "0");
            if (!profileId) {
                filamentSetServiceStatus("Select a filament profile first.", "warning");
                return;
            }
            try {
                const res = await filamentServiceRequest("/api/filaments/service/move", {
                    profile_id: parseInt(profileId, 10),
                    action: "retract",
                    length_mm: lengthMm,
                });
                filamentSetServiceStatus(
                    `Retracting ${res.length_mm} mm with ${res.profile_name} at ${res.target_temp_c}°C.`,
                    "secondary"
                );
            } catch (err) {
                filamentSetServiceStatus(`Retract failed: ${err.message}`, "danger");
            }
        });
    }

    const filamentServiceCooldownBtn = document.getElementById("filament-service-cooldown-btn");
    if (filamentServiceCooldownBtn) {
        filamentServiceCooldownBtn.addEventListener("click", function () {
            sendPrinterGCode("M104 S0\nM140 S0\nM106 S0");
            filamentSetServiceStatus("Cooldown sent: nozzle, bed and fan set to 0.", "secondary");
        });
    }

    const filamentSwapStartBtn = document.getElementById("filament-swap-start-btn");
    if (filamentSwapStartBtn) {
        filamentSwapStartBtn.addEventListener("click", async function () {
            let keepStartDisabled = false;
            this.disabled = true;
            try {
                const legacyEnabled = !!document.getElementById("filament-allow-legacy-swap")?.checked;
                const manualTempC = parseInt(document.getElementById("filament-manual-swap-temp")?.value || "180", 10);
                let payload = {
                    allow_legacy_swap: legacyEnabled,
                    manual_swap_preheat_temp_c: manualTempC,
                };
                if (legacyEnabled) {
                    const unloadProfileId = parseInt(document.getElementById("filament-swap-unload-profile")?.value || "", 10);
                    const loadProfileId = parseInt(document.getElementById("filament-swap-load-profile")?.value || "", 10);
                    const primeLengthMm = parseFloat(document.getElementById("filament-swap-prime-length")?.value || "0");
                    const unloadLengthMm = parseFloat(document.getElementById("filament-swap-unload-length")?.value || "0");
                    const loadLengthMm = parseFloat(document.getElementById("filament-swap-load-length")?.value || "0");
                    const homePauseS = parseFloat(document.getElementById("filament-swap-home-pause")?.value || "55");
                    if (!Number.isFinite(unloadProfileId) || !Number.isFinite(loadProfileId)) {
                        filamentSetServiceStatus("Select unload and load profiles first.", "warning");
                        return;
                    }
                    if (!window.confirm("Guided automatic swap will home the printer first. Make sure the bed and toolhead path are clear before starting.")) {
                        return;
                    }
                    payload = {
                        ...payload,
                        unload_profile_id: unloadProfileId,
                        load_profile_id: loadProfileId,
                        prime_length_mm: primeLengthMm,
                        unload_length_mm: unloadLengthMm,
                        load_length_mm: loadLengthMm,
                        home_pause_s: homePauseS,
                    };
                }
                filamentSetServiceStatus(
                    legacyEnabled
                        ? "Guided automatic swap started. Heating, homing, and raising Z before unload..."
                        : `Recommended guided swap started. Preheating to ${manualTempC}°C...`,
                    "warning"
                );
                const res = await filamentServiceRequest("/api/filaments/service/swap/start", payload);
                filamentUpdateSwapState(res);
                keepStartDisabled = !!res.pending;
                filamentSetServiceStatus(res.message, legacyEnabled ? "primary" : "warning");
            } catch (err) {
                filamentSetServiceStatus(`Swap start failed: ${err.message}`, "danger");
            } finally {
                if (!keepStartDisabled && !_filamentSwapToken) {
                    this.disabled = false;
                }
            }
        });
    }

    const filamentSwapConfirmBtn = document.getElementById("filament-swap-confirm-btn");
    if (filamentSwapConfirmBtn) {
        filamentSwapConfirmBtn.addEventListener("click", async function () {
            try {
                filamentSetServiceStatus("Continuing swap...", "warning");
                const res = await filamentServiceRequest("/api/filaments/service/swap/confirm", {
                    token: _filamentSwapToken,
                });
                filamentUpdateSwapState(res);
                filamentSetServiceStatus(
                    res.message,
                    res.pending ? "warning" : "success"
                );
            } catch (err) {
                filamentSetServiceStatus(`Swap confirm failed: ${err.message}`, "danger");
            }
        });
    }

    const filamentSwapCancelBtn = document.getElementById("filament-swap-cancel-btn");
    if (filamentSwapCancelBtn) {
        filamentSwapCancelBtn.addEventListener("click", async function () {
            try {
                const res = await filamentServiceRequest("/api/filaments/service/swap/cancel", {
                    token: _filamentSwapToken,
                });
                filamentUpdateSwapState(res);
                filamentSetServiceStatus(res.message || "Filament swap cancelled.", "secondary");
            } catch (err) {
                filamentSetServiceStatus(`Swap cancel failed: ${err.message}`, "danger");
            }
        });
    }

    const filamentSaveSwapSettingsBtn = document.getElementById("filament-save-swap-settings-btn");
    if (filamentSaveSwapSettingsBtn) {
        filamentSaveSwapSettingsBtn.addEventListener("click", async function () {
            const quickLengthEl = document.getElementById("filament-service-length");
            const tempEl = document.getElementById("filament-manual-swap-temp");
            const legacyEl = document.getElementById("filament-allow-legacy-swap");
            const primeLengthEl = document.getElementById("filament-swap-prime-length");
            const unloadLengthEl = document.getElementById("filament-swap-unload-length");
            const loadLengthEl = document.getElementById("filament-swap-load-length");
            const homePauseEl = document.getElementById("filament-swap-home-pause");
            const tempC = parseInt(tempEl?.value || "180", 10);
            try {
                const res = await filamentServiceRequest("/api/settings/filament-service", {
                    filament_service: {
                        allow_legacy_swap: !!legacyEl?.checked,
                        manual_swap_preheat_temp_c: tempC,
                        quick_move_length_mm: parseFloat(quickLengthEl?.value || "40"),
                        swap_prime_length_mm: parseFloat(primeLengthEl?.value || "10"),
                        swap_unload_length_mm: parseFloat(unloadLengthEl?.value || "60"),
                        swap_load_length_mm: parseFloat(loadLengthEl?.value || "120"),
                        swap_home_pause_s: parseFloat(homePauseEl?.value || "55"),
                    },
                });
                _filamentSwapSettings = res.filament_service || _filamentSwapSettings;
                if (quickLengthEl) quickLengthEl.value = _filamentSwapSettings.quick_move_length_mm ?? 40;
                if (tempEl) tempEl.value = _filamentSwapSettings.manual_swap_preheat_temp_c ?? 180;
                if (legacyEl) legacyEl.checked = !!_filamentSwapSettings.allow_legacy_swap;
                if (primeLengthEl) primeLengthEl.value = _filamentSwapSettings.swap_prime_length_mm ?? 10;
                if (unloadLengthEl) unloadLengthEl.value = _filamentSwapSettings.swap_unload_length_mm ?? 60;
                if (loadLengthEl) loadLengthEl.value = _filamentSwapSettings.swap_load_length_mm ?? 120;
                if (homePauseEl) homePauseEl.value = _filamentSwapSettings.swap_home_pause_s ?? 55;
                filamentUpdateSwapModeUi();
                filamentSetSwapSettingsStatus("Filament service settings saved.", "success");
            } catch (err) {
                filamentSetSwapSettingsStatus(`Failed to save filament service settings: ${err.message}`, "danger");
            }
        });
    }

    const filamentAllowLegacySwap = document.getElementById("filament-allow-legacy-swap");
    if (filamentAllowLegacySwap) {
        filamentAllowLegacySwap.addEventListener("change", function () {
            _filamentSwapSettings.allow_legacy_swap = !!this.checked;
            filamentUpdateSwapModeUi();
        });
    }

    // Save button: create or update
    const filamentSaveBtn = document.getElementById("filament-save-btn");
    if (filamentSaveBtn) {
        filamentSaveBtn.addEventListener("click", async function () {
            const profileId = document.getElementById("filament-id").value;
            const payload   = filamentReadForm();
            payload.name = String(payload.name || "").trim();
            if (!payload.name) {
                document.getElementById("filament-name").classList.add("is-invalid");
                flash_message("Filament profile name is required.", "warning");
                return;
            }
            document.getElementById("filament-name").classList.remove("is-invalid");

            const isNew  = !profileId;
            const url    = isNew ? "/api/filaments" : `/api/filaments/${profileId}`;
            const method = isNew ? "POST" : "PUT";
            try {
                await filamentJsonRequest(url, {
                    method: method,
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                }, "Failed to save filament profile");
                if (bsFilamentModal) bsFilamentModal.hide();
                await loadFilaments();
                flash_message(
                    isNew ? `Created filament profile "${payload.name}".` : `Saved filament profile "${payload.name}".`,
                    "success"
                );
            } catch (err) {
                flash_message(`Save failed: ${err.message}`, "danger");
            }
        });
    }

    const filamentNewBtn = document.getElementById("filament-new-btn");
    if (filamentNewBtn) {
        filamentNewBtn.addEventListener("click", filamentOpenNew);
    }

    // Scarf sub-section toggle
    const scarfEnabledEl = document.getElementById("filament-scarf-enabled");
    if (scarfEnabledEl) {
        scarfEnabledEl.addEventListener("change", filamentToggleScarf);
    }

    // Wipe sub-section toggle
    const wipeEnabledEl = document.getElementById("filament-wipe-enabled");
    if (wipeEnabledEl) {
        wipeEnabledEl.addEventListener("change", filamentToggleWipe);
    }

    // Load when tab becomes active
    const filamentsTabBtn = document.querySelector('button[data-bs-target="#filaments"]');
    if (filamentsTabBtn) {
        filamentsTabBtn.addEventListener("shown.bs.tab", function () {
            loadFilaments();
            filamentLoadSwapSettings();
            filamentRefreshSwapState();
        });
        filamentsTabBtn.addEventListener("hidden.bs.tab", function () {
            filamentStopSwapPolling();
        });
    }

    // Printer selector — switch active printer from the navbar dropdown
    document.querySelectorAll("#printer-selector .dropdown-item").forEach(function(item) {
        item.addEventListener("click", function(e) {
            e.preventDefault();
            var newIndex = parseInt(this.getAttribute("data-printer-index"), 10);
            // Skip if already active or if the device is unsupported (disabled item)
            if (isNaN(newIndex) || this.classList.contains("active") || this.classList.contains("disabled")) return;

            if (!confirm("Switch printer? Camera / PPPP connections may reconnect.")) return;

            fetch("/api/printers/active", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({index: newIndex})
            })
            .then(function(resp) {
                return resp.json().then(function(data) { return {ok: resp.ok, data: data}; });
            })
            .then(function(r) {
                if (!r.ok) {
                    alert("Error: " + (r.data.error || "Failed to switch printer"));
                    return;
                }
                // Reload shortly so the UI reconnects to the selected printer.
                setTimeout(function() { window.location.reload(); }, 1000);
            })
            .catch(function(err) {
                alert("Failed to switch printer: " + err);
            });
        });
    });

});
