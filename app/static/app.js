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

// 2) 引导气泡：在真实界面上逐个高亮元素并讲解。每个阶段第一次进入自动弹出，右上角「引导」可随时重看。
//    不遮挡操作（遮罩不拦截点击），页面轮询重绘后按选择器重新定位；目标元素不在页面上的步骤自动跳过。
(function () {
  var STEPS = {
    gate: [
      ["form[action='/gate']", "口令", "输入课程群里公布的口令。30 天内不用再输。"]],
    register: [
      ["form[action='/register'] input[name=nickname]", "代号", "同组同学能认出你就行，最多 20 字，不能和别人重复。一台设备只能登记一个代号。"],
      ["form[action='/register'] select[name=grp]", "当前组", "打开 LM Core 的 Grouping 页，看 Your selection 显示的组，照着选。"],
      ["form[action='/register'] input[name=lm_count]", "该组现在的人数", "LM Core 表格 Members 列里你现在这个组的人数，含不参加本工具的人。系统靠它判断哪个组坐得下。"],
      ["form[action='/register'] button.primary", "登记", "点这里。登记后首页底部会有「恢复链接」，换设备时用它找回身份。"]],
    home: [
      ["form[action='/me/group']", "你的身份", "代号和当前组。还没进意向组时组号填错了可以直接改。"],
      [".intro", "怎么用", "想一起组队的人建一个意向组，各自登记现在的组号和该组人数；系统把全站所有意向组放在一起算，坐得下或能互换时自动分配。"],
      ["form[action='/team/create']", "组长：创建意向组", "填你现在这个组在 LM Core 上的人数，创建后把邀请链接发给队友。"],
      ["form[action='/team/join']", "队友：加入意向组", "填邀请码和你现在这个组的人数。直接点组长发的邀请链接更省事。"],
      ["details.recover", "恢复链接", "换设备或清了浏览器数据时用它找回身份。不要外传。"]],
    "pool-team": [
      [".steps", "五个阶段", "组建 → 等待分配 → 确认 → 执行 → 完成。页面每 2.5 秒自动刷新，不用手动刷。"],
      [".todo", "下一步", "这里永远告诉你现在该做什么、在等什么。"],
      [".share", "邀请链接", "发给想一起组队的人，最多 5 人。不用拉别的组的人。"],
      [".members", "成员", "每个人现在的组号和该组人数。自己组的人数填错了可以在这里更正；有爬虫数据时以爬虫为准。"],
      [".cands", "坐得下吗", "按各组人数判断整队能不能搬进某个组。全都坐不下也别急：系统把全站的意向组放在一起算，几个意向组互换位置也算成功。"],
      [".team-actions", "组长的按钮", "人齐了点「成员已到齐，等待分配」。之后不能再加入 / 退出；要改成员就「重新开放加入」。方案被否决后点「重新参与分配」。"],
      [".timeline", "记录", "谁加入、谁填报了人数、什么时候进入分配，全组可见。"]],
    "pool-open": [
      [".steps", "四个阶段", "互选 → 确认 → 执行 → 完成。页面每 2.5 秒自动刷新，不用手动刷。"],
      [".todo", "下一步", "这里永远告诉你现在该做什么、在等谁。"],
      [".share", "邀请链接", "发到每一个参与组的群里。每个组的全部成员都加入后才开放互选。"],
      [".groups", "成员与互选", "各组到齐情况。开放后在想同组的人后面点「选」，双方都选才算数，变绿就是互选成功。不选则由系统安排。"],
      [".group-tools", "本组工具", "人数填错可以更正；有组员不肯加入可以添加「占位」代表他：占着位置、不参与互选、不会被移动。"],
      ["form[action='/pool/plan']", "生成方案", "三个条件都打勾后，任何人都可以点。系统会算出移动人数最少的方案。"],
      [".timeline", "记录", "谁加入、谁互选、谁操作了，全池可见。"]],
    "pool-confirming": [
      [".card.highlight", "你的安排", "和谁互换、留在原组还是进空位，以及新组员。看清楚后点同意；任何一人不同意都会退回。"],
      [".groups", "新分组", "每个人的去向，以及谁已经同意。「组内其他人」是方案里不动的人，不需要表态。"]],
    "pool-executing": [
      [".card.go", "轮到你了", "按提示去 LM Core 操作，点 Save my choice，回来点「已在 LM Core 保存」。右侧示意图告诉你取消哪个、勾选哪个。"],
      [".card.highlight:not(.go)", "等待", "轮到你时这张卡会变绿并提示。没轮到之前不要在 LM Core 操作。"],
      [".progress", "进度", "多少人已到位、各组此刻人数。"],
      ["#chat", "一对一对话", "和交换对象的私聊：预设短语，或「加我微信细说」。微信号端到端加密，服务器只转发密文、不保存。"],
      [".card.highlight details", "出了问题", "有人操作错、失联，或目标组被别人占了：任何人可以中止执行。"]],
    "pool-done": [
      [".groups", "最终分组", "去 LM Core 核对一下 Your selection。"],
      ["form[action='/team/leave'], form[action='/pool/leave']", "再来一轮", "退出后可以重新创建或加入。"]]
  };

  function q(sel) { return document.querySelector(sel); }
  function pageKey() {
    if (q("form[action='/gate']")) return "gate";
    if (q("form[action='/register']")) return "register";
    var todo = q("#live .todo");
    if (todo) { var m = /s-(\w+)/.exec(todo.className); return m ? "pool-" + m[1] : null; }
    if (q("form[action='/team/create']") || q("form[action='/pool/create']") || q("form[action='/pool/join']")) return "home";
    return null;
  }
  function visible(el) {
    if (!el || el.closest("[hidden]")) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function seen() { try { return JSON.parse(localStorage.getItem("gst_tour_seen") || "{}"); } catch (e) { return {}; } }
  function markSeen(key) { try { var s = seen(); s[key] = 1; localStorage.setItem("gst_tour_seen", JSON.stringify(s)); } catch (e) {} }

  var root, box, bubble, cur = { key: null, i: 0 }, ticker = null;
  function ensure() {
    if (root) return;
    root = document.createElement("div"); root.id = "tour"; root.hidden = true;
    root.innerHTML = '<div class="tour-box"></div><div class="tour-bubble" role="dialog" aria-live="polite">' +
      '<h4></h4><p></p><div class="tour-nav"><button type="button" data-t="prev">上一步</button>' +
      '<button type="button" class="primary" data-t="next">下一步</button><button type="button" class="ghost" data-t="skip">跳过</button>' +
      '<span class="tour-n"></span></div></div>';
    document.body.appendChild(root);
    box = root.firstChild; bubble = root.lastChild;
    bubble.addEventListener("click", function (e) {
      var b = e.target.closest("[data-t]"); if (!b) return;
      if (b.dataset.t === "prev") show(cur.i - 1); else if (b.dataset.t === "next") show(cur.i + 1); else end();
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !root.hidden) end(); });
    window.addEventListener("resize", place); window.addEventListener("scroll", place, true);
    window.addEventListener("load", place);  // 字体 / 图片加载完布局可能变
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(place);
  }
  function steps() {  // 当前阶段里目标元素真的在页面上的步骤
    return (STEPS[cur.key] || []).map(function (s) { return { el: q(s[0]), sel: s[0], title: s[1], text: s[2] }; })
      .filter(function (s) { return visible(s.el); });
  }
  function show(i) {
    var list = steps();
    if (!list.length) return end();
    cur.i = Math.max(0, Math.min(i, list.length - 1));
    if (i >= list.length) return end();
    var s = list[cur.i];
    bubble.querySelector("h4").textContent = s.title;
    bubble.querySelector("p").textContent = s.text;
    bubble.querySelector(".tour-n").textContent = (cur.i + 1) + "/" + list.length;
    bubble.querySelector("[data-t=prev]").disabled = cur.i === 0;
    bubble.querySelector("[data-t=next]").textContent = cur.i === list.length - 1 ? "完成" : "下一步";
    root.hidden = false;
    cur.sel = s.sel;
    s.el.scrollIntoView({ block: "center" });
    place();
  }
  function place() {
    if (!root || root.hidden) return;
    var el = q(cur.sel);
    if (!visible(el)) { show(cur.i); return; }  // 页面重绘后元素没了：按当前序号重新找
    var r = el.getBoundingClientRect(), pad = 6, vw = window.innerWidth, vh = window.innerHeight;
    box.style.left = (r.left - pad) + "px"; box.style.top = (r.top - pad) + "px";
    box.style.width = (r.width + 2 * pad) + "px"; box.style.height = (r.height + 2 * pad) + "px";
    var bw = bubble.offsetWidth, bh = bubble.offsetHeight;
    var top = r.bottom + 14;
    if (top + bh > vh - 8) top = Math.max(8, r.top - bh - 14);
    if (top + bh > vh - 8) top = Math.max(8, vh - bh - 8);
    var left = Math.min(Math.max(12, r.left), vw - bw - 12);
    bubble.style.top = top + "px"; bubble.style.left = left + "px";
  }
  function start(key, force) {
    if (!key || !STEPS[key]) return;
    if (!force && seen()[key]) return;
    ensure(); markSeen(key); cur.key = key; show(0);
  }
  function end() { if (root) root.hidden = true; cur.key = null; }

  document.addEventListener("click", function (e) {
    var a = e.target.closest("#tour-start"); if (!a) return;
    e.preventDefault(); start(pageKey(), true);
  });
  var lastKey = pageKey();
  start(lastKey, false);
  ticker = setInterval(function () {  // 池的阶段变了：关掉旧引导，新阶段第一次进入时自动弹；显示期间定期重新定位（页面重绘 / 布局变化）
    var k = pageKey();
    if (k !== lastKey) { lastKey = k; end(); start(k, false); }
    else if (root && !root.hidden) place();
  }, 500);
})();

// 3) 池页面：每 2.5 秒轮询一次。rev 没变服务器返回 204；一对一对话的新消息在响应头 X-Msg 里。
//    对话里的「加我微信细说」端到端加密：浏览器各自生成 ECDH(P-256) 密钥对，公钥经服务器交换，
//    双方派生 AES-GCM 密钥；服务器只转发密文，从不接触明文。私钥只存本机浏览器。
(function () {
  var box = document.getElementById("live");
  if (!box) return;
  var rev = box.dataset.rev, backoff = 0, timer = null;

  // ---- 端到端密钥 ----
  var subtle = (window.crypto && window.crypto.isSecureContext !== false && window.crypto.subtle) ? window.crypto.subtle : null;
  var myKeys = null, pairKeys = {};  // pair → AES-GCM CryptoKey
  function b64(buf) { return btoa(String.fromCharCode.apply(null, new Uint8Array(buf))); }
  function unb64(s) { var bin = atob(s), a = new Uint8Array(bin.length); for (var i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); return a.buffer; }
  function loadKeys() {
    if (!subtle) return Promise.resolve(null);
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem("gst_e2e") || "null"); } catch (e) {}
    var p = saved
      ? subtle.importKey("jwk", saved.priv, { name: "ECDH", namedCurve: "P-256" }, true, ["deriveKey"]).then(function (priv) { return { priv: priv, pub: saved.pub }; })
      : subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveKey"]).then(function (kp) {
          return Promise.all([subtle.exportKey("jwk", kp.privateKey), subtle.exportKey("raw", kp.publicKey)]).then(function (x) {
            var rec = { priv: x[0], pub: b64(x[1]) };
            try { localStorage.setItem("gst_e2e", JSON.stringify(rec)); } catch (e) {}
            return { priv: kp.privateKey, pub: rec.pub };
          });
        });
    return p.then(function (k) {
      myKeys = k;
      return fetch("/pool/pubkey", { method: "POST", credentials: "same-origin", body: new URLSearchParams({ pub: k.pub }) }).then(function () { return k; });
    }).catch(function () { return null; });
  }
  function pairKey(pair, peerPub) {
    if (!subtle || !myKeys || !peerPub) return Promise.resolve(null);
    if (pairKeys[pair] && pairKeys[pair].pub === peerPub) return Promise.resolve(pairKeys[pair].key);
    return subtle.importKey("raw", unb64(peerPub), { name: "ECDH", namedCurve: "P-256" }, false, [])
      .then(function (pk) { return subtle.deriveKey({ name: "ECDH", public: pk }, myKeys.priv, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]); })
      .then(function (key) { pairKeys[pair] = { pub: peerPub, key: key }; return key; })
      .catch(function () { return null; });
  }
  function encrypt(pair, text) {
    var c = chats.filter(function (x) { return x.pair === pair; })[0];
    return pairKey(pair, c && c.key).then(function (key) {
      if (!key) throw new Error("对方还没打开过页面，拿不到他的密钥，稍后再试");
      var iv = crypto.getRandomValues(new Uint8Array(12));
      return subtle.encrypt({ name: "AES-GCM", iv: iv }, key, new TextEncoder().encode(text)).then(function (ct) { return { iv: b64(iv), ct: b64(ct) }; });
    });
  }
  function decrypt(m) {  // 返回 Promise<string>
    var c = chats.filter(function (x) { return x.pair === m.pair; })[0];
    return pairKey(m.pair, c && c.key).then(function (key) {
      if (!key) throw new Error();
      return subtle.decrypt({ name: "AES-GCM", iv: unb64(m.cipher.iv) }, key, unb64(m.cipher.ct));
    }).then(function (buf) { return new TextDecoder().decode(buf); })
      .catch(function () { return "（加密消息无法解密：对方换了设备或清了浏览器数据）"; });
  }

  // ---- 一对一对话 ----
  var panel = document.getElementById("chat"), logEl = document.getElementById("chat-log");
  var chats = [], active = null, logs = {}, unread = {}, lastId = 0, pageTitle = document.title;
  var wxInput = document.getElementById("chat-wx");
  try { if (wxInput) wxInput.value = localStorage.getItem("gst_wx") || ""; } catch (e) {}

  function setChats(list) {                  // list: [{pair, peer, key}]
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
      li.className = (m.mine ? "mine" : "") + (m.cipher ? " e2e" : "");
      li.innerHTML = "<small></small><span></span>";
      li.firstChild.textContent = (m.mine ? "我" : m.from) + " " + m.at + (m.cipher ? " · 端到端加密" : "");
      li.lastChild.textContent = m.plain || m.text || "…";
      logEl.appendChild(li);
    });
    logEl.scrollTop = logEl.scrollHeight;
    var n = totalUnread();
    document.getElementById("chat-unread").textContent = n ? " · " + n + " 条新消息" : "";
    document.title = n && document.hidden ? "(" + n + ") " + pageTitle : pageTitle;
    var canE2E = !!(subtle && current && current.key);
    panel.querySelectorAll("[data-wx], #chat-wx").forEach(function (el) { el.disabled = !canE2E; });
    var hint = document.getElementById("chat-e2e-hint");
    if (hint) hint.textContent = !subtle ? "此浏览器不支持端到端加密，微信号功能不可用。" : canE2E ? "" : "对方还没打开过页面，暂时拿不到他的密钥。";
  }

  function receive(m) {
    lastId = Math.max(lastId, m.id);
    (logs[m.pair] = logs[m.pair] || []).push(m);
    var seenNow = m.pair === active && !document.hidden && !panel.classList.contains("closed");
    if (!m.mine && !seenNow) unread[m.pair] = (unread[m.pair] || 0) + 1;
    if (m.cipher) decrypt(m).then(function (t) { m.plain = t; render(); });
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
  function post(body) {
    return fetch("/pool/msg", { method: "POST", credentials: "same-origin", body: new URLSearchParams(body) })
      .then(function (r) { if (r.status !== 204) return r.text().then(function (t) { alert(t || "发送失败"); }); })
      .then(function () { schedule(200); });
  }
  panel.addEventListener("click", function (e) {
    var b = e.target.closest("[data-say]");
    if (b && active) { post({ kind: b.dataset.say, pair: active }); return; }
    var w = e.target.closest("[data-wx]");
    if (!w || !active) return;
    var wx = (wxInput.value || "").trim();
    if (!wx) { wxInput.focus(); return; }
    try { localStorage.setItem("gst_wx", wx); } catch (e2) {}
    encrypt(active, "加我微信细说：" + wx)
      .then(function (c) { return post({ kind: "wechat", pair: active, iv: c.iv, ct: c.ct }); })
      .catch(function (err) { alert(err.message || "发送失败"); });
  });
  setChats(JSON.parse(document.getElementById("chat-data").textContent).map(function (c) { return { pair: c[0], peer: c[1], key: c[2] }; }));
  loadKeys().then(render);

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
