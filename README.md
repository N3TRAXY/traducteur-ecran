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

## Pour les développeurs

- `traducteur_ecran.pyw` : le programme (Python 3.14, Windows 10/11).
- `extension/` : l'extension Chrome / Edge / Opera / Firefox (traduction directement dans les pages).
- `tests/verifier_tout.py` : toutes les vérifications (`py -B tests/verifier_tout.py`).
- `construire_exe.bat` : fabrique `TraducteurEcran-Installation.exe` (PyInstaller + Inno Setup).

## Licences des modèles

Traduction par [Argos Translate](https://github.com/argosopentech/argos-translate) (MIT) et, de
l'anglais vers le français, par le modèle [OPUS-MT](https://github.com/Helsinki-NLP/Tatoeba-Challenge)
« transformer-big » (CC-BY 4.0, Helsinki-NLP).
