// Traducteur d'écran — fenêtre de l'extension. La traduction (marche / pause) et la langue sont celles du
// programme : les changer ici les change partout. Les sites à ne pas traduire sont propres à l'extension.

const etat = document.getElementById("etat");
const actif = document.getElementById("actif");
const langue = document.getElementById("langue");
const ceSite = document.getElementById("ce-site");
const nomSite = document.getElementById("site");
const ouvrir = document.getElementById("ouvrir");

const NOMS_LANGUES = { fr: "français", en: "anglais", es: "espagnol", de: "allemand", it: "italien",
  pt: "portugais", nl: "néerlandais", pl: "polonais", ro: "roumain", sv: "suédois", tr: "turc", el: "grec",
  ru: "russe", uk: "ukrainien", ar: "arabe", hi: "hindi", zh: "chinois", ja: "japonais", ko: "coréen",
  vi: "vietnamien", th: "thaï", id: "indonésien", cs: "tchèque", hu: "hongrois", da: "danois", nb: "norvégien",
  fi: "finnois", bg: "bulgare", he: "hébreu", fa: "persan" };
// Firefox : les fonctions qui renvoient une promesse sont sous le nom « browser ».
const api = globalThis.browser ?? chrome;

for (const [code, nom] of Object.entries(NOMS_LANGUES)) {
  langue.add(new Option(nom[0].toUpperCase() + nom.slice(1), code));
}

const ligneEtat = document.getElementById("etat-ligne");

function afficherEtat(texte, genre) {
  etat.textContent = texte;
  ligneEtat.className = "etat " + genre; // pastille : verte, jaune (pause) ou rouge (erreur)
}

function montrer(reponse) {
  const connecte = reponse && !reponse.erreur;
  actif.disabled = langue.disabled = ouvrir.disabled = !connecte;
  if (!connecte) {
    actif.checked = false; // rien n'est traduit tant que le programme ne répond pas
    afficherEtat("Lance le Traducteur d'écran sur ton PC", "erreur");
    return;
  }
  actif.checked = reponse.actif;
  langue.value = reponse.langue;
  // (Comparé ici aussi : la fenêtre est toujours la version à jour, même si le reste ne l'est pas.)
  const aJour = !reponse.extension || reponse.extension === chrome.runtime.getManifest().version;
  if (!aJour || reponse.aRecharger) {
    afficherEtat("Mise à jour : recharge l'extension (page des extensions, bouton ↻)", "erreur");
    return;
  }
  if (reponse.actif) afficherEtat("Connecté · vers le " + (NOMS_LANGUES[reponse.langue] || reponse.langue), "");
  else afficherEtat("En pause", "pause");
}

// La fenêtre parle directement au programme (pas par la partie « fond » de l'extension) : elle dit la
// vérité même si cette partie est en panne.
const SERVEUR = "http://127.0.0.1:47115";
const ENTETE = { "X-Traducteur-Ecran": "1" };

async function demander(chemin, reglage) {
  const reponse = await fetch(SERVEUR + chemin, reglage
    ? { method: "POST", headers: { ...ENTETE, "Content-Type": "application/json" }, body: JSON.stringify(reglage) }
    : { headers: ENTETE });
  if (!reponse.ok) throw new Error("HTTP " + reponse.status);
  return reponse.json();
}

function envoyer(chemin, reglage) {
  demander(chemin, reglage).then((reponse) => {
    // Le programme répond : la partie « fond » de l'extension marche-t-elle ? (Sinon : à recharger.)
    chrome.runtime.sendMessage({ type: "etat" }, (fond) => {
      montrer({ ...reponse, aRecharger: Boolean(chrome.runtime.lastError || !fond || fond.erreur) });
    });
  }).catch(() => montrer(null));
}

async function afficher() {
  const [onglet] = await api.tabs.query({ active: true, currentWindow: true });
  let site = "";
  try {
    site = new URL(onglet.url).hostname;
  } catch (e) {
    // page interne du navigateur : rien à traduire
  }
  nomSite.textContent = site || "(page du navigateur)";
  const reglages = await api.storage.local.get({ sitesExclus: [] });
  ceSite.checked = site !== "" && !reglages.sitesExclus.includes(site);
  ceSite.disabled = !site;
  envoyer("/etat");

  actif.onchange = () => envoyer("/reglage", { actif: actif.checked });
  langue.onchange = () => envoyer("/reglage", { langue: langue.value });
  ouvrir.onclick = () => demander("/reglage", { fenetre: true }).finally(() => window.close());
  ceSite.onchange = async () => {
    const { sitesExclus } = await api.storage.local.get({ sitesExclus: [] });
    const autres = sitesExclus.filter((s) => s !== site);
    chrome.storage.local.set({ sitesExclus: ceSite.checked ? autres : [...autres, site] });
  };
}

afficher();
