(function () {
  var st = document.createElement("style");
  st.textContent = ".watermark{border-radius:50%!important;overflow:hidden;width:56px!important;height:56px!important;background-size:contain!important;background-repeat:no-repeat!important}" +
    ".premeeting-screen{background:linear-gradient(rgba(10,16,8,.44),rgba(10,16,8,.54)),url(/bg/" + (1 + Math.floor(Math.random() * 7)) + ".jpg) center/cover no-repeat!important}";
  (document.head || document.documentElement).appendChild(st);
})();
(function () {
  if (/^\/audio-[1-4]\/?$/.test(location.pathname) && location.hash.indexOf("startAudioOnly") < 0) {
    location.replace(location.pathname + location.search + "#config.startAudioOnly=true&config.startWithVideoMuted=true");
    return;
  }
})();
(function () {
  var seenPrejoin = false, shown = false;
  function mk(id, css, html) {
    var d = document.createElement("div");
    d.id = id; d.style.cssText = css; d.innerHTML = html;
    document.body.appendChild(d);
    return d;
  }
  function tick() {
    if (!document.body) return;
    var pre = document.getElementById("premeeting-name-input");
    var pn = document.getElementById("tfs-prejoin-notice");
    if (pre) {
      seenPrejoin = true;
      if (!pn) {
        mk("tfs-prejoin-notice",
          "position:fixed;left:0;right:0;bottom:0;z-index:99999;background:#b3261e;color:#fff;" +
          "font:600 " + (window.innerWidth < 600 ? "13px/1.35" : "17px/1.4") + " Inter,Arial,sans-serif;text-align:center;" +
          "padding:" + (window.innerWidth < 600 ? "8px 12px" : "14px 16px") + ";" +
          "box-shadow:0 -2px 8px rgba(0,0,0,.4);padding-bottom:calc(8px + env(safe-area-inset-bottom))",
          window.innerWidth < 600
            ? "<strong>All meetings are recorded</strong>, transcribed, and summarized by AI. By joining you agree to be recorded."
            : "NOTICE: All meetings on this service are recorded, saved, transcribed, and summarized by AI. " +
              "By clicking Join meeting you agree to be recorded. Recordings are kept for up to 1 month and a private link is emailed to participants.");
      }
      // keep the notice out of the way of dialogs such as the login form
      var dlg = document.querySelector("[role=dialog], .modal-dialog-form, .modal-dialog-footer, [class*=modal-dialog]");
      pn = document.getElementById("tfs-prejoin-notice");
      if (pn) { pn.style.display = "block"; }
    } else if (pn) {
      pn.parentNode.removeChild(pn);
    }
    var joined = false;
    try { joined = !!(window.APP && APP.conference && APP.conference.isJoined && APP.conference.isJoined()); } catch (e) {}
    if (!shown && !pre && (joined || seenPrejoin)) {
      if (joined || document.getElementById("largeVideoContainer") || document.querySelector(".videoconference-page,#videoconference_page")) {
        shown = true;
        var b = mk("tfs-rec-banner",
          "position:fixed;left:50%;top:70px;transform:translateX(-50%);z-index:99999;background:#b3261e;color:#fff;" +
          "font:700 20px/1.4 Inter,Arial,sans-serif;padding:14px 28px;border-radius:10px;" +
          "box-shadow:0 4px 16px rgba(0,0,0,.5);text-align:center;transition:opacity .6s",
          "This meeting is being recorded.");
        setTimeout(function () { b.style.opacity = "0"; }, 5000);
        setTimeout(function () { if (b.parentNode) b.parentNode.removeChild(b); }, 5700);
      }
    }
    if (shown && pre) { shown = false; }
  }
  setInterval(tick, 500);
})();
(function () {
  // Meeting clock: walk-in meetings start at 30 minutes; the host can add 30 at a time up to 90.
  var room = location.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
  if (!/^[a-z0-9-]+$/.test(room)) return;
  var jwt = null;
  try { jwt = new URLSearchParams(location.search).get("jwt"); } catch (e) {}
  try { if (jwt) { sessionStorage.setItem("tfsjwt:" + room, jwt); } else { jwt = sessionStorage.getItem("tfsjwt:" + room); } } catch (e) {}
  var askAgainAt = 0, hourNoticeUntil = 0, hourNoticeShown = false, flash = "", flashUntil = 0;
  function joined() {
    try { return !!(window.APP && APP.conference && APP.conference.isJoined && APP.conference.isJoined()); } catch (e) { return false; }
  }
  function el() {
    var b = document.getElementById("tfs-clock");
    if (!b) {
      b = document.createElement("div");
      b.id = "tfs-clock";
      b.style.cssText = "position:fixed;left:50%;top:70px;transform:translateX(-50%);z-index:99997;max-width:92vw;" +
        "background:#2f3b28;color:#fff;font:600 16px/1.4 Inter,Arial,sans-serif;padding:12px 18px;border-radius:10px;" +
        "box-shadow:0 4px 16px rgba(0,0,0,.5);text-align:center;display:none";
      document.body.appendChild(b);
    }
    return b;
  }
  function btn(label, primary, fn) {
    var x = document.createElement("button");
    x.textContent = label;
    x.style.cssText = "margin:8px 6px 0;padding:8px 16px;border-radius:999px;font:700 15px Inter,Arial,sans-serif;cursor:pointer;" +
      (primary ? "background:#fff;color:#2f3b28;border:2px solid #fff" : "background:transparent;color:#fff;border:2px solid #fff");
    x.onclick = fn;
    return x;
  }
  function show(text, buttons, bg) {
    var b = el();
    b.style.background = bg || "#2f3b28";
    b.textContent = text;
    if (buttons) {
      var row = document.createElement("div");
      buttons.forEach(function (x) { row.appendChild(x); });
      b.appendChild(row);
    }
    b.style.display = "block";
  }
  function hide() { var b = document.getElementById("tfs-clock"); if (b) b.style.display = "none"; }
  function extend() {
    fetch("/api/extend", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ jwt: jwt }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (x.ok) { flash = "Added 30 minutes. This meeting now runs " + x.d.minutes + " minutes in total."; flashUntil = Date.now() + 8000; }
        else { flash = (x.d && x.d.detail) || "Could not add time."; flashUntil = Date.now() + 8000; }
        tick();
      }).catch(function () { flash = "Could not add time. Please try again."; flashUntil = Date.now() + 6000; });
  }
  var lastD = null, lastAt = 0, w15Start = 0, w15Off = false;
  function fmtc(sec) { sec = Math.max(0, Math.ceil(sec)); return Math.floor(sec / 60) + ":" + ("0" + (sec % 60)).slice(-2); }
  function render(d) {
    if (flash && Date.now() < flashUntil) { show(flash); return; }
    if (!d.known) { hide(); return; }
    var rem = d.remaining, mins = Math.max(0, Math.ceil(rem / 60));
    var canAdd = !!(jwt && d.minutes < 90);
    if (rem <= 30) {
      show("This meeting ends in " + fmtc(rem), null, "#b3261e");
      var cb = document.getElementById("tfs-clock"); if (cb) { cb.style.font = "800 28px/1.3 Inter,Arial,sans-serif"; cb.style.padding = "14px 26px"; }
      return;
    }
    var cb2 = document.getElementById("tfs-clock"); if (cb2) { cb2.style.font = "600 16px/1.4 Inter,Arial,sans-serif"; cb2.style.padding = "12px 18px"; }
    if (rem <= 300) {
      if (canAdd) {
        if (Date.now() < askAgainAt) { show("This meeting ends in " + mins + " minute" + (mins === 1 ? "" : "s") + "."); return; }
        show("Last warning: this meeting ends in " + mins + " minute" + (mins === 1 ? "" : "s") + ". Add 30 more minutes?", [
          btn("Add 30 minutes", true, function () { extend(); }),
          btn("No thanks", false, function () { askAgainAt = Date.now() + 90000; tick(); })
        ], "#b3261e");
        return;
      }
      show("Last warning: this meeting ends in about " + mins + " minute" + (mins === 1 ? "" : "s") + "." +
        (d.minutes >= 90 ? " We have a 90 minute limit." : ""), null, "#b3261e");
      return;
    }
    if (rem <= 900 && !w15Off) {
      if (!w15Start) { w15Start = Date.now(); }
      if (Date.now() - w15Start < 45000) {
        var msg = "This meeting ends in about 15 minutes." + (d.minutes >= 90 ? " We have a 90 minute limit, so please start wrapping up." : "");
        show(msg, canAdd ? [
          btn("Add 30 minutes", true, function () { w15Off = true; extend(); }),
          btn("No thanks", false, function () { w15Off = true; tick(); })
        ] : null, "#8a5a12");
        return;
      }
    }
    hide();
  }
  function tick() {
    if (!document.body || !joined()) return;
    fetch("/api/room-clock?room=" + encodeURIComponent(room), { cache: "no-store" })
      .then(function (r) { return r.json(); }).then(function (d) { lastD = d; lastAt = Date.now(); render(d); }).catch(function () {});
  }
  setInterval(function () {
    if (!lastD || !lastD.known || !joined()) return;
    var rem = lastD.remaining - (Date.now() - lastAt) / 1000;
    if (rem <= 45 && rem > -5) { var d2 = {}; for (var k in lastD) d2[k] = lastD[k]; d2.remaining = rem; render(d2); }
  }, 1000);
  setInterval(function () { if (lastD && lastD.known && lastD.remaining - (Date.now() - lastAt) / 1000 < 90) tick(); }, 5000);
  setInterval(tick, 10000);
  setTimeout(tick, 3000);
})();
(function () {
  // Show the meeting name instead of the auto-generated room code.
  var room = location.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
  if (!/^[a-z0-9-]+$/.test(room)) return;
  var title = "", orig = null, tries = 0;
  function load() {
    fetch("/api/room-title?room=" + encodeURIComponent(room), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.title) { title = d.title; } })
      .catch(function () {})
      .then(function () { if (!title && ++tries < 5) { setTimeout(load, 3000); } });
  }
  load();
  function swap() {
    if (!document.body) return;
    if (orig === null) {
      var m = /^(.*?) \| /.exec(document.title || "");
      if (m && m[1]) { orig = m[1].trim(); }
      else { return; }
    }
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = w.nextNode())) {
      var t = n.nodeValue.trim();
      if (t === orig) {
        var p = n.parentNode;
        if (p && p.tagName !== "SCRIPT" && p.tagName !== "STYLE" && p.tagName !== "TITLE") {
          n.nodeValue = title || " ";
        }
      }
    }
  }
  setInterval(swap, 600);
})();
(function () {
  // Closing page wording
  var MSG = "Thank you for using Example Org's free meeting solution. Please let us know if you found it useful.";
  function fix() {
    if (!document.body) return;
    var w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    var n;
    while ((n = w.nextNode())) {
      var t = n.nodeValue;
      if (/^\s*Thank you for using\b/i.test(t) && t.trim() !== MSG) {
        var p = n.parentNode;
        if (p && p.tagName !== "SCRIPT" && p.tagName !== "STYLE") {
          n.nodeValue = MSG;
          if (!document.getElementById("tfs-feedback")) {
            var d = document.createElement("div");
            d.id = "tfs-feedback";
            d.style.cssText = "margin-top:14px;font-size:16px;line-height:1.8";
            var st = 'color:#fff;font-weight:700;text-decoration:underline';
            var mail = 'mailto:meetings-admin@example.org?subject=' + encodeURIComponent('TFS free meetings feedback');
            d.innerHTML = '<a href="' + mail + '" style="' + st + '">Email us your feedback</a>' +
              ' &nbsp;|&nbsp; <a href="https://example.org/contact/" style="' + st + '">Contact page</a>';
            p.parentNode.insertBefore(d, p.nextSibling);
          }
        }
      }
    }
  }
  setInterval(fix, 600);
})();
(function () {
  // Guests who arrive before the host: friendly message, no dead-end login button, and a way for invitees to start it.
  var room = location.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
  if (!/^[a-z0-9-]+$/.test(room)) return;
  var scheduled = !/^(tfs-|audio-)/.test(room);
  function fix() {
    if (!document.body) return;
    var dlg = document.querySelector("[role=dialog], [class*=modal-dialog]");
    if (!dlg || dlg.getAttribute("data-tfs-wait") === "1") return;
    var txt = dlg.innerText || "";
    if (txt.indexOf("no moderators have yet arrived") < 0) return;
    dlg.setAttribute("data-tfs-wait", "1");
    var w = document.createTreeWalker(dlg, NodeFilter.SHOW_TEXT, null), n;
    while ((n = w.nextNode())) {
      var t = n.nodeValue;
      if (t.indexOf("no moderators have yet arrived") >= 0) {
        n.nodeValue = scheduled
          ? "The host has not started this meeting yet. Please keep this page open and you will join automatically as soon as it starts. If you are on the invitation, you can start the meeting yourself with the button below."
          : "The host has not started this meeting yet. Please keep this page open and you will join automatically as soon as it starts.";
      } else if (t.trim() === "Waiting for a moderator…" || t.trim() === "Waiting for a moderator...") {
        n.nodeValue = "Waiting for the host";
      }
    }
    var btns = dlg.querySelectorAll("button, [role=button]");
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i], bt = (b.innerText || "").trim().toLowerCase();
      if (bt === "log-in" || bt === "login" || bt === "log in") {
        if (scheduled) {
          b.innerText = "Start the meeting";
          b.onclick = function (e) { e.preventDefault(); e.stopPropagation(); location.href = "/?room=" + encodeURIComponent(room); };
          b.addEventListener("click", function (e) { e.preventDefault(); e.stopImmediatePropagation(); location.href = "/?room=" + encodeURIComponent(room); }, true);
        } else {
          b.style.display = "none";
        }
      }
    }
  }
  setInterval(fix, 700);
})();
