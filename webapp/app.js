(() => {
  if (!window.FamDocI18n) {
    console.error("FamDocI18n missing");
  }

  const tg = window.Telegram?.WebApp;
  let initData = "";

  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor("#e8f4fc");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#f0f7fb");
  }

  /**
   * Telegram passes auth in the URL hash (#tgWebAppData=...). The official SDK
   * reads it, but if the script fails to load (CDN blocked) we still need the raw string.
   */
  function tgWebAppDataFromHash() {
    try {
      const raw = (window.location.hash || "").replace(/^#/, "");
      if (!raw) return "";
      const qIndex = raw.indexOf("?");
      const queryPart = qIndex >= 0 ? raw.slice(qIndex + 1) : raw;
      const params = new URLSearchParams(queryPart);
      return params.get("tgWebAppData") || "";
    } catch {
      return "";
    }
  }

  function syncInitData() {
    initData = (tg && tg.initData) || tgWebAppDataFromHash() || "";
  }
  syncInitData();

  const $ = (sel) =>
    typeof sel === "string" ? document.querySelector(sel) : sel;

  function detectLang() {
    try {
      const saved = localStorage.getItem("famdoc_lang");
      if (saved && ["en", "uz", "ru"].includes(saved)) return saved;
    } catch (_) {}
    const u = tg?.initDataUnsafe?.user?.language_code;
    if (u && u.toLowerCase().startsWith("uz")) return "uz";
    if (u && u.toLowerCase().startsWith("ru")) return "ru";
    return "en";
  }

  const state = {
    categories: [],
    total: 0,
    activeCategory: "all",
    searchQ: "",
    bulkMode: false,
    selection: new Set(),
    currentDetail: null,
    uploadQueue: [],
    searchTimer: null,
    lang: detectLang(),
    contextMode: "private",
    documentLimit: 0,
    billingUpgrade: false,
    billingPriceLabel: "",
    billingSlotsPerPurchase: 0,
    billingMode: "telegram",
    telegramUserId: 0,
    billingManual: null,
    manualSelectedTier: null,
    isAdmin: false,
    billingReceiptBlobUrl: null,
    adminClaimUrls: [],
    vaultLocked: false,
    vaultCrypto: {},
    vaultKdfSalt: "",
    vaultState: "none",
  };

  function pack() {
    return (
      window.FamDocI18n?.[state.lang] ||
      window.FamDocI18n?.en ||
      {}
    );
  }

  function t(key) {
    const p = pack();
    const en = window.FamDocI18n?.en || {};
    return p[key] ?? en[key] ?? key;
  }

  function tfmt(key, vars) {
    let s = t(key);
    Object.entries(vars || {}).forEach(([k, v]) => {
      s = s.split(`{${k}}`).join(String(v));
    });
    return s;
  }

  function tCat(id) {
    const p = pack();
    const en = window.FamDocI18n?.en;
    return p.categories?.[id] ?? en?.categories?.[id] ?? id;
  }

  function applyStaticI18n() {
    document.documentElement.lang = state.lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const k = el.getAttribute("data-i18n");
      if (k) el.textContent = t(k);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const k = el.getAttribute("data-i18n-placeholder");
      if (k) el.placeholder = t(k);
    });
    const ls = $("#lang-select");
    if (ls) ls.value = state.lang;
  }

  function fillLangSelect() {
    const sel = $("#lang-select");
    if (!sel) return;
    const p = pack();
    sel.innerHTML = "";
    ["en", "uz", "ru"].forEach((code) => {
      const o = document.createElement("option");
      o.value = code;
      o.textContent = p[`lang_${code}`] || code;
      sel.appendChild(o);
    });
    sel.value = state.lang;
    sel.onchange = () => {
      state.lang = sel.value;
      try {
        localStorage.setItem("famdoc_lang", state.lang);
      } catch (_) {}
      applyStaticI18n();
      setFamilyHeader();
      renderSidebar();
      fillCategorySelects();
      updateBulkBar();
      if (
        state.billingMode === "manual" &&
        $("#billing-step-pick") &&
        !$("#billing-step-pick").classList.contains("hidden")
      ) {
        renderManualTierButtons();
      }
      if (state.manualSelectedTier) {
        const t = state.manualSelectedTier;
        const payLead = $("#billing-pay-lead");
        if (payLead) {
          payLead.textContent = tfmt("billingManualPayLead", {
            amount: formatUzs(t.price_uzs),
            slots: t.slots,
          });
        }
      }
    };
  }

  const headers = () => ({
    "X-Telegram-Init-Data": initData,
    "ngrok-skip-browser-warning": "69420",
  });

  /** Mini App + vault cookie: always send credentials for HttpOnly session. */
  function apiFetch(url, options = {}) {
    const extra = options.headers || {};
    const isForm =
      typeof FormData !== "undefined" && options.body instanceof FormData;
    const h = { ...headers(), ...extra };
    if (isForm) delete h["Content-Type"];
    return fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: h,
    });
  }

  /** Non-extractable AES key; in-memory only (cleared on navigation). */
  let vaultMemoryAesKey = null;

  function clearVaultMemoryKey() {
    vaultMemoryAesKey = null;
  }

  window.addEventListener("pagehide", clearVaultMemoryKey);

  /** Absolute URL with init in query — required for Telegram.WebApp.downloadFile (HTTPS). */
  function authFileDownloadUrl(docId) {
    const base = window.location.origin || "";
    if (!initData) return `${base}/api/documents/${docId}/file`;
    return `${base}/api/documents/${docId}/file?tgWebAppData=${encodeURIComponent(initData)}`;
  }

  function downloadBlobToDevice(blob, filename) {
    const url = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      queueMicrotask(() => URL.revokeObjectURL(url));
    }
  }

  async function fetchShareLinkJson(docId) {
    const r = await apiFetch(`/api/documents/${docId}/share-link`, { headers: headers() });
    if (!r.ok) return null;
    return r.json();
  }

  function showToast(msg, ms = 3200) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => el.classList.add("hidden"), ms);
  }

  function mimeIcon(mime) {
    if (!mime) return "📄";
    if (mime.startsWith("image/")) return "🖼️";
    if (mime.includes("pdf")) return "📕";
    if (mime.includes("word") || mime.includes("document")) return "📝";
    return "📄";
  }

  function readCryptoHeaders(r) {
    return {
      enc: r.headers.get("X-FamDoc-Encrypted") === "1",
      iv: r.headers.get("X-FamDoc-Crypto-IV") || "",
      tag: r.headers.get("X-FamDoc-Crypto-Tag") || "",
      previewEnc: r.headers.get("X-FamDoc-Preview-Encrypted") === "1",
    };
  }

  function docIsEncrypted(doc) {
    return doc.encryption_state === "encrypted";
  }

  function isLegacyPlaintext(doc) {
    return (doc.encryption_state || "legacy_plaintext") === "legacy_plaintext";
  }

  async function getVaultAesKey() {
    return vaultMemoryAesKey;
  }

  async function decryptFileBlob(doc, r, blob) {
    if (!docIsEncrypted(doc)) return blob;
    const h = readCryptoHeaders(r);
    if (!h.enc) return blob;
    const key = await getVaultAesKey();
    if (!key) throw new Error("no_vault_key");
    const V = window.FamDocVaultCrypto;
    const ab = await blob.arrayBuffer();
    const plain = await V.decryptBuffer(key, h.iv, h.tag, ab);
    const mt = doc.mime_type || "application/octet-stream";
    return new Blob([plain], { type: mt });
  }

  async function decryptPreviewBlob(doc, r, blob) {
    const h = readCryptoHeaders(r);
    if (!h.previewEnc) return blob;
    const key = await getVaultAesKey();
    if (!key) throw new Error("no_vault_key");
    const V = window.FamDocVaultCrypto;
    const ab = await blob.arrayBuffer();
    const plain = await V.decryptBuffer(key, h.iv, h.tag, ab);
    return new Blob([plain], { type: "image/jpeg" });
  }

  async function buildEncryptedPartsFromPlainBlob(plainBlob, name, mime) {
    const V = window.FamDocVaultCrypto;
    const key = await getVaultAesKey();
    if (!V || !key) throw new Error("no_vault_key");
    const ab = await plainBlob.arrayBuffer();
    const { ivB64, tagB64, ciphertext } = await V.encryptBuffer(key, ab);
    const file = new File([plainBlob], name, {
      type: mime || "application/octet-stream",
    });
    const prevBlob = await V.maybeImagePreviewBlob(file, mime || "");
    let previewIv = "";
    let previewTag = "";
    let previewBlob = null;
    if (prevBlob) {
      const pab = await prevBlob.arrayBuffer();
      const pe = await V.encryptBuffer(key, pab);
      previewIv = pe.ivB64;
      previewTag = pe.tagB64;
      previewBlob = new Blob([pe.ciphertext]);
    }
    return {
      blob: new Blob([ciphertext]),
      ivB64,
      tagB64,
      previewIv,
      previewTag,
      previewBlob,
    };
  }

  function scheduleLegacyMigrate(doc, plaintextBlob) {
    if (!isLegacyPlaintext(doc) || !state.vaultKdfSalt) return;
    void (async () => {
      const key = await getVaultAesKey();
      if (!key) return;
      try {
        const enc = await buildEncryptedPartsFromPlainBlob(
          plaintextBlob,
          doc.original_filename,
          doc.mime_type,
        );
        const fd = new FormData();
        fd.append("crypto_iv", enc.ivB64);
        fd.append("crypto_tag", enc.tagB64);
        fd.append("file", enc.blob, doc.original_filename);
        if (enc.previewBlob) {
          fd.append("preview_crypto_iv", enc.previewIv);
          fd.append("preview_crypto_tag", enc.previewTag);
          fd.append("preview_file", enc.previewBlob, "preview.enc.jpg");
        }
        const r = await apiFetch(`/api/documents/${doc.id}/migrate-crypto`, {
          method: "POST",
          body: fd,
          headers: headers(),
        });
        if (r.ok) {
          doc.encryption_state = "encrypted";
          loadDocuments().catch(() => {});
        }
      } catch {
        /* retry on next access */
      }
    })();
  }

  async function buildEncryptedUploadParts(file) {
    const V = window.FamDocVaultCrypto;
    const key = await getVaultAesKey();
    if (!V || !key) throw new Error("no_vault_key");
    const ab = await file.arrayBuffer();
    const { ivB64, tagB64, ciphertext } = await V.encryptBuffer(key, ab);
    const prevBlob = await V.maybeImagePreviewBlob(file, file.type || "");
    let previewIv = "";
    let previewTag = "";
    let previewBlob = null;
    if (prevBlob) {
      const pab = await prevBlob.arrayBuffer();
      const pe = await V.encryptBuffer(key, pab);
      previewIv = pe.ivB64;
      previewTag = pe.tagB64;
      previewBlob = new Blob([pe.ciphertext]);
    }
    return {
      blob: new Blob([ciphertext]),
      ivB64,
      tagB64,
      previewIv,
      previewTag,
      previewBlob,
    };
  }

  function formatDate(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return d.toLocaleDateString(state.lang === "ru" ? "ru-RU" : state.lang === "uz" ? "uz-UZ" : "en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    } catch {
      return iso.slice(0, 10);
    }
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }

  async function apiConfig() {
    const r = await apiFetch("/api/config", {
      headers: { "ngrok-skip-browser-warning": "69420" },
    });
    if (!r.ok) throw new Error("config");
    return r.json();
  }

  async function apiBootstrap() {
    const r = await apiFetch("/api/bootstrap", { headers: headers() });
    if (!r.ok) throw new Error("bootstrap");
    return r.json();
  }

  async function apiAdminStats() {
    const r = await apiFetch("/api/admin/stats", { headers: headers() });
    if (!r.ok) throw new Error("admin_stats");
    return r.json();
  }

  async function apiAdminManualClaims() {
    const r = await apiFetch("/api/admin/manual-claims", { headers: headers() });
    if (!r.ok) throw new Error("admin_claims");
    return r.json();
  }

  async function apiDocuments() {
    const p = new URLSearchParams();
    if (state.activeCategory && state.activeCategory !== "all") {
      p.set("category", state.activeCategory);
    }
    if (state.searchQ.trim()) {
      p.set("q", state.searchQ.trim());
    }
    const q = p.toString();
    const r = await apiFetch("/api/documents" + (q ? "?" + q : ""), {
      headers: headers(),
    });
    if (!r.ok) throw new Error("documents");
    return r.json();
  }

  function setFamilyHeader() {
    const u = tg?.initDataUnsafe?.user;
    const ch = tg?.initDataUnsafe?.chat;
    const nameEl = $("#family-name");
    const metaEl = $("#family-meta");
    if (!nameEl || !metaEl) return;
    const title = t("headerTitle");
    const ctype = (ch?.type || "").toLowerCase();
    const isGroup =
      ctype === "group" || ctype === "supergroup" || ctype === "channel";
    nameEl.textContent = title;
    if (isGroup && ch?.title) {
      metaEl.textContent = ch.title;
    } else if (u?.username) {
      metaEl.textContent = "@" + u.username;
    } else if (u?.id != null) {
      metaEl.textContent = tfmt("memberTelegramId", { id: u.id });
    } else {
      metaEl.textContent = "";
    }
  }

  let listThumbUrls = [];
  let previewUrls = [];

  function revokeListThumbs() {
    listThumbUrls.forEach((u) => URL.revokeObjectURL(u));
    listThumbUrls = [];
  }

  function revokePreviews() {
    previewUrls.forEach((u) => URL.revokeObjectURL(u));
    previewUrls = [];
  }

  function renderSidebar() {
    const nav = $("#sidebar-nav");
    if (!nav) return;
    nav.innerHTML = "";
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className =
      "nav-item" + (state.activeCategory === "all" ? " active" : "");
    allBtn.innerHTML = `<span class="left"><span>📚</span> ${escapeHtml(t("allDocuments"))}</span><span class="count">${state.total}</span>`;
    allBtn.addEventListener("click", () => {
      state.activeCategory = "all";
      renderSidebar();
      loadDocuments();
      closeSidebarMobile();
    });
    nav.appendChild(allBtn);

    state.categories.forEach((c) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className =
        "nav-item" + (state.activeCategory === c.id ? " active" : "");
      const label = tCat(c.id);
      b.innerHTML = `<span class="left"><span>${c.emoji}</span> ${escapeHtml(label)}</span><span class="count">${c.count}</span>`;
      b.addEventListener("click", () => {
        state.activeCategory = c.id;
        renderSidebar();
        loadDocuments();
        closeSidebarMobile();
      });
      nav.appendChild(b);
    });
  }

  function closeSidebarMobile() {
    $("#sidebar")?.classList.add("collapsed-mobile");
    $("#sidebar-scrim")?.classList.remove("visible");
    $("#sidebar-scrim")?.setAttribute("hidden", "");
  }

  function openSidebarMobile() {
    $("#sidebar")?.classList.remove("collapsed-mobile");
    const scrim = $("#sidebar-scrim");
    if (scrim) {
      scrim.removeAttribute("hidden");
      scrim.classList.add("visible");
    }
  }

  function loadThumbForCard(thumbEl, doc) {
    if (!initData) return;
    apiFetch(`/api/documents/${doc.id}/preview`, { headers: headers() })
      .then(async (r) => {
        if (!r.ok) return null;
        let blob = await r.blob();
        if (readCryptoHeaders(r).previewEnc) {
          try {
            blob = await decryptPreviewBlob(doc, r, blob);
          } catch {
            return null;
          }
        }
        return blob;
      })
      .then((blob) => {
        if (!blob || !thumbEl.isConnected) return;
        const url = URL.createObjectURL(blob);
        listThumbUrls.push(url);
        thumbEl.innerHTML = "";
        const img = document.createElement("img");
        img.src = url;
        img.alt = "";
        thumbEl.appendChild(img);
      })
      .catch(() => {});
  }

  async function loadDocuments() {
    const grid = $("#doc-grid");
    const empty = $("#doc-empty");
    if (!initData) {
      grid.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    let data;
    try {
      data = await apiDocuments();
    } catch {
      showToast(t("toastDocuments"));
      return;
    }
    const items = data.items || [];
    revokeListThumbs();
    revokePreviews();
    grid.innerHTML = "";
    empty.classList.toggle("hidden", items.length > 0);

    items.forEach((doc) => {
      const card = document.createElement("article");
      card.className = "doc-card";
      card.dataset.id = String(doc.id);
      if (state.selection.has(doc.id)) card.classList.add("selected");

      if (state.bulkMode) {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "card-check";
        cb.checked = state.selection.has(doc.id);
        cb.addEventListener("click", (e) => e.stopPropagation());
        cb.addEventListener("change", () => {
          if (cb.checked) state.selection.add(doc.id);
          else state.selection.delete(doc.id);
          card.classList.toggle("selected", cb.checked);
          updateBulkBar();
        });
        card.appendChild(cb);
      }

      const lock = document.createElement("span");
      lock.className = "lock-mini";
      lock.title = t("storedSecurely");
      lock.textContent = "🔒";
      card.appendChild(lock);

      const thumb = document.createElement("div");
      thumb.className = "doc-thumb";
      thumb.textContent = mimeIcon(doc.mime_type);
      loadThumbForCard(thumb, doc);

      const title = document.createElement("div");
      title.className = "doc-title";
      title.textContent = doc.original_filename;

      const meta = document.createElement("div");
      meta.className = "doc-meta";
      meta.textContent = `${doc.emoji} ${tCat(doc.category)} · ${formatDate(doc.uploaded_at)}`;

      const tags = document.createElement("div");
      tags.className = "doc-tags";
      tags.textContent = doc.tags
        ? "🏷 " + doc.tags
        : doc.notes
          ? "📝 " + doc.notes.slice(0, 60) + (doc.notes.length > 60 ? "…" : "")
          : "";

      card.appendChild(thumb);
      card.appendChild(title);
      card.appendChild(meta);
      if (tags.textContent) card.appendChild(tags);

      card.addEventListener("click", () => {
        if (state.bulkMode) {
          const cb = card.querySelector(".card-check");
          if (cb) {
            cb.checked = !cb.checked;
            cb.dispatchEvent(new Event("change"));
          }
          return;
        }
        openDetail(doc);
      });

      grid.appendChild(card);
    });
  }

  function updateBulkBar() {
    const bar = $("#bulk-bar");
    const n = state.selection.size;
    const el = $("#bulk-count");
    if (el) el.textContent = tfmt("bulkSelected", { n });
    bar?.classList.toggle("hidden", !state.bulkMode);
  }

  async function loadCategoryLabels() {
    const data = await apiConfig();
    const rows = data.categories || [];
    state.categories = rows.map((c) => ({
      id: c.id,
      label: tCat(c.id),
      emoji: c.emoji,
      count: 0,
    }));
    state.total = 0;
    renderSidebar();
  }

  function atDocumentLimit() {
    return state.documentLimit > 0 && state.total >= state.documentLimit;
  }

  function uploadSlotsRemaining() {
    if (state.documentLimit <= 0) return Infinity;
    return Math.max(0, state.documentLimit - state.total);
  }

  function updateAddDocButton() {
    const btn = $("#btn-add-doc");
    if (!btn) return;
    const at = atDocumentLimit();
    const canPurchase = at && state.billingUpgrade;
    btn.disabled = at && !canPurchase;
    btn.setAttribute("aria-disabled", at && !canPurchase ? "true" : "false");
  }

  function formatUzs(n) {
    try {
      return new Intl.NumberFormat("en-US").format(Number(n)) + " UZS";
    } catch {
      return String(n) + " UZS";
    }
  }

  function revokeBillingReceiptPreview() {
    if (state.billingReceiptBlobUrl) {
      URL.revokeObjectURL(state.billingReceiptBlobUrl);
      state.billingReceiptBlobUrl = null;
    }
  }

  function clearBillingReceipt() {
    revokeBillingReceiptPreview();
    const inp = $("#billing-receipt-input");
    const fn = $("#billing-receipt-filename");
    const prev = $("#billing-receipt-preview");
    if (inp) inp.value = "";
    if (fn) fn.textContent = "";
    if (prev) {
      prev.innerHTML = "";
      prev.classList.add("hidden");
    }
  }

  function onBillingReceiptFileChange() {
    const inp = $("#billing-receipt-input");
    const file = inp?.files?.[0];
    const fn = $("#billing-receipt-filename");
    const prev = $("#billing-receipt-preview");
    revokeBillingReceiptPreview();
    if (!file) {
      if (fn) fn.textContent = "";
      if (prev) {
        prev.innerHTML = "";
        prev.classList.add("hidden");
      }
      return;
    }
    if (fn) fn.textContent = file.name || "";
    if (!prev) return;
    prev.innerHTML = "";
    prev.classList.remove("hidden");
    if ((file.type || "").startsWith("image/")) {
      const url = URL.createObjectURL(file);
      state.billingReceiptBlobUrl = url;
      prev.innerHTML = `<img src="${url}" alt="" />`;
    } else {
      prev.innerHTML = `<p class="field-hint">${escapeHtml(t("billingReceiptPdfPicked"))}</p>`;
    }
  }

  function renderManualTierButtons() {
    const wrap = $("#billing-tier-buttons");
    if (!wrap) return;
    wrap.innerHTML = "";
    const tiers = (state.billingManual && state.billingManual.tiers) || [];
    tiers.forEach((tier) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "tier-pick-btn";
      btn.textContent = tfmt("billingTierButton", {
        price: formatUzs(tier.price_uzs),
        slots: tier.slots,
      });
      btn.addEventListener("click", () => showManualPayStep(tier));
      wrap.appendChild(btn);
    });
  }

  function showManualPickStep() {
    state.manualSelectedTier = null;
    $("#billing-step-pick")?.classList.remove("hidden");
    $("#billing-step-pay")?.classList.add("hidden");
    clearBillingReceipt();
    renderManualTierButtons();
  }

  function showManualPayStep(tier) {
    state.manualSelectedTier = tier;
    clearBillingReceipt();
    $("#billing-step-pick")?.classList.add("hidden");
    $("#billing-step-pay")?.classList.remove("hidden");
    const m = state.billingManual || {};
    const cardEl = $("#billing-card-display");
    const refEl = $("#billing-payment-ref");
    const payLead = $("#billing-pay-lead");
    const instrEl = $("#billing-transfer-instructions");
    if (cardEl) cardEl.value = m.card || "";
    const uid = state.telegramUserId || 0;
    if (refEl) refEl.value = uid ? `FAMDOC-${uid}` : "";
    if (payLead) {
      payLead.textContent = tfmt("billingManualPayLead", {
        amount: formatUzs(tier.price_uzs),
        slots: tier.slots,
      });
    }
    if (instrEl) {
      const ins = (m.instructions || "").trim();
      if (ins) {
        instrEl.textContent = ins;
        instrEl.hidden = false;
      } else {
        instrEl.textContent = "";
        instrEl.hidden = true;
      }
    }
    syncInitData();
  }

  function fillManualBilling() {
    showManualPickStep();
  }

  function updateBillingPanelCopy() {
    const lead = $("#billing-limit-lead");
    const hint = $("#billing-limit-hint");
    const bbtn = $("#btn-buy-slots");
    if (lead) {
      lead.textContent = tfmt("billingLimitLead", { limit: state.documentLimit });
    }
    if (hint) {
      hint.textContent = tfmt("billingLimitHint", {
        n: state.billingSlotsPerPurchase,
        price: state.billingPriceLabel,
      });
    }
    if (bbtn) {
      bbtn.textContent = tfmt("billingBuyButton", { price: state.billingPriceLabel });
    }
  }

  async function refreshBootstrap() {
    if (!initData) return;
    const data = await apiBootstrap();
    state.categories = (data.categories || []).map((c) => ({
      ...c,
      label: tCat(c.id),
    }));
    state.total = data.total ?? 0;
    state.documentLimit = data.document_limit ?? 0;
    const b = data.billing || {};
    state.billingUpgrade = !!b.upgrade_enabled;
    state.billingPriceLabel = b.price_label || "";
    state.billingSlotsPerPurchase = b.slots_per_purchase || 0;
    state.billingMode = b.mode || "telegram";
    state.telegramUserId = data.telegram_user_id || 0;
    state.billingManual = b.manual || null;
    state.contextMode = data.context_mode || "private";
    const hideFam = state.contextMode !== "private";
    $("#btn-family-sidebar")?.classList.toggle("hidden", hideFam);
    state.isAdmin = !!data.is_admin;
    $("#btn-admin-sidebar")?.classList.toggle("hidden", !state.isAdmin);
    state.vaultLocked = !!data.vault_locked;
    state.vaultCrypto = data.vault_crypto || {};
    state.vaultKdfSalt = (state.vaultCrypto.kdf_salt_b64 || "").trim();
    state.vaultState = state.vaultCrypto.state || "none";
    renderSidebar();
    setFamilyHeader();
    updateAddDocButton();
  }

  let vaultModalMode = "unlock";

  function openVaultModal(mode) {
    vaultModalMode = mode;
    const m = $("#vault-modal");
    const titleEl = $("#vault-modal-title");
    const lead = $("#vault-modal-lead");
    const p2 = $("#vault-pass-2");
    const p2lab = $("#vault-pass-2-label");
    const sub = $("#vault-modal-submit");
    if (!m || !titleEl || !lead || !p2 || !sub) return;
    m.classList.remove("hidden");
    $("#vault-pass-1").value = "";
    $("#vault-pass-2").value = "";
    $("#vault-modal-err").hidden = true;
    if (mode === "setup") {
      titleEl.textContent = t("vaultCreateTitle");
      lead.textContent = t("vaultCreateLead");
      p2.classList.remove("hidden");
      p2lab?.classList.remove("hidden");
      sub.textContent = t("vaultCreateSubmit");
    } else {
      titleEl.textContent = t("vaultUnlockTitle");
      lead.textContent = t("vaultUnlockLead");
      p2.classList.add("hidden");
      p2lab?.classList.add("hidden");
      sub.textContent = t("vaultUnlockSubmit");
    }
  }

  function closeVaultModal() {
    $("#vault-modal")?.classList.add("hidden");
  }

  async function runVaultGate() {
    const V = window.FamDocVaultCrypto;
    if (!V) return;
    if (state.vaultState === "none") {
      openVaultModal("setup");
      return;
    }
    if (vaultMemoryAesKey) return;
    openVaultModal("unlock");
  }

  $("#vault-modal-submit")?.addEventListener("click", async () => {
    const errEl = $("#vault-modal-err");
    const p1 = $("#vault-pass-1")?.value || "";
    const p2 = $("#vault-pass-2")?.value || "";
    const V = window.FamDocVaultCrypto;
    if (!V) return;
    errEl.hidden = true;
    if (vaultModalMode === "setup") {
      if (p1.length < 10) {
        errEl.textContent = t("vaultPassTooShort");
        errEl.hidden = false;
        return;
      }
      if (p1 !== p2) {
        errEl.textContent = t("vaultPassMismatch");
        errEl.hidden = false;
        return;
      }
      const r = await postJson("/api/vault/password", { password: p1 });
      if (!r.ok) {
        errEl.textContent = t("vaultSetupFail");
        errEl.hidden = false;
        return;
      }
      const j = await r.json();
      const salt = j.vault_crypto?.kdf_salt_b64 || "";
      try {
        vaultMemoryAesKey = await V.deriveAesKey(p1, salt);
      } catch {
        errEl.textContent = t("vaultCryptoFail");
        errEl.hidden = false;
        return;
      }
      closeVaultModal();
      try {
        await refreshBootstrap();
        await loadDocuments();
      } catch {
        showToast(t("toastConnection"));
      }
      return;
    }
    const r2 = await postJson("/api/vault/unlock", { password: p1 });
    if (!r2.ok) {
      errEl.textContent =
        r2.status === 429
          ? t("vaultRateLimited")
          : t("vaultUnlockFail");
      errEl.hidden = false;
      return;
    }
    const j2 = await r2.json();
    const salt2 = j2.vault_crypto?.kdf_salt_b64 || state.vaultKdfSalt;
    try {
      vaultMemoryAesKey = await V.deriveAesKey(p1, salt2);
    } catch {
      errEl.textContent = t("vaultCryptoFail");
      errEl.hidden = false;
      return;
    }
    closeVaultModal();
    try {
      await refreshBootstrap();
      await loadDocuments();
    } catch {
      showToast(t("toastConnection"));
    }
  });

  async function boot() {
    fillLangSelect();
    applyStaticI18n();

    try {
      await loadCategoryLabels();
    } catch {
      showToast(t("toastCategories"));
      return;
    }

    for (let i = 0; i < 200; i++) {
      syncInitData();
      if (initData) break;
      await new Promise((r) => setTimeout(r, 50));
    }

    if (!initData) {
      $("#no-tg")?.classList.remove("hidden");
      return;
    }
    $("#no-tg")?.classList.add("hidden");

    try {
      await refreshBootstrap();
      await runVaultGate();
      if (!$("#vault-modal")?.classList.contains("hidden")) return;
      await loadDocuments();
    } catch {
      showToast(t("toastConnection"));
    }
  }

  $("#search-input")?.addEventListener("input", (e) => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.searchQ = e.target.value;
      loadDocuments();
    }, 320);
  });

  $("#menu-toggle")?.addEventListener("click", openSidebarMobile);
  $("#sidebar-close")?.addEventListener("click", closeSidebarMobile);
  $("#sidebar-scrim")?.addEventListener("click", closeSidebarMobile);

  function fillCategorySelects() {
    const up = $("#up-category");
    const det = $("#detail-category");
    const mv = $("#move-target");
    [up, det, mv].forEach((sel) => {
      if (!sel) return;
      sel.innerHTML = "";
      state.categories.forEach((c) => {
        const o = document.createElement("option");
        o.value = c.id;
        o.textContent = `${c.emoji} ${tCat(c.id)}`;
        sel.appendChild(o);
      });
    });
  }

  function openUploadPanel() {
    const at = atDocumentLimit();
    const upgradeOnly = at && state.billingUpgrade;
    const titleEl = $("#upload-panel-title");
    const normal = $("#upload-normal-fields");
    const lim = $("#upload-limit-block");
    const runBtn = $("#btn-upload-run");

    if (at && !state.billingUpgrade) {
      showToast(tfmt("uploadLimitReached", { limit: state.documentLimit }));
      return;
    }

    if (upgradeOnly) {
      if (titleEl) titleEl.textContent = t("billingPanelTitle");
      normal?.classList.add("hidden");
      lim?.classList.remove("hidden");
      runBtn?.classList.add("hidden");
      const inv = $("#billing-invoice-flow");
      const man = $("#billing-manual-flow");
      if (state.billingMode === "manual") {
        inv?.classList.add("hidden");
        man?.classList.remove("hidden");
        fillManualBilling();
      } else {
        man?.classList.add("hidden");
        inv?.classList.remove("hidden");
        updateBillingPanelCopy();
      }
    } else {
      if (titleEl) titleEl.textContent = t("uploadTitle");
      normal?.classList.remove("hidden");
      lim?.classList.add("hidden");
      runBtn?.classList.remove("hidden");
      fillCategorySelects();
      state.uploadQueue = [];
      $("#file-input").value = "";
      $("#cam-input").value = "";
      $("#up-display-name").value = "";
      $("#up-tags").value = "";
      $("#up-notes").value = "";
      if (runBtn) {
        runBtn.disabled = true;
        runBtn.textContent = t("uploadQueue");
      }
      $("#progress-wrap")?.classList.add("hidden");
    }

    $("#upload-panel")?.classList.remove("hidden");
    updateAddDocButton();
  }

  function closeUploadPanel() {
    $("#upload-panel")?.classList.add("hidden");
    const titleEl = $("#upload-panel-title");
    if (titleEl) titleEl.textContent = t("uploadTitle");
    $("#upload-normal-fields")?.classList.remove("hidden");
    $("#upload-limit-block")?.classList.add("hidden");
    $("#billing-invoice-flow")?.classList.add("hidden");
    $("#billing-manual-flow")?.classList.add("hidden");
    $("#billing-step-pay")?.classList.add("hidden");
    $("#billing-step-pick")?.classList.remove("hidden");
    clearBillingReceipt();
    $("#btn-upload-run")?.classList.remove("hidden");
  }

  $("#btn-add-doc")?.addEventListener("click", openUploadPanel);
  $("#upload-close")?.addEventListener("click", closeUploadPanel);

  $("#file-input")?.addEventListener("change", (e) => {
    const files = e.target.files;
    const dn = $("#up-display-name");
    if (files?.length === 1 && dn && !dn.value.trim()) {
      dn.value = files[0].name || "";
    } else if (files?.length > 1 && dn) {
      dn.value = "";
    }
    addFilesToQueue(files);
  });
  $("#cam-input")?.addEventListener("change", (e) => {
    const files = e.target.files;
    const dn = $("#up-display-name");
    if (files?.length === 1 && dn && !dn.value.trim()) {
      dn.value = files[0].name || "";
    }
    addFilesToQueue(files);
  });
  $("#btn-camera")?.addEventListener("click", () => $("#cam-input")?.click());
  $("#btn-files")?.addEventListener("click", () => $("#file-input")?.click());

  function addFilesToQueue(fileList) {
    if (!fileList?.length) return;
    const picked = Array.from(fileList);
    const cap =
      uploadSlotsRemaining() === Infinity
        ? picked.length
        : Math.max(0, uploadSlotsRemaining() - state.uploadQueue.length);
    const slice = picked.slice(0, cap);
    if (slice.length < picked.length) {
      showToast(
        tfmt("toastUploadQueueTrimmed", {
          added: slice.length,
          picked: picked.length,
          limit: state.documentLimit,
        }),
      );
    }
    if (!slice.length) return;
    state.uploadQueue.push(...slice);
    $("#btn-upload-run").disabled = state.uploadQueue.length === 0;
    $("#btn-upload-run").textContent =
      state.uploadQueue.length === 0
        ? t("uploadQueue")
        : tfmt("uploadNFiles", { n: state.uploadQueue.length });
  }

  const dz = $("#drop-zone");
  ["dragenter", "dragover"].forEach((ev) =>
    dz?.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.add("drag");
    }),
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz?.addEventListener(ev, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.remove("drag");
    }),
  );
  dz?.addEventListener("drop", (e) => {
    const fl = e.dataTransfer?.files;
    if (fl?.length) {
      const dn = $("#up-display-name");
      if (fl.length === 1 && dn && !dn.value.trim()) {
        dn.value = fl[0].name || "";
      } else if (fl.length > 1 && dn) {
        dn.value = "";
      }
      addFilesToQueue(fl);
    }
  });

  $("#btn-upload-run")?.addEventListener("click", async () => {
    if (!state.uploadQueue.length || !initData) return;
    const cat = $("#up-category").value;
    const tags = $("#up-tags").value.trim();
    const notes = $("#up-notes").value.trim();
    const displayLine = $("#up-display-name")?.value?.trim() || "";
    const catLabel = tCat(cat);
    const pw = $("#progress-wrap");
    const pf = $("#progress-fill");
    const pt = $("#progress-text");
    pw?.classList.remove("hidden");
    let tips = 0;
    let uploaded = 0;
    let errorExit = false;
    const total = state.uploadQueue.length;
    for (let i = 0; i < total; i++) {
      const file = state.uploadQueue[i];
      pt.textContent = tfmt("uploading", { cur: i + 1, total });
      pf.style.width = `${((i + 0.5) / total) * 100}%`;
      const fd = new FormData();
      fd.append("category", cat);
      fd.append("tags", tags);
      fd.append("notes", notes);
      const useE2e =
        state.vaultState === "unlocked" &&
        !!state.vaultKdfSalt &&
        !!vaultMemoryAesKey;
      if (useE2e) {
        let enc;
        try {
          enc = await buildEncryptedUploadParts(file);
        } catch {
          showToast(t("vaultCryptoFail"));
          errorExit = true;
          break;
        }
        fd.append("crypto_mode", "e2e");
        fd.append("crypto_iv", enc.ivB64);
        fd.append("crypto_tag", enc.tagB64);
        fd.append("file", enc.blob, file.name);
        if (enc.previewBlob) {
          fd.append("preview_crypto_iv", enc.previewIv);
          fd.append("preview_crypto_tag", enc.previewTag);
          fd.append("preview_file", enc.previewBlob, "preview.enc.jpg");
        }
      } else {
        fd.append("crypto_mode", "plaintext");
        fd.append("file", file);
      }
      if (total === 1 && displayLine) {
        fd.append("display_name", displayLine);
      } else {
        fd.append("display_name", "");
      }
      const r = await apiFetch("/api/upload", {
        method: "POST",
        headers: headers(),
        body: fd,
      });
      if (!r.ok) {
        let detail = "";
        try {
          const errBody = await r.json();
          detail = errBody.detail || "";
        } catch (_) {
          /* ignore */
        }
        if (uploaded) {
          let msg = tfmt("toastUploaded", { n: uploaded, cat: catLabel });
          if (tips) msg += t("toastUploadTip");
          showToast(msg);
        }
        if (r.status === 503 && detail === "storage_access_denied") {
          showToast(t("toastStorageDenied"));
        } else if (r.status === 402 || detail === "document_limit_reached") {
          showToast(tfmt("toastUploadLimit", { limit: state.documentLimit }));
        } else {
          showToast(t("toastUploadFail"));
        }
        errorExit = true;
        break;
      }
      const j = await r.json();
      uploaded++;
      if (j.suggested_match === false) tips++;
    }
    pf.style.width = errorExit ? `${Math.max(8, (uploaded / total) * 100)}%` : "100%";
    pt.textContent = t("done");
    if (errorExit) {
      tg?.HapticFeedback?.notificationOccurred?.("error");
    } else {
      tg?.HapticFeedback?.notificationOccurred?.("success");
      let msg = tfmt("toastUploaded", { n: uploaded, cat: catLabel });
      if (tips) msg += t("toastUploadTip");
      showToast(msg);
    }
    state.uploadQueue = [];
    $("#btn-upload-run").disabled = true;
    $("#btn-upload-run").textContent = t("uploadQueue");
    closeUploadPanel();
    await refreshBootstrap();
    state.activeCategory = cat;
    renderSidebar();
    await loadDocuments();
    setTimeout(() => pw?.classList.add("hidden"), 600);
  });

  function detailPdfIframeHtml(blobUrl) {
    const u = String(blobUrl).replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    return `<div class="detail-preview-frame detail-preview-pdf"><iframe class="detail-iframe" src="${u}#view=FitH&toolbar=0" title="PDF"></iframe></div>`;
  }

  function openDetail(doc) {
    state.currentDetail = doc;
    $("#detail-modal")?.classList.remove("hidden");
    $("#detail-name").value = doc.original_filename;
    $("#detail-tags").value = doc.tags || "";
    $("#detail-notes").value = doc.notes || "";
    fillCategorySelects();
    $("#detail-category").value = doc.category;
    const prev = $("#detail-preview");
    revokePreviews();
    prev.innerHTML = `<div class="detail-preview-frame"><div class="ph">${mimeIcon(doc.mime_type)}</div></div>`;

    const mime = doc.mime_type || "";
    const loadBlob = () =>
      apiFetch(`/api/documents/${doc.id}/file`, { headers: headers() }).then(
        async (r) => {
          if (!r.ok) return Promise.reject();
          let blob = await r.blob();
          if (docIsEncrypted(doc)) {
            blob = await decryptFileBlob(doc, r, blob);
          }
          if (isLegacyPlaintext(doc)) {
            scheduleLegacyMigrate(doc, blob);
          }
          return blob;
        },
      );

    const narrowForPdf =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 700px)").matches;

    if (mime.startsWith("image/")) {
      loadBlob()
        .then((blob) => {
          const url = URL.createObjectURL(blob);
          previewUrls.push(url);
          prev.innerHTML = `<div class="detail-preview-frame detail-preview-image"><img src="${url}" alt="" class="detail-img" decoding="async" /></div>`;
        })
        .catch(() => {});
    } else if (mime.includes("pdf")) {
      const showRasterPdf =
        narrowForPdf && doc.has_preview === true;
      if (showRasterPdf) {
        apiFetch(`/api/documents/${doc.id}/preview`, { headers: headers() })
          .then(async (r) => {
            if (!r.ok) return Promise.reject();
            let blob = await r.blob();
            if (readCryptoHeaders(r).previewEnc) {
              blob = await decryptPreviewBlob(doc, r, blob);
            }
            return blob;
          })
          .then((blob) => {
            const url = URL.createObjectURL(blob);
            previewUrls.push(url);
            prev.innerHTML = `<div class="detail-preview-frame detail-preview-image"><img src="${url}" alt="" class="detail-img" decoding="async" /></div>`;
          })
          .catch(() => {
            loadBlob()
              .then((blob) => {
                const url = URL.createObjectURL(blob);
                previewUrls.push(url);
                prev.innerHTML = detailPdfIframeHtml(url);
              })
              .catch(() => {});
          });
      } else {
        loadBlob()
          .then((blob) => {
            const url = URL.createObjectURL(blob);
            previewUrls.push(url);
            prev.innerHTML = detailPdfIframeHtml(url);
          })
          .catch(() => {});
      }
    }
  }

  function closeDetail() {
    $("#detail-modal")?.classList.add("hidden");
    state.currentDetail = null;
    revokePreviews();
    $("#detail-preview").innerHTML = "";
  }

  $("#detail-close")?.addEventListener("click", closeDetail);
  $("#detail-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "detail-modal") closeDetail();
  });

  $("#detail-save")?.addEventListener("click", async () => {
    const doc = state.currentDetail;
    if (!doc) return;
    const body = {
      original_filename: $("#detail-name").value.trim(),
      category: $("#detail-category").value,
      tags: $("#detail-tags").value.trim(),
      notes: $("#detail-notes").value.trim(),
    };
    const r = await apiFetch(`/api/documents/${doc.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      showToast(t("toastSaveFail"));
      return;
    }
    tg?.HapticFeedback?.impactOccurred?.("light");
    showToast(t("toastSaved"));
    closeDetail();
    await refreshBootstrap();
    await loadDocuments();
  });

  $("#detail-download")?.addEventListener("click", async () => {
    const doc = state.currentDetail;
    if (!doc) return;
    syncInitData();
    const fname = doc.original_filename;
    if (!docIsEncrypted(doc) && typeof tg?.downloadFile === "function" && initData) {
      tg.downloadFile(
        { url: authFileDownloadUrl(doc.id), file_name: fname },
        (accepted) => {
          if (accepted === false) showToast(t("toastDownloadFail"));
        },
      );
      return;
    }
    const r = await apiFetch(`/api/documents/${doc.id}/file`, { headers: headers() });
    if (!r.ok) {
      showToast(t("toastDownloadFail"));
      return;
    }
    let blob = await r.blob();
    if (docIsEncrypted(doc)) {
      try {
        blob = await decryptFileBlob(doc, r, blob);
      } catch {
        showToast(t("toastDownloadFail"));
        return;
      }
    } else if (isLegacyPlaintext(doc)) {
      scheduleLegacyMigrate(doc, blob);
    }
    downloadBlobToDevice(blob, fname);
  });

  $("#detail-share")?.addEventListener("click", async () => {
    const doc = state.currentDetail;
    if (!doc) return;
    syncInitData();
    const r = await apiFetch(`/api/documents/${doc.id}/file`, { headers: headers() });
    if (!r.ok) {
      showToast(t("toastShareFail"));
      return;
    }
    let blob = await r.blob();
    if (docIsEncrypted(doc)) {
      try {
        blob = await decryptFileBlob(doc, r, blob);
      } catch {
        showToast(t("toastShareFail"));
        return;
      }
    } else if (isLegacyPlaintext(doc)) {
      scheduleLegacyMigrate(doc, blob);
    }
    const file = new File([blob], doc.original_filename, {
      type: doc.mime_type || blob.type || "application/octet-stream",
    });
    try {
      if (navigator.share) {
        if (navigator.canShare?.({ files: [file] })) {
          await navigator.share({ files: [file], title: doc.original_filename });
          return;
        }
        const sl = await fetchShareLinkJson(doc.id);
        if (sl?.file_url) {
          try {
            await navigator.share({
              title: doc.original_filename,
              text: doc.original_filename,
              url: sl.file_url,
            });
          } catch (e2) {
            if (e2 && e2.name === "AbortError") return;
            throw e2;
          }
          return;
        }
      }
      showToast(t("toastShareDownload"));
    } catch (err) {
      if (err && err.name === "AbortError") return;
      showToast(t("toastShareDownload"));
    }
  });

  $("#detail-share-telegram")?.addEventListener("click", async () => {
    const doc = state.currentDetail;
    if (!doc) return;
    syncInitData();
    const r = await apiFetch(`/api/documents/${doc.id}/share-link`, { headers: headers() });
    if (r.status === 503) {
      showToast(t("toastShareUnavailable"));
      return;
    }
    if (!r.ok) {
      showToast(t("toastShareFail"));
      return;
    }
    const j = await r.json();
    if (tg?.openTelegramLink) {
      tg.openTelegramLink(j.telegram_url);
    } else if (tg?.openLink) {
      tg.openLink(j.telegram_url);
    } else {
      window.open(j.telegram_url, "_blank");
    }
  });

  $("#detail-delete")?.addEventListener("click", async () => {
    const doc = state.currentDetail;
    if (!doc || !confirm(t("confirmDeleteOne"))) return;
    const r = await apiFetch(`/api/documents/${doc.id}`, {
      method: "DELETE",
      headers: headers(),
    });
    if (!r.ok) {
      showToast(t("toastDeleteFail"));
      return;
    }
    closeDetail();
    await refreshBootstrap();
    await loadDocuments();
    showToast(t("toastDeleted"));
  });

  $("#bulk-toggle")?.addEventListener("click", () => {
    state.bulkMode = !state.bulkMode;
    state.selection.clear();
    $("#bulk-toggle")?.classList.toggle("active", state.bulkMode);
    updateBulkBar();
    loadDocuments();
  });

  $("#bulk-cancel")?.addEventListener("click", () => {
    state.bulkMode = false;
    state.selection.clear();
    updateBulkBar();
    loadDocuments();
  });

  async function postJson(url, body) {
    return apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  $("#btn-buy-slots")?.addEventListener("click", async () => {
    if (!initData) return;
    if (state.billingMode === "manual") return;
    const r = await postJson("/api/billing/invoice", {});
    if (!r.ok) {
      showToast(t("billingInvoiceFail"));
      return;
    }
    const j = await r.json();
    const url = j.checkout_url || j.invoice_url;
    const openWith = j.open_with || "telegram_invoice";
    if (!url || typeof url !== "string") {
      showToast(t("billingInvoiceFail"));
      return;
    }
    if (openWith === "telegram_invoice" && typeof tg?.openInvoice === "function") {
      tg.openInvoice(url, async (status) => {
        if (status === "paid") {
          showToast(t("billingPaid"));
          closeUploadPanel();
          await refreshBootstrap();
          renderSidebar();
          updateAddDocButton();
        } else if (status === "cancelled") {
          showToast(t("billingCancelled"));
        } else if (status === "failed") {
          showToast(t("billingInvoiceFail"));
        }
      });
      return;
    }
    if (typeof tg?.openLink === "function") {
      tg.openLink(url);
      showToast(t("billingPaytechHint"));
    } else {
      showToast(t("billingInvoiceFail"));
    }
  });

  $("#bulk-dl")?.addEventListener("click", async () => {
    const ids = [...state.selection];
    if (!ids.length) return;
    syncInitData();
    const q = encodeURIComponent(ids.join(","));
    const zipUrl = initData
      ? `${window.location.origin}/api/documents/bulk-zip?ids=${q}&tgWebAppData=${encodeURIComponent(initData)}`
      : "";
    if (typeof tg?.downloadFile === "function" && initData && zipUrl) {
      tg.downloadFile(
        { url: zipUrl, file_name: "famdoc_documents.zip" },
        (accepted) => {
          if (accepted) showToast(t("toastZipOk"));
          else if (accepted === false) showToast(t("toastDownloadFail"));
        },
      );
      return;
    }
    const r = await postJson("/api/documents/bulk-zip", { ids });
    if (!r.ok) {
      showToast(t("toastZipFail"));
      return;
    }
    const blob = await r.blob();
    downloadBlobToDevice(blob, "famdoc_documents.zip");
    showToast(t("toastZipOk"));
  });

  $("#bulk-move")?.addEventListener("click", () => {
    fillCategorySelects();
    $("#move-modal")?.classList.remove("hidden");
  });

  $("#move-cancel")?.addEventListener("click", () => {
    $("#move-modal")?.classList.add("hidden");
  });

  $("#move-confirm")?.addEventListener("click", async () => {
    const ids = [...state.selection];
    const category = $("#move-target").value;
    const r = await postJson("/api/documents/bulk-move", { ids, category });
    if (!r.ok) {
      showToast(t("toastMoveFail"));
      return;
    }
    $("#move-modal")?.classList.add("hidden");
    state.selection.clear();
    state.bulkMode = false;
    updateBulkBar();
    await refreshBootstrap();
    await loadDocuments();
    showToast(t("toastMoved"));
  });

  $("#bulk-del")?.addEventListener("click", async () => {
    const ids = [...state.selection];
    if (!ids.length || !confirm(tfmt("confirmDeleteN", { n: ids.length }))) return;
    const r = await postJson("/api/documents/bulk-delete", { ids });
    if (!r.ok) {
      showToast(t("toastDeleteFail"));
      return;
    }
    state.selection.clear();
    state.bulkMode = false;
    updateBulkBar();
    await refreshBootstrap();
    await loadDocuments();
    showToast(t("toastBulkDeleted"));
  });

  function renderAdminStats(s) {
    const wrap = $("#admin-stats-body");
    if (!wrap) return;
    const rows = [
      ["adminStatUsers", s.users_registered],
      ["adminStatVisits", s.miniapp_opens],
      ["adminStatVaults", s.vaults_with_uploads],
      ["adminStatDocs", s.total_documents],
    ];
    wrap.innerHTML = rows
      .map(
        ([k, v]) =>
          `<div class="admin-stat-row"><span>${escapeHtml(t(k))}</span><strong>${escapeHtml(String(v ?? ""))}</strong></div>`,
      )
      .join("");
  }

  function revokeAdminClaimUrls() {
    (state.adminClaimUrls || []).forEach((u) => URL.revokeObjectURL(u));
    state.adminClaimUrls = [];
  }

  async function renderAdminClaims(d) {
    const wrap = $("#admin-claims-body");
    if (!wrap) return;
    revokeAdminClaimUrls();
    const items = d.items || [];
    if (!items.length) {
      wrap.innerHTML = `<p class="admin-claims-empty">${escapeHtml(t("adminClaimsEmpty"))}</p>`;
      return;
    }
    wrap.innerHTML = items
      .map((m) => {
        const uid = escapeHtml(String(m.user_id ?? ""));
        const disp =
          (m.display_name || "").trim() ||
          [m.first_name, m.last_name].filter(Boolean).join(" ").trim();
        const nameShown = disp
          ? escapeHtml(disp)
          : escapeHtml(t("adminNoDisplayName"));
        const uname = m.username
          ? "@" + escapeHtml(m.username)
          : escapeHtml(t("adminNoUsername"));
        const tier = escapeHtml(
          tfmt("adminClaimTierLine", {
            price: formatUzs(m.price_uzs),
            slots: m.slots_requested,
          }),
        );
        const when = escapeHtml(formatDate(m.claimed_at));
        const rid = String(m.user_id ?? "");
        const receiptSlot = m.has_receipt
          ? `<div class="admin-claim-receipt-slot" data-claim-uid="${rid}"></div>`
          : "";
        return `<article class="admin-claim-card" aria-label="${uid}">
          <div class="admin-claim-grid">
            <div><span class="admin-claim-k">${escapeHtml(t("adminClaimColId"))}</span><span class="admin-claim-v mono">${uid}</span></div>
            <div><span class="admin-claim-k">${escapeHtml(t("adminClaimColName"))}</span><span class="admin-claim-v">${nameShown}</span></div>
            <div><span class="admin-claim-k">${escapeHtml(t("adminClaimColUser"))}</span><span class="admin-claim-v">${uname}</span></div>
            <div><span class="admin-claim-k">${escapeHtml(t("adminClaimColPlan"))}</span><span class="admin-claim-v">${tier}</span></div>
            <div><span class="admin-claim-k">${escapeHtml(t("adminClaimColTime"))}</span><span class="admin-claim-v">${when}</span></div>
          </div>
          ${receiptSlot}
          <div class="admin-claim-actions">
            <button type="button" class="btn-danger outline admin-claim-deny" data-user-id="${rid}">${escapeHtml(t("adminClaimDeny"))}</button>
          </div>
        </article>`;
      })
      .join("");
    for (const m of items) {
      if (!m.has_receipt) continue;
      const uid = m.user_id;
      const slot = wrap.querySelector(
        `.admin-claim-receipt-slot[data-claim-uid="${CSS.escape(String(uid))}"]`,
      );
      if (!slot) continue;
      try {
        const r = await apiFetch(`/api/admin/claim-receipt/${uid}`, {
          headers: headers(),
        });
        if (!r.ok) continue;
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        state.adminClaimUrls.push(url);
        const mime = (m.receipt_mime || blob.type || "").toLowerCase();
        if (mime.includes("pdf")) {
          const frame = document.createElement("iframe");
          frame.className = "admin-claim-receipt-img";
          frame.title = "Receipt";
          frame.src = url;
          slot.replaceWith(frame);
        } else {
          const img = document.createElement("img");
          img.src = url;
          img.alt = "";
          img.className = "admin-claim-receipt-img";
          slot.replaceWith(img);
        }
      } catch (_) {
        /* ignore */
      }
    }
  }

  async function openAdminModal() {
    applyStaticI18n();
    const wrap = $("#admin-stats-body");
    const claimsWrap = $("#admin-claims-body");
    if (wrap)
      wrap.innerHTML = `<p class="admin-loading">${escapeHtml(t("adminStatsLoading"))}</p>`;
    if (claimsWrap)
      claimsWrap.innerHTML = `<p class="admin-loading">${escapeHtml(t("adminClaimsLoading"))}</p>`;
    const fbEl = $("#admin-grant-feedback");
    if (fbEl) fbEl.hidden = true;
    $("#admin-modal")?.classList.remove("hidden");
    try {
      const [s, c] = await Promise.all([
        apiAdminStats(),
        apiAdminManualClaims(),
      ]);
      renderAdminStats(s);
      await renderAdminClaims(c);
    } catch {
      if (wrap)
        wrap.innerHTML = `<p class="admin-error">${escapeHtml(t("adminStatsFail"))}</p>`;
      if (claimsWrap)
        claimsWrap.innerHTML = `<p class="admin-error">${escapeHtml(t("adminClaimsFail"))}</p>`;
      showToast(t("toastConnection"));
    }
  }

  function closeAdminModal() {
    revokeAdminClaimUrls();
    $("#admin-modal")?.classList.add("hidden");
  }

  async function openFamilyModal() {
    $("#family-modal")?.classList.remove("hidden");
    $("#invite-box")?.classList.add("hidden");
    const ul = $("#family-members");
    if (!ul) return;
    ul.innerHTML = "";
    try {
      const r = await apiFetch("/api/family", { headers: headers() });
      const d = await r.json();
      if (!d.family_features) {
        const li = document.createElement("li");
        li.textContent = t("familyGroupOnly");
        ul.appendChild(li);
        return;
      }
      (d.members || []).forEach((m) => {
        const li = document.createElement("li");
        const roleLabel =
          m.role === "owner"
            ? t("roleOwner")
            : m.role === "member"
              ? t("roleMember")
              : m.role;
        const main = document.createElement("div");
        main.className = "member-main";
        main.textContent = m.display_name
          ? `${m.display_name} · ${roleLabel}`
          : tfmt("memberLine", { id: m.user_id, role: roleLabel });
        const sub = document.createElement("div");
        sub.className = "member-sub";
        if (m.username) {
          sub.textContent = "@" + m.username;
        } else {
          sub.textContent = tfmt("memberTelegramId", { id: m.user_id });
        }
        li.appendChild(main);
        li.appendChild(sub);
        ul.appendChild(li);
      });
    } catch {
      showToast(t("toastConnection"));
    }
  }

  $("#btn-admin-sidebar")?.addEventListener("click", () => {
    closeSidebarMobile();
    openAdminModal();
  });
  $("#admin-close")?.addEventListener("click", closeAdminModal);
  $("#admin-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "admin-modal") closeAdminModal();
  });

  $("#admin-claims-body")?.addEventListener("click", async (e) => {
    const btn = e.target.closest?.(".admin-claim-deny");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const uid = parseInt(btn.getAttribute("data-user-id") || "", 10);
    if (!Number.isFinite(uid) || uid < 1) return;
    if (!confirm(t("adminClaimDenyConfirm"))) return;
    const r = await postJson("/api/admin/deny-claim", {
      target_user_id: uid,
    });
    if (!r.ok) {
      let msg = t("adminClaimDenyFail");
      try {
        const err = await r.json();
        if (err.detail === "no_such_claim") msg = t("adminClaimDenyGone");
      } catch (_) {
        /* ignore */
      }
      showToast(msg);
      return;
    }
    showToast(t("adminClaimDenied"));
    try {
      const [s, c] = await Promise.all([
        apiAdminStats(),
        apiAdminManualClaims(),
      ]);
      renderAdminStats(s);
      await renderAdminClaims(c);
    } catch (_) {
      /* ignore */
    }
  });

  $("#admin-grant-submit")?.addEventListener("click", async () => {
    const uidRaw = $("#admin-target-uid")?.value?.trim() || "";
    const slotsRaw = $("#admin-slots-input")?.value?.trim() || "";
    const target_user_id = parseInt(uidRaw, 10);
    const slots = parseInt(slotsRaw, 10);
    const fb = $("#admin-grant-feedback");
    if (!Number.isFinite(target_user_id) || target_user_id < 1) {
      if (fb) {
        fb.textContent = t("adminInvalidUserId");
        fb.hidden = false;
      }
      return;
    }
    if (!Number.isFinite(slots) || slots < 1 || slots > 10000) {
      if (fb) {
        fb.textContent = t("adminInvalidSlots");
        fb.hidden = false;
      }
      return;
    }
    if (fb) fb.hidden = true;
    const r = await postJson("/api/admin/grant", { target_user_id, slots });
    if (!r.ok) {
      const msg = t("adminGrantFail");
      if (fb) {
        fb.textContent = msg;
        fb.hidden = false;
      }
      showToast(msg);
      return;
    }
    const j = await r.json();
    const cap =
      j.document_cap != null ? String(j.document_cap) : t("adminCapUnlimited");
    const okMsg = tfmt("adminGrantOk", {
      slots: j.slots_added ?? slots,
      cap,
    });
    if (fb) {
      fb.textContent = okMsg;
      fb.hidden = false;
    }
    showToast(okMsg);
    try {
      const [s, c] = await Promise.all([
        apiAdminStats(),
        apiAdminManualClaims(),
      ]);
      renderAdminStats(s);
      await renderAdminClaims(c);
    } catch (_) {}
  });

  $("#btn-family-sidebar")?.addEventListener("click", () => {
    closeSidebarMobile();
    openFamilyModal();
  });
  $("#join-accept-btn")?.addEventListener("click", async () => {
    const raw = $("#join-invite-paste")?.value?.trim() || "";
    if (!raw) {
      showToast(t("toastInviteEmpty"));
      return;
    }
    const r = await apiFetch("/api/family/accept", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invite: raw }),
    });
    if (!r.ok) {
      let key = "toastInviteAcceptFail";
      try {
        const err = await r.json();
        const d = err.detail;
        const map = {
          invalid_invite_format: "toastInviteBadFormat",
          expired: "toastInviteExpired",
          invalid: "toastInviteInvalid",
          already_in_family: "toastAlreadyInFamily",
        };
        if (d && map[d]) key = map[d];
      } catch (_) {}
      showToast(t(key));
      return;
    }
    const j = await r.json();
    const jp = $("#join-invite-paste");
    if (jp) jp.value = "";
    $("#family-modal")?.classList.add("hidden");
    showToast(
      j.reason === "already_member"
        ? t("toastJoinAlreadyMember")
        : t("toastJoinOk"),
    );
    await refreshBootstrap();
    await loadDocuments();
  });
  $("#family-close")?.addEventListener("click", () => {
    $("#family-modal")?.classList.add("hidden");
  });
  $("#family-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "family-modal") $("#family-modal")?.classList.add("hidden");
  });

  $("#invite-generate")?.addEventListener("click", async () => {
    const phone = $("#invite-phone")?.value?.trim() || "";
    const r = await apiFetch("/api/family/invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone }),
    });
    if (!r.ok) {
      showToast(t("toastInviteFail"));
      return;
    }
    const j = await r.json();
    $("#invite-box")?.classList.remove("hidden");
    const inp = $("#invite-url");
    if (inp) inp.value = j.invite_url || j.token || "";
    const warn = $("#invite-warn");
    if (warn) {
      warn.hidden = !j.warning;
      warn.textContent = j.warning || "";
    }
    showToast(t("toastInviteCreated"));
  });

  $("#invite-copy")?.addEventListener("click", async () => {
    const v = $("#invite-url")?.value;
    if (!v) return;
    try {
      await navigator.clipboard.writeText(v);
      showToast(t("toastCopied"));
    } catch {
      showToast(t("toastCopyFail"));
    }
  });

  $("#btn-copy-card")?.addEventListener("click", async () => {
    const v = $("#billing-card-display")?.value;
    if (!v) return;
    try {
      await navigator.clipboard.writeText(v);
      showToast(t("toastCopied"));
    } catch {
      showToast(t("toastCopyFail"));
    }
  });

  $("#btn-copy-ref")?.addEventListener("click", async () => {
    const v = $("#billing-payment-ref")?.value;
    if (!v) return;
    try {
      await navigator.clipboard.writeText(v);
      showToast(t("toastCopied"));
    } catch {
      showToast(t("toastCopyFail"));
    }
  });

  $("#billing-change-plan")?.addEventListener("click", () => {
    showManualPickStep();
  });

  $("#btn-billing-receipt-pick")?.addEventListener("click", () => {
    $("#billing-receipt-input")?.click();
  });
  $("#billing-receipt-input")?.addEventListener("change", onBillingReceiptFileChange);

  $("#btn-billing-i-paid")?.addEventListener("click", async () => {
    const tier = state.manualSelectedTier;
    if (!tier || state.billingMode !== "manual") {
      showToast(t("billingWaitAdminConfirm"));
      return;
    }
    const file = $("#billing-receipt-input")?.files?.[0];
    if (!file) {
      showToast(t("billingReceiptRequired"));
      return;
    }
    const fd = new FormData();
    fd.append("price_uzs", String(tier.price_uzs));
    fd.append("slots", String(tier.slots));
    fd.append("receipt", file);
    const r = await apiFetch("/api/billing/manual-claim", {
      method: "POST",
      headers: headers(),
      body: fd,
    });
    if (!r.ok) {
      let msg = t("toastConnection");
      try {
        const err = await r.json();
        const d = err.detail;
        if (d === "invalid_tier") msg = t("billingClaimInvalidTier");
        else if (d === "invalid_receipt_type") msg = t("billingReceiptBadType");
        else if (d === "receipt_too_large") msg = t("billingReceiptTooLarge");
      } catch (_) {}
      showToast(msg);
      return;
    }
    showToast(t("billingClaimRecorded"));
    clearBillingReceipt();
  });

  boot();
})();
