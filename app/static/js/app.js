// "2026-10" -> "10-2026" (month labels on charts, day/month first like every date in the app)
window.ym = s => (typeof s === "string" && /^\d{4}-\d{2}$/.test(s)) ? s.slice(5) + "-" + s.slice(0, 4) : s;

/* Every <input type="date"> is shown as DD-MM-YYYY whatever the browser / Windows language is.
   The real date input stays in the form (hidden, same name, ISO value) so the server is unchanged;
   a text box shows DD-MM-YYYY (type 16102026 or 16/10/2026 -> 16-10-2026) and 📅 opens the normal calendar. */
window.dmyDates = function (root) {
  const pad = n => String(n).padStart(2, "0");
  const toDmy = iso => /^\d{4}-\d{2}-\d{2}$/.test(iso || "") ? iso.slice(8, 10) + "-" + iso.slice(5, 7) + "-" + iso.slice(0, 4) : "";
  function toIso(txt) {
    const d = (txt || "").replace(/[^0-9]/g, "");
    let dd, mm, yy;
    const parts = (txt || "").split(/[-/. ]+/).filter(Boolean);
    if (parts.length === 3) { [dd, mm, yy] = parts.map(Number); }
    else if (d.length === 8) { dd = +d.slice(0, 2); mm = +d.slice(2, 4); yy = +d.slice(4); }
    else if (d.length === 6) { dd = +d.slice(0, 2); mm = +d.slice(2, 4); yy = 2000 + +d.slice(4); }
    else return null;
    if (yy < 100) yy += 2000;
    if (yy > 2400) yy -= 543;                       // พ.ศ. -> ค.ศ.
    const dt = new Date(yy, mm - 1, dd);
    if (dt.getFullYear() !== yy || dt.getMonth() !== mm - 1 || dt.getDate() !== dd) return null;
    return `${yy}-${pad(mm)}-${pad(dd)}`;
  }
  (root || document).querySelectorAll('input[type="date"]:not([data-dmy])').forEach(inp => {
    inp.dataset.dmy = "1";
    const wrap = document.createElement("span");
    wrap.className = "dmy";
    if (inp.getAttribute("style")) wrap.setAttribute("style", inp.getAttribute("style"));
    inp.removeAttribute("style");
    const txt = document.createElement("input");
    txt.type = "text"; txt.inputMode = "numeric"; txt.autocomplete = "off";
    txt.placeholder = "DD-MM-YYYY"; txt.maxLength = 10; txt.value = toDmy(inp.value);
    txt.title = inp.title || "วัน-เดือน-ปี (DD-MM-YYYY)";
    if (inp.required) { txt.required = true; inp.required = false; }
    if (inp.disabled) txt.disabled = true;
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "dmy-btn"; btn.tabIndex = -1; btn.textContent = "📅"; btn.title = "เลือกจากปฏิทิน";
    inp.parentNode.insertBefore(wrap, inp);
    wrap.append(txt, btn, inp);
    inp.classList.add("dmy-native");
    inp.tabIndex = -1;
    const fire = () => inp.dispatchEvent(new Event("change", { bubbles: true }));
    txt.addEventListener("input", () => {           // auto dashes while typing: 16102026 -> 16-10-2026
      const d = txt.value.replace(/[^0-9]/g, "").slice(0, 8);
      if (/^[0-9]*$/.test(txt.value.replace(/-/g, ""))) txt.value = d.length > 4 ? `${d.slice(0, 2)}-${d.slice(2, 4)}-${d.slice(4)}` : d.length > 2 ? `${d.slice(0, 2)}-${d.slice(2)}` : d;
      const iso = toIso(txt.value);
      txt.setCustomValidity(txt.value && !iso ? "รูปแบบวันที่ DD-MM-YYYY เช่น 16-10-2026" : "");
      if (iso || !txt.value) inp.value = iso || "";
    });
    txt.addEventListener("change", () => { const iso = toIso(txt.value); if (iso) txt.value = toDmy(iso); fire(); });
    inp.addEventListener("change", () => { const v = toDmy(inp.value); if (v !== txt.value) txt.value = v; });
    btn.addEventListener("click", () => {
      try { inp.showPicker(); } catch (e) { inp.style.pointerEvents = "auto"; inp.focus(); inp.click(); }
    });
  });
};
document.addEventListener("DOMContentLoaded", () => window.dmyDates());

/* stat cards: units in small type (2 ชม. 48 นาที -> 2ชม. 48นาที with small units) so the number stays on one line;
   the full label is kept as a tooltip when it is clamped to 2 lines */
window.compactKpis = function (root) {
  (root || document).querySelectorAll(".kpi .v").forEach(v => {
    if (v.dataset.k) return;
    v.dataset.k = "1";
    v.childNodes.forEach(n => {
      if (n.nodeType !== 3 || !/\d/.test(n.textContent)) return;
      const span = document.createElement("span");
      span.innerHTML = n.textContent.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))
        .replace(/(\d)\s*(ชม\.|ชั่วโมง|นาที|วัน|%|MB|฿|ครั้ง|ใบ|งาน)/g, '$1<span class="u">$2</span>');
      n.replaceWith(...span.childNodes);
    });
    v.title = v.textContent.trim();
  });
  (root || document).querySelectorAll(".kpi .l").forEach(l => { if (!l.title) l.title = l.textContent.trim(); });
};
document.addEventListener("DOMContentLoaded", () => window.compactKpis());

// Small helpers — no framework on purpose, so the app stays fast on factory phones.
(function () {
  // notification bell
  const bell = document.getElementById("bell");
  const pop = document.getElementById("notif-pop");
  async function refreshBell() {
    try {
      const r = await fetch("/api/notifications/unread", { credentials: "same-origin" });
      if (!r.ok) return;
      const d = await r.json();
      const badges = document.querySelectorAll(".bell-count");
      badges.forEach(b => { b.textContent = d.count; b.style.display = d.count ? "" : "none"; });
      if (pop) {
        pop.innerHTML = d.items.length
          ? d.items.map(i => `<a href="${i.link}" data-id="${i.id}" class="n-${i.level}"><b>${esc(i.title)}</b><small>${esc(i.body)}</small><small class="muted"> · ${i.at}</small></a>`).join("")
            + `<a href="/notifications" style="text-align:center;font-weight:700">ดูทั้งหมด / View all</a>`
          : `<a href="/notifications" class="muted">ไม่มีแจ้งเตือนใหม่</a>`;
      }
    } catch (e) { }
  }
  function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  if (bell && pop) {
    bell.addEventListener("click", e => { e.stopPropagation(); pop.classList.toggle("open"); });
    document.addEventListener("click", () => pop.classList.remove("open"));
    pop.addEventListener("click", e => {
      const a = e.target.closest("a[data-id]");
      if (a) { const fd = new FormData(); fd.append("nid", a.dataset.id); navigator.sendBeacon && navigator.sendBeacon("/notifications/read", fd); }
    });
  }
  if (document.getElementById("bell")) { refreshBell(); setInterval(refreshBell, 45000); }

  // photo input previews + client-side downscale (saves upload time on 4G)
  document.querySelectorAll(".photo-input input[type=file]").forEach(inp => {
    inp.addEventListener("change", async () => {
      const strip = inp.closest(".photo-input").querySelector(".preview-strip");
      if (strip) strip.innerHTML = "";
      const files = Array.from(inp.files || []);
      const dt = new DataTransfer();
      for (const f of files) {
        const small = await downscale(f, 1600, 0.8).catch(() => f);
        dt.items.add(small);
        if (strip) { const img = document.createElement("img"); img.src = URL.createObjectURL(small); strip.appendChild(img); }
      }
      try { inp.files = dt.files; } catch (e) { }
      const lab = inp.closest(".photo-input").querySelector(".count");
      if (lab) lab.textContent = files.length ? `${files.length} รูป` : "";
    });
  });
  function downscale(file, max, q) {
    return new Promise((res, rej) => {
      if (!file.type.startsWith("image/") || file.size < 600000) return res(file);
      const img = new Image();
      img.onload = () => {
        let { width: w, height: h } = img;
        const s = Math.min(1, max / Math.max(w, h));
        const c = document.createElement("canvas"); c.width = Math.round(w * s); c.height = Math.round(h * s);
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        c.toBlob(b => b ? res(new File([b], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" })) : rej(), "image/jpeg", q);
      };
      img.onerror = rej;
      img.src = URL.createObjectURL(file);
    });
  }

  // confirm buttons
  document.querySelectorAll("[data-confirm]").forEach(el => el.addEventListener("click", e => { if (!confirm(el.dataset.confirm)) e.preventDefault(); }));
  // toggle panels
  document.querySelectorAll("[data-toggle]").forEach(el => el.addEventListener("click", e => {
    e.preventDefault();
    const t = document.getElementById(el.dataset.toggle);
    if (t) { t.style.display = t.style.display === "none" ? "" : "none"; t.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  }));
  // auto-submit filters
  document.querySelectorAll("form[data-autosubmit] select").forEach(s => s.addEventListener("change", () => s.form.submit()));
})();

// "⬇ PNG" on every chart: exports a white-background image ready to paste into PowerPoint / LINE
window.addEventListener("load", () => {
  document.querySelectorAll(".card canvas").forEach(cv => {
    const card = cv.closest(".card");
    const h = card && card.querySelector("h3");
    if (!h || h.querySelector(".png-btn")) return;
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn sm ghost png-btn noprint"; b.textContent = "⬇ PNG"; b.style.marginLeft = "auto";
    b.title = "บันทึกกราฟเป็นรูปภาพ";
    b.onclick = () => {
      const out = document.createElement("canvas");
      out.width = cv.width; out.height = cv.height + 40;
      const g = out.getContext("2d");
      g.fillStyle = "#ffffff"; g.fillRect(0, 0, out.width, out.height);
      g.fillStyle = "#1c2431"; g.font = "bold 18px Sarabun, sans-serif";
      g.fillText(h.textContent.replace("⬇ PNG", "").trim(), 12, 26);
      g.drawImage(cv, 0, 40);
      const a = document.createElement("a");
      a.href = out.toDataURL("image/png");
      a.download = (h.textContent.replace("⬇ PNG", "").trim().replace(/[^\w฀-๿]+/g, "_") || "chart") + ".png";
      a.click();
    };
    h.style.display = "flex"; h.style.alignItems = "center"; h.style.gap = "8px";
    h.appendChild(b);
  });
});
