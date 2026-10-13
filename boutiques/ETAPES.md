# Publier l'extension dans les boutiques

Les paquets sont fabriqués par `construire_exe.bat` :

- `TraducteurEcran-extension-chrome.zip` : pour **Chrome**, **Edge** et **Opera** ;
- `TraducteurEcran-extension-firefox.zip` : pour **Firefox**.

Les textes à copier-coller sont dans [`FICHE.md`](FICHE.md), la politique de confidentialité dans
[`CONFIDENTIALITE.md`](CONFIDENTIALITE.md).

À faire **une fois par boutique** (c'est toi qui crées les comptes et qui envoies : personne ne peut le faire à ta
place). La vérification prend de quelques heures à quelques jours.

## Chrome Web Store (Chrome, Brave, et Opera avec son module « Chrome »)

1. https://chrome.google.com/webstore/devconsole : connecte-toi avec un compte Google, paie les **5 $** d'inscription
   (une seule fois).
2. « Nouvel élément » → envoie `TraducteurEcran-extension-chrome.zip`.
3. Fiche : nom, résumé, description, catégorie, langue, capture d'écran, vignette (dans `FICHE.md`).
4. Onglet « Confidentialité » : objectif unique, justification de chaque permission, données, lien de la politique de
   confidentialité (dans `FICHE.md`).
5. « Envoyer pour examen ».

## Microsoft Edge Add-ons (gratuit)

1. https://partner.microsoft.com/dashboard/microsoftedge : compte Microsoft, inscription gratuite.
2. « Créer une extension » → `TraducteurEcran-extension-chrome.zip`.
3. Mêmes textes et images, même lien de confidentialité. « Publier ».

## Opera add-ons (gratuit)

1. https://addons.opera.com/developer/ : compte Opera.
2. « Upload new extension » → `TraducteurEcran-extension-chrome.zip`.
3. Mêmes textes et images. Envoyer.

## Firefox (addons.mozilla.org, gratuit)

1. https://addons.mozilla.org/developers/ : compte Firefox.
2. « Soumettre un nouveau module » → « Sur ce site » → `TraducteurEcran-extension-firefox.zip`.
3. Code source demandé ? Réponds **Non** (le code n'est ni minifié ni assemblé : il est lisible tel quel).
4. Mêmes textes, catégorie « Langue et traduction », lien de confidentialité. Envoyer.

## Une fois publiée

Envoie-moi le lien de chaque page publiée : je le mets dans le programme (`BOUTIQUES` dans `traducteur_ecran.pyw`).
La rubrique « Sites internet » proposera alors **« Boutique »** (installation en un clic, mises à jour automatiques)
au lieu du mode développeur.

Pour une nouvelle version de l'extension : monter `version` dans `extension/manifest.json`, reconstruire, et envoyer
le nouveau zip dans chaque boutique (« Importer une nouvelle version »).
