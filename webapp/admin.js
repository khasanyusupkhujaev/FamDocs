(function () {
  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }

  function formatUzs(n) {
    try {
      return new Intl.NumberFormat("en-US").format(Number(n)) + " UZS";
    } catch {
      return String(n) + " UZS";
    }
  }

  function formatDate(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return String(iso).slice(0, 16);
    }
  }

  function tokenFromUrl() {
    try {
      return new URLSearchParams(window.location.search).get("token") || "";
    } catch {
      return "";
    }
  }

  function rememberedToken() {
    try {
      return sessionStorage.getItem("famdoc_admin_token") || "";
    } catch {
      return "";
    }
  }

  function persistToken(t) {
    try {
      sessionStorage.setItem("famdoc_admin_token", t);
    } catch (_) {}
  }

  function currentToken() {
    const inp = $("adm-token-input");
    const v = (inp && inp.value.trim()) || "";
    if (v) return v;
    const u = tokenFromUrl();
    if (u) return u;
    return rememberedToken();
  }

  function showError(msg) {
    const el = $("adm-error");
    if (!el) return;
    el.textContent = msg;
    el.hidden = !msg;
  }

  async function loadDashboard() {
    const token = currentToken();
    showError("");
    const dash = $("adm-dashboard");
    const errBox = $("adm-error");
    const loadBtn = $("adm-load-btn");
    if (!token) {
      showError("Enter the admin token (same as FAMDOC_ADMIN_WEB_TOKEN on the server).");
      return;
    }
    persistToken(token);
    if (loadBtn) loadBtn.disabled = true;
    try {
      const r = await fetch(
        "/admin/api/data?token=" + encodeURIComponent(token),
      );
      if (r.status === 401) {
        showError("Unauthorized — check that FAMDOC_ADMIN_WEB_TOKEN is set and matches.");
        if (dash) dash.classList.add("hidden");
        return;
      }
      if (!r.ok) {
        showError("Could not load data (" + r.status + ").");
        if (dash) dash.classList.add("hidden");
        return;
      }
      const data = await r.json();
      const s = data.stats || {};
      const statsEl = $("adm-stats");
      if (statsEl) {
        const rows = [
          ["Registered users", s.users_registered],
          ["Mini App opens (total)", s.miniapp_opens],
          ["Vaults with uploads", s.vaults_with_uploads],
          ["Total documents", s.total_documents],
        ];
        statsEl.innerHTML = rows
          .map(
            ([label, v]) =>
              `<div class="adm-stat-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(v ?? "—"))}</strong></div>`,
          )
          .join("");
      }
      const claimsEl = $("adm-claims");
      const claims = data.claims || [];
      if (claimsEl) {
        if (!claims.length) {
          claimsEl.innerHTML =
            '<p class="adm-empty">No pending claims.</p>';
        } else {
          claimsEl.innerHTML = claims
            .map((c) => {
              const uid = escapeHtml(String(c.user_id ?? ""));
              const name = escapeHtml(
                (c.display_name || "").trim() || "(no name)",
              );
              const un = c.username
                ? "@" + escapeHtml(c.username)
                : "(no username)";
              const tier = escapeHtml(
                formatUzs(c.price_uzs) +
                  " · +" +
                  String(c.slots_requested ?? "") +
                  " slots",
              );
              const when = escapeHtml(formatDate(c.claimed_at));
              const receiptSlot = c.has_receipt
                ? `<div class="adm-receipt-slot" data-user="${uid}">Loading receipt…</div>`
                : "";
              return `<article class="adm-claim">
              <dl>
                <dt>Telegram ID</dt><dd class="mono">${uid}</dd>
                <dt>Name</dt><dd>${name}</dd>
                <dt>Username</dt><dd>${un}</dd>
                <dt>Plan</dt><dd>${tier}</dd>
                <dt>Submitted</dt><dd>${when}</dd>
              </dl>
              ${receiptSlot}
            </article>`;
            })
            .join("");
          claims.forEach((c) => {
            if (!c.has_receipt) return;
            const slot = claimsEl.querySelector(
              `.adm-receipt-slot[data-user="${CSS.escape(String(c.user_id))}"]`,
            );
            if (!slot) return;
            const url =
              "/admin/api/receipt/" +
              encodeURIComponent(String(c.user_id)) +
              "?token=" +
              encodeURIComponent(token);
            if ((c.receipt_mime || "").toLowerCase().includes("pdf")) {
              const frame = document.createElement("iframe");
              frame.className = "adm-claim-receipt pdf";
              frame.title = "Payment receipt";
              frame.src = url;
              frame.referrerPolicy = "no-referrer";
              slot.replaceWith(frame);
            } else {
              const img = document.createElement("img");
              img.className = "adm-claim-receipt";
              img.alt = "Payment receipt";
              img.src = url;
              img.referrerPolicy = "no-referrer";
              slot.replaceWith(img);
            }
          });
        }
      }
      if (dash) dash.classList.remove("hidden");
    } catch {
      showError("Network error — try again.");
      if (dash) dash.classList.add("hidden");
    } finally {
      if (loadBtn) loadBtn.disabled = false;
    }
  }

  $("adm-load-btn")?.addEventListener("click", loadDashboard);

  $("adm-grant-btn")?.addEventListener("click", async () => {
    const token = currentToken();
    const uidRaw = ($("adm-grant-uid") && $("adm-grant-uid").value.trim()) || "";
    const slotsRaw =
      ($("adm-grant-slots") && $("adm-grant-slots").value.trim()) || "";
    const msgEl = $("adm-grant-msg");
    if (msgEl) {
      msgEl.hidden = true;
      msgEl.textContent = "";
    }
    const target_user_id = parseInt(uidRaw, 10);
    const slots = parseInt(slotsRaw, 10);
    if (!Number.isFinite(target_user_id) || target_user_id < 1) {
      if (msgEl) {
        msgEl.textContent = "Enter a valid Telegram user ID.";
        msgEl.hidden = false;
        msgEl.className = "adm-error";
      }
      return;
    }
    if (!Number.isFinite(slots) || slots < 1 || slots > 10000) {
      if (msgEl) {
        msgEl.textContent = "Slots must be between 1 and 10000.";
        msgEl.hidden = false;
        msgEl.className = "adm-error";
      }
      return;
    }
    if (!token) {
      if (msgEl) {
        msgEl.textContent = "Token required.";
        msgEl.hidden = false;
        msgEl.className = "adm-error";
      }
      return;
    }
    const btn = $("adm-grant-btn");
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(
        "/admin/api/grant?token=" + encodeURIComponent(token),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target_user_id, slots }),
        },
      );
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (msgEl) {
          msgEl.textContent =
            typeof j.detail === "string"
              ? j.detail
              : "Grant failed.";
          msgEl.hidden = false;
          msgEl.className = "adm-error";
        }
        return;
      }
      const cap =
        j.document_cap != null ? String(j.document_cap) : "—";
      if (msgEl) {
        msgEl.textContent =
          "Added " +
          String(j.slots_added ?? slots) +
          " slot(s). User document cap: " +
          cap +
          ".";
        msgEl.hidden = false;
        msgEl.className = "adm-ok";
      }
      await loadDashboard();
    } catch {
      if (msgEl) {
        msgEl.textContent = "Network error.";
        msgEl.hidden = false;
        msgEl.className = "adm-error";
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  const fromUrl = tokenFromUrl();
  const remembered = rememberedToken();
  const ti = $("adm-token-input");
  if (ti) {
    if (fromUrl) ti.value = fromUrl;
    else if (remembered) ti.value = remembered;
  }
  if (fromUrl || remembered) {
    loadDashboard();
  }
})();
