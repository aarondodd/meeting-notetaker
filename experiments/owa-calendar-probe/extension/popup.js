async function refresh() {
  const status = await new Promise((resolve) =>
    chrome.runtime.sendMessage({ type: "POPUP_STATUS" }, resolve),
  );
  if (!status) return;
  document.getElementById("ext-version").textContent =
    status.extensionVersion || "(unknown)";
  const conn = document.getElementById("conn-state");
  if (status.connected) {
    conn.textContent = "connected";
    conn.className = "value ok";
  } else {
    conn.textContent = "not connected";
    conn.className = "value bad";
  }
}

document.getElementById("fetch-now").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "POPUP_FETCH_NOW" }, (resp) => {
    if (resp && resp.ok) {
      document.getElementById("fetch-now").textContent =
        "Sent. See relay log.";
    } else {
      document.getElementById("fetch-now").textContent =
        "Failed: " + (resp && resp.error ? resp.error : "unknown");
    }
  });
});

refresh();
