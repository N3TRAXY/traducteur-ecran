# Fiche de l'extension pour les boutiques

Textes à copier-coller dans les formulaires des boutiques (Chrome Web Store, Edge, Opera, Firefox).

## Nom

Traducteur d'écran

## Résumé (132 caractères au plus)

Traduit les sites directement dans la page, hors ligne, avec le Traducteur d'écran installé sur ton PC. Rien ne part sur internet.

## Description

> **Il faut le programme Traducteur d'écran pour Windows** (gratuit) :
> https://github.com/N3TRAXY/traducteur-ecran/releases/latest

Traducteur d'écran traduit tout seul les sites dans ta langue, **directement dans la page** : le texte d'origine est
remplacé par sa traduction, avec la même mise en page.

- **Hors ligne et privé** : la traduction se fait sur ton ordinateur, par le programme Traducteur d'écran. Le texte
  des pages ne part jamais sur internet.
- **30 langues**, dont l'anglais, le japonais, le chinois, le coréen, l'espagnol, l'allemand et le russe.
- Les pseudos, le code, les nombres et les liens restent tels quels.
- Un site à ne pas traduire ? Un clic dans la petite fenêtre de l'extension.
- Le programme traduit aussi le reste de l'écran : logiciels, Discord, jeux.

Code source ouvert : https://github.com/N3TRAXY/traducteur-ecran

## Catégorie

- Chrome / Edge : **Outils** (« Productivity » → « Tools » sur le Chrome Web Store).
- Opera : **Accessibilité** ou **Productivité**.
- Firefox : **Langue et traduction** (« Language support »).

## Langue de la fiche

Français (ajouter l'anglais avec la description de `CONFIDENTIALITE.md` si on veut).

## Objectif unique (« Single purpose », Chrome Web Store)

Traduire le texte des pages web dans la langue de l'utilisateur, avec le programme Traducteur d'écran installé
sur son ordinateur.

## Justification des permissions (Chrome Web Store)

| Permission | Texte à mettre |
|---|---|
| Accès à tous les sites (`<all_urls>`, script de contenu) | L'extension lit le texte des pages pour le remplacer par sa traduction, sur tous les sites que l'utilisateur ouvre (il peut exclure un site). |
| Hôte `http://127.0.0.1:47115/*` | Seule adresse contactée : le programme Traducteur d'écran sur l'ordinateur de l'utilisateur, qui fait la traduction hors ligne. |
| `storage` | Retenir la liste des sites que l'utilisateur ne veut pas traduire. |
| `activeTab` | Afficher, dans la petite fenêtre de l'extension, le site de l'onglet ouvert pour proposer « Traduire ce site ». |
| `alarms` | Signaler toutes les 30 secondes au programme local que l'extension est active. |
| Code à distance | **Non** : tout le code est dans l'extension. |

## Données (formulaire « Confidentialité » du Chrome Web Store)

- Données collectées : **Contenu du site Web** (le texte des pages), uniquement pour la traduction, envoyé seulement
  au programme sur l'ordinateur de l'utilisateur, jamais à un serveur.
- Cocher : pas de vente, pas d'usage hors de l'objectif unique, pas d'usage pour la solvabilité.
- Politique de confidentialité :
  https://github.com/N3TRAXY/traducteur-ecran/blob/main/boutiques/CONFIDENTIALITE.md

## Images

- Icône : `extension/icones/128.png`.
- Capture d'écran 1280×800 : `boutiques/capture-1280x800.png`.
- Petite vignette promotionnelle 440×280 (Chrome, facultative) : `boutiques/vignette-440x280.png`.

## Remarque pour les vérificateurs (champ « Notes » à la soumission)

This extension works together with the free Windows program "Traducteur d'écran"
(https://github.com/N3TRAXY/traducteur-ecran/releases/latest), which runs a local translation server on
127.0.0.1:47115. Without it, the extension shows a "!" badge and does nothing. To test: install the program, choose
English → French, then open any English website.
