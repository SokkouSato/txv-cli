const WANT = new Set(["auth_token", "ct0", "twid", "auth_multi", "kdt", "gt", "guest_id"]);
const ENDPOINT = "http://127.0.0.1:8791/ingest";
const TOKEN_KEY = "bridgeToken";

const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = cls || "";
}

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
        host: c.domain,
        name: c.name,
        value: c.value,
        secure: c.secure,
        host_only: c.hostOnly,
        expires: c.expirationDate || null,
      });
    }
  }
  return all;
}

async function buildPayload() {
  const cookies = await readCookies();
  const plain = {};
  for (const c of cookies) plain[c.name] = c.value;
  return {
    captured_at: new Date().toISOString(),
    browser: navigator.userAgent,
    cookies,
    summary: { cookie_count: cookies.length, names: Object.keys(plain).sort() },
  };
}

async function send() {
  const token = await getToken();
  if (!token) {
    setStatus("未配置令牌：请把 work/bridge_token.txt 里的令牌粘贴到上方输入框，点『保存令牌』后再发送。", "err");
    await chrome.action.setBadgeText({ text: "!" });
    await chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
    return;
  }
  $("send").disabled = true;
  setStatus("读取并发送中…");
  try {
    const payload = await buildPayload();
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Token": token },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const reason = res.status === 403
        ? "被拒绝（403）：令牌不对，或扩展 ID / Origin 与服务端不一致。"
        : `上报失败：HTTP ${res.status} ${data.error || ""}`.trim();
      setStatus(reason, "err");
      await chrome.action.setBadgeText({ text: "!" });
      return;
    }
    const have = (data.with_ct0 || []).length;
    setStatus(
      `已上报 ✓ 当前账号: ${data.active_uid || "未知"}\n` +
      `已收集 ${data.accounts || 0} 个账号，其中 ${have} 个可用（有 ct0）\n` +
      (data.missing_ct0 && data.missing_ct0.length
        ? `还缺 ct0: ${data.missing_ct0.join(", ")}\n→ 切到这些账号再点一次图标`
        : "全部账号就绪，可以回话告诉 agent 了。"),
      "ok"
    );
    await chrome.action.setBadgeText({ text: `${have}/${data.accounts || 0}` });
    await chrome.action.setBadgeBackgroundColor({ color: "#1d9bf0" });
  } catch (err) {
    setStatus("发送失败: " + err.message + "\n可改用下面的『另存为 JSON 文件』。", "err");
  } finally {
    $("send").disabled = false;
  }
}

$("send").addEventListener("click", () => send());

$("saveToken").addEventListener("click", async () => {
  const token = $("token").value.trim();
  if (!token) {
    setStatus("令牌不能为空。", "err");
    return;
  }
  await chrome.storage.local.set({ [TOKEN_KEY]: token });
  $("token").value = token;
  setStatus("令牌已保存（仅保存在本机 chrome.storage.local，不再明文回显）。", "ok");
});

$("save").addEventListener("click", async () => {
  setStatus("生成文件…");
  try {
    const payload = await buildPayload();
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    await chrome.downloads.download({ url, filename: "xcookies.json", saveAs: false });
    setStatus("已保存到下载目录: xcookies.json", "ok");
  } catch (err) {
    setStatus("保存失败: " + err.message, "err");
  }
});

// 打开弹窗时：回填令牌（输入框为 password，不落盘任何页面），
// 有令牌才自动上报一次并把服务端进度显示出来。
(async () => {
  const token = await getToken();
  if (token) {
    $("token").value = token;
    await send();
  } else {
    setStatus("未配置令牌：请到 work/bridge_token.txt 读取令牌并填入上方输入框。", "err");
  }
})();
