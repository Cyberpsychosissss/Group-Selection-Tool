// 1) 复制邀请消息
document.addEventListener("click", function (e) {
  var btn = e.target.closest("[data-copy]");
  if (!btn) return;
  var text = btn.getAttribute("data-copy");
  var done = function () { var old = btn.textContent; btn.textContent = "已复制"; setTimeout(function () { btn.textContent = old; }, 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done);
  } else {
    var t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); done(); } finally { t.remove(); }
  }
});

// 1b) 需要二次确认的表单（data-confirm）
document.addEventListener("submit", function (e) {
  var f = e.target;
  if (f.dataset && f.dataset.confirm && !confirm(f.dataset.confirm)) e.preventDefault();
});

// 2) 池页面：每 2.5 秒轮询一次。rev 没变服务器返回 204；一对一对话的新消息在响应头 X-Msg 里。
(function () {
  var box = document.getElementById("live");
  if (!box) return;
  var rev = box.dataset.rev, backoff = 0, timer = null;

  // ---- 一对一对话 ----
  var panel = document.getElementById("chat"), logEl = document.getElementById("chat-log");
  var chats = [], active = null, logs = {}, unread = {}, lastId = 0, pageTitle = document.title;

  function setChats(list) {                  // list: [{pair, peer}]
    chats = list;
    var keep = {};
    chats.forEach(function (c) { keep[c.pair] = true; logs[c.pair] = logs[c.pair] || []; });
    Object.keys(logs).forEach(function (p) { if (!keep[p]) { delete logs[p]; delete unread[p]; } });
    if (!keep[active]) active = chats.length ? chats[0].pair : null;
    render();
  }

  function totalUnread() { return Object.keys(unread).reduce(function (n, p) { return n + unread[p]; }, 0); }

  function render() {
    panel.hidden = !chats.length;
    if (!chats.length) return;
    var current = chats.filter(function (c) { return c.pair === active; })[0];
    document.getElementById("chat-title").textContent = chats.length === 1 ? "与 " + current.peer : "对话";
    var tabs = document.getElementById("chat-tabs");
    tabs.innerHTML = "";
    if (chats.length > 1) chats.forEach(function (c) {
      var b = document.createElement("button");
      b.type = "button"; b.className = c.pair === active ? "on" : "";
      b.textContent = "与 " + c.peer + (unread[c.pair] ? " (" + unread[c.pair] + ")" : "");
      b.onclick = function () { active = c.pair; unread[c.pair] = 0; render(); };
      tabs.appendChild(b);
    });
    logEl.innerHTML = "";
    (logs[active] || []).forEach(function (m) {
      var li = document.createElement("li");
      li.className = m.mine ? "mine" : "";
      li.innerHTML = "<small></small><span></span>";
      li.firstChild.textContent = (m.mine ? "我" : m.from) + " " + m.at;
      li.lastChild.textContent = m.text;
      logEl.appendChild(li);
    });
    logEl.scrollTop = logEl.scrollHeight;
    var n = totalUnread();
    document.getElementById("chat-unread").textContent = n ? " · " + n + " 条新消息" : "";
    document.title = n && document.hidden ? "(" + n + ") " + pageTitle : pageTitle;
  }

  function receive(m) {
    lastId = Math.max(lastId, m.id);
    (logs[m.pair] = logs[m.pair] || []).push(m);
    var seen = m.pair === active && !document.hidden && !panel.classList.contains("closed");
    if (!m.mine && !seen) unread[m.pair] = (unread[m.pair] || 0) + 1;
  }

  window.addEventListener("focus", function () {
    if (active && !panel.classList.contains("closed")) unread[active] = 0;
    render();
  });
  document.getElementById("chat-toggle").addEventListener("click", function () {
    panel.classList.toggle("closed");
    if (!panel.classList.contains("closed") && active) unread[active] = 0;
    render();
  });
  panel.addEventListener("click", function (e) {
    var b = e.target.closest("[data-say]");
    if (!b || !active) return;
    fetch("/pool/msg", { method: "POST", credentials: "same-origin",
                         body: new URLSearchParams({ kind: b.dataset.say, pair: active }) })
      .then(function (r) { if (r.status !== 204) return r.text().then(function (t) { alert(t || "发送失败"); }); })
      .then(function () { schedule(200); });
  });
  setChats(JSON.parse(document.getElementById("chat-data").textContent).map(function (c) { return { pair: c[0], peer: c[1] }; }));

  // ---- 轮询 ----
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(tick, ms); }

  function tick() {
    fetch("/pool/live?rev=" + rev + "&m=" + lastId, { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 410) { location.href = "/"; return; } // 已不在池里
        if (r.status === 401) { location.reload(); return; }   // 口令失效
        if (r.status === 429) { backoff = 15000; return; }      // 被限流
        if (r.headers.has("X-Chats")) setChats(JSON.parse(r.headers.get("X-Chats")));
        var msgs = r.headers.get("X-Msg");
        if (msgs) { JSON.parse(msgs).forEach(receive); render(); }
        if (r.status === 200) {
          rev = r.headers.get("X-Rev") || rev;
          return r.text().then(function (html) { box.innerHTML = html; });
        }
      })
      .catch(function () {})
      .then(function () { schedule(backoff || (document.hidden ? 10000 : 2500)); backoff = 0; });
  }
  schedule(300);
})();
