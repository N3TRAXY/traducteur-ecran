# Traducteur d'écran

Traduit tout seul le texte de ton écran dans ta langue, directement par-dessus : logiciels, Discord,
et les sites internet (avec l'extension du navigateur). Tout se fait sur ton PC, rien n'est envoyé
sur internet.

## Installer

1. Télécharge **TraducteurEcran-Installation.exe** dans la page
   [Releases](../../releases/latest).
2. Lance-le. Si Windows affiche « Windows a protégé votre ordinateur » : « Informations
   complémentaires », puis « Exécuter quand même ».
3. Au premier lancement, choisis ta langue et les langues à comprendre : elles se téléchargent une
   fois, ensuite tout marche hors ligne.

Le programme se met à jour tout seul : quand une nouvelle version sort, il propose « Mettre à jour ».
Tous les raccourcis clavier se changent dans la rubrique « Options ».

## Ce qu'il sait faire

- Traduire tout l'écran par-dessus le texte d'origine (logiciels, Discord…), et les sites directement dans la page
  avec l'extension.
- **Ctrl + Alt + Z** : traduire une zone choisie à la souris, même en pause, même le texte d'une image ou d'un jeu.
- **Ctrl + Alt + Entrée** : écrire en français, envoyer dans une autre langue.
- **Historique** des dernières traductions, à relire ou copier.
- **Carte graphique NVIDIA** utilisée toute seule si elle est là : environ 10 fois plus rapide.
- Grands modèles de traduction (bien meilleurs) pour l'anglais, l'espagnol, le portugais, l'italien, le russe,
  l'ukrainien, le polonais et le chinois ; avec la carte graphique, **MADLAD-400 3B** pour toutes les langues sauf
  l'anglais (japonais, coréen…).
- **IA locale** en option (TranslateGemma 4B, intégrée : rien à installer) : retraduit les phrases avec leur sens et
  leurs expressions. Il faut une carte graphique (NVIDIA, AMD ou Intel) avec 4 Go.
- « Options » → **Signaler un problème** : une page de ce dépôt préremplie, avec le journal à joindre.

## Pour les développeurs

- `traducteur_ecran.pyw` : le programme (Python 3.14, Windows 10/11).
- `extension/` : l'extension Chrome / Edge / Opera / Firefox (traduction directement dans les pages).
- `tests/verifier_tout.py` : toutes les vérifications (`py -B tests/verifier_tout.py`).
- `construire_exe.bat` : fabrique `TraducteurEcran-Installation.exe` (PyInstaller + Inno Setup).

## Licences des modèles

Traduction par [Argos Translate](https://github.com/argosopentech/argos-translate) (MIT) et par les modèles
[OPUS-MT](https://github.com/Helsinki-NLP/Tatoeba-Challenge) de Helsinki-NLP (CC-BY 4.0), convertis pour
CTranslate2 et publiés dans la version [« modeles »](../../releases/tag/modeles) : opus-mt-tc-big-en-fr,
opus-mt-tc-big-itc-itc, opus-mt-tc-big-zle-fr, opus-mt-tc-big-zlw-en, opus-mt-tc-bible-big-zhx-en ; MADLAD-400 3B
(Google, Apache 2.0, converti par Nextcloud, téléchargé depuis Hugging Face) ; IA locale : TranslateGemma 4B (Google,
conditions d'utilisation de Gemma), au format GGUF (copie de mradermacher sur Hugging Face), qui tourne dans
[llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT, version Vulkan téléchargée depuis ses versions GitHub). Carte graphique : bibliothèque cuBLAS de NVIDIA, téléchargée depuis PyPI (paquet `nvidia-cublas-cu12`
publié par NVIDIA), seulement sur les PC qui ont une carte NVIDIA.

L'extension pour les boutiques des navigateurs : voir [`boutiques/`](boutiques/ETAPES.md).
