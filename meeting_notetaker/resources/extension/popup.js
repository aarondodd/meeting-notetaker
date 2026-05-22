// Popup logic.
// Talks to the service worker to read connection state + app version.

function refresh() {
  chrome.runtime.sendMessage({ type: "POPUP_STATUS" }, (resp) => {
    if (!resp) return;
    const dot = document.getElementById("dot");
    const statusText = document.getElementById("status-text");
    if (resp.connected) {
      dot.classList.add("connected");
      dot.classList.remove("disconnected");
      statusText.textContent = "Connected to app";
    } else {
      dot.classList.add("disconnected");
      dot.classList.remove("connected");
      statusText.textContent = "Not connected -- is Meeting Notetaker running?";
    }
    document.getElementById("ext-version").textContent = resp.extensionVersion || "-";
    document.getElementById("app-version").textContent = resp.appVersion || "(unknown)";
    if (resp.inflightCount > 0) {
      document.getElementById("inflight-row").style.display = "block";
      document.getElementById("inflight-count").textContent = resp.inflightCount;
    }
  });
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "POPUP_RECONNECT" }, () => {
    // Service worker takes a beat to redial; refresh after a short
    // delay so the dot reflects the new state.
    setTimeout(refresh, 600);
  });
});

refresh();
