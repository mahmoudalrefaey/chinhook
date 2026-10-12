// Chinhook: the page's behaviour, in plain JavaScript with no dependencies and no build step.
//
// The server holds everything that matters (the connection, the conversation); this file draws
// what /api/state says and calls the API when the visitor does something. Text from the server
// is always placed with textContent. The only HTML ever inserted is an answer, which the server
// has already rendered from Markdown and sanitised against an allowlist (chinhook/web/render.py),
// and the constant icon drawings below.

// ---------- small helpers ----------

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
        if (value === undefined || value === null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else if (key in node && typeof value !== "string") node[key] = value;
        else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

const ICONS = {
    "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    table: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
    view: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
    upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
    lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    x: '<path d="M18 6L6 18M6 6l12 12"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 19c-6.5 0-10-7-10-7a18.5 18.5 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c6.5 0 10 7 10 7a18.5 18.5 0 0 1-2.16 3.19"/><path d="M1 1l22 22"/>',
    code: '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    chart: '<path d="M3 3v18h18"/><path d="M8 17V9M13 17V5M18 17v-6"/>',
    help: '<circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
    retry: '<path d="M3 12a9 9 0 1 0 2.64-6.36L3 8"/><path d="M3 3v5h5"/>',
};

function icon(name) {
    const span = document.createElement("span");
    span.className = "icon";
    span.setAttribute("aria-hidden", "true");
    span.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[name] || ""}</svg>`;
    return span;
}

function hydrateIcons(root = document) {
    for (const slot of $$("[data-icon]", root)) {
        slot.replaceWith(icon(slot.dataset.icon));
    }
}

function setIcon(button, name) {
    const old = $(".icon", button);
    if (old) old.replaceWith(icon(name));
}

const number = new Intl.NumberFormat();

function plural(count, word) {
    return `${number.format(count)} ${word}${count === 1 ? "" : "s"}`;
}

function toast(message, kind = "ok") {
    const node = h("div", { class: `toast toast-${kind}` },
        icon(kind === "error" ? "alert" : "check"), h("span", { text: message }));
    $("[data-toasts]").append(node);
    setTimeout(() => {
        node.classList.add("leaving");
        setTimeout(() => node.remove(), 220);
    }, kind === "error" ? 6000 : 3200);
}

function showError(box, message, detail) {
    if (!box) return;
    box.replaceChildren(h("span", { text: message }));
    if (detail) box.append(h("details", { class: "small" }, h("summary", { text: "Technical detail" }), h("pre", { class: "pre", text: detail })));
    box.hidden = false;
}

function clearError(box) {
    if (box) { box.hidden = true; box.replaceChildren(); }
}

function busyButton(button, busy) {
    button.disabled = busy;
    button.classList.toggle("is-loading", busy);
}

async function copyText(text, button) {
    try {
        await navigator.clipboard.writeText(text);
        if (button) {
            setIcon(button, "check");
            setTimeout(() => setIcon(button, "copy"), 1400);
        }
    } catch {
        toast("Could not copy to the clipboard.", "error");
    }
}

function download(name, blob) {
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function confirmDialog(title, text, okLabel = "Continue", danger = false) {
    const dialog = $("#dlg-confirm");
    $("[data-confirm-title]", dialog).textContent = title;
    $("[data-confirm-text]", dialog).textContent = text;
    const ok = $("[data-confirm-ok]", dialog);
    ok.textContent = okLabel;
    ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
    dialog.returnValue = "";
    dialog.showModal();
    return new Promise((resolve) => {
        dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    });
}

// ---------- the API ----------

const TOKEN_KEY = "chinhook.session";
let token = sessionStorage.getItem(TOKEN_KEY);

class ApiError extends Error {
    constructor(message, { status = 0, code = "", detail = "" } = {}) {
        super(message);
        this.status = status;
        this.code = code;
        this.detail = detail;
    }
}

async function ensureSession() {
    if (token) return;
    const response = await fetch("/api/session", { method: "POST" });
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new ApiError(data.error || "The server could not be reached.", { status: response.status, code: data.code, detail: data.detail });
    }
    token = (await response.json()).token;
    sessionStorage.setItem(TOKEN_KEY, token);
}

async function api(path, { method = "GET", body, raw = false } = {}) {
    await ensureSession();
    let response;
    try {
        response = await fetch(path, {
            method,
            headers: { "X-Session": token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    } catch {
        throw new ApiError("The server could not be reached. Check your connection and try again.");
    }
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const error = new ApiError(data.error || "Something went wrong. Try again.", { status: response.status, code: data.code, detail: data.detail });
        if (error.code === "session") {
            // The server forgot this session (it was idle too long, or the server restarted):
            // start a fresh one and go back to wherever that leaves us.
            token = null;
            sessionStorage.removeItem(TOKEN_KEY);
            toast("Your session ended, so the connection was forgotten. Connect again to continue.", "error");
            boot();
            error.handled = true;
        } else if (error.code === "phase") {
            boot();
            error.handled = true;
        }
        throw error;
    }
    return raw ? response : response.json();
}

async function stream(path, body, onEvent) {
    const response = await api(path, { method: "POST", body, raw: true });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, end).trim();
            buffer = buffer.slice(end + 1);
            if (line) onEvent(JSON.parse(line));
        }
    }
}

// ---------- views ----------

let state = null;

function show(view) {
    $("#boot").hidden = true;
    for (const node of $$(".view")) node.hidden = node.id !== `view-${view}`;
}

async function boot() {
    try {
        state = await api("/api/state");
    } catch (error) {
        if (error.handled) return;
        $("#fatal-message").textContent = error.message;
        $("#fatal-detail").textContent = error.detail || "";
        $("#fatal-detail").closest("details").hidden = !error.detail;
        show("fatal");
        return;
    }
    if (state.phase === "auth") return showAuth();
    if (state.phase === "setup") return showSetup();
    if (state.phase === "prepare") return showPrepare();
    return showApp();
}

// ---------- sign in ----------

function showAuth() {
    show("auth");
    const form = $("#auth-form");
    $("input", form).focus();
}

$("#auth-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type=submit]", form);
    clearError($("[data-error]", form));
    busyButton(button, true);
    try {
        await api("/api/auth", { method: "POST", body: { passphrase: form.passphrase.value } });
        form.reset();
        await boot();
    } catch (error) {
        if (!error.handled) showError($("[data-error]", form), error.message);
    } finally {
        busyButton(button, false);
    }
});

// ---------- setup ----------

const setupForm = $("#setup-form");
let setupReady = false;

function segmentedValue(name) {
    return $(`[data-segmented="${name}"] [aria-checked="true"]`, setupForm)?.dataset.value;
}

function setSegmented(name, value) {
    for (const button of $$(`[data-segmented="${name}"] button`, setupForm)) {
        button.setAttribute("aria-checked", String(button.dataset.value === value));
    }
    applySetupVisibility();
}

const SSL_HINTS = {
    prefer: "Encrypt when the server supports it.",
    require: "Always encrypt; refuse an unencrypted connection.",
    disable: "Never encrypt. Only for a database on a private network.",
};

function applySetupVisibility() {
    const dialect = segmentedValue("dialect");
    const mode = segmentedValue("mode");
    for (const node of $$("[data-when-mode]", setupForm)) node.hidden = node.dataset.whenMode !== mode;
    for (const node of $$("[data-when-dialect]", setupForm)) node.hidden = node.dataset.whenDialect !== dialect;
    const postgres = dialect === "postgresql";
    $("[data-placeholder-url]", setupForm).placeholder = postgres
        ? "postgresql://user:password@host:5432/database"
        : "mysql://user:password@host:3306/database";
    $("[data-placeholder-port]", setupForm).placeholder = postgres ? "5432" : "3306";
    if (state?.setup) $("[data-readonly-sql]", setupForm).textContent = state.setup.read_only_sql[dialect];
    $("[data-ssl-hint]", setupForm).textContent = SSL_HINTS[setupForm.ssl_mode.value] || "";
}

function initSetup() {
    if (setupReady) return;
    setupReady = true;
    const { providers, ssl_modes } = state.setup;
    for (const name of Object.keys(providers)) setupForm.provider.append(h("option", { value: name, text: name }));
    for (const mode of ssl_modes) setupForm.ssl_mode.append(h("option", { value: mode, text: mode }));
    setupForm.provider.value = "OpenAI";
    setupForm.base_url.value = providers.OpenAI;
    setSegmented("dialect", "postgresql");
    setSegmented("mode", "url");

    for (const group of $$("[data-segmented]", setupForm)) {
        group.addEventListener("click", (event) => {
            const button = event.target.closest("button");
            if (button) setSegmented(group.dataset.segmented, button.dataset.value);
        });
    }
    setupForm.provider.addEventListener("change", () => {
        setupForm.base_url.value = state.setup.providers[setupForm.provider.value] ?? "";
        setupForm.base_url.focus();
    });
    setupForm.ssl_mode.addEventListener("change", applySetupVisibility);
    setupForm.addEventListener("submit", (event) => {
        event.preventDefault();
        connect(readSetupForm());
    });
}

function readSetupForm() {
    const form = setupForm;
    return {
        dialect: segmentedValue("dialect"),
        mode: segmentedValue("mode"),
        url: form.url.value,
        host: form.host.value,
        port: form.port.value,
        database: form.database.value,
        username: form.username.value,
        password: form.password.value,
        schema: form.schema.value,
        ssl_mode: form.ssl_mode.value,
        base_url: form.base_url.value,
        api_key: form.api_key.value,
        model: form.model.value,
        fast_model: form.fast_model.value,
    };
}

function fillSetupForm(values) {
    const form = setupForm;
    setSegmented("dialect", values.dialect);
    setSegmented("mode", "url");
    form.url.value = values.url;
    if (values.dialect === "postgresql") form.schema.value = values.schema;
    form.ssl_mode.value = values.ssl_mode;
    if ([...form.provider.options].some((option) => option.value === values.provider)) form.provider.value = values.provider;
    form.base_url.value = values.base_url;
    form.model.value = values.model;
    form.fast_model.value = values.fast_model || "";
    if (values.api_key) form.api_key.value = values.api_key;
    applySetupVisibility();
}

function showSetup() {
    show("setup");
    initSetup();
    applySetupVisibility();
}

async function connect(values) {
    const button = $("[data-connect]", setupForm);
    const steps = $("[data-steps]", setupForm);
    const errorBox = $("[data-error]", setupForm);
    clearError(errorBox);
    steps.replaceChildren();
    steps.hidden = true;
    busyButton(button, true);
    let connected = false;
    try {
        await stream("/api/connect", values, (event) => {
            if (event.type === "step") {
                steps.hidden = false;
                let row = $(`[data-step="${event.id}"]`, steps);
                if (!row) {
                    row = h("li", { "data-step": event.id }, h("span", { class: "state" }), h("span", { class: "text" }));
                    steps.append(row);
                }
                row.className = event.state;
                if (event.text) $(".text", row).textContent = event.text;
                const mark = $(".state", row);
                mark.replaceChildren(event.state === "done" ? icon("check") : event.state === "failed" ? icon("x") : "");
            } else if (event.type === "error") {
                showError(errorBox, event.message, event.detail);
            } else if (event.type === "done") {
                connected = true;
            }
        });
    } catch (error) {
        if (!error.handled) showError(errorBox, error.message, error.detail);
    } finally {
        busyButton(button, false);
    }
    if (connected) {
        // The secrets are on the server now; nothing about them needs to stay in the page.
        for (const name of ["url", "password", "api_key"]) setupForm[name].value = "";
        steps.hidden = true;
        await boot();
    } else if (!errorBox.hidden) {
        errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
    }
}

// Show and hide for every password field on the page.
document.addEventListener("click", (event) => {
    const reveal = event.target.closest(".reveal");
    if (!reveal) return;
    const input = $("input", reveal.parentElement);
    const hidden = input.type === "password";
    input.type = hidden ? "text" : "password";
    reveal.setAttribute("aria-label", hidden ? "Hide" : "Show");
    setIcon(reveal, hidden ? "eye-off" : "eye");
});

document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy-from]");
    if (button) copyText($(button.dataset.copyFrom).textContent, button);
});

// ---------- importing a configuration file ----------

const importDialog = $("#dlg-import");
const importForm = $("[data-import-form]", importDialog);
let importContent = "";
let importEncrypted = false;

function resetImport() {
    importContent = "";
    importForm.reset();
    $("[data-import-name]", importForm).textContent = "Choose a file";
    $("[data-import-unlock]", importForm).hidden = true;
    clearError($("[data-error]", importForm));
    for (const button of $$("[data-import-fill], [data-import-connect]", importForm)) button.disabled = true;
}

$("#open-import").addEventListener("click", () => {
    resetImport();
    importDialog.showModal();
});

async function readImportFile(file) {
    const errorBox = $("[data-error]", importForm);
    clearError(errorBox);
    $("[data-import-unlock]", importForm).hidden = true;
    if (!file) return;
    $("[data-import-name]", importForm).textContent = file.name;
    if (file.size > 1_000_000) return showError(errorBox, "That file is too large to be a configuration file.");
    try {
        importContent = await file.text();
        const info = await api("/api/config/inspect", { method: "POST", body: { content: importContent } });
        importEncrypted = info.encrypted;
        $("[data-import-label]", importForm).textContent = info.encrypted
            ? `This file says it is for ${info.label}. Its password and API key are encrypted.`
            : `For ${info.label}. The file holds no password or API key, so enter them here.`;
        $("[data-import-encrypted]", importForm).hidden = !info.encrypted;
        $("[data-import-plain]", importForm).hidden = info.encrypted;
        $("[data-import-unlock]", importForm).hidden = false;
        for (const button of $$("[data-import-fill], [data-import-connect]", importForm)) button.disabled = false;
        (info.encrypted ? importForm.passphrase : importForm.password).focus();
    } catch (error) {
        importContent = "";
        if (!error.handled) showError(errorBox, error.message);
    }
}

$("[data-import-file]", importForm).addEventListener("change", (event) => readImportFile(event.target.files[0]));

const dropzone = $(".dropzone", importForm);
dropzone.addEventListener("dragover", (event) => { event.preventDefault(); dropzone.classList.add("is-over"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-over"));
dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-over");
    readImportFile(event.dataTransfer.files[0]);
});

async function loadImport(andConnect) {
    const errorBox = $("[data-error]", importForm);
    clearError(errorBox);
    const button = $(andConnect ? "[data-import-connect]" : "[data-import-fill]", importForm);
    busyButton(button, true);
    const body = {
        content: importContent,
        passphrase: importForm.passphrase.value,
        password: importForm.password.value,
        api_key: importForm.api_key.value,
    };
    // Typed only to open this one file, and cleared as soon as the attempt is over.
    for (const name of ["passphrase", "password", "api_key"]) importForm[name].value = "";
    try {
        const values = await api("/api/config/load", { method: "POST", body });
        fillSetupForm(values);
        importDialog.close();
        if (andConnect) connect(readSetupForm());
        else toast(`Loaded ${values.summary}. Check the form, then press Connect.`);
    } catch (error) {
        if (!error.handled) showError(errorBox, error.message);
        (importEncrypted ? importForm.passphrase : importForm.password).focus();
    } finally {
        busyButton(button, false);
    }
}

$("[data-import-fill]", importForm).addEventListener("click", () => loadImport(false));
// Enter in a field means "load and connect"; only the close button closes the dialog.
importForm.addEventListener("submit", (event) => {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    if (importContent) loadImport(true);
});
$("[data-import-connect]", importForm).addEventListener("click", () => loadImport(true));

// ---------- preparing the index ----------

const prepareView = $("#view-prepare");

async function showPrepare() {
    show("prepare");
    const connection = state.connection;
    $("[data-prepare-title]", prepareView).textContent = `Preparing ${connection.database || "your database"}`;
    $("[data-prepare-progress]", prepareView).hidden = false;
    $("[data-prepare-error]", prepareView).hidden = true;
    const bar = $("[data-bar]", prepareView);
    const message = $("[data-message]", prepareView);
    const count = $("[data-count]", prepareView);
    bar.classList.add("indeterminate");
    bar.style.width = "";
    message.textContent = "Checking the index";
    count.textContent = "";

    let failed = null;
    let done = false;
    try {
        await stream("/api/prepare", {}, (event) => {
            if (event.type === "progress") {
                const total = Math.max(event.total, 1);
                bar.classList.remove("indeterminate");
                bar.style.width = `${Math.min(100, (event.done / total) * 100)}%`;
                message.textContent = event.message;
                count.textContent = event.total > 1 ? `${event.done} of ${event.total}` : "";
            } else if (event.type === "error") {
                failed = event;
            } else if (event.type === "done") {
                done = true;
            }
        });
    } catch (error) {
        if (error.handled) return;
        if (error.status === 409) {
            // Indexing is already running for this session (the page was reloaded part-way);
            // look again shortly rather than starting a second run.
            setTimeout(boot, 2000);
            return;
        }
        failed = { message: error.message, detail: error.detail };
    }
    if (done) {
        bar.style.width = "100%";
        await boot();
        return;
    }
    if (failed) {
        $("[data-prepare-progress]", prepareView).hidden = true;
        $("[data-error-message]", prepareView).textContent = failed.message;
        $("[data-error-detail]", prepareView).textContent = failed.detail || "";
        $("[data-error-detail]", prepareView).closest("details").hidden = !failed.detail;
        $("[data-prepare-error]", prepareView).hidden = false;
    }
}

$("[data-retry]", prepareView).addEventListener("click", () => showPrepare());
$("[data-change]", prepareView).addEventListener("click", async () => {
    await api("/api/disconnect", { method: "POST" }).catch(() => {});
    boot();
});

// ---------- the app: frame, sidebar, routing ----------

const appView = $("#view-app");
const messagesBox = $("[data-messages]", appView);
const chatScroll = $("[data-chat-scroll]", appView);
const composer = $("[data-composer]", appView);
const questionBox = composer.question;
let answering = false;
let schemaLoaded = false;

function showApp() {
    show("app");
    schemaLoaded = false;
    renderConnection();
    loadStatus();
    renderChat();
    route();
    if (state.busy) waitForAnswer();
}

function setSidebar(open) {
    appView.classList.toggle("sidebar-open", open);
    $("[data-scrim]", appView).hidden = !open;
}

$("[data-open-sidebar]", appView).addEventListener("click", () => setSidebar(true));
$("[data-close-sidebar]", appView).addEventListener("click", () => setSidebar(false));
$("[data-scrim]", appView).addEventListener("click", () => setSidebar(false));
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setSidebar(false);
});

function route() {
    if (state?.phase !== "chat") return;
    const page = location.hash === "#/schema" ? "schema" : "chat";
    for (const node of $$("[data-page]", appView)) node.hidden = node.dataset.page !== page;
    for (const link of $$("[data-route]", appView)) {
        if (link.dataset.route === page) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
    }
    setSidebar(false);
    if (page === "schema") loadSchema();
    else questionBox.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);

function renderConnection(status) {
    const connection = state.connection;
    const card = $("[data-conn]", appView);
    let statusLine;
    if (!status) statusLine = h("div", { class: "conn-status" }, h("span", { class: "dot" }), "Checking…");
    else if (status.unavailable) statusLine = h("div", { class: "conn-status off" }, h("span", { class: "dot" }), "Index status unavailable");
    else if (status.stale) statusLine = h("div", { class: "conn-status warn" }, h("span", { class: "dot" }), "Schema changed");
    else statusLine = h("div", { class: "conn-status ok" }, h("span", { class: "dot" }), "Connected");

    const where = [connection.dialect_name, connection.host, connection.schema && `schema ${connection.schema}`].filter(Boolean).join(" · ");
    const rows = [["Model", connection.model]];
    if (connection.fast_model) rows.push(["Fast model", connection.fast_model]);
    if (status && !status.unavailable) rows.push(["Indexed", `${status.indexed} of ${plural(status.tables, "table")}`]);

    card.replaceChildren(
        statusLine,
        h("div", { class: "conn-name", text: connection.database }),
        h("div", { class: "conn-sub", text: where }),
        h("div", { class: "conn-rows" }, rows.map(([label, value]) => h("div", { class: "conn-row" }, h("span", { text: label }), h("span", { text: value })))),
    );
    if (status?.stale) card.append(h("div", { class: "conn-hint", text: "Refresh the index to pick up the changes." }));
}

async function loadStatus() {
    try {
        renderConnection(await api("/api/status"));
    } catch (error) {
        if (!error.handled) renderConnection({ unavailable: true });
    }
}

$("[data-refresh-index]", appView).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.classList.add("is-loading");
    try {
        const { count } = await api("/api/index/refresh", { method: "POST" });
        toast(count ? `Re-indexed ${plural(count, "table")}.` : "The index is already up to date.");
        schemaLoaded = false;
        await loadStatus();
    } catch (error) {
        if (!error.handled) toast(error.message, "error");
    } finally {
        button.disabled = false;
        button.classList.remove("is-loading");
    }
});

$("[data-disconnect]", appView).addEventListener("click", async () => {
    const ok = await confirmDialog(
        "Change connection?",
        `This disconnects from ${state.connection.database} and clears this chat. The search index is kept, so reconnecting later is quick.`,
        "Disconnect",
    );
    if (!ok) return;
    try {
        await api("/api/disconnect", { method: "POST" });
        location.hash = "";
        await boot();
    } catch (error) {
        if (!error.handled) toast(error.message, "error");
    }
});

for (const button of $$("[data-new-chat]", appView)) {
    button.addEventListener("click", async () => {
        if (answering) return toast("Wait for the current answer to finish first.", "error");
        if (state.messages.length) {
            const ok = await confirmDialog("Start a new chat?", "The current conversation will be cleared. Nothing of it is kept.", "New chat");
            if (!ok) return;
            try {
                await api("/api/chat/new", { method: "POST" });
            } catch (error) {
                if (!error.handled) toast(error.message, "error");
                return;
            }
        }
        state.messages = [];
        renderChat();
        if (location.hash === "#/schema") location.hash = "";
        else route();
    });
}

// ---------- export and data dialogs ----------

const exportDialog = $("#dlg-export");
const exportForm = $("[data-export-form]", exportDialog);

function applyExportMode() {
    const include = exportForm.include_secrets.checked;
    $("[data-export-secret]", exportForm).hidden = !include;
    $("[data-export-plain]", exportForm).hidden = include;
    clearError($("[data-error]", exportForm));
}

$("[data-open-export]", appView).addEventListener("click", () => {
    exportForm.reset();
    $("[data-export-intro]", exportForm).textContent =
        `Save the connection to ${state.connection.describe} and ${state.connection.model} to a file. Next time, import it on the setup screen instead of filling in the form again.`;
    $("[data-passphrase-hint]", exportForm).textContent =
        `At least ${state.setup.min_passphrase} characters. You need it to import the file, and it cannot be recovered if it is forgotten.`;
    applyExportMode();
    setSidebar(false);
    exportDialog.showModal();
});

exportForm.include_secrets.addEventListener("change", applyExportMode);
exportForm.addEventListener("submit", (event) => {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    $("[data-export-create]", exportForm).click();
});

$("[data-export-create]", exportForm).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const errorBox = $("[data-error]", exportForm);
    clearError(errorBox);
    busyButton(button, true);
    const body = {
        include_secrets: exportForm.include_secrets.checked,
        passphrase: exportForm.passphrase.value,
        passphrase_again: exportForm.passphrase_again.value,
    };
    exportForm.passphrase.value = "";
    exportForm.passphrase_again.value = "";
    try {
        const response = await api("/api/export", { method: "POST", body, raw: true });
        const disposition = response.headers.get("Content-Disposition") || "";
        const name = /filename="([^"]+)"/.exec(disposition)?.[1] || "chinhook-settings.json";
        download(name, await response.blob());
        exportDialog.close();
        toast(`Saved ${name}.`);
    } catch (error) {
        if (!error.handled) showError(errorBox, error.message);
    } finally {
        busyButton(button, false);
    }
});

const dataDialog = $("#dlg-data");

$("[data-open-data]", appView).addEventListener("click", () => {
    const days = state.setup.retention_days;
    $("[data-retention]", dataDialog).textContent =
        "Table definitions, a one-line description of each table, and common values such as countries or categories are kept on this server so questions can find the right tables" +
        (days ? `, and deleted after ${plural(days, "day")} unused.` : ".");
    clearError($("[data-error]", dataDialog));
    setSidebar(false);
    dataDialog.showModal();
});

$("[data-delete-index]", dataDialog).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    dataDialog.close();
    const ok = await confirmDialog(
        "Delete this database's index?",
        "Everything this server holds about the database is removed and you are disconnected. Connecting again rebuilds it.",
        "Delete index",
        true,
    );
    if (!ok) return;
    busyButton(button, true);
    try {
        await api("/api/index/delete", { method: "POST" });
        toast("The index was deleted.");
        location.hash = "";
        await boot();
    } catch (error) {
        if (!error.handled) toast(error.message, "error");
    } finally {
        busyButton(button, false);
    }
});

// ---------- chat ----------

const SUGGESTION_ICONS = ["table", "chart", "help", "file"];

function renderChat() {
    $("[data-db-name]", appView).textContent = state.connection.database;
    const suggestions = $("[data-suggestions]", appView);
    suggestions.replaceChildren(...state.examples.map((text, position) =>
        h("button", { type: "button", class: "suggestion", onclick: () => ask(text) },
            icon(SUGGESTION_ICONS[position % SUGGESTION_ICONS.length]), h("span", { text }))));
    messagesBox.replaceChildren(...state.messages.map(messageNode));
    $("[data-empty]", appView).hidden = state.messages.length > 0;
    scrollToEnd(false);
}

function nearBottom() {
    return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 140;
}

function scrollToEnd(smooth = true) {
    chatScroll.scrollTo({ top: chatScroll.scrollHeight, behavior: smooth ? "smooth" : "instant" });
}

function userNode(text) {
    return h("div", { class: "msg msg-user" }, h("div", { class: "bubble", text }));
}

function botShell(...children) {
    return h("div", { class: "msg msg-bot" },
        h("img", { class: "avatar", src: "/static/icon.svg", alt: "" }),
        h("div", { class: "msg-body" }, ...children));
}

function answerNode(html, streaming = false) {
    const node = h("div", { class: `answer${streaming ? " streaming" : ""}` });
    node.innerHTML = html; // Sanitised on the server against an allowlist; see render.py.
    for (const table of $$("table", node)) {
        const wrap = h("div", { class: "table-wrap" });
        table.replaceWith(wrap);
        wrap.append(table);
    }
    return node;
}

function messageNode(message) {
    if (message.role === "user") return userNode(message.text);
    if (message.failed) return failureNode(message);

    const body = [answerNode(message.html)];
    if (message.clarification) body.push(choicesNode(message));
    body.push(metaNode(message));

    const actions = h("div", { class: "msg-actions" });
    const copy = h("button", { type: "button", class: "btn btn-ghost", onclick: (event) => copyText(message.text, event.currentTarget) }, icon("copy"), "Copy");
    actions.append(copy);
    let panel = null;
    if (message.details) {
        const toggle = h("button", { type: "button", class: "btn btn-ghost", "aria-expanded": "false" }, icon("code"), message.details.result ? "SQL and results" : "SQL and steps");
        toggle.addEventListener("click", () => {
            const open = toggle.getAttribute("aria-expanded") !== "true";
            toggle.setAttribute("aria-expanded", String(open));
            if (!panel) {
                panel = detailsNode(message);
                actions.after(panel);
            }
            panel.hidden = !open;
            // After the first tab has drawn, so the whole panel is measured.
            if (open) requestAnimationFrame(() => panel.scrollIntoView({ behavior: "smooth", block: "nearest" }));
        });
        actions.prepend(toggle);
    }
    body.push(actions);
    return botShell(...body);
}

function metaNode(message) {
    const parts = [];
    if (message.model) parts.push(message.model);
    const total = message.timings?.total;
    if (total) parts.push(`${total.toFixed(1)}s`);
    const tasks = (message.details?.tasks || []).filter((task) => task.sql);
    if (tasks.length) {
        const rows = tasks.reduce((sum, task) => sum + (task.row_count || 0), 0);
        const more = tasks.some((task) => task.truncated) ? "+" : "";
        parts.push(`${number.format(rows)}${more} ${rows === 1 ? "row" : "rows"}`);
        if (tasks.length > 1) parts.push(`${tasks.length} queries`);
        const verdicts = new Set(tasks.map((task) => task.verification));
        // Only what the workflow established: "Checked" when every query passed verification.
        if (verdicts.size === 1 && verdicts.has("pass")) parts.push(h("span", { class: "badge badge-ok" }, icon("check"), "Checked"));
        else if (verdicts.has("fail")) parts.push(h("span", { class: "badge badge-warn", text: "Not verified" }));
    }
    const node = h("div", { class: "meta" });
    parts.forEach((part, position) => {
        if (position) node.append(h("span", { class: "sep", text: "·" }));
        node.append(part);
    });
    return node;
}

function failureNode(message) {
    const previous = state.messages.find((other) => other.index === message.index - 1 && other.role === "user");
    return botShell(h("div", { class: "failure" },
        h("div", { class: "alert alert-error" },
            h("strong", { text: "That question could not be answered." }),
            h("span", { text: "Something went wrong before there was a result to show. Asking again usually works." })),
        message.error ? h("details", { class: "disclosure" }, h("summary", { text: "Technical detail" }), h("pre", { class: "pre", text: message.error })) : null,
        previous ? h("div", { class: "msg-actions" }, h("button", { type: "button", class: "btn btn-ghost", onclick: () => ask(previous.text) }, icon("retry"), "Ask again")) : null,
    ));
}

function choicesNode(message) {
    const { options, multi } = message.clarification;
    const chosen = new Set();
    const send = h("button", { type: "button", class: "btn btn-primary", disabled: true }, "Send");
    const chips = options.map((option) => {
        const chip = h("button", { type: "button", class: "chip", "aria-pressed": "false", text: option });
        chip.addEventListener("click", () => {
            if (!multi) {
                chosen.clear();
                for (const other of chips) other.setAttribute("aria-pressed", "false");
            }
            if (chosen.has(option)) chosen.delete(option);
            else chosen.add(option);
            chip.setAttribute("aria-pressed", String(chosen.has(option)));
            send.disabled = chosen.size === 0;
        });
        return chip;
    });
    const node = h("div", { class: "choices" },
        h("div", { class: "choices-label", text: multi ? "Pick one or more" : "Pick one" }),
        h("div", { class: "chips" }, chips),
        send);
    send.addEventListener("click", () => {
        // In the order they were offered, not the order they were clicked.
        const selection = options.filter((option) => chosen.has(option)).join(", ");
        if (selection) ask(selection, message.index).then((sent) => { if (sent) node.remove(); });
    });
    return node;
}

// ---------- answer details ----------

function detailsNode(message) {
    const details = message.details;
    const tabs = [];
    if (details.result) tabs.push(["Results", () => resultPanel(details.result)]);
    if (details.sql) tabs.push(["SQL", () => sqlPanel(details)]);
    tabs.push(["Steps", () => stepsPanel(message)]);

    const bar = h("div", { class: "tabs", role: "tablist" });
    const body = h("div");
    const buttons = tabs.map(([label, build], position) => {
        let built = null;
        const tab = h("button", { type: "button", class: "tab", role: "tab", text: label });
        tab.addEventListener("click", () => {
            for (const other of buttons) other.setAttribute("aria-selected", String(other === tab));
            built = built || build();
            body.replaceChildren(built);
        });
        if (position === 0) queueMicrotask(() => tab.click());
        return tab;
    });
    bar.append(...buttons);
    return h("div", { class: "details" }, bar, body);
}

function isNumeric(value) {
    return typeof value === "number" || (typeof value === "string" && value.trim() !== "" && /^-?\d+(\.\d+)?$/.test(value.trim()));
}

function dataTable(columns, rows) {
    const numeric = columns.map((_, position) => rows.length > 0 && rows.every((row) => row[position] === null || isNumeric(row[position])));
    return h("div", { class: "grid-table" }, h("table", {},
        h("thead", {}, h("tr", {}, columns.map((name, position) => h("th", { class: numeric[position] ? "num" : null, text: name })))),
        h("tbody", {}, rows.map((row) => h("tr", {}, row.map((value, position) => {
            if (value === null) return h("td", { class: "null", text: "null" });
            const text = typeof value === "number" ? number.format(value) : String(value);
            return h("td", { class: numeric[position] ? "num" : null, text, title: text.length > 40 ? text : null });
        }))))));
}

function csv(columns, rows) {
    const cell = (value) => {
        if (value === null || value === undefined) return "";
        let text = String(value);
        // A cell starting with one of these is run as a formula by spreadsheet programs.
        if (/^[=+\-@\t\r]/.test(text) && !isNumeric(text)) text = `'${text}`;
        return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    return [columns, ...rows].map((row) => row.map(cell).join(",")).join("\r\n");
}

function chartNode(chart) {
    const max = Math.max(...chart.points.map(([, value]) => Math.abs(value)), 0) || 1;
    return h("div", {},
        h("div", { class: "panel-row" },
            h("span", { class: "panel-label", text: `${chart.value} by ${chart.label}` })),
        h("div", { class: "bars" }, chart.points.map(([label, value, shown]) => {
            const fill = h("div", { class: "bar-fill" });
            fill.style.width = `${(Math.abs(value) / max) * 100}%`;
            return h("div", { class: "bar-row" },
                h("span", { class: "bar-label", text: label, title: label }),
                h("div", { class: "bar-track" }, fill),
                h("span", { class: "bar-value", text: shown ?? number.format(value) }));
        })));
}

function resultPanel(result) {
    const shown = result.rows.length;
    const note = result.total > shown ? `Showing ${number.format(shown)} of ${number.format(result.total)} rows` : plural(result.total, "row");
    const save = h("button", { type: "button", class: "btn btn-secondary", onclick: () => download("chinhook-result.csv", new Blob(["﻿" + csv(result.columns, result.rows)], { type: "text/csv;charset=utf-8" })) }, icon("download"), "Download CSV");
    return h("div", { class: "tab-panel" },
        h("div", { class: "panel-row" }, h("span", { class: "panel-note", text: note }), save),
        dataTable(result.columns, result.rows),
        result.chart ? chartNode(result.chart) : null);
}

function codeBlock(text) {
    const copy = h("button", { type: "button", class: "icon-btn copy", "aria-label": "Copy", onclick: (event) => copyText(text, event.currentTarget) }, icon("copy"));
    return h("div", { class: "code-block" }, h("pre", {}, h("code", { text })), copy);
}

function sqlPanel(details) {
    return h("div", { class: "tab-panel" },
        codeBlock(details.sql),
        details.tables.length ? h("div", {},
            h("div", { class: "panel-label", text: "Tables used" }),
            h("div", { class: "tags" }, details.tables.map((name) => h("span", { class: "tag", text: name })))) : null);
}

function stepsNode(pipeline, { active = null, completed = [], timings = {} } = {}) {
    const node = h("div", { class: "steps" });
    pipeline.forEach((step, position) => {
        const state = step.key === active ? "active" : completed.includes(step.key) ? "done" : "idle";
        if (position) node.append(h("span", { class: `step-wire${state !== "idle" ? " done" : ""}` }));
        const seconds = timings[step.key];
        node.append(h("span", { class: `step-node ${state}`, title: step.detail },
            h("span", { class: "step-dot" }, state === "done" ? icon("check") : String(position + 1)),
            h("span", { class: "stage-title", text: step.title }),
            seconds ? h("span", { class: "step-time", text: `${seconds.toFixed(1)}s` }) : null));
    });
    return node;
}

function stepsPanel(message) {
    const details = message.details;
    const timings = message.timings || {};
    const usage = details.usage || {};
    const panel = h("div", { class: "tab-panel" },
        // Driven by which stages actually recorded a timing, not by every stage that exists.
        stepsNode(state.pipeline, { completed: Object.keys(timings), timings }));

    for (const task of details.tasks) {
        const badges = h("span", { class: "badges" });
        if (task.verification === "pass") badges.append(h("span", { class: "badge badge-ok", text: "Checked" }));
        else if (task.verification === "fail") badges.append(h("span", { class: "badge badge-warn", text: "Not verified" }));
        badges.append(h("span", { class: "badge", text: `${number.format(task.row_count || 0)}${task.truncated ? "+" : ""} rows` }));
        const tables = (task.schema_tables || []).join(", ") || "no tables";
        panel.append(h("div", { class: "task" },
            h("div", { class: "task-head" }, h("span", { text: task.question || task.task_id }), badges),
            h("div", { class: "task-line", text: `${task.intent || "query"} · ${task.status || ""}` }),
            h("div", { class: "task-line", text: `Tables: ${tables}${task.schema_from_cache ? ", reused from earlier in this chat" : ""}` }),
            task.semantic ? h("div", { class: "task-line", text: `Grounding: ${task.semantic}` }) : null,
            task.failure_reason ? h("div", { class: "task-line", text: `Failed: ${task.failure_reason}` }) : null,
            details.tasks.length > 1 && task.sql ? codeBlock(task.sql) : null));
    }

    if (usage.total_tokens) {
        panel.append(h("div", { class: "panel-note", text:
            `${message.model} · ${number.format(usage.input_tokens || 0)} input tokens · ${number.format(usage.output_tokens || 0)} output tokens · ${plural(usage.llm_calls || 0, "model call")}` }));
    }
    if (details.schema) {
        panel.append(h("details", { class: "disclosure" },
            h("summary", { text: "Schema that was searched" }),
            h("pre", { class: "pre", text: details.schema })));
    }
    return panel;
}

// ---------- asking ----------

function autosize() {
    questionBox.style.height = "auto";
    questionBox.style.height = `${Math.min(questionBox.scrollHeight, 200)}px`;
    $(".send", composer).disabled = answering || !questionBox.value.trim();
}

questionBox.addEventListener("input", autosize);
questionBox.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        composer.requestSubmit();
    }
});

composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = questionBox.value.trim();
    if (!question || answering) return;
    ask(question);
});

function setAnswering(value) {
    answering = value;
    questionBox.placeholder = value ? "Working on the answer…" : "Ask a question about your data";
    autosize();
}

// Asks one question and shows the answer arriving. Resolves to whether it was accepted.
async function ask(question, answers = null) {
    if (answering) return false;
    if (location.hash === "#/schema") location.hash = "";
    setAnswering(true);
    const typed = questionBox.value;
    questionBox.value = "";
    autosize();
    $("[data-empty]", appView).hidden = true;

    const user = userNode(question);
    const completed = ["question"];
    let active = null;
    const steps = h("div");
    const label = h("span", { text: "Reading the question" });
    const working = h("div", { class: "working" }, steps, h("div", { class: "thinking" }, h("span", { class: "dots" }, h("span"), h("span"), h("span")), label));
    const body = h("div");
    body.append(working);
    const pending = botShell(body);
    const drawSteps = () => steps.replaceChildren(stepsNode(state.pipeline, { active, completed }));
    drawSteps();
    messagesBox.append(user, pending);
    scrollToEnd();

    let final = null;
    let failure = null;
    try {
        await stream("/api/ask", { question, answers }, (event) => {
            const follow = nearBottom();
            if (event.type === "stage") {
                if (event.key) {
                    if (active && !completed.includes(active)) completed.push(active);
                    active = event.key;
                    drawSteps();
                }
                label.textContent = event.label;
            } else if (event.type === "partial") {
                body.replaceChildren(answerNode(event.html, true));
            } else if (event.type === "message") {
                final = event.message;
            } else if (event.type === "error") {
                failure = event;
            }
            if (follow) scrollToEnd(false);
        });
    } catch (error) {
        user.remove();
        pending.remove();
        setAnswering(false);
        if (!state.messages.length) $("[data-empty]", appView).hidden = false;
        if (!error.handled) {
            questionBox.value = typed || question;
            autosize();
            toast(error.message, "error");
        }
        return false;
    }

    // What the server now holds is the source of truth; drawing from it keeps the page and
    // the conversation the same, clarifying choices included.
    setAnswering(false);
    try {
        state = await api("/api/state");
    } catch (error) {
        if (!error.handled) toast(error.message, "error");
        return true;
    }
    if (final || failure) {
        renderChat();
        scrollToEnd(false);
    }
    questionBox.focus({ preventScroll: true });
    return true;
}

// After a reload in the middle of an answer: wait for it to finish, then draw it.
async function waitForAnswer() {
    setAnswering(true);
    const note = botShell(h("div", { class: "thinking" }, h("span", { class: "dots" }, h("span"), h("span"), h("span")), h("span", { text: "Finishing the previous answer" })));
    messagesBox.append(note);
    $("[data-empty]", appView).hidden = true;
    for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        try {
            state = await api("/api/state");
        } catch {
            break;
        }
        if (state.phase !== "chat" || !state.busy) break;
    }
    note.remove();
    setAnswering(false);
    if (state.phase === "chat") renderChat();
    else boot();
}

// ---------- schema ----------

async function loadSchema() {
    if (schemaLoaded) return;
    schemaLoaded = true;
    const list = $("[data-schema-list]", appView);
    const stats = $("[data-schema-stats]", appView);
    stats.replaceChildren(...[0, 1, 2].map(() => h("div", { class: "stat skeleton", style: null }, h("div", { class: "stat-value", text: " " }), h("div", { class: "stat-label", text: " " }))));
    list.replaceChildren(...[0, 1, 2, 3].map(() => h("div", { class: "table-card skeleton" }, h("div", { class: "table-name", text: " " }))));
    let tables;
    try {
        ({ tables } = await api("/api/schema"));
    } catch (error) {
        schemaLoaded = false;
        if (error.handled) return;
        stats.replaceChildren();
        list.replaceChildren(h("div", { class: "alert alert-error" }, h("strong", { text: error.message }), error.detail ? h("span", { text: error.detail }) : null));
        return;
    }
    const columns = tables.reduce((sum, table) => sum + table.columns.length, 0);
    const rows = tables.reduce((sum, table) => sum + table.rows, 0);
    stats.replaceChildren(
        ...[["Tables and views", tables.length], ["Columns", columns], ["Rows (approx.)", rows]].map(([label, value]) =>
            h("div", { class: "stat" }, h("div", { class: "stat-value", text: number.format(value) }), h("div", { class: "stat-label", text: label }))));

    const cards = tables.map((table) => {
        const card = h("details", { class: "table-card" },
            h("summary", {},
                icon(table.kind === "view" ? "view" : "table"),
                h("span", { class: "table-name", text: table.name }),
                table.kind === "view" ? h("span", { class: "badge", text: "view" }) : null,
                h("span", { class: "table-meta", text: `${plural(table.columns.length, "column")} · ~${number.format(table.rows)} rows` })));
        card.addEventListener("toggle", () => {
            if (!card.open || card.querySelector(".table-body")) return;
            card.append(h("div", { class: "table-body" },
                dataTable(["Column", "Type"], table.columns),
                h("div", {}, h("button", { type: "button", class: "btn btn-secondary", onclick: () => askAbout(table.name) }, icon("message"), "Ask about this table"))));
        });
        card.dataset.search = [table.name, ...table.columns.map(([name]) => name)].join(" ").toLowerCase();
        return card;
    });
    const empty = h("div", { class: "empty-note", text: "No tables match that filter.", hidden: true });
    list.replaceChildren(...cards, empty);
    if (!tables.length) {
        empty.textContent = "This schema has no tables or views this login can read.";
        empty.hidden = false;
    }

    const filter = $("[data-schema-filter]", appView);
    filter.value = "";
    filter.oninput = () => {
        const term = filter.value.trim().toLowerCase();
        let shown = 0;
        for (const card of cards) {
            const match = !term || card.dataset.search.includes(term);
            card.hidden = !match;
            shown += match ? 1 : 0;
        }
        empty.hidden = shown > 0 || !tables.length;
    };
}

function askAbout(name) {
    location.hash = "";
    questionBox.value = `What does the ${name} table contain, and what can I ask about it?`;
    autosize();
    questionBox.focus();
}

// ---------- start ----------

hydrateIcons();
boot();
