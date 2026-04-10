/**
 * Client-side vault crypto (zero-knowledge file layer).
 *
 * 1) Server stores only Argon2(password) and a random kdf_salt (public).
 * 2) PBKDF2-SHA256 derives AES-256-GCM keys from password + kdf_salt.
 * 3) The AES key must live only in the host page memory (see app.js). Never use
 *    sessionStorage/localStorage for keys or key material.
 * 4) After a full reload, the user must enter the password again so the app can
 *    call deriveAesKey on demand.
 *
 * Web Crypto: AES-GCM returns ciphertext with the 128-bit tag appended; we split tag for DB metadata.
 */
(function () {
  const PBKDF2_ITERATIONS = 310000;
  const TAG_LENGTH = 128;
  const IV_LENGTH = 12;

  function bytesToB64(u8) {
    let s = "";
    u8.forEach((b) => (s += String.fromCharCode(b)));
    return btoa(s);
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function splitTag(combined) {
    const u8 = new Uint8Array(combined);
    if (u8.length < 17) throw new Error("truncated_cipher");
    const ct = u8.slice(0, u8.length - 16);
    const tag = u8.slice(u8.length - 16);
    return { ciphertext: ct, tag };
  }

  function joinTag(ciphertext, tag) {
    const c = new Uint8Array(ciphertext);
    const t = new Uint8Array(tag);
    const out = new Uint8Array(c.length + t.length);
    out.set(c, 0);
    out.set(t, c.length);
    return out.buffer;
  }

  async function deriveRawKeyBits(password, saltB64) {
    const enc = new TextEncoder();
    const salt = b64ToBytes(saltB64);
    const keyMaterial = await crypto.subtle.importKey(
      "raw",
      enc.encode(password),
      "PBKDF2",
      false,
      ["deriveBits"],
    );
    return crypto.subtle.deriveBits(
      {
        name: "PBKDF2",
        salt,
        iterations: PBKDF2_ITERATIONS,
        hash: "SHA-256",
      },
      keyMaterial,
      256,
    );
  }

  /** PBKDF2(password, salt) → AES-256-GCM key (not extractable). Call on demand; do not cache in storage. */
  async function deriveAesKey(password, saltB64) {
    const bits = await deriveRawKeyBits(password, saltB64);
    return crypto.subtle.importKey("raw", bits, { name: "AES-GCM", length: 256 }, false, [
      "encrypt",
      "decrypt",
    ]);
  }

  async function encryptBuffer(key, plainBuffer) {
    const iv = crypto.getRandomValues(new Uint8Array(IV_LENGTH));
    const combined = await crypto.subtle.encrypt(
      { name: "AES-GCM", iv, tagLength: TAG_LENGTH },
      key,
      plainBuffer,
    );
    const { ciphertext, tag } = splitTag(combined);
    return {
      ivB64: bytesToB64(iv),
      tagB64: bytesToB64(tag),
      ciphertext,
    };
  }

  async function decryptBuffer(key, ivB64, tagB64, ciphertextBuffer) {
    const iv = b64ToBytes(ivB64);
    const tag = b64ToBytes(tagB64);
    const combined = joinTag(ciphertextBuffer, tag);
    const plain = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv, tagLength: TAG_LENGTH },
      key,
      combined,
    );
    return plain;
  }

  async function maybeImagePreviewBlob(file, mime) {
    if (!mime || !mime.startsWith("image/")) return null;
    try {
      const bmp = await createImageBitmap(file);
      try {
        const maxW = 480;
        const scale = bmp.width > maxW ? maxW / bmp.width : 1;
        const w = Math.max(1, Math.round(bmp.width * scale));
        const h = Math.max(1, Math.round(bmp.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) return null;
        ctx.drawImage(bmp, 0, 0, w, h);
        const blob = await new Promise((res) =>
          canvas.toBlob((b) => res(b), "image/jpeg", 0.82),
        );
        return blob;
      } finally {
        bmp.close();
      }
    } catch {
      return null;
    }
  }

  /** First page of a PDF → JPEG (for E2E upload preview). Requires pdf.min.js (pdfjsLib). */
  async function maybePdfPreviewBlob(file) {
    const mime = (file && file.type) || "";
    const name = (file && file.name) || "";
    if (!mime.includes("pdf") && !/\.pdf$/i.test(name)) return null;
    const pdfjsLib = globalThis.pdfjsLib;
    if (!pdfjsLib || typeof pdfjsLib.getDocument !== "function") return null;
    try {
      pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/pdf.worker.min.js";
      const ab = await file.arrayBuffer();
      const pdf = await pdfjsLib.getDocument({ data: ab }).promise;
      const page = await pdf.getPage(1);
      const baseVp = page.getViewport({ scale: 1 });
      const maxW = 480;
      const scale = baseVp.width > maxW ? maxW / baseVp.width : 1;
      const viewport = page.getViewport({ scale });
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      if (!ctx) return null;
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      const task = page.render({ canvasContext: ctx, viewport });
      await task.promise;
      const blob = await new Promise((res) =>
        canvas.toBlob((b) => res(b), "image/jpeg", 0.82),
      );
      return blob;
    } catch {
      return null;
    }
  }

  window.FamDocVaultCrypto = {
    deriveAesKey,
    encryptBuffer,
    decryptBuffer,
    maybeImagePreviewBlob,
    maybePdfPreviewBlob,
    bytesToB64,
    b64ToBytes,
  };
})();
