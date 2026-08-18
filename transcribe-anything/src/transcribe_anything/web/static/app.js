const state = { source: "file", config: null };
const form = document.querySelector("#transcription-form");
const fileInput = document.querySelector("#media-file");
const urlInput = document.querySelector("#source-url");
const submit = form.querySelector("button[type=submit]");
const statusBox = document.querySelector("#status");
const resultBox = document.querySelector("#result");
const errorBox = document.querySelector("#error");

function show(el, visible = true) { el.classList.toggle("hidden", !visible); }
function error(message) { errorBox.textContent = message; show(errorBox); }

function selectProvider(provider, resetModel = true) {
  const config = state.config;
  if (!config) return;
  const model = document.querySelector("#model");
  const options = document.querySelector("#model-options");
  options.replaceChildren();
  (config.models_by_provider[provider] || []).forEach(value => options.append(new Option(value, value)));
  if (resetModel) model.value = config.default_models[provider] || "";

  const notice = document.querySelector("#config-warning");
  if (provider === "openai" && !config.api_key_configured) {
    notice.textContent = "OPENAI_API_KEY is not configured yet. Add it to a local .env file before transcribing.";
    show(notice);
  } else if (provider === "audiocpp") {
    notice.textContent = `audio.cpp must be running at ${config.audiocpp_base_url}, and the model id must match its server configuration.`;
    show(notice);
  } else {
    show(notice, false);
  }
}

async function loadConfig() {
  try {
    const config = await fetch("/api/config").then(r => r.json());
    state.config = config;
    const providers = document.querySelector("#provider");
    config.providers.forEach(provider => providers.add(
      new Option(provider, provider, false, provider === config.default_provider)
    ));
    selectProvider(config.default_provider);
    document.querySelector("#model").value = config.default_model;
    providers.addEventListener("change", () => selectProvider(providers.value));
    const formats = document.querySelector("#formats");
    config.formats.forEach(format => {
      const label = document.createElement("label");
      label.innerHTML = `<input type="checkbox" name="formats" value="${format}" checked>${format}`;
      formats.append(label);
    });
  } catch { error("Could not load application configuration."); }
}

document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  state.source = tab.dataset.source;
  show(errorBox, false);
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
  show(document.querySelector("#file-source"), state.source === "file");
  show(document.querySelector("#url-source"), state.source === "url");
}));

fileInput.addEventListener("change", () => {
  show(errorBox, false);
  document.querySelector("#file-name").textContent = fileInput.files[0]?.name || "Audio or video";
});

urlInput.addEventListener("input", () => show(errorBox, false));

const drop = document.querySelector(".drop-zone");
["dragenter", "dragover"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.add("drag"); }));
["dragleave", "drop"].forEach(name => drop.addEventListener(name, event => { event.preventDefault(); drop.classList.remove("drag"); }));
drop.addEventListener("drop", event => {
  if (event.dataTransfer.files.length) { fileInput.files = event.dataTransfer.files; fileInput.dispatchEvent(new Event("change")); }
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  show(errorBox, false); show(resultBox, false);
  if (state.source === "file" && !fileInput.files.length) return error("Choose an audio or video file first.");
  if (state.source === "url" && !urlInput.value.trim()) return error("Paste a public media link first.");
  const selectedFormats = [...form.querySelectorAll("input[name=formats]:checked")];
  if (!selectedFormats.length) return error("Select at least one download format.");

  const data = new FormData();
  if (state.source === "file") data.append("media_file", fileInput.files[0]); else data.append("source_url", urlInput.value.trim());
  data.append("provider", document.querySelector("#provider").value);
  data.append("model", document.querySelector("#model").value);
  const language = document.querySelector("#language").value.trim();
  if (language) data.append("language", language);
  selectedFormats.forEach(input => data.append("formats", input.value));

  submit.disabled = true; show(statusBox);
  try {
    const response = await fetch("/api/transcribe", { method: "POST", body: data });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Transcription failed.");
    document.querySelector("#preview").textContent = payload.preview;
    const downloads = document.querySelector("#downloads"); downloads.replaceChildren();
    Object.entries(payload.files).forEach(([format, url]) => {
      const link = document.createElement("a"); link.href = url; link.textContent = `↓ ${format}`; downloads.append(link);
    });
    show(resultBox); resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { error(err.message || "Transcription failed."); }
  finally { submit.disabled = false; show(statusBox, false); }
});

loadConfig();
