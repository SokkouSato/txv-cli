// Auto-capture: whenever Chrome's X session switches account (auth_token / ct0
// change), push the cookies to the local ingest server.
//
// The bridge now requires a shared token. The token lives in
// chrome.storage.local.bridgeToken (saved from the popup). Without a token we
// never send anything and only flag the badge so the user notices.

const WANT = new Set(["auth_token", "ct0", "twid", "auth_multi", "kdt", "gt", "guest_id"]);
const ENDPOINT = "http://127.0.0.1:8791/ingest";
const TOKEN_KEY = "bridgeToken";
let timer = null;

async function getToken() {
  try {
    const stored = await chrome.storage.local.get(TOKEN_KEY);
    return String(stored[TOKEN_KEY] || "").trim();
  } catch (err) {
    return "";
  }
}

async function readCookies() {
  const all = [];
  for (const domain of ["x.com", "twitter.com"]) {
    const list = await chrome.cookies.getAll({ domain });
    for (const c of list) {
      if (!WANT.has(c.name)) continue;
      all.push({
        host: c.domain, name: c.name, value: c.value,
        secure: c.secure, host_only: c.hostOnly, expires: c.expirationDate || null,
      });
    }
  }
  return all;
}

async function markNoToken() {
  await chrome.action.setBadgeText({ text: "!" });
  try {
    await chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
    await chrome.action.setTitle({ title: "未配置桥接令牌（打开扩展弹窗填写）" });
  } catch (err) {
    console.log("badge update failed", err);
  }
}

async function push() {
  try {
    const token = await getToken();
    if (!token) {
      // No token -> no request at all; only tell the user via the badge.
      await markNoToken();
      console.log("bridge token not configured; capture not sent");
      return;
    }
    const cookies = await readCookies();
    if (!cookies.some((c) => c.name === "auth_token")) return;
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Bridge-Token": token,
      },
      body: JSON.stringify({ captured_at: new Date().toISOString(), auto: true, browser: navigator.userAgent, cookies }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      await markNoToken();
      console.log("capture rejected", res.status);
      return;
    }
    console.log("capture ok", data);
    if (data.accounts) {
      await chrome.action.setBadgeText({ text: `${data.with_ct0 ? data.with_ct0.length : 0}/${data.accounts}` });
      await chrome.action.setBadgeBackgroundColor({ color: "#1d9bf0" });
      await chrome.action.setTitle({ title: "X Cookie Bridge" });
    }
  } catch (err) {
    console.log("capture failed", err);
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(push, 1500);
}

chrome.cookies.onChanged.addListener((info) => {
  const name = info.cookie.name;
  if (!["ct0", "auth_token", "twid"].includes(name)) return;
  if (!String(info.cookie.domain).includes("x.com")) return;
  schedule();
});

// Re-push right after the user saves/clears the token in the popup.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[TOKEN_KEY]) schedule();
});

chrome.runtime.onStartup.addListener(schedule);
chrome.runtime.onInstalled.addListener(schedule);
schedule();
