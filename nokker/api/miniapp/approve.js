/**
 * approve.js — logique applicative de la Mini App Telegram d'approbation Nokker.
 *
 * Tout le crypto/canonique pur vit dans approve-core.js (testé par `node --test`, sans DOM). Ce
 * fichier ne fait QUE : piloter l'UI, parler à `Telegram.WebApp` (thème, BiometricManager,
 * MainButton/SecondaryButton, HapticFeedback) et appeler l'API Nokker (docs/CLIENT-API.md,
 * docs/APPROVALS.md). Aucun secret ne transite par ici en clair plus longtemps que nécessaire : la
 * clé privée n'existe en mémoire JS que pendant la signature d'UNE décision (voir handleDecision).
 *
 * Points du contrat non fixés explicitement par docs/APPROVALS.md et docs/CLIENT-API.md au moment où
 * ce client a été écrit (lus en entier avant ce fichier) — assumptions documentées ici ET dans
 * README.md :
 *   1. Forme exacte du JSON de `GET /v1/approvals/{id}` : le contrat liste les CHAMPS ("outil,
 *      arguments, effet, raison, avis de Nina, niveau, expiration") mais pas les clés JSON. Ce
 *      fichier lit plusieurs noms plausibles (`tool.name`/`tool_name`/`name`, `strength`/`level`,
 *      `advice`/`nina_advice`, `expires_at`/`expiresAt`, …) — voir `readApprovalFields()`.
 *   2. Authentification des appels faits DEPUIS la Mini App : le contrat décrit un jeton de session
 *      Bearer générique (`POST /v1/sessions`) pour les clients CLI/natifs, mais pour Telegram il dit
 *      seulement qu'« en plus, `initData` ... est validé ». Ce client envoie `initData` sur CHAQUE
 *      appel (en-tête `X-Telegram-Init-Data` pour les GET, champ `init_data` dans le corps pour les
 *      POST) et NE fait PAS la danse `POST /v1/sessions` — en supposant que `initData` suffit comme
 *      preuve d'identité pour un client Mini App. Si le serveur exige quand même un Bearer de
 *      session, tout passe par `apiFetch()` ci-dessous : un seul endroit à modifier.
 *   3. Forme de la réponse de `POST /v1/devices` (code d'appairage) et de `GET /v1/devices`
 *      (statut par appareil) : non spécifiée. Lus défensivement (`id`/`device_id`,
 *      `pairing_code`/`code`, `status`/`state`).
 */
import * as core from "./approve-core.js";

const tg = window.Telegram && window.Telegram.WebApp;

const LS_DEVICE_ID = "nokker:device_id";

const state = {
  countdownTimer: null,
  currentApproval: null,
};

let mainButtonHandler = null;
let secondaryButtonHandler = null;
let decisionInFlight = false;

const $ = (selector) => document.querySelector(selector);

// ---------------------------------------------------------------------------
// Petites utilitaires DOM / Telegram
// ---------------------------------------------------------------------------

function showScreen(id, loadingLabel) {
  document.querySelectorAll(".screen").forEach((el) => {
    el.hidden = el.id !== id;
  });
  if (id === "screen-loading" && loadingLabel) {
    $("#loading-label").textContent = loadingLabel;
  }
  if (id !== "screen-card") {
    clearCountdown();
    if (tg?.MainButton) {
      try {
        if (mainButtonHandler) tg.MainButton.offClick(mainButtonHandler);
      } catch {
        /* client sans support offClick */
      }
      tg.MainButton.hide();
    }
    if (tg?.SecondaryButton) {
      try {
        if (secondaryButtonHandler) tg.SecondaryButton.offClick(secondaryButtonHandler);
      } catch {
        /* Bot API < 7.10, pas de SecondaryButton */
      }
      tg.SecondaryButton.hide();
    }
  }
}

function showError(title, detail, opts = {}) {
  showScreen("screen-error");
  $("#error-title").textContent = title;
  $("#error-detail").textContent = detail;
  const retryBtn = $("#btn-error-retry");
  if (opts.onRetry) {
    retryBtn.hidden = false;
    retryBtn.onclick = () => opts.onRetry();
  } else {
    retryBtn.hidden = true;
    retryBtn.onclick = null;
  }
  safeHaptic("error");
}

// Signature Nokker (heurtoir, « Knocks for you ») : toc toc toc en vibration — trois impacts forts et égaux,
// même rythme que le son (nokker-toc-A, 130 ms entre les coups). Le son reste désactivable, la vibration suit le
// réglage haptique du téléphone.
function knockHaptic() {
  if (!tg?.HapticFeedback) return;
  // Nick 18.10 : trois coups ÉGAUX et FORTS, au rythme exact du son A (130 ms) — pas de dégressif.
  [["heavy", 0], ["heavy", 130], ["heavy", 260]].forEach(([style, delay]) => {
    setTimeout(() => {
      try {
        tg.HapticFeedback.impactOccurred(style);
      } catch {
        /* client Telegram sans retour haptique : rien */
      }
    }, delay);
  });
}

function safeHaptic(kind) {
  try {
    if (!tg?.HapticFeedback) return;
    if (kind === "success") tg.HapticFeedback.notificationOccurred("success");
    else if (kind === "error") tg.HapticFeedback.notificationOccurred("error");
    else if (kind === "impact") tg.HapticFeedback.impactOccurred("light");
  } catch {
    /* best-effort, jamais bloquant */
  }
}

function applyTheme() {
  if (!tg) return;
  const root = document.documentElement;
  const params = tg.themeParams || {};
  const map = {
    bg_color: "--tg-bg",
    text_color: "--tg-text",
    hint_color: "--tg-hint",
    link_color: "--tg-link",
    button_color: "--tg-button",
    button_text_color: "--tg-button-text",
    secondary_bg_color: "--tg-secondary-bg",
    section_bg_color: "--tg-section-bg",
    section_header_text_color: "--tg-section-header-text",
    subtitle_text_color: "--tg-subtitle-text",
    destructive_text_color: "--tg-destructive-text",
    accent_text_color: "--tg-accent-text",
  };
  for (const [tgKey, cssVar] of Object.entries(map)) {
    const value = params[tgKey];
    if (value) root.style.setProperty(cssVar, value.startsWith("#") ? value : `#${value}`);
  }
  try {
    tg.setHeaderColor?.("bg_color");
  } catch {
    /* ignore si non supporté par ce client */
  }
  try {
    tg.setBackgroundColor?.("bg_color");
  } catch {
    /* idem */
  }
}

function getStoredDeviceId() {
  try {
    return localStorage.getItem(LS_DEVICE_ID);
  } catch {
    return null;
  }
}

function setStoredDeviceId(id) {
  try {
    if (id) localStorage.setItem(LS_DEVICE_ID, id);
  } catch {
    /* stockage indisponible (navigation privée, quota) — pas bloquant, on retentera plus tard */
  }
}

/**
 * Retrouve le `device_id` serveur à envoyer avec une décision. `localStorage` est le cas normal
 * (posé pendant l'enrôlement) ; s'il a été vidé alors que `BiometricManager.isBiometricTokenSaved`
 * est resté vrai (stockages différents : site data vs trousseau biométrique Telegram), on retente
 * via `GET /v1/devices` — s'il n'y a qu'un seul appareil pour cet utilisateur, c'est forcément lui.
 * En tout dernier recours, `BiometricManager.deviceId` (identifiant Telegram, pas forcément celui du
 * serveur) : mieux qu'un échec silencieux, et un mauvais id sera rejeté proprement par le serveur.
 */
async function resolveDeviceId(initData) {
  const stored = getStoredDeviceId();
  if (stored) return stored;
  try {
    const resp = await apiFetch("/v1/devices", { initData });
    const list = Array.isArray(resp) ? resp : resp?.devices ?? [];
    if (list.length === 1) {
      const onlyId = list[0]?.id ?? list[0]?.device_id ?? null;
      if (onlyId) {
        setStoredDeviceId(onlyId);
        return onlyId;
      }
    }
  } catch {
    /* on retombe sur le dernier repli ci-dessous */
  }
  return tg?.BiometricManager?.deviceId ?? null;
}

function buildDeviceName() {
  const platform = tg?.platform || "telegram";
  const user = tg?.initDataUnsafe?.user;
  const who = user?.username ? `@${user.username}` : user?.id ? `id${user.id}` : "mini-app";
  return `Telegram Mini App · ${platform} · ${who}`;
}

function describeToolShort(approval) {
  const f = readApprovalFields(approval);
  const label = f.effect ? `${f.tool} (${f.effect})` : f.tool;
  return label.length > 80 ? `${label.slice(0, 77)}…` : label;
}

function describeApiError(e) {
  if (e instanceof TypeError) return "Connexion impossible — vérifie ta connexion et réessaie.";
  if (e?.status === 404) return "Cette demande n'existe pas (identifiant invalide ou nettoyée du registre).";
  if (e?.status === 401 || e?.status === 403) return "Accès refusé par le serveur — reconnecte-toi depuis Telegram.";
  if (e?.status === 409) return "Cette carte a changé d'état entre deux appels (déjà décidée ailleurs ?).";
  if (e?.status >= 500) return "Le serveur Nokker a répondu une erreur — réessaie dans un instant.";
  return e?.message || "Erreur inconnue.";
}

// ---------------------------------------------------------------------------
// Appels réseau — un seul point d'entrée, pour centraliser l'identité envoyée.
// ---------------------------------------------------------------------------

async function apiFetch(path, { method = "GET", body, initData } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (initData) headers["X-Telegram-Init-Data"] = initData; // voir note d'ambiguïté #2 en tête de fichier
  let res;
  try {
    res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch (networkError) {
    throw new TypeError(networkError?.message || "network error");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* corps vide ou non-JSON (ex: 204) — pas une erreur en soi */
  }
  if (!res.ok) {
    const message = (data && (data.message || data.error || data.detail)) || `Erreur ${res.status}`;
    const err = new Error(message);
    err.status = res.status;
    err.body = data;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------------------
// BiometricManager — promisification des callbacks Telegram.
// ---------------------------------------------------------------------------

function biometricInit() {
  return new Promise((resolve) => tg.BiometricManager.init(() => resolve()));
}

function biometricRequestAccess(reason) {
  return new Promise((resolve) => tg.BiometricManager.requestAccess({ reason }, (granted) => resolve(!!granted)));
}

function biometricUpdateToken(token) {
  return new Promise((resolve) => tg.BiometricManager.updateBiometricToken(token, (updated) => resolve(!!updated)));
}

function biometricAuthenticate(reason) {
  return new Promise((resolve) =>
    tg.BiometricManager.authenticate({ reason }, (ok, token) => resolve({ ok: !!ok, token: token || null })),
  );
}

// ---------------------------------------------------------------------------
// Lecture défensive des champs de la carte d'approbation (ambiguïté #1, voir en-tête).
// ---------------------------------------------------------------------------

function readStatus(obj) {
  return obj?.status ?? obj?.state ?? null;
}

function readApprovalFields(approval) {
  const tool = approval?.tool?.name ?? approval?.tool_name ?? approval?.name ?? approval?.call?.name ?? "—";
  const args = approval?.tool?.arguments ?? approval?.arguments ?? approval?.call?.arguments ?? {};
  const effect = approval?.effect ?? approval?.tool?.effect ?? "";
  const rule = approval?.rule ?? approval?.verdict?.rule ?? "";
  const reason = approval?.reason ?? approval?.verdict?.reason ?? "";
  const advice = approval?.advice ?? approval?.nina_advice ?? approval?.verdict?.advice ?? null;
  const strength = approval?.strength ?? approval?.level ?? approval?.verdict?.strength ?? "standard";
  const expiresAt = approval?.expires_at ?? approval?.expiresAt ?? null;
  const id = approval?.id ?? approval?.approval_id ?? null;
  return { tool, args, effect, rule, reason, advice, strength, expiresAt, id };
}

// ---------------------------------------------------------------------------
// Écran : enrôlement d'appareil
// ---------------------------------------------------------------------------

async function runEnrollment(initData) {
  showScreen("screen-enroll-intro");
  $("#enroll-intro-note").textContent = "";
  const startBtn = $("#btn-enroll-start");

  return new Promise((resolve) => {
    startBtn.onclick = async () => {
      startBtn.disabled = true;
      try {
        const granted = await biometricRequestAccess(
          "Nokker a besoin d'un accès biométrique pour créer ta clé d'approbation.",
        );
        if (!granted) {
          showError(
            "Accès biométrique refusé",
            "Sans accès biométrique, Nokker ne peut pas créer de clé d'approbation forte sur cet appareil — aucun repli silencieux n'est utilisé. Relance l'enrôlement quand tu es prêt·e.",
            { onRetry: () => { startBtn.disabled = false; runEnrollment(initData).then(resolve); } },
          );
          return;
        }

        showScreen("screen-loading", "Génération de la clé…");
        const { privateKey, publicKey } = await core.generateDeviceKeyPair();
        const privateKeyB64 = await core.exportPrivateKeyPkcs8Base64Url(privateKey);
        const publicKeyB64 = await core.exportPublicKeySpkiBase64Url(publicKey);

        const saved = await biometricUpdateToken(privateKeyB64);
        if (!saved) {
          showError(
            "Impossible d'enregistrer la clé biométrique",
            "La biométrie n'a pas pu protéger la nouvelle clé — rien n'a été envoyé à Nokker. Réessaie.",
            { onRetry: () => { startBtn.disabled = false; runEnrollment(initData).then(resolve); } },
          );
          return;
        }

        showScreen("screen-loading", "Enregistrement de l'appareil…");
        const response = await apiFetch("/v1/devices", {
          method: "POST",
          initData,
          body: { public_key_spki: publicKeyB64, name: buildDeviceName(), init_data: initData },
        });

        const deviceId = response?.id ?? response?.device_id ?? tg.BiometricManager.deviceId ?? null;
        if (deviceId) setStoredDeviceId(deviceId);
        const pairingCode = response?.pairing_code ?? response?.code ?? null;

        showEnrollPending({ deviceId, pairingCode });
        const confirmed = await waitForDeviceConfirmation(deviceId, initData);
        resolve(confirmed);
      } catch (e) {
        showError("Enrôlement interrompu", describeApiError(e), {
          onRetry: () => { startBtn.disabled = false; runEnrollment(initData).then(resolve); },
        });
      }
    };
  });
}

function showEnrollPending({ deviceId, pairingCode }) {
  showScreen("screen-enroll-pending");
  $("#pairing-code").textContent = pairingCode || deviceId || "----";
  $("#pairing-device-id").textContent = deviceId ? `Identifiant d'appareil : ${deviceId}` : "";
}

function waitForDeviceConfirmation(deviceId, initData) {
  return new Promise((resolve) => {
    let stopped = false;
    let timer = null;

    function clearTimer() {
      if (timer) clearTimeout(timer);
      timer = null;
    }

    async function checkOnce() {
      if (stopped) return;
      try {
        const resp = await apiFetch("/v1/devices", { initData });
        const list = Array.isArray(resp) ? resp : resp?.devices ?? [];
        const mine = list.find((d) => (d.id ?? d.device_id) === deviceId) ?? (list.length === 1 ? list[0] : null);
        const status = readStatus(mine);
        if (status && status !== "pending") {
          stopped = true;
          clearTimer();
          document.removeEventListener("visibilitychange", onVisibility);
          if (["rejected", "revoked", "denied"].includes(status)) {
            showError(
              "Appareil non confirmé",
              `L'appareil n'a pas été confirmé (statut : ${status}). Relance l'enrôlement si c'est inattendu.`,
            );
            resolve(false);
          } else {
            resolve(true);
          }
        }
      } catch (e) {
        $("#pairing-device-id").textContent =
          `Vérification impossible pour l'instant (${describeApiError(e)}) — nouvel essai dans quelques secondes…`;
      }
    }

    function scheduleNext() {
      if (stopped) return;
      timer = setTimeout(async () => {
        await checkOnce();
        scheduleNext();
      }, 3000);
    }

    function onVisibility() {
      if (stopped) return;
      if (document.hidden) {
        clearTimer();
      } else {
        clearTimer();
        checkOnce();
        scheduleNext();
      }
    }

    $("#btn-enroll-refresh").onclick = () => checkOnce();
    document.addEventListener("visibilitychange", onVisibility);

    checkOnce();
    scheduleNext();
  });
}

// ---------------------------------------------------------------------------
// Écran : carte d'approbation
// ---------------------------------------------------------------------------

async function runApproval(approvalId, initData) {
  showScreen("screen-loading", "Chargement de la demande…");
  let approval;
  try {
    approval = await apiFetch(`/v1/approvals/${encodeURIComponent(approvalId)}`, { initData });
  } catch (e) {
    showError("Impossible de charger la demande", describeApiError(e), {
      onRetry: () => runApproval(approvalId, initData),
    });
    return;
  }

  const status = readStatus(approval);
  if (status && status !== "pending") {
    showTerminalStatus(approval, status);
    return;
  }

  renderCard(approval);
  wireCardActions(approval, initData);
}

function showTerminalStatus(approval, status) {
  const by = approval?.decided_by ?? approval?.decidedBy ?? approval?.by ?? null;
  const via = approval?.decided_via ?? approval?.via ?? null;
  const whoText = by ? ` par ${by}${via ? ` (${via})` : ""}` : "";
  const map = {
    approved: { icon: "✓", title: "Déjà approuvée", detail: `Cette demande a déjà été approuvée${whoText}.` },
    executed: { icon: "✓", title: "Déjà exécutée", detail: `Cette action a déjà été exécutée${whoText}.` },
    rejected: { icon: "✕", title: "Déjà refusée", detail: `Cette demande a déjà été refusée${whoText}.` },
    expired: {
      icon: "⏳",
      title: "Demande expirée",
      detail: "Cette carte a expiré avant toute décision. Si c'est toujours nécessaire, l'action devra être redemandée.",
    },
    failed: {
      icon: "⚠",
      title: "Exécution en échec",
      detail: "La demande a été approuvée mais l'exécution a échoué — voir le journal.",
    },
  };
  const info = map[status] ?? { icon: "ℹ", title: "Demande déjà traitée", detail: `Statut : ${status}` };
  showScreen("screen-result");
  $("#result-icon").textContent = info.icon;
  $("#result-title").textContent = info.title;
  $("#result-detail").textContent = info.detail;
  $("#btn-close").onclick = () => {
    try {
      tg.close();
    } catch {
      /* hors Telegram : rien à fermer */
    }
  };
  safeHaptic(status === "rejected" || status === "failed" ? "error" : "success");
}

function renderCard(approval) {
  knockHaptic();
  state.currentApproval = approval;
  const f = readApprovalFields(approval);

  $("#card-tool").textContent = f.tool;

  const argsText = JSON.stringify(f.args, null, 2);
  $("#card-arguments").textContent = argsText;
  $("#args-details").open = argsText.length <= 220;

  $("#card-reason").textContent = [f.rule, f.reason].filter(Boolean).join(" — ") || "—";

  if (f.advice) {
    $("#field-advice-wrap").hidden = false;
    $("#card-advice").textContent = f.advice;
  } else {
    $("#field-advice-wrap").hidden = true;
  }

  const isStrong = f.strength === "strong";
  $("#badge-level").textContent = isStrong ? "Niveau fort" : "Niveau standard";
  $("#badge-level").className = `badge ${isStrong ? "badge-level-strong" : "badge-level-standard"}`;
  $("#badge-effect").textContent = f.effect || "effet inconnu";

  $("#card-error").hidden = true;
  $("#fallback-actions").hidden = false; // valeur par défaut, ajustée par wireCardActions

  startCountdown(f.expiresAt);
  showScreen("screen-card");
}

function startCountdown(expiresAtIso) {
  clearCountdown();
  const el = $("#card-expiry");
  if (!expiresAtIso) {
    el.textContent = "—";
    return;
  }
  const expiresAtMs = Date.parse(expiresAtIso);
  if (Number.isNaN(expiresAtMs)) {
    el.textContent = String(expiresAtIso);
    return;
  }

  function tick() {
    const remainingMs = expiresAtMs - Date.now();
    if (remainingMs <= 0) {
      el.textContent = "Expirée";
      el.classList.add("countdown-warn");
      clearCountdown();
      onCardExpired();
      return;
    }
    const totalSec = Math.floor(remainingMs / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    el.textContent =
      h > 0 ? `${h} h ${String(m).padStart(2, "0")} min` : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    el.classList.toggle("countdown-warn", remainingMs < 60000);
  }

  tick();
  state.countdownTimer = setInterval(tick, 1000);
}

function clearCountdown() {
  if (state.countdownTimer) clearInterval(state.countdownTimer);
  state.countdownTimer = null;
  $("#card-expiry")?.classList.remove("countdown-warn");
}

function onCardExpired() {
  $("#card-error").hidden = false;
  $("#card-error").textContent = "Cette carte a expiré : aucune décision n'est plus possible depuis cette page.";
  $("#fallback-actions").hidden = true;
  if (tg?.MainButton) {
    try {
      if (mainButtonHandler) tg.MainButton.offClick(mainButtonHandler);
    } catch {
      /* ignore */
    }
    tg.MainButton.hide();
  }
  if (tg?.SecondaryButton) {
    try {
      if (secondaryButtonHandler) tg.SecondaryButton.offClick(secondaryButtonHandler);
    } catch {
      /* ignore */
    }
    tg.SecondaryButton.hide();
  }
}

function wireCardActions(approval, initData) {
  const hasSecondary = !!tg?.SecondaryButton;

  if (tg?.MainButton) {
    try {
      if (mainButtonHandler) tg.MainButton.offClick(mainButtonHandler);
    } catch {
      /* ignore */
    }
    mainButtonHandler = () => handleDecision("approve", approval, initData);
    tg.MainButton.setParams({ text: "Approuver", is_visible: true, is_active: true });
    tg.MainButton.onClick(mainButtonHandler);
  }

  if (hasSecondary) {
    $("#fallback-actions").hidden = true;
    try {
      if (secondaryButtonHandler) tg.SecondaryButton.offClick(secondaryButtonHandler);
    } catch {
      /* ignore */
    }
    secondaryButtonHandler = () => handleDecision("reject", approval, initData);
    tg.SecondaryButton.setParams({ text: "Refuser", is_visible: true, is_active: true, position: "bottom" });
    tg.SecondaryButton.onClick(secondaryButtonHandler);
  } else {
    // Bot API < 7.10 (ou client sans SecondaryButton) : repli HTML pour le refus uniquement — le
    // MainButton Telegram reste la seule voie pour "Approuver" (cohérence visuelle avec l'OS).
    $("#fallback-actions").hidden = false;
    $("#btn-approve-fallback").hidden = true;
    $("#btn-reject-fallback").hidden = false;
    $("#btn-reject-fallback").onclick = () => handleDecision("reject", approval, initData);
  }
}

function setButtonsBusy(busy) {
  if (tg?.MainButton) {
    try {
      if (busy) tg.MainButton.showProgress(true);
      else tg.MainButton.hideProgress();
      tg.MainButton.setParams({ is_active: !busy });
    } catch {
      /* ignore */
    }
  }
  if (tg?.SecondaryButton) {
    try {
      tg.SecondaryButton.setParams({ is_active: !busy });
    } catch {
      /* ignore */
    }
  }
  const rejectBtn = $("#btn-reject-fallback");
  if (rejectBtn) rejectBtn.disabled = busy;
}

async function handleDecision(decision, approval, initData) {
  if (decisionInFlight) return;
  decisionInFlight = true;
  setButtonsBusy(true);
  const f = readApprovalFields(approval);

  const backToCard = () => {
    renderCard(approval);
    wireCardActions(approval, initData);
  };

  try {
    safeHaptic("impact");

    const challengeResp = await apiFetch(`/v1/approvals/${encodeURIComponent(f.id)}/challenge`, {
      method: "POST",
      initData,
    });
    const challenge = challengeResp?.challenge;
    if (!challenge) throw new Error('Réponse de défi invalide (champ "challenge" absent).');

    showScreen("screen-biometric-wait");
    $("#biometric-wait-label").textContent =
      decision === "approve"
        ? "Confirme ton approbation avec Face ID / Touch ID / ton empreinte…"
        : "Confirme ton refus avec Face ID / Touch ID / ton empreinte…";

    const { ok, token } = await biometricAuthenticate(
      `${decision === "approve" ? "Approuver" : "Refuser"} : ${describeToolShort(approval)}`,
    );

    if (!ok || !token) {
      showError(
        "Biométrie refusée ou indisponible",
        "La décision n'a pas été signée : sans preuve biométrique valide, Nokker n'exécute jamais une action à effet. Aucun repli silencieux n'a été tenté.",
        { onRetry: backToCard },
      );
      return;
    }

    const privateKey = await core.importPrivateKeyForSigning(token);
    const signature = await core.signChallengeDecision(privateKey, challenge, decision);
    const deviceId = await resolveDeviceId(initData);

    showScreen("screen-loading", decision === "approve" ? "Envoi de l'approbation…" : "Envoi du refus…");

    const decisionResp = await apiFetch(`/v1/approvals/${encodeURIComponent(f.id)}/decision`, {
      method: "POST",
      initData,
      body: { decision, device_id: deviceId, challenge, signature, init_data: initData },
    });

    showDecisionResult(decision, decisionResp);
  } catch (e) {
    showError("La décision n'a pas pu être envoyée", describeApiError(e), { onRetry: backToCard });
  } finally {
    decisionInFlight = false;
    setButtonsBusy(false);
  }
}

function showDecisionResult(decision, decisionResp) {
  const status = readStatus(decisionResp) ?? (decision === "approve" ? "approved" : "rejected");
  showScreen("screen-result");
  if (decision === "approve") {
    $("#result-icon").textContent = "✓";
    if (status === "failed") {
      $("#result-icon").textContent = "⚠";
      $("#result-title").textContent = "Approuvé, mais l'exécution a échoué";
      $("#result-detail").textContent = (decisionResp && decisionResp.error) || "Nina rend compte de l'erreur sur le fil d'origine.";
    } else {
      $("#result-title").textContent = status === "executed" ? "Approuvé et exécuté" : "Approuvé";
      $("#result-detail").textContent = "Nina va rendre compte sur le fil d'origine.";
    }
  } else {
    $("#result-icon").textContent = "✕";
    $("#result-title").textContent = "Refusé";
    $("#result-detail").textContent = "L'action ne sera pas exécutée.";
  }
  safeHaptic("success");
  $("#btn-close").onclick = () => {
    try {
      tg.close();
    } catch {
      /* hors Telegram : rien à fermer */
    }
  };
}

// ---------------------------------------------------------------------------
// Point d'entrée
// ---------------------------------------------------------------------------

async function main() {
  showScreen("screen-loading");

  if (!tg) {
    showError(
      "Ouvre cette page depuis Telegram",
      "Cette Mini App doit être ouverte depuis un bouton Telegram (carte d'approbation ou menu du bot) — le SDK telegram-web-app.js ne s'est pas initialisé.",
    );
    return;
  }

  tg.ready();
  tg.expand();
  applyTheme();
  tg.onEvent("themeChanged", applyTheme);

  const initData = tg.initData;
  if (!initData) {
    showError(
      "Session Telegram absente",
      "initData est vide : cette page a été ouverte hors du contexte Telegram (navigateur externe, cache, lien partagé). Rouvre-la depuis le bot.",
    );
    return;
  }

  if (!tg.BiometricManager) {
    showError(
      "Biométrie indisponible sur ce client",
      "Ce client Telegram (souvent un client desktop) n'expose pas BiometricManager. Les approbations fortes nécessitent un client mobile (iOS/Android) avec Face ID, Touch ID ou empreinte. Rouvre cette carte depuis ton téléphone.",
    );
    return;
  }

  try {
    await biometricInit();
  } catch (e) {
    showError("Initialisation biométrique impossible", describeApiError(e));
    return;
  }

  if (!tg.BiometricManager.isBiometricAvailable) {
    showError(
      "Biométrie indisponible sur cet appareil",
      "Face ID, Touch ID ou l'empreinte ne sont pas configurés ou autorisés pour Telegram sur ce téléphone. Active-les dans les réglages de l'appareil puis reviens sur cette page.",
    );
    return;
  }

  // start_param n'existe que pour un lien t.me (Mini App déclarée chez BotFather) ; un bouton `web_app` inline
  // (TelegramNotifier) ouvre l'URL telle quelle : l'id arrive alors dans `?startapp=` de l'URL.
  const approvalId = tg.initDataUnsafe?.start_param
    || new URLSearchParams(window.location.search).get("startapp") || null;

  if (!tg.BiometricManager.isBiometricTokenSaved) {
    const enrolled = await runEnrollment(initData);
    if (!enrolled) return; // un écran d'erreur a déjà été affiché par runEnrollment
  }

  if (!approvalId) {
    showScreen("screen-no-target");
    return;
  }

  await runApproval(approvalId, initData);
}

main().catch((e) => showError("Erreur inattendue", describeApiError(e)));
