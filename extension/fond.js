// Traducteur d'écran — partie de l'extension qui parle au programme installé sur le PC.
// Les pages ne peuvent pas joindre le programme elles-mêmes : elles passent par ici.

const SERVEUR = "http://127.0.0.1:47115";
const ENTETE = { "X-Traducteur-Ecran": "1" };

async function demander(chemin, options = {}) {
  const reponse = await fetch(SERVEUR + chemin, {
    ...options,
    headers: { ...ENTETE, ...(options.headers || {}) },
  });
  if (!reponse.ok) throw new Error("HTTP " + reponse.status);
  return reponse.json();
}

// Petit badge sur l'icône : rien quand tout va bien, « off » en pause, « ! » si le programme
// n'est pas lancé.
function badge(connecte, actif, aRecharger) {
  chrome.action.setBadgeText({ text: !connecte ? "!" : aRecharger ? "↻" : actif ? "" : "off" });
  chrome.action.setBadgeBackgroundColor({ color: !connecte ? "#D14343" : aRecharger ? "#E8890C" : "#6366F1" });
}

// L'extension chargée par le navigateur est-elle plus ancienne que celle du dossier (mise à jour, mais
// pas rechargée dans la page des extensions) ?
function aRecharger(etat) {
  return Boolean(etat && etat.extension && etat.extension !== chrome.runtime.getManifest().version);
}

chrome.runtime.onMessage.addListener((message, _expediteur, repondre) => {
  let demande;
  if (message.type === "traduire") {
    demande = demander("/traduire", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ textes: message.textes }),
    });
  } else if (message.type === "etat") {
    demande = demander("/etat");
  } else {
    return false;
  }
  demande
    .then((resultat) => {
      badge(true, resultat.actif, aRecharger(resultat));
      repondre({ ...resultat, aRecharger: aRecharger(resultat) });
    })
    .catch(() => {
      badge(false);
      repondre({ erreur: true });
    });
  return true; // réponse envoyée plus tard (asynchrone)
});

// Signe de vie au programme toutes les 30 s, quelle que soit la page affichée : sur les pages où
// l'extension ne peut pas entrer (magasin d'extensions, pages du navigateur), le programme laisse quand
// même ce navigateur à l'extension, au lieu de le traduire par l'image de l'écran.
function signeDeVie() {
  demander("/etat").then((etat) => badge(true, etat.actif, aRecharger(etat))).catch(() => badge(false));
}
// (Sans la permission « alarms » — extension mise à jour mais pas rechargée — le reste marche quand même.)
if (chrome.alarms) {
  chrome.alarms.create("signe-de-vie", { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener(signeDeVie);
}
signeDeVie();
