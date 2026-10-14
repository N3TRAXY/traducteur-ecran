"""Vérifie le Traducteur d'écran, cas par cas : tous les problèmes déjà rencontrés, pour qu'ils ne reviennent pas.

    py -B tests/verifier_tout.py

À lancer après chaque modification (une à deux minutes : les vrais modèles de traduction sont utilisés).
Rien n'est enregistré dans les réglages ni dans les traductions mémorisées de l'utilisateur.
"""
import http.client
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
from dataclasses import replace

sys.dont_write_bytecode = True  # pas de dossier __pycache__ dans le projet
sys.stdout.reconfigure(errors="replace")  # une console sans japonais n'arrête pas les vérifications
ICI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
chargeur = importlib.machinery.SourceFileLoader("te", os.path.join(ICI, "traducteur_ecran.pyw"))
spec = importlib.util.spec_from_loader("te", chargeur)
te = importlib.util.module_from_spec(spec)
chargeur.exec_module(te)
te.CACHE_TRADUCTIONS = os.path.join(tempfile.mkdtemp(prefix="traducteur_tests_"), "traductions.json")

echecs = []


def verifier(condition, nom, detail=""):
    print(("  ok   " if condition else "  ÉCHEC ") + nom + ("" if condition else f"  → {detail}"))
    if not condition:
        echecs.append(nom)


# --------------------------------------------------------------------------- sans modèle de traduction
print("Fonctions de base")
for texte in ("court", "Ligne 1\nLigne 2\n\nLigne 3", "Phrase un. " * 30, "日本語の文。" * 40 + "\nEnd"):
    verifier("".join(te.decouper_texte(texte)) == texte, "découpage sans perte : " + texte[:20].replace("\n", " "))
verifier(len(te.decouper_texte("A\nB")) == 3, "un texte sur deux lignes est découpé")
protege, codes = te.proteger_code("Click `Save` then `Exit`")
verifier(protege == "Click [A] then [B]" and te.remettre_code("Cliquez [A] puis [B]", codes) == "Cliquez `Save` puis `Exit`",
         "code cité protégé puis remis")
verifier(te.remettre_code("Cliquez puis [B]", codes) is None, "repère de code perdu : traduction refusée")
verifier(te.remettre_inconnus("use the `x` — ok", "utilise ⁇ x ⁇ ⁇ ok") == "utilise ` x ` — ok", "caractères inconnus remis")
verifier(te.traduction_aberrante("la mastication " * 10) and te.traduction_aberrante("?" * 20)
         and not te.traduction_aberrante("Une phrase tout à fait normale, avec des mots variés et sensés."),
         "boucles du modèle repérées")
verifier(te.developper_argot("idk tbh ngl that was kinda sus") == "I don't know, to be honest, not gonna lie, that was kind of suspicious",
         "argot du chat", te.developper_argot("idk tbh ngl that was kinda sus"))
verifier(te.developper_argot("Waffffie soooo cute") == "Waffffie so cute", "lettres répétées (pas dans les pseudos)",
         te.developper_argot("Waffffie soooo cute"))
verifier(te.touches_du_raccourci("ctrl+alt+t") == (3, 0x54) and te.touches_du_raccourci("t") is None
         and te.touches_du_raccourci("print screen") == (0, 0x2C) and te.touches_du_raccourci("f8") == (0, 0x77),
         "raccourci clavier lu pour Windows (une touche seule : seulement si elle n'écrit rien)")
# Bug : Spotify traduisait titres, artistes et playlists (« THE FINALS » -> « LES FINALES »). Seuls ses boutons et
# textes le sont ; ailleurs (navigateur), liens et lignes de tableau restent traduits.
lecteur_essai = te.Lecteur()
U = lecteur_essai.UIA
zones_spotify = [lecteur_essai._zone_ignoree("spotify.exe", t, loc) for t, loc in (
    (U.UIA_HyperlinkControlTypeId, "lien"), (U.UIA_DataItemControlTypeId, "ligne"),
    (U.UIA_TreeItemControlTypeId, "Élément de l'arborescence"), (U.UIA_TextControlTypeId, "titre"),
    (U.UIA_ButtonControlTypeId, "Bouton"), (U.UIA_TextControlTypeId, "Texte"))]
verifier(zones_spotify == [True, True, True, True, False, False]
         and not lecteur_essai._zone_ignoree("opera.exe", U.UIA_HyperlinkControlTypeId, "lien")
         and not lecteur_essai._zone_ignoree("opera.exe", U.UIA_DataItemControlTypeId, "ligne"),
         "Spotify : titres et noms jamais traduits, ses boutons oui", zones_spotify)
# La fenêtre des réglages se construit (4 rubriques) : un oubli dans son code arrêtait le programme au lancement.
class FauxApp:
    def __init__(self):
        self.config = dict(te.CONFIG_DEFAUT, langues=["en"])
        self.actif, self.historique, self.etat_carte = True, [], "Active"
        self.etat_grand_modele, self.etat_ia, self.niveau_qualite = "Actif", "Active", "maximale"
        self.progression_installation, self.erreur_installation = None, ""
        self.traducteur = types.SimpleNamespace(langues_vues={})
        self._extension_affichee = set()

    def __getattr__(self, nom):  # (les actions des boutons : rien à faire ici)
        return lambda *a, **k: {"en"} if nom == "langues_choisies" else set() if nom == "navigateurs_extension" else None

    def _nom_raccourci(self, action):
        return "+".join(te.noms_des_touches(self.config[te.RACCOURCIS[action][0]]))


# Neumorphisme : en relief, la lumière en haut à gauche et l'ombre en bas à droite (l'ombre ne cache pas la lumière) ;
# creusé, l'inverse, à l'intérieur.
relief_essai = te.image_relief(120, 80, 14, False, False, 11, 3, 4).convert("L")
creux_essai = te.image_relief(120, 80, 14, True, False, 11, 3, 4).convert("L")
fond_essai = te.Image.new("L", (1, 1), te.FOND[0]).getpixel((0, 0))
mesures_relief = [relief_essai.getpixel((60, 8)), fond_essai, relief_essai.getpixel((60, 72)),
                  creux_essai.getpixel((60, 13)), creux_essai.getpixel((60, 66))]  # (milieux des bords)
verifier(mesures_relief[0] > fond_essai > mesures_relief[2] and mesures_relief[3] < fond_essai < mesures_relief[4],
         "neumorphisme : lumière et ombre du bon côté", mesures_relief)
racine_essai = te.ctk.CTk()
racine_essai.withdraw()
fenetre_essai = te.Reglages(racine_essai, FauxApp())
verifier(list(fenetre_essai.onglets.pages) == ["Accueil", "Langues", "Apparence", "Plus"]
         and fenetre_essai.qualite.get() == "Maximale (IA)"
         and fenetre_essai.tuiles_raccourcis["zone"].cget("text") == "Ctrl+Alt+Z",
         "fenêtre des réglages : 4 rubriques, qualité et raccourcis affichés", list(fenetre_essai.onglets.pages))
# Animations : un relief passe d'un état à l'autre en fondu et finit bien sur le nouvel état ; couleurs mêlées.
racine_essai.geometry("+-4000+-4000")  # (affichée hors de l'écran : il faut une vraie taille pour dessiner)
racine_essai.deiconify()
relief_anime = te.Relief(fenetre_essai.onglets.tab("Accueil"))
relief_anime.pack(fill="x")
te.ctk.CTkLabel(relief_anime.dedans, text="essai").pack()
racine_essai.update()
relief_anime.redessiner()
relief_anime.changer("creux")
fin_animation = time.monotonic() + 2
while relief_anime._animation and time.monotonic() < fin_animation:
    racine_essai.update()
    time.sleep(0.01)
attendues = relief_anime._images_de("creux", 1.0)
# La pastille des choix glisse jusqu'au nouveau choix.
choix_essai = te.ChoixRelief(fenetre_essai.onglets.tab("Accueil"), ["A", "B", "C"], te.ctk.StringVar(value="A"),
                             lambda v: None)
choix_essai.pack(fill="x")
racine_essai.update()
choix_essai.variable.set("C")
fin_animation = time.monotonic() + 2
while choix_essai._glissement and time.monotonic() < fin_animation:
    racine_essai.update()
    time.sleep(0.01)
interrupteur_essai = te.InterrupteurRelief(fenetre_essai.onglets.tab("Accueil"))
interrupteur_essai.pack()
racine_essai.update()
interrupteur_essai.select()
fin_animation = time.monotonic() + 2
while interrupteur_essai._animation and time.monotonic() < fin_animation:
    racine_essai.update()
    time.sleep(0.01)
verifier(interrupteur_essai.get() == 1 and interrupteur_essai._animation is None and interrupteur_essai.avance == 1.0,
         "interrupteur : il glisse jusqu'au bout et reste allumé", interrupteur_essai.avance)
carte_page = fenetre_essai.onglets.carte
racine_essai.update()
avant_glissement = (carte_page.winfo_x(), carte_page.winfo_y(), racine_essai.winfo_width())
fenetre_essai.onglets.set("Plus")
pendant_glissement = carte_page.winfo_manager()
fin_animation = time.monotonic() + 2
while fenetre_essai.onglets._glissement and time.monotonic() < fin_animation:
    racine_essai.update()
    time.sleep(0.01)
racine_essai.update()
apres_glissement = (carte_page.winfo_x(), carte_page.winfo_y(), racine_essai.winfo_width())  # (la hauteur suit
# les éléments d'essai ajoutés plus haut dans l'accueil)
verifier(pendant_glissement == "pack" and carte_page.winfo_manager() == "pack" and apres_glissement == avant_glissement
         and not fenetre_essai.onglets._vue,
         "rubrique : la photo de la carte glisse, la vraie carte ne bouge pas et réapparaît",
         (pendant_glissement, avant_glissement, apres_glissement))
verifier(choix_essai._glissement is None and abs(choix_essai.position - 2 * choix_essai._case()) < 1,
         "choix : la pastille glisse jusqu'au nouveau choix", (choix_essai.position, choix_essai._case()))
verifier(relief_anime._animation is None and attendues is not None
         and list(relief_anime._images[0].getdata()) == list(attendues[0].getdata())
         and te.melanger_couleurs("#000000", "#FFFFFF", 0.5) == "#808080",
         "animations : fondu jusqu'au nouvel état, couleurs mêlées", relief_anime.etat)
racine_essai.destroy()
# La qualité en un seul choix allume ou coupe chaque moyen (carte graphique, grand modèle, IA).
appels_qualite = []
app_qualite = types.SimpleNamespace(config={"carte_graphique": False, "grand_modele": False, "ia_locale": False},
                                   reglages=types.SimpleNamespace(afficher_carte=lambda: None))
for cle, nom in (("carte_graphique", "choisir_carte_graphique"), ("grand_modele", "choisir_grand_modele"),
                 ("ia_locale", "choisir_ia")):
    setattr(app_qualite, nom, lambda actif, cle=cle: (appels_qualite.append((cle, actif)),
                                                      app_qualite.config.__setitem__(cle, actif)))
niveaux_vus = []
for niveau in ("maximale", "meilleure", "normale"):
    te.Application.choisir_qualite(app_qualite, niveau)
    niveaux_vus.append(te.Application.niveau_qualite.fget(app_qualite))
verifier(niveaux_vus == ["maximale", "meilleure", "normale"] and appels_qualite == [
    ("carte_graphique", True), ("grand_modele", True), ("ia_locale", True), ("ia_locale", False),
    ("carte_graphique", False), ("grand_modele", False)], "qualité en un seul choix", (niveaux_vus, appels_qualite))
# Bug : sur un clavier français (AZERTY), Ctrl+Alt+Z devenait Ctrl+Alt+W (la zone choisie ne marchait pas).
verifier(te.touches_du_raccourci("ctrl+alt+z") == (3, 0x5A) and te.touches_du_raccourci("ctrl+alt+a") == (3, 0x41)
         and te.touches_du_raccourci("ctrl+shift+2") == (6, 0x32),
         "raccourci : la lettre écrite, quel que soit le clavier (AZERTY)",
         [te.touches_du_raccourci(c) for c in ("ctrl+alt+z", "ctrl+alt+a", "ctrl+shift+2")])
verifier(te.noms_des_touches("ctrl+alt+enter") == ["Ctrl", "Alt", "Entrée"] and te.noms_des_touches("print screen")
         == ["Impr. écran"] and te.noms_des_touches("left windows+shift+s") == ["Win", "Maj", "S"]
         and te.noms_des_touches("") == ["Aucun"], "raccourcis dessinés avec les noms français des touches")
verifier(all(te.CONFIG_DEFAUT[cle] == defaut for cle, defaut, _ in te.RACCOURCIS.values())
         and len({te.touches_du_raccourci(d) for _, d, _ in te.RACCOURCIS.values()}) == len(te.RACCOURCIS),
         "tous les raccourcis réglables, sans doublon par défaut")
verifier(te.numero_de_version("v2.10.1") > te.numero_de_version("2.9.9") == (2, 9, 9)
         and te.numero_de_version(te.VERSION_PROGRAMME) >= (2, 0, 0),
         "versions comparées dans le bon ordre (2.10 après 2.9)")
verifier(te.grands_modeles_manquants({("de", "en")}) == [] and (te.DEPOT_GITHUB or te.derniere_version() is None),
         "grand modèle demandé seulement pour les langues qui en ont un ; sans dépôt, pas de recherche de mise à jour")
# Le menu de l'icône est construit par un autre fil dès sa création : tout ce qu'il lit doit déjà exister
# (« mise_a_jour » créé après l'icône faisait planter le menu au lancement de l'exe).
import inspect
import re
debut = inspect.getsource(te.Application.__init__)
avant_icone = debut[:debut.index("self.icone = self._creer_icone()")]
lus_par_le_menu = set(re.findall(r"self\.(\w+)", inspect.getsource(te.Application._creer_icone)))
crees_apres = sorted(n for n in lus_par_le_menu if f"self.{n} =" in debut and f"self.{n} =" not in avant_icone)
verifier(not crees_apres, "le menu de l'icône ne lit rien qui soit créé après lui", crees_apres)
# Discord : une phrase qui cite du code arrive en morceaux ; on la recolle (code protégé), sans fusionner deux messages.
morceaux = [(505, 1011, 541, 1033, "Yeah"), (544, 1013, 700, 1032, "_CameraDepthTexture"),
            (505, 1011, 1898, 1077, "is for getting the depth of opaque surfaces behind what you're drawing. "
                                    "If you need the depth of the pixel, you probably want something else like"),
            (1324, 1057, 1553, 1076, "UNITY_Z_0_FAR_FROM_CLIPSPACE"), (1563, 1061, 1596, 1075, "modifié"),
            (505, 1100, 872, 1122, "did you set the Render Queues properly?"),
            (505, 1126, 888, 1148, "so that the materials always render in the correct order?")]
recolles = te.recoller_morceaux(morceaux, 22)
M = te.MARQUE_CODE
verifier([r[5] for r in recolles] == [[0, 1, 2, 3], [4], [5], [6]]
         and recolles[0][4].startswith("Yeah " + M + "_CameraDepthTexture" + M + " is for")
         and recolles[0][:4] == (505, 1011, 1898, 1077), "morceaux d'une phrase de Discord recollés", recolles[0][4][:80])
# Texte sur un fond qui bouge (vidéo derrière une fenêtre translucide, stream) : le même texte, gardé tel quel ;
# un chiffre qui change, lui, doit être relu.
from PIL import Image, ImageDraw
import numpy as np


def image_texte(texte, decalage):
    """Texte clair sur un fond flou qui se déplace (vidéo vue à travers une fenêtre translucide), un peu bruité."""
    x, y = np.arange(260)[None, :, None], np.arange(40)[:, None, None]
    fond = np.repeat(30 + 25 * np.sin((x + decalage) / 23.0) + 10 * np.cos((y + decalage) / 9.0), 3, axis=2)
    fond = fond + np.random.default_rng(decalage).integers(-6, 7, fond.shape)
    image = Image.fromarray(np.clip(fond, 0, 255).astype(np.uint8))
    ImageDraw.Draw(image).text((6, 10), texte, fill=(235, 235, 235), font=te.police_pil(18))
    return np.asarray(image).astype(np.int16)


suivi_essai = te.SuiviEcran.__new__(te.SuiviEcran)
morceau = te.Texte("Open Project 12:01", 0, 0, 260, 40, "morceau", (0, 0, 260, 40))
suivi_essai.motifs = {morceau: image_texte("Open Project 12:01", 0)}
verifier(suivi_essai._memes_lettres(morceau, image_texte("Open Project 12:01", 37), 0, 0, 260, 40)
         and not suivi_essai._memes_lettres(morceau, image_texte("Open Project 12:02", 37), 0, 0, 260, 40)
         and not suivi_essai._memes_lettres(morceau, image_texte("Open Project 12:02", 0), 0, 0, 260, 40),
         "fond qui bouge : le texte reste ; un chiffre qui change : relu")
motif = image_texte("Open Project 12:01", 0)
avec_encre = te.Texte("Open Project 12:01", 0, 0, 260, 40, "morceau", (0, 0, 260, 40), encre=te.couleur_encre(motif))
suivi_essai.motifs = {avec_encre: motif}
verifier(bool(avec_encre.encre) and suivi_essai._memes_lettres(avec_encre, image_texte("Open Project 12:01", 37), 0, 0, 260, 40)
         and not suivi_essai._memes_lettres(avec_encre, image_texte("Open Project 12:02", 37), 0, 0, 260, 40),
         "même texte reconnu à la couleur de ses lettres, malgré le fond")
verifier(te.LecteurImage._sans_icone([("@", 0, 0, 22, 22), ("World", 30, 4, 70, 20)]) == [("World", 30, 4, 70, 20)]
         and len(te.LecteurImage._sans_icone([("a", 0, 5, 8, 20), ("month", 12, 5, 50, 20)])) == 2
         and len(te.LecteurImage._sans_icone([("a", 0, 9, 9, 19), ("month", 16, 3, 60, 20)])) == 2
         and len(te.LecteurImage._sans_icone([("a", 0, 2, 8, 11), ("month", 14, 1, 57, 11)])) == 2
         and len(te.LecteurImage._sans_icone([("I", 0, 5, 4, 20), ("agree", 8, 5, 50, 20)])) == 2,
         "icône lue comme une lettre (« @ World ») retirée, pas « a month »")
# Lecture de l'écran : texte blanc sur un bouton coloré (« Open Project » était lu « n Pro• »), mots entiers.
from PIL import ImageFont
faux_ecran = Image.new("RGB", (990, 120), (37, 37, 37))
dessin = ImageDraw.Draw(faux_ecran)
dessin.rounded_rectangle((840, 20, 956, 53), 3, fill=(23, 145, 168))
dessin.text((898, 36), "Open Project", font=ImageFont.truetype("segoeuib.ttf", 15), fill="white", anchor="mm")
dessin.text((60, 36), "Learn", font=ImageFont.truetype("segoeuib.ttf", 16), fill="white", anchor="lm")
dessin.rounded_rectangle((520, 70, 700, 102), 3, fill=(23, 145, 168))
dessin.text((610, 86), "Create New Project", font=ImageFont.truetype("segoeuib.ttf", 15), fill="white", anchor="mm")
dessin.text((700, 36), "a month ago", font=ImageFont.truetype("segoeui.ttf", 15), fill=(220, 220, 220), anchor="lm")
image_bgra = np.dstack([np.asarray(faux_ecran)[..., ::-1], np.full((120, 990), 255, np.uint8)])
gras_bouton = te.couleurs_autour(image_bgra, (0, 0), te.Texte("Open Project", 852, 29, 92, 16, "texte", (0, 0, 990, 120)))
gras_date = te.couleurs_autour(image_bgra, (0, 0), te.Texte("a month ago", 699, 29, 88, 16, "texte", (0, 0, 990, 120)))
verifier(gras_bouton[4] and not gras_date[4], "texte en gras reconnu (la traduction prend la même graisse)",
         (gras_bouton[4], gras_date[4]))
lus = {t.texte: (t.x, t.y) for t in te.LecteurImage().lire(image_bgra, (0, 0))}
verifier({"Open Project", "Learn", "Create New Project"} <= set(lus) and 850 <= lus["Open Project"][0] <= 875
         and 25 <= lus["Open Project"][1] <= 35, "boutons colorés lus justes, à la bonne place", lus)
# Fenêtre déplacée : ses textes lus la suivent (sinon, relus à moitié, ils revenaient décalés et en double).
suivi_essai.morceaux = [te.Texte("Learn", 100, 200, 40, 18, "morceau", (50, 150, 500, 600)),
                        te.Texte("Other", 900, 200, 40, 18, "morceau", (800, 100, 1200, 600))]
suivi_essai.perdus, suivi_essai.a_decaler = [], []
suivi_essai.motifs = {suivi_essai.morceaux[0]: "motif"}
suivi_essai.decaler_fenetre((50, 150, 500, 600), -60, -37)
suivi_essai._decaler_fenetres()
deplace = suivi_essai.morceaux[0]
verifier((deplace.x, deplace.y, deplace.fenetre) == (40, 163, (-10, 113, 440, 563))
         and suivi_essai.morceaux[1].x == 900 and suivi_essai.motifs.get(deplace) == "motif",
         "fenêtre déplacée : ses textes la suivent", suivi_essai.morceaux)
verifier(te.texte_exact("Leam", ["Learn"]) == "Learn" and te.texte_exact("Open Pr*ct", ["Open Project"]) == "Open Project"
         and te.texte_exact("Settings", ["Paramètres avancés du compte"]) == "Settings",
         "lettre mal lue : le nom exact de l'accessibilité est repris")
# Capture d'écran (Impr. écran, Win+Maj+S) : l'outil Capture de Windows passe devant tout l'écran ; les traductions
# ne doivent pas disparaître (elles disparaissaient de l'image).
caches = []
faux_capture = types.SimpleNamespace(suivi_en_cours=False, _capture_utilisateur_en_cours=lambda: True,
                                     calque=types.SimpleNamespace(cacher_sous=caches.append))
te.Application._cmd_cacher_sous(faux_capture, (0, 0, 2560, 1440))
faux_capture._capture_utilisateur_en_cours = lambda: False
te.Application._cmd_cacher_sous(faux_capture, (0, 0, 2560, 1440))
verifier(caches == [(0, 0, 2560, 1440)], "capture d'écran : les traductions restent quand l'outil Capture passe devant")
# Fenêtre déplacée ou défilement suivis avec ~1 s de retard : le fil du suivi attendait son tour face aux autres fils
# Python (lecture de l'écran, traduction). Pendant un suivi, Python passe la main bien plus souvent.
faux_suivi = types.SimpleNamespace(arret=threading.Event(), reveil_suivi=threading.Event(), demande_suivi=("fenetre", 1),
                                   actif=True, calque=types.SimpleNamespace(generation=0), fins_traitees=0,
                                   fins_envoyees=0, suivi_en_cours=False, reveil_image=threading.Event(),
                                   reveil=threading.Event(), reveil_accessibilite=threading.Event(),
                                   demande_pendant_suivi=None, textes_suivis=frozenset())
intervalles = []
faux_suivi._suivre_fenetre = lambda hwnd: (intervalles.append(sys.getswitchinterval()), faux_suivi.arret.set())
faux_suivi.reveil_suivi.set()
intervalle_normal = sys.getswitchinterval()
te.Application._boucle_suivi(faux_suivi)
verifier(intervalles and intervalles[0] <= 0.001 and sys.getswitchinterval() == intervalle_normal,
         "suivi en direct : Python passe la main souvent (pas de retard), puis revient à la normale", intervalles)
# Une autre fenêtre passée devant (glissée par-dessus, Alt+Tab), une fenêtre réduite : ses traductions ne restent pas
# à flotter jusqu'à la prochaine lecture de l'écran. Celles qu'un suivi en direct est en train de déplacer, si.
FENETRE_A, FENETRE_B = (0, 0, 900, 600), (400, 300, 1300, 900)
gardes = [te.Texte("Open Project", 100, 100, 90, 20, "texte", FENETRE_A, "a.exe"),
          te.Texte("a month ago", 600, 500, 90, 20, "texte", FENETRE_A, "a.exe"),
          te.Texte("Settings", 700, 450, 60, 20, "texte", FENETRE_A, "a.exe"),
          te.Texte("Profile", 200, 400, 60, 20, "texte", FENETRE_A, "a.exe")]
calque_garde = te.Calque.__new__(te.Calque)
rangees = []
calque_garde.affiches = {t: types.SimpleNamespace(cacher=lambda t=t: rangees.append(t.texte)) for t in gardes}
calque_garde.dispositions, calque_garde.libres = {t: None for t in gardes}, []
au_point = lambda x, y: None if x < 250 and y > 300 else (FENETRE_B if x >= 400 and y >= 300 else FENETRE_A)
calque_garde.cacher_couvertes(sauf={gardes[2]}, fenetre_au_point=au_point)
verifier(sorted(t.texte for t in calque_garde.affiches) == ["Open Project", "Profile", "Settings"]
         and rangees == ["a month ago"],
         "traduction recouverte par une autre fenêtre : rangée tout de suite (sauf celles d'un suivi en direct)", rangees)
# Stream Discord regardé en écrivant dans le chat : la fenêtre est « périmée » (on vient de taper) mais la vidéo n'a
# pas bougé ; son texte ne doit pas être traduit entre deux relectures (il clignotait).
FENETRE = (0, 0, 2560, 1392)
dans_la_video = te.Texte("Press start to continue", 600, 400, 300, 30, "texte", FENETRE, "discord.exe")
hors_video = te.Texte("Please read the terms of use before buying.", 600, 1000, 500, 30, "texte", FENETRE, "discord.exe")
releve = [(500, 300, 1500, 900, "", "image")]
faux_lecteur = types.SimpleNamespace(vus=releve, a_relire=set(), vus_hors=lambda h: [] if h else releve, _a_traduire=lambda p: True,
                                    _est_pseudo=lambda *a: False)
faux_app = types.SimpleNamespace(textes_image=[dans_la_video, hors_video], memoire_fusion={}, suivi=None)
lus = [t.texte for t in te.Application._fusionner(faux_app, faux_lecteur, {1234})]
verifier(lus == [hors_video.texte], "texte d'une vidéo jamais traduit, même juste après une frappe", lus)
# Champ de saisie : son texte d'aide est traduit tant qu'il est seul ; ce qu'on y écrit, jamais.
champ = [(500, 300, 900, 340, "Search Projects...", "champ")]
lecteur_champ = types.SimpleNamespace(vus=champ, a_relire=set(), vus_hors=lambda h: champ, _a_traduire=lambda p: True,
                                      _est_pseudo=lambda *a: False)
aide_seule = te.Texte("Search Projects...", 520, 310, 150, 18, "texte", FENETRE, "x.exe")
ecrit = te.Texte("my secret project name", 520, 310, 170, 18, "texte", FENETRE, "x.exe")
lus_aide = [t.texte for t in te.Application._fusionner(types.SimpleNamespace(
    textes_image=[aide_seule], memoire_fusion={}, suivi=None), lecteur_champ)]
lus_ecrit = [t.texte for t in te.Application._fusionner(types.SimpleNamespace(
    textes_image=[ecrit], memoire_fusion={}, suivi=None), lecteur_champ)]
verifier(lus_aide == ["Search Projects..."] and lus_ecrit == [],
         "champ de saisie : texte d'aide traduit, ce qu'on écrit jamais", (lus_aide, lus_ecrit))
verifier(te.mesurer("pour écrire dans le tampon de profondeur", 16, 100_000, 75)[0]
         < 0.85 * te.mesurer("pour écrire dans le tampon de profondeur", 16, 100_000)[0],
         "police étroite pour une traduction trop longue")
mots = te.MesMots(["Mayo", "watch streak = série de visionnage", "# commentaire", ""])
verifier(mots.texte_entier("mayo") is None and mots.texte_entier("Watch Streak") == "série de visionnage"
         and mots.texte_entier("autre") is False, "Mes mots : texte entier")
protege, codes = te.proteger_code("The Mayo avatar started a watch streak", mots)
verifier(protege == "The [A] avatar started a [B]" and codes == ["Mayo", "série de visionnage"], "Mes mots : protégés dans la phrase",
         (protege, codes))
verifier(not te.ressemble_a_un_pseudo("🎁 Exclusive In-Game Bonus:", lambda: False)
         and not te.ressemble_a_un_pseudo("🎃 LIMITED PET VARIANT: IN-GAME TREATS! 🎃", lambda: False)
         and te.ressemble_a_un_pseudo("❤ ☁ BOSTON BAY ☁ ❤", lambda: False), "titre d'annonce avec émoji ≠ pseudo décoré")
verifier(not te.ressemble_a_un_pseudo("Sale ends March 31st", lambda: False)
         and not te.ressemble_a_un_pseudo("Starts at 4PM", lambda: False)
         and te.ressemble_a_un_pseudo("Ninja2077", lambda: False) and te.ressemble_a_un_pseudo("xX_dark_Xx", lambda: False),
         "« 31st », « 4PM » ne font pas passer une phrase pour un pseudo")
verifier(not te.texte_de_page_a_traduire("npx skills add vercel-labs") and not te.texte_de_page_a_traduire("1.8K views")
         and te.texte_de_page_a_traduire("Please read the terms of use."), "filtre des pages : code et nombres écartés")
verifier(te.retoucher_francais("S'il vous plaît lire le manuel. Attention: prix à 16:00 sur https://a.b !")
         == "Veuillez lire le manuel. Attention : prix à 16:00 sur https://a.b !"
         and te.retoucher_francais("S'il vous plaît votre avis?") == "S'il vous plaît votre avis ?",
         "retouches du français : « Veuillez », espaces avant « : ! ? »")
verifier(all(bouton in (None, "Le dossier") and "://" not in texte for texte, bouton in te.guide_extension())
         and "traduire_navigateurs" not in te.CONFIG_DEFAUT and "raccourci_copier" not in te.CONFIG_DEFAUT,
         "étapes de l'extension sans lien ni bouton « Ouvrir » ; option des navigateurs retirée")
verifier(te.libelle_du_glossaire("Add to Cart") == "Ajouter au panier" and te.libelle_du_glossaire("SIGN IN") == "SE CONNECTER"
         and te.libelle_du_glossaire("Followers:") == "Abonnés :" and te.libelle_du_glossaire("Buy this hat") is None,
         "glossaire des libellés d'interface")
verifier(te.developper_argot("Sale ends March 31st") == "Sale ends March 31"
         and te.developper_argot("the 2nd of May") == "the 2 May" and te.developper_argot("1st place") == "1st place",
         "dates anglaises : « March 31st » -> « March 31 »")
# Anti-scintillement : un texte relu un peu autrement au même endroit garde la traduction déjà affichée.
Lu = types.SimpleNamespace
ancien, nouveau = Lu(texte="Recently viewed items", x=10, y=10, l=200, h=20), Lu(texte="Recently viewed item", x=11, y=10, l=198, h=20)
faux = Lu(cache={("Recently viewed items", "fr"): "Articles récemment consultés"})
verifier(te.Application._traduction_voisine(nouveau, [ancien], faux, "fr") == "Articles récemment consultés"
         and te.Application._traduction_voisine(Lu(texte="Settings", x=10, y=10, l=200, h=20), [ancien], faux, "fr") is None
         and te.Application._traduction_voisine(Lu(texte="Recently viewed item", x=10, y=300, l=200, h=20), [ancien], faux,
                                                "fr") is None, "anti-scintillement : traduction voisine gardée")

# --------------------------------------------------------------------------- nouveautés 2.1 (sans vrais modèles)
print("Carte graphique, grands modèles, zone, historique")
import collections
import pathlib
import time
# Carte graphique : un essai qui ne répond pas (la RTX 5070 Ti « bloquait » plus de 5 min) laisse tout sur le processeur ;
# un essai réussi passe les traductions sur la carte ; un modèle qui n'y tient pas se charge sur le processeur.
sauves = (te.carte_nvidia, te.preparer_carte_graphique, te.ctranslate2, te.argos)
te.carte_nvidia, te.preparer_carte_graphique = (lambda: True), (lambda: True)
te.argos = types.SimpleNamespace(get_installed_packages=lambda: [types.SimpleNamespace(package_path=pathlib.Path("x"))])
essai_gpu = te.Traducteur.__new__(te.Traducteur)
essai_gpu.appareil, essai_gpu.modeles, essai_gpu.grands, essai_gpu.verrou = "cpu", {}, {}, threading.RLock()
te.ctranslate2 = types.SimpleNamespace(Translator=lambda *a, **k: time.sleep(3))
etat_bloque = essai_gpu.essayer_carte_graphique(delai=0.3)
faux_modele = types.SimpleNamespace(translate_batch=lambda *a, **k: [])
te.ctranslate2 = types.SimpleNamespace(Translator=lambda *a, **k: faux_modele)
etat_marche = essai_gpu.essayer_carte_graphique(delai=2)


def charger(chemin, device, **k):
    if device == "cuda":
        raise RuntimeError("CUDA out of memory")
    return "sur le processeur"


te.ctranslate2 = types.SimpleNamespace(Translator=charger)
repli = essai_gpu._creer("x")
te.carte_nvidia, te.preparer_carte_graphique, te.ctranslate2, te.argos = sauves
verifier(etat_bloque.startswith("Inutilisable") and etat_marche == "Active" and essai_gpu.appareil == "cuda"
         and repli == "sur le processeur", "carte graphique : bloquée → processeur, qui marche → carte, pleine → processeur",
         (etat_bloque, etat_marche, repli))
# Grands modèles : un même modèle (espagnol, portugais, italien) téléchargé une fois ; le jeton de la langue voulue en tête ;
# une paire directe sans modèle habituel traduit tout au grand modèle ; le chinois compte ses caractères comme des mots.
manquants = te.grands_modeles_manquants({("es", "fr"), ("pt", "fr"), ("it", "fr")})
verifier(manquants.count("itc_itc") <= 1 and te.GRANDS_MODELES[("es", "fr")] in te.MODELES_MULTI_CIBLES,
         "grand modèle partagé par trois langues : téléchargé une fois", manquants)
recus = []
faux_decoupeur = types.SimpleNamespace(encode=lambda t, out_type=str: t.split(), decode=lambda jetons: " ".join(jetons))
faux_grand = types.SimpleNamespace(translate_batch=lambda sources, **k: recus.extend(sources) or [
    types.SimpleNamespace(hypotheses=[["traduit"]]) for _ in sources])
essai_grand = te.Traducteur.__new__(te.Traducteur)
essai_grand.grands = {("es", "fr"): (faux_grand, faux_decoupeur, faux_decoupeur),
                      ("zh", "en"): (faux_grand, faux_decoupeur, faux_decoupeur)}
essai_grand.installees = {("es", "en"), ("en", "fr"), ("zh", "en")}
sortie = essai_grand._traduire_lot(["Hola"], "es", "fr") + essai_grand._traduire_lot(["保存更改"], "zh", "en")
verifier(sortie == ["traduit", "traduit"] and recus[0] == [">>fra<<", "Hola"] and recus[1] == ["保存更改"]
         and essai_grand._chemin("es", "fr") == [("es", "fr")] and essai_grand._chemin("zh", "fr") == [("zh", "en"), ("en", "fr")],
         "grands modèles : jeton >>fra<<, mot seul au modèle direct, chinois sans espaces, chemin direct", (sortie, recus))
verifier(("ja", "en") not in te.GRANDS_MODELES and ("ko", "en") not in te.GRANDS_MODELES,
         "japonais et coréen : pas de grand modèle (ceux essayés inventaient sur les pages de boutique)")
# Zone choisie : seulement ce qui est dedans, même le texte d'une image (on l'a choisie exprès).
FEN = (0, 0, 1200, 800)
dans_image = te.Texte("Limited edition", 100, 100, 120, 20, "morceau", FEN, "opera.exe")
hors_zone = te.Texte("Account settings", 700, 600, 140, 20, "morceau", FEN, "opera.exe")
lecteur_image = types.SimpleNamespace(vus=[(50, 50, 400, 300, "", "image")], a_relire=set(),
                                      vus_hors=lambda h: lecteur_image.vus, _a_traduire=lambda p: False,
                                      _est_pseudo=lambda *a: False)
app_zone = types.SimpleNamespace(textes_image=[dans_image, hors_zone], memoire_fusion={}, suivi=None,
                                 zone_choisie=(40, 40, 500, 400))
app_zone._dans_zone = lambda t: te.Application._dans_zone(app_zone, t)
lus_zone = [t.texte for t in te.Application._fusionner(app_zone, lecteur_image)]
app_zone.zone_choisie = None
lus_sans = [t.texte for t in te.Application._fusionner(app_zone, lecteur_image)]
verifier(lus_zone == ["Limited edition"] and lus_sans == [],
         "zone choisie : son texte lu même dans une image et un programme exclu, rien d'autre", (lus_zone, lus_sans))
choisies = []
selecteur = te.SelecteurZone.__new__(te.SelecteurZone)
selecteur.fini, selecteur.win = choisies.append, types.SimpleNamespace(destroy=lambda: None)
selecteur.depart = ((300, 200), (300, 200))
selecteur._fin(types.SimpleNamespace(x_root=100, y_root=260))
selecteur.depart = ((300, 200), (300, 200))
selecteur._fin(types.SimpleNamespace(x_root=304, y_root=203))  # un simple clic
verifier(choisies == [(100, 200, 300, 260), None], "zone : rectangle tracé dans tous les sens, simple clic annulé", choisies)
# Historique : une traduction restée à l'écran n'est notée qu'une fois ; ce qu'on écrit (Ctrl+Alt+Entrée) aussi.
app_histo = types.SimpleNamespace(historique=collections.deque(maxlen=300), _historique_vu={})
for _ in range(3):
    te.Application._noter_historique(app_histo, [(dans_image, "Édition limitée")])
te.Application._noter_historique(app_histo, [("Salut", "Hi")])
verifier([h[1:] for h in app_histo.historique] == [("Limited edition", "Édition limitée"), ("Salut", "Hi")],
         "historique : chaque traduction une fois", list(app_histo.historique))
# Vitres : au plus RESERVE_MAX gardées en réserve, les autres détruites (une grosse page en laissait des centaines).
calque_reserve = te.Calque.__new__(te.Calque)
detruites = []
calque_reserve.libres = [object()] * te.Calque.RESERVE_MAX
calque_reserve.hwnds = (1, 2, 3)
calque_reserve._rendre(types.SimpleNamespace(hwnd=2, cacher=lambda: None,
                                             win=types.SimpleNamespace(destroy=lambda: detruites.append(2))))
verifier(detruites == [2] and calque_reserve.hwnds == (1, 3) and len(calque_reserve.libres) == te.Calque.RESERVE_MAX,
         "vitres en réserve limitées : la vitre en trop est détruite")
# Signaler un problème : la page GitHub préremplie ne contient rien de personnel (ni nom d'utilisateur, ni chemin).
ouvertes = []
sauves_os = (te.os.startfile, te.subprocess.Popen, te.APP_DIR)
te.os.startfile, te.subprocess.Popen, te.APP_DIR = ouvertes.append, (lambda *a, **k: None), tempfile.mkdtemp()
te.Application.signaler_probleme(types.SimpleNamespace(etat_carte="Active"))
te.os.startfile, te.subprocess.Popen, te.APP_DIR = sauves_os
lien = ouvertes[0] if ouvertes else ""
verifier(lien.startswith(f"https://github.com/{te.DEPOT_GITHUB}/issues/new?") and os.environ["USERNAME"] not in lien
         and "Users" not in lien, "signaler un problème : page GitHub préremplie, rien de personnel", lien[:120])
verifier(te.jeu_plein_ecran() in (True, False), "plein écran exclusif : Windows répond")
# Grand modèle de traduction (MADLAD) : toutes les langues sauf l'anglais, directement, langue voulue en tête (« <2fr> »),
# seulement avec la carte graphique.
madlad_recus = []
faux_madlad = types.SimpleNamespace(translate_batch=lambda sources, **k: madlad_recus.extend(sources) or [
    types.SimpleNamespace(hypotheses=[["▁Tu", "▁veux", "▁jouer", "▁?"]]) for _ in sources])
faux_spm = types.SimpleNamespace(encode=lambda t, out_type=str: t.split(), decode=lambda j: "".join(j).replace("▁", " "))
essai_madlad = te.Traducteur.__new__(te.Traducteur)
essai_madlad.multilingue_permis, essai_madlad.appareil, essai_madlad.multilingue = True, "cuda", (faux_madlad, faux_spm)
essai_madlad.installees, essai_madlad.grands = {("ja", "en"), ("en", "fr")}, {}
sauve_present = te.madlad_present
te.madlad_present = lambda: True
chemins_madlad = (essai_madlad._chemin("ja", "fr"), essai_madlad._chemin("en", "fr"))
sortie_madlad = essai_madlad._traduire_lot(["今夜一緒にゲームしない？"], "ja", "fr")
essai_madlad.appareil = "cpu"
sans_carte = essai_madlad._chemin("ja", "fr")
te.madlad_present = sauve_present
verifier(chemins_madlad == ([("ja", "fr")], [("en", "fr")]) and madlad_recus[0][:1] == ["<2fr>"]
         and madlad_recus[0][-1] == "</s>" and sortie_madlad == ["Tu veux jouer ?"] and sans_carte == [("ja", "en"), ("en", "fr")],
         "grand modèle : direct sauf l'anglais, « <2fr> » en tête, rien sans la carte graphique", (chemins_madlad, madlad_recus))
# IA locale (moteur intégré) : seulement les phrases ; ses traductions remplacent les premières ; un nombre changé est
# refusé ; un texte refusé par le moteur garde sa traduction ; la couper remet les traductions habituelles.
essai_ia_trad = te.Traducteur.__new__(te.Traducteur)
essai_ia_trad.verrou, essai_ia_trad.mots, essai_ia_trad.generation = threading.RLock(), te.MesMots(), 0
essai_ia_trad.cache = {("Break a leg at your show tomorrow!", "fr"): "Cassez une jambe à votre spectacle demain !",
                       ("I have 3 cats at home now.", "fr"): "J'ai 3 chats à la maison maintenant.",
                       ("Open Project", "fr"): "Ouvrir le projet",
                       ("This text is far too long for the engine.", "fr"): "Ce texte est bien trop long pour le moteur."}
essai_ia_trad.cache_modifie = False
essai_ia_trad.de_ia = set()
essai_ia_trad._langue = lambda texte, cible: "en"
reponses_ia = {"Break a leg at your show tomorrow!": "Bonne chance pour ton spectacle demain !",
               "I have 3 cats at home now.": "J'ai 4 chats à la maison."}
invites_ia = []


class FauxMoteur:
    def completer(self, invite, longueur):
        invites_ia.append(invite)
        if "far too long" in invite:
            raise urllib.error.HTTPError("http://127.0.0.1", 400, "trop long", {}, None)
        return next(v for k, v in reponses_ia.items() if k in invite)

    def en_veille(self):
        pass


pretes = []
ia = te.IALocale(essai_ia_trad, lambda: pretes.append(1))
ia.moteur = FauxMoteur()
ia.actif = True
ia.proposer(list(k for k, _ in essai_ia_trad.cache), "fr")
proposes = [t for t, _ in ia.file]
arret_ia = threading.Event()
fil_ia = threading.Thread(target=ia.boucle, args=(arret_ia,), daemon=True)
fil_ia.start()
for _ in range(100):
    if not ia.file and pretes:
        break
    time.sleep(0.02)
apres_ia = dict(essai_ia_trad.cache)
ia.oublier()
arret_ia.set()
ia.reveil.set()
verifier("Open Project" not in proposes
         and apres_ia[("Break a leg at your show tomorrow!", "fr")].replace("\xa0", " ") == "Bonne chance pour ton spectacle demain !"
         and apres_ia[("I have 3 cats at home now.", "fr")] == "J'ai 3 chats à la maison maintenant."
         and apres_ia[("This text is far too long for the engine.", "fr")] == "Ce texte est bien trop long pour le moteur."
         and ("Break a leg at your show tomorrow!", "fr") not in essai_ia_trad.cache and pretes
         and invites_ia and all(i.startswith("<start_of_turn>user\nYou are a professional English (en) to French (fr)")
                                and i.endswith("<end_of_turn>\n<start_of_turn>model\n") for i in invites_ia),
         "IA locale : phrases seulement, traduction remplacée, nombre changé et texte refusé gardés, consigne de "
         "TranslateGemma, coupée : traductions habituelles", (proposes, apres_ia, invites_ia[:1]))
# La carte graphique de l'IA : une vraie carte avec assez de mémoire (pas la puce graphique du processeur), la plus
# grande ; aucune sinon.
liste_cartes = ("ggml_vulkan: Found 2 Vulkan devices:\n"
                "ggml_vulkan: 0 = Intel(R) UHD Graphics 770 (Intel Corporation) | uma: 1 | fp16: 1 | warp size: 32\n"
                "ggml_vulkan: 1 = AMD Radeon RX 7800 XT (AMD proprietary driver) | uma: 0 | fp16: 1 | warp size: 64\n"
                "Available devices:\n  Vulkan0: Intel(R) UHD Graphics 770 (16263 MiB, 15800 MiB free)\n"
                "  Vulkan1: AMD Radeon RX 7800 XT (16368 MiB, 15900 MiB free)\n")
petite_carte = ("ggml_vulkan: 0 = NVIDIA GeForce GTX 1050 (NVIDIA) | uma: 0 | fp16: 0\n"
                "Available devices:\n  Vulkan0: NVIDIA GeForce GTX 1050 (2048 MiB, 1900 MiB free)\n")
verifier(te.choisir_carte_ia(liste_cartes) == ("Vulkan1", "AMD Radeon RX 7800 XT")
         and te.choisir_carte_ia(petite_carte) is None and te.choisir_carte_ia("Available devices:\n") is None,
         "IA locale : vraie carte graphique avec 4 Go, jamais la puce du processeur",
         (te.choisir_carte_ia(liste_cartes), te.choisir_carte_ia(petite_carte)))
# Bug corrigé : le moteur qui démarre n'est pas « sans travail depuis 15 min » (en_veille l'arrêtait aussitôt prêt).
moteur_essai = te.MoteurIA()
moteur_essai.carte = ("Vulkan0", "essai")
inactif_au_demarrage = []


class FauxProcessus:
    pid, returncode, _handle = 1, None, 0

    def poll(self):
        return None


class FausseSante:
    """Réponse « /health » : note si le moteur passait pour inactif pendant son démarrage."""

    def __enter__(self):
        inactif_au_demarrage.append(time.monotonic() - moteur_essai.servi_le > te.VEILLE_IA)
        return self

    def __exit__(self, *a):
        return False


sauve_popen, sauve_lier, sauve_dossier = te.subprocess.Popen, te.lier_au_programme, te.DOSSIER_IA
te.subprocess.Popen = lambda *a, **k: FauxProcessus()
te.lier_au_programme = lambda processus: None
te.DOSSIER_IA = tempfile.mkdtemp()
moteur_essai.ouvreur = types.SimpleNamespace(open=lambda *a, **k: FausseSante())
try:
    moteur_essai.demarrer()
finally:
    te.subprocess.Popen, te.lier_au_programme = sauve_popen, sauve_lier
    shutil.rmtree(te.DOSSIER_IA, ignore_errors=True)
    te.DOSSIER_IA = sauve_dossier
moteur_essai.en_veille()
verifier(inactif_au_demarrage == [False] and moteur_essai.processus is not None,
         "IA locale : le moteur qui démarre n'est pas arrêté par la veille", inactif_au_demarrage)
moteur_essai.processus = None
# Ce que dit l'interrupteur de l'IA : pas de carte (même coupée), coupée, à télécharger, démarrage, active.
etat_app = types.SimpleNamespace(config={"ia_locale": False}, ia=types.SimpleNamespace(etat="", moteur=te.MoteurIA()))
sauve_presente = te.ia_presente
etats_ia = []
for carte, actif, presente, etat in ((False, True, True, ""), (None, False, False, ""), (None, True, False, ""),
                                     (("Vulkan0", "RTX"), True, True, ""), (("Vulkan0", "RTX"), True, True, "Active")):
    etat_app.ia.moteur.carte, etat_app.config["ia_locale"], etat_app.ia.etat = carte, actif, etat
    te.ia_presente = lambda presente=presente: presente
    etats_ia.append(te.Application.etat_ia.fget(etat_app))
te.ia_presente = sauve_presente
verifier(etats_ia == ["Pas de carte graphique adaptée sur ce PC", "Désactivé", "téléchargement de 2,4 Go au premier usage",
                      "démarrage…", "Active"], "IA locale : état affiché sous l'interrupteur", etats_ia)

# Bug (VRChat Creator Companion) : le même texte lu tantôt par l'accessibilité, tantôt par l'image (cadres décalés de
# quelques pixels) faisait reposer sa traduction à chaque relecture : elle clignotait. La vitre reste.
calque_clignote = te.Calque.__new__(te.Calque)
calque_clignote.visible, calque_clignote.generation, calque_clignote.fondu = True, 0, False
calque_clignote.affiches, calque_clignote.dispositions, vitres_rendues = {}, {}, []
calque_clignote._vitre = lambda: types.SimpleNamespace(montrer=lambda *a, **k: None)
calque_clignote._rendre = vitres_rendues.append
par_accessibilite = te.Texte("Logs", 319, 763, 35, 22, "morceau", (0, 0, 2000, 2000), "creatorcompanion.exe")
par_image = te.Texte("Logs", 324, 760, 36, 23, "morceau", (0, 0, 2000, 2000), "creatorcompanion.exe")
disposition_a = ((("Journaux",),), 15, 315, 759, 70, 30, (30, 30, 30), (230, 230, 230), False, 100, False)
disposition_b = ((("Journaux",),), 15, 320, 756, 70, 31, (32, 31, 30), (228, 230, 231), False, 100, False)
calque_clignote.mettre_a_jour([(par_accessibilite, disposition_a, None)])
vitre_avant = calque_clignote.affiches[par_accessibilite]
calque_clignote.mettre_a_jour([(par_image, disposition_b, None)])
verifier(calque_clignote.affiches.get(par_image) is vitre_avant and not vitres_rendues
         and calque_clignote.dispositions[par_image] == disposition_a,
         "traduction relue à quelques pixels près : la même vitre reste (plus de clignotement)", vitres_rendues)
# Lecture de l'image : une zone identique à la même place n'est pas relue (curseur qui clignote…) ; changée, si.
lecteur_memoire = te.LecteurImage.__new__(te.LecteurImage)
lecteur_memoire.memoire, lecteur_memoire.evitees, lectures_faites = collections.OrderedDict(), 0, []
lecteur_memoire.lire = lambda pixels, origine, zone: lectures_faites.append(zone) or ["lu"]
image_memoire = np.zeros((100, 200, 4), np.uint8)
premiere_lecture = lecteur_memoire.lire_memoire(image_memoire, (0, 0), (0, 0, 200, 50))
lecteur_memoire.lire_memoire(image_memoire, (0, 0), (0, 0, 200, 50))
image_memoire[10, 10] = 255
lecteur_memoire.lire_memoire(image_memoire, (0, 0), (0, 0, 200, 50))
verifier(premiere_lecture == ["lu"] and len(lectures_faites) == 2 and lecteur_memoire.evitees == 1,
         "lecture de l'image : zone identique pas relue, zone changée relue", (lectures_faites, lecteur_memoire.evitees))
# Bugs trouvés à la relecture (2026-10-14) :
# 1. un « [ » dans le texte coupait toute protection (nombres changés, texte abandonné) : les crochets sont protégés ;
# 2. un nombre collé à du japonais protégeait toute la phrase (elle revenait sans être traduite) ;
# 6. « Loading.......... » passait pour une erreur du modèle.
crochets_proteges = te.proteger_code("[Info] Meet me at 4 PM tomorrow", None, True)
japonais_protege = te.proteger_code("明日の午後4時に駅で会いましょう", None, True)
verifier(crochets_proteges == ("[A] Meet me at [B] PM tomorrow", ["[Info]", "4"])
         and te.remettre_code("[A] Rendez-vous à [B] PM demain", crochets_proteges[1]) == "[Info] Rendez-vous à 4 PM demain"
         and japonais_protege == ("明日の午後[A]時に駅で会いましょう", ["4"])
         and not te.traduction_aberrante("Chargement..........", "Loading..........")
         and te.traduction_aberrante("??????????????", "Why?"),
         "protection : crochets et nombres (même en japonais), répétitions du texte d'origine acceptées",
         (crochets_proteges, japonais_protege))
# 3. l'IA perdait le guillemet final et collait les lignes d'un paragraphe ; des guillemets autour de toute sa
# réponse sont retirés.
ia_verif = te.IALocale.__new__(te.IALocale)
ia_verif.traducteur = types.SimpleNamespace(_en_capitales=lambda texte: False)
verifies = [ia_verif._verifier('He said "hello"', 'Il a dit "bonjour"', "fr"),
            ia_verif._verifier("Line one is here now\nLine two is here now", "Ligne un est ici\nLigne deux est ici", "fr"),
            ia_verif._verifier("Please click the button below", '"Cliquez sur le bouton ci-dessous"', "fr")]
verifier(verifies == ['Il a dit "bonjour"', "Ligne un est ici\nLigne deux est ici", "Cliquez sur le bouton ci-dessous"],
         "IA : guillemets et lignes gardés", verifies)
# 4. couper l'IA après un redémarrage laissait ses traductions : elles sont gardées sur le disque avec la marque IA.
avant_ia = te.Traducteur.__new__(te.Traducteur)
avant_ia.verrou, avant_ia.cache_modifie = threading.RLock(), True
avant_ia.cache = {("Click the button below now", "fr"): "TRADUCTION DE L'IA", ("Hello there", "fr"): "Bonjour"}
avant_ia.de_ia = {("Click the button below now", "fr")}
avant_ia.sauver()
apres_ia = te.Traducteur.__new__(te.Traducteur)
apres_ia.verrou, apres_ia.cache, apres_ia.de_ia, apres_ia.generation = threading.RLock(), {}, set(), 0
apres_ia._charger_cache()
te.IALocale(apres_ia, lambda: None).oublier()
verifier(("Click the button below now", "fr") not in apres_ia.cache and apres_ia.cache.get(("Hello there", "fr")) == "Bonjour",
         "IA coupée après un redémarrage : ses traductions sont retirées, pas les autres", apres_ia.cache)
# 5. une traduction de l'IA faite pendant un changement de « Mes mots » n'est pas gardée (anciennes règles).
regles_trad = te.Traducteur.__new__(te.Traducteur)
regles_trad.verrou, regles_trad.mots, regles_trad.generation = threading.RLock(), te.MesMots(), 0
regles_trad.cache, regles_trad.de_ia, regles_trad.cache_modifie = {("Click the button below to go on", "fr"): "Cliquez"}, set(), False
regles_trad._langue = lambda texte, cible: "en"


class MoteurPendantChangement:
    def completer(self, invite, longueur):
        regles_trad.changer_mots(["button = bouton-perso"])  # (pendant que l'IA travaille)
        regles_trad.cache[("Click the button below to go on", "fr")] = "Cliquez sur le bouton-perso"
        return "Cliquez sur le bouton ci-dessous pour continuer"

    def en_veille(self):
        pass


ia_regles = te.IALocale(regles_trad, lambda: None)
ia_regles.moteur, ia_regles.actif = MoteurPendantChangement(), True
ia_regles.proposer(["Click the button below to go on"], "fr")
arret_regles = threading.Event()
threading.Thread(target=ia_regles.boucle, args=(arret_regles,), daemon=True).start()
for _ in range(100):
    if not ia_regles.file:
        break
    time.sleep(0.02)
time.sleep(0.2)
arret_regles.set()
ia_regles.reveil.set()
verifier(regles_trad.cache.get(("Click the button below to go on", "fr")) == "Cliquez sur le bouton-perso",
         "IA : un lot traduit pendant un changement de « Mes mots » n'est pas gardé", regles_trad.cache)

# --------------------------------------------------------------------------- avec les vrais modèles
print("Traductions (vrais modèles)")
installees = te.langues_installees("fr")
traducteur = te.Traducteur(installees)
# Bugs du site VRChat (2026-10-14) : « Logout » pris pour un pseudo, « Description » devenu « Désignation des
# marchandises », libellés en mots anglais qui existent aussi en français (« World Info », « Public Link ») gardés en
# anglais, « Help Desk » et « World Store » recopiés tels quels par le modèle.
libelles_vrchat = ["Logout", "Description", "World Info", "Public Link", "Help Desk", "World Store", "Favorite Avatars",
                   "Invite Me"]
traduits_vrchat = traducteur.traduire(libelles_vrchat, "fr", filtre=te.texte_de_page_a_traduire)
verifier(traduits_vrchat[0] == "Déconnexion" and traduits_vrchat[1] in (None, "Description")
         and all(traduits_vrchat[2:]),
         "site VRChat : boutons courts traduits, « Description » reste « Description »",
         list(zip(libelles_vrchat, traduits_vrchat)))
# Bugs trouvés à la relecture : un texte avec des crochets est traduit, ses nombres gardés ; ce qu'on écrit garde son
# heure ; une erreur passagère (carte graphique pleine) ne rend pas une paire de langues impossible.
avec_crochets = traducteur.traduire(["[Info] Meet me at 4 PM tomorrow"], "fr")[0]
ecrit = traducteur.traduire_ecrit("On se voit à 21h demain [ok]", "fr", "en") if ("fr", "en") in te.paires_installees() else "21"
sauve_lot = traducteur._traduire_avec_reperes
traducteur._traduire_avec_reperes = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CUDA out of memory"))
traducteur._traduire(["A brand new sentence to translate now"], "fr")
traducteur._traduire_avec_reperes = sauve_lot
passagere = ("en", "fr") not in traducteur.paires_impossibles and ("en", "fr") in traducteur.echecs
traducteur.echecs.clear()
verifier(avec_crochets and "[Info]" in avec_crochets and "4" in avec_crochets and ecrit and "21" in ecrit and passagere,
         "crochets et nombres gardés, erreur passagère réessayée", (avec_crochets, ecrit, passagere))
traducteur.prechauffer("fr")
verifier(("en", "fr") in traducteur.modeles, "modèles anglais → français préchargés à l'activation")
CAS = [
    # (texte, ce qui doit apparaître dans la traduction, ce qui ne doit pas y être)
    ("Please read the terms of use before buying this item.", ["conditions"], []),
    ("You're authoring a skill rather than finding one — use the `skill-creator` skill", ["`skill-creator`"], ["⁇"]),
    ("Environments without Node, since every command runs through `npx` — install manually instead", ["`npx`"], ["⁇"]),
    ("idk tbh ngl that was kinda sus lol", ["Je ne sais pas"], []),
    ("thx for the sub", ["abonnement"], []),
]
if "ja" in installees:
    CAS.append(("アニメーションは、.zipファイル内のunitypackageに含まれています。", [".zip"], ["⁇"]))
for texte, presents, absents in CAS:
    traduit = traducteur.traduire([texte], "fr")[0] or ""
    verifier(traduit and all(p in traduit for p in presents) and not any(a in traduit for a in absents),
             "traduit : " + texte[:45], traduit)
verifier(traducteur.traduire(["Unity"], "fr")[0] is None, "« Unity » seul n'est pas traduit")
# Phrase japonaise qui commence par un mot latin : prise pour un pseudo (un seul « mot » sans espaces), jamais traduite.
verifier(not any(te.ressemble_a_un_pseudo(p, lambda: False) for p in ("Quest版には対応しておりません。",
                                                                      "Modular Avatarに対応しています。"))
         and te.ressemble_a_un_pseudo("Kiriko", lambda: False),
         "phrase japonaise avec un mot latin : pas un pseudo (traduite dans les pages)")
# Un pseudo en minuscules (profil VRChat) devenait « C'est pourquoi je vous prie de bien vouloir me faire confiance. »
verifier(te.ressemble_a_un_pseudo("thithis", lambda: False) and not te.ressemble_a_un_pseudo("settings", lambda: False)
         and traducteur.traduire(["thithis"], "fr")[0] is None
         and traducteur.traduire(["thithis"], "fr", filtre=te.texte_de_page_a_traduire)[0] is None
         and traducteur.traduire(["settings"], "fr")[0] not in (None, "settings"),
         "pseudo en minuscules jamais traduit (ni inventé), vrai mot en minuscules traduit",
         traducteur.traduire(["thithis", "settings"], "fr"))
libelles = traducteur.traduire(["Add to cart", "Followers", "Mark as read", "Sign in", "Contact this shop"], "fr")
verifier(libelles == ["Ajouter au panier", "Abonnés", "Marquer comme lu", "Se connecter", "Contacter cette boutique"],
         "libellés courts bien traduits", libelles)
traduit = traducteur.traduire(["Sale ends March 31st"], "fr", filtre=te.texte_de_page_a_traduire)[0] or ""
verifier("31 mars" in traduit and "31st" not in traduit, "date avec « 31st » traduite (aussi dans les pages)", traduit)
traduit = traducteur.traduire([recolles[0][4]], "fr")[0] or ""
verifier("_CameraDepthTexture" in traduit and "UNITY_Z_0_FAR_FROM_CLIPSPACE" in traduit and M not in traduit
         and "profondeur" in traduit, "phrase recollée traduite, code intact", traduit)
date = "Limited Time: Pre-orders close on Saturday, October 31st, 2026 at 16:00 (4 PM CEST)!"
traduit = traducteur.traduire([date], "fr")[0] or ""
verifier(traduit and "2026" in traduit and "16:00" in traduit, "phrase avec date et heure traduite (nombres gardés)", traduit)

avec_mots = te.Traducteur(installees, ["Mayo", "watch streak = série de visionnage"])
traduit = avec_mots.traduire(["The Mayo avatar is really beautiful and easy to use."], "fr")[0] or ""
verifier("Mayo" in traduit and "avatar" in traduit.lower(), "Mes mots : mot gardé dans la traduction", traduit)
traduit = avec_mots.traduire(["mattye_west watched 7 consecutive streams and sparked a watch streak!"], "fr")[0] or ""
verifier("série de visionnage" in traduit, "Mes mots : traduction imposée", traduit)
verifier(avec_mots.traduire(["Watch streak"], "fr")[0] == "série de visionnage", "Mes mots : libellé seul")

if ("fr", "en") in te.paires_installees():
    ecrit = traducteur.traduire_ecrit("Salut, tu veux jouer avec nous ce soir ?\nJe lance le monde à 21h.", "fr", "en") or ""
    verifier("tonight" in ecrit.lower() and "\n" in ecrit and "21" in ecrit, "Ctrl+Alt+Entrée : écrit en français → anglais", ecrit)
    verifier(traducteur.deja_en("Hey, do you want to play with us tonight?", "en")
             and not traducteur.deja_en("Salut, tu veux jouer avec nous ce soir ?", "en"), "Ctrl+Alt+Entrée : déjà en anglais")
verifier(te.touches_du_raccourci(te.RACCOURCIS["ecrire"][1]) == (3, 0x0D), "Ctrl+Alt+Entrée lu pour Windows")

# Bloc de boutique : japonais, anglais et numéros de version mêlés, sur plusieurs lignes (BOOTH).
bloc = ("作成環境:Unity3D 2022.3.22f1 Adobe Photoshop 2025 Visual Studio 2026\n\n"
        "A total of two groups of dynamic expressions, local expression UI display.\n"
        "Supports customized stacking of face shape keys. Expression can be paused and continued.\n\n"
        "The product does not include the 3D model.\nPlease purchase it yourself.")
if "ja" in installees:
    bloc = "頬を指でつついたり引っ張ったりするインタラクション表現、口を開ける表現。\n" + bloc
traduit = traducteur.traduire([bloc], "fr")[0] or ""
lignes, lignes_traduites = bloc.split("\n"), traduit.split("\n")
verifier(len(lignes) == len(lignes_traduites), "bloc multiligne : toutes les lignes gardées", traduit[:200])
verifier("achet" in traduit.lower() and "two groups" not in traduit, "bloc multiligne : l'anglais est traduit", traduit[-300:])
verifier("2022.3.22f1" in traduit, "bloc multiligne : la ligne de versions reste intacte")
filtre_page = traducteur.traduire([bloc], "fr", filtre=te.texte_de_page_a_traduire)[0]
verifier(filtre_page == traduit, "bloc multiligne : même résultat pour l'extension")
long = " ".join(["This feature was added in version 2.0 of the software."] * 2 + ["It works with every avatar you own."] * 5)
verifier(traducteur.traduire([long], "fr")[0] is not None, "long paragraphe : une phrase ratée ne bloque pas les autres")
# Un texte en plusieurs morceaux dont une langue est en échec passager (réessayée 30 s plus tard) : pas mémorisé comme
# « rien à traduire », sinon il le restait pour de bon.
if "ja" in installees:
    melange = "Please read the terms of use.\n今日はとても良い天気ですね。一緒に散歩しませんか。"
    traducteur.echecs[("ja", "fr")] = time.monotonic()
    traducteur.traduire([melange], "fr")
    memorise_en_echec = (melange, "fr") in traducteur.cache
    traducteur.echecs.clear()
    traduit_melange = traducteur.traduire([melange], "fr")[0] or ""
    verifier(not memorise_en_echec and "conditions" in traduit_melange and "今日" not in traduit_melange,
             "texte en morceaux : un échec passager n'est pas mémorisé, retraduit ensuite", traduit_melange)

# --------------------------------------------------------------------------- bandeaux, de bout en bout
# Une fausse fenêtre d'appli (fond sombre, bouton coloré en gras, dates) passe par toute la chaîne : lecture de
# l'image, fusion, traduction, mise en page. Chaque traduction doit couvrir exactement son texte, avec la même
# graisse ; un fond qui bouge derrière (vidéo, fenêtre translucide) ne doit rien faire relire ni bouger.
print("Bandeaux (de bout en bout)")


def fausse_fenetre(decalage_fond=0):
    image = Image.new("RGB", (900, 260), (37, 37, 37))
    d = ImageDraw.Draw(image)
    for x in range(0, 900, 6):  # un fond légèrement texturé qui peut « bouger » (vidéo vue à travers)
        d.line((x + decalage_fond % 6, 0, x + decalage_fond % 6, 8), fill=(42, 42, 44))
    for i, y in enumerate((60, 120, 180)):
        d.rounded_rectangle((720, y, 836, y + 33), 3, fill=(23, 145, 168))
        d.text((778, y + 16), "Open Project", font=ImageFont.truetype("segoeuib.ttf", 15), fill="white", anchor="mm")
        d.text((560, y + 16), "a month ago", font=ImageFont.truetype("segoeui.ttf", 15), fill=(220, 220, 220), anchor="lm")
    d.text((40, 76), "Settings", font=ImageFont.truetype("segoeui.ttf", 16), fill=(230, 230, 230), anchor="lm")
    return np.ascontiguousarray(np.dstack([np.asarray(image)[..., ::-1], np.full((260, 900), 255, np.uint8)]))


image_courante = [fausse_fenetre()]
suivi_bout = te.SuiviEcran(lambda: (image_courante[0], (0, 0)))
suivi_bout.mouvement_recent = lambda: False
suivi_bout.analyser()
lecteur_vide = types.SimpleNamespace(vus=[], a_relire=set(), vus_hors=lambda h: [], _a_traduire=lambda p: True,
                                     _est_pseudo=lambda *a: False)
app_bout = types.SimpleNamespace(textes_image=list(suivi_bout.morceaux), memoire_fusion={}, suivi=suivi_bout)
textes_bout = te.Application._fusionner(app_bout, lecteur_vide)
# (La fenêtre de chaque texte est demandée au vrai écran : une fenêtre ouverte là, comme celle des réglages en haut à
# gauche, faisait échouer l'essai. La fausse fenêtre est toute l'image.)
textes_bout = [replace(t, fenetre=(0, 0, 900, 260)) for t in textes_bout]
traducteur.traduire([t.texte for t in textes_bout], "fr")
calque_essai = te.Calque.__new__(te.Calque)
calque_essai.style, calque_essai.facteur_taille = "auto", 1.0
mauvais, boutons_gras = [], 0
for t in textes_bout:
    traduction_bout = traducteur.cache.get((t.texte, "fr"))
    if not traduction_bout:
        continue
    teintes = te.couleurs_autour(image_courante[0], (0, 0), t)
    d = calque_essai._disposer(t, traduction_bout, teintes, textes_bout, ())
    if d is None or not (d[2] <= t.x and d[3] <= t.y and d[2] + d[4] >= t.x + t.l and d[3] + d[5] >= t.y + t.h):
        mauvais.append((t.texte, (t.x, t.y, t.l, t.h), d and d[2:6]))
    boutons_gras += bool(t.texte == "Open Project" and d and d[-1])
lus_bout = sorted(t.texte for t in textes_bout)
verifier(lus_bout.count("Open Project") == 3 and lus_bout.count("a month ago") == 3 and "Settings" in lus_bout
         and not mauvais and boutons_gras == 3,
         "fausse appli : tout lu, chaque traduction couvre son texte, boutons en gras", (lus_bout, mauvais, boutons_gras))
avant_fond = set(suivi_bout.morceaux)
image_courante[0] = fausse_fenetre(decalage_fond=3)
suivi_bout.analyser()
suivi_bout.analyser()
verifier(set(suivi_bout.morceaux) == avant_fond, "fond qui bouge derrière : rien n'est relu ni déplacé")

# --------------------------------------------------------------------------- serveur de l'extension
print("Serveur de l'extension")
te.PORT_EXTENSION = 47199  # (le vrai programme peut tourner sur le port habituel)
app = types.SimpleNamespace(actif=True, config={"langue_cible": "fr"}, traducteur=traducteur, commandes=types.SimpleNamespace(put=lambda c: None))
app.extension_vue = lambda agent: None
app.etat_extension = lambda agent: te.Application.etat_extension(app, agent)
app.traduire_pour_extension = lambda textes, agent: te.Application.traduire_pour_extension(app, textes, agent)
serveur = te.ThreadingHTTPServer(("127.0.0.1", te.PORT_EXTENSION), te.RequeteExtension)
serveur.app = app
threading.Thread(target=serveur.serve_forever, daemon=True).start()


def demande(methode, chemin, corps=None, entetes=None):
    connexion = http.client.HTTPConnection("127.0.0.1", te.PORT_EXTENSION, timeout=120)
    connexion.request(methode, chemin, body=corps, headers=entetes or {})
    reponse = connexion.getresponse()
    return reponse.status, reponse.read().decode("utf-8")


EXT = {"X-Traducteur-Ecran": "1", "Content-Type": "application/json"}
code, corps = demande("GET", "/etat", entetes=EXT)
etat = json.loads(corps) if code == 200 else {}
verifier(code == 200 and {"actif", "langue", "version", "extension", "theme"} <= set(etat), "/etat complet", corps)
verifier(demande("GET", "/etat")[0] == 403, "sans l'en-tête de l'extension : refusé")
verifier(demande("GET", "/etat", entetes={**EXT, "Host": "site.com"})[0] == 403, "autre adresse : refusé")
verifier(demande("OPTIONS", "/traduire", entetes={"Origin": "https://site.com"})[0] == 501, "page web : refusée")
verifier(demande("POST", "/reglage", json.dumps({"langue": "xx"}), EXT)[0] == 400, "réglage invalide : refusé")
code, corps = demande("POST", "/traduire", json.dumps({"textes": [bloc, "Sign in", "SQUEEZIE"]}), EXT)
traductions = json.loads(corps).get("traductions", []) if code == 200 else []
verifier(code == 200 and traductions[:1] == [traduit] and traductions[2] is None, "/traduire : bloc traduit, pseudo laissé", corps[:200])
app.zone_temporaire = True  # zone choisie pendant la pause : les pages web restent comme elles sont
pause_zone = json.loads(demande("GET", "/etat", entetes=EXT)[1]).get("actif")
app.zone_temporaire = False
verifier(pause_zone is False, "zone choisie pendant la pause : l'extension reste en pause")
serveur.shutdown()

# --------------------------------------------------------------------------- extension
print("Extension")
dossier = os.path.join(ICI, "extension")
for fichier in ("page.js", "fond.js", "fenetre.js"):
    resultat = subprocess.run(["node", "--check", os.path.join(dossier, fichier)], capture_output=True, text=True)
    verifier(resultat.returncode == 0, "syntaxe de " + fichier, resultat.stderr[-200:])
with open(os.path.join(dossier, "manifest.json"), encoding="utf-8") as f:
    manifeste = json.load(f)
verifier(manifeste.get("version") == etat.get("extension"), "le programme connaît la version de l'extension")
verifier({"storage", "alarms"} <= set(manifeste.get("permissions", [])), "permissions de l'extension")
# « 'background.scripts' requires manifest version of 2 or lower » (Opera, Chrome) : « scripts » n'est que pour Firefox,
# ajouté à sa copie de l'extension par construire_exe.bat.
with open(os.path.join(ICI, "construire_exe.bat"), encoding="utf-8") as f:
    construction = f.read()
with open(os.path.join(dossier, "fond.js"), encoding="utf-8") as f:
    fond = f.read()
verifier('installType === "development"' in fond and "enDeveloppement &&" in fond,
         "extension installée depuis une boutique : jamais de « ↻ » (elle se met à jour toute seule)")
verifier(set(manifeste.get("background", {})) == {"service_worker"}
         and "m['background']={'scripts': ['fond.js']}" in construction,
         "extension : pas d'erreur « background.scripts » dans Chrome et Opera, Firefox a ses « scripts »")
with open(os.path.join(dossier, "page.js"), encoding="utf-8") as f:
    page = f.read()
verifier("whiteSpace" in page and "< 20000" in page, "page.js garde les retours à la ligne et accepte les longs textes")

print()
print("TOUT EST BON" if not echecs else f"{len(echecs)} ÉCHEC(S) : " + " ; ".join(echecs))
sys.exit(1 if echecs else 0)
