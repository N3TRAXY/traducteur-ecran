// Traducteur d'écran — traduit le texte de la page, directement dans la page.
//
// Le texte est remplacé là où il est (rien n'est posé par-dessus) : la traduction défile avec la
// page, garde sa mise en page, et Ctrl+C copie la traduction. Les éléments de la page (liens,
// boutons, messages d'un chat) ne sont jamais retirés : seul leur texte change, et il revient
// tel quel quand on arrête. Les nouveaux textes (messages d'un chat) sont traduits dès qu'ils
// apparaissent ; ce qui est visible passe en premier.
(() => {
  if (window.__traducteurEcran) return;
  window.__traducteurEcran = true;

  const IGNOREES = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "INPUT", "SELECT", "OPTION", "CODE",
    "PRE", "KBD", "SAMP", "VAR", "SVG", "MATH", "CANVAS", "IFRAME", "TEMPLATE", "TITLE", "HEAD"]);
  // Balises qui ne coupent pas une phrase (un lien, un mot en gras au milieu du texte).
  const EN_LIGNE = new Set(["A", "ABBR", "B", "BDI", "BDO", "CITE", "DFN", "EM", "FONT", "I", "LABEL", "MARK",
    "Q", "S", "SMALL", "SPAN", "STRONG", "SUB", "SUP", "TIME", "U", "IMG", "WBR"]);
  // Pseudos, noms d'auteurs et de chaînes, mentions : jamais traduits (d'après leur classe ou balise).
  const NOMS = /(^|[-_\s])(author|username|user-name|display-?name|channel-?name|mention|nickname|handle)([-_\s]|$)/i;
  const LETTRES = /\p{L}{2,}/u;
  const TAILLE_PAQUET = 25; // textes par demande au programme
  const CARACTERES_PAQUET = 1500; // (un texte plus long part seul)
  const PRINCIPALE = window.top === window;
  const site = location.hostname;

  let actif = false; // traduction en marche sur cette page
  let langue = null; // langue des traductions (réglée dans le programme)
  let version = null; // change quand le programme sait traduire plus (relancé, langue ajoutée…)
  const infos = new WeakMap(); // nœud texte -> {original, traduit}
  const traduits = new Set(); // nœuds texte modifiés (pour tout remettre)
  const appliques = new Set(); // phrases traduites affichées (pour les mettre à jour sans tout remettre)
  const memoire = new Map(); // texte d'origine -> traduction, ou null (rien à traduire)
  const ignores = new WeakMap(); // élément -> son texte est-il à laisser tel quel ?
  let enAttente = new Set(); // nœuds ajoutés ou changés, à examiner
  let minuterie = null;
  let enCours = false;

  function envoyer(message) {
    return new Promise((resoudre) => {
      try {
        chrome.runtime.sendMessage(message, (reponse) => {
          resoudre(chrome.runtime.lastError || !reponse ? { erreur: true } : reponse);
        });
      } catch (e) {
        resoudre({ erreur: true }); // extension rechargée : cette page n'y est plus reliée
      }
    });
  }

  // ---- quels textes traduire

  function aIgnorer(element) {
    if (!element) return true;
    if (ignores.has(element)) return ignores.get(element);
    let resultat;
    const classe = typeof element.className === "string" ? element.className : "";
    if (IGNOREES.has(element.tagName) || element.isContentEditable
        || element.getAttribute("translate") === "no" || element.classList.contains("notranslate")
        || NOMS.test(classe) || NOMS.test(element.id || "") || NOMS.test(element.tagName.toLowerCase())) {
      resultat = true;
    } else {
      resultat = element.parentElement ? aIgnorer(element.parentElement) : false;
    }
    ignores.set(element, resultat);
    return resultat;
  }

  function aTraduire(noeud) {
    return noeud.nodeType === Node.TEXT_NODE && noeud.isConnected && !infos.has(noeud)
      && noeud.nodeValue.length < 20000 && LETTRES.test(noeud.nodeValue) && !aIgnorer(noeud.parentElement);
  }

  // Nœuds texte à traduire sous ces racines, dans l'ordre de la page.
  function collecter(racines) {
    const vus = new Set();
    for (const racine of racines) {
      if (!racine.isConnected) continue;
      if (racine.nodeType === Node.TEXT_NODE) {
        if (aTraduire(racine)) vus.add(racine);
      } else if (racine.nodeType === Node.ELEMENT_NODE && !aIgnorer(racine)) {
        const parcours = document.createTreeWalker(racine, NodeFilter.SHOW_TEXT);
        for (let n = parcours.nextNode(); n; n = parcours.nextNode()) {
          if (aTraduire(n)) vus.add(n);
        }
      }
    }
    return [...vus].sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
  }

  // Le bloc (paragraphe, message, cellule) qui contient ce texte.
  function bloc(noeud) {
    let e = noeud.parentElement;
    while (e && e.parentElement && EN_LIGNE.has(e.tagName)) e = e.parentElement;
    return e;
  }

  // Entre deux morceaux d'un même bloc, y a-t-il un retour à la ligne, un autre bloc, ou un texte
  // laissé tel quel (une mention, un pseudo) ? Alors ce sont deux phrases à part.
  function coupure(a, b, dans) {
    const parcours = document.createTreeWalker(dans, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    parcours.currentNode = a;
    for (let n = parcours.nextNode(); n && n !== b; n = parcours.nextNode()) {
      if (n.nodeType === Node.ELEMENT_NODE && (n.tagName === "BR" || !EN_LIGNE.has(n.tagName))) return true;
      if (n.nodeType === Node.TEXT_NODE && LETTRES.test(n.nodeValue)) return true;
    }
    return false;
  }

  function lien(noeud) {
    return noeud.parentElement ? noeud.parentElement.closest("a") : null;
  }

  // Regroupe les morceaux de texte en phrases : une phrase coupée par un mot en gras se traduit
  // bien mieux en entier. Mais deux liens côte à côte (un menu) ou un lien au milieu d'une phrase
  // restent des morceaux à part (le lien doit garder son texte pour rester cliquable).
  function segments(noeuds) {
    const resultat = [];
    let courant = null;
    for (const n of noeuds) {
      const b = bloc(n);
      const precedent = courant && courant.noeuds[courant.noeuds.length - 1];
      if (courant && courant.bloc === b && !coupure(precedent, n, b)) {
        courant.noeuds.push(n);
      } else {
        courant = { bloc: b, noeuds: [n] };
        resultat.push(courant);
      }
    }
    const final = [];
    for (const s of resultat) {
      if (s.noeuds.length > 1 && new Set(s.noeuds.map(lien)).size > 1) {
        for (const n of s.noeuds) final.push({ bloc: s.bloc, noeuds: [n] });
      } else {
        final.push(s);
      }
    }
    for (const s of final) {
      s.valeurs = s.noeuds.map((n) => n.nodeValue);
      const brut = s.valeurs.join("");
      // Retours à la ligne affichés (description de boutique, texte « pre ») : gardés, le programme
      // traduit alors ligne par ligne (japonais et anglais mêlés). Ailleurs ce ne sont que des espaces.
      s.texte = /\n/.test(brut) && /^(pre|break-spaces)/.test(getComputedStyle(s.bloc).whiteSpace)
        ? brut.replace(/[^\S\n]+/g, " ").replace(/ ?\n ?/g, "\n").replace(/\n{3,}/g, "\n\n").trim()
        : brut.replace(/\s+/g, " ").trim();
    }
    return final;
  }

  // ---- traduire, remettre

  function appliquer(s, traduction) {
    if (!s.noeuds.every((n, i) => n.isConnected && n.nodeValue === s.valeurs[i])) return; // a changé entre-temps
    const dernier = s.noeuds.length - 1;
    const avant = s.valeurs[0].match(/^\s*/)[0];
    const apres = s.valeurs[dernier].match(/\s*$/)[0];
    s.noeuds.forEach((n, i) => {
      // La phrase entière va dans le premier morceau ; les suivants (vidés, pas retirés) gardent
      // seulement leur espace de fin.
      const traduit = i === 0 ? avant + traduction + (dernier === 0 ? apres : "") : (i === dernier ? apres : "");
      infos.set(n, { original: n.nodeValue, traduit });
      traduits.add(n);
      n.nodeValue = traduit;
    });
    s.traduction = traduction;
    appliques.add(s);
  }

  // Une phrase déjà traduite reçoit une autre traduction (le programme a changé) : on remplace le texte
  // affiché directement, sans remettre l'original entre-temps (pas de clignotement).
  function remplacer(s, traduction) {
    const premier = s.noeuds[0];
    const info = infos.get(premier);
    if (!premier.isConnected || !info || premier.nodeValue !== info.traduit) return;
    const dernier = s.noeuds.length - 1;
    const valeur = s.valeurs[0].match(/^\s*/)[0] + traduction + (dernier === 0 ? s.valeurs[0].match(/\s*$/)[0] : "");
    infos.set(premier, { original: info.original, traduit: valeur });
    premier.nodeValue = valeur;
    s.traduction = traduction;
  }

  // Une phrase déjà traduite ne doit plus l'être (« Mes mots ») : son texte d'origine revient.
  function remettreSegment(s) {
    for (const n of s.noeuds) {
      const info = infos.get(n);
      if (info && n.nodeValue === info.traduit) n.nodeValue = info.original;
      infos.delete(n);
      traduits.delete(n);
    }
    appliques.delete(s);
  }

  function remettre() {
    observateur.disconnect();
    for (const n of traduits) {
      const info = infos.get(n);
      if (info && n.nodeValue === info.traduit) n.nodeValue = info.original;
      infos.delete(n);
    }
    traduits.clear();
    appliques.clear();
  }

  function distance(s) {
    const r = s.bloc.getBoundingClientRect();
    if (r.bottom >= 0 && r.top <= innerHeight) return 0; // visible
    return r.top > innerHeight ? r.top - innerHeight : -r.bottom * 1.5; // plus bas d'abord
  }

  // Petit anneau discret en bas à droite de la page tant qu'une traduction se prépare (dans une ombre
  // DOM : la page n'en voit rien, et les clics le traversent).
  let anneau = null;
  let minuterieAnneau = null;
  function chargement(enCours) {
    clearTimeout(minuterieAnneau);
    if (!enCours) {
      if (anneau) anneau.style.display = "none";
      return;
    }
    minuterieAnneau = setTimeout(() => {
      if (!anneau) {
        anneau = document.createElement("traducteur-ecran-chargement");
        anneau.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:2147483647;pointer-events:none";
        anneau.attachShadow({ mode: "closed" }).innerHTML =
          "<style>div{width:14px;height:14px;border-radius:50%;border:2.5px solid rgba(99,102,241,.25);" +
          "border-top-color:#818cf8;animation:t .8s linear infinite;background:rgba(18,19,26,.55)}" +
          "@keyframes t{to{transform:rotate(360deg)}}</style><div></div>";
        document.documentElement.appendChild(anneau);
      }
      anneau.style.display = "block";
    }, 300);
  }

  function planifier() {
    if (!minuterie) minuterie = setTimeout(traiter, 30);
  }

  // Les messages sortis de la page (chat, fil qui défile pendant des heures) ne restent pas en mémoire.
  function oublierDisparus() {
    for (const n of traduits) if (!n.isConnected) traduits.delete(n);
    for (const s of appliques) if (!s.noeuds[0].isConnected) appliques.delete(s);
  }

  async function traiter() {
    minuterie = null;
    if (!actif || enCours) return;
    if (traduits.size > 3000 || appliques.size > 3000) oublierDisparus();
    const noeuds = collecter(enAttente);
    enAttente = new Set();
    const inconnus = [];
    for (const s of segments(noeuds)) {
      if (!s.texte) continue;
      if (!memoire.has(s.texte)) inconnus.push(s);
      else if (memoire.get(s.texte)) appliquer(s, memoire.get(s.texte)); // déjà traduit : tout de suite
    }
    if (!inconnus.length) return;
    inconnus.forEach((s) => (s.distance = distance(s)));
    inconnus.sort((a, b) => a.distance - b.distance);
    enCours = true;
    chargement(true);
    try {
      // Paquets de 25 textes au plus, et pas trop longs : une grosse description (traduite en ~10 s) ne
      // fait pas attendre les petits textes visibles.
      const paquets = [];
      for (const s of inconnus) {
        const dernier = paquets[paquets.length - 1];
        if (dernier && dernier.length < TAILLE_PAQUET && dernier.taille + s.texte.length <= CARACTERES_PAQUET) {
          dernier.push(s);
          dernier.taille += s.texte.length;
        } else {
          const nouveau = [s];
          nouveau.taille = s.texte.length;
          paquets.push(nouveau);
        }
      }
      for (const paquet of paquets) {
        if (!actif) break;
        const textes = [...new Set(paquet.map((s) => s.texte))];
        const reponse = await envoyer({ type: "traduire", textes });
        if (!actif) return; // arrêtée pendant l'attente (pause, site exclu) : la page garde son texte
        if (reponse.erreur || !reponse.actif) {
          // Programme fermé ou en pause : la page reprend son texte ; on réessaiera plus tard.
          arreter();
          return;
        }
        if (langue && reponse.langue !== langue) {
          changerDeLangue(reponse.langue);
          return;
        }
        langue = reponse.langue;
        if (memoire.size > 20000) memoire.clear();
        textes.forEach((t, j) => memoire.set(t, reponse.traductions[j] || null));
        for (const s of paquet) {
          const traduction = memoire.get(s.texte);
          if (traduction) appliquer(s, traduction);
        }
      }
    } finally {
      enCours = false;
      chargement(false);
      if (enAttente.size) planifier();
    }
  }

  // Ce qui change dans la page : nouveaux messages, texte remis par la page elle-même.
  const observateur = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === "characterData") {
        const n = m.target;
        const info = infos.get(n);
        if (info && n.nodeValue === info.traduit) continue; // notre propre changement
        if (info) {
          infos.delete(n);
          traduits.delete(n);
        }
        enAttente.add(n);
      } else {
        m.addedNodes.forEach((n) => enAttente.add(n));
      }
    }
    if (enAttente.size) planifier();
  });

  function demarrer() {
    if (actif || !document.body) return;
    actif = true;
    observateur.observe(document.body, { childList: true, subtree: true, characterData: true });
    enAttente.add(document.body);
    planifier();
  }

  function arreter() {
    if (!actif) return;
    actif = false;
    enAttente = new Set();
    remettre();
  }

  function changerDeLangue(nouvelle) {
    arreter();
    memoire.clear();
    langue = nouvelle;
    demarrer();
  }

  // Le programme a changé (relancé, nouvelle langue, nouveau modèle, « Mes mots » modifiés) : les phrases
  // déjà traduites sont redemandées en arrière-plan et mises à jour seulement si leur traduction change ;
  // celles laissées telles quelles sont réexaminées. Rien ne repasse par le texte d'origine.
  async function revoir() {
    for (const [texte, traduction] of memoire) if (!traduction) memoire.delete(texte);
    const segments = [...appliques].filter((s) => s.noeuds[0].isConnected && infos.has(s.noeuds[0]));
    appliques.clear();
    for (const s of segments) appliques.add(s);
    for (let i = 0; i < segments.length && actif; i += TAILLE_PAQUET) {
      const paquet = segments.slice(i, i + TAILLE_PAQUET);
      const reponse = await envoyer({ type: "traduire", textes: paquet.map((s) => s.texte) });
      if (reponse.erreur || !reponse.actif) return;
      paquet.forEach((s, j) => {
        const traduction = reponse.traductions[j] || null;
        memoire.set(s.texte, traduction);
        if (!traduction) remettreSegment(s);
        else if (traduction !== s.traduction) remplacer(s, traduction);
      });
    }
    if (actif && document.body) {
      enAttente.add(document.body);
      planifier();
    }
  }

  // ---- réglages (fenêtre de l'extension) et état du programme

  async function verifier() {
    let reglages;
    try {
      // (« browser » : Firefox, où les fonctions qui renvoient une promesse sont sous ce nom)
      reglages = await (globalThis.browser ?? chrome).storage.local.get({ sitesExclus: [] });
    } catch (e) {
      return; // extension rechargée
    }
    if (reglages.sitesExclus.includes(site)) {
      arreter();
      return;
    }
    const etat = await envoyer({ type: "etat" });
    if (etat.erreur || !etat.actif) {
      arreter(); // programme fermé, ou traduction en pause (Ctrl+Alt+T)
      return;
    }
    if (langue && etat.langue !== langue) {
      changerDeLangue(etat.langue);
      return;
    }
    langue = etat.langue;
    demarrer();
    if (version !== null && etat.version !== version) revoir();
    version = etat.version;
  }

  chrome.storage.onChanged.addListener(verifier);
  // Signe de vie au programme (qui laisse alors ce navigateur à l'extension) et reprise si le
  // programme vient d'être lancé. Les cadres intérieurs (publicités…) se manifestent moins souvent.
  setInterval(() => {
    if (document.visibilityState === "visible") verifier();
  }, PRINCIPALE ? 2000 : 15000); // (2 s : Ctrl+Alt+T dans le programme est suivi presque aussitôt)
  verifier();
})();
