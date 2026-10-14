"""Traducteur d'écran en temps réel.

Lit le texte visible à l'écran (logiciels, sites internet…) avec la reconnaissance
de texte de Windows, complétée par l'accessibilité de Windows (UI Automation) quand
elle voit le même texte. La langue de chaque texte est détectée automatiquement :
seul ce qui n'est pas déjà dans la langue choisie est traduit (hors-ligne) et
affiché par-dessus, aux couleurs de la page. Les pseudos, noms propres, nombres,
bouts de code et le texte des images et vidéos sont laissés tels quels.

  Ctrl+Alt+T : activer / désactiver la traduction
"""

import asyncio
import bisect
import collections
import functools
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
import ctypes
import hashlib
import json
import logging
import os
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

# Les fils de calcul de la traduction ne doivent pas tourner à vide en attendant du travail
# (comportement par défaut d'OpenMP) : sinon le processeur est occupé pour rien entre deux phrases.
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("KMP_BLOCKTIME", "0")

import queue
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unicodedata
import shutil
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from tkinter import filedialog
import winreg
from ctypes import wintypes
from dataclasses import dataclass, replace

import argostranslate.package as argos
import comtypes
import comtypes.client
import ctranslate2
import customtkinter as ctk
import keyboard
import mss
import numpy as np
import pystray
import sentencepiece
from py3langid.langid import MODEL_FILE as MODELE_LANGID
from py3langid.langid import LanguageIdentifier
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from winrt.windows.globalization import Language as LangueWindows
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter
from wordfreq import zipf_frequency as frequence_mot

try:
    import dxcam  # capture d'écran par la carte graphique (duplication d'écran DirectX)
except Exception:  # absent ou inutilisable : on se contente de la copie classique (mss)
    dxcam = None

APP_NAME = "TraducteurEcran"
APP_DIR = os.path.join(os.environ["APPDATA"], APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "journal.txt")
ICONE_PATH = os.path.join(APP_DIR, "icone.ico")
CACHE_TRADUCTIONS = os.path.join(APP_DIR, "traductions.json")  # traductions déjà faites
NAVIGATEURS = {"chrome.exe", "msedge.exe", "opera.exe", "brave.exe", "vivaldi.exe", "firefox.exe"}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ARG_DEMARRAGE = "--demarrage"  # lancé par Windows : pas de fenêtre de réglages
EST_EXE = getattr(sys, "frozen", False)  # version .exe (PyInstaller)
# Grands modèles de traduction (OPUS-MT « transformer-big », convertis pour CTranslate2), bien
# meilleurs sur les phrases que ceux d'Argos : cherchés à côté du programme (version .exe), puis
# dans le dossier des réglages.
# Chacun gardé après comparaison avec la traduction d'Argos, sur des phrases d'interface, de discussion et de
# boutique (VRChat, BOOTH : « Modular Avatar », « README.txt », « version Quest ») : celle-ci inventait parfois
# (polonais, chinois : « Impossible de fermer le dossier temporaire : %s ») ou coupait des phrases (italien).
# Écartés : japonais (opus-mt-ja-en) et coréen (opus-mt-ko-en), pires sur les phrases de boutique (« (Rires) »,
# « lire le médicament avant de l'acheter ») ; tc-big-ko-en ne connaît pas le hangeul.
GRANDS_MODELES = {
    ("en", "fr"): "en_fr",
    # Directement vers le français (sans passer par l'anglais).
    ("es", "fr"): "itc_itc", ("pt", "fr"): "itc_itc", ("it", "fr"): "itc_itc",  # opus-mt-tc-big-itc-itc
    ("ru", "fr"): "zle_fr", ("uk", "fr"): "zle_fr",  # opus-mt-tc-big-zle-fr
    # Vers l'anglais : la première étape (le grand modèle anglais -> français fait la seconde).
    ("pl", "en"): "zlw_en",  # opus-mt-tc-big-zlw-en
    ("zh", "en"): "zhx_en",  # opus-mt-tc-bible-big-zhx-en
}
# Modèles qui traduisent vers plusieurs langues : la langue voulue est indiquée par un jeton en tête du texte.
JETONS_CIBLE = {"fr": ">>fra<<", "es": ">>spa<<", "pt": ">>por<<", "it": ">>ita<<", "ro": ">>ron<<"}
MODELES_MULTI_CIBLES = {"itc_itc"}
# « Grand modèle de traduction » (réglage) : MADLAD-400 3B (Google, licence Apache 2.0), converti pour CTranslate2 par
# Nextcloud, ~3 Go, sur la carte graphique seulement. Bien meilleur que les petits modèles pour toutes les langues sauf
# l'anglais (japonais, chinois, coréen… : « Modular Avatar », « README.txt » gardés, sens juste) ; pour l'anglais, les
# modèles habituels restent meilleurs (boutons, argot : MADLAD donnait « Ajouter à la liste d'envies » pour « Add to cart »).
MADLAD = {"depot": "Nextcloud-AI/madlad400-3b-mt-ct2-int8", "dossier": "madlad",
          "fichiers": ("config.json", "shared_vocabulary.json", "spiece.model", "model.bin")}
# Carte graphique NVIDIA : sa bibliothèque de calcul (cuBLAS) est téléchargée à part, seulement si elle sert.
DOSSIER_GPU = os.path.join(APP_DIR, "carte_graphique")
FICHIERS_GPU = ("cublas64_12.dll", "cublasLt64_12.dll")
CUBLAS = ("nvidia-cublas-cu12", "12.9.2.10")  # (paquet publié par NVIDIA sur PyPI ; version figée, vérifiée)
VERSION_PROGRAMME = "2.3.0"
# Dépôt GitHub (« compte/dépôt ») où sont publiées les versions (mises à jour) et les grands modèles. Vide :
# ni mise à jour automatique, ni grand modèle téléchargé (celui-ci peut alors être livré à côté du programme).
DEPOT_GITHUB = "N3TRAXY/traducteur-ecran"
DOSSIERS_MODELES = ([os.path.join(os.path.dirname(sys.executable), "modeles")] if EST_EXE else []) + [
    os.path.join(APP_DIR, "modeles")]

# Nom affiché -> (code du modèle de traduction, langue pour la détection)
LANGUES = {
    "Français": "fr",
    "Anglais": "en",
    "Espagnol": "es",
    "Allemand": "de",
    "Italien": "it",
    "Portugais": "pt",
    "Néerlandais": "nl",
    "Polonais": "pl",
    "Roumain": "ro",
    "Suédois": "sv",
    "Turc": "tr",
    "Grec": "el",
    "Russe": "ru",
    "Ukrainien": "uk",
    "Arabe": "ar",
    "Hindi": "hi",
    "Chinois": "zh",
    "Japonais": "ja",
    "Coréen": "ko",
    "Vietnamien": "vi",
    "Thaï": "th",
    "Indonésien": "id",
    "Tchèque": "cs",
    "Hongrois": "hu",
    "Danois": "da",
    "Norvégien": "nb",
    "Finnois": "fi",
    "Bulgare": "bg",
    "Hébreu": "he",
    "Persan": "fa",
}
TAILLE_MODELE = 100  # Mo, à peu près, par modèle de traduction à télécharger
DOSSIER_TELECHARGEMENTS = os.path.join(APP_DIR, "telechargements")  # modèles en cours de téléchargement
NOM_PAR_CODE = {code: nom for nom, code in LANGUES.items()}

# Couleurs (fond, texte) des traductions quand on ne copie pas celles de l'écran.
# Le style « auto » reprend la couleur du fond et du texte d'origine.
STYLES = {
    "clair": ((250, 250, 252), (20, 22, 30)),
    "sombre": ((30, 32, 38), (240, 241, 245)),
}
NOMS_STYLES = {"auto": "Auto", "clair": "Clair", "sombre": "Sombre"}
# Taille du texte des traductions : nom affiché, et facteur par rapport au texte d'origine.
TAILLES = {"petite": ("Petite", 0.88), "normale": ("Normale", 1.0), "grande": ("Grande", 1.15)}
# Qualité des traductions, en un seul choix : normale (le processeur), meilleure (la carte graphique NVIDIA et le
# grand modèle de traduction), maximale (les deux, et l'IA locale). Voir Application.choisir_qualite.
NIVEAUX_QUALITE = {"normale": "Normale", "meilleure": "Meilleure", "maximale": "Maximale (IA)"}
# Réactivité : nom affiché, et secondes entre deux analyses de l'écran (juste après une action, au repos).
REACTIVITES = {"econome": ("Économe", 0.3, 1.0), "normale": ("Normale", 0.1, 0.5), "rapide": ("Rapide", 0.05, 0.25)}
# Combinaisons que tout le monde utilise déjà (copier, coller, fermer…) : refusées comme raccourci.
RACCOURCIS_RESERVES = {"ctrl+" + t for t in "acvxyzsfpnwtrq"} | {"alt+f4", "alt+tab", "ctrl+alt+delete", "ctrl+shift+esc",
                                                              "windows+d", "windows+l", "windows+e", "windows+r"}
# Réglage « Programmes » : la liste choisie est-elle celle des programmes à ne pas traduire, ou à traduire ?
MODES_PROGRAMMES = {"sauf": "Tous sauf la liste", "seulement": "Seulement la liste"}
# Programmes de Windows qui ont des fenêtres mais qu'on ne proposera pas dans la liste.
PROGRAMMES_SYSTEME = {"svchost.exe", "textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe",
                      "startmenuexperiencehost.exe", "lockapp.exe", "dwm.exe", "sihost.exe", "ctfmon.exe",
                      "runtimebroker.exe", "applicationframehost.exe", "searchapp.exe", "widgets.exe",
                      "pythonw.exe", "traducteurecran.exe", ""}

CONFIG_DEFAUT = {
    "langue_cible": "fr",
    "actif": True,  # traduction active dès le lancement
    "style": "auto",  # couleur des traductions : "auto" (celles de l'écran), "clair" ou "sombre"
    "intervalle": 1.0,  # secondes max entre deux lectures (sinon : dès qu'il se passe quelque chose)
    "raccourci": "ctrl+alt+t",
    "taille": "normale",  # taille du texte des traductions (voir TAILLES)
    "opacite": 100,  # opacité des traductions, en %
    "fondu": True,  # les traductions apparaissent en douceur
    "programmes": [],  # liste de programmes choisis (« steam.exe »…)
    "mode_programmes": "sauf",  # "sauf" : tout sauf la liste ; "seulement" : seulement la liste
    "reactivite": "normale",  # voir REACTIVITES
    "langue_ecriture": "en",  # Ctrl+Alt+Entrée : ce qu'on écrit est traduit dans cette langue
    "mots": [],  # « Mes mots » : un mot jamais traduit, ou « mot = traduction imposée », par ligne
    "carte_graphique": True,  # traduire sur la carte graphique NVIDIA s'il y en a une (~10 fois plus vite)
    "grand_modele": True,  # grand modèle de traduction (MADLAD, 3 Go), avec la carte graphique seulement
    "ia_locale": False,  # IA locale (TranslateGemma) : le sens et les expressions (carte graphique, 2,4 Go)
}
# Raccourcis clavier, tous changeables (rubrique « Options ») : action -> (clé des réglages, par défaut, rôle).
# Une combinaison vide : aucun raccourci pour cette action.
RACCOURCIS = {
    "pause": ("raccourci", "ctrl+alt+t", "Marche / pause"),
    "ecrire": ("raccourci_ecrire", "ctrl+alt+enter", "Traduire ton message"),  # (pas Ctrl+Alt+E : AltGr+E, « € »)
    "zone": ("raccourci_zone", "ctrl+alt+z", "Traduire une zone choisie"),
    "capture": ("raccourci_capture", "print screen", "Capture d'écran"),
}
CONFIG_DEFAUT.update({cle: defaut for cle, defaut, _ in RACCOURCIS.values()})
IDS_RACCOURCIS = {"pause": 1, "ecrire": 2, "zone": 4}  # raccourcis de Windows (RegisterHotKey)
CAPTURES = ("capture",)  # touches seulement guettées : la capture est faite par Windows
# Noms des touches sur les touches dessinées (« print screen » -> « Impr. écran »).
NOMS_TOUCHES = {"ctrl": "Ctrl", "alt": "Alt", "alt gr": "AltGr", "shift": "Maj", "maj": "Maj", "windows": "Win",
                "enter": "Entrée", "print screen": "Impr. écran", "space": "Espace", "esc": "Échap", "delete": "Suppr",
                "tab": "Tab", "backspace": "Retour", "insert": "Inser", "home": "Début", "end": "Fin",
                "page up": "Page ↑", "page down": "Page ↓", "up": "↑", "down": "↓", "left": "←", "right": "→"}


def noms_des_touches(combinaison):
    """« ctrl+alt+enter » -> ["Ctrl", "Alt", "Entrée"] ; aucune combinaison -> ["Aucun"]."""
    if not combinaison:
        return ["Aucun"]
    noms = []
    for touche in combinaison.lower().split("+"):
        touche = re.sub(r"^(?:left|right) (?=\w)", "", touche.strip())  # « left windows » -> « windows »
        noms.append(NOMS_TOUCHES.get(touche, touche.upper() if len(touche) <= 3 else touche.capitalize()))
    return noms

# Fenêtres jamais traduites : le bureau et la barre des tâches.
CLASSES_IGNOREES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# mss.mss est renommé mss.MSS dans les versions récentes.
CaptureEcran = getattr(mss, "MSS", None) or mss.mss

# Pause après un coup de molette ou un déplacement de fenêtre avant de regarder l'écran (ensuite,
# on attend simplement que l'image ne bouge plus).
PAUSE_MOUVEMENT = 0.06
# Secondes entre deux analyses de l'image de l'écran (seulement ce qui a changé) : souvent juste
# après une action (clic, molette, touche), plus rarement quand on ne touche à rien.
ACTIVITE_RECENTE = 3.0  # secondes après une action pendant lesquelles on analyse souvent
TUILE = 64  # côté (pixels) des carrés de l'écran comparés d'une image à l'autre
MEMOIRE_VIDEO = 3.0  # secondes pendant lesquelles un endroit où une vidéo jouait n'est pas lu
BLOC_IMAGE = 16  # côté (pixels) des blocs examinés pour repérer les photos et vidéos
# Nombre de nouveaux textes traduits avant d'afficher un résultat partiel.
TAILLE_PAQUET = 8

ACCENT = "#2563EB"  # bleu : boutons, interrupteurs, rubrique choisie
ACCENT_SURVOL = "#1D4ED8"
ACCENT_FONCE = "#1E40AF"

# Constantes Win32
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x2
AC_SRC_ALPHA = 0x1

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
SM_CXSCREEN = 0
GA_ROOT = 2
ERROR_ALREADY_EXISTS = 183
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
WINEVENT_OUTOFCONTEXT = 0


user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_ASYNCWINDOWPOS = 0x0001, 0x0004, 0x0010, 0x4000
user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wintypes.HWND
dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
WinEventProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WinEventProc, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
user32.SetWinEventHook.restype = wintypes.HANDLE

log = logging.getLogger(APP_NAME)
WM_CHANGER_RACCOURCI = 0x8001  # (WM_APP + 1) au fil des entrées : enregistrer le nouveau raccourci
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                                             wintypes.DWORD]


kernel32 = ctypes.windll.kernel32
CF_UNICODETEXT = 13
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def _ouvrir_presse_papiers():
    for _ in range(25):  # (un autre programme peut l'avoir ouvert un instant)
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
# Formats qui ne sont pas de simples blocs de mémoire (image GDI, métafichier…) : Windows les refait lui-même à
# partir des autres (CF_DIB pour une image).
FORMATS_NON_COPIABLES = {2, 3, 9, 14, 0x0080, 0x0082, 0x0083, 0x008E}


def sauver_presse_papiers():
    """Tout le contenu du presse-papiers (texte, image, fichiers…) : [(format, octets)], pour le remettre ensuite
    tel quel (Ctrl+Alt+Entrée ne doit pas faire perdre une image qu'on avait copiée). None si illisible."""
    if not _ouvrir_presse_papiers():
        return None
    contenu = []
    try:
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in FORMATS_NON_COPIABLES:
                poignee = user32.GetClipboardData(fmt)
                taille = kernel32.GlobalSize(poignee) if poignee else 0
                pointeur = kernel32.GlobalLock(poignee) if taille else None
                if pointeur:
                    try:
                        contenu.append((fmt, ctypes.string_at(pointeur, taille)))
                    finally:
                        kernel32.GlobalUnlock(poignee)
            fmt = user32.EnumClipboardFormats(fmt)
    finally:
        user32.CloseClipboard()
    return contenu


def remettre_presse_papiers(contenu):
    """Remet un contenu sauvé par sauver_presse_papiers."""
    if contenu is None or not _ouvrir_presse_papiers():
        return False
    try:
        user32.EmptyClipboard()
        for fmt, octets in contenu:
            poignee = kernel32.GlobalAlloc(0x0002, max(1, len(octets)))  # GMEM_MOVEABLE
            pointeur = kernel32.GlobalLock(poignee)
            ctypes.memmove(pointeur, octets, len(octets))
            kernel32.GlobalUnlock(poignee)
            user32.SetClipboardData(fmt, poignee)  # (Windows garde la mémoire)
        return True
    finally:
        user32.CloseClipboard()


def lire_presse_papiers():
    """Le texte du presse-papiers de Windows (depuis n'importe quel fil), ou None."""
    if not _ouvrir_presse_papiers():
        return None
    try:
        poignee = user32.GetClipboardData(CF_UNICODETEXT)
        if not poignee:
            return None
        pointeur = kernel32.GlobalLock(poignee)
        try:
            return ctypes.wstring_at(pointeur) if pointeur else None
        finally:
            kernel32.GlobalUnlock(poignee)
    finally:
        user32.CloseClipboard()


def ecrire_presse_papiers(texte):
    """Met ce texte dans le presse-papiers de Windows (depuis n'importe quel fil)."""
    donnees = ctypes.create_unicode_buffer(texte)
    poignee = kernel32.GlobalAlloc(0x0002, ctypes.sizeof(donnees))  # GMEM_MOVEABLE
    pointeur = kernel32.GlobalLock(poignee)
    ctypes.memmove(pointeur, donnees, ctypes.sizeof(donnees))
    kernel32.GlobalUnlock(poignee)
    if not _ouvrir_presse_papiers():
        return False
    try:
        user32.EmptyClipboard()
        return bool(user32.SetClipboardData(CF_UNICODETEXT, poignee))  # (Windows garde la mémoire)
    finally:
        user32.CloseClipboard()


def touches_du_raccourci(combinaison):
    """« ctrl+alt+t » -> (modificateurs, touche) pour RegisterHotKey ; None si illisible. Une touche seule
    seulement si elle ne sert à rien d'autre (« f8 », « print screen ») : une lettre seule empêcherait d'écrire."""
    modificateurs, touche = 0, None
    for nom in (n.strip() for n in combinaison.lower().split("+")):
        if "alt gr" in nom:
            modificateurs |= 0x1 | 0x2
        elif any(m in nom for m in ("ctrl", "alt", "shift", "maj", "windows")):  # (« left ctrl »…)
            modificateurs |= (0x2 if "ctrl" in nom else 0x1 if "alt" in nom else 0x8 if "windows" in nom else 0x4)
        elif touche is None and len(nom) == 1 and nom.isascii() and nom.isalnum():
            # Une lettre ou un chiffre : son code est celui de la touche marquée ainsi, quel que soit le clavier.
            # (Passer par le code physique donnait Ctrl+Alt+W pour Ctrl+Alt+Z sur un clavier français AZERTY : la
            # zone choisie ne marchait pas.)
            touche = ord(nom.upper())
        elif touche is None:
            try:
                touche = user32.MapVirtualKeyW(keyboard.key_to_scan_codes(nom)[0], 1)  # MAPVK_VSC_TO_VK
            except (ValueError, IndexError):
                return None
        else:
            return None  # deux touches ordinaires
    if not touche:
        return None
    # Seule, seulement une touche qui ne sert à rien d'autre : F1 à F24, Impr. écran, Pause, Arrêt défil.
    # (Pas Entrée, Tab, Retour, les flèches… : Windows ne les laisserait plus à personne.)
    seule_permise = 0x70 <= touche <= 0x87 or touche in (0x2C, 0x13, 0x91)
    return (modificateurs, touche) if modificateurs or seule_permise else None


# --------------------------------------------------------------------------- config

def charger_config():
    config = dict(CONFIG_DEFAUT)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            donnees = json.load(f)
        if isinstance(donnees, dict):
            config.update(donnees)
            config.pop("traduire_navigateurs", None)  # (option retirée : sans l'extension, l'écran est lu)
            for ancien in ("raccourci_copier", "raccourci_copie", "raccourci_capture_zone"):
                config.pop(ancien, None)  # (raccourcis retirés : copier la traduction, capture d'une zone)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Configuration illisible, valeurs par défaut utilisées")
    # Une valeur du mauvais type (fichier abîmé ou modifié à la main) reprend sa valeur par défaut :
    # le programme démarre toujours.
    for cle, defaut in CONFIG_DEFAUT.items():
        valeur = config.get(cle)
        if isinstance(defaut, bool):
            correcte = isinstance(valeur, bool)
        elif isinstance(defaut, (int, float)):
            correcte = isinstance(valeur, (int, float)) and not isinstance(valeur, bool)
        else:
            correcte = isinstance(valeur, type(defaut))
        if not correcte:
            config[cle] = defaut
    if not isinstance(config.get("version", 1), int):
        config["version"] = 1
    if config["langue_cible"] not in NOM_PAR_CODE:
        config["langue_cible"] = CONFIG_DEFAUT["langue_cible"]
    if config.get("version", 1) < 2:  # nouveauté : couleurs copiées de l'écran, par défaut
        config["style"] = "auto"
        config["version"] = 2
    if config["style"] not in NOMS_STYLES:
        config["style"] = CONFIG_DEFAUT["style"]
    config.pop("langue_source", None)  # réglage d'une ancienne version
    config.pop("navigateurs", None)  # réglage d'un essai abandonné
    config["intervalle"] = CONFIG_DEFAUT["intervalle"]  # réglage interne, pas modifiable
    if config["taille"] not in TAILLES:
        config["taille"] = CONFIG_DEFAUT["taille"]
    if config["reactivite"] not in REACTIVITES:
        config["reactivite"] = CONFIG_DEFAUT["reactivite"]
    try:
        config["opacite"] = max(40, min(100, int(config["opacite"])))
    except (TypeError, ValueError, OverflowError):
        config["opacite"] = CONFIG_DEFAUT["opacite"]
    config["fondu"] = bool(config["fondu"])
    anciens = config.pop("exclus", None)  # nom de la liste dans la version précédente
    if isinstance(anciens, list) and not config["programmes"]:
        config["programmes"] = anciens
    if not isinstance(config["programmes"], list):
        config["programmes"] = []
    config["programmes"] = sorted({str(p).strip().lower() for p in config["programmes"] if str(p).strip()})
    if config["mode_programmes"] not in MODES_PROGRAMMES:
        config["mode_programmes"] = CONFIG_DEFAUT["mode_programmes"]
    if config["langue_ecriture"] not in NOM_PAR_CODE:
        config["langue_ecriture"] = CONFIG_DEFAUT["langue_ecriture"]
    config["mots"] = [str(m).strip()[:200] for m in config["mots"] if str(m).strip()][:500]
    # Langues d'origine à traduire ; absent tant qu'on ne les a pas choisies (la fenêtre du choix s'ouvre).
    if "langues" in config:
        if isinstance(config["langues"], list):
            config["langues"] = sorted({c for c in config["langues"] if isinstance(c, str) and c in NOM_PAR_CODE})
        else:
            del config["langues"]
    return config


def sauver_config(config):
    """Enregistre les réglages d'un bloc (fichier provisoire puis remplacement : un arrêt brutal ne
    laisse jamais un fichier à moitié écrit). Une erreur d'écriture est notée, sans bloquer le réglage."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        provisoire = CONFIG_PATH + ".tmp"
        with open(provisoire, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(provisoire, CONFIG_PATH)
    except OSError:
        log.exception("Impossible d'enregistrer les réglages")


def demarrage_actif():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as cle:
            winreg.QueryValueEx(cle, APP_NAME)
        return True
    except FileNotFoundError:
        return False


def definir_demarrage(actif):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as cle:
        if actif:
            if EST_EXE:
                commande = f'"{sys.executable}" {ARG_DEMARRAGE}'
            else:
                pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                commande = f'"{pythonw}" "{os.path.abspath(__file__)}" {ARG_DEMARRAGE}'
            winreg.SetValueEx(cle, APP_NAME, 0, winreg.REG_SZ, commande)
        else:
            try:
                winreg.DeleteValue(cle, APP_NAME)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- icône

def dessiner_icone(taille=256):
    """Deux bulles de dialogue « A » et « 文 » sur un fond dégradé indigo → cyan."""
    s = taille / 256
    fond = Image.new("RGBA", (taille, taille))
    dessin_fond = ImageDraw.Draw(fond)
    haut, bas = (99, 102, 241), (6, 182, 212)
    for y in range(taille):
        t = y / (taille - 1)
        couleur = tuple(int(a + (b - a) * t) for a, b in zip(haut, bas))
        dessin_fond.line([(0, y), (taille, y)], fill=couleur + (255,))
    masque = Image.new("L", (taille, taille), 0)
    ImageDraw.Draw(masque).rounded_rectangle((0, 0, taille - 1, taille - 1), radius=int(58 * s), fill=255)
    image = Image.new("RGBA", (taille, taille), (0, 0, 0, 0))
    image.paste(fond, (0, 0), masque)

    dessin = ImageDraw.Draw(image)

    def police(fichiers, pixels):
        for fichier in fichiers:
            try:
                return ImageFont.truetype(fichier, int(pixels * s))
            except OSError:
                pass
        return ImageFont.load_default()

    # Bulle blanche « A »
    dessin.rounded_rectangle((30 * s, 34 * s, 150 * s, 140 * s), radius=int(26 * s), fill="white")
    dessin.polygon([(52 * s, 132 * s), (46 * s, 168 * s), (86 * s, 136 * s)], fill="white")
    dessin.text((90 * s, 88 * s), "A", font=police(["segoeuib.ttf", "arialbd.ttf"], 84), fill=ACCENT, anchor="mm")

    # Bulle sombre « 文 »
    sombre = (30, 27, 75, 255)
    dessin.rounded_rectangle((106 * s, 112 * s, 226 * s, 218 * s), radius=int(26 * s), fill=sombre)
    dessin.polygon([(204 * s, 210 * s), (212 * s, 244 * s), (172 * s, 214 * s)], fill=sombre)
    dessin.text((166 * s, 164 * s), "文", font=police(["msyhbd.ttc", "msyh.ttc", "simsun.ttc"], 72),
                fill="white", anchor="mm")
    return image


def assurer_icone():
    if not os.path.exists(ICONE_PATH):
        try:
            tailles = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
            dessiner_icone().save(ICONE_PATH, sizes=tailles)
        except OSError:
            log.exception("Impossible d'enregistrer l'icône")


# --------------------------------------------------------------------------- lecture du texte

@dataclass(frozen=True)
class Texte:
    texte: str
    x: int
    y: int
    l: int
    h: int
    genre: str  # "texte" (cadre ajusté au texte), "bouton" (texte centré), "bloc" (cellule, élément de liste…)
    fenetre: tuple  # cadre de la fenêtre qui contient le texte : une traduction n'en sort jamais
    programme: str = ""  # exécutable de la fenêtre (« chrome.exe »…)
    version: int = 0  # numéro de la lecture dans l'image (un texte relu n'est plus le même objet)
    taille: int = 0  # taille de la police d'origine (pixels), estimée d'après les lignes lues
    encre: tuple = ()  # couleur des lettres (paliers de 16), pour une ligne lue dans l'image


def couleur_encre(motif):
    """Couleur des lettres d'une ligne (pixels BGR, int16), en paliers de 16 ; () si on ne sait pas."""
    if motif.shape[0] < 3 or motif.shape[1] < 3:
        return ()
    bord = np.concatenate([motif[0], motif[-1], motif[:, 0], motif[:, -1]])
    ecarts = np.abs(motif - np.median(bord, axis=0)).sum(axis=2)
    if ecarts.max() < 60:
        return ()
    encre = motif[ecarts >= np.percentile(ecarts, 90)].mean(axis=0)
    return tuple(int(c) // 16 for c in encre)


def encres_differentes(a, b):
    """Deux lignes de couleurs nettement différentes (titre blanc, détails gris) ?"""
    return bool(a.encre and b.encre) and sum(abs(x - y) for x, y in zip(a.encre, b.encre)) >= 4


def proportion_encre(texte):
    """Hauteur de l'encre d'une ligne (capitales, hampes, jambages) par rapport à la taille de la
    police : « Sign in to your account » descend sous la ligne, « HUSS VALLEY » non."""
    haut = any(c.isupper() or c.isdigit() or c in "bdfhiklt'\"!?" for c in texte)
    bas = any(c in "gjpqy,;()[]{}|" for c in texte)
    # Mesuré avec la reconnaissance de texte de Windows (Segoe UI, Arial, de 11 à 26 pixels).
    return {(True, True): 1.0, (True, False): 0.77, (False, True): 0.73, (False, False): 0.6}[(haut, bas)]


A_TRADUIRE = re.compile(r"[^\W\d_]{2,}")  # au moins deux lettres d'affilée
# Code, commandes, liens, chemins, noms de fichiers et de salons : on ne les traduit pas.
# Les phrases normales qui citent un bout de code (`npx skills`) restent traduites.
RESSEMBLE_A_DU_CODE = re.compile(
    r"[{}$\\]|=>|==|://|\(\)|;\s*$|\w@\w|<\w+>|\w/\w+/"
    r"|^(?:npx|npm|pnpm|yarn|pip|git|cd|sudo|py|python|curl|wget|docker|winget|choco)\s"  # commande
    r"|^\S*(?:\w[._/]\w|[a-z][A-Z])\S*$"  # un seul « mot » technique : fichier_1.zip, nom_variable, camelCase
    r"|^[A-Z][a-z]+-[A-Z]\w*$"  # Join-Path
    r"|^#?[a-z0-9]+(?:[-_][a-z0-9]+)+(?:\s*\(.*\))?$"  # nom de salon : community-support (salon textuel)
)
# Mot qui ressemble à un pseudo : chiffres collés aux lettres, majuscule au milieu.
MOT_DE_PSEUDO = re.compile(r"[^\W\d_]\d|\d[^\W\d_]|[a-z][A-Z]|_")
# … sauf un nombre suivi d'un ordinal ou d'une unité : « 31st », « 4PM », « 3D », « 60fps », « 16GB ».
NOMBRE_AVEC_UNITE = re.compile(r"\d+(?:[.,]\d+)?(?:st|nd|rd|th|am|pm|h|min|s|ms|k|m|d|p|x|fps|hz|gb|mb|kb|tb|px|"
                               r"cm|mm|km|kg|g|ml|l)", re.IGNORECASE)
# Petits mots qui restent en minuscules dans un nom propre (« Hit the Road »).
MOTS_DE_LIAISON = {"the", "of", "a", "an", "and", "or", "in", "on", "at", "to", "for", "by", "with",
                   "de", "du", "des", "la", "le", "les", "et", "&", "x", "vs", "-", "|"}
# Mots typiques d'une phrase ou d'un bouton : un texte qui en contient n'est pas un nom propre
# (« Install This Skill », « Load More », « Sign In »).
MOTS_DE_PHRASE = {
    "this", "that", "these", "those", "your", "my", "our", "their", "you", "we", "it", "is", "are",
    "all", "more", "less", "new", "now", "here", "how", "what", "why", "when", "where", "who", "about",
    "from", "not", "no", "get", "see", "view", "show", "hide", "add", "remove", "install", "update",
    "sign", "log", "learn", "read", "watch", "open", "close", "edit", "delete", "save", "share",
    "create", "submit", "report", "browse", "find", "search", "load", "back", "next", "previous",
    "go", "start", "stop", "help", "settings", "home", "join", "leave", "send", "reply", "follow",
    "download", "upload", "buy", "try", "use", "click", "select", "manage", "copy", "paste",
}
PONCTUATION_DE_PHRASE = re.compile(r"[.,!?:;]")
# Noms de logiciels et de marques qui sont aussi des mots courants : seuls, ce sont des noms.
NOMS_DE_MARQUES = {"unity", "unreal", "blender", "steam", "discord", "twitch", "booth", "opera", "edge", "chrome",
                   "windows", "office", "word", "teams", "spotify", "apple", "amazon", "prime", "origin", "uplay",
                   "epic", "epic games", "rider", "affinity", "substance", "shotgun", "notion", "slack", "zoom",
                   "patreon", "gumroad", "itch", "quest", "oculus", "index", "pico", "virtual desktop"}
DECOUPE_PHRASES = re.compile(r"(?<=[.!?…])\s+|(?<=[。！？])")
# Écritures sans espaces entre les mots (japonais, chinois) et coréen : on compte les caractères.
CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯ｦ-ﾟ]")
KANA = re.compile(r"[぀-ヿｦ-ﾟ]")  # hiragana, katakana : du japonais à coup sûr
HANGUL = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")
CHIFFRE = re.compile(r"\d")
MOT = re.compile(r"[^\W\d_]{2,}")


def duree_moyenne(durees):
    """« moy. 120 ms, max 300 ms » pour le journal (durées en secondes)."""
    if not durees:
        return "(rien)"
    return "moy. %d ms, max %d ms" % (1000 * sum(durees) / len(durees), 1000 * max(durees))


SEPARATEUR_DE_DETAILS = re.compile(r"[•·▷|>]")


def remettre_inconnus(source, traduit):
    """Les modèles écrivent « ⁇ » à la place d'un caractère qu'ils ne connaissent pas (« ` », « — »,
    émoji, symbole) : on remet, dans l'ordre, ceux du texte d'origine qui manquent à la traduction.
    None si ça ne correspond pas."""
    if "⁇" not in traduit:
        return traduit
    speciaux = [c for c in source if unicodedata.category(c)[0] in "SP" and c not in traduit]
    if traduit.count("⁇") != len(speciaux):
        return None  # on ne sait pas quoi remettre : mieux vaut laisser l'original
    for c in speciaux:
        traduit = traduit.replace("⁇", c, 1)
    return traduit


# Bout de code cité dans une phrase (« lance `npx skills update` ») : remplacé par un repère que les
# modèles recopient tel quel ([A], [B]…), pour qu'il ne soit ni traduit ni abîmé.
# Un bout de code ou un lien recollé dans sa phrase par le Lecteur (Discord : « Yeah `_CameraDepthTexture` is
# for… ») est entouré de MARQUE_CODE (caractère invisible) : protégé de même, et rendu sans la marque.
MARQUE_CODE = "⁣"
CODE_CITE = re.compile(r"`[^`\n]{1,100}`|⁣[^⁣\n]{1,300}⁣")


NOMBRE_A_PROTEGER = re.compile(r"(?<![\w\[])[\w.:,/-]*\d[\w.:,/-]*")  # « 21h », « 2.0 », « 14:30 »


def proteger_code(texte, mots=None, chiffres=False):
    """(texte avec des repères à la place du code cité et des « Mes mots », ce qui revient à chaque repère).
    Un mot jamais traduit revient tel quel ; un mot à traduction imposée revient traduit comme demandé.
    `chiffres` : les nombres aussi (ce qu'on écrit : une heure ne doit pas être convertie)."""
    if "[" in texte:  # de vrais crochets se confondraient avec les repères
        return texte.replace(MARQUE_CODE, ""), []
    codes = []

    def reperer(valeur):
        codes.append(valeur)
        return "[%s]" % chr(ord("A") + len(codes) - 1)

    texte = CODE_CITE.sub(lambda m: reperer(m.group(0)) if len(codes) < 26 else m.group(0), texte)
    if mots and mots.motif:
        texte = mots.motif.sub(lambda m: reperer(mots.imposes.get(m.group(0).casefold(), m.group(0)))
                               if len(codes) < 26 else m.group(0), texte)
    if chiffres:
        texte = NOMBRE_A_PROTEGER.sub(lambda m: reperer(m.group(0)) if len(codes) < 26 else m.group(0), texte)
    # (Une marque restée seule — plus de 26 codes, code très long — ne part pas au modèle : il en ferait « ⁇ ».)
    return texte.replace(MARQUE_CODE, ""), codes


class MesMots:
    """« Mes mots » (réglages) : des mots jamais traduits, et des traductions imposées (« mot = traduction »)."""

    def __init__(self, lignes=()):
        self.gardes, self.imposes = set(), {}
        for ligne in lignes:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            if "=" in ligne:
                mot, traduction = (x.strip() for x in ligne.split("=", 1))
                if mot and traduction:
                    self.imposes[mot.casefold()] = traduction
            else:
                self.gardes.add(ligne.casefold())
        tous = sorted(self.gardes | set(self.imposes), key=len, reverse=True)
        self.motif = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(map(re.escape, tous)), re.IGNORECASE) if tous else None

    def texte_entier(self, texte):
        """Le texte entier est-il un de « Mes mots » ? (False : non ; None : jamais traduit ; sinon sa traduction.)"""
        cle = texte.strip().casefold()
        if cle in self.imposes:
            return self.imposes[cle]
        return None if cle in self.gardes else False


def remettre_code(traduit, codes):
    """Remet le code cité à la place de ses repères ; None si un repère a été perdu ou recopié deux fois."""
    for i, code in enumerate(codes):
        repere = "[%s]" % chr(ord("A") + i)
        if traduit.count(repere) != 1:
            return None
        traduit = traduit.replace(repere, code.strip(MARQUE_CODE))
    return traduit


# Chat (Twitch, Discord…) : abréviations et mots tapés à la va-vite, remis en anglais courant avant la
# traduction, que les modèles ne connaissent pas (« idk tbh » -> « I don't know to be honest »).
ARGOT_ANGLAIS = {
    "idk": "I don't know", "idc": "I don't care", "tbh": "to be honest", "ngl": "not gonna lie",
    "imo": "in my opinion", "imho": "in my opinion", "btw": "by the way", "brb": "I'll be right back",
    "rn": "right now", "pls": "please", "plz": "please", "thx": "thanks", "ty": "thank you",
    "tysm": "thank you so much", "np": "no problem", "yw": "you're welcome", "wb": "welcome back",
    "gl": "good luck", "gn": "good night", "nvm": "never mind", "jk": "just kidding", "ikr": "I know, right",
    "irl": "in real life", "ily": "I love you", "ilysm": "I love you so much", "lmk": "let me know",
    "wyd": "what are you doing", "hbu": "how about you", "wbu": "what about you", "cya": "see you",
    "tmrw": "tomorrow", "tmr": "tomorrow", "ppl": "people", "abt": "about", "rly": "really",
    "srsly": "seriously", "ofc": "of course", "prolly": "probably", "obv": "obviously", "obvs": "obviously",
    "tho": "though", "thru": "through", "sry": "sorry", "bc": "because", "cuz": "because", "kinda": "kind of",
    "sorta": "sort of", "lil": "little", "sus": "suspicious", "u": "you", "im": "I'm", "ive": "I've",
    "dont": "don't", "cant": "can't", "didnt": "didn't", "doesnt": "doesn't", "isnt": "isn't",
    "wasnt": "wasn't", "arent": "aren't", "werent": "weren't", "havent": "haven't", "hasnt": "hasn't",
    "wouldnt": "wouldn't", "couldnt": "couldn't", "shouldnt": "shouldn't", "thats": "that's",
    "whats": "what's", "theyre": "they're", "youre": "you're", "i": "I", "subs": "subscriptions",
    "pfp": "profile picture",
}
# Un mot seul, pas collé à un pseudo (@ty, ty_42), à un repère de code ([U]) ni à un mot composé (u-turn).
_AUTOUR = r"(?<![@\w'’\[-])(?:%s)(?![\w'’\]-]|\.\w)"
ARGOT = re.compile(_AUTOUR % "|".join(sorted(map(re.escape, ARGOT_ANGLAIS), key=len, reverse=True)), re.IGNORECASE)
MOIS_ANGLAIS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
                r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
EXPRESSIONS_ANGLAIS = [  # tournures du chat, dans leur contexte
    (re.compile(_AUTOUR % r"ur(?= (?:so|a|an|the|very|too|really|gonna|going|not|such|literally|just|always|never)\b)",
                re.IGNORECASE), "you're"),
    (re.compile(_AUTOUR % "ur", re.IGNORECASE), "your"),
    (re.compile(r"^sup\b", re.IGNORECASE), "hey"),
    (re.compile(r"\bfr(?: fr)?(?=[\s.!?]*$)", re.IGNORECASE), "for real"),
    (re.compile(r"\blets go+\b", re.IGNORECASE), "let's go"),
    (re.compile(r"\b(the|a|my|your|gifted|gift) sub\b", re.IGNORECASE), r"\1 subscription"),
    (re.compile(r"\bwatch streak\b", re.IGNORECASE), "viewing streak"),  # message de Twitch
    (re.compile(r"(^|[,.!?;]\s*)gotta\b", re.IGNORECASE), r"\1I have to"),  # « brb, gotta go » : sans sujet
    # Dates : « March 31st » -> « March 31 » (sinon « Soldes fin mars 31st »).
    (re.compile(r"\b(%s \d{1,2})(?:st|nd|rd|th)\b" % MOIS_ANGLAIS, re.IGNORECASE), r"\1"),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)(?: of)? (%s)" % MOIS_ANGLAIS, re.IGNORECASE), r"\1 \2"),
]
# Suivis d'une virgule quand la phrase continue (« idk tbh that's… » -> « I don't know, to be honest, that's… »).
MARQUEURS_ANGLAIS = {"idk", "tbh", "ngl", "imo", "imho", "btw", "brb", "jk", "nvm", "ofc", "obv", "obvs"}
LETTRE_REPETEE = re.compile(r"([^\W\d_])\1+")


def developper_argot(texte):
    """Texte anglais du chat remis en anglais courant : abréviations, apostrophes oubliées, lettres
    répétées (« hiii », « soooo »). Les pseudos et les mots inconnus restent tels quels."""

    def developper(trouve):
        mot = trouve.group(0).lower()
        if mot in MARQUEURS_ANGLAIS and re.match(r"\s+[^\W\d_]", trouve.string[trouve.end():]):
            return ARGOT_ANGLAIS[mot] + ","
        return ARGOT_ANGLAIS[mot]

    def sans_repetitions(trouve):
        mot = trouve.group(0)
        frequence = frequence_mot(mot.lower(), "en")
        if frequence >= 2.5 and not re.search(r"([^\W\d_])\1\1", mot):
            return mot  # vrai mot (« good », « too ») : on n'y touche pas
        for double in (r"\1\1", r"\1"):  # « cooool » -> « cool », « soooo » -> « so »
            court = LETTRE_REPETEE.sub(double, mot)
            if court != mot and frequence_mot(court.lower(), "en") >= max(3.5, frequence + 1):
                return court
        return mot  # « Waffffie » : un pseudo, gardé tel quel

    texte = ARGOT.sub(developper, texte)
    for expression, remplacement in EXPRESSIONS_ANGLAIS:
        texte = expression.sub(remplacement, texte)
    return re.sub(r"[^\W\d_]*([^\W\d_])\1[^\W\d_]*", sans_repetitions, texte)


# Libellés d'interface courants (anglais -> français) : des termes justes et toujours les mêmes, là où les
# modèles hésitent (« Add to cart » -> « Ajouter à ma liste », « Followers » -> « Suiveurs »).
GLOSSAIRE_INTERFACE_FR = {
    "add to cart": "Ajouter au panier", "add to wishlist": "Ajouter à la liste de souhaits", "buy now": "Acheter maintenant",
    "checkout": "Paiement", "sign in": "Se connecter", "log in": "Se connecter", "login": "Connexion",
    "sign up": "S'inscrire", "register": "S'inscrire", "log out": "Se déconnecter", "sign out": "Se déconnecter",
    "logout": "Déconnexion", "description": "Description", "invite me": "M'inviter", "followers": "Abonnés", "following": "Abonnements", "follow": "Suivre",
    "unfollow": "Ne plus suivre", "subscribe": "S'abonner", "subscribed": "Abonné", "unsubscribe": "Se désabonner",
    "open": "Ouvrir", "open project": "Ouvrir le projet", "open file": "Ouvrir le fichier", "save": "Enregistrer",
    "save changes": "Enregistrer les modifications", "cancel": "Annuler", "delete": "Supprimer", "remove": "Retirer",
    "edit": "Modifier", "share": "Partager", "reply": "Répondre", "replies": "Réponses", "like": "J'aime",
    "settings": "Paramètres", "overview": "Aperçu", "reviews": "Avis", "review": "Avis",
    "write a review": "Écrire un avis", "wishlist": "Liste de souhaits", "shop now": "Acheter maintenant", "search": "Rechercher", "home": "Accueil", "back": "Retour", "next": "Suivant",
    "previous": "Précédent", "close": "Fermer", "download": "Télécharger", "upload": "Importer", "show more": "Afficher plus",
    "show less": "Afficher moins", "see more": "Voir plus", "see all": "Tout voir", "view all": "Tout afficher",
    "load more": "Charger plus", "learn more": "En savoir plus", "read more": "Lire la suite", "continue": "Continuer",
    "submit": "Envoyer", "send": "Envoyer", "copy link": "Copier le lien", "copy": "Copier", "paste": "Coller",
    "mark as read": "Marquer comme lu", "mark all as read": "Tout marquer comme lu", "notifications": "Notifications",
    "messages": "Messages", "profile": "Profil", "account": "Compte", "help": "Aide", "about": "À propos",
    "terms of use": "Conditions d'utilisation", "terms of service": "Conditions d'utilisation",
    "privacy policy": "Politique de confidentialité", "limited time": "Durée limitée", "new": "Nouveau",
    "popular": "Populaire", "trending": "Tendances", "recommended": "Recommandé", "online": "En ligne",
    "offline": "Hors ligne", "members": "Membres", "pinned": "Épinglé", "pin": "Épingler", "unpin": "Désépingler",
    "watch later": "À regarder plus tard", "add friend": "Ajouter en ami", "friends": "Amis", "invite": "Inviter",
    "leave server": "Quitter le serveur", "mute": "Couper le son", "unmute": "Réactiver le son", "report": "Signaler",
    "block": "Bloquer", "loading": "Chargement", "loading...": "Chargement…", "please wait": "Veuillez patienter",
    "free shipping": "Livraison gratuite", "out of stock": "Rupture de stock", "in stock": "En stock",
}
LONGUEUR_PHRASE_PAR_PHRASE = 160  # au-delà (un caractère chinois compte triple), une ligne se traduit phrase par phrase


def decouper_texte(texte):
    """[morceau, séparateur, morceau, …, morceau] : les lignes, puis les phrases des lignes longues.
    Un seul élément pour un texte court sur une ligne. "".join(…) redonne le texte exact."""
    parties = []
    for i, ligne in enumerate(re.split(r"(\n+)", texte)):
        if i % 2:
            parties.append(ligne)  # retour(s) à la ligne
        elif len(ligne) + 2 * len(CJK.findall(ligne)) > LONGUEUR_PHRASE_PAR_PHRASE:
            parties += re.split(r"((?<=[.!?…])\s+|(?<=[。！？]))", ligne)
        else:
            parties.append(ligne)
    return parties


def libelle_du_glossaire(texte):
    """Traduction française du glossaire pour ce libellé anglais (« Sign in: » -> « Se connecter : »), ou None."""
    deux_points = texte.rstrip().endswith(":")
    cle = texte.strip().rstrip(":").strip().casefold()
    traduit = GLOSSAIRE_INTERFACE_FR.get(cle)
    if traduit is None:
        return None
    if texte.strip().isupper() and len(cle) > 1:
        traduit = traduit.upper()
    return traduit + (" :" if deux_points else "")


def retoucher_francais(texte):
    """Petites retouches d'une traduction française : « S'il vous plaît lire » -> « Veuillez lire », et
    l'espace insécable avant « ! ? ; : » (typographie française ; pas dans « 16:00 » ni dans les liens)."""
    texte = re.sub(r"(^|[.!?…]\s+)S'il vous plaît,? (?!(?:votre|notre|autre|entre|contre|leur|lettre|titre|"
                   r"membre|livre|offre|ordre)\b)(\w+(?:er|ir|re|oir))\b", r"\1Veuillez \2", texte)
    texte = re.sub(r"(?<=[^\s\d!?;: ]) ?([!?;]+)(?=\s|$|[)\"»])", " \\1", texte)
    return re.sub(r"(?<=[^\s\d :/]) ?:(?=\s|$)", " :", texte)


def memes_chiffres(texte, traduit):
    """La traduction garde-t-elle les mêmes chiffres ? (Chiffres « pleine largeur » compris : ２０２６.)"""
    return sorted(CHIFFRE.findall(unicodedata.normalize("NFKC", traduit))) == sorted(
        CHIFFRE.findall(unicodedata.normalize("NFKC", texte)))


def traduction_aberrante(traduit):
    """Le modèle s'est emballé : le même caractère répété (« ?????? »), ou quelques mots en boucle
    (« la mastication de la mastication de la… », « * * * * »)."""
    if re.search(r"(\S)\1{9,}", traduit):
        return True
    mots = traduit.split()
    return len(mots) >= 12 and len(set(mots)) < 0.35 * len(mots)


def surtout_des_chiffres(texte):
    """Nombres, dates, prix, statistiques (« 1,2 k vues », « 9 Oct 2026 », « 128 members »,
    « navigazer • 1,8 M • il y a 1 an ») : on ne les traduit pas. Une vraie phrase qui contient un
    nombre (« 3 days ago ») le reste."""
    if not CHIFFRE.search(texte):
        return False
    mots = len(MOT.findall(texte))
    return mots <= 1 or (mots <= 4 and bool(SEPARATEUR_DE_DETAILS.search(texte)))


def a_des_caracteres_decoratifs(texte):
    """Émojis, symboles, lettres « fantaisie » (𝑁𝑖𝑠𝑝ℎ𝑦𝑎, ꜱᴍᴀʟʟ ᴄᴀᴘꜱ…) : typique d'un pseudo.

    Les flèches, puces et symboles mathématiques courants (→ • ×) ne comptent pas.
    """
    for c in texte:
        code = ord(c)
        if code < 0x2000 or 0x2190 <= code <= 0x21FF:  # texte courant, flèches
            continue
        if 0x3000 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xFF00 <= code <= 0xFFEF:
            continue  # japonais, chinois, coréen (« ー » du japonais n'a rien de décoratif)
        if unicodedata.category(c) in ("So", "Sk", "Lm") or 0x1D400 <= code <= 0x1D7FF:
            return True
        if 0x1D00 <= code <= 0x1DBF or 0xA720 <= code <= 0xA7FF:  # petites capitales, lettres modifiées
            return True
    return False


_noms_processus = {}


def nom_processus(pid):
    """Nom de l'exécutable d'un processus (« chrome.exe »…), mis en cache 30 s (Windows réutilise
    vite les numéros de processus : un nouveau programme ne doit pas hériter du nom d'un ancien)."""
    maintenant = time.monotonic()
    connu = _noms_processus.get(pid)
    if connu and maintenant - connu[1] < 30:
        return connu[0]
    nom = ""
    poignee = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if poignee:
        tampon = ctypes.create_unicode_buffer(260)
        taille = wintypes.DWORD(260)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(poignee, 0, tampon, ctypes.byref(taille)):
            nom = os.path.basename(tampon.value).lower()
        ctypes.windll.kernel32.CloseHandle(poignee)
    if nom:  # un échec (processus protégé…) n'est pas gardé : on réessaiera
        if len(_noms_processus) > 2000:
            _noms_processus.clear()
        _noms_processus[pid] = (nom, maintenant)
    return nom


def zones_des_pages(hwnd):
    """Cadres des pages web d'une fenêtre de navigateur Chromium (Chrome, Edge, Opera, Brave…) :
    chaque page est une sous-fenêtre « Chrome_RenderWidgetHostHWND », présente même quand
    l'accessibilité du navigateur est en veille."""
    cadres = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visiter(enfant, _):
        classe = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(enfant, classe, 64)
        if classe.value == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(enfant):
            r = wintypes.RECT()
            user32.GetWindowRect(enfant, ctypes.byref(r))
            if r.right - r.left > 100 and r.bottom - r.top > 100:
                cadres.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumChildWindows(hwnd, visiter, 0)
    return cadres


def programme_de_fenetre(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return nom_processus(pid.value)


def _rect_fenetre(hwnd):
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)):
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def jeu_plein_ecran():
    """Une application Direct3D (un jeu) est-elle en plein écran exclusif ? (QUNS_RUNNING_D3D_FULL_SCREEN)"""
    etat = ctypes.c_int(0)
    return ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(etat)) == 0 and etat.value == 3


def fenetre_au_point(x, y):
    """Cadre de la fenêtre visible en ce point de l'écran (comme celui noté pour un texte lu), ou None. Nos
    traductions ne comptent pas : les clics les traversent, Windows ne les voit pas ici."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    racine = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
    return tuple(_rect_fenetre(racine)) if racine else None


def fenetre_sous_souris():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    hwnd = user32.WindowFromPoint(point)
    return user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None


def fenetres_visibles():
    """Fenêtres affichées, de la plus en avant à la plus en arrière.

    Renvoie (hwnd, rectangle, à_lire). Les fenêtres de ce programme ne sont pas
    lues mais comptent pour savoir ce qui est caché derrière elles.
    """
    mon_pid = os.getpid()
    fenetres = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visiter(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
            return True  # fenêtres transparentes aux clics (dont nos vitres de traduction)
        cache = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cache), ctypes.sizeof(cache))
        if cache.value:  # fenêtre d'un autre bureau virtuel, ou appli suspendue
            return True
        classe = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, classe, 64)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = _rect_fenetre(hwnd)
        if rect[2] - rect[0] > 1 and rect[3] - rect[1] > 1:
            a_lire = pid.value != mon_pid and classe.value not in CLASSES_IGNOREES
            fenetres.append((hwnd, rect, a_lire))
        return True

    user32.EnumWindows(visiter, 0)
    return fenetres


def programmes_avec_fenetre():
    """Programmes qui ont une fenêtre ouverte (même réduite), sauf ceux de Windows et celui-ci."""
    mon_pid = os.getpid()
    noms = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visiter(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) \
                and not user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != mon_pid:
                noms.add(nom_processus(pid.value))
        return True

    user32.EnumWindows(visiter, 0)
    return sorted(noms - PROGRAMMES_SYSTEME)


def soustraire(rect, autres):
    """Parties de `rect` qui ne sont sous aucun des rectangles `autres` (x1, y1, x2, y2)."""
    morceaux = [rect]
    for a, b, c, d in autres:
        restes = []
        for x1, y1, x2, y2 in morceaux:
            if a >= x2 or c <= x1 or b >= y2 or d <= y1:
                restes.append((x1, y1, x2, y2))
                continue
            haut, bas = max(y1, b), min(y2, d)
            if y1 < b:
                restes.append((x1, y1, x2, b))
            if d < y2:
                restes.append((x1, d, x2, y2))
            if x1 < a:
                restes.append((x1, haut, a, bas))
            if c < x2:
                restes.append((c, haut, x2, bas))
        morceaux = restes
    return morceaux


def entierement_cachee(rect, devant):
    """La fenêtre est-elle entièrement recouverte par les fenêtres au premier plan ? (On vérifie
    une grille de points répartis sur toute la fenêtre.)"""
    x1, y1, x2, y2 = rect
    for i in range(9):
        for j in range(9):
            x, y = x1 + (x2 - x1 - 1) * i // 8, y1 + (y2 - y1 - 1) * j // 8
            if not any(a <= x < c and b <= y < d for a, b, c, d in devant):
                return False
    return True


def est_un_vrai_mot(mot):
    """Mot courant d'une langue (« Beginner », « Criteria ») ou pas (« Amixem », « Vercel ») ?
    D'après la fréquence des mots dans des millions de textes (échelle de 0 à 8)."""
    mot = mot.lower().strip("()[]!?.,:;'\"")
    return max(frequence_mot(mot, langue) for langue in ("en", "fr", "es", "de")) >= 3.0


def sans_emojis_autour(texte):
    """Le texte sans les émojis et symboles du début et de la fin (« 🎃 TREATS! 🎃 » -> « TREATS! »)."""
    def decor(c):
        return c.isspace() or unicodedata.category(c) in ("So", "Sk", "Mn", "Cf", "Cs") or 0x1F000 <= ord(c) <= 0x1FAFF

    debut, fin = 0, len(texte)
    while debut < fin and decor(texte[debut]):
        debut += 1
    while fin > debut and decor(texte[fin - 1]):
        fin -= 1
    return texte[debut:fin]


def ressemble_a_un_pseudo(texte, photo_a_cote):
    """Pseudo ou nom propre (à ne pas traduire) ? `photo_a_cote()` dit si une photo de
    profil est juste à côté du texte."""
    if libelle_du_glossaire(texte):
        return False  # « Logout » : un mot inconnu du dictionnaire, mais un libellé connu
    mots = texte.split()
    if a_des_caracteres_decoratifs(texte) and len(mots) <= 6:
        coeur = sans_emojis_autour(texte)
        # « 🎁 Exclusive In-Game Bonus: », « ⏰ Limited Time: » : un titre d'annonce décoré d'un émoji (il finit
        # comme une phrase), pas un pseudo.
        if not (coeur and not a_des_caracteres_decoratifs(coeur) and coeur[-1] in ":!?."):
            return True  # ❤ ☁ BOSTON BAY ☁ ❤, 𝑁𝑖𝑠𝑝ℎ𝑦𝑎
        texte, mots = coeur, coeur.split()
    if len(mots) > 4 or len(texte) > 32:
        return False
    # Japonais, chinois : pas d'espaces, toute une phrase passait pour un seul « mot » inconnu (« Quest版には対応して
    # おりません。 », « Modular Avatarに対応しています。 » n'étaient jamais traduits dans les pages).
    if len(CJK.findall(texte)) >= 4 or texte.rstrip().endswith(("。", "！", "？", "．")):
        return False
    if any(MOT_DE_PSEUDO.search(m) and not NOMBRE_AVEC_UNITE.fullmatch(m.strip("()[]!?.,:;")) for m in mots):
        return True  # Ninja2077, BeatLeader, dark_angel
    if any(m.lower().strip("()[]") in MOTS_DE_PHRASE for m in mots):
        return False  # Install This Skill, Load More
    if len(mots) == 1:
        if texte.endswith(":") and not est_un_vrai_mot(texte):
            return True  # « ketchvptime: » : le pseudo qui commence une ligne de chat
        if len(texte) >= 3 and texte.isupper():
            return not est_un_vrai_mot(texte)  # NETRA (pseudo) / AUTHOR (libellé)
        if not texte[0].isupper():
            # « thithis » : un mot en minuscules qui n'existe dans aucune langue, c'est un pseudo (le modèle en
            # faisait « C'est pourquoi je vous prie de bien vouloir me faire confiance. »).
            return len(texte) >= 3 and texte.isalpha() and not est_un_vrai_mot(texte)
        # Un mot avec majuscule qui n'est pas un mot courant (« Amixem ») : un nom.
        # « Cloudy », « Aries » sont de vrais mots : pseudo seulement à côté d'une photo de profil.
        return not est_un_vrai_mot(texte) or photo_a_cote()
    importants = [m for m in mots if m.lower() not in MOTS_DE_LIAISON and m[0].isalpha()]
    if texte.isupper():
        # QUICK STATS : libellé en majuscules ; HUSS VALLEY (un mot inconnu) : un nom.
        return not all(est_un_vrai_mot(m) for m in importants)
    if len(importants) < 2 or PONCTUATION_DE_PHRASE.search(texte) \
            or not all(m[0].isupper() for m in importants):
        return False
    # Chaque mot important commence par une majuscule. « Vercel Labs » contient un mot
    # inconnu : un nom. « Ranking Criteria », « Advanced Filters » : de vrais mots, donc un
    # libellé à traduire… sauf à côté d'une photo de profil (« Fallen Ninja », « Gentle Mates »).
    return not all(est_un_vrai_mot(m) for m in importants) or photo_a_cote()


# Zones de Discord qui ne contiennent que des noms : la barre des salons (ou des messages privés) et le
# panneau des membres et des profils (« navigation » et « complémentaire » pour l'accessibilité).
ZONES_DE_NOMS_DISCORD = {"navigation", "complémentaire", "complementary"}


def morceau_a_garder(nom):
    """Un morceau de phrase à recopier tel quel (code, lien, nom propre) plutôt qu'à traduire ? Un long
    morceau est de la prose (« … (so like 1.0/depth) so you need… »), traduit."""
    mots = nom.split()
    if not A_TRADUIRE.search(nom) or len(mots) > 4:
        return not A_TRADUIRE.search(nom)
    return bool(RESSEMBLE_A_DU_CODE.search(nom) or "://" in nom
                or any(re.search(r"\w[._/]\w|[a-z][A-Z]|^[A-Z0-9_]{3,}$|\"", m) for m in mots)
                or ressemble_a_un_pseudo(nom, lambda: False))


def recoller_morceaux(elements, ligne):
    """Discord, navigateurs : une phrase qui cite du code ou un lien arrive en morceaux (« Yeah »,
    « _CameraDepthTexture », « is for getting… » sur trois lignes). Seul, chaque morceau se traduit mal, et
    celui qui passe à la ligne englobe les autres (il serait écarté comme un bloc). On les recolle quand
    chacun commence là où le précédent finit : juste après sur la même ligne, ou sur la ligne où le précédent
    se termine. Le code et les liens sont entourés de MARQUE_CODE : protégés à la traduction.

    `elements` : [(x1, y1, x2, y2, nom)] dans l'ordre de lecture ; `ligne` : hauteur d'une ligne de texte.
    Renvoie [(x1, y1, x2, y2, texte, indices des morceaux)] : un morceau seul garde son nom."""

    def suit(a, b):
        ax1, ay1, ax2, ay2 = a[:4]
        bx1, by1, bx2, by2 = b[:4]
        if min(ay2 - ay1, by2 - by1) < 0.75 * ligne:
            return False  # texte plus petit (« (modifié) ») : autre chose
        if abs(by1 - (ay2 - ligne)) > 0.45 * ligne:
            return False  # b ne commence pas sur la dernière ligne de a
        a_seule, b_seule = ay2 - ay1 <= 1.5 * ligne, by2 - by1 <= 1.5 * ligne
        if a_seule and b_seule:
            # Côte à côte sur la même ligne : séparés d'une espace (un bout de code a en plus son cadre). Deux
            # libellés plus écartés (onglets, menu) ne sont pas une phrase.
            ecart = 1.4 * ligne if morceau_a_garder(a[4]) or morceau_a_garder(b[4]) else 0.6 * ligne
            return -8 <= bx1 - ax2 <= ecart
        if b_seule:
            return bx1 >= ax1 - 4  # sur la dernière ligne de a, après son début
        return bx2 >= ax2 - 8  # b passe à la ligne : sa première ligne va jusqu'au bout

    groupes = []
    for i, e in enumerate(elements):
        if groupes:
            dernier = elements[groupes[-1][-1]]
            if e[4] == dernier[4] and dernier[0] - 3 <= e[0] and e[2] <= dernier[2] + 3 \
                    and dernier[1] - 3 <= e[1] and e[3] <= dernier[3] + 3:
                continue  # le texte d'un lien, en double du lien
            if suit(dernier, e):
                groupes[-1].append(i)
                continue
        groupes.append([i])
    resultat = []
    for g in groupes:
        morceaux = [elements[i] for i in g]
        if len(g) == 1 or all(morceau_a_garder(m[4]) for m in morceaux):
            resultat += [(*m[:5], [i]) for m, i in zip(morceaux, g)]
            continue
        texte = ""
        for m in morceaux:
            nom = MARQUE_CODE + m[4] + MARQUE_CODE if morceau_a_garder(m[4]) else m[4]
            if texte and not re.match(r"[,.;:!?)\]}%]", m[4]) and not texte.endswith(("(", "[")):
                texte += " "
            texte += nom
        resultat.append((min(m[0] for m in morceaux), min(m[1] for m in morceaux), max(m[2] for m in morceaux),
                         max(m[3] for m in morceaux), texte, g))
    return resultat


class Lecteur:
    """Lit le texte des fenêtres via UI Automation (à utiliser depuis un seul thread)."""

    def __init__(self):
        comtypes.CoInitialize()
        self.UIA = UIA = comtypes.client.GetModule("UIAutomationCore.dll")
        self.auto = auto = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        self.parcours = auto.ControlViewWalker
        self.pseudos = {}  # texte -> est un pseudo ? (décidé une fois pour toutes)
        self.fenetres = {}  # hwnd -> (instant de lecture, (cadre, fenêtres devant), textes, cadres vus)
        self.releve = ([], {})  # (tout ce qui est vu, par fenêtre) : remplacé d'un bloc à chaque lecture
        self.lu_le = {}  # hwnd -> instant de sa dernière lecture
        self.couts = {}  # hwnd -> durée (secondes) de sa dernière lecture
        self.a_relire = set()  # fenêtres à relire en priorité (défilement, déplacement)
        self.signale_le = -1e9  # dernière lecture ratée notée dans le journal (au plus une par minute)
        # Réglages « Programmes » : (programmes jamais traduits, seuls programmes traduits ou None).
        # Remplacé d'un bloc par la fenêtre principale.
        self.filtre = (frozenset(), None)

        self.cache = auto.CreateCacheRequest()
        for propriete in (UIA.UIA_NamePropertyId, UIA.UIA_BoundingRectanglePropertyId, UIA.UIA_ControlTypePropertyId,
                          UIA.UIA_ClassNamePropertyId, UIA.UIA_LocalizedControlTypePropertyId):
            self.cache.AddProperty(propriete)
        self.cache_rect = auto.CreateCacheRequest()
        self.cache_rect.AddProperty(UIA.UIA_BoundingRectanglePropertyId)

        # Éléments dont le nom est du texte affiché.
        self.types_texte = {
            UIA.UIA_TextControlTypeId, UIA.UIA_HyperlinkControlTypeId, UIA.UIA_MenuItemControlTypeId,
            UIA.UIA_HeaderItemControlTypeId, UIA.UIA_CheckBoxControlTypeId, UIA.UIA_RadioButtonControlTypeId,
        }
        # Blocs dont le texte est souvent regroupé dans le nom (listes, boutons… dans
        # Chrome, Opera, Discord). Gardés seulement s'ils ne contiennent pas d'autre texte.
        self.types_bloc = {
            UIA.UIA_ButtonControlTypeId, UIA.UIA_ListItemControlTypeId, UIA.UIA_GroupControlTypeId,
            UIA.UIA_TreeItemControlTypeId, UIA.UIA_DataItemControlTypeId, UIA.UIA_SplitButtonControlTypeId,
        }
        self.types_contenants = {UIA.UIA_GroupControlTypeId, UIA.UIA_ListItemControlTypeId}
        # Zones dont on ne traduit rien : onglets et barres d'outils (titres de pages, favoris, noms de
        # sites), et champs de saisie (recherche, message en cours d'écriture : jamais couverts).
        self.types_zones_ignorees = {UIA.UIA_TabItemControlTypeId, UIA.UIA_ToolBarControlTypeId,
                                     UIA.UIA_EditControlTypeId}
        # Spotify : titres, artistes, albums et playlists sont des liens, des lignes de liste, de tableau ou de la
        # file d'attente, ou le grand titre d'une page (« THE FINALS » devenait « LES FINALES », « Call Me Maybe »
        # « Appelez-moi peut-être ») ; seul le reste (boutons, textes) est traduit.
        self.types_noms_spotify = {UIA.UIA_HyperlinkControlTypeId, UIA.UIA_DataItemControlTypeId,
                                   UIA.UIA_TreeItemControlTypeId, UIA.UIA_ListItemControlTypeId}

        condition_type = None
        for t in self.types_texte | self.types_bloc | self.types_zones_ignorees:
            c = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, t)
            condition_type = c if condition_type is None else auto.CreateOrCondition(condition_type, c)
        self.condition = auto.CreateAndCondition(
            condition_type,
            auto.CreateAndCondition(
                auto.CreatePropertyCondition(UIA.UIA_IsOffscreenPropertyId, False),
                auto.CreateNotCondition(auto.CreatePropertyCondition(UIA.UIA_NamePropertyId, "")),
            ),
        )
        self.condition_image = auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_ImageControlTypeId)
        # Images et vidéos (une vidéo de page web est un « groupe » dont la classe parle de vidéo).
        self.condition_medias = auto.CreateAndCondition(
            auto.CreatePropertyCondition(UIA.UIA_IsOffscreenPropertyId, False),
            auto.CreateOrCondition(
                self.condition_image,
                auto.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_GroupControlTypeId)))
        self.cache_medias = auto.CreateCacheRequest()
        for propriete in (UIA.UIA_ControlTypePropertyId, UIA.UIA_BoundingRectanglePropertyId,
                          UIA.UIA_ClassNamePropertyId, UIA.UIA_LocalizedControlTypePropertyId):
            self.cache_medias.AddProperty(propriete)

    RELECTURE_ARRIERE_PLAN = 1.5  # secondes entre deux lectures d'une fenêtre qui n'est pas active

    def invalider(self, hwnd):
        """À appeler quand une fenêtre bouge ou défile : elle sera relue tout de suite."""
        self.a_relire.add(hwnd)

    def vus_hors(self, hwnds):
        """Ce que l'accessibilité voyait, sauf dans ces fenêtres (positions ou contenu périmés).
        Utilisable depuis un autre fil : le relevé est remplacé d'un bloc à chaque lecture."""
        vus, vus_fenetres = self.releve
        perimes = {v for h in hwnds for v in vus_fenetres.get(h, ())}
        return [v for v in vus if v not in perimes]

    def perimees(self, derniere_interaction):
        """Fenêtres dont le relevé est périmé : elles ont défilé, ou on a cliqué (ou tapé) dans la
        fenêtre active depuis sa dernière lecture (la page a pu changer)."""
        try:
            hwnds = set(self.a_relire)
        except RuntimeError:  # modifié pendant la copie
            hwnds = set()
        active = user32.GetForegroundWindow()
        if self.lu_le.get(active, 0.0) < derniere_interaction:
            hwnds.add(active)
        return hwnds

    @property
    def vus(self):
        return self.releve[0]

    def lire(self, relire_active=True):
        """Relit ce que l'accessibilité voit à l'écran (le relevé). La fenêtre active est relue à
        chaque fois (réactivité quand on change de page) ; les autres, plus rarement, sauf si elles
        bougent."""
        # Tout ce que l'accessibilité voit, avec sa décision : (x1, y1, x2, y2, texte, décision),
        # décision = "traduire", "pseudo", "autre" (code, nombres…), "ignorer" (onglets, barres…)
        # ou "image" (image, vidéo : rien n'y est traduit, même le texte posé dessus).
        # Construit à part, puis remplacé d'un bloc : un autre fil peut le lire à tout moment.
        tout_vu = []
        vus_fenetres = {}  # hwnd -> ce que l'accessibilité voit dans cette fenêtre
        devant = []  # rectangles des fenêtres déjà vues (donc au premier plan)
        active = user32.GetForegroundWindow()
        maintenant = time.monotonic()
        vues = set()
        for hwnd, rect, a_lire in fenetres_visibles():
            if a_lire and not self._a_traduire(programme_de_fenetre(hwnd)):
                a_lire = False  # programme que l'utilisateur ne veut pas traduire
            if a_lire and hwnd != active and entierement_cachee(rect, devant):
                a_lire = False  # cachée derrière d'autres fenêtres : rien à lire (Steam derrière le navigateur…)
            if a_lire:
                vues.add(hwnd)
                memoire = self.fenetres.get(hwnd)
                # Seules les fenêtres posées sur celle-ci comptent (une bulle d'aide ailleurs ne la fait
                # pas relire).
                dessus = tuple(d for d in devant if d[0] < rect[2] and rect[0] < d[2] and d[1] < rect[3] and rect[1] < d[3])
                # Une fenêtre d'arrière-plan longue à lire (Steam : jusqu'à 2 s) est relue moins souvent.
                delai = max(self.RELECTURE_ARRIERE_PLAN, 10 * self.couts.get(hwnd, 0.0))
                if ((hwnd == active and relire_active) or hwnd in self.a_relire or memoire is None
                        or memoire[1] != (rect, dessus)
                        or (hwnd != active and maintenant - memoire[0] > delai)
                        or (hwnd == active and maintenant - memoire[0] > self.RELECTURE_ARRIERE_PLAN)):
                    self.a_relire.discard(hwnd)
                    avant = time.monotonic()
                    try:
                        memoire = self.fenetres[hwnd] = (maintenant, (rect, dessus),
                                                         self._lire_fenetre(hwnd, rect, devant))
                    except Exception as e:
                        # Lecture ratée (fenêtre qui se ferme, programme qui ne répond pas) : on garde la
                        # précédente plutôt que de faire comme si la fenêtre était vide.
                        if maintenant - self.signale_le > 60:
                            self.signale_le = maintenant
                            log.warning("Fenêtre illisible par l'accessibilité (%s) : %s", programme_de_fenetre(hwnd), e)
                        if memoire is None:
                            memoire = self.fenetres[hwnd] = (maintenant, (rect, dessus), [])
                    self.couts[hwnd] = time.monotonic() - avant
                tout_vu += memoire[2]
                vus_fenetres[hwnd] = memoire[2]
            elif self._a_ignorer(hwnd):
                # Barre des tâches, fenêtres de ce programme, programmes qu'on ne veut pas traduire :
                # l'analyse d'image n'y traduit rien. Seulement la partie visible : une fenêtre posée
                # par-dessus (les réglages ouverts sur le navigateur…) reste traduite.
                for partie in soustraire(rect, devant):
                    tout_vu.append((*partie, "", "ignorer"))
            devant.append(rect)
        for hwnd in list(self.fenetres):
            if hwnd not in vues:
                del self.fenetres[hwnd]  # fenêtre fermée ou réduite
        self.releve = (tout_vu, vus_fenetres)
        self.lu_le = {hwnd: memoire[0] for hwnd, memoire in self.fenetres.items()}

    def _a_traduire(self, programme):
        """Ce programme est-il à traduire, d'après les réglages « Programmes » ?"""
        exclus, seulement = self.filtre
        return programme not in exclus and (seulement is None or programme in seulement)

    def _a_ignorer(self, hwnd):
        """Fenêtre jamais traduite mais à ne pas confondre avec le fond (pas le bureau, qui est
        derrière tout : l'ignorer bloquerait tout l'écran… sauf la partie visible qu'on demande à
        ne pas traduire)."""
        if not self._a_traduire(programme_de_fenetre(hwnd)):
            return True  # programme que l'utilisateur ne veut pas traduire
        classe = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, classe, 64)
        if classe.value in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def _zone_ignoree(self, programme, type_, type_localise):
        """Un élément dont on ne traduit rien (ni ce qu'il contient) : onglets, barres d'outils, champs de saisie ;
        zones de noms de Discord ; titres, artistes et playlists de Spotify."""
        localise = (type_localise or "").lower()
        if programme == "discord.exe" and type_ == self.UIA.UIA_GroupControlTypeId and localise in ZONES_DE_NOMS_DISCORD:
            return True
        if programme == "spotify.exe" and (type_ in self.types_noms_spotify or (
                type_ == self.UIA.UIA_TextControlTypeId and localise in ("titre", "heading"))):
            return True
        return type_ in self.types_zones_ignorees

    def _lire_fenetre(self, hwnd, rect_fenetre, devant):
        vus = []
        programme = programme_de_fenetre(hwnd)
        if programme in NAVIGATEURS:
            # Onglets, favoris, barre d'adresse : rien à traduire (même si l'accessibilité dort).
            for a, b, c, d in self._hors_de_la_page(hwnd, rect_fenetre):
                if c > a and d > b and not any(a < c2 and a2 < c and b < d2 and b2 < d for a2, b2, c2, d2 in devant):
                    vus.append((a, b, c, d, "", "ignorer"))
        fx1, fy1, fx2, fy2 = rect_fenetre
        trouves = self.auto.ElementFromHandle(hwnd).FindAllBuildCache(
            self.UIA.TreeScope_Subtree, self.condition, self.cache)
        candidats = []  # (x1, y1, x2, y2, nom, type, élément)
        zones_ignorees = []
        champs = []  # champs de saisie dont le texte d'aide peut être traduit
        # Pages web et applis faites comme elles (Discord) : une phrase qui cite du code ou un lien arrive en
        # morceaux, recollés ensuite (voir recoller_morceaux).
        classe_fenetre = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, classe_fenetre, 64)
        recoller = classe_fenetre.value in ("Chrome_WidgetWin_1", "MozillaWindowClass")
        types_morceaux = (self.UIA.UIA_TextControlTypeId, self.UIA.UIA_HyperlinkControlTypeId)
        sequence = []  # morceaux de texte, dans l'ordre de lecture
        for i in range(trouves.Length):
            el = trouves.GetElement(i)
            r = el.CachedBoundingRectangle
            type_ = el.CachedControlType
            # Garder uniquement la partie visible dans la fenêtre.
            x1, y1, x2, y2 = max(r.left, fx1), max(r.top, fy1), min(r.right, fx2), min(r.bottom, fy2)
            # Caché, même en partie, par une fenêtre au premier plan : on n'y touche pas, et ce
            # texte ne compte pas comme « vu » (sinon il bloquerait l'analyse d'image à cet endroit).
            if any(x1 < c and a < x2 and y1 < d and b < y2 for a, b, c, d in devant):
                continue
            if self._zone_ignoree(programme, type_, el.CachedLocalizedControlType):
                zones_ignorees.append((x1, y1, x2, y2))
                nom = " ".join((el.CachedName or "").split())
                if type_ == self.UIA.UIA_EditControlTypeId and A_TRADUIRE.search(nom) and len(nom) <= 80:
                    # Champ de saisie : son texte d'aide (« Search Projects… ») est traduit tant qu'il est affiché
                    # seul ; ce qu'on y écrit, jamais (voir _fusionner).
                    champs.append((x1, y1, x2, y2))
                    vus.append((x1, y1, x2, y2, nom, "champ"))
                else:
                    vus.append((x1, y1, x2, y2, "", "ignorer"))
                continue
            nom = " ".join((el.CachedName or "").split())
            # Fichiers et dossiers (Explorateur, fenêtres « Ouvrir » et « Enregistrer » de tous les programmes,
            # chemin du dossier, arborescence de gauche) : ce sont des noms, jamais traduits (« requirements »).
            classe = el.CachedClassName or ""
            if classe == "UIItem" or classe.startswith("FileExplorerExtensions.") or (
                    programme == "explorer.exe" and type_ == self.UIA.UIA_TreeItemControlTypeId):
                vus.append((x1, y1, x2, y2, nom, "ignorer"))
                continue
            l, h = x2 - x1, y2 - y1
            if l < 12 or h < 8 or h > 400:
                continue
            if recoller and type_ in types_morceaux \
                    and (el.CachedLocalizedControlType or "").lower() not in ("titre", "heading"):
                sequence.append((x1, y1, x2, y2, nom, type_, el))
                continue
            if type_ in self.types_bloc:
                if l < 1.6 * h:
                    continue  # bouton-icône : son nom n'est pas affiché
                if type_ in self.types_contenants and l * h > 4 * self._surface_texte(nom, h):
                    continue  # zone bien plus grande que son texte : c'est une étiquette de région
            if not A_TRADUIRE.search(nom) or RESSEMBLE_A_DU_CODE.search(nom):
                vus.append((x1, y1, x2, y2, nom, "autre"))
                continue
            candidats.append((x1, y1, x2, y2, nom, type_, el))
        if sequence:
            hauteurs = sorted(m[3] - m[1] for m in sequence if m[3] - m[1] <= 32)
            ligne = hauteurs[len(hauteurs) // 2] if hauteurs else 20
            for x1, y1, x2, y2, nom, indices in recoller_morceaux([m[:5] for m in sequence], ligne):
                premier = sequence[indices[0]]
                if len(indices) > 1:  # une phrase recollée : jugée sur sa prose, sans le code qu'elle cite
                    prose = re.sub(MARQUE_CODE + ".*?" + MARQUE_CODE, " ", nom)
                    if A_TRADUIRE.search(prose) and not RESSEMBLE_A_DU_CODE.search(prose):
                        candidats.append((x1, y1, x2, y2, nom, "phrase", premier[6]))
                    else:
                        vus.append((x1, y1, x2, y2, nom.replace(MARQUE_CODE, ""), "autre"))
                elif not A_TRADUIRE.search(nom) or RESSEMBLE_A_DU_CODE.search(nom):
                    vus.append((x1, y1, x2, y2, nom, "autre"))
                else:
                    candidats.append((x1, y1, x2, y2, nom, premier[5], premier[6]))

        def dans_une_zone_ignoree(x1, y1, x2, y2):
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            return any(a <= cx < c and b <= cy < d for a, b, c, d in zones_ignorees)

        gardes_par_nom = {}
        # Du plus petit au plus grand : un lien et le texte qu'il contient portent le même
        # nom avec des cadres presque identiques ; on ne garde que le plus précis.
        for x1, y1, x2, y2, nom, genre, el in sorted(self._feuilles(candidats),
                                                 key=lambda c: (c[2] - c[0]) * (c[3] - c[1])):
            if dans_une_zone_ignoree(x1, y1, x2, y2):
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                if not any(a <= cx < c and b <= cy < d for a, b, c, d in champs):
                    vus.append((x1, y1, x2, y2, nom, "ignorer"))  # (dans un champ : le champ décide, voir plus haut)
                continue
            if any(self._se_chevauchent((x1, y1, x2, y2), r) for r in gardes_par_nom.get(nom, ())):
                continue
            if genre != "phrase" and self._est_pseudo(nom, el, (x1, y1, x2, y2)):  # (phrase : son code n'en fait pas un pseudo)
                vus.append((x1, y1, x2, y2, nom, "pseudo"))
                continue
            gardes_par_nom.setdefault(nom, []).append((x1, y1, x2, y2))
            vus.append((x1, y1, x2, y2, nom, "traduire"))
        return vus + self._medias(hwnd, rect_fenetre, devant)

    def _medias(self, hwnd, rect_fenetre, devant):
        """Images et vidéos de la fenêtre (rien n'y sera traduit), d'après l'accessibilité."""
        fx1, fy1, fx2, fy2 = rect_fenetre
        aire_fenetre = max(1, (fx2 - fx1) * (fy2 - fy1))
        trouves = self.auto.ElementFromHandle(hwnd).FindAllBuildCache(
            self.UIA.TreeScope_Subtree, self.condition_medias, self.cache_medias)
        medias = []
        for i in range(trouves.Length):
            el = trouves.GetElement(i)
            r = el.CachedBoundingRectangle
            x1, y1, x2, y2 = max(r.left, fx1), max(r.top, fy1), min(r.right, fx2), min(r.bottom, fy2)
            if x2 - x1 < 48 or y2 - y1 < 32 or any(x1 < c and a < x2 and y1 < d and b < y2 for a, b, c, d in devant):
                continue
            video = "vid" in f"{el.CachedClassName or ''} {el.CachedLocalizedControlType or ''}".lower()
            image = el.CachedControlType == self.UIA.UIA_ImageControlTypeId
            # Une « image » qui couvre presque toute la fenêtre est un décor de fond (halo d'une
            # vidéo, fond d'écran d'une page) : on ne s'en sert pas. Une vidéo plein écran, si.
            if video or (image and (x2 - x1) * (y2 - y1) < 0.7 * aire_fenetre):
                medias.append((x1, y1, x2, y2, "", "image"))
        return medias

    @staticmethod
    def _se_chevauchent(a, b):
        """Vrai si les deux cadres se recouvrent sur plus de la moitié du plus petit."""
        largeur = min(a[2], b[2]) - max(a[0], b[0])
        hauteur = min(a[3], b[3]) - max(a[1], b[1])
        if largeur <= 0 or hauteur <= 0:
            return False
        plus_petit = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return largeur * hauteur > 0.5 * plus_petit

    def _hors_de_la_page(self, hwnd, rect_fenetre):
        """Dans un navigateur, tout ce qui n'est pas la page web (onglets, favoris, barre
        d'adresse) : l'analyse d'image n'y traduit rien."""
        fx1, fy1, fx2, fy2 = rect_fenetre
        cadres = zones_des_pages(hwnd)
        if not cadres:
            # Firefox (ou page introuvable) : la barre d'onglets et d'adresse fait environ 85 px à 100 %.
            haut = fy1 + int(85 * user32.GetDpiForWindow(hwnd) / 96)
            cadres = [(fx1, haut, fx2, fy2)]
        px1, py1 = min(c[0] for c in cadres), min(c[1] for c in cadres)
        px2, py2 = max(c[2] for c in cadres), max(c[3] for c in cadres)
        return [(fx1, fy1, fx2, py1), (fx1, py2, fx2, fy2), (fx1, py1, px1, py2), (px2, py1, fx2, py2)]

    @staticmethod
    def _surface_texte(nom, h):
        taille = min(h, 22) * 0.75
        return max(1, len(nom)) * taille * 0.55 * taille * 1.35

    @staticmethod
    def _feuilles(candidats):
        """Écarte les blocs qui contiennent d'autres textes (on garde le texte le plus précis)."""
        par_haut = sorted(candidats, key=lambda c: c[1])
        hauts = [c[1] for c in par_haut]
        gardes = []
        for c in candidats:
            if c[5] == "phrase":
                gardes.append(c)  # phrase recollée : son cadre contient forcément ses propres morceaux
                continue
            x1, y1, x2, y2 = c[:4]
            debut = bisect.bisect_left(hauts, y1 - 3)
            fin = bisect.bisect_right(hauts, y2 + 3)
            # (3 pixels de tolérance : le texte d'un lien dépasse parfois d'un pixel.)
            contient = any(
                autre is not c and x1 - 3 <= autre[0] and autre[2] <= x2 + 3 and autre[3] <= y2 + 3
                and (autre[2] - autre[0]) * (autre[3] - autre[1]) < (x2 - x1) * (y2 - y1)
                for autre in par_haut[debut:fin]
            )
            if not contient:
                gardes.append(c)
        return gardes

    # ---- pseudos et noms propres

    def _est_pseudo(self, texte, element, rect):
        if texte in self.pseudos:
            return self.pseudos[texte]
        try:
            pseudo = self._ressemble_a_un_pseudo(texte, element, rect)
        except Exception as e:
            # Erreur passagère (fenêtre fermée pendant la lecture…) : décision sans la photo, pas gardée.
            log.debug("Pseudo « %s » : %s", texte, e)
            return ressemble_a_un_pseudo(texte, lambda: False)
        if element is None:
            return pseudo  # décidé sans regarder s'il y a une photo à côté : pas gardé pour la suite
        if len(self.pseudos) > 20000:
            self.pseudos.clear()
        self.pseudos[texte] = pseudo
        return pseudo

    def _ressemble_a_un_pseudo(self, texte, element, rect):
        return ressemble_a_un_pseudo(texte, lambda: self._photo_a_cote(element, rect))

    def _photo_a_cote(self, element, rect):
        """Cherche une image carrée (avatar) dans le petit bloc qui entoure le texte."""
        if element is None:
            return False  # texte lu dans l'image de l'écran : pas d'élément à explorer
        bloc = element
        for _ in range(3):
            parent = self.parcours.GetParentElement(bloc)
            if not parent:
                break
            r = parent.CurrentBoundingRectangle
            if r.bottom - r.top > 160:
                break  # on est sorti de l'élément (message, membre…) : trop large
            bloc = parent
        if bloc is element:
            return False
        images = bloc.FindAllBuildCache(self.UIA.TreeScope_Subtree, self.condition_image, self.cache_rect)
        for i in range(images.Length):
            r = images.GetElement(i).CachedBoundingRectangle
            l, h = r.right - r.left, r.bottom - r.top
            # Une photo de profil : carrée, assez grande (pas une icône), à gauche du nom.
            if l >= 28 and abs(l - h) <= 0.25 * l and r.right <= rect[0] + 4:
                return True
        return False


def cadre_de(morceaux):
    return (min(m.x for m in morceaux), min(m.y for m in morceaux),
            max(m.x + m.l for m in morceaux), max(m.y + m.h for m in morceaux))


def assembler_morceaux(morceaux):
    """Texte de plusieurs morceaux, dans l'ordre de lecture (lignes de haut en bas)."""
    hauteur = max(1, min(m.h for m in morceaux))
    ordre = sorted(morceaux, key=lambda m: (round((m.y + m.h / 2) / hauteur), m.x))
    texte = ""
    for m in ordre:
        if texte.endswith("-") and len(texte) > 1 and texte[-2].isalpha():
            texte = texte[:-1] + m.texte  # mot coupé en fin de ligne
        else:
            texte = f"{texte} {m.texte}" if texte else m.texte
    return texte


def texte_depuis_morceaux(nom, morceaux):
    """Un texte à traduire, posé exactement sur les morceaux vus à l'écran (avec une petite
    marge pour couvrir accents et jambages)."""
    x1, y1, x2, y2 = cadre_de(morceaux)
    marge = max(1, min(m.h for m in morceaux) // 5)
    tailles = sorted(m.h / proportion_encre(m.texte) for m in morceaux)
    return Texte(nom, x1 - 1, y1 - marge, x2 - x1 + 2, y2 - y1 + 2 * marge, "texte",
                 morceaux[0].fenetre, morceaux[0].programme, max(m.version for m in morceaux),
                 round(tailles[len(tailles) // 2]))


def regrouper_paragraphes(morceaux, element=lambda m: None):
    """Regroupe les lignes d'un même paragraphe (même marge gauche, même taille de texte,
    lignes rapprochées) : une phrase sur plusieurs lignes se traduit bien mieux en entier.

    `element(m)` : l'élément de l'accessibilité qui contient la ligne (ou None). Deux lignes du
    même élément vont ensemble ; deux lignes d'éléments différents restent séparées (deux
    libellés l'un sous l'autre). Sans l'accessibilité, on regarde si la ligne du dessus a été
    coupée par un retour à la ligne automatique (ligne longue, suite en minuscule)."""
    taille = lambda m: m.h / proportion_encre(m.texte)  # noqa: E731  taille de police estimée
    paragraphes = []  # [morceaux, début de la dernière ligne, dernier morceau]
    for m in sorted(morceaux, key=lambda m: ((m.y + m.h // 2) // 6, m.x)):
        for p in paragraphes:
            morceaux_p, d, fin = p
            # Suite de la même ligne, séparée par un grand espace : seulement si c'est le même élément.
            if abs((m.y + m.h / 2) - (fin.y + fin.h / 2)) <= 0.5 * max(m.h, fin.h) and m.x >= fin.x + fin.l - 2:
                if element(m) is not None and element(m) == element(fin) and m.x - (fin.x + fin.l) <= 3 * taille(fin):
                    morceaux_p.append(m)
                    p[2] = m
                    break
                continue
            largeur = fin.x + fin.l - d.x  # largeur de la dernière ligne
            if not (abs(m.x - d.x) <= max(6, taille(d) * 0.6) and 0.85 <= taille(m) / max(1, taille(d)) <= 1.18
                    and -2 <= m.y - (d.y + d.h) <= taille(d) * 0.9 and m.x < d.x + largeur and d.x < m.x + m.l
                    and m.fenetre == d.fenetre and not fin.texte.endswith((":", "•"))):
                continue
            suite = m.texte[:1].islower() or fin.texte.endswith((",", "-"))
            # Couleurs différentes (titre blanc, détails gris) : deux blocs différents… sauf si la ligne
            # du dessus est une longue ligne coupée au milieu d'une phrase qui continue (« … can » /
            # « be delivered… », avec des liens d'une autre couleur). Un titre court suivi d'un nom de
            # chaîne en minuscules (« navigazer ») reste séparé.
            if encres_differentes(m, d) and not (suite and largeur >= 12 * taille(d)):
                continue
            a, b = element(d), element(m)
            if a is not None and b is not None and a != b:
                continue
            # Même élément de l'accessibilité : seulement si le texte a vraiment la même taille (une
            # vignette YouTube est un seul lien avec titre, chaîne, vues et date, qui restent séparés).
            meme_element = a is not None and a == b and 0.93 <= taille(m) / max(1, taille(d)) <= 1.07
            if meme_element or largeur >= 18 * taille(d) or suite:
                morceaux_p.append(m)
                p[1] = p[2] = m
                break
        else:
            paragraphes.append([[m], m, m])
    return [p[0] for p in paragraphes]


def lettres_differentes(a, b):
    """Nombre de lettres à changer, ajouter ou enlever pour passer de a à b (« Leam » -> « Learn » : 2)."""
    precedente = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        courante = [i]
        for j, cb in enumerate(b, 1):
            courante.append(min(precedente[j] + 1, courante[j - 1] + 1, precedente[j - 1] + (ca != cb)))
        precedente = courante
    return precedente[-1]


def texte_exact(lu, noms):
    """Le texte exact d'après l'accessibilité quand elle voit la même chose que l'image (sans les
    erreurs de lecture), sinon le texte lu dans l'image. `noms` : noms des éléments de
    l'accessibilité qui contiennent le texte, dans l'ordre de lecture."""
    if not noms:
        return lu
    joint = " ".join(noms)
    ressemblance = SequenceMatcher(None, joint.replace(MARQUE_CODE, "").casefold(), lu.casefold()).ratio()
    # (Un seul élément sous le texte, presque le même : une lettre mal lue, « Leam », « Open Pr*ct » -> son nom exact.)
    if ressemblance >= 0.92 or (len(noms) == 1 and abs(len(joint) - len(lu)) <= 3 and (
            ressemblance >= 0.75 or (len(joint) >= 4 and lettres_differentes(joint.casefold(), lu.casefold()) <= 2))):
        return joint  # la même chose, sans les erreurs de lecture
    joint = joint.replace(MARQUE_CODE, "")  # (un passage pourrait couper une paire de marques)
    # L'élément contient plus que ce qu'on voit (description entière, « … by navigazer » d'un lien
    # YouTube) : seulement le passage qui correspond à ce qu'on voit.
    blocs = [b for b in SequenceMatcher(None, joint, lu, autojunk=False).get_matching_blocks() if b.size >= 3]
    if not blocs:
        return lu
    debut, fin = blocs[0].a, blocs[-1].a + blocs[-1].size
    while debut > 0 and not joint[debut - 1].isspace():
        debut -= 1
    while fin < len(joint) and not joint[fin].isspace():
        fin += 1
    candidat = joint[debut:fin]
    return candidat if SequenceMatcher(None, candidat.casefold(), lu.casefold()).ratio() >= 0.8 else lu


class LecteurImage:
    """Lit le texte dans l'image de l'écran avec la reconnaissance de texte (OCR) de Windows.

    L'image lui est donnée agrandie (AGRANDISSEMENT) : en taille réelle, Windows lit mal le texte clair d'un
    bouton coloré (« Open Project » -> « n Pro• ») et coupe des mots (« Lea rn ») ; à 1,5, il les lit justes,
    pour moitié plus de temps.

    Source principale pour le contenu des pages web (l'accessibilité des navigateurs n'en
    montre qu'une partie), et complément ailleurs : texte dans des images, des vidéos…
    À utiliser depuis un seul thread."""

    def __init__(self):
        langues = [langue.language_tag for langue in OcrEngine.available_recognizer_languages]
        # L'anglais d'abord : c'est la langue qu'on a le plus souvent à traduire.
        tag = next((t for t in langues if t.startswith("en")), langues[0] if langues else None)
        moteur = OcrEngine.try_create_from_language(LangueWindows(tag)) if tag else None
        self.moteur = moteur or OcrEngine.try_create_from_user_profile_languages()
        if self.moteur is None:  # (le message « Lecture de l'écran impossible » s'affiche alors)
            raise RuntimeError("Aucune langue de reconnaissance de texte installée dans Windows")
        # Plus grande image que Windows sait lire (2600 px sur d'anciennes versions : un écran 4K, deux écrans).
        self.limite = int(getattr(OcrEngine, "max_image_dimension", 2600) or 2600)
        self.boucle = asyncio.new_event_loop()
        self.aides = None  # autres lecteurs, pour lire une grande zone à plusieurs (créés au premier besoin)
        self.parallele = None

    def lire(self, pixels, origine, zone=None):
        """Morceaux de texte de l'image (ou seulement de la zone (x1, y1, x2, y2) de l'image),
        en coordonnées de l'écran : une ligne, ou une partie de ligne (élément de menu, bouton…)."""
        if zone:
            x1, y1, x2, y2 = zone
            pixels = pixels[y1:y2, x1:x2]
            origine = (origine[0] + x1, origine[1] + y1)
        if pixels.shape[0] < 8 or pixels.shape[1] < 8:
            return []
        hauteur, largeur = pixels.shape[:2]
        if largeur * self.AGRANDISSEMENT > self.limite or hauteur * self.AGRANDISSEMENT > self.limite:
            # Trop grande pour Windows : lue en morceaux qui se chevauchent (une ligne coupée par un bord est
            # entière dans le morceau voisin), sans garder deux fois le même texte.
            pas, recouvrement = int(self.limite / self.AGRANDISSEMENT) - 400, 400
            textes = []
            for y in range(0, hauteur, pas):
                for x in range(0, largeur, pas):
                    for t in self.lire(pixels, origine, (x, y, min(largeur, x + pas + recouvrement),
                                                         min(hauteur, y + pas + recouvrement))):
                        if not any(surface_commune((t.x, t.y, t.x + t.l, t.y + t.h), (u.x, u.y, u.x + u.l, u.y + u.h))
                                   >= 0.5 * min(t.l * t.h, u.l * u.h) for u in textes):
                            textes.append(t)
            return textes
        if hauteur >= 600 and self.aides is not False:
            return self._lire_a_plusieurs(pixels, origine)
        # BGRA de Windows lu tel quel (sans copie ni conversion), agrandi, puis donné directement à l'OCR.
        image = Image.frombuffer("RGB", (largeur, hauteur), np.ascontiguousarray(pixels), "raw", "BGRX", 0, 1)
        image = image.resize((round(largeur * self.AGRANDISSEMENT), round(hauteur * self.AGRANDISSEMENT)), Image.BILINEAR)
        lignes = self.boucle.run_until_complete(self._lire(image, self.AGRANDISSEMENT))
        ox, oy = origine
        morceaux = []
        for mots in lignes:
            morceaux += self._decouper(mots)
        textes = []
        for mots in morceaux:
            x1 = min(m[1] for m in mots) + ox
            y1 = min(m[2] for m in mots) + oy
            x2 = max(m[3] for m in mots) + ox
            y2 = max(m[4] for m in mots) + oy
            nom = self._assembler(mots)
            if x2 - x1 < 6 or y2 - y1 < 5 or not A_TRADUIRE.search(nom):
                continue
            hwnd = user32.WindowFromPoint(wintypes.POINT((x1 + x2) // 2, (y1 + y2) // 2))
            racine = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None
            fenetre = _rect_fenetre(racine) if racine else (x1, y1, x2, y2)
            textes.append(Texte(nom, x1, y1, x2 - x1, y2 - y1, "morceau", tuple(fenetre),
                                programme_de_fenetre(racine) if racine else ""))
        return textes

    AGRANDISSEMENT = 1.5
    BANDES = 4

    def _lire_a_plusieurs(self, pixels, origine):
        """Grande zone (écran entier, nouvelle page) : lue en 4 bandes en même temps, par 4 lecteurs (la lecture
        de Windows se fait hors de Python) : ~2,5 fois plus vite. Les bandes se chevauchent de 80 px ; une ligne
        coupée par le bord d'une bande est prise dans la voisine, où elle est entière."""
        if self.aides is None:
            self.aides = [LecteurImage() for _ in range(self.BANDES - 1)]
            for aide in self.aides:
                aide.aides = False  # (eux lisent leur bande d'un coup)
            self.parallele = ThreadPoolExecutor(max_workers=self.BANDES - 1)
        hauteur, largeur = pixels.shape[:2]
        pas, recouvrement = -(-hauteur // self.BANDES), 80
        bandes = [(y, min(hauteur, y + pas + recouvrement)) for y in range(0, hauteur, pas)]
        lecteurs = [self] + self.aides
        aides = [self.parallele.submit(lecteur._lire_bande, pixels, origine, y1, y2)
                 for lecteur, (y1, y2) in zip(lecteurs[1:], bandes[1:])]
        resultats = [self._lire_bande(pixels, origine, *bandes[0])] + [a.result() for a in aides]
        textes = []
        for (y1, y2), lus in zip(bandes, resultats):
            for t in lus:
                haut, bas = t.y - origine[1], t.y - origine[1] + t.h
                if (y1 > 0 and haut <= y1 + 1) or (y2 < hauteur and bas >= y2 - 1):
                    continue  # coupé par le bord de la bande : entier dans la voisine
                if not any(surface_commune((t.x, t.y, t.x + t.l, t.y + t.h), (u.x, u.y, u.x + u.l, u.y + u.h))
                           >= 0.5 * min(t.l * t.h, u.l * u.h) for u in textes):
                    textes.append(t)
        return textes

    def _lire_bande(self, pixels, origine, y1, y2):
        aides, self.aides = self.aides, False  # (lue d'un coup, sans redécouper)
        try:
            return self.lire(pixels, origine, (0, y1, pixels.shape[1], y2))
        finally:
            self.aides = aides

    async def _lire(self, image, echelle=1.0):
        """Mots lus dans l'image (PIL, RVB), par ligne : (texte, x1, y1, x2, y2) ramenés à la taille réelle."""
        if not isinstance(image, Image.Image):
            image = Image.fromarray(image)
        ecrivain = DataWriter()
        ecrivain.write_bytes(image.convert("RGB").tobytes("raw", "BGRX"))  # (sans passer par un fichier BMP)
        bitmap = SoftwareBitmap.create_copy_from_buffer(ecrivain.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                        image.width, image.height)
        resultat = await self.moteur.recognize_async(bitmap)
        lignes = []
        for ligne in resultat.lines:
            mots = []
            for mot in ligne.words:
                r = mot.bounding_rect
                mots.append((mot.text, int(r.x / echelle), int(r.y / echelle), round((r.x + r.width) / echelle),
                             round((r.y + r.height) / echelle)))
            if mots:
                lignes.append(sorted(mots, key=lambda m: m[1]))
        return lignes

    @staticmethod
    def _decouper(mots):
        """Une « ligne » de l'OCR regroupe parfois plusieurs éléments alignés (menus, onglets,
        boutons) : on la coupe là où l'espace entre deux mots est bien plus grand qu'une espace."""
        hauteur = max(1, sorted(m[4] - m[2] for m in mots)[len(mots) // 2])
        morceaux, courant = [], [mots[0]]
        for precedent, mot in zip(mots, mots[1:]):
            # (Un tiret entre deux mots, que la lecture ne voit pas toujours, laisse ~1 hauteur d'écart.)
            if mot[1] - precedent[3] > hauteur * 1.4:
                morceaux.append(courant)
                courant = []
            courant.append(mot)
        morceaux.append(courant)
        # Ligne de chat (« 20:42 ow_shark: get iiiit ») : le pseudo et le message sont séparés. Seul le
        # message est traduit, et sa traduction se pose exactement sur lui.
        separes = []
        for groupe in morceaux:
            fin = LecteurImage._fin_de_pseudo(groupe)
            if fin is not None and fin + 1 < len(groupe):
                separes += [groupe[:fin + 1], groupe[fin + 1:]]
            else:
                separes.append(groupe)
        return [LecteurImage._sans_icone(g) for g in separes]

    @staticmethod
    def _sans_icone(mots):
        """Une icône devant le texte, lue comme une lettre (le globe -> « @ World », la loupe -> « Q Search ») :
        retirée, du texte et de son cadre (sinon le cadre trop haut donnait une traduction énorme). C'est un
        « mot » d'un seul signe, presque carré, loin du suivant ; pas « a », « I » ou « à », étroits et proches."""
        if len(mots) < 2 or len(mots[0][0]) != 1:
            return mots
        signe, x1, y1, x2, y2 = mots[0]
        hauteur_suivant = max(1, mots[1][4] - mots[1][2])
        # (Une icône est aussi haute que le texte qui suit ; « a » de « a month ago » est plus petit que « month ».)
        carre = (x2 - x1) >= 0.75 * (y2 - y1) and (y2 - y1) >= 0.9 * hauteur_suivant
        loin = mots[1][1] - x2 >= 0.35 * hauteur_suivant
        if signe in "aAàÀyYIiô":  # de vrais mots d'une lettre (« a month ago », « à jour », « I agree »)
            return mots
        return mots[1:] if (not signe.isalnum() and loin) or (carre and loin) else mots

    @staticmethod
    def _fin_de_pseudo(mots):
        """Place du mot « pseudo: » qui commence une ligne de chat (après l'heure et les badges), ou
        None. Seulement un vrai pseudo (ow_shark, GamingWithFrancesca, HEMOO), pas « Note : »."""
        for i, (mot, *_) in enumerate(mots[:4]):
            if not mot.endswith(":"):
                continue
            nom = mot[:-1] or (mots[i - 1][0] if i else "")  # « Streamlabs : » : deux-points à part
            if 2 <= len(nom) <= 25 and "/" not in nom and nom.lower() not in ("http", "https") \
                    and (MOT_DE_PSEUDO.search(nom) or not est_un_vrai_mot(nom)):
                return i
            return None
        return None

    @staticmethod
    def _assembler(mots):
        texte = ""
        for mot, *_ in mots:
            if texte.endswith("-") and len(texte) > 1 and texte[-2].isalpha():
                texte = texte[:-1] + mot  # mot coupé en fin de ligne
            else:
                texte = f"{texte} {mot}" if texte else mot
        return texte


class CaptureRapide:
    """Image de tout l'écran, utilisable depuis plusieurs fils.

    Par la carte graphique (duplication d'écran DirectX : ~3 ms, et rien à faire quand l'écran
    n'a pas changé) quand c'est possible, sinon par copie classique (mss : ~25 ms). Les fenêtres
    cachées des captures (nos vitres, quand on lit l'écran) n'y apparaissent pas."""

    def __init__(self):
        self.verrou = threading.Lock()
        self.camera = None
        self._preparer()

    @staticmethod
    def _disposition_ecrans():
        """Place, taille et nombre des écrans (lu à chaque image : ça ne coûte rien)."""
        return tuple(user32.GetSystemMetrics(i) for i in (76, 77, 78, 79, 80))  # SM_XVIRTUALSCREEN… SM_CMONITORS

    def _preparer(self):
        """(Re)prépare la capture pour les écrans actuels : au lancement, et quand on branche un écran ou change la
        résolution (sinon la capture restait sur l'ancienne taille, tronquée ou noire)."""
        if self.camera is not None:
            try:
                self.camera.release()
            except Exception:
                pass
        self.disposition = self._disposition_ecrans()
        self.locales = threading.local()  # mss : une instance par fil
        self.camera = None
        self.derniere = None
        with CaptureEcran() as capture:
            ecrans = capture.monitors
        self.ecran = ecrans[0]  # rectangle de tous les écrans réunis
        self.origine = (self.ecran["left"], self.ecran["top"])
        if dxcam is not None and len(ecrans) == 2:  # un seul écran (sinon : copie classique)
            try:
                camera = dxcam.create(output_idx=0, output_color="BGRA")
                if (camera.width, camera.height) == (self.ecran["width"], self.ecran["height"]):
                    self.camera = camera
            except Exception:
                log.info("Capture par la carte graphique indisponible : copie classique de l'écran")

    def prendre(self):
        """(pixels BGRA, origine) de l'écran tel qu'il est maintenant."""
        with self.verrou:
            if self._disposition_ecrans() != self.disposition:
                log.info("Écrans changés (nombre ou résolution) : capture préparée à nouveau")
                self._preparer()
            if self.camera is not None:
                try:
                    image = self.camera.grab()
                    if image is None:  # rien n'a changé depuis la dernière image
                        image = self.derniere if self.derniere is not None else \
                            self.camera.grab(new_frame_only=False)
                    if image is not None:
                        self.derniere = image
                        return image, self.origine
                except Exception:
                    log.exception("Capture par la carte graphique impossible : copie classique de l'écran")
                    self.camera = None
            capture = getattr(self.locales, "mss", None)
            if capture is None:
                capture = self.locales.mss = CaptureEcran()
            image = capture.grab(self.ecran)
            return np.frombuffer(image.bgra, np.uint8).reshape(image.height, image.width, 4), self.origine


# --------------------------------------------------------------------------- images et vidéos
#
# Le texte posé sur une image (photo, miniature de vidéo, vidéo en pause, dessin) n'est pas
# traduit. Une image se repère à sa texture : beaucoup de petites variations de couleur d'un
# pixel à l'autre, alors qu'une interface est faite d'aplats et de contours nets. On part des
# zones texturées et on les agrandit jusqu'au fond lisse qui entoure l'image : les parties
# lisses d'une image (ciel, aplats d'un dessin) sont ainsi couvertes aussi.

def blocs_textures(rgb):
    """Blocs de BLOC_IMAGE pixels de côté qui ressemblent à une photo (`rgb` : tableau int16)."""
    b = BLOC_IMAGE
    h, w = rgb.shape[0] // b * b, rgb.shape[1] // b * b
    if h < b or w < 2 * b:
        return np.zeros((0, 0), bool)
    d = np.abs(rgb[:h, 1:w] - rgb[:h, :w - 1]).sum(axis=2)
    d = np.concatenate([d, np.zeros((h, 1), d.dtype)], axis=1)
    return ((d > 12) & (d < 90)).reshape(h // b, b, w // b, b).mean(axis=(1, 3)) > 0.25


def groupes_de_blocs(masque, min_blocs=5):
    """Cadres (en blocs : x1, y1, x2, y2) des groupes de blocs voisins d'au moins 3 × 3 blocs."""
    hauteur, largeur = masque.shape
    vu = np.zeros_like(masque)
    cadres = []
    for y0, x0 in zip(*np.nonzero(masque)):
        if vu[y0, x0]:
            continue
        pile, n = [(y0, x0)], 0
        vu[y0, x0] = True
        ya = yb = y0
        xa = xb = x0
        while pile:
            y, x = pile.pop()
            n += 1
            ya, yb, xa, xb = min(ya, y), max(yb, y), min(xa, x), max(xb, x)
            for yy in (y - 1, y, y + 1):
                for xx in (x - 1, x, x + 1):
                    if 0 <= yy < hauteur and 0 <= xx < largeur and masque[yy, xx] and not vu[yy, xx]:
                        vu[yy, xx] = True
                        pile.append((yy, xx))
        if n >= min_blocs and yb - ya >= 2 and xb - xa >= 2:
            cadres.append((int(xa), int(ya), int(xb) + 1, int(yb) + 1))
    return cadres


def lignes_bords(lignes, fonds=None):
    """Pour chaque ligne de pixels (tableau lignes × longueur × BGRA) : est-ce le bord d'une image,
    c'est-à-dire une ligne lisse (sans contour net) et, si `fonds` est donné, de l'une de ces
    couleurs de fond de page (BGR) ?"""
    gris = lignes[..., :3].sum(axis=2, dtype=np.int16)
    if gris.shape[1] < 2:
        lisse = np.ones(len(gris), bool)
    else:
        lisse = (np.abs(gris[:, 1:] - gris[:, :-1]) <= 12).mean(axis=1) >= 0.97
    if fonds is None or not lisse.any():
        return lisse
    moyennes = lignes[lisse, :, :3].mean(axis=1)
    lisse[lisse] = (np.abs(moyennes[:, None, :] - fonds[None]).sum(axis=2) <= 45).any(axis=1)
    return lisse


def chercher_ligne(ecran, debut, pas, fin, de, a, en_colonnes, bord, fonds=None):
    """Première rangée (ou colonne, si `en_colonnes`), de `debut` vers `fin` (exclue) par pas de
    `pas`, qui est un bord d'image (si `bord`) ou qui n'en est pas un (sinon) ; `fin` si aucune.
    Chaque rangée va de la colonne `de` à `a` (chaque colonne, de la rangée `de` à `a`).
    Examine les lignes par paquets (4, puis 16, puis 64) : le bord est le plus souvent tout près."""
    i, paquet = debut, 4
    while i != fin:
        j = min(i + paquet, fin) if pas > 0 else max(i - paquet, fin)
        paquet = min(64, paquet * 4)
        indices = np.arange(i, j, pas)
        lignes = ecran[de:a, indices].transpose(1, 0, 2) if en_colonnes else ecran[indices, de:a]
        trouve = lignes_bords(lignes, fonds) == bord
        if trouve.any():
            return int(indices[np.argmax(trouve)])
        i = j
    return fin


def ajuster_image(ecran, x1, y1, x2, y2):
    """Agrandit le cadre tant que son bord traverse l'image, puis le resserre sur le fond lisse."""
    hauteur, largeur = ecran.shape[:2]
    for _ in range(2):
        y1 = chercher_ligne(ecran, y1 - 1, -1, -1, x1, x2, False, True) + 1
        y2 = chercher_ligne(ecran, y2, 1, hauteur, x1, x2, False, True)
        x1 = chercher_ligne(ecran, x1 - 1, -1, -1, y1, y2, True, True) + 1
        x2 = chercher_ligne(ecran, x2, 1, largeur, y1, y2, True, True)
    if y2 - y1 > 8:
        y1 = chercher_ligne(ecran, y1, 1, y2 - 8, x1, x2, False, False)
        y2 = chercher_ligne(ecran, y2 - 1, -1, y1 + 7, x1, x2, False, False) + 1
    if x2 - x1 > 8:
        x1 = chercher_ligne(ecran, x1, 1, x2 - 8, y1, y2, True, False)
        x2 = chercher_ligne(ecran, x2 - 1, -1, x1 + 7, y1, y2, True, False) + 1
    return x1, y1, x2, y2


def se_touchent(a, b, marge=0):
    return a[0] - marge < b[2] and b[0] - marge < a[2] and a[1] - marge < b[3] and b[1] - marge < a[3]


def surface_commune(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def sans_doublons(textes):
    """Retire les textes qui en chevauchent un autre (la même ligne lue deux fois, à quelques
    pixels près) : la lecture la plus récente l'emporte, puis la plus grande. Garde l'ordre."""
    gardes = []
    for t in sorted(textes, key=lambda t: (-t.version, -t.l * t.h)):
        cadre = (t.x, t.y, t.x + t.l, t.y + t.h)
        if not any(surface_commune(cadre, (g.x, g.y, g.x + g.l, g.y + g.h)) >= 0.3 * min(t.l * t.h, g.l * g.h)
                   for g in gardes):
            gardes.append(t)
    gardes = set(gardes)
    return [t for t in textes if t in gardes]


def etendre_jusqu_au_fond(ecran, cadre, fonds):
    """Une miniature en dessin a de grands aplats de couleur (qui arrêtent ajuster_image) : on
    agrandit le cadre jusqu'à retrouver le fond de la page tout autour (`fonds` : couleurs neutres
    du fond, BGR). Renvoie le cadre agrandi, ou None si ça ne ressemble pas à une miniature
    (touche le bord de l'écran, trop grand, trop allongé)."""
    x1, y1, x2, y2 = cadre
    hauteur, largeur = ecran.shape[:2]
    for _ in range(2):
        y1 = chercher_ligne(ecran, y1 - 1, -1, -1, x1, x2, False, True, fonds) + 1
        y2 = chercher_ligne(ecran, y2, 1, hauteur, x1, x2, False, True, fonds)
        x1 = chercher_ligne(ecran, x1 - 1, -1, -1, y1, y2, True, True, fonds) + 1
        x2 = chercher_ligne(ecran, x2, 1, largeur, y1, y2, True, True, fonds)
    l, h = x2 - x1, y2 - y1
    if x1 == 0 or y1 == 0 or x2 == largeur or y2 == hauteur or l * h > 0.25 * largeur * hauteur \
            or not 0.75 <= l / max(1, h) <= 2.0:
        return None
    return x1, y1, x2, y2


def rectangles_images(ecran, zone, fonds, deja=()):
    """Grands rectangles posés sur le fond de la page (`fonds` : ses couleurs, BGR) dont l'intérieur
    change de couleur d'un bout à l'autre (dégradé d'un rendu 3D, photo floue) : des images, même
    sans texture. Un bouton, une carte d'interface sont presque d'une seule couleur (en ne comptant
    pas les images `deja` repérées dedans : une carte avec une photo reste une carte)."""
    x1, y1, x2, y2 = zone
    b = BLOC_IMAGE
    h, w = (y2 - y1) // b * b, (x2 - x1) // b * b
    if h < 8 * b or w < 10 * b:
        return []
    pixels = ecran[y1:y1 + h:2, x1:x1 + w:2, :3].astype(np.int16)  # un pixel sur deux suffit
    est_fond = np.zeros(pixels.shape[:2], bool)
    for couleur in fonds:
        est_fond |= np.abs(pixels - couleur).sum(axis=2) <= 24
    hb, lb, d = h // b, w // b, b // 2
    contenu = est_fond.reshape(hb, d, lb, d).mean(axis=(1, 3)) < 0.3
    # Couleur de chaque bloc (médiane de 5 points : le fond du bloc, pas les lettres), en paliers de 12.
    blocs = pixels.reshape(hb, d, lb, d, 3)
    points = np.stack([blocs[:, 0, :, 0], blocs[:, 0, :, -1], blocs[:, -1, :, 0], blocs[:, -1, :, -1],
                       blocs[:, d // 2, :, d // 2]], axis=-1)
    medianes = np.median(points, axis=-1).astype(np.int32) // 12
    cles = (medianes[..., 0] * 32 + medianes[..., 1]) * 32 + medianes[..., 2]
    hors_images = np.ones((hb, lb), bool)
    for cx1, cy1, cx2, cy2 in deja:
        hors_images[max(0, (cy1 - y1) // b):max(0, -(-(cy2 - y1) // b)),
                    max(0, (cx1 - x1) // b):max(0, -(-(cx2 - x1) // b))] = False
    cadres = []
    for a, c, e, f in groupes_de_blocs(contenu, min_blocs=60):
        if e - a < 10 or f - c < 8 or contenu[c:f, a:e].mean() < 0.85:
            continue  # trop petit, ou pas un rectangle plein
        if (e - a) * b > 0.75 * ecran.shape[1]:
            continue  # bandeau sur toute la largeur : un décor de page, pas une image
        if (e - a) >= 10 and 1.6 <= (e - a) / (f - c) <= 1.95:
            # Format d'une miniature de vidéo (16:9) : une image, même faite d'aplats de couleur
            # (« Exit 8 » sur fond jaune ressemble sinon à une carte d'interface).
            cadres.append((x1 + a * b, y1 + c * b, x1 + e * b, y1 + f * b))
            continue
        garder = contenu[c:f, a:e] & hors_images[c:f, a:e]
        if garder.sum() < 0.3 * contenu[c:f, a:e].sum():
            continue  # presque entièrement une image déjà repérée
        couleurs = cles[c:f, a:e][garder]
        if np.bincount(couleurs).max() < 0.35 * len(couleurs):
            cadres.append((x1 + a * b, y1 + c * b, x1 + e * b, y1 + f * b))
    return cadres


def zones_images(ecran, zone):
    """Cadres (coordonnées de l'image `ecran`, BGRA) des photos, miniatures et vidéos qui ont une
    partie texturée dans la zone (x1, y1, x2, y2), et des grands rectangles d'image sans texture."""
    x1, y1, x2, y2 = zone
    b = BLOC_IMAGE
    textures = blocs_textures(ecran[y1:y2, x1:x2, :3].astype(np.int16))
    cadres = [(x1 + a * b, y1 + c * b, x1 + d * b, y1 + e * b) for a, c, d, e in groupes_de_blocs(textures)]
    for _ in range(3):
        cadres = [ajuster_image(ecran, *c) for c in cadres]
        # Les morceaux d'une même image (une vidéo a aussi des parties lisses) se rejoignent.
        fusion = []
        for c in cadres:
            for i, f in enumerate(fusion):
                if se_touchent(c, f, marge=8):
                    fusion[i] = (min(c[0], f[0]), min(c[1], f[1]), max(c[2], f[2]), max(c[3], f[3]))
                    break
            else:
                fusion.append(c)
        if len(fusion) == len(cadres):
            break
        cadres = fusion
    # Couleurs du fond de la page : les plus fréquentes de la zone, si elles sont neutres
    # (blanc, gris, noir) ; une couleur vive est plutôt l'aplat d'un dessin.
    echantillon = ecran[y1:y2:4, x1:x2:4, :3] // 8
    cles = ((echantillon[..., 0].astype(np.int32) * 32 + echantillon[..., 1]) * 32 + echantillon[..., 2]).ravel()
    if not len(cles):
        return cadres
    comptes = np.bincount(cles)
    fonds = []
    for mode in np.argsort(comptes)[::-1][:6]:
        if comptes[mode] < 0.02 * len(cles):
            break
        couleur = np.array([mode // 1024 * 8 + 4, mode // 32 % 32 * 8 + 4, mode % 32 * 8 + 4])
        if couleur.max() - couleur.min() <= 40:
            fonds.append(couleur)
    if fonds:
        cadres = [etendre_jusqu_au_fond(ecran, c, np.array(fonds)) or c for c in cadres]
        # Ici, seulement la couleur de fond principale : le dégradé sombre d'une image en a d'autres.
        cadres += rectangles_images(ecran, zone, np.array(fonds[:1]), cadres)
    return cadres


def sur_fond_varie(ecran, x1, y1, x2, y2):
    """Le texte est-il posé sur un fond varié (photo, dessin) plutôt que sur un aplat ? On regarde
    un liseré autour du texte : sur une interface, une ou deux couleurs suffisent à le remplir."""
    e = max(3, (y2 - y1) // 3)
    hauteur, largeur = ecran.shape[:2]
    a1, b1, a2, b2 = max(0, x1 - e), max(0, y1 - e), min(largeur, x2 + e), min(hauteur, y2 + e)
    zone = ecran[b1:b2, a1:a2, :3]
    masque = np.ones(zone.shape[:2], bool)
    masque[max(0, y1 - 1 - b1):max(0, y2 + 1 - b1), max(0, x1 - 1 - a1):max(0, x2 + 1 - a1)] = False
    lisere = zone[masque] // 24
    if len(lisere) < 20:
        return False
    cles = (lisere[:, 0].astype(np.int32) * 16 + lisere[:, 1]) * 16 + lisere[:, 2]
    comptes = np.sort(np.bincount(cles))
    return comptes[-2:].sum() < 0.6 * len(cles)


def profil_vertical(image, origine, zone, gauche, droite):
    """Luminosité moyenne de chaque rangée de pixels de la zone, entre les colonnes gauche et
    droite (coordonnées de l'écran) : une « empreinte » qui glisse avec le contenu."""
    ox, oy = origine
    return image[zone[1] - oy:zone[3] - oy, gauche - ox:droite - ox:2, :3].mean(axis=(1, 2), dtype=np.float32)


def estimer_decalage(avant, apres, precedent, portee=300):
    """De combien de pixels le contenu a glissé verticalement (positif : vers le bas), d'après
    les empreintes d'avant et d'après. Cherché autour du décalage précédent ; None si on ne peut
    pas le savoir (zone unie, ou rien qui corresponde)."""
    hauteur = len(apres)
    if hauteur < 40 or avant.std() < 1.5:
        return None
    marge = abs(precedent) + portee
    vide = np.full(marge, np.nan, np.float32)
    fenetres = np.lib.stride_tricks.sliding_window_view(np.concatenate([vide, avant, vide]), hauteur)
    fenetres = fenetres[marge - precedent - portee:marge - precedent + portee + 1]
    decalages = precedent + portee - np.arange(len(fenetres))  # ligne j : décalage de precedent + portee - j
    ecarts = np.minimum(np.abs(fenetres - apres), 25)  # un en-tête fixe ne compte pas trop
    nombre = np.count_nonzero(~np.isnan(ecarts), axis=1)
    score = np.nansum(ecarts, axis=1) / np.maximum(nombre, 1) + 0.002 * np.abs(decalages - precedent)
    score = np.where(nombre >= hauteur * 0.3, score, np.inf)
    j = int(np.argmin(score))
    if score[j] >= 6:
        return None
    # Une page presque vide (marges blanches, peu de texte) colle à plusieurs décalages : si un
    # autre décalage, nettement différent, colle presque aussi bien, on ne sait pas.
    ailleurs = np.abs(decalages - decalages[j]) > 4
    if ailleurs.any() and score[ailleurs].min() - score[j] < 1.0:
        return None
    return int(decalages[j])


def ecart_motif(avant, apres, origine, t, dy):
    """Écart moyen entre le texte `t` dans l'image d'avant et le même endroit décalé de `dy`
    dans l'image d'après (deux images de tout l'écran). None si cet endroit sort de l'écran."""
    x, y = t.x - origine[0], t.y - origine[1]
    hauteur, largeur = apres.shape[:2]
    if x < 0 or y < 0 or x + t.l > largeur or y + t.h > hauteur or y + dy < 0 or y + dy + t.h > hauteur:
        return None
    a = avant[y:y + t.h:2, x:x + t.l:2, :3].astype(np.int16)
    b = apres[y + dy:y + dy + t.h:2, x:x + t.l:2, :3].astype(np.int16)
    return float(np.abs(a - b).sum(axis=2).mean()) if a.size else None


class SuiviEcran:
    """Suit le texte visible dans l'image de l'écran (à utiliser depuis un seul fil).

    Un texte lu reste valable tant que ses pixels ne changent pas : on ne relit que ce qui a
    changé, et seulement une fois que l'image ne bouge plus (fin d'un défilement, d'une
    animation), pour ne jamais poser une traduction au mauvais endroit. Après un défilement, les
    textes qui ont seulement glissé sont retrouvés à leur nouvelle place sans être relus. Ce qui
    bouge sans arrêt (vidéo, animation) n'est pas lu, et le texte posé sur une image (photo,
    miniature, vidéo en pause) est écarté."""

    def __init__(self, capturer):
        self.capturer = capturer  # () -> (pixels BGRA, origine) sans nos traductions, ou None
        self.ocr = LecteurImage()
        self.parallele = ThreadPoolExecutor(max_workers=1)  # recherche des images pendant la lecture
        self.morceaux = []  # textes lus, en coordonnées de l'écran
        self.a_decaler = []  # (cadre de fenêtre, dx, dy) : fenêtres déplacées (rempli par un autre fil)
        self.motifs = {}  # morceau -> ses pixels au moment de la lecture
        self.perdus = []  # morceaux dont les pixels ont changé (peut-être seulement décalés)
        self.origines = {}  # morceau retrouvé après un défilement -> le même avant le défilement
        self.apercu = None  # aperçu (1/8) de la dernière image
        self.reference = None  # dernière image lue (copie modifiable)
        self.agitation = None  # par tuile : nombre d'images de suite où elle a changé
        self.en_attente = None  # par tuile : a changé mais n'a pas encore été relue
        self.image = None  # dernière image : (pixels, origine)
        self.numero = 0  # numéro de la dernière lecture
        self.mouvement_recent = lambda: False  # () -> on fait défiler ou on déplace une fenêtre ?
        self.medias = lambda: []  # () -> cadres (écran) des images et vidéos signalées par le navigateur
        # () -> cadres (écran) des textes d'interface signalés par l'accessibilité : là, c'est du texte même si le
        # fond bouge sans arrêt (vidéo derrière une fenêtre translucide) ; ce n'est pas une vidéo à ne pas lire.
        self.textes_surs = lambda: []
        self.verifie_le = 0.0  # dernière vérification de tous les textes

    def oublier(self):
        """Tout relire à la prochaine analyse (reprise après une pause, erreur)."""
        self.apercu = None

    @staticmethod
    def _apercu(pixels):
        """Aperçu (1/8) : somme du vert de chaque carré de 8 × 8 pixels (un pixel sur deux en long et
        en large). Assez fin pour voir apparaître ou disparaître une petite ligne de texte, qu'un
        seul pixel par carré pouvait manquer (une traduction restait alors affichée). Le vert seul
        suffit (le texte se voit dans toutes les couleurs) et va 6 fois plus vite ; un changement de
        couleur à vert égal est rattrapé par la vérification complète, une fois par seconde."""
        h, w = pixels.shape[0] // 8 * 8, pixels.shape[1] // 8 * 8
        vert = pixels[:h:2, :w:2, 1].astype(np.int32)
        return vert.reshape(h // 8, 4, w // 8, 4).sum(axis=(1, 3))

    @staticmethod
    def _tuiles_changees(a, b):
        change = np.abs(a - b) > 50  # (sur le vert seul : l'équivalent de 150 sur les trois couleurs)
        cote = TUILE // 8
        th, tl = -(-change.shape[0] // cote), -(-change.shape[1] // cote)
        grand = np.zeros((th * cote, tl * cote), bool)
        grand[:change.shape[0], :change.shape[1]] = change
        return grand.reshape(th, cote, tl, cote).any(axis=(1, 3))

    def decaler_fenetre(self, fenetre, dx, dy):
        """(Autre fil.) Une fenêtre a été déplacée : ses textes la suivent, sans être relus (relus à moitié,
        ils revenaient décalés, en double)."""
        self.a_decaler.append((tuple(fenetre), dx, dy))

    def _decaler_fenetres(self):
        while self.a_decaler:
            fenetre, dx, dy = self.a_decaler.pop(0)
            nouvelle = (fenetre[0] + dx, fenetre[1] + dy, fenetre[2] + dx, fenetre[3] + dy)

            def decaler(m):
                if tuple(m.fenetre) != fenetre:
                    return m
                deplace = replace(m, x=m.x + dx, y=m.y + dy, fenetre=nouvelle)
                if m in self.motifs:
                    self.motifs[deplace] = self.motifs.pop(m)
                return deplace

            self.morceaux = [decaler(m) for m in self.morceaux]
            self.perdus = [decaler(m) for m in self.perdus]

    def analyser(self):
        """Une analyse de l'écran. Renvoie (les textes ont changé ?, tout ce qui ne bouge pas est lu ?)."""
        self._decaler_fenetres()
        image = self.capturer()
        if image is None:
            return False, False
        pixels, origine = image
        apercu = self._apercu(pixels)
        if self.apercu is None or apercu.shape != self.apercu.shape:
            self._tout_lire(pixels, origine, apercu)
            return True, True
        changees = self._tuiles_changees(apercu, self.apercu)
        self.apercu, self.image = apercu, image
        # (Plafonné : après une demi-heure de vidéo, le compteur ne repart pas dans les négatifs.)
        self.agitation = np.where(changees, np.minimum(self.agitation, 1000) + 1, 0)
        if not self.mouvement_recent():
            # Bouge sans arrêt depuis ~3 secondes (pas un défilement, ni une page qui se charge) : une vidéo.
            self.agite_le[self.agitation >= 30] = time.monotonic()
        self.en_attente |= changees
        if time.monotonic() - self.verifie_le > 1.0:
            # Une fois par seconde, tous les textes sont comparés à l'écran, même là où l'aperçu n'a
            # rien vu changer : aucune traduction ne reste sur un texte qui n'est plus là.
            self.verifie_le = time.monotonic()
            deja_perdus = len(self.perdus)
            modifie = self._oublier_changes(pixels, origine, np.ones_like(changees))
            for m in self.perdus[deja_perdus:]:  # là où un texte a changé sans qu'on le voie : à relire
                x1, y1 = max(0, m.x - origine[0]), max(0, m.y - origine[1])
                self.en_attente[y1 // TUILE:(y1 + m.h - 1) // TUILE + 1, x1 // TUILE:(x1 + m.l - 1) // TUILE + 1] = True
        else:
            modifie = self._oublier_changes(pixels, origine, changees)
        # Un chat, une liste qui défile tout seul : les textes connus qui ont seulement glissé sont
        # retrouvés tout de suite (sinon ce qui change à chaque analyse passerait pour une vidéo).
        modifie |= self._defile(pixels, origine, changees)
        surs = self._tuiles_textes_surs(changees.shape, origine)
        if surs.any():
            # Fond qui bouge sans arrêt : relu au plus toutes les 0,7 s (le texte, lui, ne change pas).
            surs &= (self.agitation < 3) | (time.monotonic() - self.relu_sur_le > 0.7)
        candidates = self.en_attente & ((self.agitation < 3) | surs)  # ce qui bouge sans arrêt attend
        if not candidates.any():
            return modifie, True
        # Attendre que l'image ne bouge plus à cet endroit (défilement fluide, animation).
        time.sleep(0.025)
        image = self.capturer()
        if image is None:
            return modifie, False
        pixels, origine = image
        apercu = self._apercu(pixels)
        bouge = self._tuiles_changees(apercu, self.apercu)
        self.apercu, self.image = apercu, image
        self.agitation = np.where(bouge, np.minimum(self.agitation, 1000) + 1, self.agitation)
        self.en_attente |= bouge
        modifie |= self._oublier_changes(pixels, origine, bouge)
        stables = candidates & (~bouge | surs)
        # Là où une vidéo jouait il y a quelques secondes (pause, plan fixe), on attend avant de lire.
        recentes = stables & (time.monotonic() - self.agite_le < MEMOIRE_VIDEO) & ~surs
        if (stables & ~recentes).any():
            modifie |= self._relire(pixels, origine, stables & ~recentes)
            self.en_attente &= ~(stables & ~recentes)
            self.relu_sur_le[stables & surs] = time.monotonic()
        return modifie, not (self.en_attente & ((self.agitation < 3) | surs) & ~recentes).any()

    def _tuiles_textes_surs(self, forme, origine):
        """Tuiles touchées par un texte d'interface que l'accessibilité voit (voir textes_surs)."""
        masque = np.zeros(forme, bool)
        for x1, y1, x2, y2 in self.textes_surs():
            tx1, ty1 = max(0, (x1 - origine[0]) // TUILE), max(0, (y1 - origine[1]) // TUILE)
            tx2, ty2 = (x2 - origine[0] - 1) // TUILE + 1, (y2 - origine[1] - 1) // TUILE + 1
            if tx2 > tx1 and ty2 > ty1:
                masque[ty1:ty2, tx1:tx2] = True
        return masque

    def _defile(self, pixels, origine, changees):
        """Contenu qui défile tout seul (nouveau message dans un chat) : les textes connus qui ont
        seulement glissé sont retrouvés à leur nouvelle place, sans relecture et sans attendre que
        l'écran se calme. Leur colonne ne compte plus comme « agitée » (ce n'est pas une vidéo) : les
        nouveaux messages qui arrivent au bord sont lus dès l'analyse suivante. Renvoie True si des
        textes ont été retrouvés."""
        if not self.perdus or self.reference is None:
            return False
        ox = origine[0]
        # Seulement les colonnes où des textes connus ont changé (pas la vidéo à côté du chat).
        colonnes = np.zeros(changees.shape[1], bool)
        for m in self.perdus:
            colonnes[max(0, m.x - ox) // TUILE:max(0, m.x - ox + m.l - 1) // TUILE + 1] = True
        retrouves, trouves, expliquees = self._retrouver_decales(pixels, origine, changees & colonnes[None, :])
        if not trouves:
            return False
        self.morceaux = self.morceaux + retrouves
        self.perdus = [m for m in self.perdus if m not in trouves]
        if expliquees.any():
            lignes, cols = np.nonzero(expliquees.any(axis=1))[0], np.nonzero(expliquees.any(axis=0))[0]
            boite = np.zeros_like(changees)
            # (+ 1 rangée : le bord où arrivent les nouvelles lignes.)
            boite[lignes.min():lignes.max() + 2, cols.min():cols.max() + 1] = True
            self.agitation[boite] = 0
            self.en_attente &= ~expliquees
            for ty, tx in zip(*np.nonzero(boite)):
                y1, x1 = int(ty) * TUILE, int(tx) * TUILE
                self.reference[y1:y1 + TUILE, x1:x1 + TUILE] = pixels[y1:y1 + TUILE, x1:x1 + TUILE]
        return True

    def _tout_lire(self, pixels, origine, apercu):
        self.apercu, self.image = apercu, (pixels, origine)
        self.reference = pixels.copy()
        forme = self._tuiles_changees(apercu, apercu).shape
        self.agitation = np.zeros(forme, np.int16)
        self.agite_le = np.zeros(forme)  # par tuile : dernier instant où elle bougeait comme une vidéo
        self.relu_sur_le = np.zeros(forme)  # par tuile : dernière relecture d'un texte sûr sur un fond qui bouge
        self.en_attente = np.zeros(forme, bool)
        self.morceaux, self.motifs, self.perdus = [], {}, []
        hauteur, largeur = pixels.shape[:2]
        self._lire_zone(pixels, origine, (0, 0, largeur, hauteur))

    def _a_change(self, m, pixels, x1, y1, x2, y2):
        """Les pixels du texte `m` sont-ils différents à cet endroit de l'image ?"""
        motif = self.motifs.get(m)
        actuel = pixels[max(0, y1):y2, max(0, x1):x2, :3]
        if motif is None or actuel.shape != motif.shape:
            return True
        ecarts = np.abs(actuel.astype(np.int16) - motif).sum(axis=2) > 30
        return np.count_nonzero(ecarts) > max(2, ecarts.size // 100)

    def _oublier_changes(self, pixels, origine, tuiles):
        """Oublie les textes dont les pixels ont changé dans ces tuiles (ils sont gardés de côté :
        après un défilement, on les retrouvera plus haut ou plus bas)."""
        if not tuiles.any() or not self.morceaux:
            return False
        ox, oy = origine
        gardes, perdus, recolores = [], [], False
        for m in self.morceaux:
            x1, y1 = m.x - ox, m.y - oy
            x2, y2 = x1 + m.l, y1 + m.h
            dessous = tuiles[max(0, y1) // TUILE:max(0, y2 - 1) // TUILE + 1,
                             max(0, x1) // TUILE:max(0, x2 - 1) // TUILE + 1]
            if not (dessous.any() and self._a_change(m, pixels, x1, y1, x2, y2)):
                gardes.append(m)
            elif self._memes_lettres(m, pixels, x1, y1, x2, y2):
                motif = pixels[y1:y2, x1:x2, :3].astype(np.int16)
                encre = couleur_encre(motif)
                if not encre or not m.encre or sum(abs(x - y) for x, y in zip(encre, m.encre)) < 4:
                    # Seul le fond a changé (vidéo derrière une fenêtre translucide, image d'un stream) : le
                    # même texte au même endroit. Rien ne bouge, sa traduction reste telle quelle (avant, il
                    # était oublié, attendait que le fond se calme, puis revenait décalé : ça clignotait).
                    self.motifs[m] = motif
                    gardes.append(m)
                    continue
                # Survol, clic, focus : mêmes lettres, autres couleurs. Le texte reste (sa traduction
                # ne disparaît pas) ; nouvelle version, pour qu'elle reprenne les nouvelles couleurs.
                self.numero += 1
                nouveau = replace(m, version=self.numero, encre=encre)
                self.motifs[nouveau] = motif
                gardes.append(nouveau)
                recolores = True
            else:
                perdus.append(m)
        if not perdus and not recolores:
            return False
        self.morceaux = gardes
        self.perdus += perdus
        return True

    @staticmethod
    def _contours(pixels):
        gris = pixels.sum(axis=2)
        return np.abs(gris[:, 1:] - gris[:, :-1]) > 90

    def _memes_lettres(self, m, pixels, x1, y1, x2, y2):
        """Mêmes lettres au même endroit, seulement recolorées (survol, clic, focus) ou sur un fond qui a
        changé (vidéo derrière une fenêtre translucide, stream) ?"""
        motif = self.motifs.get(m)
        actuel = pixels[max(0, y1):y2, max(0, x1):x2, :3]
        if motif is None or actuel.shape != motif.shape or motif.shape[1] < 4:
            return False
        actuel = actuel.astype(np.int16)
        bouts = max(1, motif.shape[1] // 24)

        def memes_partout(a, b):
            """Par petits bouts de ~3 lettres : une seule lettre ou un chiffre qui change (« 12:01 » -> « 12:02 »)
            s'y voit, même si tout le reste (fond, couleurs) a changé aussi."""
            return not any(np.count_nonzero(pa ^ pb) > 0.3 * max(8, np.count_nonzero(pa | pb))
                           for pa, pb in zip(np.array_split(a, bouts, axis=1), np.array_split(b, bouts, axis=1)))

        if m.encre:
            # Les pixels couleur des lettres aux mêmes endroits : le même texte, quoi qu'il se passe autour (une
            # vidéo derrière une fenêtre translucide brouille les contours, pas les lettres elles-mêmes).
            encre = np.array(m.encre, np.int16) * 16 + 8
            a, b = np.abs(motif - encre).sum(axis=2) < 90, np.abs(actuel - encre).sum(axis=2) < 90
            union = np.count_nonzero(a | b)
            if union >= 10 and np.count_nonzero(a & b) >= 0.85 * union and memes_partout(a, b):
                return True
        a, b = self._contours(motif), self._contours(actuel)
        union = np.count_nonzero(a | b)
        if not union or np.count_nonzero(a & b) < 0.8 * union or not memes_partout(a, b):
            return False
        # Une recoloration touche tout le texte ; un mot ou un chiffre qui change, un seul endroit.
        change = np.abs(actuel - motif).sum(axis=2) > 30
        parts = np.array_split(change, max(1, min(4, change.shape[1] // 20)), axis=1)
        if all(p.mean() >= 0.05 for p in parts):
            return True
        # Seul le fond a bougé : les contours des lettres sont presque les mêmes partout.
        return np.count_nonzero(a & b) >= 0.9 * union

    def _relire(self, pixels, origine, stables):
        """Relit les tuiles qui ont changé et ne bougent plus. Renvoie True si des textes ont été ajoutés."""
        retrouves, trouves, expliquees = self._retrouver_decales(pixels, origine, stables)
        self.morceaux = self.morceaux + retrouves
        self.perdus = [m for m in self.perdus if m not in trouves]
        ajoutes = 0
        # Dans une image ou une vidéo signalée par le navigateur, rien ne sera traduit : inutile de lire.
        for zone in self._zones(stables & ~expliquees & ~self._tuiles_medias(stables.shape, origine),
                                pixels.shape, origine):
            ajoutes += self._lire_zone(pixels, origine, zone)
        self.perdus = []
        self.morceaux = sans_doublons(self.morceaux)
        self.motifs = {m: self.motifs[m] for m in self.morceaux if m in self.motifs}
        presents = set(self.morceaux)
        self.origines = {n: m for n, m in self.origines.items() if n in presents}
        # L'image de référence prend les tuiles relues (par suites de tuiles voisines sur une rangée).
        for ty in np.nonzero(stables.any(axis=1))[0]:
            rangee = np.concatenate([[False], stables[ty], [False]])
            bords = np.nonzero(rangee[1:] != rangee[:-1])[0]
            y1, y2 = int(ty) * TUILE, (int(ty) + 1) * TUILE
            for debut, fin in zip(bords[::2], bords[1::2]):
                self.reference[y1:y2, debut * TUILE:fin * TUILE] = pixels[y1:y2, debut * TUILE:fin * TUILE]
        return bool(retrouves) or ajoutes > 0

    def _tuiles_medias(self, forme, origine):
        """Tuiles entièrement dans une image ou une vidéo signalée par le navigateur."""
        masque = np.zeros(forme, bool)
        for x1, y1, x2, y2 in self.medias():
            tx1, ty1 = -(-(x1 - origine[0]) // TUILE), -(-(y1 - origine[1]) // TUILE)
            tx2, ty2 = (x2 - origine[0]) // TUILE, (y2 - origine[1]) // TUILE
            if tx2 > tx1 and ty2 > ty1:
                masque[max(0, ty1):ty2, max(0, tx1):tx2] = True
        return masque

    def _lire_zone(self, pixels, origine, zone):
        """Lit le texte d'une zone de l'image (coordonnées de l'image) et l'ajoute aux textes
        connus. Renvoie le nombre de textes ajoutés."""
        ox, oy = origine
        hauteur, largeur = pixels.shape[:2]
        # Grande zone (nouvelle page) : on cherche les images pendant que le texte est lu, en
        # parallèle (la lecture se fait hors de Python). Petite zone : après, autour du texte nouveau.
        grande = (zone[2] - zone[0]) * (zone[3] - zone[1]) >= 0.3 * hauteur * largeur
        images_en_cours = self.parallele.submit(zones_images, pixels, zone) if grande else None
        nouveaux = []  # (texte lu, cadre dans l'image, anciens textes qu'il remplace)
        lus = self.ocr.lire(pixels, origine, zone)
        if zone[0] > 0 or zone[2] < largeur:
            # Une ligne coupée par le bord gauche ou droit de la zone : la bande est relue sur toute la largeur.
            if any((m.x - ox <= zone[0] + 2 and zone[0] > 0) or (m.x - ox + m.l >= zone[2] - 2 and zone[2] < largeur)
                   for m in lus):
                return self._lire_zone(pixels, origine, (0, zone[1], largeur, zone[3]))
        for m in lus:
            cadre = (m.x - ox, m.y - oy, m.x - ox + m.l, m.y - oy + m.h)
            surface = max(1, m.l * m.h)
            if (cadre[1] <= zone[1] + 1 and zone[1] > 0) or (cadre[3] >= zone[3] - 1 and zone[3] < hauteur):
                continue  # coupé par le bord de la zone lue : ce n'est qu'un bout de ligne
            remplaces, deja_connu = [], False
            for k in self.morceaux:
                cadre_k = (k.x, k.y, k.x + k.l, k.y + k.h)
                commun = surface_commune((m.x, m.y, m.x + m.l, m.y + m.h), cadre_k)
                surface_k = max(1, k.l * k.h)
                # Le même texte lu tout près, un peu ailleurs (moins d'une ligne et demie, même colonne) : l'ancienne
                # lecture est périmée (fenêtre bougée sans qu'on le sache). Sinon il restait en double, décalé, et
                # les deux se gênaient (l'une écrite minuscule pour ne pas cacher l'autre).
                if commun < 0.85 * max(surface, surface_k) and k.texte.casefold() == m.texte.casefold() \
                        and abs(k.y - m.y) < 1.5 * max(k.h, m.h) and abs(k.x - m.x) < max(k.l, m.l):
                    remplaces.append(k)
                    continue
                if not commun:
                    continue
                if commun >= 0.85 * max(surface, surface_k):
                    deja_connu = True  # le même texte, inchangé : on garde l'ancien (rien ne clignote)
                    break
                if surface >= surface_k and commun >= 0.3 * surface_k:
                    remplaces.append(k)  # lecture plus complète (le texte s'est allongé, ligne entière…)
                elif commun >= 0.3 * surface:
                    deja_connu = True  # l'ancienne lecture couvre déjà l'essentiel de celle-ci
                    break
            if not deja_connu:
                nouveaux.append((m, cadre, remplaces))
        if not nouveaux:
            return 0
        if images_en_cours is not None:
            images = images_en_cours.result()
        else:
            # Images et vidéos : cherchées seulement autour des textes nouveaux (bien plus rapide que
            # sur toute la zone relue).
            x1 = max(0, min(c[0] for _, c, _ in nouveaux) - 300)
            y1 = max(0, min(c[1] for _, c, _ in nouveaux) - 300)
            x2 = min(largeur, max(c[2] for _, c, _ in nouveaux) + 300)
            y2 = min(hauteur, max(c[3] for _, c, _ in nouveaux) + 300)
            images = zones_images(pixels, (x1, y1, x2, y2))
        ajoutes = 0
        surs = self.textes_surs()
        for m, cadre, remplaces in nouveaux:
            # Un texte d'interface vu par l'accessibilité : du texte, même sur un fond qui bouge.
            sur = any(a <= m.x + m.l // 2 < c and b <= m.y + m.h // 2 < d for a, b, c, d in surs)
            if not sur and (any(surface_commune(cadre, c) >= 0.5 * m.l * m.h for c in images)
                            or sur_fond_varie(pixels, *cadre)):
                continue  # texte dans une image, une miniature, une vidéo : pas traduit
            dessous = self.agite_le[max(0, cadre[1]) // TUILE:max(0, cadre[3] - 1) // TUILE + 1,
                                    max(0, cadre[0]) // TUILE:max(0, cadre[2] - 1) // TUILE + 1]
            # Une vidéo jouait là il y a quelques secondes (pause, plan fixe) : pas traduit. La majorité du
            # texte doit y être (une ligne de chat qui touche le bord de la vidéo reste lue).
            if not sur and (time.monotonic() - dessous < MEMOIRE_VIDEO).mean() > 0.5:
                continue
            if remplaces:
                self.morceaux = [k for k in self.morceaux if k not in remplaces]
            self.numero += 1
            motif = pixels[cadre[1]:cadre[3], cadre[0]:cadre[2], :3].astype(np.int16)
            m = replace(m, version=self.numero, encre=couleur_encre(motif))
            self.morceaux.append(m)
            self.motifs[m] = motif
            ajoutes += 1
        return ajoutes

    def _zones(self, a_lire, forme, origine):
        """Zones de l'image à relire : autour des tuiles changées (avec une marge), élargies pour ne jamais couper
        en deux un texte connu (sinon on lirait la moitié d'une ligne). Une ligne nouvelle plus large que la zone
        est relue sur toute la largeur (voir _lire_zone). Avant, toute la largeur de l'écran était relue : un petit
        changement coûtait ~0,4 s de lecture au lieu de ~0,04."""
        hauteur, largeur = forme[:2]
        zones = []
        lignes = np.nonzero(a_lire.any(axis=1))[0]
        debut = None
        for i, ty in enumerate(lignes):
            debut = ty if debut is None else debut
            if i + 1 == len(lignes) or lignes[i + 1] != ty + 1:
                y1, y2 = int(debut) * TUILE - 24, (int(ty) + 1) * TUILE + 24
                colonnes = np.nonzero(a_lire[debut:ty + 1].any(axis=0))[0]
                groupe = [colonnes[0], colonnes[0]]
                for tx in list(colonnes[1:]) + [None]:
                    if tx is not None and tx - groupe[1] <= 6:  # (moins de 6 tuiles d'écart : la même zone)
                        groupe[1] = tx
                        continue
                    zones.append([(int(groupe[0]) - 3) * TUILE, y1, (int(groupe[1]) + 4) * TUILE, y2])
                    if tx is not None:
                        groupe = [tx, tx]
                debut = None
        ox, oy = origine
        connus = [(m.x - ox - 4, m.y - oy - 4, m.x - ox + m.l + 4, m.y - oy + m.h + 4)
                  for m in self.morceaux + self.perdus]
        for _ in range(2):
            for z in zones:
                for c in connus:
                    if se_touchent(z, c):
                        z[0], z[1], z[2], z[3] = min(z[0], c[0]), min(z[1], c[1]), max(z[2], c[2]), max(z[3], c[3])
        fusionnees = []
        for z in sorted(zones, key=lambda z: z[1]):
            z = [max(0, z[0]), max(0, z[1]), min(largeur, z[2]), min(hauteur, z[3])]
            if fusionnees and se_touchent(fusionnees[-1], z):
                f = fusionnees[-1]
                fusionnees[-1] = [min(f[0], z[0]), min(f[1], z[1]), max(f[2], z[2]), max(f[3], z[3])]
            else:
                fusionnees.append(z)
        return [tuple(z) for z in fusionnees if z[2] - z[0] >= 8 and z[3] - z[1] >= 8]

    # ---- défilement : les textes qui ont seulement glissé

    def _retrouver_decales(self, pixels, origine, stables):
        """Retrouve, décalés verticalement, les textes perdus. Renvoie (textes retrouvés à leur
        nouvelle place, textes perdus retrouvés, tuiles où il n'y a rien de nouveau à lire)."""
        aucun = ([], set(), np.zeros_like(stables))
        if not self.perdus or self.reference is None:
            return aucun
        votes = {}
        for m in sorted(self.perdus, key=lambda m: -m.l * m.h)[:8]:
            dy = self._chercher_decalage(m, pixels, origine)
            if dy:
                votes[dy] = votes.get(dy, 0) + 1
        decalages = sorted(votes, key=votes.get, reverse=True)[:3]
        if not decalages:
            return aucun
        ox, oy = origine
        retrouves, trouves = [], set()
        connus = [(k.x, k.y, k.x + k.l, k.y + k.h) for k in self.morceaux]
        for m in self.perdus:
            for dy in decalages:
                x1, y1 = m.x - ox, m.y - oy + dy
                cadre = (m.x, m.y + dy, m.x + m.l, m.y + dy + m.h)
                if any(surface_commune(cadre, c) >= 0.3 * m.l * m.h for c in connus):
                    trouves.add(m)  # un texte est déjà connu à cet endroit : pas de doublon
                    break
                if y1 >= 0 and y1 + m.h <= pixels.shape[0] and not self._a_change(m, pixels, x1, y1, x1 + m.l, y1 + m.h):
                    nouveau = replace(m, y=m.y + dy)
                    self.motifs[nouveau] = self.motifs[m]
                    self.origines[nouveau] = m  # ses décisions (pseudo, code…) le suivent
                    retrouves.append(nouveau)
                    trouves.add(m)
                    break
        expliquees = self._tuiles_decalees(pixels, stables, decalages[0])
        # Là où un texte perdu n'a pas été retrouvé, on relit (avant et après le décalage).
        for m in self.perdus:
            if m not in trouves:
                for y in (m.y - oy, m.y - oy + decalages[0]):
                    expliquees[max(0, y) // TUILE:max(0, y + m.h - 1) // TUILE + 1,
                               max(0, m.x - ox) // TUILE:max(0, m.x - ox + m.l - 1) // TUILE + 1] = False
        return retrouves, trouves, expliquees

    def _chercher_decalage(self, m, pixels, origine):
        """De combien de pixels (vers le haut ou le bas) le texte `m` a-t-il glissé ? None si introuvable."""
        motif = self.motifs.get(m)
        x1, y0 = m.x - origine[0], m.y - origine[1]
        if motif is None or x1 < 0 or x1 + m.l > pixels.shape[1] or len(motif) < 4:
            return None
        # Empreinte de chaque rangée de pixels (luminosité moyenne) sur la colonne du texte.
        empreinte = pixels[:, x1:x1 + m.l:3, :3].mean(axis=(1, 2))
        cherchee = motif[:, ::3].mean(axis=(1, 2))
        if len(empreinte) < len(cherchee):
            return None
        ecarts = np.abs(np.lib.stride_tricks.sliding_window_view(empreinte, len(cherchee)) - cherchee).mean(axis=1)
        for y in np.argsort(ecarts)[:5]:
            if ecarts[y] > 1.5:
                break
            y = int(y)
            if y != y0 and not self._a_change(m, pixels, x1, y, x1 + m.l, y + m.h):
                return y - y0
        return None

    def _tuiles_decalees(self, pixels, stables, dy):
        """Tuiles dont le contenu est celui d'avant, décalé de dy : rien de nouveau à y lire."""
        expliquees = np.zeros_like(stables)
        hauteur, largeur = pixels.shape[:2]
        for ty, tx in zip(*np.nonzero(stables)):
            y1, x1 = int(ty) * TUILE, int(tx) * TUILE
            y2, x2 = min(hauteur, y1 + TUILE), min(largeur, x1 + TUILE)
            if y1 - dy < 0 or y2 - dy > hauteur:
                continue
            ecarts = np.abs(pixels[y1:y2, x1:x2, :3].astype(np.int16)
                            - self.reference[y1 - dy:y2 - dy, x1:x2, :3]).sum(axis=2) > 30
            expliquees[ty, tx] = np.count_nonzero(ecarts) <= ecarts.size // 200
        return expliquees


# --------------------------------------------------------------------------- traduction

def paires_necessaires(langues, cible):
    """Modèles (de, vers) qu'il faut pour traduire ces langues vers la langue voulue : directs vers ou
    depuis l'anglais, sinon en passant par l'anglais."""
    paires = set()
    for de in langues:
        if de != cible:
            paires |= {(de, cible)} if "en" in (de, cible) else {(de, "en"), ("en", cible)}
    return paires


def paires_installees():
    return {(p.from_code, p.to_code) for p in argos.get_installed_packages()}


def langues_installees(cible):
    """Langues qu'on peut déjà traduire vers cette langue avec les modèles présents."""
    installees = paires_installees()
    return {code for code in NOM_PAR_CODE if code != cible and paires_necessaires({code}, cible) <= installees}


def grands_modeles_manquants(paires):
    """Noms des grands modèles utiles à ces paires de langues et absents (téléchargeables depuis GitHub), chacun
    une fois (un même modèle sert à plusieurs langues : espagnol, portugais, italien)."""
    return list(dict.fromkeys(GRANDS_MODELES[p] for p in sorted(paires) if p in GRANDS_MODELES and not any(
        os.path.exists(os.path.join(d, GRANDS_MODELES[p], "model.bin")) for d in DOSSIERS_MODELES)))


def installer_grand_modele(nom, progression, annule):
    """Télécharge un grand modèle (zip publié sur GitHub, ~200 Mo) dans le dossier des modèles. False si annulé."""
    if not DEPOT_GITHUB:
        return True
    os.makedirs(DOSSIER_TELECHARGEMENTS, exist_ok=True)
    chemin = os.path.join(DOSSIER_TELECHARGEMENTS, nom + ".zip")
    lien = f"https://github.com/{DEPOT_GITHUB}/releases/download/modeles/modele-{nom.replace('_', '-')}.zip"

    def avancer(lu, total):
        progression(f"Traduction de qualité : {lu // 2**20} / {total // 2**20} Mo" if total
                    else f"Traduction de qualité : {lu // 2**20} Mo", lu / total if total else 0.0)

    try:
        if not _telecharger([lien], chemin, avancer, annule):
            return False
        progression("Traduction de qualité : installation…", 1.0)
        dossier = os.path.join(APP_DIR, "modeles", nom)
        provisoire = dossier + ".nouveau"
        shutil.rmtree(provisoire, ignore_errors=True)
        with zipfile.ZipFile(chemin) as archive:
            archive.extractall(provisoire)
        if not os.path.exists(os.path.join(provisoire, "model.bin")):
            raise RuntimeError("archive du grand modèle incomplète")
        shutil.rmtree(dossier, ignore_errors=True)
        os.replace(provisoire, dossier)
        log.info("Grand modèle %s installé", nom)
        return True
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass


def carte_nvidia():
    """Une carte graphique NVIDIA (et son pilote) est-elle là ? Elle traduit ~10 fois plus vite que le processeur."""
    try:
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # (pas de pilote NVIDIA)
        return False


def bibliotheque_gpu_presente():
    return all(os.path.exists(os.path.join(DOSSIER_GPU, f)) for f in FICHIERS_GPU)


def preparer_carte_graphique():
    """Rend la bibliothèque de calcul de NVIDIA (téléchargée à part) trouvable par CTranslate2. True si elle est là."""
    if not bibliotheque_gpu_presente():
        return False
    if DOSSIER_GPU not in os.environ.get("PATH", ""):
        os.add_dll_directory(DOSSIER_GPU)
        os.environ["PATH"] = DOSSIER_GPU + os.pathsep + os.environ.get("PATH", "")
    return True


def installer_bibliotheque_gpu(progression, annule):
    """Télécharge la bibliothèque de calcul de NVIDIA (cuBLAS, ~530 Mo, depuis PyPI où NVIDIA la publie), vérifiée,
    et n'en garde que les deux fichiers utiles. Seulement s'il y a une carte NVIDIA. False si annulé."""
    nom, version = CUBLAS
    requete = urllib.request.Request(f"https://pypi.org/pypi/{nom}/{version}/json", headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(requete, timeout=15) as reponse:
        fichier = next(f for f in json.load(reponse)["urls"] if f["filename"].endswith("win_amd64.whl"))
    os.makedirs(DOSSIER_TELECHARGEMENTS, exist_ok=True)
    chemin = os.path.join(DOSSIER_TELECHARGEMENTS, fichier["filename"])

    def avancer(lu, total):
        progression(f"Carte graphique : {lu // 2**20} / {total // 2**20} Mo", lu / total if total else 0.0)

    try:
        if not _telecharger([fichier["url"]], chemin, avancer, annule):
            return False
        progression("Carte graphique : vérification…", 1.0)
        empreinte = hashlib.sha256()
        with open(chemin, "rb") as f:
            while bloc := f.read(1 << 20):
                empreinte.update(bloc)
        if empreinte.hexdigest() != fichier["digests"]["sha256"]:
            raise OSError("fichier de NVIDIA abîmé pendant le téléchargement")
        provisoire = DOSSIER_GPU + ".nouveau"
        shutil.rmtree(provisoire, ignore_errors=True)
        os.makedirs(provisoire)
        with zipfile.ZipFile(chemin) as archive:
            for nom_fichier in FICHIERS_GPU:
                with archive.open(f"nvidia/cublas/bin/{nom_fichier}") as source, \
                        open(os.path.join(provisoire, nom_fichier), "wb") as copie:
                    shutil.copyfileobj(source, copie, 1 << 20)
        shutil.rmtree(DOSSIER_GPU, ignore_errors=True)
        os.replace(provisoire, DOSSIER_GPU)
        log.info("Bibliothèque de la carte graphique installée (%s %s)", nom, version)
        return True
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass


def dossier_madlad():
    return os.path.join(APP_DIR, "modeles", MADLAD["dossier"])


def madlad_present():
    return all(os.path.exists(os.path.join(dossier_madlad(), f)) for f in MADLAD["fichiers"])


def installer_madlad(progression, annule):
    """Télécharge le grand modèle de traduction (~3 Go) depuis Hugging Face, fichier par fichier, vérifié (empreinte
    SHA-256 donnée par Hugging Face). False si annulé."""
    requete = urllib.request.Request(f"https://huggingface.co/api/models/{MADLAD['depot']}/tree/main",
                                     headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(requete, timeout=20) as reponse:
        empreintes = {f["path"]: (f.get("lfs") or {}).get("oid") for f in json.load(reponse)}
    provisoire = dossier_madlad() + ".nouveau"
    os.makedirs(provisoire, exist_ok=True)
    for i, nom in enumerate(MADLAD["fichiers"]):
        chemin = os.path.join(provisoire, nom)

        def avancer(lu, total, nom=nom):
            progression(f"Grand modèle de traduction : {lu // 2**20} / {total // 2**20} Mo" if total > 2**20
                        else f"Grand modèle de traduction : {nom}", lu / total if total else 0.0)

        if not _telecharger([f"https://huggingface.co/{MADLAD['depot']}/resolve/main/{nom}"], chemin, avancer, annule):
            return False
        if empreintes.get(nom):
            empreinte = hashlib.sha256()
            with open(chemin, "rb") as f:
                while bloc := f.read(1 << 20):
                    empreinte.update(bloc)
            if empreinte.hexdigest() != empreintes[nom]:
                raise OSError(f"{nom} abîmé pendant le téléchargement")
    shutil.rmtree(dossier_madlad(), ignore_errors=True)
    os.replace(provisoire, dossier_madlad())
    log.info("Grand modèle de traduction installé (%s)", MADLAD["depot"])
    return True


def numero_de_version(texte):
    """« v2.10.1 » -> (2, 10, 1) : comparable."""
    return tuple(int(n) for n in re.findall(r"\d+", texte)[:3])


def derniere_version():
    """(version, lien de l'installateur) de la dernière version publiée sur GitHub, ou None."""
    if not DEPOT_GITHUB:
        return None
    requete = urllib.request.Request(f"https://api.github.com/repos/{DEPOT_GITHUB}/releases/latest",
                                     headers={"User-Agent": APP_NAME, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(requete, timeout=15) as reponse:
        donnees = json.load(reponse)
    version = (donnees.get("tag_name") or "").lstrip("vV")
    lien = next((a.get("browser_download_url") for a in donnees.get("assets", [])
                 if (a.get("name") or "").endswith("-Installation.exe")), None)
    return (version, lien) if version and lien else None


def installer_modeles(paires, progression, annule):
    """Télécharge et installe les modèles Argos Translate de ces paires de langues, l'un après l'autre.
    progression(texte, fraction de l'ensemble) ; annule() vrai : on s'arrête (renvoie False).
    OSError : pas d'internet, téléchargement interrompu ; RuntimeError : modèle inexistant."""
    progression("Recherche des modèles…", 0.0)
    argos.update_package_index()  # (ses erreurs ne remontent pas : un index vide veut dire pas d'internet)
    try:
        disponibles = {(p.from_code, p.to_code): p for p in argos.get_available_packages()}
    except (OSError, ValueError):
        disponibles = {}
    if not disponibles:
        raise OSError("Liste des modèles introuvable : pas de connexion internet ?")
    introuvables = [p for p in paires if p not in disponibles]
    if introuvables:
        raise RuntimeError("Pas de modèle pour " + ", ".join(f"{de} → {vers}" for de, vers in introuvables))
    os.makedirs(DOSSIER_TELECHARGEMENTS, exist_ok=True)
    for reste in os.listdir(DOSSIER_TELECHARGEMENTS):  # restes d'un téléchargement coupé (PC éteint…)
        try:
            os.remove(os.path.join(DOSSIER_TELECHARGEMENTS, reste))
        except OSError:
            pass
    for numero, (de, vers) in enumerate(paires):
        nom = f"{NOM_PAR_CODE.get(de, de)} → {NOM_PAR_CODE.get(vers, vers)}"
        etape = f" ({numero + 1} sur {len(paires)})" if len(paires) > 1 else ""

        def avancer(lu, total, numero=numero, nom=nom, etape=etape):
            fraction = lu / total if total else 0.0
            taille = f"{lu // 2**20} / {total // 2**20} Mo" if total else f"{lu // 2**20} Mo"
            progression(f"{nom} : {taille}{etape}", (numero + fraction) / len(paires))

        chemin = os.path.join(DOSSIER_TELECHARGEMENTS, f"{de}_{vers}.argosmodel")
        try:
            if not _telecharger(disponibles[(de, vers)].links, chemin, avancer, annule):
                return False
            progression(f"{nom} : installation…{etape}", (numero + 1) / len(paires))
            argos.install_from_path(chemin)
            log.info("Modèle %s → %s installé", de, vers)
        finally:
            try:
                os.remove(chemin)  # le modèle est installé (ou pas) : le fichier téléchargé ne sert plus
            except OSError:
                pass
    return True


def _telecharger(liens, chemin, avancer, annule):
    """Enregistre le fichier du premier lien qui répond ; False si annulé."""
    erreur = OSError("Aucun lien de téléchargement")
    for lien in liens:
        if lien.startswith("http://"):
            lien = "https://" + lien[len("http://"):]  # (connexion chiffrée : un modèle ne peut pas être échangé en route)
        if not lien.startswith("https://"):
            continue  # (liens « ipfs:// » de l'index : illisibles sans logiciel spécial)
        # Le serveur des modèles refuse le nom de client par défaut de Python (erreur 403) : on se
        # présente comme Argos Translate lui-même.
        requete = urllib.request.Request(lien, headers={"User-Agent": "ArgosTranslate"})
        try:
            with urllib.request.urlopen(requete, timeout=30) as reponse, open(chemin, "wb") as fichier:
                total, lu = int(reponse.headers.get("Content-Length") or 0), 0
                while bloc := reponse.read(1 << 18):
                    if annule():
                        return False
                    fichier.write(bloc)
                    lu += len(bloc)
                    avancer(lu, total)
            if total and lu < total:
                raise OSError(f"Téléchargement interrompu ({lu} octets sur {total})")
            return True
        except (OSError, ValueError, HTTPException) as e:
            log.warning("Téléchargement depuis %s impossible : %s", lien, e)
            erreur = e if isinstance(e, OSError) else OSError(str(e))
    raise erreur


class DetecteurLangue:
    """Reconnaît la langue d'un texte parmi celles du programme (py3langid : 4 Mo et très rapide ; il
    remplace lingua, 290 Mo, en reconnaissant aussi bien les langues). Pas sûr entre plusieurs fils : le
    traducteur l'appelle sous son verrou."""

    AUTRES_CODES = {"nb": "no"}  # (le norvégien s'appelle « no » pour py3langid)

    def __init__(self):
        self.identifiant = LanguageIdentifier.from_model_file(MODELE_LANGID, norm_probs=True)
        self.identifiant.set_languages(sorted(self.AUTRES_CODES.get(c, c) for c in NOM_PAR_CODE))
        self.nos_codes = {v: k for k, v in self.AUTRES_CODES.items()}

    def confiances(self, texte):
        """{code de langue: probabilité (0 à 1)} pour ce texte."""
        return {self.nos_codes.get(code, code): float(p) for code, p in self.identifiant.rank(texte)}


class Traducteur:
    """Détecte la langue de chaque texte et traduit hors-ligne (modèles Argos Translate).

    Rien n'est envoyé sur internet. Seules les langues choisies sont traduites : leurs modèles
    (~100 Mo chacun) se téléchargent depuis le choix des langues (voir installer_modeles).
    """

    CONFIANCE_MIN = 0.2  # en dessous, on ne sait pas quelle langue c'est : on laisse tel quel
    CONFIANCE_MIN_HORS_ANGLAIS = 0.5  # plus exigeant : une langue rare n'est pas reconnue par erreur
    MOTS_MIN_HORS_ANGLAIS = 3  # un ou deux mots ne suffisent pas à reconnaître une langue rare
    CONFIANCE_DEJA_TRADUIT = 0.35  # le texte ressemble déjà assez à la langue voulue

    MOTS_PHRASE = 2  # à partir de ce nombre de mots, un texte va au grand modèle s'il existe
    VERSION_CACHE = 8  # à changer quand les modèles ou les règles changent : les anciennes traductions sont oubliées

    def __init__(self, langues=(), mots=()):
        self.detecteur = DetecteurLangue()
        self.langues = frozenset(langues)  # langues d'origine choisies : les autres restent telles quelles
        self.mots = MesMots(mots)  # « Mes mots » : jamais traduits, ou traduction imposée
        self.langues_vues = {}  # langue pas choisie -> nombre de textes vus à l'écran dans cette langue
        self.generation = 0  # augmente quand les langues ou les modèles changent
        self.installees = None  # paires de langues dont le modèle est installé (relu après un téléchargement)
        self.cache = {}  # (texte, cible) -> traduction, ou None si rien à traduire
        self.cache_modifie = False
        self.modeles = {}
        self.grands = {}  # (de, vers) -> grand modèle (traducteur, découpeur source, découpeur cible) ou None
        self.paires_impossibles = set()
        self.echecs = {}  # (source, cible) -> instant du dernier échec passager
        self.japonais_vu_le = -1e9  # dernier texte japonais rencontré (hiragana, katakana)
        self.verrou = threading.RLock()  # la traduction peut être demandée par plusieurs fils
        self.appareil = "cpu"  # "cuda" : carte graphique NVIDIA, une fois l'essai réussi (essayer_carte_graphique)
        self.multilingue_permis = False  # réglage « Grand modèle de traduction »
        self.multilingue = None  # MADLAD chargé (traducteur, découpeur) ; None : pas encore essayé ; False : inutilisable
        self._charger_cache()

    def _madlad(self):
        """Le grand modèle de traduction (MADLAD) s'il est permis (réglage), présent et la carte graphique utilisée
        (sur le processeur, il serait bien trop lent) : (traducteur, découpeur), sinon None."""
        if not getattr(self, "multilingue_permis", False) or self.appareil != "cuda" or not madlad_present():
            return None
        if self.multilingue is None:
            try:
                self.multilingue = (
                    ctranslate2.Translator(dossier_madlad(), device="cuda", compute_type="int8_float16"),
                    sentencepiece.SentencePieceProcessor(model_file=os.path.join(dossier_madlad(), "spiece.model")))
                log.info("Grand modèle de traduction chargé sur la carte graphique")
            except Exception:
                log.exception("Grand modèle de traduction inutilisable")
                self.multilingue = False
        return self.multilingue or None

    def oublier_madlad(self):
        """(Réglage coupé, carte graphique abandonnée.) Le grand modèle libère la carte graphique."""
        with self.verrou:
            self.multilingue = None
            self.cache = {}  # (ce qu'il a traduit est retraduit avec les modèles habituels)
            self.generation += 1

    def _creer(self, chemin):
        """Un modèle chargé sur la carte graphique si elle est utilisée (sinon, ou si elle n'a plus de place, sur
        le processeur)."""
        if self.appareil == "cuda":
            try:
                return ctranslate2.Translator(chemin, device="cuda", compute_type="int8_float16")
            except Exception as e:
                log.warning("Modèle chargé sur le processeur (carte graphique : %s)", e)
        return ctranslate2.Translator(chemin, device="cpu", compute_type="int8",
                                      intra_threads=min(4, max(1, (os.cpu_count() or 2) // 2)))

    def essayer_carte_graphique(self, delai=90):
        """(Fil à part.) Passe les traductions sur la carte graphique NVIDIA si elle marche vraiment : un essai sur un
        modèle, `delai` secondes au plus (la toute première fois, la carte prépare son code de calcul). Sinon tout
        reste sur le processeur. Renvoie un message d'état (affiché dans « Options »)."""
        if self.appareil == "cuda":
            return "Active"
        if not carte_nvidia():
            return "Pas de carte NVIDIA"
        if not preparer_carte_graphique():
            return "Bibliothèque de NVIDIA pas encore téléchargée"
        modeles = [str(p.package_path / "model") for p in argos.get_installed_packages()]
        if not modeles:
            return "Aucun modèle de traduction à essayer"
        resultat = {}

        def essai():
            try:
                debut = time.monotonic()
                modele = ctranslate2.Translator(modeles[0], device="cuda", compute_type="int8_float16")
                modele.translate_batch([["▁Hello", "▁world", "."]] * 4, beam_size=2)
                resultat["duree"] = time.monotonic() - debut
            except Exception as e:
                resultat["erreur"] = str(e)

        fil = threading.Thread(target=essai, name="essai_carte_graphique", daemon=True)
        fil.start()
        fil.join(delai)
        if "duree" not in resultat:
            raison = resultat.get("erreur") or f"pas de réponse en {delai} s"
            log.warning("Carte graphique inutilisable, traduction sur le processeur : %s", raison)
            return "Inutilisable : " + raison[:80]
        with self.verrou:
            self.appareil = "cuda"
            self.modeles.clear()  # rechargés sur la carte à leur prochaine traduction
            self.grands.clear()
        log.info("Traduction sur la carte graphique NVIDIA (essai en %.1f s)", resultat["duree"])
        return "Active"

    def connu(self, texte, cible):
        return (texte, cible) in self.cache

    def prechauffer(self, cible):
        """(Fil à part, à l'activation.) Charge tout de suite les modèles de l'anglais (le plus courant à l'écran)
        vers la langue voulue, et les mots courants : pendant la première lecture de l'écran, plutôt qu'au
        moment où la première traduction les attend. Les autres langues se chargent quand on les rencontre."""
        try:
            frequence_mot("the", "en")
            frequence_mot("le", cible)
            if "en" in self.langues and cible != "en":
                with self.verrou:
                    for de, vers in self._chemin("en", cible):
                        self._modele(de, vers)
                        self._grand_modele(de, vers)
        except Exception:  # (modèle absent, téléchargement en cours…) : il se chargera à la demande
            log.debug("Préchargement des modèles impossible", exc_info=True)

    def changer_langues(self, langues):
        """Nouvelles langues choisies, ou modèles tout juste installés : ce qui avait été laissé tel quel
        (langue pas choisie, modèle absent) est revu."""
        with self.verrou:
            self.generation += 1  # (les pages déjà traduites par l'extension redemandent ce qu'elles avaient laissé)
            self.langues = frozenset(langues)
            self.installees = None
            self.cache = {cle: traduit for cle, traduit in self.cache.items() if traduit is not None}
            self.paires_impossibles.clear()
            self.echecs.clear()
            self.langues_vues = {code: n for code, n in self.langues_vues.items() if code not in self.langues}

    def traduire_ecrit(self, texte, de, vers):
        """Ce qu'on écrit (Ctrl+Alt+Entrée), de sa propre langue vers une autre : pas de détection de la
        langue d'origine, on sait dans quelle langue on écrit. Ligne par ligne, phrase par phrase.
        None si rien à traduire ; RuntimeError si le modèle manque."""
        if de == vers:
            return None
        with self.verrou:
            parties = decouper_texte(texte)
            morceaux = list(dict.fromkeys(p.strip() for p in parties[::2] if p.strip() and A_TRADUIRE.search(p)))
            if not morceaux:
                return None
            # (Les nombres protégés : « 21h » ne doit pas devenir « 9:00 ».)
            resultats = self._traduire_avec_reperes(morceaux, de, vers, self.mots, chiffres=True)
            traduits = {m: retoucher_francais(r) if vers == "fr" else r
                        for m, r in zip(morceaux, resultats) if r and not traduction_aberrante(r)}
            if not traduits:
                return None
            return "".join(p.replace(p.strip(), traduits[p.strip()], 1) if i % 2 == 0 and traduits.get(p.strip()) else p
                           for i, p in enumerate(parties))

    def deja_en(self, texte, langue):
        """Le texte est-il déjà dans cette langue (on ne le traduit pas une deuxième fois) ?"""
        with self.verrou:
            confiances = self.detecteur.confiances(texte)
        return confiances.get(langue, 0) >= 0.5

    def changer_mots(self, lignes):
        """« Mes mots » modifiés : tout est retraduit avec les nouvelles règles."""
        with self.verrou:
            self.mots = MesMots(lignes)
            self.cache = {}
            self.cache_modifie = True
            self.generation += 1  # (les pages traduites par l'extension se retraduisent)

    # ---- traductions gardées sur le disque : une page déjà vue est traduite tout de suite,
    # même après un redémarrage

    def _charger_cache(self):
        try:
            with open(CACHE_TRADUCTIONS, encoding="utf-8") as fichier:
                donnees = json.load(fichier)
            if donnees.get("version") == self.VERSION_CACHE:
                for cible, texte, traduit in donnees["traductions"]:
                    self.cache[(texte, cible)] = traduit
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def sauver(self):
        with self.verrou:
            if not self.cache_modifie:
                return
            self.cache_modifie = False
            if len(self.cache) > 60_000:  # (une longue soirée sur un chat : la mémoire ne grossit pas sans fin)
                self.cache = dict(list(self.cache.items())[-30_000:])
            entrees = [(cible, texte, traduit) for (texte, cible), traduit in self.cache.items() if traduit]
        try:
            provisoire = CACHE_TRADUCTIONS + ".tmp"
            with open(provisoire, "w", encoding="utf-8") as fichier:
                json.dump({"version": self.VERSION_CACHE, "traductions": entrees[-30_000:]}, fichier,
                          ensure_ascii=False)
            os.replace(provisoire, CACHE_TRADUCTIONS)
        except OSError:
            log.exception("Impossible d'enregistrer les traductions")

    def traduire(self, textes, cible, filtre=None):
        """Renvoie, pour chaque texte, sa traduction ou None s'il ne faut pas le traduire.

        Un texte long ou sur plusieurs lignes est traduit morceau par morceau (lignes, puis phrases) :
        chaque morceau a sa propre langue et ses propres vérifications, et un morceau qu'on ne sait pas
        traduire (numéros de version, langue pas choisie, modèle qui s'emballe) reste tel quel sans
        empêcher les autres. `filtre(morceau)` : ce morceau est-il à traduire (pseudos, code…) ?"""
        accepte = (lambda t: True) if filtre is None else filtre
        with self.verrou:
            uniques = list(dict.fromkeys(textes))
            decoupes = {t: decouper_texte(t) for t in uniques if (t, cible) not in self.cache}
            decoupes = {t: parties for t, parties in decoupes.items() if len(parties) > 1}
            simples = [t for t in uniques if t not in decoupes and accepte(t)]
            if simples:
                self._traduire(simples, cible)
            if decoupes:
                morceaux = list(dict.fromkeys(p.strip() for parties in decoupes.values() for p in parties[::2]
                                              if p.strip() and A_TRADUIRE.search(p) and accepte(p.strip())))
                traduits = dict(zip(morceaux, self._traduire(morceaux, cible))) if morceaux else {}
                for texte, parties in decoupes.items():
                    # Un morceau sauté (échec passager, réessayé dans 30 s) : le texte entier n'est pas mémorisé,
                    # sinon il resterait à moitié traduit (ou pas du tout) pour de bon.
                    if any((p.strip(), cible) not in self.cache for p in parties[::2] if p.strip() in traduits):
                        continue
                    resultat = "".join(
                        p.replace(p.strip(), traduits[p.strip()], 1) if i % 2 == 0 and traduits.get(p.strip()) else p
                        for i, p in enumerate(parties))
                    self.cache[(texte, cible)] = resultat if resultat != texte else None
                    self.cache_modifie = True
            return [self.cache.get((t, cible)) if t in decoupes or accepte(t) else None for t in textes]

    def _traduire(self, textes, cible):
        a_faire = {}  # langue source -> textes
        for texte in dict.fromkeys(textes):
            if (texte, cible) in self.cache:
                continue
            entier = self.mots.texte_entier(texte)
            if entier is not False:  # le texte entier est un de « Mes mots »
                self.cache[(texte, cible)] = entier
                continue
            try:
                # Un libellé du glossaire est de l'anglais, même trop court ou ambigu pour le détecteur (« Logout »,
                # « Invite Me »).
                source = "en" if cible == "fr" and "en" in self.langues and libelle_du_glossaire(texte) \
                    else self._langue_a_traduire(self._normal(texte), cible)
            except Exception:  # un texte qui fait échouer la détection ne bloque pas les autres
                log.exception("Langue de « %s » introuvable", texte[:80])
                source = None
            if source is None:
                self.cache[(texte, cible)] = None
            elif source == "en" and cible == "fr" and libelle_du_glossaire(texte) \
                    and not (self.mots.motif and self.mots.motif.search(texte)):
                self.cache[(texte, cible)] = libelle_du_glossaire(texte)  # libellé courant : toujours le même terme
                self.cache_modifie = True
            else:
                a_faire.setdefault(source, []).append(texte)

        for source, lot in a_faire.items():
            if time.monotonic() - self.echecs.get((source, cible), -1e9) < 30:
                continue  # échec il y a moins de 30 s : on ne réessaie pas tout de suite
            try:
                resultats = self._traduire_avec_reperes([self._normal(t) for t in lot], source, cible, self.mots,
                                                        argot=source == "en")
            except RuntimeError:  # aucun modèle pour ces langues : inutile de réessayer
                log.exception("Traduction %s → %s impossible", source, cible)
                self.paires_impossibles.add((source, cible))
                resultats = [None] * len(lot)
            except Exception:  # erreur passagère (internet coupé pendant un téléchargement…)
                log.exception("Traduction %s → %s en échec, elle sera retentée", source, cible)
                self.echecs[(source, cible)] = time.monotonic()
                continue  # rien n'est gardé en mémoire : ces textes seront retraduits plus tard
            # Un nombre changé (« 4 PM » -> « 16h ») : on retraduit en protégeant les nombres.
            nombres_changes = [i for i, (texte, traduit) in enumerate(zip(lot, resultats))
                               if traduit and not memes_chiffres(texte, traduit)]
            if nombres_changes:
                try:
                    for i, traduit in zip(nombres_changes, self._traduire_avec_reperes(
                            [self._normal(lot[i]) for i in nombres_changes], source, cible, self.mots, chiffres=True,
                            argot=source == "en")):
                        resultats[i] = traduit
                except Exception:
                    log.exception("Nouvel essai avec les nombres protégés impossible")
            # Un libellé avec une majuscule à chaque mot recopié tel quel (« Help Desk », « World Store » : pris pour
            # un nom de marque) : retraduit écrit comme une phrase (« Help desk » -> « Service d'assistance »).
            recopies = [i for i, (texte, traduit) in enumerate(zip(lot, resultats))
                        if traduit and traduit.casefold() == texte.casefold() and 2 <= len(texte.split()) <= 4
                        and texte.istitle()]
            if recopies:
                try:
                    for i, traduit in zip(recopies, self._traduire_avec_reperes(
                            [" ".join([lot[i].split()[0]] + [m.lower() for m in lot[i].split()[1:]]) for i in recopies],
                            source, cible, self.mots, argot=source == "en")):
                        resultats[i] = traduit
                except Exception:
                    log.exception("Nouvel essai en minuscules impossible")
            for texte, traduit in zip(lot, resultats):
                if traduit and not memes_chiffres(texte, traduit):
                    traduit = None  # la traduction a changé un nombre : on garde le texte d'origine
                if traduit and len(texte.split()) == 1 and not CJK.search(texte) and len(traduit.split()) > 4:
                    log.info("Traduction inventée écartée : %s -> %s", texte[:40], traduit[:80])
                    traduit = None  # un seul mot devenu toute une phrase : le modèle a inventé
                if traduit and traduction_aberrante(traduit):
                    log.info("Traduction aberrante écartée : %s", traduit[:80])
                    traduit = None  # le modèle s'est emballé : mieux vaut le texte d'origine
                if traduit and cible == "fr":
                    traduit = retoucher_francais(traduit)
                if traduit and self._en_capitales(texte):
                    traduit = traduit.upper()  # QUICK STATS → STATISTIQUES RAPIDES
                inutile = not traduit or traduit.casefold() == texte.casefold()
                self.cache[(texte, cible)] = None if inutile else traduit
                self.cache_modifie = True
        return [self.cache.get((t, cible)) for t in textes]

    def _traduire_avec_reperes(self, textes, de, vers, mots, chiffres=False, argot=False):
        """Traduit en protégeant par des repères le code cité, « Mes mots » (et les nombres). Si le modèle perd
        un repère (« The [A] avatar » -> « L'avatar »), ce texte est retraduit en ne protégeant que le code.
        Une traduction (repères et caractères inconnus remis) ou None par texte. RuntimeError : pas de modèle."""
        chemin = self._chemin(de, vers)

        def passe(textes, mots, chiffres):
            prepares = [proteger_code(t, mots, chiffres) for t in textes]
            resultats = [developper_argot(p) if argot else p for p, _ in prepares]
            for d, v in chemin:
                # Étape par l'anglais : un mot inconnu (« ⁇ ») y fausserait toute la suite.
                resultats = self._traduire_lot(resultats, d, v, sans_inconnus=v != vers)
            sortie = []
            for (protege, codes), traduit in zip(prepares, resultats):
                if traduit:
                    traduit = remettre_inconnus(protege, traduit)
                if traduit:
                    traduit = remettre_code(traduit, codes)
                sortie.append(traduit)
            return sortie

        sortie = passe(textes, mots, chiffres)
        a_refaire = [i for i, traduit in enumerate(sortie) if traduit is None] if (mots and mots.motif) or chiffres else []
        if a_refaire:
            for i, traduit in zip(a_refaire, passe([textes[i] for i in a_refaire], None, False)):
                sortie[i] = traduit
        # Le modèle a perdu le repère d'un code cité : chaque bout de phrase entre deux codes est traduit à part,
        # le code reste entre eux (plutôt que toute la phrase en anglais).
        for i, texte in enumerate(textes):
            if sortie[i] is None and CODE_CITE.search(texte):
                bouts = CODE_CITE.split(texte)
                prose = [b.strip() for b in bouts if b.strip() and A_TRADUIRE.search(b)]
                traduits = dict(zip(prose, passe(prose, None, False))) if prose else {}
                if prose and all(traduits.get(p) for p in prose):
                    codes = CODE_CITE.findall(texte)
                    sortie[i] = "".join(
                        (b.replace(b.strip(), traduits[b.strip()], 1) if b.strip() in traduits else b)
                        + (codes[n].strip(MARQUE_CODE) if n < len(codes) else "") for n, b in enumerate(bouts))
        return sortie

    @staticmethod
    def _en_capitales(texte):
        """Texte tout en majuscules (« QUICK STATS ») ? Pas une phrase japonaise qui cite « UV »."""
        return texte.isupper() and not CJK.search(texte)

    @classmethod
    def _normal(cls, texte):
        """Le détecteur et les modèles comprennent mal les textes tout en majuscules."""
        return texte.capitalize() if cls._en_capitales(texte) else texte

    def _langue_a_traduire(self, texte, cible):
        """La langue d'origine du texte s'il faut le traduire, sinon None (déjà dans la langue voulue,
        langue incertaine, pas choisie, ou sans modèle)."""
        if texte.strip().casefold() in NOMS_DE_MARQUES:
            return None  # « Unity » seul est le logiciel, pas « l'unité »
        source = self._langue(texte, cible)
        if source is None or (source, cible) in self.paires_impossibles:
            return None
        if source not in self.langues:
            self.langues_vues[source] = self.langues_vues.get(source, 0) + 1  # proposée dans les réglages
            return None
        return source

    def _langue(self, texte, cible):
        # Un à trois mots : trop court pour le détecteur de langue. On regarde plutôt si ce
        # sont des mots courants en anglais et pas dans la langue voulue (« Beginner », « Sign in »).
        mots = [m.lower() for m in re.findall(r"[^\W\d_]+", texte)]
        if 1 <= len(mots) <= 12 and cible != "en":
            # Mots courants en anglais, et bien moins dans la langue voulue (« Beginner », « Sign in »,
            # « 3D Animation data of the Lets Groove Dance ») : de l'anglais, même si quelques mots
            # existent aussi dans la langue voulue (« animation », « data »).
            anglais = sum(frequence_mot(m, "en") for m in mots) / len(mots)
            voulu = sum(frequence_mot(m, cible) for m in mots) / len(mots)
            if anglais >= 3.0 and anglais - voulu >= (0.8 if len(mots) <= 3 else 1.2):
                return "en"
            # Un libellé de deux ou trois mots dont l'un est nettement anglais, les autres existant aussi dans la
            # langue voulue (« World Info », « Public Link » : « info », « public » sont aussi français).
            if 2 <= len(mots) <= 3 and anglais > voulu and any(
                    frequence_mot(m, "en") - frequence_mot(m, cible) >= 1.0 for m in mots):
                return "en"
        # Japonais et coréen se reconnaissent à leur écriture, même en un seul mot (« 利用規約 » seul
        # est en caractères chinois, partagés par les deux langues : le détecteur tranche).
        lettres = len(re.findall(r"[^\W\d_]", texte))
        for ecriture, code in ((KANA, "ja"), (HANGUL, "ko")):
            if code != cible and lettres and len(ecriture.findall(texte)) >= max(1, 0.2 * lettres):
                if code == "ja":
                    self.japonais_vu_le = time.monotonic()
                return code
        confiances = self.detecteur.confiances(texte)
        if not confiances:
            return None
        source, confiance = max(confiances.items(), key=lambda kv: kv[1])
        if source == "zh" and time.monotonic() - self.japonais_vu_le < 120 and cible != "ja" \
                and len(CJK.findall(texte)) <= 10:
            # Quelques caractères chinois seuls (« 利用規約 ») au milieu de japonais : du japonais. (Une vraie
            # phrase japonaise a presque toujours des kana : une longue phrase sans kana est du chinois.)
            source, confiance = "ja", max(confiance, confiances.get("ja", 0))
        if source == cible or confiances.get(cible, 0) >= self.CONFIANCE_DEJA_TRADUIT:
            return None
        if source == "en":
            assez_sur = confiance >= self.CONFIANCE_MIN
        else:
            # Sans espaces entre les mots (chinois, japonais), deux caractères valent à peu près un mot.
            mots = len(texte.split()) + len(CJK.findall(texte)) / 2
            assez_sur = confiance >= self.CONFIANCE_MIN_HORS_ANGLAIS and mots >= self.MOTS_MIN_HORS_ANGLAIS - (
                1 if CJK.search(texte) else 0)
        return source if assez_sur else None

    def _chemin(self, source, cible):
        if self.installees is None:
            self.installees = paires_installees()
        installees = self.installees
        # Grand modèle de traduction (réglage) : directement, de toute langue sauf l'anglais.
        if source != "en" and self._madlad():
            return [(source, cible)]
        if (source, cible) in installees:
            return [(source, cible)]
        # Un grand modèle direct (espagnol -> français…) : meilleur que deux traductions à la suite.
        if (source, cible) in GRANDS_MODELES and self._grand_modele(source, cible):
            return [(source, cible)]
        # Pas de modèle direct : on passe par l'anglais.
        if "en" not in (source, cible) and {(source, "en"), ("en", cible)} <= installees:
            return [(source, "en"), ("en", cible)]
        raise RuntimeError(f"Aucun modèle de traduction {source} → {cible} (pas encore téléchargé ?)")

    def _modele(self, de, vers):
        if (de, vers) not in self.modeles:
            paquet = next(
                p for p in argos.get_installed_packages()
                if p.from_code == de and p.to_code == vers
            )
            # Le découpeur de texte dépend du modèle (SentencePiece ou BPE) : celui d'Argos gère les deux.
            self.modeles[(de, vers)] = (self._creer(str(paquet.package_path / "model")), paquet.tokenizer)
        return self.modeles[(de, vers)]

    def _grand_modele(self, de, vers):
        """Le grand modèle de cette paire de langues s'il est présent (OPUS-MT « transformer-big »,
        bien meilleur sur les phrases) : (traducteur, découpeur source, découpeur cible), ou None."""
        if (de, vers) not in self.grands:
            self.grands[(de, vers)] = None
            nom = GRANDS_MODELES.get((de, vers))
            for dossier in DOSSIERS_MODELES:
                chemin = os.path.join(dossier, nom) if nom else ""
                if nom and os.path.exists(os.path.join(chemin, "model.bin")):
                    try:
                        self.grands[(de, vers)] = (
                            self._creer(chemin),
                            sentencepiece.SentencePieceProcessor(model_file=os.path.join(chemin, "source.spm")),
                            sentencepiece.SentencePieceProcessor(model_file=os.path.join(chemin, "target.spm")),
                        )
                        log.info("Grand modèle de traduction %s → %s chargé", de, vers)
                    except Exception:
                        log.exception("Grand modèle %s → %s inutilisable", de, vers)
                    break
        return self.grands[(de, vers)]

    def _traduire_lot(self, textes, de, vers, sans_inconnus=False):
        # Les longs paragraphes sont traduits phrase par phrase : meilleure qualité.
        phrases, nombres = [], []
        for texte in textes:
            longueur = len(texte) + len(CJK.findall(texte)) * 2  # un caractère chinois vaut un mot
            morceaux = [m for m in DECOUPE_PHRASES.split(texte) if m] if longueur > 200 else [texte]
            phrases += morceaux
            nombres.append(len(morceaux))
        traduites = [None] * len(phrases)
        madlad = self._madlad() if de != "en" else None
        if madlad:  # (la langue voulue est indiquée en tête : « <2fr> »)
            modele, decoupeur = madlad
            resultats = modele.translate_batch(
                [decoupeur.encode(f"<2{vers}> " + unicodedata.normalize("NFKC", p), out_type=str) + ["</s>"]
                 for p in phrases], beam_size=2, max_batch_size=32, max_decoding_length=256, no_repeat_ngram_size=4)
            traduites = [" ".join(decoupeur.decode(r.hypotheses[0]).split()) for r in resultats]
            sortie, debut = [], 0
            for nombre in nombres:
                sortie.append(" ".join(traduites[debut:debut + nombre]))
                debut += nombre
            return sortie
        # Le grand modèle est meilleur dès deux mots (« Se connecter », « Marquer comme lu »), mais pas sur
        # un mot seul (« Wishlist » gardé, « Followers » -> « Suiveurs ») : ceux-là restent au modèle habituel.
        # (Les libellés où il se trompe encore, « Add to cart », sont dans GLOSSAIRE_INTERFACE_FR.)
        # (Chinois, japonais : pas d'espaces, deux caractères valent à peu près un mot. Un grand modèle direct, sans
        # modèle habituel pour cette paire, traduit tout.)
        grand = self._grand_modele(de, vers)
        seul = grand and (de, vers) not in (self.installees or ())
        pour_grand = [i for i, p in enumerate(phrases)
                      if grand and (seul or len(p.split()) + len(CJK.findall(p)) / 2 >= self.MOTS_PHRASE)]
        pour_petit = sorted(set(range(len(phrases))) - set(pour_grand))
        if pour_grand:
            modele, source, cible = grand
            jeton = [JETONS_CIBLE[vers]] if GRANDS_MODELES.get((de, vers)) in MODELES_MULTI_CIBLES else []
            resultats = modele.translate_batch(
                [jeton + source.encode(unicodedata.normalize("NFKC", phrases[i]), out_type=str) for i in pour_grand],
                beam_size=2, max_batch_size=32, max_decoding_length=256, disable_unk=sans_inconnus,
                no_repeat_ngram_size=4,  # pas de boucle « un gonflement d'un gonflement d'un… »
            )
            for i, r in zip(pour_grand, resultats):
                traduites[i] = " ".join(cible.decode(r.hypotheses[0]).split())
        if pour_petit:
            modele, decoupeur = self._modele(de, vers)
            resultats = modele.translate_batch(
                [decoupeur.encode(phrases[i]) for i in pour_petit],
                beam_size=2, max_batch_size=32, max_decoding_length=256, disable_unk=sans_inconnus,
                no_repeat_ngram_size=4,
            )
            for i, r in zip(pour_petit, resultats):
                traduites[i] = " ".join(decoupeur.decode(r.hypotheses[0]).split())
        sortie, debut = [], 0
        for nombre in nombres:
            sortie.append(" ".join(traduites[debut:debut + nombre]))
            debut += nombre
        return sortie


# --------------------------------------------------------------------------- IA locale
#
# « Qualité maximale » (réglage) : une IA sur ce PC retraduit en arrière-plan ce que les modèles de traduction ont déjà
# traduit (les phrases). Elle rend le sens et les expressions (« Break a leg » n'est plus « Cassez une jambe » mais
# « Bonne chance ») ; ses traductions remplacent les premières dès qu'elles sont prêtes. Rien ne part sur internet.
# Elle tourne dans llama.cpp (le moteur dont Ollama se sert lui-même), téléchargé par le programme avec elle : rien à
# installer. Carte graphique seulement (NVIDIA, AMD ou Intel, par Vulkan) : sur le processeur, plusieurs secondes par
# phrase.

DOSSIER_IA = os.path.join(APP_DIR, "ia")
# Moteur : llama-server, version Vulkan (30 Mo, toutes les cartes graphiques), publiée par les auteurs de llama.cpp sur
# GitHub ; version figée, empreinte vérifiée. Mesuré sur une RTX 5070 Ti : ~0,2 s par phrase, 2,8 Go sur la carte.
MOTEUR_IA = {"version": "b11617", "fichier": "llama-b11617-bin-win-vulkan-x64.zip",
             "sha256": "540649b52663e8c9c94613288824dc4c96dfb7b226d2ec2507dfd1279dc4c60c"}
# TranslateGemma 4B (Google, fait pour traduire ; conditions d'utilisation de Gemma) : comparé à TranslateGemma 12B et à
# Qwen3 8B sur des expressions, de l'argot, des pages de boutique (japonais, chinois) : presque aussi bon que le 12B,
# meilleur que Qwen3 (qui inventait : « Ce mod ne fonctionne pas »), et il laisse de la place à un jeu (VRChat) sur la
# carte. Au format GGUF, compressé « Q4_K_M » comme celui d'Ollama (2,4 Go), copie libre d'accès de mradermacher sur
# Hugging Face (celle de Google demande un compte) ; version figée, empreinte vérifiée.
# Les boutons courts restent aux modèles habituels : toutes ces IA en faisaient « Projet ouvert » (Open Project).
MODELE_IA = {"depot": "mradermacher/translategemma-4b-it-GGUF", "revision": "d37868f759d2f817a43d28dd80cf9f982ffa62fd",
             "fichier": "translategemma-4b-it.Q4_K_M.gguf",
             "sha256": "81200d03e843d2ec1ece6eeafe7d13cb6e5211e1fcd336ade55790b683a08330"}
MEMOIRE_IA = 4000  # Mo au moins sur la carte graphique (l'IA en prend 2,8)
VEILLE_IA = 15 * 60  # s sans rien à retraduire : le moteur s'arrête et rend la mémoire de la carte (à un jeu)
MOTS_IA = 4  # à partir de ce nombre de mots (deux caractères chinois ou japonais valent un mot), l'IA retraduit
NOMS_ANGLAIS = {"fr": "French", "en": "English", "es": "Spanish", "de": "German", "it": "Italian", "pt": "Portuguese",
                "nl": "Dutch", "pl": "Polish", "ro": "Romanian", "sv": "Swedish", "tr": "Turkish", "el": "Greek",
                "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic", "hi": "Hindi", "zh": "Chinese", "ja": "Japanese",
                "ko": "Korean", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "cs": "Czech", "hu": "Hungarian",
                "da": "Danish", "nb": "Norwegian", "fi": "Finnish", "bg": "Bulgarian", "he": "Hebrew", "fa": "Persian"}


def moteur_ia():
    return os.path.join(DOSSIER_IA, "moteur", "llama-server.exe")


def chemin_modele_ia():
    return os.path.join(DOSSIER_IA, MODELE_IA["fichier"])


def ia_presente():
    """Le moteur et le modèle de l'IA sont-ils téléchargés ?"""
    return os.path.exists(moteur_ia()) and os.path.exists(chemin_modele_ia())


def empreinte_sha256(chemin):
    empreinte = hashlib.sha256()
    with open(chemin, "rb") as f:
        while bloc := f.read(1 << 20):
            empreinte.update(bloc)
    return empreinte.hexdigest()


def installer_moteur_ia(progression, annule):
    """Télécharge le moteur de l'IA (llama.cpp, 30 Mo, depuis GitHub), vérifié, et n'en garde que le serveur et ses
    bibliothèques. False si annulé."""
    lien = f"https://github.com/ggml-org/llama.cpp/releases/download/{MOTEUR_IA['version']}/{MOTEUR_IA['fichier']}"
    os.makedirs(DOSSIER_TELECHARGEMENTS, exist_ok=True)
    chemin = os.path.join(DOSSIER_TELECHARGEMENTS, MOTEUR_IA["fichier"])

    def avancer(lu, total):
        progression(f"IA locale (moteur) : {lu // 2**20} / {total // 2**20} Mo", lu / total if total else 0.0)

    try:
        if not _telecharger([lien], chemin, avancer, annule):
            return False
        progression("IA locale (moteur) : vérification…", 1.0)
        if empreinte_sha256(chemin) != MOTEUR_IA["sha256"]:
            raise OSError("moteur de l'IA abîmé pendant le téléchargement")
        dossier = os.path.dirname(moteur_ia())
        provisoire = dossier + ".nouveau"
        shutil.rmtree(provisoire, ignore_errors=True)
        with zipfile.ZipFile(chemin) as archive:
            for nom in archive.namelist():
                if nom.endswith(".dll") or nom == "llama-server.exe" or nom.startswith("LICENSE"):
                    archive.extract(nom, provisoire)
        if not os.path.exists(os.path.join(provisoire, "llama-server.exe")):
            raise RuntimeError("archive du moteur de l'IA incomplète")
        shutil.rmtree(dossier, ignore_errors=True)
        os.replace(provisoire, dossier)
        log.info("Moteur de l'IA installé (llama.cpp %s)", MOTEUR_IA["version"])
        return True
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass


def installer_modele_ia(progression, annule):
    """Télécharge le modèle de l'IA (TranslateGemma 4B, 2,4 Go, depuis Hugging Face), vérifié. False si annulé."""
    lien = f"https://huggingface.co/{MODELE_IA['depot']}/resolve/{MODELE_IA['revision']}/{MODELE_IA['fichier']}"
    os.makedirs(DOSSIER_TELECHARGEMENTS, exist_ok=True)
    chemin = os.path.join(DOSSIER_TELECHARGEMENTS, MODELE_IA["fichier"])

    def avancer(lu, total):
        progression(f"IA locale : {lu // 2**20} / {total // 2**20} Mo", lu / total if total else 0.0)

    try:
        if not _telecharger([lien], chemin, avancer, annule):
            return False
        progression("IA locale : vérification…", 1.0)
        if empreinte_sha256(chemin) != MODELE_IA["sha256"]:
            raise OSError("modèle de l'IA abîmé pendant le téléchargement")
        os.makedirs(DOSSIER_IA, exist_ok=True)
        os.replace(chemin, chemin_modele_ia())
        log.info("Modèle de l'IA installé (%s)", MODELE_IA["fichier"])
        return True
    finally:
        try:
            os.remove(chemin)
        except OSError:
            pass


def choisir_carte_ia(liste):
    """D'après la liste des cartes graphiques du moteur (llama-server -v --list-devices) : (« Vulkan0 », nom) de la carte
    où l'IA tournera, ou None. Une vraie carte (pas la puce graphique du processeur, « uma: 1 » : trop lente) avec assez
    de mémoire ; la plus grande s'il y en a plusieurs."""
    integrees = {numero for numero, uma in re.findall(r"ggml_vulkan: (\d+) = .*?\| uma: (\d)", liste) if uma == "1"}
    cartes = [(int(memoire), f"Vulkan{numero}", nom) for numero, nom, memoire
              in re.findall(r"^\s*Vulkan(\d+): (.+?) \((\d+) MiB, \d+ MiB free\)", liste, re.M)
              if numero not in integrees and int(memoire) >= MEMOIRE_IA]
    return max(cartes)[1:] if cartes else None


def carte_pour_ia():
    """La carte graphique où l'IA tournera (voir choisir_carte_ia), demandée au moteur. OSError s'il ne se lance pas."""
    try:
        sortie = subprocess.run([moteur_ia(), "-v", "--list-devices"], cwd=os.path.dirname(moteur_ia()),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                timeout=60, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except subprocess.SubprocessError as e:
        raise OSError(str(e)) from e
    return choisir_carte_ia(sortie.decode("utf-8", "replace"))


class _LimitesTache(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION (Windows)."""
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
                ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_TACHE_WINDOWS = []  # (le « job » Windows des processus lancés par le programme, créé une fois)


def lier_au_programme(processus):
    """Ce processus s'arrêtera avec le programme, même si celui-ci plante : il est mis dans un « job » Windows qui tue
    ses processus quand il est fermé, ce que Windows fait à la fin du programme."""
    noyau = ctypes.WinDLL("kernel32", use_last_error=True)
    noyau.CreateJobObjectW.restype = wintypes.HANDLE
    noyau.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    noyau.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    if not _TACHE_WINDOWS:
        tache = noyau.CreateJobObjectW(None, None)
        limites = _LimitesTache(LimitFlags=0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not tache or not noyau.SetInformationJobObject(tache, 9, ctypes.byref(limites), ctypes.sizeof(limites)):
            raise ctypes.WinError(ctypes.get_last_error())
        _TACHE_WINDOWS.append(tache)
    if not noyau.AssignProcessToJobObject(_TACHE_WINDOWS[0], int(processus._handle)):
        raise ctypes.WinError(ctypes.get_last_error())


class MoteurIA:
    """Le moteur de l'IA (llama-server) lancé par le programme : invisible, joignable depuis ce PC seulement (adresse
    locale, port au hasard, clé secrète : une page web ne peut pas s'en servir). Démarré au premier besoin, arrêté après
    VEILLE_IA sans travail, quand l'IA est coupée et quand le programme se ferme (même s'il plante)."""

    def __init__(self):
        self.carte = None  # (« Vulkan0 », nom) où tourne l'IA ; False : pas de carte adaptée ; None : pas encore cherchée
        self.processus = None
        self.port, self.cle = 0, ""
        self.servi_le = 0.0
        self.verrou = threading.Lock()
        self.ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # (jamais par un proxy)

    def demarrer(self):
        """Lance le moteur s'il ne tourne pas, et attend qu'il ait chargé le modèle sur la carte (quelques secondes)."""
        with self.verrou:
            if self.processus is not None and self.processus.poll() is None:
                return
            with socket.socket() as essai:  # (un port libre)
                essai.bind(("127.0.0.1", 0))
                self.port = essai.getsockname()[1]
            self.cle = secrets.token_hex(16)
            self.servi_le = time.monotonic()  # (pendant qu'il démarre, il n'est pas « sans travail » : en_veille l'attend)
            # (Le modèle est désigné depuis son dossier : un chemin avec des lettres d'une autre langue que celle de
            # Windows, dans le nom d'utilisateur, ne passerait pas.)
            with open(os.path.join(DOSSIER_IA, "journal-moteur.txt"), "wb") as journal:
                self.processus = subprocess.Popen(
                    [moteur_ia(), "-m", MODELE_IA["fichier"], "--host", "127.0.0.1", "--port", str(self.port),
                     "--api-key", self.cle, "--device", self.carte[0], "-ngl", "99", "-c", "4096", "-np", "1"],
                    cwd=DOSSIER_IA, stdout=journal, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                lier_au_programme(self.processus)
            except OSError:
                log.exception("IA locale : moteur non lié au programme")
            log.info("IA locale : démarrage du moteur sur %s", self.carte[1])
            fin = time.monotonic() + 180
            while time.monotonic() < fin:
                if self.processus.poll() is not None:
                    code, self.processus = self.processus.returncode, None
                    raise RuntimeError(f"le moteur de l'IA s'est arrêté (code {code}, voir ia\\journal-moteur.txt)")
                try:
                    with self.ouvreur.open(f"http://127.0.0.1:{self.port}/health", timeout=2):
                        self.servi_le = time.monotonic()
                        return
                except (OSError, HTTPException):  # (503 : le modèle se charge encore)
                    time.sleep(0.5)
            self.processus.kill()
            self.processus = None
            raise TimeoutError("le moteur de l'IA ne démarre pas (voir ia\\journal-moteur.txt)")

    def completer(self, invite, longueur):
        """La suite de l'invite écrite par l'IA (au plus `longueur` jetons)."""
        self.demarrer()
        corps = {"prompt": invite, "n_predict": longueur, "temperature": 0, "stop": ["<end_of_turn>"]}
        requete = urllib.request.Request(f"http://127.0.0.1:{self.port}/completion", json.dumps(corps).encode(),
                                         {"Content-Type": "application/json", "Authorization": "Bearer " + self.cle})
        with self.ouvreur.open(requete, timeout=120) as reponse:
            texte = json.load(reponse)["content"]
        self.servi_le = time.monotonic()
        return texte

    def en_veille(self):
        """Arrête le moteur s'il n'a rien fait depuis VEILLE_IA : la carte graphique retrouve sa mémoire."""
        if self.processus is not None and time.monotonic() - self.servi_le > VEILLE_IA:
            log.info("IA locale : moteur arrêté (rien à retraduire depuis %d min)", VEILLE_IA // 60)
            self.arreter()

    def arreter(self):
        with self.verrou:
            processus, self.processus = self.processus, None
        if processus is not None and processus.poll() is None:
            processus.terminate()
            try:
                processus.wait(5)
            except subprocess.TimeoutExpired:
                pass


class IALocale:
    """Retraduit par l'IA locale, en arrière-plan et dans l'ordre de lecture, les textes déjà traduits ; ses traductions
    remplacent les premières dans la mémoire du traducteur, puis `pret()` les fait afficher."""

    LOT = 8  # textes pris à la fois dans la file (dans l'ordre de lecture : ce qui est en haut d'abord)

    def __init__(self, traducteur, pret):
        self.traducteur, self.pret = traducteur, pret
        self.file = collections.OrderedDict()  # (texte, cible) -> None, dans l'ordre de lecture
        self.faites = set()  # (texte, cible) déjà vus par l'IA (gardés ou non)
        self.gardees = set()  # (texte, cible) dont la traduction est celle de l'IA (retirées si on la coupe)
        self.verrou = threading.Lock()
        self.reveil = threading.Event()
        self.actif = False
        self.etat = ""
        self.moteur = MoteurIA()

    def a_ameliorer(self, texte):
        """Ce que l'IA retraduit : une phrase (pas un bouton court), sans code protégé, sans « Mes mots », pas un
        libellé fixe du glossaire."""
        mots = self.traducteur.mots
        return (len(texte.split()) + len(CJK.findall(texte)) / 2 >= MOTS_IA
                and MARQUE_CODE not in texte and not CODE_CITE.search(texte)
                and not (mots.motif and mots.motif.search(texte)) and not libelle_du_glossaire(texte))

    def proposer(self, textes, cible):
        if not self.actif:
            return
        with self.verrou:
            for texte in textes:
                cle = (texte, cible)
                if cle not in self.faites and cle not in self.file and self.a_ameliorer(texte):
                    self.file[cle] = None
            while len(self.file) > 400:  # (une très grosse page : les plus anciens attendront)
                self.file.popitem(last=False)
        if self.file:
            self.reveil.set()

    def boucle(self, arret):
        """(Fil dédié.)"""
        while not arret.is_set():
            self.reveil.wait(5)
            self.reveil.clear()
            self.moteur.en_veille()
            while self.actif and not arret.is_set() and self.file:
                with self.verrou:
                    lot = list(self.file)[:self.LOT]
                    cible = lot[0][1]
                    lot = [cle for cle in lot if cle[1] == cible]
                    for cle in lot:
                        del self.file[cle]
                        self.faites.add(cle)
                textes = [t for t, _ in lot if self.traducteur.cache.get((t, cible))]  # (traduits par les modèles)
                if not textes:
                    continue
                try:
                    resultats = self._traduire(textes, cible)
                except Exception as e:
                    log.warning("IA locale : pas de réponse (%s)", e)
                    self.etat = "le moteur de l'IA ne répond pas (voir journal.txt)"
                    with self.verrou:
                        self.faites.difference_update(lot)  # (réessayés plus tard)
                    arret.wait(10)
                    break
                gardes = 0
                with self.traducteur.verrou:
                    for texte, traduit in zip(textes, resultats):
                        traduit = self._verifier(texte, traduit, cible)
                        if traduit:
                            self.traducteur.cache[(texte, cible)] = traduit
                            self.traducteur.cache_modifie = True
                            self.gardees.add((texte, cible))
                            gardes += 1
                if gardes:
                    self.etat = "Active"
                    self.pret()

    def oublier(self):
        """L'IA est coupée : ses traductions sont retirées (les modèles habituels retraduisent ces textes)."""
        self.actif = False
        with self.verrou:
            self.file.clear()
            self.faites.clear()
            gardees, self.gardees = self.gardees, set()
        with self.traducteur.verrou:
            for cle in gardees:
                self.traducteur.cache.pop(cle, None)
            self.traducteur.cache_modifie = True
            self.traducteur.generation += 1

    def _verifier(self, texte, traduit, cible):
        """La traduction de l'IA, retouchée, ou None si elle n'est pas sûre (nombres changés, beaucoup trop longue,
        explications au lieu d'une traduction)."""
        traduit = " ".join((traduit or "").split()).strip("\"«» ")
        if not traduit or traduit.casefold() == texte.casefold() or len(traduit) > 3 * len(texte) + 40 \
                or not memes_chiffres(texte, traduit) or traduction_aberrante(traduit):
            return None
        if cible == "fr":
            traduit = retoucher_francais(traduit)
        return traduit.upper() if self.traducteur._en_capitales(texte) else traduit

    def _traduire(self, textes, cible):
        """Une traduction par texte (TranslateGemma : un texte par demande, avec la consigne qu'il attend) ; "" pour un
        texte que le moteur refuse (trop long pour lui)."""
        sortie = []
        for texte in textes:
            with self.traducteur.verrou:
                source = self.traducteur._langue(texte, cible) or "en"
            de, vers = NOMS_ANGLAIS.get(source, "English"), NOMS_ANGLAIS.get(cible, "French")
            invite = (f"You are a professional {de} ({source}) to {vers} ({cible}) translator. Your goal is to accurately "
                      f"convey the meaning and nuances of the original {de} text while adhering to {vers} grammar, "
                      f"vocabulary, and cultural sensitivities.\nProduce only the {vers} translation, without any "
                      f"additional explanations or commentary. Please translate the following {de} text into "
                      f"{vers}:\n\n\n{texte}")
            try:
                sortie.append(self.moteur.completer(f"<start_of_turn>user\n{invite}<end_of_turn>\n<start_of_turn>model\n",
                                                    min(2 * len(texte) + 64, 1536)))
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                log.warning("IA locale : texte refusé par le moteur (%d caractères)", len(texte))
                sortie.append("")
        return sortie


# --------------------------------------------------------------------------- vitres de traduction
#
# Chaque traduction est une petite fenêtre dessinée image par image, posée sur le texte
# d'origine. En mode « auto », elle reprend la couleur du fond et celle du texte qu'elle
# recouvre : la traduction se fond dans la page, comme si elle y était écrite.


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


gdi32 = ctypes.windll.gdi32
user32.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, wintypes.DWORD]
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

MARGE_X, MARGE_Y = 4, 1
VITRE_VIDE = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
_polices = {}


CHASSES = (100, 88, 75)  # largeur des lettres (%) : Segoe UI, puis Bahnschrift de plus en plus étroite


def police_pil(taille, chasse=100, gras=False):
    """Segoe UI ; avec une chasse plus petite, Bahnschrift étroite (police de Windows) : même hauteur, lettres
    plus serrées, pour une traduction plus longue que la place plutôt que de l'écrire tout petit."""
    cle = (taille, chasse, gras)
    if cle not in _polices:
        try:
            if chasse < 100:
                police = ImageFont.truetype("bahnschrift.ttf", taille)
                police.set_variation_by_axes([550 if gras else 350, chasse])  # (graisse proche de Segoe UI, largeur)
            else:
                police = ImageFont.truetype("seguisb.ttf" if gras else "segoeui.ttf", taille)  # (semi-gras : celui des boutons)
        except (OSError, ValueError):
            police = police_pil(taille) if chasse < 100 or gras else ImageFont.load_default(taille)
        _polices[cle] = police
    return _polices[cle]


def hauteur_ligne(taille):
    return round(taille * 1.25)


_longueurs = {}  # ((taille, chasse, gras), texte) -> largeur en pixels


def mesurer(texte, taille, largeur_max, chasse=100, gras=False):
    """Découpe le texte en lignes tenant dans la largeur ; renvoie (largeur, hauteur, lignes).

    On mesure avec la police double taille, celle qui sert au dessin, pour que le texte dessiné
    ait exactement la largeur prévue (sinon la fin des lignes peut être coupée)."""
    police = police_pil(taille * 2, chasse, gras)
    cle = (taille, chasse, gras)

    def longueur(t):
        # (Mémorisé : la mise en page essaie des dizaines de tailles et de largeurs pour chaque texte.)
        valeur = _longueurs.get((cle, t))
        if valeur is None:
            if len(_longueurs) > 200_000:
                _longueurs.clear()
            valeur = _longueurs[(cle, t)] = police.getlength(t) / 2
        return valeur

    entier = " ".join(texte.split())
    if longueur(entier) <= largeur_max:  # tient sur une ligne : inutile de l'essayer mot à mot
        return longueur(entier) + 1, hauteur_ligne(taille), [entier]
    lignes, ligne = [], ""
    for mot in texte.split():
        essai = f"{ligne} {mot}" if ligne else mot
        if ligne and longueur(essai) > largeur_max:
            lignes.append(ligne)
            ligne = mot
        else:
            ligne = essai
    lignes.append(ligne)
    return max(longueur(l) for l in lignes) + 1, len(lignes) * hauteur_ligne(taille), lignes


def dessiner_vitre(lignes, taille, l, h, fond, encre, rayon=3, centre=False, chasse=100, gras=False):
    """Image RGBA d'une vitre : fond uni aux coins légèrement arrondis, texte par-dessus
    (aligné à gauche, ou centré)."""
    s = 2  # dessin en double taille puis réduit : bords et lettres bien lisses
    L, H = l * s, h * s
    masque = Image.new("L", (L, H), 0)
    ImageDraw.Draw(masque).rounded_rectangle((0, 0, L - 1, H - 1), radius=min(rayon, h // 2) * s, fill=255)
    image = Image.new("RGBA", (L, H), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (L, H), tuple(fond) + (255,)), (0, 0), masque)
    police = police_pil(taille * s, chasse, gras)
    pas = hauteur_ligne(taille) * s
    haut = (H - pas * len(lignes)) // 2 + pas // 2
    trace = ImageDraw.Draw(image)
    for i, ligne in enumerate(lignes):
        gauche = (L - police.getlength(ligne)) / 2 if centre else MARGE_X * s
        trace.text((gauche, haut + i * pas), ligne, font=police, fill=tuple(encre) + (255,), anchor="lm")
    return image.resize((l, h), Image.LANCZOS)


def changer_opacite(hwnd, opacite):
    """Change seulement la transparence d'une fenêtre déjà dessinée (0 à 255)."""
    user32.UpdateLayeredWindow(hwnd, None, None, None, None, None, 0,
                               ctypes.byref(BLENDFUNCTION(0, 0, opacite, AC_SRC_ALPHA)), ULW_ALPHA)


def poser_image(hwnd, image, x, y, opacite=255):
    """Affiche l'image (avec sa transparence) comme contenu de la fenêtre, à la position donnée."""
    l, h = image.size
    pixels = np.asarray(image, dtype=np.uint16)
    alpha = pixels[..., 3:4]
    bgra = np.concatenate([pixels[..., 2::-1] * alpha // 255, alpha], axis=2).astype(np.uint8)
    donnees = np.ascontiguousarray(bgra[::-1]).tobytes()  # Windows range les lignes de bas en haut
    ecran = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(ecran)
    bitmap = None
    try:
        entete = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), l, h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(ecran, ctypes.byref(entete), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise OSError("Image de %d × %d impossible à créer (mémoire graphique)" % (l, h))
        ctypes.memmove(bits, donnees, len(donnees))
        ancien = gdi32.SelectObject(dc, bitmap)
        user32.UpdateLayeredWindow(hwnd, ecran, ctypes.byref(wintypes.POINT(x, y)), ctypes.byref(wintypes.SIZE(l, h)),
                                   dc, ctypes.byref(wintypes.POINT(0, 0)), 0,
                                   ctypes.byref(BLENDFUNCTION(0, 0, opacite, AC_SRC_ALPHA)), ULW_ALPHA)
        gdi32.SelectObject(dc, ancien)
    finally:  # les ressources de dessin de Windows sont toujours rendues
        if bitmap:
            gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, ecran)


# ---- couleurs de l'écran

def couleurs_autour(ecran, origine, t):
    """(couleur du fond, couleur du texte) d'un texte, d'après les pixels de son cadre.

    `ecran` est l'image de tout l'écran (tableau numpy BGRA) où nos vitres n'apparaissent pas.
    """
    ox, oy = origine
    x1, y1, x2, y2 = t.x - ox, t.y - oy, t.x + t.l - ox, t.y + t.h - oy
    if t.genre == "texte":
        # Le cadre colle au texte : le fond se lit sur un liseré juste autour.
        x1, y1, x2, y2 = x1 - 2, y1 - 2, x2 + 2, y2 + 2
    else:
        # Bouton, cellule : le fond se lit sur un liseré juste à l'intérieur du cadre.
        x1, y1, x2, y2 = x1 + 2, y1 + 2, x2 - 2, y2 - 2
    hauteur, largeur = ecran.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(largeur, x2), min(hauteur, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    zone = ecran[y1:y2, x1:x2, 2::-1]  # BGRA de Windows -> RGB
    bord = np.concatenate([zone[0], zone[-1], zone[:, 0], zone[:, -1]]).astype(np.int32)
    # Couleur la plus fréquente du liseré (regroupée par paliers de 16 pour ignorer le bruit).
    paliers = (bord // 16) @ np.array([256 * 256, 256, 1])
    valeurs, nombres = np.unique(paliers, return_counts=True)
    fond = bord[paliers == valeurs[np.argmax(nombres)]].mean(axis=0)
    # Le texte : les pixels intérieurs les plus éloignés de la couleur du fond.
    interieur = zone.reshape(-1, 3).astype(np.int32)
    ecarts = np.abs(interieur - fond).sum(axis=1)
    marques = ecarts > 90
    if marques.sum() >= 4:
        # Le cœur des lettres (les bords sont teintés par le lissage) : parmi les pixels bien contrastés, la
        # couleur la plus fréquente, pas la plus extrême (un lien bleu, un émoji ou un bout de code dans la
        # ligne donneraient sinon leur couleur à toute la traduction).
        forts = interieur[ecarts >= np.percentile(ecarts[marques], 75)]
        paliers_encre = (forts // 24) @ np.array([256 * 256, 256, 1])
        valeurs, nombres = np.unique(paliers_encre, return_counts=True)
        encre = forts[paliers_encre == valeurs[np.argmax(nombres)]].mean(axis=0)
    else:
        encre = (20, 22, 30) if fond.mean() > 128 else (240, 241, 245)
    libre = fond_libre_autour(ecran, (t.x - ox, t.y - oy, t.x + t.l - ox, t.y + t.h - oy), fond)
    lettres = np.abs(zone.astype(np.int32) - encre).sum(axis=2) < 100  # le cœur des lettres
    return (tuple(int(c) for c in fond), tuple(int(c) for c in encre),
            compter_lignes(ecarts.reshape(zone.shape[:2]) > 90), libre, ecrit_en_gras(lettres, t))


def epaisseur_traits(lettres):
    """Épaisseur moyenne des traits (pixels) : longueur moyenne des suites de pixels « lettre » sur une rangée."""
    suites = np.count_nonzero(lettres[:, 1:] & ~lettres[:, :-1]) + np.count_nonzero(lettres[:, 0])
    return np.count_nonzero(lettres) / suites if suites else 0.0


def ecrit_en_gras(lettres, t):
    """Le texte d'origine est-il en gras (un bouton « Open Project ») ? Ses traits comparés à ceux du même texte
    écrit en Segoe UI normal, à la taille qui donne la même largeur (la hauteur lue est imprécise : une taille
    fausse épaissit ou affine les traits autant que le gras). Mesuré sur le Creator Companion : rapport ~1,05 pour
    le texte normal, ~1,3 à 1,6 pour le gras."""
    if not lettres.any() or not A_TRADUIRE.search(t.texte) or len(t.texte) > 60:
        return False
    try:
        taille = max(6, min(60, round(20 * max(4, t.l - 2) / max(1.0, police_pil(20).getlength(t.texte)))))
        police = police_pil(taille)  # (polices gardées en mémoire : les recharger coûtait ~1 ms par texte)
    except OSError:
        return False
    image = Image.new("L", (int(police.getlength(t.texte)) + 8, taille * 2), 0)
    ImageDraw.Draw(image).text((4, taille // 3), t.texte, font=police, fill=255)
    normal = epaisseur_traits(np.asarray(image) > 140)
    return bool(normal) and epaisseur_traits(lettres) >= 1.25 * normal


def fond_libre_autour(ecran, cadre, fond, portee=600):
    """(fond vide à gauche, à droite, texte centré dans un cadre ?) : combien de pixels de la
    couleur `fond` il y a de chaque côté du texte, à sa hauteur (la place où la traduction peut
    s'étendre), et si le texte est centré entre deux bords nets (un bouton)."""
    x1, y1, x2, y2 = cadre
    hauteur, largeur = ecran.shape[:2]
    y1, y2 = max(0, y1), min(hauteur, y2)
    if y2 <= y1:
        return 0, 0, False
    fond = np.array(fond[::-1], np.int16)  # l'image est en BGRA

    def mesurer(a, b, vers_la_gauche):
        """(colonnes vides depuis le texte, arrêté par un bord net sur toute la hauteur ?)"""
        bande = ecran[y1:y2, max(0, a):max(0, min(largeur, b)), :3].astype(np.int16)
        vides = np.abs(bande - fond).sum(axis=2) <= 30
        if vers_la_gauche:
            vides = vides[:, ::-1]
        colonnes = vides.all(axis=0)
        if colonnes.all():
            return len(colonnes), False  # bord de l'écran ou rien sur `portee` pixels
        n = int(np.argmin(colonnes))
        # Un bord (de bouton, même arrondi) arrête toutes les rangées presque au même endroit ;
        # des lettres voisines, seulement quelques-unes.
        arrets = np.where(~vides.all(axis=1), np.argmin(vides, axis=1), 10_000)
        return n, bool(arrets.max() - n <= max(8, 0.6 * (y2 - y1)))

    gauche, bord_g = mesurer(x1 - portee, x1, True)
    droite, bord_d = mesurer(x2, x2 + portee, False)
    centre = bord_g and bord_d and abs(gauche - droite) <= max(6, 0.25 * (gauche + droite))
    return gauche, droite, centre


def compter_lignes(encre):
    """Nombre de lignes de texte : groupes de rangées de pixels qui contiennent de l'encre
    (les petits creux, comme entre le point et le corps d'un « i », ne coupent pas une ligne)."""
    lignes, epaisseur, creux = 0, 0, 0
    for rangee_encree in encre.any(axis=1):
        if rangee_encree:
            epaisseur += 1
            creux = 0
        else:
            creux += 1
            if creux > 2 and epaisseur:
                lignes += epaisseur > 3  # une ligne de texte fait plus de 3 pixels de haut
                epaisseur = 0
    lignes += epaisseur > 3
    return lignes


class Vitre:
    """Fenêtre qui affiche une image. Les clics la traversent. Elle reste visible sur les
    captures d'écran, sauf le court instant où le programme lit lui-même l'écran.

    `etat` change à chaque fois que la fenêtre principale la montre ou la cache : le suivi en
    direct (autre fil) ne la déplace que si elle n'a pas changé depuis qu'il l'a prise en charge
    (sinon il ferait réapparaître une vitre rangée, ou en déplacerait une qui montre autre chose)."""

    def __init__(self, root, verrou):
        self.verrou = verrou  # partagé avec le suivi en direct
        self.etat = 0
        self.deplacee = False  # un déplacement du suivi en direct est peut-être encore en attente (voir _replacer)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        # La fenêtre doit exister à l'écran pour recevoir ses styles : on la crée hors de vue.
        self.win.geometry("1x1+-32000+-32000")
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        etendu = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                              etendu | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        # Pas d'arrondi par Windows 11 : il ajouterait une ombre. L'arrondi est dans l'image.
        sans_arrondi = ctypes.c_uint(DWMWCP_DONOTROUND)
        dwmapi.DwmSetWindowAttribute(self.hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(sans_arrondi), ctypes.sizeof(sans_arrondi))
        self.cacher()

    OPACITES_FONDU = (110, 200, 255)  # apparition en douceur, en 3 images (~0,05 s)
    opacite = 255  # opacité de toutes les traductions (réglage « Opacité »)

    def montrer(self, image, x, y, fondu=True):
        """Affiche l'image : en fondu pour une nouvelle traduction, d'un coup pour en remplacer une."""
        self._arreter_fondu()
        with self.verrou:
            self.etat += 1
            poser_image(self.hwnd, image, x, y, opacite=0 if fondu else Vitre.opacite)
            self._replacer(x, y)
        if fondu:
            self._fondu(0)

    def _replacer(self, x, y):
        """(Sous le verrou.) La vitre a été déplacée par le suivi en direct : un déplacement encore en attente (confié
        au fil de la fenêtre principale) la pousserait à l'ancienne place après coup. Sa vraie place est confiée de
        la même façon, après lui : c'est elle qui l'emporte."""
        if self.deplacee:
            self.deplacee = False
            user32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)

    def deplacer(self, etat, x, y):
        """Déplace la vitre (suivi en direct, autre fil) si elle n'a pas changé depuis `etat`.

        Le déplacement est confié au fil de la fenêtre principale sans attendre qu'il le traite :
        sinon le suivi attendrait ce fil (occupé à poser des traductions) en tenant le verrou que ce
        même fil demande, et tout se figerait."""
        with self.verrou:
            if self.etat == etat:
                self.deplacee = True
                user32.SetWindowPos(self.hwnd, None, int(x), int(y), 0, 0,
                                    SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)

    def _fondu(self, etape):
        changer_opacite(self.hwnd, round(self.OPACITES_FONDU[etape] * Vitre.opacite / 255))
        if etape + 1 < len(self.OPACITES_FONDU):
            self._suite_fondu = self.win.after(20, self._fondu, etape + 1)
        else:
            self._suite_fondu = None

    def _arreter_fondu(self):
        if getattr(self, "_suite_fondu", None):
            self.win.after_cancel(self._suite_fondu)
            self._suite_fondu = None

    def cacher(self):
        # On ne masque jamais la fenêtre au sens de Windows (elle ne pourrait plus recevoir
        # d'image) : on la vide et on la range hors de l'écran.
        self._arreter_fondu()
        with self.verrou:
            self.etat += 1
            poser_image(self.hwnd, VITRE_VIDE, -32000, -32000)
            self._replacer(-32000, -32000)


def dessiner_chargement(etapes=12, taille=22):
    """Les images de l'anneau de chargement : un arc indigo qui tourne, sur une pastille sombre discrète."""
    images = []
    grand = taille * 4  # dessiné en grand puis réduit : bords lisses
    for etape in range(etapes):
        image = Image.new("RGBA", (grand, grand), (0, 0, 0, 0))
        dessin = ImageDraw.Draw(image)
        dessin.ellipse((0, 0, grand - 1, grand - 1), fill=(18, 19, 26, 150))
        marge, epaisseur = grand // 5, grand // 9
        boite = (marge, marge, grand - marge, grand - marge)
        dessin.arc(boite, 0, 360, fill=(99, 102, 241, 70), width=epaisseur)
        debut = etape * 360 / etapes
        dessin.arc(boite, debut, debut + 100, fill=(129, 140, 248, 255), width=epaisseur)
        images.append(image.resize((taille, taille), Image.LANCZOS))
    return images


IMAGES_CHARGEMENT = dessiner_chargement()


class Calque:
    """Affiche les traductions sur des vitres posées sur le texte d'origine.

    Seuls les textes qui changent sont redessinés, et les vitres sont réutilisées.
    """

    TAILLE_MIN = 8
    TAILLE_MAX = 20
    facteur_taille = 1.0  # taille du texte des traductions par rapport à l'original (réglage)
    fondu = True  # les nouvelles traductions apparaissent en douceur (réglage)

    def __init__(self, root, style):
        self.root = root
        self.style = style
        self.affiches = {}  # Texte -> Vitre
        self.dispositions = {}  # Texte -> disposition de sa vitre (pour la garder telle quelle)
        self.libres = []  # vitres prêtes à resservir
        self.vitre_badge = None
        self.vitre_chargement = None  # petit anneau qui tourne pendant qu'une traduction se prépare
        self.vitre_cadre = None  # fin cadre autour d'une zone choisie (raccourci « zone »)
        self.etape_chargement = None  # image de l'anneau affichée (None : caché)
        self.visible = False
        # Pour l'analyse de l'image (autre fil) : les fenêtres des vitres, à cacher des captures,
        # et un compteur impair pendant qu'on pose des traductions (une capture prise à ce
        # moment-là est refaite).
        self.hwnds = ()
        self.generation = 0
        self.verrou = threading.Lock()  # montrer / cacher une vitre (Tk) contre la déplacer (suivi en direct)
        # Mises en page et dessins déjà faits (fil de traduction).
        self._dispositions_faites = {}
        self._dessins = {}

    def _vitre(self):
        if self.libres:
            return self.libres.pop()
        vitre = Vitre(self.root, self.verrou)
        self.hwnds += (vitre.hwnd,)
        return vitre

    RESERVE_MAX = 40  # vitres gardées prêtes à resservir ; les autres sont détruites (une grosse page en laissait des centaines)

    def _rendre(self, vitre):
        vitre.cacher()
        if len(self.libres) < self.RESERVE_MAX:
            self.libres.append(vitre)
            return
        self.hwnds = tuple(h for h in self.hwnds if h != vitre.hwnd)
        try:
            vitre.win.destroy()
        except tk.TclError:
            pass

    def changer_style(self, style):
        self.style = style
        self.effacer()  # tout sera redessiné dans le nouveau style à la prochaine lecture

    def changer_taille(self, facteur):
        self.facteur_taille = facteur
        self.effacer()  # tout sera redessiné à la nouvelle taille à la prochaine lecture

    def regler_opacite(self, opacite):
        """Opacité de toutes les traductions (0 à 255), appliquée tout de suite à celles affichées."""
        Vitre.opacite = opacite
        for vitre in list(self.affiches.values()):
            changer_opacite(vitre.hwnd, opacite)

    def montrer(self):
        self.visible = True

    def cacher(self):
        self.visible = False
        self.effacer()
        self.badge("")

    def effacer(self, zone=None):
        """Efface les traductions : toutes, ou celles dont le centre est dans la zone (coordonnées écran)."""
        for texte in list(self.affiches):
            cx, cy = texte.x + texte.l // 2, texte.y + texte.h // 2
            if zone is None or (zone[0] <= cx < zone[2] and zone[1] <= cy < zone[3]):
                self._rendre(self.affiches.pop(texte))
                self.dispositions.pop(texte, None)

    def cacher_sous(self, fenetre):
        """Une fenêtre vient de passer devant : les traductions des autres fenêtres qu'elle recouvre
        disparaissent tout de suite, sans attendre la relecture de l'écran (elles flotteraient par-dessus)."""
        x1, y1, x2, y2 = fenetre
        for texte in list(self.affiches):
            if tuple(texte.fenetre) != tuple(fenetre) and texte.x < x2 and x1 < texte.x + texte.l \
                    and texte.y < y2 and y1 < texte.y + texte.h:
                self._rendre(self.affiches.pop(texte))
                self.dispositions.pop(texte, None)

    def cacher_couvertes(self, sauf=frozenset(), fenetre_au_point=fenetre_au_point):
        """Range les traductions dont le texte n'est plus visible : une autre fenêtre glissée ou passée dessus,
        leur fenêtre réduite, fermée, déplacée ou redimensionnée sans qu'on l'ait suivie. Sans attendre la
        prochaine lecture de l'écran (elles flottaient jusque-là sur ce qui les recouvre). `sauf` : celles qu'un
        suivi en direct est en train de déplacer (leur place enregistrée est l'ancienne)."""
        for texte in list(self.affiches):
            if texte in sauf:
                continue
            visible = fenetre_au_point(texte.x + texte.l // 2, texte.y + texte.h // 2)
            if visible is not None and visible != tuple(texte.fenetre):
                self._rendre(self.affiches.pop(texte))
                self.dispositions.pop(texte, None)

    def cadre_zone(self, zone):
        """Un fin cadre de couleur autour de la zone choisie (None : retiré). Les clics le traversent, et la lecture
        de l'écran ne le voit pas (comme les traductions)."""
        if zone is None:
            if self.vitre_cadre is not None:
                self.vitre_cadre.cacher()
            return
        if self.vitre_cadre is None:
            self.vitre_cadre = Vitre(self.root, self.verrou)
            self.hwnds += (self.vitre_cadre.hwnd,)
        x1, y1, x2, y2 = zone
        marge = 3
        image = Image.new("RGBA", (x2 - x1 + 2 * marge, y2 - y1 + 2 * marge), (0, 0, 0, 0))
        ImageDraw.Draw(image).rounded_rectangle((0, 0, image.width - 1, image.height - 1), 6,
                                                outline=(129, 140, 248, 255), width=2)
        self.vitre_cadre.montrer(image, x1 - marge, y1 - marge, fondu=False)

    def chargement(self, visible):
        """Petit anneau discret qui tourne en haut à droite de l'écran tant qu'une traduction se prépare."""
        if not visible:
            self.etape_chargement = None
            if self.vitre_chargement:
                self.vitre_chargement.cacher()
            return
        if self.vitre_chargement is None:
            self.vitre_chargement = Vitre(self.root, self.verrou)
            self.hwnds += (self.vitre_chargement.hwnd,)
        if self.etape_chargement is None:
            self.etape_chargement = 0
            if getattr(self, "_tour_prevu", None) is None:  # (une seule minuterie : sinon il tournerait de plus en plus vite)
                self._tourner()

    def _tourner(self):
        self._tour_prevu = None
        if self.etape_chargement is None:
            return
        image = IMAGES_CHARGEMENT[self.etape_chargement % len(IMAGES_CHARGEMENT)]
        self.vitre_chargement.montrer(image, user32.GetSystemMetrics(SM_CXSCREEN) - image.width - 24, 24, fondu=False)
        self.etape_chargement += 1
        self._tour_prevu = self.root.after(70, self._tourner)

    def badge(self, texte):
        # (Fondu seulement quand il apparaît : pas à chaque mise à jour, 4 fois par seconde pendant un téléchargement.)
        apparait, self._badge_affiche = not getattr(self, "_badge_affiche", ""), texte
        if not texte:
            if self.vitre_badge:
                self.vitre_badge.cacher()
            return
        if not self.vitre_badge:
            self.vitre_badge = Vitre(self.root, self.verrou)
            self.hwnds += (self.vitre_badge.hwnd,)
        largeur, _, lignes = mesurer(texte, 15, 10_000)
        l, h = int(largeur) + 2 * MARGE_X + 16, 34
        image = dessiner_vitre(lignes, 15, l, h, (99, 102, 241), (255, 255, 255), rayon=10)
        self.generation += 1
        try:
            self.vitre_badge.montrer(image, user32.GetSystemMetrics(SM_CXSCREEN) - l - 24, 24,
                                     fondu=apparait)
        finally:
            self.generation += 1

    @staticmethod
    def _meme_place(a, b):
        """Les deux textes occupent-ils (presque) la même place à l'écran ?"""
        commun = surface_commune((a.x, a.y, a.x + a.l, a.y + a.h), (b.x, b.y, b.x + b.l, b.y + b.h))
        return commun > 0.5 * min(a.l * a.h, b.l * b.h)

    def preparer(self, textes, traductions, couleurs, obstacles=()):
        """Met en page et dessine les traductions (dans le fil de traduction : la fenêtre
        principale n'a plus qu'à les afficher). `obstacles` : tous les textes lus à l'écran,
        traduits ou non, qu'une traduction ne doit pas cacher en s'étendant.
        Renvoie [(texte, disposition, image)] ; un texte sans place est laissé de côté."""
        if len(self._dessins) > 2000:
            self._dispositions_faites, self._dessins = {}, {}
        autres = list(textes) + list(obstacles)
        # Fenêtres de la plus en avant à la plus en arrière : celles devant la fenêtre d'un texte sont des
        # obstacles pour sa traduction (les vitres sont au-dessus de tout : elles flotteraient sur elles).
        ordre = [rect for _, rect, _ in fenetres_visibles()]
        preparees = []
        for texte, traduit, teintes in zip(textes, traductions, couleurs):
            f = tuple(texte.fenetre)
            devant = tuple(r for r in ordre[:ordre.index(f)] if r[0] < f[2] and f[0] < r[2] and r[1] < f[3]
                           and f[1] < r[3]) if f in ordre else ()
            cle = (texte, traduit, teintes, self.style, self.facteur_taille, devant)
            if cle not in self._dispositions_faites:
                self._dispositions_faites[cle] = self._disposer(texte, traduit, teintes, autres, devant)
            disposition = self._dispositions_faites[cle]
            if disposition is None:
                continue  # vraiment pas la place : mieux vaut laisser l'original lisible
            if disposition not in self._dessins:
                lignes, taille, x, y, l, h, fond, encre, centre, chasse, gras = disposition
                self._dessins[disposition] = dessiner_vitre(lignes, taille, l, h, fond, encre, centre=centre,
                                                            chasse=chasse, gras=gras)
            preparees.append((texte, disposition, self._dessins[disposition]))
        return preparees

    def mettre_a_jour(self, preparees):
        """Affiche ces traductions préparées (et retire les autres).

        Une vitre déjà en place qui montre la même chose est gardée telle quelle ; une traduction
        qui en remplace une autre au même endroit apparaît d'un coup, avant que l'ancienne ne
        disparaisse : rien ne clignote."""
        if not self.visible:
            return
        self.generation += 1
        partants = {}
        try:
            nouveaux = {t: (d, image) for t, d, image in preparees}
            partants = {t: self.affiches.pop(t) for t in list(self.affiches) if t not in nouveaux}
            if log.isEnabledFor(logging.DEBUG):
                for t in partants:
                    log.debug("retirée : %r (%d,%d %dx%d) v%d", t.texte[:40], t.x, t.y, t.l, t.h, t.version)
                for t in nouveaux:
                    if t not in self.affiches:
                        log.debug("posée : %r (%d,%d %dx%d) v%d", t.texte[:40], t.x, t.y, t.l, t.h, t.version)
            for texte, (disposition, image) in nouveaux.items():
                if texte in self.affiches:
                    continue
                voisins = [a for a in partants if self._meme_place(a, texte)]
                identique = next((a for a in voisins if self.dispositions.get(a) == disposition), None)
                if identique is not None:
                    vitre = partants.pop(identique)
                    self.dispositions.pop(identique, None)
                else:
                    vitre = self._vitre()
                    vitre.montrer(image, disposition[2], disposition[3], fondu=self.fondu and not voisins)
                self.affiches[texte] = vitre
                self.dispositions[texte] = disposition
        finally:
            # Même si une vitre n'a pas pu s'afficher : les anciennes sont rangées (aucune ne reste à
            # l'écran sans que le programme le sache).
            try:
                for ancien, vitre in partants.items():
                    self.dispositions.pop(ancien, None)
                    self._rendre(vitre)
            finally:
                self.generation += 1  # toujours pair à la fin (sinon la lecture de l'écran attendrait)

    def instantane(self):
        """[(texte, vitre, disposition)] des traductions affichées. Lisible depuis un autre fil
        (le suivi en direct), quand aucune mise à jour n'est en cours."""
        for _ in range(5):
            try:
                return [(t, v, self.dispositions.get(t)) for t, v in list(self.affiches.items())]
            except RuntimeError:  # modifié pendant la lecture : on recommence
                continue
        return []

    def decaler(self, deplacements, caches):
        """Après un suivi en direct (défilement, fenêtre déplacée) : les vitres déplacées gardent
        leur nouvelle place sans être redessinées ; celles qui ont été cachées sont rangées.
        `deplacements` : [(texte, dx, dy, la fenêtre a bougé ?)]."""
        for texte in caches:
            self.dispositions.pop(texte, None)
            vitre = self.affiches.pop(texte, None)
            if vitre:
                self._rendre(vitre)
        deplaces = []
        for texte, dx, dy, fenetre_bouge in deplacements:
            vitre = self.affiches.pop(texte, None)
            disposition = self.dispositions.pop(texte, None)
            if vitre is None:
                continue
            if disposition is None:
                self._rendre(vitre)
                continue
            f = texte.fenetre
            fenetre = (f[0] + dx, f[1] + dy, f[2] + dx, f[3] + dy) if fenetre_bouge else f
            lignes, taille, x, y, l, h, fond, encre, centre, chasse, gras = disposition
            deplaces.append((replace(texte, x=texte.x + dx, y=texte.y + dy, fenetre=fenetre), vitre,
                             (lignes, taille, x + dx, y + dy, l, h, fond, encre, centre, chasse, gras)))
        for texte, vitre, disposition in deplaces:
            if texte in self.affiches:  # deux textes arrivés au même endroit : un seul suffit
                self._rendre(vitre)
                continue
            self.affiches[texte] = vitre
            self.dispositions[texte] = disposition

    def _taille_origine(self, t, teintes=None):
        """Taille estimée du texte d'origine, d'après la place qu'il occupe."""
        return self._selon_largeur(t, self._taille_selon_hauteur(t, teintes))

    def _taille_selon_hauteur(self, t, teintes):
        if t.taille:
            return max(self.TAILLE_MIN, min(40, t.taille))  # mesurée sur les lignes lues
        texte_seul = t.genre == "texte"
        if texte_seul and teintes and teintes[2]:
            # Nombre de lignes lu à l'écran : la hauteur d'une ligne donne la taille de la police.
            return max(self.TAILLE_MIN, min(28, round(t.h / teintes[2] / 1.33)))
        plafond = self.TAILLE_MAX if texte_seul else 15  # un bouton est plus grand que son texte
        for taille in range(min(plafond, max(self.TAILLE_MIN, int(t.h * 0.75))), self.TAILLE_MIN - 1, -1):
            largeur, hauteur, _ = mesurer(t.texte, taille, t.l + 4)
            if hauteur <= t.h + 2 and (not texte_seul or hauteur > taille * 1.6 or largeur <= t.l + 4):
                return taille
        return self.TAILLE_MIN

    def _selon_largeur(self, t, taille):
        """Une seule ligne : en plus, le texte d'origine écrit à cette taille ne doit pas dépasser la largeur
        qu'il occupe à l'écran. (La hauteur lue est souvent trop grande : lien souligné, bout de code encadré,
        émoji, bouton plus haut que son texte, police plus étroite que la nôtre.)"""
        if len(t.texte) < 4 or t.h > hauteur_ligne(taille) * 1.6:
            return taille
        plancher = max(self.TAILLE_MIN, round(taille * 0.75))
        while taille > plancher and mesurer(t.texte, taille, 100_000)[0] > t.l * 1.05:
            taille -= 1
        return taille

    @staticmethod
    def _place_libre(t, autres, teintes):
        """Limites (gauche, droite, bas) jusqu'où la vitre peut s'étendre : le fond vide autour du
        texte, sans cacher un autre texte (traduit ou non) ni sortir de sa fenêtre."""
        libre_g, libre_d = teintes[3][:2] if teintes and len(teintes) > 3 else (0, 0)
        fx1, fy1, fx2, fy2 = t.fenetre
        gauche = max(fx1, t.x - MARGE_X - max(0, libre_g - 2))
        droite = min(fx2, t.x + t.l + MARGE_X + max(0, libre_d - 2))
        bas = fy2
        for o in autres:
            cx, cy = o.x + o.l // 2, o.y + o.h // 2
            if t.x <= cx < t.x + t.l and t.y <= cy < t.y + t.h:
                continue  # le texte lui-même, ou une de ses lignes
            meme_ligne = o.y < t.y + t.h and t.y < o.y + o.h
            meme_colonne = o.x < t.x + t.l and t.x < o.x + o.l
            if meme_ligne:
                if cx >= t.x + t.l // 2:
                    droite = min(droite, o.x - 3)
                else:
                    gauche = max(gauche, o.x + o.l + 3)
            if meme_colonne and cy >= t.y + t.h:
                bas = min(bas, o.y - 2)
        return min(gauche, t.x - MARGE_X), max(droite, t.x + t.l + MARGE_X), bas

    def _disposer(self, t, traduit, teintes, autres, devant=()):
        """Où et comment écrire la traduction : (lignes, taille, x, y, l, h, fond, encre, centré ?),
        ou None.

        La traduction garde la taille du texte d'origine autant que possible. Un libellé, un titre,
        un bouton reste sur une ligne et peut s'étendre dans le fond vide autour de lui (sans
        cacher un autre texte) ; un paragraphe garde sa largeur et peut prendre des lignes de plus
        en dessous, si c'est vide. Sinon, elle s'écrit un peu plus petit."""
        mesuree = self._taille_origine(t, teintes)
        gras = bool(teintes and len(teintes) > 4 and teintes[4])  # même graisse que le texte d'origine
        origine = max(self.TAILLE_MIN, round(mesuree * self.facteur_taille))  # réglage « Taille du texte »
        gauche, droite, bas = self._place_libre(t, autres, teintes)
        # Les fenêtres posées devant celle du texte : la traduction ne s'étend pas sur elles.
        for a, b, c, d in devant:
            if b < t.y + t.h and t.y < d:  # à hauteur du texte
                if a >= t.x + t.l // 2:
                    droite = min(droite, a - 1)
                elif c <= t.x + t.l // 2:
                    gauche = max(gauche, c + 1)
            if a < t.x + t.l and t.x < c and b >= t.y + t.h:  # en dessous
                bas = min(bas, b - 1)
        droite, gauche = max(droite, t.x + t.l + MARGE_X), min(gauche, t.x - MARGE_X)
        peu_reduit = max(self.TAILLE_MIN, round(origine * 0.8))
        # Texte centré dans un cadre (un bouton) : la traduction aussi.
        centre = bool(teintes and len(teintes) > 3 and teintes[3][2])
        if t.h < hauteur_ligne(mesuree) * 1.8:
            # Un texte aligné à gauche ne s'étend que vers la droite (jamais hors de sa colonne, à gauche de
            # son début) ; un texte centré, des deux côtés.
            large = (droite - gauche if centre else droite - t.x + MARGE_X) - 2 * MARGE_X
            # En plus grand, une ligne peut dépasser un peu en hauteur.
            haut = round(t.h * max(1.0, self.facteur_taille))
            # Place libre en dessous : sur deux lignes plutôt qu'écrite tout petit.
            dessous = bas - t.y - 2 * MARGE_Y
            # (Chaque essai : place, tailles de la plus grande à la plus petite, et à chaque taille les largeurs de
            # lettres permises : la police étroite passe avant une taille plus petite.)
            essais = ((t.l, haut, origine, origine, (100,)), (large, haut, origine, peu_reduit, CHASSES)) + (
                ((large, min(dessous, round(haut * 2.3)), origine, peu_reduit, CHASSES),)
                if dessous >= haut * 1.8 else ()) + ((large, haut, peu_reduit, self.TAILLE_MIN, CHASSES),)
        else:
            haut = max(t.h, bas - t.y - 2 * MARGE_Y)
            essais = ((t.l, haut, origine, peu_reduit, CHASSES), (t.l, haut, peu_reduit, self.TAILLE_MIN, CHASSES))
        place = None
        for largeur_dispo, hauteur_dispo, plus_grande, plus_petite, chasses in essais:
            for taille in range(plus_grande, plus_petite - 1, -1):
                for chasse in chasses:
                    largeur, hauteur, lignes = mesurer(traduit, taille, largeur_dispo, chasse, gras)
                    if hauteur <= hauteur_dispo + 2 and largeur <= largeur_dispo + 1:
                        place = (largeur, hauteur, lignes, taille, chasse)
                        break
                if place:
                    break
            if place:
                break
        if not place:
            return None  # vraiment pas la place : mieux vaut laisser l'original lisible
        largeur, hauteur, lignes, taille, chasse = place

        if self.style == "auto" and teintes:
            fond, encre = teintes[:2]
        else:
            fond, encre = STYLES["sombre" if self.style == "sombre" else "clair"]
        # La vitre couvre toujours tout le texte d'origine.
        l = max(largeur, t.l) + 2 * MARGE_X
        h = max(hauteur, t.h) + 2 * MARGE_Y
        x, y = t.x - MARGE_X, t.y - MARGE_Y
        if l > t.l + 2 * MARGE_X:
            if centre:
                x = t.x + t.l // 2 - l // 2
            x = max(gauche, min(x, droite - l))
        # Ne jamais sortir de la fenêtre d'origine.
        fx1, fy1, fx2, fy2 = t.fenetre
        x, y = max(fx1, x), max(fy1, y)
        l, h = int(min(l, fx2 - x)), int(min(h, fy2 - y))
        if l < 8 or h < 6:
            return None
        if any(x < c and a < x + l and y < d and b < y + h for a, b, c, d in devant):
            return None  # une fenêtre est posée dessus : la traduction flotterait par-dessus elle
        return tuple(lignes), taille, int(x), int(y), l, h, tuple(fond), tuple(encre), centre, chasse, gras


# --------------------------------------------------------------------------- extension du navigateur
#
# L'extension traduit les pages directement dans la page (rien n'est posé par-dessus) : elle demande
# les traductions à ce programme, sur ce PC seulement. Le navigateur où elle tourne est alors laissé
# de côté par la lecture de l'image (sinon il serait traduit deux fois).

PORT_EXTENSION = 47115
VERSION_LANCEMENT = str(int(time.time()))  # distingue ce lancement du programme des précédents


def navigateur_par_defaut():
    """Nom interne du navigateur par défaut de Windows (« operagxstable », « firefoxurl-… »), ou ""."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                                      r"\UrlAssociations\http\UserChoice") as cle:
            return winreg.QueryValueEx(cle, "ProgId")[0].lower()
    except OSError:
        return ""


def dossier_extension():
    """Dossier de l'extension, à côté du programme (version .exe) ou de ce fichier. Celui de la version
    pour Firefox si c'est le navigateur par défaut (et qu'elle est là : elle est faite avec le .exe)."""
    base = os.path.dirname(sys.executable) if EST_EXE else os.path.dirname(os.path.abspath(__file__))
    firefox = os.path.join(base, "extension-firefox")
    return firefox if "firefox" in navigateur_par_defaut() and os.path.isdir(firefox) else os.path.join(
        base, "extension")


_version_extension = {}


def version_extension():
    """Version de l'extension présente sur le disque (son manifeste), relue au plus une fois par minute."""
    maintenant = time.monotonic()
    if maintenant - _version_extension.get("lue_le", -1e9) > 60:
        try:
            with open(os.path.join(dossier_extension(), "manifest.json"), encoding="utf-8") as fichier:
                _version_extension["valeur"] = json.load(fichier).get("version")
        except (OSError, ValueError, AttributeError):
            _version_extension["valeur"] = None
        _version_extension["lue_le"] = maintenant
    return _version_extension["valeur"]


def navigateurs_de(agent):
    """Programmes du navigateur qui a envoyé une requête, d'après son « User-Agent »."""
    if "Firefox/" in agent:
        return frozenset({"firefox.exe"})
    if "OPR/" in agent:
        return frozenset({"opera.exe"})
    if "Edg/" in agent:
        return frozenset({"msedge.exe"})
    if "Vivaldi" in agent:
        return frozenset({"vivaldi.exe"})
    return frozenset({"chrome.exe", "brave.exe"})  # Brave se présente comme Chrome


# Pages de l'extension dans les boutiques des navigateurs (à remplir une fois publiée : voir boutiques/ETAPES.md).
# Tant qu'une page manque, l'extension s'installe depuis son dossier (mode développeur).
BOUTIQUES = {"chrome": "", "edge": "", "opera": "", "firefox": ""}


def boutique_extension():
    """Page de l'extension dans la boutique du navigateur par défaut, ou "" (pas encore publiée)."""
    nom = navigateur_par_defaut()
    famille = ("firefox" if "firefox" in nom else "opera" if "opera" in nom else "edge" if "edge" in nom
               else "chrome")
    return BOUTIQUES.get(famille, "")


def guide_extension():
    """Étapes d'installation de l'extension : [(texte, bouton ou None)], le bouton « Le dossier » ouvrant celui
    de l'extension (« Boutique » : sa page dans la boutique du navigateur, une fois publiée). Les mêmes pour Chrome,
    Edge, Opera, Brave et Vivaldi ; d'autres pour Firefox."""
    if boutique_extension():
        return [("Ouvre sa page dans la boutique de ton navigateur, puis « Ajouter ». Elle se met ensuite à jour "
                 "toute seule.", "Boutique"),
                ("Épingle-la : l'icône des extensions (pièce de puzzle ou cube) près de la barre d'adresse, "
                 "puis la punaise.", None)]
    if "firefox" in navigateur_par_defaut():
        return [("Dans le menu de Firefox : « Extensions et thèmes », la roue dentée, puis « Déboguer des modules ».",
                 None),
                ("Clique sur « Charger un module complémentaire temporaire… ».", None),
                ("Choisis le fichier « manifest.json » du dossier « extension-firefox ».", "Le dossier"),
                ("Firefox l'enlève quand il se ferme : refais ces étapes à sa prochaine ouverture.", None)]
    return [("Dans le menu de ton navigateur, ouvre « Extensions » : la page qui les gère toutes.", None),
            ("Active « Mode développeur » : l'interrupteur en haut à droite de cette page (à gauche dans Edge).",
             None),
            ("Clique sur « Charger l'extension non empaquetée » (ou « décompressée ») et choisis le dossier "
             "« extension ».", "Le dossier"),
            ("Épingle-la : l'icône des extensions (pièce de puzzle ou cube) près de la barre d'adresse, "
             "puis la punaise.", None)]


def texte_de_page_a_traduire(texte):
    """Un texte de page web envoyé par l'extension est-il à traduire ? Pas un pseudo, un nom, du
    code, des nombres (« 1,2 k vues »)."""
    # (Le japonais et le chinois n'ont pas d'espaces : leurs caractères comptent comme des séparateurs,
    # sinon « …の.zipファイル… » passerait pour un seul mot technique, un nom de fichier.)
    return (len(texte) <= 5000 and bool(A_TRADUIRE.search(texte)) and not RESSEMBLE_A_DU_CODE.search(CJK.sub(" ", texte))
            and not surtout_des_chiffres(texte) and not ressemble_a_un_pseudo(texte, lambda: False))


class RequeteExtension(BaseHTTPRequestHandler):
    """Requêtes de l'extension du navigateur. Seulement depuis ce PC, avec l'en-tête de l'extension
    et la bonne adresse : une page web ne peut ni ajouter cet en-tête, ni lire les réponses, ni se
    faire passer pour ce PC en changeant l'adresse de son site."""

    protocol_version = "HTTP/1.1"
    timeout = 10  # secondes : un client qui n'envoie rien ne garde pas un fil pour toujours

    def log_message(self, format, *args):  # pas une ligne de journal par requête
        pass

    def _repondre(self, code, donnees):
        corps = json.dumps(donnees, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _autorisee(self):
        return (self.headers.get("X-Traducteur-Ecran") == "1"
                and self.headers.get("Host") == f"127.0.0.1:{PORT_EXTENSION}")

    def do_GET(self):
        if not self._autorisee():
            self._repondre(403, {"erreur": "interdit"})
        elif self.path == "/etat":
            self._repondre(200, self.server.app.etat_extension(self.headers.get("User-Agent", "")))
        else:
            self._repondre(404, {"erreur": "inconnu"})

    def do_POST(self):
        if not self._autorisee():
            self._repondre(403, {"erreur": "interdit"})
            return
        if self.path == "/reglage":
            self._reglage()
            return
        if self.path != "/traduire":
            self._repondre(404, {"erreur": "inconnu"})
            return
        try:
            taille = int(self.headers.get("Content-Length", "0"))
            if not 0 < taille <= 2_000_000:
                raise ValueError
            textes = json.loads(self.rfile.read(taille))["textes"]
            if not isinstance(textes, list) or len(textes) > 200 or not all(isinstance(t, str) for t in textes):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self._repondre(400, {"erreur": "requête invalide"})
            return
        try:
            reponse = self.server.app.traduire_pour_extension(textes, self.headers.get("User-Agent", ""))
        except Exception:
            log.exception("Traduction pour l'extension impossible")
            self._repondre(500, {"erreur": "traduction impossible"})
            return
        self._repondre(200, reponse)

    def _reglage(self):
        """La fenêtre de l'extension règle le programme : traduction en marche ou en pause, langue voulue
        (les deux restent ainsi les mêmes partout)."""
        try:
            taille = int(self.headers.get("Content-Length", "0"))
            if not 0 < taille <= 1000:
                raise ValueError
            demande = json.loads(self.rfile.read(taille))
            actif, langue, fenetre = demande.get("actif"), demande.get("langue"), demande.get("fenetre")
            if (actif is not None and not isinstance(actif, bool)) or (langue is not None and langue not in NOM_PAR_CODE) \
                    or (fenetre is not None and not isinstance(fenetre, bool)):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            self._repondre(400, {"erreur": "requête invalide"})
            return
        app = self.server.app
        if actif is not None:
            app.commandes.put(("activer", actif))
        if langue is not None:
            app.commandes.put(("langue", langue))
        if fenetre:
            app.commandes.put(("reglages",))  # « Ouvrir le traducteur »
        etat =app.etat_extension(self.headers.get("User-Agent", ""))
        self._repondre(200, {**etat, **({"actif": actif} if actif is not None else {}),
                             **({"langue": langue} if langue is not None else {})})


# --------------------------------------------------------------------------- neumorphisme
#
# Style de la fenêtre : tout est de la couleur du fond, en relief (ombre claire en haut à gauche, sombre en bas à
# droite) ou creusé. customtkinter ne dessine pas d'ombres : ce sont des images (PIL), refaites à la taille de chaque
# élément. Clair ou sombre, comme Windows (customtkinter choisit l'image du mode en cours).

FOND = ("#E4E9F0", "#252831")
FOND_CREUX = ("#DCE2EA", "#1F2129")  # champs de saisie, listes
STYLE_BOUTON = dict(fg_color=FOND, hover_color=("#EDF1F6", "#2C3039"), text_color=ACCENT, corner_radius=10,
                    font=("Segoe UI", 12, "bold"))
STYLE_INTERRUPTEUR = dict(progress_color=ACCENT, fg_color=("#C9D1DC", "#17191F"), button_color=("#FFFFFF", "#E6E9F0"),
                          button_hover_color=("#FFFFFF", "#FFFFFF"))
TEXTE = ("#2F3645", "#E6E9F0")
OMBRES = {False: ((255, 255, 255, 255), (163, 177, 198, 150)),  # mode clair : (lumière, ombre)
          True: ((52, 57, 70, 255), (14, 15, 19, 255))}  # mode sombre


@functools.lru_cache(maxsize=256)
def image_relief(largeur, hauteur, rayon, creux, sombre, marge, decalage, flou):
    """Les ombres d'un élément (pixels réels, marge pour l'ombre comprise) : en relief, ou creusé. Dessinées en
    double taille puis réduites : bords nets."""
    s = 2
    l, h, r, m, d, f = largeur * s, hauteur * s, rayon * s, marge * s, decalage * s, flou * s
    fond = FOND[sombre]
    lumiere, ombre = OMBRES[sombre]
    image = Image.new("RGBA", (l, h), fond)
    if l - 2 * m < 2 * r + 2 or h - 2 * m < 2 * r + 2:
        return image.resize((largeur, hauteur))  # (trop petit, le temps de la mise en place : pas d'ombre)
    cadre = (m, m, l - m, h - m)
    if not creux:
        for couleur, dx in ((ombre, d), (lumiere, -d)):  # (chacune à part : l'ombre ne cache pas la lumière)
            calque = Image.new("RGBA", (l, h), couleur[:3] + (0,))
            ImageDraw.Draw(calque).rounded_rectangle(tuple(v + dx for v in cadre), r, fill=couleur)
            image.alpha_composite(calque.filter(ImageFilter.GaussianBlur(f)))
        ImageDraw.Draw(image).rounded_rectangle(cadre, r, fill=fond)
    else:
        masque = Image.new("L", (l, h), 0)
        ImageDraw.Draw(masque).rounded_rectangle(cadre, r, fill=255)
        vide = Image.new("RGBA", (l, h), (0, 0, 0, 0))
        x1, y1, x2, y2 = cadre
        loin = l + h  # (le trou s'étend loin du côté opposé : chaque ombre reste de son côté, sans déborder)
        for couleur, trou in ((ombre if sombre else ombre[:3] + (ombre[3] // 2,), (x1 + d, y1 + d, x2 + loin, y2 + loin)),
                              (lumiere, (x1 - loin, y1 - loin, x2 - d, y2 - d))):
            calque = Image.new("RGBA", (l, h), couleur)
            # (Trou transparent de la même couleur : le flou ne grise pas la lumière en la mêlant à du noir.)
            ImageDraw.Draw(calque).rounded_rectangle(trou, r, fill=couleur[:3] + (0,))
            image.alpha_composite(Image.composite(calque.filter(ImageFilter.GaussianBlur(f * 0.6)), vide, masque))
    return image.resize((largeur, hauteur), Image.LANCZOS)


def melanger_couleurs(a, b, t):
    """La couleur à t (0 à 1) entre a et b (« #RRGGBB »)."""
    a, b = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def en_douceur(t):
    return 1 - (1 - t) ** 3  # (rapide au début, ralentit à la fin)


def suivre_survol(dehors, morceaux, entree, sortie):
    """entree() quand la souris arrive sur `dehors` (ou l'un de ses `morceaux`), sortie() quand elle en sort
    vraiment : passer d'un morceau à l'autre ne compte pas (pas d'animation qui repart à chaque fois)."""
    dedans = [False]

    def arrivee(_e):
        if not dedans[0]:
            dedans[0] = True
            entree()

    def verifier():
        x, y = dehors.winfo_pointerxy()
        gauche, haut = dehors.winfo_rootx(), dehors.winfo_rooty()
        if dedans[0] and not (gauche <= x < gauche + dehors.winfo_width() and haut <= y < haut + dehors.winfo_height()):
            dedans[0] = False
            sortie()

    for morceau in morceaux:
        morceau.bind("<Enter>", arrivee, add="+")
        morceau.bind("<Leave>", lambda _e: dehors.after(30, verifier), add="+")


def animer_bouton(bouton):
    """Survol d'un bouton en fondu (la couleur glisse vers celle du survol) au lieu d'un changement d'un coup."""
    normal, survol = bouton.cget("fg_color"), bouton.cget("hover_color")
    if normal == "transparent" or getattr(bouton, "_survol_anime", False):
        return
    paires = [(n, s) for n, s in zip(*(c if isinstance(c, (tuple, list)) else (c, c) for c in (normal, survol)))]
    if not all(isinstance(c, str) and c.startswith("#") and len(c) == 7 for paire in paires for c in paire):
        return
    bouton._survol_anime = True
    bouton.configure(hover=False)
    etat = {"t": 0.0, "but": 0.0, "minuterie": None}

    def avancer():
        pas = 1 / 6
        etat["t"] = min(etat["but"], etat["t"] + pas) if etat["but"] > etat["t"] else max(etat["but"], etat["t"] - pas)
        t = en_douceur(etat["t"])
        bouton.configure(fg_color=tuple(melanger_couleurs(n, s, t) for n, s in paires))
        etat["minuterie"] = bouton.after(20, avancer) if etat["t"] != etat["but"] else None

    def aller(but):
        etat["but"] = but
        if etat["minuterie"] is None:
            avancer()

    suivre_survol(bouton, [bouton] + list(bouton.winfo_children()), lambda: aller(1.0), lambda: aller(0.0))


def enfoncer(relief, bouton):
    """Un bouton posé sur un relief : il s'enfonce quand on appuie, et remonte quand on lâche."""
    for morceau in [bouton] + list(bouton.winfo_children()):
        morceau.bind("<ButtonPress-1>", lambda _e: relief.changer("creux", 0.8), add="+")
        morceau.bind("<ButtonRelease-1>", lambda _e: relief.changer("relief"), add="+")


def apparaitre(fenetre, duree=160):
    """Montre la fenêtre en fondu (transparente, puis de plus en plus opaque)."""
    fenetre.attributes("-alpha", 0.0)
    fenetre.deiconify()

    def etape(numero, etapes=8):
        fenetre.attributes("-alpha", en_douceur(numero / etapes))
        if numero < etapes:
            fenetre.after(duree // etapes, etape, numero + 1)

    etape(1)


def animer_boutons(widget):
    """Tous les boutons de cette fenêtre : survol en fondu."""
    for enfant in widget.winfo_children():
        if isinstance(enfant, ctk.CTkButton):
            animer_bouton(enfant)
        animer_boutons(enfant)


class Relief(ctk.CTkFrame):
    """Un cadre en relief, creusé ou plat (`etat`, plus ou moins marqué selon `force`) : ses ombres sont une image
    sous son contenu, refaite à sa taille. Un changement d'état se fait en fondu (quelques images intermédiaires).
    Le contenu se met dans `.dedans` (assez loin des bords pour ne pas cacher les ombres)."""

    DUREE = 150  # ms d'une transition
    ETAPES = 8

    def __init__(self, parent, etat="relief", rayon=14, marge=None, decalage=3, flou=4, interieur=None):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        if marge is None:
            marge = decalage + 2 * flou  # (toute l'ombre tient dans la marge : jamais coupée net)
        self.etat, self.force = etat, 1.0
        self.rayon, self.marge, self.decalage, self.flou = rayon, marge, decalage, flou
        # (Posée sous le contenu sans compter dans la taille du cadre : seul le contenu décide de la taille.)
        self.fond = ctk.CTkLabel(self, text="", fg_color="transparent", width=1, height=1)
        self.fond.place(x=0, y=0, relwidth=1, relheight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        retrait = marge + (rayon // 2 if interieur is None else interieur)
        if etat == "creux":  # (le contenu commence après l'ombre intérieure : il ne la coupe pas en angle)
            retrait = max(retrait, marge + decalage + round(1.2 * flou) + 1)
        self.dedans = ctk.CTkFrame(self, fg_color=FOND, corner_radius=0)
        self.dedans.grid(row=0, column=0, sticky="nsew", padx=retrait, pady=retrait)  # (créé après : au-dessus)
        self._taille, self._images, self._animation = None, None, None
        self.bind("<Configure>", lambda _e: self.redessiner())

    def _images_de(self, etat, force):
        """(image claire, image sombre) de cet état, à la taille actuelle ; None tant que la taille est inconnue."""
        largeur, hauteur = self.winfo_width(), self.winfo_height()
        if largeur < 4 or hauteur < 4:
            return None
        plates = [Image.new("RGBA", (largeur, hauteur), FOND[sombre]) for sombre in (False, True)]
        if etat == "plat" or force <= 0:
            return plates
        echelle = self._get_widget_scaling()
        images = [image_relief(largeur, hauteur, round(self.rayon * echelle), etat == "creux", sombre,
                               round(self.marge * echelle), round(self.decalage * echelle),
                               round(self.flou * echelle)) for sombre in (False, True)]
        return images if force >= 1 else [Image.blend(p, i, force) for p, i in zip(plates, images)]

    def _poser(self, images):
        self._images = images
        echelle = self._get_widget_scaling()
        largeur, hauteur = images[0].size
        self._image = ctk.CTkImage(images[0], images[1], size=(int(largeur / echelle), int(hauteur / echelle)))
        self.fond.configure(image=self._image)

    def changer(self, etat, force=1.0, anime=True):
        """Passe à cet état (en fondu, sauf `anime=False`)."""
        if (etat, force) == (self.etat, self.force):
            return
        self.etat, self.force = etat, force
        if self._animation:
            self.after_cancel(self._animation)
            self._animation = None
        cible = self._images_de(etat, force)
        if cible is None:
            return
        depart = self._images
        if not anime or depart is None or depart[0].size != cible[0].size:
            self._poser(cible)
            return

        def etape(numero):
            t = en_douceur(numero / self.ETAPES)
            self._poser([Image.blend(d, c, t) for d, c in zip(depart, cible)])
            self._animation = self.after(self.DUREE // self.ETAPES, etape, numero + 1) if numero < self.ETAPES else None

        etape(1)

    def redessiner(self):
        taille = (self.winfo_width(), self.winfo_height())
        if taille == self._taille or self._animation:
            return
        images = self._images_de(self.etat, self.force)
        if images is not None:
            self._taille = taille
            self._poser(images)


class ChoixRelief(Relief):
    """Plusieurs choix dans un rail creusé ; le choix en cours est en relief (remplace CTkSegmentedButton)."""

    def __init__(self, parent, valeurs, variable, commande):
        super().__init__(parent, etat="creux", rayon=14, marge=4, decalage=2, flou=3, interieur=4)
        self.variable, self.commande, self.options = variable, commande, {}
        for colonne, valeur in enumerate(valeurs):
            self.dedans.grid_columnconfigure(colonne, weight=1, uniform="choix")
            option = Relief(self.dedans, etat="plat", rayon=11, decalage=1, flou=2, interieur=1)
            option.grid(row=0, column=colonne, sticky="ew")
            etiquette = ctk.CTkLabel(option.dedans, text=valeur, height=26, font=ctk.CTkFont("Segoe UI", 12))
            etiquette.pack(fill="x", padx=4)
            morceaux = (option, option.fond, option.dedans, etiquette)
            for morceau in morceaux:
                morceau.bind("<Button-1>", lambda _e, v=valeur: self._choisir(v))
            suivre_survol(option, morceaux, lambda v=valeur: self._survoler(v, True),
                          lambda v=valeur: self._survoler(v, False))
            self.options[valeur] = (option, etiquette)
        variable.trace_add("write", lambda *_: self._afficher())
        self._afficher()

    def _choisir(self, valeur):
        if valeur != self.variable.get():
            self.variable.set(valeur)
            self.commande(valeur)

    def _survoler(self, valeur, dessus):
        """Au survol, un choix (pas celui en cours) ressort un peu."""
        if valeur != self.variable.get():
            self.options[valeur][0].changer("relief", 0.45) if dessus else self.options[valeur][0].changer("plat")

    def _afficher(self):
        for valeur, (option, etiquette) in self.options.items():
            choisi = valeur == self.variable.get()
            option.changer("relief" if choisi else "plat")
            etiquette.configure(text_color=ACCENT if choisi else ("#7A8499", "#8C93A6"),
                                font=ctk.CTkFont("Segoe UI", 12, "bold" if choisi else "normal"))


# --------------------------------------------------------------------------- fenêtre principale

class Navigation:
    """Barre de navigation à gauche (une icône et un nom par rubrique), la rubrique choisie à droite.
    S'utilise comme les onglets de customtkinter : add(nom) -> page, get(), set(nom), tab(nom)."""

    ICONES = {"Accueil": "", "Langues": "", "Apparence": "", "Plus": ""}  # (police « Segoe MDL2 Assets »)
    DESCRIPTIONS = {"Accueil": "L'essentiel, en un coup d'œil",
                    "Langues": "Ce que tu lis, ce que tu écris, et tes mots",
                    "Apparence": "L'allure des traductions",
                    "Plus": "Où traduire, sites internet, raccourcis, historique"}
    GRIS = ("#7A8499", "#8C93A6")

    def __init__(self, barre, zone, titre, description, commande=None):
        self.barre, self.zone, self.titre, self.description = barre, zone, titre, description
        self.commande = commande
        self.pages, self.elements = {}, {}
        self.actif = None

    def add(self, nom):
        page = ctk.CTkFrame(self.zone, fg_color="transparent")
        element = Relief(self.barre, etat="creux", rayon=12, decalage=2, flou=3, interieur=3)
        element.changer("plat", anime=False)  # (marges d'un élément creusé : la rubrique choisie le devient sans bouger)
        element.pack(fill="x", padx=8, pady=1)
        icone = ctk.CTkLabel(element.dedans, text=self.ICONES.get(nom, ""), width=26,
                             font=ctk.CTkFont("Segoe MDL2 Assets", 15))
        icone.pack(side="left", padx=(8, 6))
        texte = ctk.CTkLabel(element.dedans, text=nom, font=ctk.CTkFont("Segoe UI", 13), anchor="w")
        texte.pack(side="left", fill="x", expand=True)
        morceaux = (element, element.fond, element.dedans, icone, texte)
        for morceau in morceaux:
            morceau.bind("<Button-1>", lambda _e, n=nom: self._cliquer(n))
        suivre_survol(element, morceaux, lambda n=nom: self._survoler(n, True), lambda n=nom: self._survoler(n, False))
        self.pages[nom], self.elements[nom] = page, (element, icone, texte)
        if self.actif is None:
            self.set(nom)
        return page

    def _cliquer(self, nom):
        self.set(nom)
        if self.commande:
            self.commande()

    def _survoler(self, nom, dessus):
        if nom != self.actif:
            element = self.elements[nom][0]
            element.changer("relief", 0.5) if dessus else element.changer("plat")
            for etiquette in self.elements[nom][1:]:
                etiquette.configure(text_color=TEXTE if dessus else self.GRIS)

    def set(self, nom):
        if self.actif:
            self.pages[self.actif].pack_forget()
        self.actif = nom
        self.pages[nom].pack(fill="both", expand=True)
        self.ajuster_hauteur()
        for autre, (element, icone, texte) in self.elements.items():
            choisi = autre == nom
            element.changer("creux" if choisi else "plat")
            for etiquette in (icone, texte):
                etiquette.configure(text_color=ACCENT if choisi else self.GRIS)
            texte.configure(font=ctk.CTkFont("Segoe UI", 13, "bold" if choisi else "normal"))
        self.titre.configure(text=nom)
        self.description.configure(text=self.DESCRIPTIONS.get(nom, ""))

    def get(self):
        return self.actif

    def ajuster_hauteur(self):
        """La zone prend la hauteur de la rubrique la plus haute : rien n'est coupé en bas, même avec une
        grande taille de texte de Windows ou beaucoup de langues. Elle ne rétrécit pas (la fenêtre ne saute pas)."""
        self.zone.update_idletasks()
        besoin = max(page.winfo_reqheight() for page in self.pages.values()) / self.zone._get_widget_scaling()
        if besoin + 4 > self.zone.cget("height"):
            self.zone.configure(height=int(besoin) + 4)

    def tab(self, nom):
        return self.pages[nom]


class SelecteurZone:
    """Choisir une zone à traduire : tout l'écran s'assombrit, on trace un rectangle à la souris (sa zone reste nette).
    Échap, clic droit ou simple clic : annulé. fini((x1, y1, x2, y2)) en pixels de l'écran, ou fini(None)."""

    TRANSPARENT = "#FF00FE"  # couleur rendue invisible : l'intérieur du rectangle tracé reste net

    def __init__(self, root, fini):
        self.fini = fini
        gauche, haut = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # tous les écrans ensemble
        largeur, hauteur = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.4)
        self.win.attributes("-transparentcolor", self.TRANSPARENT)
        self.win.geometry(f"{largeur}x{hauteur}+{gauche}+{haut}")
        self.toile = tk.Canvas(self.win, bg="#000000", highlightthickness=0, cursor="crosshair")
        self.toile.pack(fill="both", expand=True)
        self.depart = None  # (écran, toile) au début du tracé
        self.rectangle = None
        self.toile.bind("<ButtonPress-1>", self._debut)
        self.toile.bind("<B1-Motion>", self._glisser)
        self.toile.bind("<ButtonRelease-1>", self._fin)
        self.toile.bind("<ButtonPress-3>", lambda _e: self._fermer(None))
        self.win.bind("<Escape>", lambda _e: self._fermer(None))
        self.win.after(20, self._premier_plan)

    def _premier_plan(self):
        self.win.lift()
        self.win.focus_force()  # (Échap doit arriver ici)

    def _debut(self, e):
        self.depart = ((e.x_root, e.y_root), (e.x, e.y))

    def _glisser(self, e):
        if self.depart is None:
            return
        (_, (x, y)) = self.depart
        if self.rectangle is None:
            self.rectangle = self.toile.create_rectangle(x, y, e.x, e.y, fill=self.TRANSPARENT, outline="#60A5FA",
                                                         width=2)
        else:
            self.toile.coords(self.rectangle, x, y, e.x, e.y)

    def _fin(self, e):
        if self.depart is None:
            return self._fermer(None)
        (x, y), _ = self.depart
        x1, x2 = sorted((x, e.x_root))
        y1, y2 = sorted((y, e.y_root))
        self._fermer((x1, y1, x2, y2) if x2 - x1 >= 16 and y2 - y1 >= 10 else None)  # (un simple clic : annulé)

    def _fermer(self, zone):
        try:
            self.win.destroy()
        finally:
            self.fini(zone)


class Reglages:
    """Fenêtre principale : à gauche la navigation, à droite la rubrique choisie. Quatre rubriques : l'accueil
    (marche / pause, langues, qualité, raccourcis), les langues, l'apparence, et « Plus » (le reste)."""

    GRIS = ("#7A8499", "#8C93A6")
    CARTE = FOND

    def __init__(self, root, app):
        self.app = app
        self.win = root
        root.title("Traducteur d'écran")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.cacher)
        root.configure(fg_color=FOND)
        try:
            root.iconbitmap(ICONE_PATH)
        except tk.TclError:
            pass

        # À gauche : le logo, la navigation, les raccourcis et « Quitter ».
        barre = ctk.CTkFrame(root, width=212, corner_radius=0, fg_color=FOND)
        barre.pack(side="left", fill="y")
        barre.pack_propagate(False)
        entete = ctk.CTkFrame(barre, fg_color="transparent")
        entete.pack(fill="x", padx=16, pady=(20, 18))
        self.logo = ctk.CTkImage(dessiner_icone(128), size=(38, 38))
        ctk.CTkLabel(entete, image=self.logo, text="").pack(side="left")
        titres = ctk.CTkFrame(entete, fg_color="transparent")
        titres.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(titres, text="Traducteur", font=ctk.CTkFont("Segoe UI", 16, "bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(titres, text="d'écran", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                     anchor="w").pack(fill="x")
        pied = ctk.CTkFrame(barre, fg_color="transparent")
        pied.pack(side="bottom", fill="x", padx=16, pady=(0, 18))
        self.aide = ctk.CTkLabel(pied, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 11),
                                 justify="left", anchor="w", wraplength=176)
        self.aide.pack(fill="x", pady=(0, 10))
        self._bouton_discret(pied, "Quitter le traducteur", app.quitter, 180).pack(fill="x")

        # À droite : la rubrique choisie, son titre puis son contenu.
        corps = ctk.CTkFrame(root, fg_color="transparent")
        corps.pack(side="left", fill="both", expand=True, padx=26, pady=(20, 22))
        titre = ctk.CTkLabel(corps, text="", font=ctk.CTkFont("Segoe UI", 19, "bold"), anchor="w")
        titre.pack(fill="x")
        description = ctk.CTkLabel(corps, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12), anchor="w")
        description.pack(fill="x")
        zone = ctk.CTkFrame(corps, width=460, height=470, fg_color="transparent")
        zone.pack(fill="both", expand=True, pady=(10, 0))
        zone.pack_propagate(False)
        onglets = Navigation(barre, zone, titre, description, self._onglet_change)
        self.onglets = onglets
        self._onglet_accueil(onglets.add("Accueil"))
        self._onglet_langue(onglets.add("Langues"))
        self._onglet_apparence(onglets.add("Apparence"))
        self._onglet_plus(onglets.add("Plus"))
        self.rafraichir()
        animer_boutons(root)

    # ---- onglets

    def _onglet_langue(self, onglet):
        app = self.app
        self._titre(onglet, "Je lis en", 4)
        self.langue = ctk.StringVar()
        ctk.CTkOptionMenu(
            onglet, values=list(LANGUES), variable=self.langue, height=36,
            font=ctk.CTkFont("Segoe UI", 14), dropdown_font=ctk.CTkFont("Segoe UI", 13),
            fg_color=ACCENT, button_color=ACCENT_SURVOL, button_hover_color=ACCENT_FONCE,
            command=lambda nom: app.choisir_langue(LANGUES[nom]),
        ).pack(fill="x", padx=6, pady=(6, 0))
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(14, 0))
        textes = ctk.CTkFrame(ligne, fg_color="transparent")
        textes.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(textes, text="Langues que je veux comprendre", font=ctk.CTkFont("Segoe UI", 13, "bold"),
                     anchor="w").pack(fill="x")
        self.liste_langues = ctk.CTkLabel(textes, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                                          anchor="w", justify="left", wraplength=290)
        self.liste_langues.pack(fill="x")
        self._bouton_discret(ligne, "Choisir…", app.ouvrir_choix_langues, 90).pack(side="right", anchor="n")
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(6, 0))
        self.etat_langues = ctk.CTkLabel(ligne, text="", font=ctk.CTkFont("Segoe UI", 12), anchor="w",
                                         justify="left", wraplength=290)
        self.etat_langues.pack(side="left", fill="x", expand=True)
        self.action_langues = ctk.CTkButton(ligne, text="", width=90, height=26, font=ctk.CTkFont("Segoe UI", 12),
                                            fg_color=ACCENT, hover_color=ACCENT_SURVOL)
        self._ligne_etat_langues = ligne
        self.barre_langues = ctk.CTkProgressBar(onglet, height=6, progress_color=ACCENT)  # pendant un téléchargement
        self._langues_affichees = None
        self._note(onglet, "Seules ces langues sont traduites, hors ligne, sur ton PC. Jamais traduits : les "
                           "pseudos, les nombres, le code, le texte des images et des vidéos.")
        self._onglet_ecrire(onglet)

    def _onglet_accueil(self, onglet):
        """L'essentiel sur une page : marche / pause, les langues, la qualité, les raccourcis, l'extension."""
        app = self.app
        # Nouvelle version disponible (cachée tant qu'il n'y en a pas : voir montrer_mise_a_jour).
        self.carte_maj = ctk.CTkFrame(onglet, corner_radius=12, fg_color=(ACCENT, ACCENT))
        self.texte_maj = ctk.CTkLabel(self.carte_maj, text="", text_color="#FFFFFF", anchor="w",
                                      font=ctk.CTkFont("Segoe UI", 13, "bold"))
        self.texte_maj.pack(side="left", fill="x", expand=True, padx=(14, 6), pady=8)
        ctk.CTkButton(self.carte_maj, text="Mettre à jour", width=120, height=28, fg_color="#FFFFFF",
                      text_color=ACCENT, hover_color="#DBEAFE", font=ctk.CTkFont("Segoe UI", 12, "bold"),
                      command=lambda: self.app.commandes.put(("installer_mise_a_jour",))).pack(side="right", padx=(0, 10))
        # Marche / pause, en grand.
        carte = self.carte_marche = Relief(onglet, rayon=18)
        carte.pack(fill="x", pady=(0, 4))
        ligne = ctk.CTkFrame(carte.dedans, fg_color="transparent")
        ligne.pack(fill="x", padx=8, pady=4)
        textes = ctk.CTkFrame(ligne, fg_color="transparent")
        textes.pack(side="left", fill="x", expand=True)
        self.titre_marche = ctk.CTkLabel(textes, text="", font=ctk.CTkFont("Segoe UI", 16, "bold"), anchor="w")
        self.titre_marche.pack(fill="x")
        self.etat = ctk.CTkLabel(textes, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12), anchor="w")
        self.etat.pack(fill="x")
        self.interrupteur = ctk.CTkSwitch(ligne, text="", command=app.basculer, width=58, switch_width=52,
                                          switch_height=28, **STYLE_INTERRUPTEUR)
        self.interrupteur.pack(side="right")
        # Les langues en un coup d'œil : « Anglais, Japonais  →  Français ».
        bandeau = self.bandeau_langues = ctk.CTkFrame(onglet, fg_color="transparent")
        bandeau.pack(fill="x", padx=6, pady=(0, 6))
        self.resume_langues = ctk.CTkLabel(bandeau, text="", font=ctk.CTkFont("Segoe UI", 14), anchor="w",
                                           justify="left", wraplength=340)
        self.resume_langues.pack(side="left", fill="x", expand=True, padx=(6, 6))
        changer = Relief(bandeau, rayon=12, decalage=2, flou=3, interieur=0)
        changer.pack(side="right")
        bouton = ctk.CTkButton(changer.dedans, text="Changer", width=84, height=28,
                               command=lambda: self.onglets.set("Langues"), **STYLE_BOUTON)
        bouton.pack()
        enfoncer(changer, bouton)
        # La qualité, en un seul choix (voir Application.choisir_qualite).
        ctk.CTkLabel(onglet, text="Qualité", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                     anchor="w").pack(fill="x", padx=10)
        self.qualite = ctk.StringVar()
        self._choix(onglet, NIVEAUX_QUALITE.values(), self.qualite,
                    lambda v: app.choisir_qualite(next(c for c, nom in NIVEAUX_QUALITE.items() if nom == v)))
        self.detail_qualite = ctk.CTkLabel(onglet, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                                           anchor="w", justify="left", wraplength=430)
        self.detail_qualite.pack(fill="x", padx=10, pady=(2, 8))
        # Les trois raccourcis, en tuiles.
        tuiles = ctk.CTkFrame(onglet, fg_color="transparent")
        tuiles.pack(fill="x", pady=(0, 4))
        self.tuiles_raccourcis = {}  # action -> étiquette de la combinaison
        self._raccourcis_affiches = None  # (redessinés avec les raccourcis choisis par rafraichir)
        for colonne, (action, role) in enumerate((("pause", "Pause"), ("zone", "Traduire une zone"),
                                                   ("ecrire", "Écrire traduit"))):
            tuiles.grid_columnconfigure(colonne, weight=1, uniform="tuile")
            tuile = Relief(tuiles, rayon=16)
            tuile.grid(row=0, column=colonne, sticky="ew")
            ctk.CTkLabel(tuile.dedans, text=role, text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 11), height=18,
                         anchor="w").pack(fill="x", padx=4)
            touches = ctk.CTkLabel(tuile.dedans, text="", font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w",
                                   height=22)
            touches.pack(fill="x", padx=4)
            self.tuiles_raccourcis[action] = touches
        # Les sites internet : l'extension.
        relief = Relief(onglet, rayon=16)
        relief.pack(fill="x")
        carte = relief.dedans
        ctk.CTkLabel(carte, text="", width=30, font=ctk.CTkFont("Segoe MDL2 Assets", 17),
                     text_color=self.GRIS).pack(side="left", padx=(2, 6))
        textes = ctk.CTkFrame(carte, fg_color="transparent")
        textes.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(textes, text="Sites internet", font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w").pack(fill="x")
        self.etat_extension_accueil = ctk.CTkLabel(textes, text="", text_color=self.GRIS, anchor="w",
                                                   font=ctk.CTkFont("Segoe UI", 12))
        self.etat_extension_accueil.pack(fill="x")
        self.bouton_extension = ctk.CTkButton(carte, text="Installer", width=84, height=28, fg_color=ACCENT,
                                              hover_color=ACCENT_SURVOL, font=ctk.CTkFont("Segoe UI", 12),
                                              command=lambda: self.onglets.set("Plus"))
        self.bouton_extension.pack(side="right", padx=4)

    def montrer_mise_a_jour(self, version):
        """Une nouvelle version est publiée : la carte « Mettre à jour » apparaît en haut de l'accueil."""
        self.texte_maj.configure(text=f"Nouvelle version {version} disponible")
        if not self.carte_maj.winfo_ismapped():
            self.carte_maj.pack(fill="x", padx=6, pady=(0, 8), before=self.carte_marche)
        self.onglets.ajuster_hauteur()

    def _touches(self, parent, touches):
        """Une combinaison dessinée comme des touches de clavier : [Ctrl] + [Alt] + [T]."""
        rangee = ctk.CTkFrame(parent, fg_color="transparent")
        self._dessiner_touches(rangee, touches)
        return rangee

    def _dessiner_touches(self, rangee, touches):
        for enfant in rangee.winfo_children():
            enfant.destroy()
        for i, touche in enumerate(touches):
            if i:
                ctk.CTkLabel(rangee, text="+", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 11)).pack(side="left", padx=3)
            ctk.CTkLabel(rangee, text=touche, font=ctk.CTkFont("Segoe UI", 11, "bold"), corner_radius=6, height=22,
                         fg_color=FOND_CREUX, padx=8).pack(side="left")

    def _onglet_ecrire(self, onglet):
        """« J'écris en » (le message traduit par Ctrl+Alt+Entrée) et « Mes mots », dans la rubrique « Langues »."""
        app = self.app
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(16, 0))
        ctk.CTkLabel(ligne, text="J'écris en", font=ctk.CTkFont("Segoe UI", 13, "bold")).pack(side="left")
        self.langue_ecriture = ctk.StringVar()
        ctk.CTkOptionMenu(
            ligne, values=list(LANGUES), variable=self.langue_ecriture, width=170, height=30,
            font=ctk.CTkFont("Segoe UI", 13), dropdown_font=ctk.CTkFont("Segoe UI", 13),
            fg_color=ACCENT, button_color=ACCENT_SURVOL, button_hover_color=ACCENT_FONCE,
            command=lambda nom: app.choisir_langue_ecriture(LANGUES[nom])).pack(side="right")
        self.note_ecrire = ctk.CTkLabel(onglet, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                                        anchor="w", justify="left", wraplength=430)
        self.note_ecrire.pack(fill="x", padx=6, pady=(6, 0))

        # « Mes mots » : ajouter un mot (et sa traduction, si on veut l'imposer), voir et retirer les siens.
        self._titre(onglet, "Mes mots", 14)
        ajout = ctk.CTkFrame(onglet, fg_color="transparent")
        ajout.pack(fill="x", padx=6, pady=(6, 0))
        style = dict(height=32, font=ctk.CTkFont("Segoe UI", 12), border_width=0, corner_radius=10,
                     fg_color=FOND_CREUX)
        self.nouveau_mot = ctk.CTkEntry(ajout, width=130, placeholder_text="Mot ou nom", **style)
        self.nouveau_mot.pack(side="left")
        self.nouvelle_traduction = ctk.CTkEntry(ajout, placeholder_text="Sa traduction (vide : jamais traduit)", **style)
        self.nouvelle_traduction.pack(side="left", fill="x", expand=True, padx=(6, 6))
        ctk.CTkButton(ajout, text="Ajouter", width=76, height=32, fg_color=ACCENT, hover_color=ACCENT_SURVOL,
                      command=self._ajouter_mot).pack(side="right")
        for champ in (self.nouveau_mot, self.nouvelle_traduction):
            champ.bind("<Return>", lambda _e: self._ajouter_mot())
        # (Petite hauteur demandée : la liste s'étire sur la place libre, sans agrandir la fenêtre.)
        self.liste_mots = ctk.CTkScrollableFrame(onglet, height=60, corner_radius=12, fg_color=FOND_CREUX)
        self.liste_mots._scrollbar.configure(height=60)  # (sinon sa barre de défilement demande 200 px)
        self.liste_mots.pack(fill="both", expand=True, padx=6, pady=(8, 0))
        self._mots_affiches = None

    @staticmethod
    def _regle(ligne):
        """« mot = traduction » -> (mot, traduction) ; « mot » -> (mot, None)."""
        if "=" in ligne:
            mot, traduction = (x.strip() for x in ligne.split("=", 1))
            return mot, traduction or None
        return ligne.strip(), None

    def _ajouter_mot(self):
        mot, traduction = self.nouveau_mot.get().strip(), self.nouvelle_traduction.get().strip()
        if not mot or "=" in mot:
            return
        autres = [l for l in self.app.config["mots"] if self._regle(l)[0].casefold() != mot.casefold()]
        self.app.choisir_mots(autres + [f"{mot} = {traduction}" if traduction else mot])
        self.nouveau_mot.delete(0, "end")
        self.nouvelle_traduction.delete(0, "end")
        self.nouveau_mot.focus_set()
        self._afficher_mots()

    def _retirer_mot(self, ligne):
        self.app.choisir_mots([l for l in self.app.config["mots"] if l != ligne])
        self._afficher_mots()

    def _afficher_mots(self):
        mots = list(self.app.config["mots"])
        if mots == self._mots_affiches:
            return
        self._mots_affiches = mots
        for enfant in self.liste_mots.winfo_children():
            enfant.destroy()
        if not mots:
            ctk.CTkLabel(self.liste_mots, text="Aucun mot. Exemples : « Mayo » (jamais traduit),\n« watch streak » → "
                                               "« série de visionnage » (traduction imposée).",
                         text_color=self.GRIS, justify="left", font=ctk.CTkFont("Segoe UI", 12)).pack(anchor="w", padx=6, pady=6)
            return
        for ligne in reversed(mots):  # le dernier ajouté en haut
            mot, traduction = self._regle(ligne)
            rangee = ctk.CTkFrame(self.liste_mots, fg_color="transparent")
            rangee.pack(fill="x", pady=1)
            ctk.CTkLabel(rangee, text=mot, font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w").pack(side="left", padx=(6, 8))
            ctk.CTkLabel(rangee, text=f"→  {traduction}" if traduction else "jamais traduit", anchor="w",
                         text_color=("#30333F", "#C9CCD8") if traduction else self.GRIS,
                         font=ctk.CTkFont("Segoe UI", 12)).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(rangee, text="", width=28, height=24, font=ctk.CTkFont("Segoe MDL2 Assets", 10),
                          fg_color="transparent", hover_color=("#ECEDF2", "#2A2C3A"), text_color=self.GRIS,
                          command=lambda l=ligne: self._retirer_mot(l)).pack(side="right")

    def _onglet_plus(self, onglet):
        """Tout le reste, en sections, sur une page qui défile."""
        page = self.page_plus = ctk.CTkScrollableFrame(onglet, height=462, fg_color="transparent")
        page.pack(fill="both", expand=True)
        for titre, construire in (("Où traduire", self._onglet_programmes), ("Sites internet", self._onglet_navigateur),
                                  ("Raccourcis et démarrage", self._onglet_general), ("Historique", self._onglet_historique)):
            ctk.CTkLabel(page, text=titre, font=ctk.CTkFont("Segoe UI", 15, "bold"), anchor="w").pack(
                fill="x", padx=6, pady=(4 if titre == "Où traduire" else 24, 0))
            construire(page)

    def _onglet_historique(self, onglet):
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(4, 0))
        ctk.CTkLabel(ligne, text="Gardé seulement tant que le programme tourne, jamais enregistré.", anchor="w",
                     text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12)).pack(side="left")
        self._bouton_discret(ligne, "Effacer", self._effacer_historique, 76).pack(side="right")
        self.liste_historique = ctk.CTkFrame(onglet, corner_radius=12, fg_color=FOND_CREUX)
        self.liste_historique.pack(fill="x", padx=6, pady=(8, 0))
        self._historique_affiche = None

    HISTORIQUE_AFFICHE = 10  # lignes au plus (les plus récentes) : la page reste courte et rapide

    def afficher_historique(self):
        historique = list(self.app.historique)[-self.HISTORIQUE_AFFICHE:]
        cle = (len(self.app.historique), historique[-1] if historique else None)
        if cle == self._historique_affiche:
            return
        self._historique_affiche = cle
        for enfant in self.liste_historique.winfo_children():
            enfant.destroy()
        if not historique:
            ctk.CTkLabel(self.liste_historique, text="Rien pour l'instant : les traductions qui apparaissent à l'écran "
                                                     "s'ajoutent ici.", text_color=self.GRIS, justify="left",
                         wraplength=380, font=ctk.CTkFont("Segoe UI", 12)).pack(anchor="w", padx=6, pady=6)
            return
        for heure, original, traduit in reversed(historique):  # la plus récente en haut
            rangee = ctk.CTkFrame(self.liste_historique, fg_color="transparent")
            rangee.pack(fill="x", pady=(2, 4))
            ctk.CTkButton(rangee, text="", width=28, height=24, font=ctk.CTkFont("Segoe MDL2 Assets", 11),
                          fg_color="transparent", hover_color=("#ECEDF2", "#2A2C3A"), text_color=self.GRIS,
                          command=lambda t=traduit: self._copier(t)).pack(side="right", anchor="n")
            textes = ctk.CTkFrame(rangee, fg_color="transparent")
            textes.pack(side="left", fill="x", expand=True, padx=(6, 4))
            ctk.CTkLabel(textes, text=traduit[:400], font=ctk.CTkFont("Segoe UI", 13), anchor="w", justify="left",
                         wraplength=340).pack(fill="x")
            ctk.CTkLabel(textes, text=f"{heure} · {original[:200]}", text_color=self.GRIS, anchor="w", justify="left",
                         wraplength=340, font=ctk.CTkFont("Segoe UI", 11)).pack(fill="x")

    def _copier(self, texte):
        self.win.clipboard_clear()
        self.win.clipboard_append(texte)
        self.app.numero_presse_papiers = user32.GetClipboardSequenceNumber() + 1  # (pas retraduit par la copie)

    def _effacer_historique(self):
        self.app.historique.clear()
        self.afficher_historique()

    def _onglet_apparence(self, onglet):
        app = self.app
        self._titre(onglet, "Couleur des traductions", 4)
        self.style = ctk.StringVar()
        self._choix(onglet, NOMS_STYLES.values(), self.style,
                    lambda v: app.choisir_style(next(c for c, nom in NOMS_STYLES.items() if nom == v)))
        self._titre(onglet, "Taille du texte")
        self.taille = ctk.StringVar()
        self._choix(onglet, [nom for nom, _ in TAILLES.values()], self.taille,
                    lambda v: app.choisir_taille(next(c for c, (nom, _) in TAILLES.items() if nom == v)))
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(12, 0))
        ctk.CTkLabel(ligne, text="Opacité", font=ctk.CTkFont("Segoe UI", 13, "bold")).pack(side="left")
        self.valeur_opacite = ctk.CTkLabel(ligne, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12))
        self.valeur_opacite.pack(side="right")
        self.opacite = ctk.CTkSlider(onglet, from_=40, to=100, number_of_steps=12, command=self._changer_opacite,
                                     progress_color=ACCENT, button_color=ACCENT, button_hover_color=ACCENT_SURVOL)
        self.opacite.pack(fill="x", padx=6, pady=(6, 0))
        self.fondu = self._interrupteur(onglet, "Apparition en douceur", "Les traductions apparaissent en fondu",
                                        lambda: app.choisir_fondu(self.fondu.get() == 1))

    def _onglet_navigateur(self, onglet):
        app = self.app
        self.etat_extension = ctk.CTkLabel(onglet, text="", font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w")
        self.etat_extension.pack(fill="x", padx=6, pady=(4, 0))
        etapes = guide_extension()
        self._note(onglet, f"Elle traduit les sites directement dans la page. À installer une fois, en {len(etapes)} "
                           "étapes :")
        for numero, (texte, bouton) in enumerate(etapes, 1):
            commande = (lambda: os.startfile(boutique_extension())) if bouton == "Boutique" else self._ouvrir_dossier_extension
            self._etape(onglet, numero, texte, bouton, bouton and commande)
        # (Remplacé par ce que fait le bouton « Le dossier ».)
        self.info_extension = ctk.CTkLabel(onglet, text="Son icône affiche « ↻ » ? Clique sur ↻ dans sa carte, sur la "
                                                        "page des extensions.", text_color=self.GRIS,
                                           font=ctk.CTkFont("Segoe UI", 12), anchor="w", justify="left", wraplength=420)
        self.info_extension.pack(fill="x", padx=6, pady=(4, 0))

    def _etape(self, parent, numero, texte, bouton=None, commande=None):
        """Une étape numérotée (rond de couleur), avec son bouton s'il y a quelque chose à faire."""
        relief = Relief(parent, rayon=14, decalage=2, flou=3)
        relief.pack(fill="x", pady=(2, 0))
        ligne = relief.dedans
        ctk.CTkLabel(ligne, text=str(numero), width=24, height=24, corner_radius=12, fg_color=ACCENT,
                     text_color="#FFFFFF", font=ctk.CTkFont("Segoe UI", 12, "bold")).pack(side="left", padx=(0, 10))
        if bouton:
            ctk.CTkButton(ligne, text=bouton, width=92, height=28, fg_color=ACCENT, hover_color=ACCENT_SURVOL,
                          font=ctk.CTkFont("Segoe UI", 12), command=commande).pack(side="right", padx=(6, 0))
        ctk.CTkLabel(ligne, text=texte, anchor="w", justify="left", wraplength=255 if bouton else 355,
                     font=ctk.CTkFont("Segoe UI", 12)).pack(side="left", fill="x", expand=True)

    def _ouvrir_dossier_extension(self):
        dossier = dossier_extension()
        if os.path.isdir(dossier):
            os.startfile(dossier)
            self.win.clipboard_clear()
            self.win.clipboard_append(dossier)
            fin = ("choisis « manifest.json »" if "firefox" in navigateur_par_defaut()
                   else "« Sélectionner le dossier »")
            self.info_extension.configure(text="Le dossier s'est ouvert, et son adresse est copiée : dans la fenêtre du "
                                               f"navigateur, colle-la (Ctrl+V) en haut, puis {fin}.")
        else:
            self.info_extension.configure(text="Dossier introuvable : " + dossier)

    def _onglet_programmes(self, onglet):
        app = self.app
        self.mode_programmes = ctk.StringVar()
        self._choix(onglet, MODES_PROGRAMMES.values(), self.mode_programmes,
                    lambda v: app.choisir_mode_programmes(next(c for c, nom in MODES_PROGRAMMES.items() if nom == v)))
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(8, 0))
        self.a_ajouter = ctk.StringVar(value=self.CHOISIR)
        self.menu_programmes = ctk.CTkOptionMenu(
            ligne, values=[self.CHOISIR], variable=self.a_ajouter, height=32, dynamic_resizing=False,
            font=ctk.CTkFont("Segoe UI", 13), dropdown_font=ctk.CTkFont("Segoe UI", 13),
            fg_color=FOND_CREUX, button_color=FOND_CREUX,
            button_hover_color=("#D5D8E3", "#3D4057"), text_color=("#30333F", "#E4E6EE"))
        self.menu_programmes.pack(side="left", fill="x", expand=True)
        self._bouton_discret(ligne, "Parcourir…", self._parcourir, 92).pack(side="right", padx=(6, 0))
        ctk.CTkButton(ligne, text="Ajouter", width=76, height=32, command=self._ajouter_programme,
                      fg_color=ACCENT, hover_color=ACCENT_SURVOL).pack(side="right", padx=(6, 0))
        self.liste_programmes = ctk.CTkFrame(onglet, corner_radius=12, fg_color=FOND_CREUX)
        self.liste_programmes.pack(fill="x", padx=6, pady=(8, 0))
        self._liste_affichee = None

    def _onglet_general(self, onglet):
        self.demarrage = self._interrupteur(onglet, "Lancer au démarrage de Windows",
                                            "Démarre discrètement, près de l'horloge", self._changer_demarrage, 4)
        # Tous les raccourcis, dessinés comme des touches, chacun avec son bouton « Changer ».
        self._titre(onglet, "Raccourcis clavier", 14)
        relief = Relief(onglet, rayon=14)
        relief.pack(fill="x", pady=(2, 0))
        carte = relief.dedans
        carte.grid_columnconfigure(1, weight=1)
        self.lignes_raccourcis = {}  # action -> (touches dessinées, rôle, bouton « Changer »)
        for i, (action, (cle, _, role)) in enumerate(RACCOURCIS.items()):
            haut, bas = (8 if i == 0 else 2), (8 if i == len(RACCOURCIS) - 1 else 2)
            rangee = self._touches(carte, noms_des_touches(self.app.config[cle]))
            rangee.grid(row=i, column=0, sticky="w", padx=(12, 10), pady=(haut, bas))
            etiquette = ctk.CTkLabel(carte, text=role, text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                                     anchor="w", justify="left", wraplength=135)
            etiquette.grid(row=i, column=1, sticky="w", pady=(haut, bas))
            bouton = self._bouton_discret(carte, "Changer", lambda a=action: self._changer_raccourci(a), 70)
            bouton.configure(height=26)
            bouton.grid(row=i, column=2, padx=(6, 10), pady=(haut, bas))
            self.lignes_raccourcis[action] = (rangee, etiquette, bouton)
        self._note(onglet, "Les captures d'écran montrent alors les traductions. « Changer » puis Suppr : aucun "
                           "raccourci.")
        self._titre(onglet, "Réactivité", 14)
        self.reactivite = ctk.StringVar()
        self._choix(onglet, [nom for nom, _, _ in REACTIVITES.values()], self.reactivite,
                    lambda v: self.app.choisir_reactivite(next(c for c, r in REACTIVITES.items() if r[0] == v)))
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(14, 0))
        self._bouton_discret(ligne, "Oublier les traductions mémorisées", self._oublier, 0).pack(side="left")
        self.info_memoire = ctk.CTkLabel(ligne, text="", text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12))
        self.info_memoire.pack(side="left", padx=(10, 0))
        ligne = ctk.CTkFrame(onglet, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(8, 0))
        self._bouton_discret(ligne, "Signaler un problème", self.app.signaler_probleme, 0).pack(side="left")
        ctk.CTkLabel(ligne, text="sur GitHub", text_color=self.GRIS,
                     font=ctk.CTkFont("Segoe UI", 12)).pack(side="left", padx=(10, 0))

    # ---- éléments de la fenêtre

    CHOISIR = "Choisir un programme ouvert…"

    def _titre(self, parent, texte, haut=12):
        ctk.CTkLabel(parent, text=texte, font=ctk.CTkFont("Segoe UI", 13, "bold"), anchor="w").pack(
            fill="x", padx=6, pady=(haut, 0))

    def _note(self, parent, texte):
        ctk.CTkLabel(parent, text=texte, text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12), anchor="w",
                     justify="left", wraplength=390).pack(fill="x", padx=6, pady=(8, 0))

    def _choix(self, parent, valeurs, variable, commande):
        ChoixRelief(parent, list(valeurs), variable, commande).pack(fill="x", padx=2, pady=(4, 0))

    def _interrupteur(self, parent, titre, detail, commande, haut=14, avec_detail=False):
        """Une ligne « titre + explication » avec son interrupteur à droite (et son explication, si elle change)."""
        ligne = ctk.CTkFrame(parent, fg_color="transparent")
        ligne.pack(fill="x", padx=6, pady=(haut, 0))
        textes = ctk.CTkFrame(ligne, fg_color="transparent")
        textes.pack(side="left")
        ctk.CTkLabel(textes, text=titre, font=ctk.CTkFont("Segoe UI", 14, "bold"), anchor="w").pack(fill="x")
        explication = ctk.CTkLabel(textes, text=detail, text_color=self.GRIS, font=ctk.CTkFont("Segoe UI", 12),
                                   anchor="w")
        explication.pack(fill="x")
        interrupteur = ctk.CTkSwitch(ligne, text="", command=commande, width=52,
                                     switch_width=46, switch_height=24, **STYLE_INTERRUPTEUR)
        interrupteur.pack(side="right")
        return (interrupteur, explication) if avec_detail else interrupteur

    def afficher_carte(self):
        """La qualité choisie, et où en sont les moyens qu'elle utilise (carte graphique, grand modèle, IA)."""
        app = self.app
        niveau = app.niveau_qualite
        self.qualite.set(NIVEAUX_QUALITE[niveau])
        moyens = [("Carte graphique", app.etat_carte), ("Grand modèle", getattr(app, "etat_grand_modele", ""))]
        moyens = {"normale": [], "meilleure": moyens,
                  "maximale": moyens + [("IA", getattr(app, "etat_ia", ""))]}[niveau]
        en_attente = []
        for nom, etat in moyens:
            etat = etat or "préparation…"
            if etat.startswith("Pas de carte NVIDIA"):
                etat = "pas de carte NVIDIA, le processeur traduit"
                if any(e.endswith(etat) for e in en_attente):
                    continue
                en_attente.append(etat[0].upper() + etat[1:])
            elif etat not in ("Active", "Actif"):
                en_attente.append(f"{nom} : {etat}")
        if niveau == "normale":
            texte, couleur = "Le processeur traduit : rien de plus à télécharger", self.GRIS
        elif en_attente:
            texte, couleur = " · ".join(en_attente), self.GRIS
        else:
            texte = ("✓ Carte graphique et grand modèle actifs" if niveau == "meilleure"
                     else "✓ Tout est actif, avec l'IA : le sens et les expressions")
            couleur = ("#1E8E3E", "#5BD17F")
        self.detail_qualite.configure(text=texte, text_color=couleur)

    def _bouton_discret(self, parent, texte, commande, largeur):
        return ctk.CTkButton(parent, text=texte, width=largeur or 260, height=32, command=commande,
                             fg_color=FOND_CREUX, border_width=0, corner_radius=10, text_color=TEXTE,
                             hover_color=("#EDF1F6", "#2C3039"))

    # ---- état affiché

    def actualiser(self, nombre=None):
        """État de la traduction en direct (appelé souvent : seulement l'en-tête)."""
        app = self.app
        (self.interrupteur.select if app.actif else self.interrupteur.deselect)()
        self.titre_marche.configure(text="Traduction en marche" if app.actif else "Traduction en pause")
        if not app.actif:
            texte = "Ton écran garde son texte d'origine"
        elif not nombre:
            texte = "Ton écran est traduit tout seul"
        else:
            pluriel = "s" if nombre > 1 else ""
            texte = f"{nombre} texte{pluriel} traduit{pluriel} à l'écran"
        self.etat.configure(text=texte)

    def rafraichir(self):
        """Tous les réglages, tels qu'ils sont enregistrés."""
        config = self.app.config
        self.actualiser()
        (self.demarrage.select if demarrage_actif() else self.demarrage.deselect)()
        self.langue.set(NOM_PAR_CODE[config["langue_cible"]])
        self.langue_ecriture.set(NOM_PAR_CODE[config["langue_ecriture"]])
        self._afficher_mots()
        self.style.set(NOMS_STYLES[config["style"]])
        self.taille.set(TAILLES[config["taille"]][0])
        self.opacite.set(config["opacite"])
        self.valeur_opacite.configure(text=f"{config['opacite']} %")
        (self.fondu.select if config["fondu"] else self.fondu.deselect)()
        self.mode_programmes.set(MODES_PROGRAMMES[config["mode_programmes"]])
        app = self.app
        app._extension_affichee = app.navigateurs_extension()
        if app._extension_affichee:
            noms = {"opera.exe": "Opera", "msedge.exe": "Edge", "vivaldi.exe": "Vivaldi", "firefox.exe": "Firefox"}
            navigateurs = sorted({noms.get(p, "Chrome / Brave") for p in app._extension_affichee})
            texte = (f"✓ Connectée ({navigateurs[0]}) : elle traduit ce navigateur" if len(navigateurs) == 1
                     else "✓ Connectée : " + ", ".join(navigateurs))
            self.etat_extension.configure(text=texte, text_color=("#1E8E3E", "#5BD17F"))
            self.etat_extension_accueil.configure(text="✓ Extension connectée (" + ", ".join(navigateurs) + ")")
            self.bouton_extension.pack_forget()
        else:
            self.etat_extension.configure(text="Pas encore installée (ou navigateur fermé)", text_color=self.GRIS)
            self.etat_extension_accueil.configure(text="Pour des sites traduits dans la page")
            if not self.bouton_extension.winfo_ismapped():
                self.bouton_extension.pack(side="right", padx=4)
        self.reactivite.set(REACTIVITES[config["reactivite"]][0])
        self.afficher_carte()
        combinaisons ={action: config[cle] for action, (cle, _, _) in RACCOURCIS.items()}
        if combinaisons != self._raccourcis_affiches:  # (changés dans « Options » : partout les nouveaux)
            self._raccourcis_affiches = combinaisons
            for action, ligne in self.lignes_raccourcis.items():
                self._dessiner_touches(ligne[0], noms_des_touches(combinaisons[action]))
            for action, etiquette in self.tuiles_raccourcis.items():
                etiquette.configure(text="+".join(noms_des_touches(combinaisons[action])))
            self.note_ecrire.configure(
                text=f"Écris ton message dans n'importe quelle zone de texte, puis {self.app._nom_raccourci('ecrire')} : "
                     "il est remplacé par sa traduction, prête à envoyer.")
        for action, (_, etiquette, _) in self.lignes_raccourcis.items():
            etiquette.configure(text=RACCOURCIS[action][2])
        aide = [f"{'+'.join(noms_des_touches(combinaisons[action]))} : {role}"
                for action, role in (("pause", "marche / pause"), ("ecrire", "traduire ton message"),
                                     ("zone", "traduire une zone"))
                if combinaisons[action]]
        self.aide.configure(text="\n".join(aide + ["Fermer la fenêtre ne l'arrête pas"]))
        self._afficher_programmes()
        self.afficher_langues()
        self.onglets.ajuster_hauteur()

    def afficher_langues(self):
        """Langues traduites, et ce qui les concerne : téléchargement en cours, échec (Réessayer), ou
        langue vue à l'écran mais pas choisie (Ajouter)."""
        app = self.app
        ordre = list(NOM_PAR_CODE)
        langues = ", ".join(NOM_PAR_CODE[c] for c in sorted(app.langues_choisies(), key=ordre.index))
        action, couleur = None, self.GRIS
        if app.progression_installation:
            etat = "Téléchargement : " + app.progression_installation[0]
        elif app.erreur_installation:
            etat, couleur = "Téléchargement impossible : " + app.erreur_installation, ("#C62828", "#FF8A80")
            action = ("Réessayer", app.synchroniser_modeles)
        else:
            cible = app.config["langue_cible"]
            vues = [(n, c) for c, n in app.traducteur.langues_vues.items() if n >= 3 and c != cible]
            if vues:
                code = max(vues)[1]  # la plus vue
                etat = f"{NOM_PAR_CODE[code]} : vu à l'écran, pas traduit"
                action = ("Ajouter", lambda: app.ajouter_langue(code))
            else:
                etat = ""
        if app.progression_installation:
            self.barre_langues.set(app.progression_installation[1])
            if not self.barre_langues.winfo_ismapped():
                self.barre_langues.pack(fill="x", padx=6, pady=(6, 0), after=self._ligne_etat_langues)
        elif self.barre_langues.winfo_ismapped():
            self.barre_langues.pack_forget()
        affiche = (langues, etat, action and action[0], app.config["langue_cible"])
        if affiche == self._langues_affichees:
            return
        self._langues_affichees = affiche
        noms = langues.split(", ") if langues else []
        resume = ", ".join(noms[:3]) + (f" +{len(noms) - 3}" if len(noms) > 3 else "")  # sur une ligne
        self.resume_langues.configure(text=(resume or "Aucune langue") + "  →  "
                                      + NOM_PAR_CODE[app.config["langue_cible"]])
        self.liste_langues.configure(text=langues or "Aucune : rien n'est traduit")
        self.etat_langues.configure(text=etat, text_color=couleur, wraplength=290 if action else 390)
        if action:
            self.action_langues.configure(text=action[0], command=action[1])
            self.action_langues.pack(side="right", anchor="n")
        else:
            self.action_langues.pack_forget()
        self.onglets.ajuster_hauteur()  # (un long message d'erreur ne doit pas être coupé en bas)

    def _afficher_programmes(self):
        programmes, mode = tuple(self.app.config["programmes"]), self.app.config["mode_programmes"]
        if (programmes, mode) == self._liste_affichee:
            return
        self._liste_affichee = (programmes, mode)
        for enfant in self.liste_programmes.winfo_children():
            enfant.destroy()
        if not programmes:
            vide = ("Aucun : tous les programmes sont traduits" if mode == "sauf"
                    else "Aucun : ajoute les programmes à traduire")
            ctk.CTkLabel(self.liste_programmes, text=vide, text_color=self.GRIS,
                         font=ctk.CTkFont("Segoe UI", 12)).pack(anchor="w", padx=8, pady=4)
        # Au plus 3 lignes (la fenêtre garde sa taille) : les suivants apparaissent quand on en retire.
        if len(programmes) > 3:
            ctk.CTkLabel(self.liste_programmes, text=f"… et {len(programmes) - 3} autres", text_color=self.GRIS,
                         font=ctk.CTkFont("Segoe UI", 12)).pack(side="bottom", anchor="w", padx=8, pady=(0, 4))
            programmes = programmes[:3]
        for programme in programmes:
            ligne = ctk.CTkFrame(self.liste_programmes, fg_color="transparent")
            ligne.pack(fill="x", padx=4, pady=2)
            ctk.CTkLabel(ligne, text=programme, font=ctk.CTkFont("Segoe UI", 13), anchor="w").pack(side="left", padx=4)
            ctk.CTkButton(ligne, text="Retirer", width=70, height=24, font=ctk.CTkFont("Segoe UI", 12),
                          fg_color=FOND, border_width=0, corner_radius=8,
                          text_color=TEXTE, hover_color=("#EDF1F6", "#2C3039"),
                          command=lambda p=programme: self._retirer_programme(p)).pack(side="right")

    def _mettre_a_jour_programmes(self):
        """Les programmes qui ont une fenêtre ouverte (même réduite), proposés pour la liste."""
        programmes = [p for p in programmes_avec_fenetre() if p not in self.app.config["programmes"]]
        self.menu_programmes.configure(values=programmes or ["Aucun autre programme ouvert"])
        self.a_ajouter.set(self.CHOISIR)

    # ---- actions

    def _onglet_change(self):
        if self.onglets.get() == "Plus":
            self._mettre_a_jour_programmes()
            self._suivre_historique()

    def _suivre_historique(self):
        """Rubrique « Plus » (et son historique) ouverte et fenêtre visible : les nouvelles traductions s'y ajoutent."""
        if getattr(self, "_minuterie_historique", None):
            self.win.after_cancel(self._minuterie_historique)  # (une seule à la fois)
        self._minuterie_historique = None
        if self.onglets.get() != "Plus" or not self.win.winfo_viewable():
            return
        self.afficher_historique()
        self._minuterie_historique = self.win.after(2000, self._suivre_historique)

    def _changer_demarrage(self):
        try:
            definir_demarrage(self.demarrage.get() == 1)
        except Exception:
            log.exception("Impossible de modifier le lancement au démarrage")
        self.rafraichir()

    def _changer_opacite(self, valeur):
        pourcentage = int(round(valeur))
        self.valeur_opacite.configure(text=f"{pourcentage} %")
        self.app.choisir_opacite(pourcentage)

    def _ajouter_programme(self):
        programme = self.a_ajouter.get()
        if programme.lower().endswith(".exe"):
            self.app.ajouter_programme(programme)
            self._mettre_a_jour_programmes()

    def _parcourir(self):
        """N'importe quel programme du PC, même fermé : on choisit son fichier .exe."""
        chemin = filedialog.askopenfilename(parent=self.win, title="Choisir un programme",
                                            filetypes=[("Programmes", "*.exe"), ("Tous les fichiers", "*.*")])
        if chemin:
            self.app.ajouter_programme(os.path.basename(chemin))
            self._mettre_a_jour_programmes()

    def _retirer_programme(self, programme):
        self.app.retirer_programme(programme)
        self._mettre_a_jour_programmes()

    def _changer_raccourci(self, action):
        """Attend la nouvelle combinaison de touches pour cette action (dans un autre fil : la fenêtre reste
        vivante). Un seul raccourci à la fois."""
        for _, etiquette, bouton in self.lignes_raccourcis.values():
            bouton.configure(state="disabled")
        _, etiquette, bouton = self.lignes_raccourcis[action]
        bouton.configure(text="…")
        etiquette.configure(text="Appuie sur les touches (Échap : annuler)", text_color=ACCENT)

        def attendre():
            try:
                combinaison = keyboard.read_hotkey(suppress=False)
            except Exception:
                log.exception("Lecture du raccourci impossible")
                combinaison = ""
            self.app.commandes.put(("raccourci", action, combinaison))

        threading.Thread(target=attendre, daemon=True).start()

    def fin_raccourci(self, action, message=""):
        for _, etiquette, bouton in self.lignes_raccourcis.values():
            bouton.configure(text="Changer", state="normal")
            etiquette.configure(text_color=self.GRIS)
        self.rafraichir()
        if message:
            self.lignes_raccourcis[action][1].configure(text=message, text_color=("#C62828", "#FF8A80"))

    def _oublier(self):
        self.app.oublier_traductions()
        self.info_memoire.configure(text="C'est fait")
        self.win.after(3000, lambda: self.info_memoire.configure(text=""))

    def montrer(self):
        self.rafraichir()
        if self.onglets.get() == "Plus":
            self._mettre_a_jour_programmes()
        apparaitre(self.win)
        if self.onglets.get() == "Plus":
            self.win.after(100, self._suivre_historique)
        # Windows refuse souvent de passer une fenêtre au premier plan : on la met
        # brièvement « toujours au-dessus » pour qu'elle apparaisse devant les autres.
        self.win.attributes("-topmost", True)
        self.win.lift()
        self.win.focus_force()
        self.win.after(600, lambda: self.win.attributes("-topmost", False))
        if "langues" not in self.app.config and not self.app.choix_propose:
            self.win.after(700, lambda: self.app.ouvrir_choix_langues(bienvenue=True))  # langues jamais choisies

    def cacher(self):
        self.win.withdraw()


class ChoixLangues:
    """Fenêtre du choix des langues à traduire : au premier lancement, puis depuis l'onglet « Langue ».
    Les modèles des langues cochées se téléchargent une fois, ici même, avec leur progression."""

    COLONNES = 3

    def __init__(self, app, bienvenue):
        self.app = app
        self.bienvenue = bienvenue
        fenetre = self.fenetre = ctk.CTkToplevel(app.root)
        fenetre.title("Langues à traduire")
        fenetre.resizable(False, False)
        fenetre.configure(fg_color=FOND)
        fenetre.protocol("WM_DELETE_WINDOW", self.fermer)
        fenetre.after(250, self._icone)  # customtkinter remet sa propre icône juste après l'ouverture
        gris = Reglages.GRIS

        corps = ctk.CTkFrame(fenetre, fg_color="transparent")
        corps.pack(fill="both", expand=True, padx=24, pady=20)
        ctk.CTkLabel(corps, text="Bienvenue !" if bienvenue else "Langues à traduire",
                     font=ctk.CTkFont("Segoe UI", 21, "bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(corps, text="Coche les langues que tu veux voir traduites. Chacune se télécharge une seule "
                                 f"fois (environ {TAILLE_MODELE} Mo), ensuite tout marche hors ligne. Tu pourras en "
                                 "ajouter d'autres dans la rubrique « Langues ».",
                     text_color=gris, font=ctk.CTkFont("Segoe UI", 12), anchor="w", justify="left",
                     wraplength=450).pack(fill="x", pady=(4, 0))

        ligne = ctk.CTkFrame(corps, fg_color="transparent")
        ligne.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(ligne, text="Je lis en", font=ctk.CTkFont("Segoe UI", 14, "bold")).pack(side="left")
        self.cible = ctk.StringVar(value=NOM_PAR_CODE[app.config["langue_cible"]])
        self.menu_cible = ctk.CTkOptionMenu(
            ligne, values=list(LANGUES), variable=self.cible, width=200, height=32,
            font=ctk.CTkFont("Segoe UI", 13), dropdown_font=ctk.CTkFont("Segoe UI", 13),
            fg_color=ACCENT, button_color=ACCENT_SURVOL, button_hover_color=ACCENT_FONCE,
            command=lambda _: self._mettre_a_jour())
        self.menu_cible.pack(side="right")

        ctk.CTkLabel(corps, text="Langues que je veux comprendre", font=ctk.CTkFont("Segoe UI", 14, "bold"), anchor="w").pack(
            fill="x", pady=(14, 6))
        grille = ctk.CTkFrame(corps, fg_color=Reglages.CARTE, corner_radius=12)
        grille.pack(fill="x")
        choisies = app.langues_choisies() or {"en"}
        self.cases = {}
        for i, (nom, code) in enumerate(LANGUES.items()):
            case = ctk.CTkCheckBox(grille, text=nom, font=ctk.CTkFont("Segoe UI", 13), command=self._mettre_a_jour,
                                   checkbox_width=20, checkbox_height=20, fg_color=ACCENT, hover_color=ACCENT_SURVOL)
            case.grid(row=i // self.COLONNES, column=i % self.COLONNES, sticky="w", padx=(14, 4),
                      pady=(10 if i < self.COLONNES else 3, 10 if i >= len(LANGUES) - self.COLONNES else 3))
            if code in choisies:
                case.select()
            self.cases[code] = case
        for colonne in range(self.COLONNES):
            grille.grid_columnconfigure(colonne, weight=1)

        self.info = ctk.CTkLabel(corps, text="", text_color=gris, font=ctk.CTkFont("Segoe UI", 12), anchor="w",
                                 justify="left", wraplength=450)
        self.info.pack(fill="x", pady=(10, 0))
        self.barre = ctk.CTkProgressBar(corps, height=8, progress_color=ACCENT)
        self.barre.set(0)
        boutons = self.boutons = ctk.CTkFrame(corps, fg_color="transparent")
        boutons.pack(fill="x", pady=(12, 0))
        self.bouton_valider = ctk.CTkButton(boutons, text="", width=200, height=36, fg_color=ACCENT,
                                            hover_color=ACCENT_SURVOL, font=ctk.CTkFont("Segoe UI", 13, "bold"),
                                            command=self._valider)
        self.bouton_valider.pack(side="right")
        self.bouton_fermer = app.reglages._bouton_discret(boutons, "Plus tard" if bienvenue else "Annuler",
                                                          self.fermer, 110)
        self.bouton_fermer.pack(side="right", padx=(0, 8))
        self.installees = paires_installees()
        self._mettre_a_jour()
        if app.installation is not None:  # un téléchargement est déjà en cours : on le montre
            self._mode_telechargement()
        self.montrer()

    def _icone(self):
        try:
            self.fenetre.iconbitmap(ICONE_PATH)
        except tk.TclError:
            pass

    def _choix(self):
        cible = LANGUES[self.cible.get()]
        return {code for code, case in self.cases.items() if case.get() == 1 and code != cible}, cible

    def _activer_cases(self):
        cible = LANGUES[self.cible.get()]
        for code, case in self.cases.items():
            case.configure(state="disabled" if code == cible else "normal")  # on ne traduit pas sa propre langue
        self.menu_cible.configure(state="normal")

    def _mettre_a_jour(self):
        self._activer_cases()
        langues, cible = self._choix()
        # (Comme synchroniser_modeles : la langue dans laquelle j'écris, pour Ctrl+Alt+Entrée, compte aussi.)
        manquantes = (paires_necessaires(langues, cible)
                      | paires_necessaires({cible}, self.app.config["langue_ecriture"])) - self.installees
        if not langues:
            texte = "Aucune langue cochée : rien ne sera traduit."
        else:
            pluriel = "s" if len(langues) > 1 else ""
            texte = f"{len(langues)} langue{pluriel} cochée{pluriel} · " + (
                f"environ {len(manquantes) * TAILLE_MODELE} Mo à télécharger" if manquantes else "rien à télécharger")
        self.info.configure(text=texte, text_color=Reglages.GRIS)
        self.bouton_valider.configure(text="Télécharger et commencer" if manquantes and self.bienvenue
                                      else "Télécharger" if manquantes else "Valider", state="normal")

    def _valider(self):
        langues, cible = self._choix()
        if self.app.choisir_langues(langues, cible):
            self._mode_telechargement()
        else:
            self._terminer()

    def _mode_telechargement(self):
        for case in self.cases.values():
            case.configure(state="disabled")
        self.menu_cible.configure(state="disabled")
        self.barre.pack(fill="x", pady=(8, 0), before=self.boutons)
        self.bouton_valider.configure(text="Téléchargement…", state="disabled")
        self.bouton_fermer.configure(text="Continuer en arrière-plan", width=190)
        if self.app.progression_installation:
            self.progression(*self.app.progression_installation)

    def progression(self, texte, fraction):
        self.info.configure(text=texte, text_color=Reglages.GRIS)
        self.barre.set(fraction)

    def fini(self, erreur):
        """Fin du téléchargement : prêt, ou erreur (on peut réessayer)."""
        if erreur:
            self.info.configure(text=f"Téléchargement impossible : {erreur}", text_color=("#C62828", "#FF8A80"))
            self.bouton_valider.configure(text="Réessayer", state="normal")
            self.bouton_fermer.configure(text="Fermer", width=110)
            self.barre.pack_forget()
            self.installees = paires_installees()
            self._activer_cases()
            return
        self.barre.set(1)
        self.info.configure(text="C'est prêt ✓  Les langues choisies sont traduites.",
                            text_color=("#1E8E3E", "#5BD17F"))
        self.bouton_valider.configure(text="Terminé", state="disabled")
        self.fenetre.after(1500, self._terminer)

    def _terminer(self):
        bienvenue = self.bienvenue
        self.fermer()
        if bienvenue:
            self.app.reglages.montrer()  # au premier lancement : on découvre ensuite les réglages

    def montrer(self):
        fenetre = self.fenetre
        fenetre.update_idletasks()
        x = (fenetre.winfo_screenwidth() - fenetre.winfo_reqwidth()) // 2
        y = max(0, (fenetre.winfo_screenheight() - fenetre.winfo_reqheight()) // 3)
        fenetre.geometry(f"+{x}+{y}")
        animer_boutons(fenetre)
        apparaitre(fenetre)
        fenetre.attributes("-topmost", True)  # (même astuce que la fenêtre des réglages)
        fenetre.lift()
        fenetre.focus_force()
        fenetre.after(600, lambda: fenetre.attributes("-topmost", False))

    def fermer(self):
        """Ferme la fenêtre ; un téléchargement en cours continue (sa progression reste visible dans
        l'onglet « Langue »)."""
        self.app.choix_langues = None
        self.fenetre.destroy()


# --------------------------------------------------------------------------- application

class Application:
    def __init__(self, montrer_reglages=True):
        self.config = charger_config()
        ctk.set_appearance_mode("dark")
        self.root = ctk.CTk()
        self.root.withdraw()
        # Une erreur dans la fenêtre (réglages, affichage) est notée dans le journal.
        self.root.report_callback_exception = lambda *infos: log.error("Erreur dans la fenêtre", exc_info=infos)
        self.calque = Calque(self.root, self.config["style"])
        self.calque.facteur_taille = TAILLES[self.config["taille"]][1]
        self.calque.fondu = self.config["fondu"]
        Vitre.opacite = round(self.config["opacite"] * 255 / 100)
        self.commandes = queue.Queue()
        self.actif = False
        self.arret = threading.Event()
        self.reveil = threading.Event()  # demande une nouvelle fusion / traduction
        self.reveil_accessibilite = threading.Event()  # demande une relecture de l'accessibilité
        self.dernier_mouvement = 0.0
        self.derniere_interaction = 0.0
        self.lecteur = None  # créé par le thread de lecture
        self.textes_image = []  # textes trouvés par l'analyse de l'image de l'écran
        self.image_courante = None  # dernière image de l'écran (pixels, origine), sans nos traductions
        self.image_a_jour = 0.0  # instant de la dernière image entièrement lue (hors vidéos)
        self.image_change_le = 0.0  # instant du dernier changement de texte dans l'image
        self.memoire_fusion = {}  # morceau lu dans l'image -> décision de l'accessibilité à son sujet
        self.medias_lus = (0.0, [])  # (instant, cadres des images et vidéos vues par l'accessibilité)
        self.textes_lus = (0.0, [])  # (instant, cadres des textes d'interface vus par l'accessibilité)
        self.reveil_image = threading.Event()  # demande une nouvelle analyse de l'image
        self.capture = CaptureRapide()  # image de l'écran (fils d'analyse et de suivi)
        self.verrou_exclusion = threading.Lock()
        self.exclusions = 0  # nombre de fils qui ont besoin que les vitres soient cachées des captures
        self.capture_utilisateur_jusqua = 0.0  # l'utilisateur prend une capture d'écran : vitres visibles
        self.en_chargement = False  # des traductions sont en train de se faire (anneau de chargement)
        self.ecriture_en_cours = False  # Ctrl+Alt+Entrée : un message est en train d'être traduit et collé
        self.suivi = None  # SuiviEcran, créé par le fil d'analyse de l'image
        # Suivi en direct : pendant un défilement ou le déplacement d'une fenêtre, les traductions
        # suivent le contenu image par image (au lieu de disparaître puis réapparaître).
        self.suivi_en_cours = False
        self.demande_suivi = None  # ("defilement" | "clavier" | "fenetre", hwnd)
        self.demande_pendant_suivi = None  # demande arrivée pendant un suivi (reprise s'il s'arrête déjà)
        self.debut_deplacement = 0.0  # instant où l'on a commencé à déplacer une fenêtre
        self.fin_deplacement = 0.0  # instant où l'on a lâché une fenêtre déplacée
        self.fins_envoyees = self.fins_traitees = 0  # fins de suivi envoyées / traitées par la fenêtre principale
        self.textes_suivis = frozenset()  # traductions qu'un suivi en direct déplace (leur place enregistrée est périmée)
        self.reveil_suivi = threading.Event()
        # Durées (secondes) relevées pour le journal : lecture de l'image, traduction des nouveaux textes.
        self.mesures = {"image": [], "traduction": [], "accessibilite": []}
        self.oubli_demande = False  # « Oublier les traductions mémorisées » (fait par le fil de traduction)
        self.extension_vue_le = {}  # programme du navigateur -> dernier signe de vie de son extension
        self._extension_affichee = frozenset()  # navigateurs de l'extension tels qu'affichés dans les réglages
        self.serveur = None  # serveur local pour l'extension
        self.numero_presse_papiers = user32.GetClipboardSequenceNumber()
        self.choix_langues = None  # fenêtre du choix des langues, quand elle est ouverte
        self.choix_propose = False  # elle a été ouverte depuis le lancement
        self.installation = None  # fil qui télécharge des modèles de traduction
        self.installation_a_refaire = False  # le choix a changé pendant un téléchargement
        self.progression_installation = None  # (texte, fraction) pendant un téléchargement
        self.erreur_installation = ""  # pourquoi le dernier téléchargement a échoué
        self.etat_carte = ""  # carte graphique NVIDIA : « Active », « Pas de carte NVIDIA »… (rubrique « Options »)
        # « Traduire une zone choisie » (raccourci) : seule cette zone est traduite, même en pause, même une image.
        self.zone_choisie = None  # (x1, y1, x2, y2) en pixels de l'écran
        self.zone_le = 0.0
        self.zone_temporaire = False  # la traduction était en pause : elle y revient à la fin de la zone
        self.selecteur = None  # SelecteurZone ouvert
        self.historique = collections.deque(maxlen=300)  # (heure, texte d'origine, traduction), le plus récent à la fin
        self._historique_vu = {}  # texte d'origine -> instant de son dernier ajout (pas dix fois le même d'affilée)
        # (Avant l'icône : son menu, construit par un autre fil, le lit tout de suite.)
        self.mise_a_jour = None  # (version, lien de l'installateur) d'une version plus récente, s'il y en a une

        self.icone = self._creer_icone()
        threading.Thread(target=self.icone.run, daemon=True).start()
        self.traducteur = Traducteur(self.langues_choisies(), self.config["mots"])
        self.traducteur.multilingue_permis = self.config["grand_modele"]
        threading.Thread(target=self._preparer_carte_graphique, name="carte_graphique", daemon=True).start()
        self.ia = IALocale(self.traducteur, self._ia_prete)
        self._ia_signalee_le = 0.0
        threading.Thread(target=self.ia.boucle, args=(self.arret,), name="ia_locale", daemon=True).start()
        if self.config["ia_locale"]:
            threading.Thread(target=self._preparer_ia, name="preparation_ia", daemon=True).start()
        self.reglages = Reglages(self.root, self)
        threading.Thread(target=self._boucle_accessibilite, daemon=True).start()
        threading.Thread(target=self._boucle_lecture, daemon=True).start()
        threading.Thread(target=self._boucle_image, daemon=True).start()
        threading.Thread(target=self._boucle_suivi, daemon=True).start()
        if EST_EXE and DEPOT_GITHUB:
            threading.Thread(target=self._surveiller_mises_a_jour, name="mises_a_jour", daemon=True).start()
        self._demarrer_serveur()
        self.root.after(3000, self._verifier_extension)
        self.root.after(100, self._garder_vitres)
        self.plein_ecran_signales = set()  # jeux en plein écran exclusif déjà signalés (une fois chacun)
        self.root.after(3000, self._surveiller_plein_ecran)

        for action, (cle, defaut, _) in RACCOURCIS.items():  # raccourci enregistré illisible : celui par défaut
            combinaison = self.config[cle]
            if combinaison and touches_du_raccourci(combinaison) is None:
                log.warning("Raccourci %r inutilisable : celui par défaut", combinaison)
                self.config[cle] = defaut
        self.raccourci_actif = None  # raccourci de secours (bibliothèque « keyboard »), s'il a fallu
        self.fil_entrees = None  # identifiant du fil qui écoute le clavier et la souris
        self.raccourci_demande = None  # (action, nouvelle combinaison) à enregistrer (par ce fil)
        self.touches_capture = self._touches_capture()
        threading.Thread(target=self._ecouter_entrees, name="entrees", daemon=True).start()
        self._ecouter_fenetres()

        self._activer(self.config["actif"])
        if montrer_reglages:
            # customtkinter re-cache la fenêtre juste après sa création : on l'affiche après lui.
            # (Au premier lancement, la fenêtre du choix des langues s'ouvre par-dessus.)
            self.root.after(400, self.reglages.montrer)
        if "langues" in self.config:
            self.root.after(1500, self.synchroniser_modeles)  # reprend un téléchargement interrompu
        self.root.after(30, self._traiter_commandes)

    # ---- réactivité : on relit l'écran dès que quelque chose bouge

    def _ecouter_entrees(self):
        """(Fil dédié.) Le raccourci clavier, par RegisterHotKey : un vrai raccourci de Windows, qui ne
        tombe jamais. Molette, clics et touches, par des crochets de Windows, réinstallés toutes les
        20 s : Windows retire sans prévenir un crochet qui a tardé à répondre (programme très occupé,
        au démarrage surtout), et le raccourci et le suivi du défilement ne marchaient plus."""
        WM_HOTKEY, WM_KEYDOWN, WM_SYSKEYDOWN = 0x0312, 0x0100, 0x0104
        WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
        BOUTONS_RELACHES = {0x0202, 0x0205, 0x0208}  # gauche, droit, milieu
        TOUCHES_DEFILEMENT = {0x21, 0x22, 0x23, 0x24, 0x26, 0x28}  # pages, fin, début, flèches haut et bas
        Procedure = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

        def sur_souris(code, message, donnees):
            if code == 0:
                if message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    self._demander_suivi("defilement")
                elif message in BOUTONS_RELACHES:
                    zone = self.zone_choisie
                    if zone:  # un clic hors de la zone choisie : elle n'est plus traduite
                        point = ctypes.cast(donnees, ctypes.POINTER(wintypes.POINT))[0]
                        if not (zone[0] <= point.x < zone[2] and zone[1] <= point.y < zone[3]):
                            self.commandes.put(("fin_zone",))
                    self._interaction()
            return user32.CallNextHookEx(None, code, message, donnees)

        def enfoncee(touche):
            return user32.GetAsyncKeyState(touche) & 0x8000

        def sur_clavier(code, message, donnees):
            if code == 0 and message in (WM_KEYDOWN, WM_SYSKEYDOWN):
                touche = ctypes.cast(donnees, ctypes.POINTER(wintypes.DWORD))[0]
                if touche == 0x1B and self.zone_choisie:  # Échap : la zone choisie n'est plus traduite
                    self.commandes.put(("fin_zone",))
                if any(touche == vk and (not mods & 1 or enfoncee(0x12)) and (not mods & 2 or enfoncee(0x11))
                       and (not mods & 4 or enfoncee(0x10)) and (not mods & 8 or enfoncee(0x5B) or enfoncee(0x5C))
                       for mods, vk in self.touches_capture):
                    self.capture_utilisateur()  # Impr. écran, Win+Maj+S (ou ceux choisis)
                elif touche in TOUCHES_DEFILEMENT:
                    self._demander_suivi("clavier")
                else:
                    self._interaction()
            return user32.CallNextHookEx(None, code, message, donnees)

        self._rappels = (Procedure(sur_souris), Procedure(sur_clavier))  # garder une référence
        crochets = []

        def installer():
            for crochet in crochets:
                user32.UnhookWindowsHookEx(crochet)
            crochets[:] = [c for c in (user32.SetWindowsHookExW(14, self._rappels[0], None, 0),  # WH_MOUSE_LL
                                       user32.SetWindowsHookExW(13, self._rappels[1], None, 0))  # WH_KEYBOARD_LL
                           if c]
            if len(crochets) < 2:
                log.error("Impossible de surveiller la souris ou le clavier")

        self.fil_entrees = ctypes.windll.kernel32.GetCurrentThreadId()
        installer()
        actuels = {}  # action -> combinaison enregistrée auprès de Windows
        for action in IDS_RACCOURCIS:
            actuels[action] = self.config[RACCOURCIS[action][0]]
            if not self._enregistrer_raccourci(actuels[action], action):
                log.warning("Raccourci %s déjà pris par un autre programme", actuels[action])
                if action == "pause":  # le plus important : écoute de secours
                    try:
                        self.raccourci_actif = keyboard.add_hotkey(actuels[action], self._raccourci_presse)
                    except Exception:
                        log.exception("Raccourci %r inutilisable", actuels[action])
        actions = {ident: action for action, ident in IDS_RACCOURCIS.items()}
        message = wintypes.MSG()
        installe_le = time.monotonic()
        while not self.arret.is_set():
            try:  # (une erreur ne doit pas arrêter ce fil : plus aucun raccourci ni suivi du défilement sinon)
                if user32.MsgWaitForMultipleObjects(0, None, False, 1000, 0x04FF) == 0:  # QS_ALLINPUT
                    while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):  # PM_REMOVE
                        action = actions.get(message.wParam) if message.message == WM_HOTKEY else None
                        if action == "ecrire":
                            threading.Thread(target=self._ecrire_traduit, daemon=True).start()
                        elif action == "pause":
                            self.commandes.put(("basculer",))
                        elif action == "zone":
                            self.commandes.put(("choisir_zone",))
                        elif message.message == WM_CHANGER_RACCOURCI and self.raccourci_demande:
                            action, nouveau = self.raccourci_demande
                            pris = False
                            try:
                                user32.UnregisterHotKey(None, IDS_RACCOURCIS[action])
                                pris = self._enregistrer_raccourci(nouveau, action)
                                if pris:
                                    actuels[action] = nouveau
                                else:
                                    self._enregistrer_raccourci(actuels[action], action)
                            finally:  # toujours une réponse : les boutons « Changer » ne restent pas bloqués
                                self.commandes.put(("raccourci_pris", action, nouveau, pris))
                if time.monotonic() - installe_le > 20:
                    installe_le = time.monotonic()
                    installer()
            except Exception:
                log.exception("Erreur dans l'écoute du clavier et de la souris")
                self.arret.wait(1)

    @staticmethod
    def _enregistrer_raccourci(combinaison, action="pause"):
        """(Fil des entrées.) True si Windows a accepté ce raccourci (il peut être pris par un autre programme).
        Aucun raccourci : rien à enregistrer."""
        if not combinaison:
            return True
        touches = touches_du_raccourci(combinaison)
        return bool(touches) and bool(user32.RegisterHotKey(None, IDS_RACCOURCIS[action], touches[0] | 0x4000,
                                                            touches[1]))  # MOD_NOREPEAT

    def _touches_capture(self):
        """(modificateurs, touche) des raccourcis de capture d'écran choisis."""
        return [t for t in (touches_du_raccourci(self.config[RACCOURCIS[a][0]]) for a in CAPTURES
                            if self.config[RACCOURCIS[a][0]]) if t]

    def _interaction(self):
        """Clic, touche, changement de fenêtre : le contenu a pu changer, on relit tout de suite."""
        self.derniere_interaction = time.monotonic()
        self.reveil.set()
        self.reveil_accessibilite.set()
        self.reveil_image.set()  # (sans attendre la fin de sa pause : jusqu'à 1 s en « Économe »)

    def _ecouter_fenetres(self):
        def sur_evenement(_hook, evenement, hwnd, _objet, _enfant, _thread, _temps):
            if evenement == EVENT_SYSTEM_MOVESIZESTART:
                self.debut_deplacement = time.monotonic()
                self._demander_suivi("fenetre", hwnd)
            else:
                if evenement == EVENT_SYSTEM_MOVESIZEEND:
                    self.fin_deplacement = time.monotonic()
                if evenement == EVENT_SYSTEM_FOREGROUND and hwnd:
                    # Les traductions d'autres fenêtres qu'elle recouvre disparaissent tout de suite.
                    rect = _rect_fenetre(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)
                    if rect:
                        self.commandes.put(("cacher_sous", rect))
                self._interaction()

        self._rappel_fenetres = WinEventProc(sur_evenement)  # garder une référence
        for debut, fin in ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                           (EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZEEND),
                           (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND)):
            if not user32.SetWinEventHook(debut, fin, None, self._rappel_fenetres, 0, 0, WINEVENT_OUTOFCONTEXT):
                log.error("Impossible de surveiller les fenêtres (événements %#x à %#x)", debut, fin)

    def _demander_suivi(self, genre, hwnd=None):
        """Molette, touche de défilement, fenêtre qu'on déplace (appelé par les crochets de
        Windows) : les traductions vont suivre le contenu en direct, puis tout sera relu une fois
        l'écran immobile."""
        self.dernier_mouvement = self.derniere_interaction = time.monotonic()
        if not self.suivi_en_cours:
            self.demande_suivi = (genre, hwnd)
            self.reveil_suivi.set()
        else:
            self.demande_pendant_suivi = (genre, hwnd)  # repris si le suivi en cours s'arrête déjà
        self.reveil.set()
        self.reveil_accessibilite.set()

    # ---- suivi en direct (défilement, fenêtre déplacée)

    def _cmd_fin_suivi(self, deplacements, caches):
        try:
            self.calque.decaler(deplacements, caches)
        finally:
            self.fins_traitees += 1  # même en cas d'erreur : sinon chaque suivi suivant attendrait 0,5 s
            if not self.suivi_en_cours:
                self.textes_suivis = frozenset()  # à leur nouvelle place : la garde des vitres les surveille à nouveau

    def _envoyer_fin_suivi(self, deplacements, caches):
        """Remet les traductions suivies à la fenêtre principale (qui les range ou les garde à leur
        nouvelle place). Le suivi suivant attend qu'elle l'ait fait."""
        self.fins_envoyees += 1
        self.commandes.put(("fin_suivi", deplacements, caches))

    def _cmd_effacer_zone(self, zone):
        self.calque.effacer(zone)

    def _cmd_cacher_sous(self, fenetre):
        # (Pendant un suivi en direct, c'est lui qui place les traductions. Pendant une capture d'écran, l'outil
        # Capture de Windows passe devant tout l'écran : les traductions doivent rester, pour être sur l'image.)
        if not self.suivi_en_cours and not self._capture_utilisateur_en_cours():
            self.calque.cacher_sous(fenetre)

    def _surveiller_plein_ecran(self):
        """(Toutes les 3 s.) Un jeu en plein écran exclusif : Windows n'y laisse rien s'afficher par-dessus, les
        traductions n'apparaissent pas. On le dit une fois par jeu, avec la solution (« fenêtré sans bordure »)."""
        try:
            if self.actif and jeu_plein_ecran():
                programme = programme_de_fenetre(user32.GetForegroundWindow())
                if programme and programme not in self.plein_ecran_signales:
                    self.plein_ecran_signales.add(programme)
                    log.info("Plein écran exclusif : %s", programme)
                    self.icone.notify(f"« {programme} » est en plein écran : si ses traductions n'apparaissent pas, "
                                      "choisis « Fenêtré sans bordure » dans ses options d'affichage.",
                                      "Traducteur d'écran")
        except Exception:
            log.exception("Surveillance du plein écran impossible")
        self.root.after(3000, self._surveiller_plein_ecran)

    def _garder_vitres(self):
        """(Fenêtre principale, toutes les 0,1 s.) Aucune traduction ne reste sur un texte qu'on ne voit plus.
        Pas pendant une capture d'écran : l'outil Capture passe devant tout, les traductions doivent y être."""
        try:
            if self.calque.affiches and not self._capture_utilisateur_en_cours():
                self.calque.cacher_couvertes(self.textes_suivis)
        except Exception:
            log.exception("Erreur de la garde des traductions")
        self.root.after(100, self._garder_vitres)

    def _boucle_suivi(self):
        """Fait suivre les traductions au contenu pendant un défilement ou le déplacement d'une
        fenêtre, image par image : elles restent collées au texte au lieu de disparaître."""
        while not self.arret.is_set():
            self.reveil_suivi.wait()
            self.reveil_suivi.clear()
            demande, self.demande_suivi = self.demande_suivi, None
            if demande is None or not self.actif or self.arret.is_set():
                continue
            self.suivi_en_cours = True  # pendant ce temps, rien d'autre ne touche aux traductions
            # Python ne fait tourner qu'un fil à la fois : face à la lecture de l'écran ou à la traduction, le suivi
            # attendait son tour à chaque appel à Windows (jusqu'à 5 ms chaque fois, plus d'une seconde de retard par
            # image). Pendant le suivi, la main passe toutes les 0,5 ms : les traductions collent au contenu.
            intervalle = sys.getswitchinterval()
            sys.setswitchinterval(0.0005)
            try:
                # Laisser finir une mise à jour déjà commencée, et la fin du suivi précédent (sinon on
                # reprendrait des traductions déjà rangées, à leur ancienne place).
                attente = time.monotonic()
                while (self.calque.generation % 2 or self.fins_traitees < self.fins_envoyees) \
                        and time.monotonic() - attente < 0.5:
                    time.sleep(0.002)
                genre, hwnd = demande
                if genre == "fenetre":
                    self._suivre_fenetre(hwnd)
                else:
                    self._suivre_defilement(genre)
            except Exception:
                log.exception("Échec du suivi en direct")
            finally:
                sys.setswitchinterval(intervalle)
                self.suivi_en_cours = False
                if self.fins_traitees >= self.fins_envoyees:
                    self.textes_suivis = frozenset()  # (sinon, la fenêtre principale le fera en les rangeant)
                self.reveil_image.set()
                self.reveil.set()
                self.reveil_accessibilite.set()
            # Un cran de molette arrivé juste à la fin du suivi n'est pas perdu : on suit à nouveau.
            relance, self.demande_pendant_suivi = self.demande_pendant_suivi, None
            if relance is not None and self.demande_suivi is None:
                self.demande_suivi = relance
                self.reveil_suivi.set()

    def _zone_defilante(self, hwnd, point):
        """Partie de la fenêtre qui défile : la page web sous la souris (navigateur), sinon toute
        la fenêtre ; limitée à l'écran. None si trop petite."""
        pages = zones_des_pages(hwnd) if programme_de_fenetre(hwnd) in NAVIGATEURS else []
        zone = next((p for p in pages if p[0] <= point[0] < p[2] and p[1] <= point[1] < p[3]), None)
        zone = zone or _rect_fenetre(hwnd)
        (ox, oy), ecran = self.capture.origine, self.capture.ecran
        zone = (max(zone[0], ox), max(zone[1], oy),
                min(zone[2], ox + ecran["width"]), min(zone[3], oy + ecran["height"]))
        return zone if zone[2] - zone[0] > 60 and zone[3] - zone[1] > 60 else None

    def _suivre_defilement(self, genre):
        """Pendant un défilement : à chaque image, mesure de combien le contenu a glissé et
        déplace les traductions d'autant. Celles d'un en-tête fixe restent en place ; celles qui
        sortent de la zone ou passent sous un en-tête sont cachées."""
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        hwnd = fenetre_sous_souris() if genre == "defilement" else user32.GetForegroundWindow()
        if not hwnd:
            return
        suivi = self.suivi
        if suivi is None or suivi.reference is None or suivi.image is None:
            self.commandes.put(("effacer_zone", _rect_fenetre(hwnd)))  # impossible de suivre : on cache
            return
        if self.lecteur:
            self.lecteur.invalider(hwnd)  # l'accessibilité relira cette fenêtre ensuite
        if genre != "defilement":
            r = _rect_fenetre(hwnd)
            point = wintypes.POINT((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
        zone = self._zone_defilante(hwnd, (point.x, point.y))
        if zone is None:
            self.commandes.put(("effacer_zone", _rect_fenetre(hwnd)))
            return
        affichees = [(t, v, d) for t, v, d in self.calque.instantane()
                     if d and zone[0] <= t.x + t.l // 2 < zone[2] and zone[1] <= t.y + t.h // 2 < zone[3]]
        if not affichees:
            return  # rien à faire suivre : l'analyse d'image relira la zone une fois immobile
        self.textes_suivis = frozenset(t for t, _, _ in affichees)  # (la garde des vitres les laisse au suivi)
        # Copie : une analyse de l'image déjà en cours pourrait encore modifier la référence pendant le
        # suivi (des traductions seraient alors prises pour un en-tête fixe et resteraient sur place).
        avant, origine = suivi.reference.copy(), suivi.image[1]
        # Bande verticale où l'on mesure le glissement : là où sont les traductions (c'est elles
        # qu'il faut déplacer), ou toute la largeur si cette bande est unie (marge blanche…).
        centres = sorted(t.x + t.l // 2 for t, _, _ in affichees)
        centre = centres[len(centres) // 2]
        gauche, droite = max(zone[0], centre - 400), min(zone[2], centre + 400)
        profil_avant = profil_vertical(avant, origine, zone, gauche, droite)
        if droite - gauche < 60 or profil_avant.std() < 1.5:
            gauche, droite = zone[0], zone[2]
            profil_avant = profil_vertical(avant, origine, zone, gauche, droite)
        # Pour chaque traduction : [suit le contenu ?, images de suite où son texte n'est pas vu, position]
        etats = {t: [True, 0, (d[2], d[3])] for t, _, d in affichees}
        prises = {t: v.etat for t, v, _ in affichees}  # état de chaque vitre quand on l'a prise en charge
        self._exclure_vitres(True)
        try:
            dwmapi.DwmFlush()  # laisser Windows redessiner l'écran sans elles
            dwmapi.DwmFlush()
            dy, debut = 0, time.monotonic()
            dernier_changement, inconnu = debut, 0
            images = sans_mesure = 0
            while not self.arret.is_set() and not self._capture_utilisateur_en_cours():
                self.demande_pendant_suivi = None  # les crans de molette d'ici là sont suivis par ce suivi-ci
                image, _ = self.capture.prendre()
                profil = profil_vertical(image, origine, zone, gauche, droite)
                mesure = estimer_decalage(profil_avant, profil, dy)
                images += 1
                sans_mesure += mesure is None
                if mesure is not None and mesure != dy:
                    dy, dernier_changement = mesure, time.monotonic()
                # Le contenu a bougé mais on ne sait pas de combien : mieux vaut cacher les traductions
                # (elles reviendront à la relecture) que les laisser au mauvais endroit.
                inconnu = inconnu + 1 if mesure is None and np.abs(profil - profil_avant).mean() > 2 else 0
                if inconnu >= 2:
                    for etat in etats.values():
                        etat[1] = 99
                for t, vitre, d in affichees:
                    # À chaque image, chaque traduction vérifie que son texte d'origine est bien
                    # dessous (glissé, ou resté en place) ; sinon elle se cache au bout de 3 images.
                    etat = etats[t]
                    ici = ecart_motif(avant, image, origine, t, dy) if dy else None
                    sur_place = ecart_motif(avant, image, origine, t, 0)
                    if ici is not None and ici < 30:
                        etat[0], etat[1] = True, 0  # le texte a glissé avec le reste
                    elif sur_place is not None and sur_place < 15:
                        etat[0], etat[1] = False, 0  # il n'a pas bougé (en-tête fixe, ou pas encore)
                    else:
                        etat[1] += 1
                    decalage = dy if etat[0] else 0
                    visible = etat[1] < 3 and zone[1] <= t.y + decalage and t.y + t.h + decalage <= zone[3]
                    position = (d[2], d[3] + decalage) if visible else None
                    if position != etat[2]:
                        etat[2] = position
                        vitre.deplacer(prises[t], *(position or (-32000, -32000)))
                maintenant = time.monotonic()
                # Fin : plus de molette depuis 0,25 s et la page ne glisse plus (fin du défilement
                # fluide), et en tout cas 0,6 s après le dernier cran : l'analyse reprend la main.
                calme = maintenant - self.dernier_mouvement
                if (calme > 0.25 and maintenant - dernier_changement > 0.15) or calme > 0.6 \
                        or maintenant - debut > 30:
                    break
                time.sleep(max(0.0, 1 / 60 - (time.monotonic() - maintenant)))  # ~60 images par seconde
        except Exception:
            # Des traductions ont peut-être déjà été déplacées : on les range toutes (elles reviendront à
            # la relecture) plutôt que de les laisser à une place que la fenêtre principale ignore.
            log.exception("Échec du suivi d'un défilement")
            self._envoyer_fin_suivi([], [t for t, _, _ in affichees])
            return
        finally:
            self._exclure_vitres(False)
        deplacements = [(t, 0, etats[t][2][1] - d[3], False) for t, _, d in affichees
                        if etats[t][2] is not None and etats[t][2][1] != d[3]]
        caches = [t for t, _, _ in affichees if etats[t][2] is None]
        log.info("Défilement suivi : %d images en %.1f s, glissement %d px (%d images sans mesure), "
                 "%d traductions déplacées, %d cachées sur %d", images, time.monotonic() - debut, dy,
                 sans_mesure, len(deplacements), len(caches), len(affichees))
        if deplacements or caches:
            self._envoyer_fin_suivi(deplacements, caches)

    def _suivre_fenetre(self, hwnd):
        """Pendant qu'on déplace une fenêtre, ses traductions la suivent ; si on la redimensionne,
        elles sont cachées (le texte va se réorganiser, il sera relu)."""
        racine = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        depart = _rect_fenetre(racine)
        affichees = [(t, v, d) for t, v, d in self.calque.instantane() if d and tuple(t.fenetre) == depart]
        self.textes_suivis = frozenset(t for t, _, _ in affichees)  # (la garde des vitres les laisse au suivi)
        prises = {t: v.etat for t, v, _ in affichees}
        debut = time.monotonic()
        dx = dy = 0
        cachees = False
        # Bouton principal de la souris (le droit si les boutons sont inversés, pour un gaucher). Un
        # déplacement au clavier ou au doigt ne l'enfonce pas : on se fie alors à la fin signalée.
        bouton = 0x02 if user32.GetSystemMetrics(23) else 0x01  # SM_SWAPBUTTON
        a_la_souris = bool(user32.GetAsyncKeyState(bouton) & 0x8000)
        if not affichees:
            # Aucune traduction à faire suivre : on attend seulement la fin du déplacement, pour que les textes
            # lus dans cette fenêtre la suivent (sinon relus à moitié, ils revenaient décalés).
            while not self.arret.is_set() and self.fin_deplacement < self.debut_deplacement \
                    and (not a_la_souris or user32.GetAsyncKeyState(bouton) & 0x8000) \
                    and time.monotonic() - debut < 10:
                time.sleep(0.03)
            fin = _rect_fenetre(racine)
            if getattr(self, "suivi", None) is not None and fin and (fin[2] - fin[0], fin[3] - fin[1]) == (
                    depart[2] - depart[0], depart[3] - depart[1]) and fin != depart:
                self.suivi.decaler_fenetre(depart, fin[0] - depart[0], fin[1] - depart[1])
            return
        try:
            # Jusqu'à la fin du déplacement (signalée par Windows), ou dès que le bouton de la souris est
            # relâché (une fin manquée ne bloque plus rien), et en tout cas 10 s au plus.
            while not self.arret.is_set() and self.fin_deplacement < self.debut_deplacement \
                    and (not a_la_souris or user32.GetAsyncKeyState(bouton) & 0x8000) \
                    and time.monotonic() - debut < 10:
                cachees, dx, dy = self._placer_suivies(racine, depart, affichees, prises, cachees, dx, dy)
                dwmapi.DwmFlush()  # une fois par image affichée
            cachees, dx, dy = self._placer_suivies(racine, depart, affichees, prises, cachees, dx, dy)
        except Exception:
            log.exception("Échec du suivi d'une fenêtre déplacée")
            cachees = True  # place inconnue : on range ses traductions (elles reviendront à la relecture)
        if cachees:
            self._envoyer_fin_suivi([], [t for t, _, _ in affichees])
        elif dx or dy:
            if getattr(self, "suivi", None) is not None:
                self.suivi.decaler_fenetre(depart, dx, dy)  # ses textes lus suivent aussi
            self._envoyer_fin_suivi([(t, dx, dy, True) for t, _, _ in affichees], [])

    @staticmethod
    def _placer_suivies(racine, depart, affichees, prises, cachees, dx, dy):
        """Place les traductions d'une fenêtre qu'on déplace (cachées si on la redimensionne)."""
        r = _rect_fenetre(racine)
        if (r[2] - r[0], r[3] - r[1]) != (depart[2] - depart[0], depart[3] - depart[1]):
            if not cachees:
                for t, vitre, _ in affichees:
                    vitre.deplacer(prises[t], -32000, -32000)
            return True, dx, dy
        if cachees or (r[0] - depart[0], r[1] - depart[1]) != (dx, dy):
            dx, dy = r[0] - depart[0], r[1] - depart[1]
            for t, vitre, d in affichees:
                vitre.deplacer(prises[t], d[2] + dx, d[3] + dy)
        return False, dx, dy

    # ---- lecture de l'image de l'écran

    def capture_utilisateur(self):
        """(Fil des entrées.) Impr. écran, Win+Maj+S : l'utilisateur prend une capture. Pendant 15 s, les
        traductions restent visibles aux captures (on ne lit plus l'écran nous-mêmes), pour qu'elles y soient."""
        # (Jusqu'à ce que l'image soit prise — elle arrive dans le presse-papiers —, 10 s au plus si on annule : les
        # traductions sont figées pendant ce temps, elles ne doivent pas le rester une fois la capture faite.)
        self.capture_numero = user32.GetClipboardSequenceNumber()
        self.capture_utilisateur_jusqua = time.monotonic() + 10
        with self.verrou_exclusion:
            for hwnd in self.calque.hwnds:
                user32.SetWindowDisplayAffinity(hwnd, WDA_NONE)

    def _capture_utilisateur_en_cours(self):
        if time.monotonic() >= self.capture_utilisateur_jusqua:
            return False
        if user32.GetClipboardSequenceNumber() != getattr(self, "capture_numero", None):
            self.capture_utilisateur_jusqua = 0.0  # capture prise : tout reprend tout de suite
            self.reveil.set()
            self.reveil_image.set()
            return False
        return True

    def _exclure_vitres(self, exclure):
        """Cache les vitres aux captures d'écran (ou les y remet). Plusieurs fils peuvent le
        demander à la fois : elles réapparaissent quand plus aucun n'en a besoin."""
        with self.verrou_exclusion:
            self.exclusions += 1 if exclure else -1
            if exclure and self._capture_utilisateur_en_cours():
                return  # l'utilisateur prend une capture : les traductions doivent y être
            if self.exclusions == (1 if exclure else 0):
                for hwnd in self.calque.hwnds:
                    user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if exclure else WDA_NONE)

    def _capturer(self):
        """Image de tout l'écran SANS nos traductions : (pixels BGRA, origine), ou None.

        Appelée par le fil d'analyse de l'image. Les vitres ne sont cachées des captures que
        pendant cet instant : le reste du temps, elles apparaissent sur les captures d'écran."""
        if self._capture_utilisateur_en_cours():
            return None  # nos traductions sont visibles aux captures : on ne relit pas l'écran pendant ce temps
        for _ in range(50):
            generation, hwnds = self.calque.generation, self.calque.hwnds
            if generation % 2:  # des traductions sont en train d'être posées : on attend la fin
                time.sleep(0.005)
                continue
            self._exclure_vitres(True)
            try:
                if hwnds:
                    dwmapi.DwmFlush()  # laisser Windows redessiner l'écran sans elles
                    dwmapi.DwmFlush()
                image = self.capture.prendre()
            except Exception:
                log.exception("Capture de l'écran impossible")
                return None
            finally:
                self._exclure_vitres(False)
            if self.calque.generation == generation:  # aucune traduction n'est apparue entre-temps
                return image
        return None

    def _boucle_image(self):
        """Analyse l'image de l'écran (reconnaissance de texte de Windows) : c'est elle qui dit
        quel texte est visible, et où. Tourne en priorité basse et ne relit que ce qui a changé."""
        ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        try:
            suivi = self.suivi = SuiviEcran(self._capturer)
            suivi.mouvement_recent = lambda: time.monotonic() - self.dernier_mouvement < 1.0
            # Images et vidéos vues par l'accessibilité, seulement si relevées après le dernier défilement ou
            # déplacement (sinon elles ont pu bouger). Un clic ou une touche ne les déplace pas : un stream regardé
            # en écrivant dans le chat reste une vidéo, jamais lue.
            # (Dans une zone choisie, on veut justement leur texte : elles sont lues.)
            suivi.medias = lambda: self.medias_lus[1] if self.medias_lus[0] > self.dernier_mouvement \
                and not self.zone_choisie else []
            suivi.textes_surs = lambda: self.textes_lus[1] if self.textes_lus[0] > self.dernier_mouvement else []
        except Exception:
            log.exception("Reconnaissance de texte de Windows indisponible")
            self.commandes.put(("badge", "Lecture de l'écran impossible (voir journal.txt)"))
            return
        while not self.arret.is_set():
            # Analyses rapprochées juste après une action, mais aussi tant que le texte à l'écran change
            # tout seul (un chat qui défile) : un nouveau message est lu tout de suite.
            actif = time.monotonic() - max(self.derniere_interaction, self.image_change_le) < ACTIVITE_RECENTE
            _, apres_action, au_repos = REACTIVITES[self.config["reactivite"]]  # réglage « Réactivité »
            self.reveil_image.wait(apres_action if actif else au_repos)
            self.reveil_image.clear()
            if not self.actif or self.arret.is_set():
                suivi.oublier()  # à la reprise, tout sera relu
                self.textes_image = []  # l'écran d'avant ne réapparaît pas à la reprise
                continue
            if self.suivi_en_cours:
                continue  # les traductions suivent un défilement : on relira quand ce sera fini
            while time.monotonic() - self.dernier_mouvement < PAUSE_MOUVEMENT:
                time.sleep(0.02)
            debut = time.monotonic()
            try:
                modifie, a_jour = suivi.analyser()
                if modifie:
                    self.mesures["image"].append(time.monotonic() - debut)
            except Exception:
                log.exception("Échec de l'analyse de l'image")
                suivi.oublier()
                # Ce qui était lu n'est plus sûr : pas de traductions laissées à d'anciennes places.
                self.textes_image = []
                self.reveil.set()
                self.arret.wait(1)
                continue
            self.image_courante = suivi.image
            attendue = self.image_a_jour < self.dernier_mouvement  # la lecture attend cette image
            if modifie:
                self.textes_image = list(suivi.morceaux)
                self.image_change_le = time.monotonic()
            if a_jour:
                self.image_a_jour = debut
            if modifie or (a_jour and attendue):
                self.reveil.set()
            if a_jour and attendue:
                self.reveil_accessibilite.set()  # positions stables : l'accessibilité peut relire

    # ---- petits messages et touches tapées pour l'utilisateur

    def _signaler(self, message, duree=3.5):
        """Petit message en haut à droite de l'écran (même en pause), qui s'efface tout seul."""
        log.info("Message affiché : %s", message)
        self.commandes.put(("badge_toujours", message))
        minuterie = threading.Timer(duree, lambda: self.commandes.put(("badge_toujours", "")))
        minuterie.daemon = True  # (ne retient pas la fermeture du programme)
        minuterie.start()

    def _cmd_badge_toujours(self, texte):
        self.calque.badge(texte)

    @staticmethod
    def _taper(*touches):
        """Appuie puis relâche une combinaison (codes de touches de Windows : ne dépend pas du clavier,
        AZERTY ou QWERTY). Ex. : _taper(0x11, 0x41) pour Ctrl+A."""
        for touche in touches:
            user32.keybd_event(touche, 0, 0, 0)
        for touche in reversed(touches):
            user32.keybd_event(touche, 0, 0x0002, 0)  # KEYEVENTF_KEYUP
        time.sleep(0.03)

    def _attendre_touches_lachees(self):
        """Attend qu'on ait lâché Ctrl, Alt, Maj, Windows et Entrée (au plus 2 s) : sinon une touche qu'on tape
        pour l'utilisateur se combinerait avec (Ctrl+A deviendrait Ctrl+Alt+A)."""
        fin = time.monotonic() + 2
        while time.monotonic() < fin and any(user32.GetAsyncKeyState(t) & 0x8000
                                             for t in (0x10, 0x11, 0x12, 0x0D, 0x5B, 0x5C)):
            time.sleep(0.02)

    def _nom_raccourci(self, action):
        """Le raccourci de cette action, lisible (« Ctrl+Alt+Entrée »)."""
        return "+".join(noms_des_touches(self.config[RACCOURCIS[action][0]]))

    def _ecrire_traduit(self):
        """(Fil à part.) Ctrl+Alt+Entrée : le message en cours d'écriture (la zone de texte où l'on écrit) est
        remplacé par sa traduction, prête à envoyer. Le presse-papiers est rendu tel qu'il était."""
        if self.ecriture_en_cours:
            return
        self.ecriture_en_cours = True
        avant = None
        log.info("Ctrl+Alt+Entrée : traduction de ce qui est écrit (%s)",
                 programme_de_fenetre(user32.GetForegroundWindow()))
        try:
            self._attendre_touches_lachees()
            avant = sauver_presse_papiers()  # tout (texte, image, fichiers), remis tel quel à la fin
            numero = user32.GetClipboardSequenceNumber()
            self._taper(0x11, 0x41)  # Ctrl+A : tout le message
            time.sleep(0.05)
            self._taper(0x11, 0x43)  # Ctrl+C
            fin = time.monotonic() + 0.8
            while user32.GetClipboardSequenceNumber() == numero and time.monotonic() < fin:
                time.sleep(0.02)
            texte = lire_presse_papiers() if user32.GetClipboardSequenceNumber() != numero else None
            log.info("Ctrl+Alt+Entrée : %s caractères copiés", len(texte) if texte else 0)
            if not texte or not texte.strip():
                self._signaler(f"Clique d'abord dans la zone où tu écris, puis {self._nom_raccourci('ecrire')}")
                return
            if len(texte) > 3000:
                self._signaler("Message trop long à traduire (3000 caractères au plus)")
                return
            de, vers = self.config["langue_cible"], self.config["langue_ecriture"]
            if self.traducteur.deja_en(texte, vers):
                self._signaler(f"Déjà en {NOM_PAR_CODE[vers].lower()}")
                return
            self.commandes.put(("chargement", True))
            try:
                traduit = self.traducteur.traduire_ecrit(texte, de, vers)
            except RuntimeError:
                self._signaler(f"Langue {NOM_PAR_CODE[de].lower()} → {NOM_PAR_CODE[vers].lower()} pas encore téléchargée")
                return
            finally:
                self.commandes.put(("chargement", False))
            if not traduit:
                self._signaler("Rien à traduire")
                return
            ecrire_presse_papiers(traduit)
            self._noter_historique([(texte.strip(), traduit)])
            time.sleep(0.03)
            self._taper(0x11, 0x56)  # Ctrl+V : la traduction remplace le message sélectionné
            log.info("Ctrl+Alt+Entrée : traduction collée (%d caractères)", len(traduit))
            time.sleep(0.4)  # le programme a collé : on peut rendre le presse-papiers
        except Exception:
            log.exception("Traduction de ce qui est écrit impossible")
            self._signaler("Traduction impossible (voir journal.txt)")
        finally:
            if avant is not None:
                remettre_presse_papiers(avant)
            self.numero_presse_papiers = user32.GetClipboardSequenceNumber()
            self.ecriture_en_cours = False

    # ---- boucle principale (thread Tk)

    def lancer(self):
        log.info("Démarrage")
        self.root.mainloop()

    def _traiter_commandes(self):
        while True:
            try:
                commande, *args = self.commandes.get_nowait()
            except queue.Empty:
                break
            try:
                getattr(self, "_cmd_" + commande)(*args)
            except Exception:  # une commande qui échoue n'empêche pas les suivantes
                log.exception("Erreur dans la commande « %s »", commande)
        self.root.after(30, self._traiter_commandes)

    def _activer(self, actif, enregistrer=True):
        self.actif = actif
        if enregistrer:  # (pas pour une zone choisie pendant la pause : la pause reste le réglage)
            self.config["actif"] = actif
            sauver_config(self.config)
        self.active_le = time.monotonic() if actif else None
        if actif:
            threading.Thread(target=self.traducteur.prechauffer, args=(self.config["langue_cible"],),
                             name="prechauffage", daemon=True).start()
            self.calque.montrer()
            self.reveil.set()
            self.reveil_accessibilite.set()
        else:
            self.calque.cacher()
            self.calque.chargement(False)
        self.reglages.actualiser()
        try:
            self.icone.icon = self.image_icone if actif else self.image_icone_pause
            self.icone.title = "Traducteur d'écran" if actif else "Traducteur d'écran (en pause)"
        except Exception:  # (icône pas encore affichée)
            log.exception("Icône de la zone de notification inchangée")
        self.icone.update_menu()

    def basculer(self):
        self._fin_zone()
        self._activer(not self.actif)

    # ---- traduire une zone choisie (raccourci Ctrl+Alt+Z)

    def _cmd_choisir_zone(self):
        """L'écran s'assombrit : on entoure à la souris ce qu'on veut traduire."""
        if self.selecteur is not None:
            return
        self._fin_zone()  # (une nouvelle zone remplace l'ancienne)
        self.selecteur = SelecteurZone(self.root, self._zone_choisie)

    def _zone_choisie(self, zone):
        """Seule cette zone est traduite, même en pause, même le texte d'une image, d'une vidéo ou d'un jeu (on l'a
        choisie exprès), jusqu'à Échap ou un clic ailleurs."""
        self.selecteur = None
        if zone is None:
            return  # annulé
        log.info("Zone choisie : %s", zone)
        self.zone_choisie, self.zone_le = zone, time.monotonic()
        self.calque.cadre_zone(zone)
        if self.actif:
            self.calque.effacer()  # le reste de l'écran n'est plus traduit pendant ce temps
        else:
            self.zone_temporaire = True
            self._activer(True, enregistrer=False)
        self.reveil.set()
        self.reveil_image.set()
        self.reveil_accessibilite.set()
        self._signaler("Zone traduite · Échap ou un clic ailleurs pour arrêter", 3)

    def _cmd_fin_zone(self):
        if self.zone_choisie and time.monotonic() - self.zone_le > 0.3:  # (pas le clic qui a fini de la tracer)
            self._fin_zone()

    def _fin_zone(self):
        if self.zone_choisie is None:
            return
        self.zone_choisie = None
        self.calque.cadre_zone(None)
        if self.zone_temporaire:
            self.zone_temporaire = False
            self._activer(False, enregistrer=False)
        else:
            self.reveil.set()  # tout l'écran est de nouveau traduit
        log.info("Fin de la zone choisie")

    def _dans_zone(self, t):
        zone = self.zone_choisie
        return zone is not None and zone[0] <= t.x + t.l // 2 < zone[2] and zone[1] <= t.y + t.h // 2 < zone[3]

    def choisir_langue(self, code):
        self.config["langue_cible"] = code
        sauver_config(self.config)
        self.calque.effacer()  # effacer les traductions dans l'ancienne langue
        self.icone.update_menu()
        self._appliquer_langues()

    # ---- langues à traduire et téléchargement de leurs modèles

    def langues_choisies(self):
        """Langues d'origine à traduire : celles choisies ; tant qu'on n'a pas choisi, celles dont les
        modèles sont déjà là (rien ne change pour qui avait la version d'avant)."""
        langues, cible = self.config.get("langues"), self.config["langue_cible"]
        return set(langues) - {cible} if langues is not None else langues_installees(cible)

    def ouvrir_choix_langues(self, bienvenue=False):
        self.choix_propose = True
        if self.choix_langues is None:
            self.choix_langues = ChoixLangues(self, bienvenue)
        else:
            self.choix_langues.montrer()

    def choisir_langues(self, langues, cible):
        """(Fenêtre du choix des langues.) Renvoie True si des modèles se téléchargent."""
        self.config["langues"] = sorted(langues)
        if cible != self.config["langue_cible"]:
            self.config["langue_cible"] = cible
            self.calque.effacer()
            self.icone.update_menu()
        sauver_config(self.config)
        return self._appliquer_langues()

    def ajouter_langue(self, code):
        """« Ajouter » une langue vue à l'écran (onglet « Langue »)."""
        self.config["langues"] = sorted(self.langues_choisies() | {code})
        sauver_config(self.config)
        self._appliquer_langues()

    def _appliquer_langues(self):
        """Les langues ont changé : le traducteur les adopte (dans un autre fil : il peut être en train de
        traduire) et ce qui manque se télécharge. Renvoie True si des modèles se téléchargent."""
        langues = self.langues_choisies()

        def adopter():
            self.traducteur.changer_langues(langues)
            self.reveil.set()

        threading.Thread(target=adopter, daemon=True).start()
        self.reglages.rafraichir()
        return self.synchroniser_modeles()

    def synchroniser_modeles(self):
        """Télécharge en arrière-plan les modèles qui manquent aux langues choisies. Un seul téléchargement
        à la fois : si le choix change entre-temps, on revérifie à la fin. True si un téléchargement est en cours."""
        if self.installation is not None:
            self.installation_a_refaire = True
            return True
        self.erreur_installation = ""
        cible = self.config["langue_cible"]
        utiles = (paires_necessaires(self.langues_choisies(), cible)  # pour lire
                  | paires_necessaires({cible}, self.config["langue_ecriture"]))  # pour écrire
        manquantes = sorted(utiles - paires_installees())
        # Grands modèles (bien meilleure traduction) : téléchargés à part, s'ils sont publiés (pas livrés avec le
        # programme). Ceux de l'anglais, ceux d'une étape par l'anglais, et ceux qui traduisent en direct.
        directs = {(de, cible) for de in self.langues_choisies()}
        grands = grands_modeles_manquants(utiles | directs | {("en", cible)}) if DEPOT_GITHUB else []
        # Carte graphique NVIDIA : sa bibliothèque de calcul, une fois (seulement si elle sert) ; et le grand modèle de
        # traduction, qui ne marche qu'avec elle.
        avec_carte = self.config["carte_graphique"] and carte_nvidia()
        carte = avec_carte and not bibliotheque_gpu_presente()
        madlad = avec_carte and self.config["grand_modele"] and not madlad_present()
        # IA locale : son moteur (30 Mo), puis, s'il trouve une carte graphique qui convient, son modèle (2,4 Go).
        ia = self.config["ia_locale"] and self.ia.moteur.carte is not False and not ia_presente()
        if not manquantes and not grands and not carte and not madlad and not ia:
            self.reglages.afficher_langues()
            return False
        log.info("Téléchargement des modèles : %s", ", ".join(
            [f"{de} → {vers}" for de, vers in manquantes] + grands + (["carte graphique"] if carte else [])
            + (["grand modèle de traduction"] if madlad else []) + (["IA locale"] if ia else [])))
        envoye_le = [0.0]

        def progression(texte, fraction):
            maintenant = time.monotonic()
            if maintenant - envoye_le[0] >= 0.25 or "Mo" not in texte:  # pas plus de 4 fois par seconde
                envoye_le[0] = maintenant
                self.commandes.put(("progression", texte, fraction))

        def travail():
            erreur = ""
            try:
                if manquantes and not installer_modeles(manquantes, progression, self.arret.is_set):
                    return  # le programme se ferme
                for nom in grands:  # (en échec, le petit modèle suffit : on le note, sans rien bloquer)
                    try:
                        if not installer_grand_modele(nom, progression, self.arret.is_set):
                            return
                        self.traducteur.grands = {}  # relu au prochain besoin
                    except Exception:
                        log.exception("Grand modèle %s non téléchargé", nom)
                if carte:  # (en échec, le processeur suffit : on le note, sans rien bloquer)
                    try:
                        if not installer_bibliotheque_gpu(progression, self.arret.is_set):
                            return
                        threading.Thread(target=self._preparer_carte_graphique, daemon=True).start()
                    except Exception as e:
                        log.exception("Bibliothèque de la carte graphique non téléchargée")
                        self.etat_carte = "Téléchargement impossible : " + (str(e)[:60] or "erreur")
                if madlad:
                    try:
                        if not installer_madlad(progression, self.arret.is_set):
                            return
                        self.traducteur.oublier_madlad()  # (chargé à la prochaine traduction ; tout est retraduit)
                    except Exception:
                        log.exception("Grand modèle de traduction non téléchargé")
                if ia:  # (en échec, les modèles habituels traduisent : on le note, sans rien bloquer)
                    def ia_coupee():
                        return self.arret.is_set() or not self.config["ia_locale"]

                    try:  # (coupée pendant le téléchargement : il s'arrête, la suite continue)
                        if (os.path.exists(moteur_ia()) or installer_moteur_ia(progression, ia_coupee)) \
                                and self._chercher_carte_ia() \
                                and (os.path.exists(chemin_modele_ia()) or installer_modele_ia(progression, ia_coupee)):
                            threading.Thread(target=self._preparer_ia, daemon=True).start()
                    except Exception as e:
                        log.exception("IA locale non téléchargée")
                        self.ia.etat = "téléchargement impossible : " + (str(e)[:60] or "erreur")
                    self.commandes.put(("etat_carte",))
            except urllib.error.HTTPError as e:
                log.exception("Téléchargement des modèles impossible")
                erreur = f"le serveur des modèles a refusé (erreur {e.code}), réessaie plus tard"
            except urllib.error.URLError:
                log.exception("Téléchargement des modèles impossible")
                erreur = "serveur injoignable, vérifie ta connexion à internet"
            except (TimeoutError, ConnectionError, HTTPException):
                log.exception("Téléchargement des modèles impossible")
                erreur = "la connexion a été coupée pendant le téléchargement, réessaie"
            except OSError as e:  # disque plein, fichier verrouillé…
                log.exception("Téléchargement des modèles impossible")
                erreur = ("plus assez de place sur le disque (environ 100 Mo par langue)" if e.errno == 28  # ENOSPC
                          else "le dossier des modèles est bloqué (un antivirus ?), réessaie" if e.errno == 13
                          else e.strerror or str(e))
            except Exception as e:
                log.exception("Téléchargement des modèles impossible")
                erreur = str(e) if isinstance(e, RuntimeError) else "erreur inattendue (voir journal.txt)"
            try:
                self.traducteur.changer_langues(self.langues_choisies())  # modèles neufs : on revoit ce qui restait
            except Exception:
                log.exception("Langues non mises à jour après le téléchargement")
            finally:  # toujours : sinon plus aucun téléchargement possible, « préparation… » pour toujours
                self.commandes.put(("installation_finie", erreur))

        self.progression_installation = ("préparation…", 0.0)
        self.installation = threading.Thread(target=travail, name="telechargement", daemon=True)
        self.installation.start()
        self.reglages.afficher_langues()
        return True

    def _cmd_progression(self, texte, fraction):
        if self.installation is None:
            return  # message arrivé après la fin
        self.progression_installation = (texte, fraction)
        if self.choix_langues is not None:
            self.choix_langues.progression(texte, fraction)
        self.reglages.afficher_langues()
        if self.actif:
            self.calque.badge("Téléchargement : " + texte)

    def _cmd_installation_finie(self, erreur):
        self.installation = None
        self.progression_installation = None
        self.erreur_installation = erreur
        self.calque.badge("")
        self.reveil.set()
        if self.choix_langues is not None:
            self.choix_langues.fini(erreur)
        if self.installation_a_refaire:
            self.installation_a_refaire = False
            if not erreur:
                self.synchroniser_modeles()
        self.reglages.afficher_langues()

    def choisir_style(self, style):
        self.config["style"] = style
        sauver_config(self.config)
        self.calque.changer_style(style)
        self.reveil.set()

    def choisir_taille(self, taille):
        self.config["taille"] = taille
        sauver_config(self.config)
        self.calque.changer_taille(TAILLES[taille][1])
        self.reveil.set()

    def choisir_opacite(self, pourcentage):
        self.config["opacite"] = pourcentage
        sauver_config(self.config)
        self.calque.regler_opacite(round(pourcentage * 255 / 100))

    def choisir_fondu(self, actif):
        self.config["fondu"] = actif
        sauver_config(self.config)
        self.calque.fondu = actif

    # ---- IA locale

    @property
    def etat_ia(self):
        if self.ia.moteur.carte is False:
            return "Pas de carte graphique adaptée sur ce PC"
        if not self.config["ia_locale"]:
            return "Désactivé"
        if self.ia.etat:  # (« Active », démarrage, erreur)
            return self.ia.etat
        return "téléchargement de 2,4 Go au premier usage" if not ia_presente() else "démarrage…"

    def _ia_prete(self):
        """(Fil de l'IA.) De meilleures traductions sont prêtes : l'écran se met à jour, et les pages web (l'extension
        les redemande quand la version change ; pas plus d'une fois toutes les 3 s)."""
        self.reveil.set()
        if time.monotonic() - self._ia_signalee_le > 3:
            self._ia_signalee_le = time.monotonic()
            self.traducteur.generation += 1
        self.commandes.put(("etat_carte",))

    def _chercher_carte_ia(self):
        """(Fil à part.) La carte graphique de l'IA, demandée une fois au moteur. False si aucune ne convient."""
        moteur = self.ia.moteur
        if moteur.carte is None:
            moteur.carte = carte_pour_ia() or False
            log.info("IA locale : %s", f"carte graphique {moteur.carte[1]}" if moteur.carte
                     else "pas de carte graphique adaptée (il en faut une avec 4 Go)")
        return moteur.carte

    def _preparer_ia(self):
        """(Fil à part.) Le moteur et le modèle de l'IA téléchargés (par synchroniser_modeles), le moteur démarre sur la
        carte graphique, puis l'IA se met au travail."""
        ia = self.ia
        if not self.config["ia_locale"] or not ia_presente():
            return
        try:
            if self._chercher_carte_ia():
                ia.etat = "démarrage…"
                self.commandes.put(("etat_carte",))
                ia.moteur.demarrer()
                ia.etat = "Active"
                ia.actif = self.config["ia_locale"]
                log.info("IA locale prête (%s)", MODELE_IA["fichier"])
                self.reveil.set()  # (ce qui est à l'écran lui est proposé)
        except Exception as e:
            log.exception("IA locale indisponible")
            ia.etat = "le moteur de l'IA ne démarre pas : " + (str(e)[:60] or "erreur")
        if not self.config["ia_locale"]:  # (coupée entre-temps)
            ia.moteur.arreter()
        self.commandes.put(("etat_carte",))

    @property
    def niveau_qualite(self):
        """« normale », « meilleure » ou « maximale », d'après les réglages."""
        if self.config["ia_locale"]:
            return "maximale"
        return "meilleure" if self.config["carte_graphique"] or self.config["grand_modele"] else "normale"

    def choisir_qualite(self, niveau):
        """La qualité en un seul choix : chaque moyen (carte graphique, grand modèle, IA) s'allume ou se coupe."""
        voulus = {"carte_graphique": niveau != "normale", "grand_modele": niveau != "normale",
                  "ia_locale": niveau == "maximale"}
        for cle, choisir in (("carte_graphique", self.choisir_carte_graphique),
                             ("grand_modele", self.choisir_grand_modele), ("ia_locale", self.choisir_ia)):
            if self.config[cle] != voulus[cle]:
                choisir(voulus[cle])
        log.info("Qualité choisie : %s", niveau)
        self.reglages.afficher_carte()

    def choisir_ia(self, actif):
        self.config["ia_locale"] = actif
        sauver_config(self.config)
        self.ia.etat = ""
        if actif:
            if ia_presente():
                threading.Thread(target=self._preparer_ia, daemon=True).start()
            else:
                self.synchroniser_modeles()  # (télécharge le moteur et le modèle, puis revient à _preparer_ia)
        else:
            def couper():
                self.ia.oublier()
                self.ia.moteur.arreter()

            threading.Thread(target=couper, daemon=True).start()
            self.reveil.set()
        self.reglages.afficher_carte()

    @property
    def etat_grand_modele(self):
        """Où en est le grand modèle de traduction (rubrique « Langues »)."""
        if not self.config["grand_modele"]:
            return "Désactivé"
        if not carte_nvidia():
            return "Pas de carte NVIDIA sur ce PC"
        if not self.config["carte_graphique"]:
            return "il faut la carte graphique (au-dessus)"
        if not madlad_present():
            return "téléchargement de 3 Go au premier usage"
        if self.traducteur.appareil != "cuda":
            return "en attente de la carte graphique"
        return "Actif"

    def choisir_grand_modele(self, actif):
        self.config["grand_modele"] = actif
        sauver_config(self.config)
        self.traducteur.multilingue_permis = actif
        threading.Thread(target=self.traducteur.oublier_madlad, daemon=True).start()  # (retraduit avec ou sans lui)
        if actif and not madlad_present():
            self.synchroniser_modeles()
        self.reveil.set()
        self.reglages.afficher_carte()

    def signaler_probleme(self):
        """« Signaler un problème » : le journal du programme est mis dans un zip, montré dans l'Explorateur, et la
        page « nouveau problème » du dépôt GitHub s'ouvre, préremplie (version, Windows) : il reste à décrire le
        problème et à y glisser le zip. Rien ne part tout seul."""
        zip_journal = os.path.join(APP_DIR, "journal-traducteur.zip")
        try:
            with zipfile.ZipFile(zip_journal, "w", zipfile.ZIP_DEFLATED) as archive:
                for nom in ("journal.txt", "journal.txt.1"):
                    if os.path.exists(os.path.join(APP_DIR, nom)):
                        archive.write(os.path.join(APP_DIR, nom), nom)
            subprocess.Popen(["explorer", "/select,", zip_journal])
        except OSError:
            log.exception("Journal non préparé pour le signalement")
        corps = ("**Ce qui ne marche pas :**\n\n(décris ici ce que tu faisais et ce qui s'est passé)\n\n"
                 "**Journal :** glisse ici le fichier « journal-traducteur.zip » (il vient de s'ouvrir dans "
                 "l'Explorateur).\n\n---\n"
                 f"Version {VERSION_PROGRAMME} · Windows {sys.getwindowsversion().build} · "
                 f"{'exe' if EST_EXE else 'Python'} · carte graphique : {self.etat_carte or '?'}")
        os.startfile(f"https://github.com/{DEPOT_GITHUB}/issues/new?"
                     + urllib.parse.urlencode({"title": "Problème : ", "body": corps}))
        log.info("Signalement d'un problème : page GitHub ouverte")

    def _preparer_carte_graphique(self):
        """(Fil à part.) Au lancement, ou une fois sa bibliothèque téléchargée : la carte graphique NVIDIA prend les
        traductions si elle marche vraiment (sinon le processeur continue, rien ne change)."""
        if not self.config["carte_graphique"]:
            self.etat_carte = "Désactivée"
        elif not carte_nvidia():
            self.etat_carte = "Pas de carte NVIDIA sur ce PC"
        elif not bibliotheque_gpu_presente():
            self.etat_carte = "Téléchargement de 530 Mo au premier usage"
        else:
            self.etat_carte = "Essai en cours…"
            self.commandes.put(("etat_carte",))
            self.etat_carte = self.traducteur.essayer_carte_graphique()
            if self.traducteur.appareil == "cuda" and self.config["grand_modele"] and madlad_present():
                self.traducteur.oublier_madlad()  # (ce qui a été traduit sans lui l'est à nouveau, avec lui)
            if self.traducteur.appareil == "cuda" and self.actif:
                self.traducteur.prechauffer(self.config["langue_cible"])  # (sur la carte, cette fois)
        self.commandes.put(("etat_carte",))

    def _cmd_etat_carte(self):
        self.reglages.afficher_carte()

    def choisir_carte_graphique(self, actif):
        self.config["carte_graphique"] = actif
        sauver_config(self.config)
        if actif:
            if bibliotheque_gpu_presente():
                threading.Thread(target=self._preparer_carte_graphique, daemon=True).start()
            else:
                self.synchroniser_modeles()  # (télécharge sa bibliothèque)
        else:
            traducteur = self.traducteur

            def processeur():
                with traducteur.verrou:
                    traducteur.appareil = "cpu"
                    traducteur.modeles.clear()
                    traducteur.grands.clear()
                    traducteur.multilingue = None  # (le grand modèle de traduction ne marche qu'avec la carte)

            threading.Thread(target=processeur, daemon=True).start()
            self.etat_carte = "Désactivée"
        self.reglages.afficher_carte()

    def ajouter_programme(self, programme):
        programme = programme.strip().lower()
        if programme and programme not in self.config["programmes"]:
            self.config["programmes"] = sorted(self.config["programmes"] + [programme])
            sauver_config(self.config)
            self._appliquer_exclus()

    def retirer_programme(self, programme):
        if programme in self.config["programmes"]:
            self.config["programmes"] = [p for p in self.config["programmes"] if p != programme]
            sauver_config(self.config)
            self._appliquer_exclus()

    def choisir_mode_programmes(self, mode):
        self.config["mode_programmes"] = mode
        sauver_config(self.config)
        self._appliquer_exclus()

    def _filtre_programmes(self):
        """(programmes jamais traduits, seuls programmes traduits ou None), d'après les réglages :
        la liste choisie (à exclure, ou seule traduite), et les navigateurs que l'extension traduit déjà."""
        liste = frozenset(self.config["programmes"])
        exclus = self.navigateurs_extension()
        if self.config["mode_programmes"] == "seulement":
            return exclus, liste
        return exclus | liste, None

    def _mettre_a_jour_filtre(self):
        """(Tout fil.) Les programmes à traduire, pour la lecture de l'écran."""
        if self.lecteur is not None:
            self.lecteur.filtre = self._filtre_programmes()  # remplacé d'un bloc : l'autre fil le lit

    def _appliquer_exclus(self):
        self._mettre_a_jour_filtre()
        self.derniere_interaction = time.monotonic()  # tout relire avec le nouveau réglage
        self.reveil_accessibilite.set()
        self.reveil.set()
        self.reglages.rafraichir()

    # ---- extension du navigateur

    def _demarrer_serveur(self):
        try:
            serveur = ThreadingHTTPServer(("127.0.0.1", PORT_EXTENSION), RequeteExtension)
        except OSError:
            log.exception("Port %d déjà pris : l'extension du navigateur ne pourra pas se connecter", PORT_EXTENSION)
            return
        serveur.daemon_threads = True
        serveur.app = self
        self.serveur = serveur
        threading.Thread(target=serveur.serve_forever, name="extension", daemon=True).start()

    def navigateurs_extension(self):
        """Programmes des navigateurs dont l'extension s'est manifestée ces 20 dernières secondes."""
        maintenant = time.monotonic()
        # (L'extension se manifeste au moins toutes les 30 s, même sur les pages où elle ne peut pas entrer.)
        return frozenset(p for p, vue_le in self.extension_vue_le.items() if maintenant - vue_le < 75)

    def extension_vue(self, agent):
        """(Fil du serveur.) L'extension d'un navigateur s'est manifestée : ce navigateur lui est laissé
        (chaque navigateur pour soi : l'extension peut tourner dans plusieurs à la fois)."""
        navigateurs = navigateurs_de(agent)
        nouveau = not navigateurs <= self.navigateurs_extension()
        # remplacé d'un bloc : les autres fils le lisent
        self.extension_vue_le = {**self.extension_vue_le, **dict.fromkeys(navigateurs, time.monotonic())}
        if nouveau:
            log.info("Extension du navigateur connectée (%s)", ", ".join(sorted(navigateurs)))
            self._mettre_a_jour_filtre()
            self.reveil.set()
            self.commandes.put(("extension",))

    def etat_extension(self, agent):
        """(Fil du serveur.) Ce que l'extension doit savoir : traduction en marche ? vers quelle langue ?"""
        self.extension_vue(agent)
        # Version des traductions : elle change à chaque lancement et quand les langues ou les modèles
        # changent ; l'extension redemande alors ce qu'elle avait laissé tel quel.
        version = f"{VERSION_LANCEMENT}-{self.traducteur.generation}"
        # Version de l'extension présente sur le disque : si le navigateur en a chargé une plus ancienne
        # (fichiers mis à jour, extension pas rechargée), elle le dit au lieu de mal marcher.
        # (Une zone choisie pendant la pause ne remet pas les pages web en traduction.)
        return {"actif": self.actif and not getattr(self, "zone_temporaire", False), "langue": self.config["langue_cible"],
                "version": version, "extension": version_extension()}

    def traduire_pour_extension(self, textes, agent):
        """(Fil du serveur.) Traductions de textes de page web ; None pour ce qui reste tel quel."""
        self.extension_vue(agent)
        cible = self.config["langue_cible"]
        if not self.actif or getattr(self, "zone_temporaire", False):
            return {"actif": False, "langue": cible, "traductions": [None] * len(textes)}
        # (Un long bloc mêlant japonais et anglais est traduit ligne par ligne, phrase par phrase : voir traduire.)
        # Par petits paquets : le traducteur est libéré entre deux, et l'écran passe entre (au lancement, chaque
        # onglet ouvert redemande toute sa page, et l'écran attendait que tout soit fini).
        traductions = []
        for i in range(0, len(textes), 6):
            traductions += self.traducteur.traduire(textes[i:i + 6], cible, filtre=texte_de_page_a_traduire)
        ia = getattr(self, "ia", None)
        if ia is not None:  # (IA locale : améliorées ensuite ; la page les redemande quand la version change)
            ia.proposer([t for t, traduit in zip(textes, traductions) if traduit], cible)
        return {"actif": True, "langue": cible, "traductions": traductions}

    def _verifier_extension(self):
        """Toutes les 3 s : l'extension s'est-elle tue (navigateur fermé) ? Le navigateur redevient
        alors traduit par la lecture de l'image. (Et une langue pas choisie a-t-elle été vue à l'écran ?)"""
        try:
            if self.navigateurs_extension() != self._extension_affichee:
                self._mettre_a_jour_filtre()
                self.reglages.rafraichir()
            if self.root.winfo_viewable():
                self.reglages.afficher_langues()
        finally:
            self.root.after(3000, self._verifier_extension)

    def _cmd_extension(self):
        self.reglages.rafraichir()

    def choisir_reactivite(self, reactivite):
        self.config["reactivite"] = reactivite
        sauver_config(self.config)
        self.reveil_image.set()

    def oublier_traductions(self):
        """Efface les traductions gardées en mémoire et sur le disque (elles seront refaites). Fait par
        le fil de traduction : la fenêtre ne se fige pas si une traduction est en cours."""
        self.oubli_demande = True
        self.calque.effacer()
        self.reveil.set()

    @staticmethod
    def _oublier(traducteur):
        """(Fil de traduction.) Oublie toutes les traductions, en mémoire et sur le disque."""
        with traducteur.verrou:
            traducteur.cache.clear()
            traducteur.cache_modifie = False
            try:
                os.remove(CACHE_TRADUCTIONS)
            except FileNotFoundError:
                pass
            except OSError:
                log.exception("Impossible d'effacer les traductions mémorisées")
        log.info("Traductions mémorisées oubliées")

    def _raccourci_presse(self):
        self.commandes.put(("basculer",))

    def _cmd_raccourci(self, action, combinaison):
        """Nouvelle combinaison tapée dans les réglages pour cette action : Échap annule, Suppr n'en met aucune.
        Sinon elle remplace l'ancienne si elle est valable (Ctrl, Alt, Maj ou Windows + une autre touche, ou une
        touche qui n'écrit rien comme F8) et pas déjà utilisée."""
        touches = [t.strip() for t in combinaison.lower().split("+") if t.strip()]
        if not touches or any(t in ("esc", "échap", "escape") for t in touches):
            self.reglages.fin_raccourci(action)
            return
        if touches in (["delete"], ["suppr"], ["suppression"]):
            combinaison = ""  # aucun raccourci pour cette action
        elif touches_du_raccourci(combinaison) is None:
            self.reglages.fin_raccourci(action, "Il faut Ctrl, Alt, Maj ou Win + une touche (ou F1 à F12)")
            return
        elif action not in CAPTURES and combinaison in RACCOURCIS_RESERVES:
            self.reglages.fin_raccourci(action, "Déjà utilisée par Windows, choisis-en une autre")
            return
        else:
            touches_voulues = touches_du_raccourci(combinaison)
            for autre, (cle, _, role) in RACCOURCIS.items():
                if autre != action and self.config[cle] and touches_du_raccourci(self.config[cle]) == touches_voulues:
                    self.reglages.fin_raccourci(action, f"Déjà utilisée pour « {role} »")
                    return
        if action in CAPTURES:  # (seulement guettée : rien à enregistrer auprès de Windows)
            self._cmd_raccourci_pris(action, combinaison, True)
            return
        if not self.fil_entrees:
            self.reglages.fin_raccourci(action, "Cette combinaison ne marche pas, essaie une autre")
            return
        # Le fil des entrées l'enregistre, puis répond (_cmd_raccourci_pris).
        self.raccourci_demande = (action, combinaison)
        if not user32.PostThreadMessageW(self.fil_entrees, WM_CHANGER_RACCOURCI, 0, 0):
            self.reglages.fin_raccourci(action, "Cette combinaison ne marche pas, essaie une autre")

    def _cmd_raccourci_pris(self, action, combinaison, pris):
        if not pris:
            self.reglages.fin_raccourci(action, "Déjà prise par un autre programme, choisis-en une autre")
            return
        if action == "pause" and self.raccourci_actif is not None:  # le raccourci de secours n'a plus lieu d'être
            try:
                keyboard.remove_hotkey(self.raccourci_actif)
            except (KeyError, ValueError):
                pass
            self.raccourci_actif = None
        self.config[RACCOURCIS[action][0]] = combinaison
        sauver_config(self.config)
        self.touches_capture = self._touches_capture()
        self.reglages.fin_raccourci(action)
        self.icone.update_menu()

    def _cmd_langue(self, code):
        if code != self.config["langue_cible"]:
            self.choisir_langue(code)

    def choisir_langue_ecriture(self, code):
        """Ctrl+Alt+Entrée traduit ce qu'on écrit vers cette langue (son modèle se télécharge s'il manque)."""
        self.config["langue_ecriture"] = code
        sauver_config(self.config)
        self.synchroniser_modeles()

    def choisir_mots(self, lignes):
        """« Mes mots » modifiés : tout est retraduit avec les nouvelles règles (dans un autre fil : le
        traducteur peut être en train de traduire)."""
        self.config["mots"] = [l.strip() for l in lignes if l.strip()][:500]
        sauver_config(self.config)
        self.calque.effacer()

        def appliquer():
            self.traducteur.changer_mots(self.config["mots"])
            self.reveil.set()

        threading.Thread(target=appliquer, daemon=True).start()

    def _cmd_chargement(self, en_cours):
        """Anneau de chargement : seulement si la traduction prend plus de 0,3 s (sinon il clignoterait)."""
        self.en_chargement = en_cours
        if not en_cours:
            self.calque.chargement(False)
        else:
            self.root.after(300, lambda: self.calque.chargement(self.en_chargement and self.actif))

    def _cmd_activer(self, actif):
        if actif != self.actif:
            self._activer(actif)

    def quitter(self):
        log.info("Arrêt")
        self.traducteur.sauver()
        self.arret.set()
        self.ia.moteur.arreter()
        self.reveil.set()
        self.reveil_accessibilite.set()
        keyboard.unhook_all()
        self.icone.stop()
        self.root.destroy()

    def _cmd_basculer(self):
        self.basculer()

    def _cmd_reglages(self):
        self.reglages.montrer()

    def _cmd_quitter(self):
        self.quitter()

    def _cmd_badge(self, texte):
        if self.actif:
            self.calque.badge(texte)

    # ---- mises à jour

    def _surveiller_mises_a_jour(self):
        """(Fil à part.) Une nouvelle version publiée ? Regardé peu après le lancement, puis toutes les 6 h."""
        self.arret.wait(20)
        while not self.arret.is_set():
            try:
                trouvee = derniere_version()
                if trouvee and numero_de_version(trouvee[0]) > numero_de_version(VERSION_PROGRAMME):
                    if self.mise_a_jour != trouvee:
                        log.info("Nouvelle version disponible : %s", trouvee[0])
                        self.mise_a_jour = trouvee
                        self.commandes.put(("mise_a_jour",))
            except Exception as e:  # (pas d'internet, GitHub indisponible : on réessaiera)
                log.info("Recherche de mise à jour impossible : %s", e)
            self.arret.wait(6 * 3600)

    def _cmd_mise_a_jour(self):
        self.reglages.montrer_mise_a_jour(self.mise_a_jour[0])
        self.icone.update_menu()
        # (Une notification de Windows : la fenêtre est souvent fermée, la carte de l'accueil passait inaperçue.)
        try:
            self.icone.notify(f"La version {self.mise_a_jour[0]} est disponible : clic droit sur l'icône du traducteur "
                              "(près de l'horloge), puis « Mettre à jour ».", "Traducteur d'écran")
        except Exception:
            log.exception("Notification de mise à jour impossible")

    def _cmd_installer_mise_a_jour(self):
        if self.mise_a_jour and not getattr(self, "maj_en_cours", False):
            self.maj_en_cours = True
            threading.Thread(target=self._installer_mise_a_jour, name="mise_a_jour", daemon=True).start()

    def _installer_mise_a_jour(self):
        """(Fil à part.) Télécharge l'installateur de la nouvelle version et le lance : il ferme ce programme,
        le remplace et le relance (réglages, langues et modèles sont gardés)."""
        version, lien = self.mise_a_jour
        chemin = os.path.join(tempfile.gettempdir(), f"TraducteurEcran-Installation-{version}.exe")

        def avancer(lu, total):
            self.commandes.put(("badge_toujours", f"Mise à jour : {lu // 2**20} / {total // 2**20} Mo"
                                if total else f"Mise à jour : {lu // 2**20} Mo"))

        try:
            if _telecharger([lien], chemin, avancer, self.arret.is_set):
                log.info("Installation de la version %s", version)
                subprocess.Popen([chemin, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
                self.commandes.put(("quitter",))
                return
        except Exception:
            log.exception("Mise à jour impossible")
            self._signaler("Mise à jour impossible (voir journal.txt) : réessaie plus tard")
        self.commandes.put(("badge_toujours", ""))
        self.maj_en_cours = False

    def _cmd_resultat(self, debut, cible, preparees):
        # Ignorer une lecture commencée avant un défilement (positions périmées), et ne rien
        # toucher pendant que les traductions suivent un défilement en direct.
        if (not self.actif or cible != self.config["langue_cible"] or debut < self.dernier_mouvement
                or self.suivi_en_cours):
            self.reveil.set()
            return
        if self._capture_utilisateur_en_cours():
            return  # capture d'écran en cours : les traductions restent telles quelles, figées, pour y être
        if self.image_a_jour < self.dernier_mouvement:
            # Calculé sur l'image d'avant le défilement (la lecture a attendu trop longtemps) : les places
            # seraient fausses. On attend l'image à jour (son analyse relancera la traduction).
            return
        self.calque.mettre_a_jour(preparees)
        self.reglages.actualiser(len(self.calque.affiches))
        if self.active_le is not None and self.calque.affiches:  # (pour mesurer l'attente au lancement)
            log.info("Premières traductions affichées %.1f s après l'activation", time.monotonic() - self.active_le)
            self.active_le = None

    # ---- lecture et traduction en continu (thread dédié)

    def _attendre_image_a_jour(self, reveil):
        """Après un défilement, attendre que l'image de l'écran soit relue (l'analyse d'image attend
        elle-même que plus rien ne bouge) : les positions sont alors les bonnes."""
        while not self.arret.is_set() and (self.suivi_en_cours or (
                self.image_a_jour < self.dernier_mouvement and time.monotonic() - self.dernier_mouvement < 1.5)):
            reveil.clear()
            reveil.wait(0.03)
        reveil.clear()

    def _boucle_accessibilite(self):
        """Lit l'accessibilité de Windows (texte exact, pseudos, images…) dans son propre fil : sur une
        grosse page, ça prend jusqu'à une seconde, et la traduction n'a pas à l'attendre."""
        ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        lecteur = None
        while lecteur is None and not self.arret.is_set():
            try:
                lecteur = Lecteur()
            except Exception:  # accessibilité de Windows indisponible : on le dit, et on réessaie
                log.exception("Accessibilité de Windows indisponible")
                self.commandes.put(("badge", "Accessibilité de Windows indisponible (voir journal.txt)"))
                self.arret.wait(5)
        if lecteur is None:
            return
        lecteur.filtre = self._filtre_programmes()
        self.lecteur = lecteur
        derniere_lecture = 0.0
        while not self.arret.is_set():
            self.reveil_accessibilite.wait(self.config["intervalle"])
            self.reveil_accessibilite.clear()
            if not self.actif or self.arret.is_set():
                continue
            self._attendre_image_a_jour(self.reveil_accessibilite)
            debut = time.monotonic()
            # Rien n'a bougé à l'écran et aucune action depuis la dernière lecture : inutile de relire.
            if derniere_lecture and self.derniere_interaction < derniere_lecture \
                    and self.image_change_le < derniere_lecture and not lecteur.a_relire:
                continue
            try:
                # La fenêtre active n'est relue qu'après une interaction (clic, touche, défilement)
                # ou une fois par seconde si l'écran change.
                relire = self.derniere_interaction >= derniere_lecture or debut - derniere_lecture > 1.0
                if relire:
                    derniere_lecture = debut
                lecteur.lire(relire_active=relire)
                self.mesures["accessibilite"].append(time.monotonic() - debut)
                self.medias_lus = (time.monotonic(), [v[:4] for v in lecteur.vus if v[5] == "image"])
                self.textes_lus = (time.monotonic(), [v[:4] for v in lecteur.vus
                                                      if v[5] == "traduire" and v[3] - v[1] <= 120])
                self.reveil.set()  # la traduction refait la fusion avec ces informations à jour
            except Exception:
                log.exception("Échec de la lecture de l'accessibilité")
                self.arret.wait(2)

    def _boucle_lecture(self):
        traducteur = self.traducteur
        self.teintes = {}  # Texte -> (fond, texte, nombre de lignes) lus à l'écran
        erreur_affichee = False
        while self.lecteur is None and not self.arret.is_set():
            self.arret.wait(0.05)  # créé par le fil de l'accessibilité
        lecteur = self.lecteur
        while not self.arret.is_set():
            self.reveil.wait(self.config["intervalle"])
            self.reveil.clear()
            if self.oubli_demande:
                self.oubli_demande = False
                self._oublier(traducteur)
            if not self.actif or self.arret.is_set():
                continue
            self._attendre_image_a_jour(self.reveil)
            debut = time.monotonic()
            try:
                cible = self.config["langue_cible"]
                # Ce que l'accessibilité voyait dans une fenêtre qui a défilé, ou où l'on vient de
                # cliquer (la page a pu changer), n'est pas utilisé tant qu'elle ne l'a pas relue :
                # on traduit tout de suite d'après l'image, puis on affine.
                textes = self._fusionner(lecteur, lecteur.perimees(self.derniere_interaction))
                if debut - getattr(self, "_dernier_releve", 0) > 10:
                    # Relevé dans le journal, pour comprendre ce qui est traduit ou non, et le temps pris.
                    self._dernier_releve = debut
                    mesures, self.mesures = self.mesures, {"image": [], "traduction": [], "accessibilite": []}
                    log.info("Relevé : %d éléments vus par l'accessibilité, %d morceaux dans l'image, "
                             "%d textes à traduire, %d traductions affichées ; lecture de l'image %s ; "
                             "accessibilité %s ; traduction de %d textes nouveaux %s",
                             len(lecteur.vus), len(self.textes_image), len(textes), len(self.calque.affiches),
                             duree_moyenne(mesures["image"]), duree_moyenne(mesures["accessibilite"]),
                             sum(n for n, _ in mesures["traduction"]),
                             duree_moyenne([d for _, d in mesures["traduction"]]))
                # Ce qui est déjà traduit s'affiche tout de suite ; le reste arrive par petits paquets,
                # dans l'ordre de lecture (de haut en bas).
                nouveaux = list(dict.fromkeys(t.texte for t in sorted(textes, key=lambda t: (t.y, t.x))
                                              if not traducteur.connu(t.texte, cible)))
                self._publier(debut, cible, textes, traducteur)
                if nouveaux:
                    self.commandes.put(("chargement", True))
                try:
                    for i in range(0, len(nouveaux), TAILLE_PAQUET):
                        if self.dernier_mouvement > debut:
                            break  # l'écran a bougé : on recommence la lecture
                        paquet = time.monotonic()
                        traducteur.traduire(nouveaux[i:i + TAILLE_PAQUET], cible)
                        self.mesures["traduction"].append((len(nouveaux[i:i + TAILLE_PAQUET]), time.monotonic() - paquet))
                        self._publier(debut, cible, textes, traducteur)
                finally:
                    if nouveaux:
                        self.commandes.put(("chargement", False))
                # IA locale (réglage) : ce qui est à l'écran, dans l'ordre de lecture, pour qu'elle l'améliore.
                self.ia.proposer([t.texte for t in sorted(textes, key=lambda t: (t.y, t.x))], cible)
                if erreur_affichee:
                    self.commandes.put(("badge", ""))
                    erreur_affichee = False
                if debut - getattr(self, "_derniere_sauvegarde", 0) > 15:  # (rien n'est écrit si rien n'a changé)
                    self._derniere_sauvegarde = debut
                    traducteur.sauver()
            except Exception as e:
                log.exception("Échec de la lecture ou de la traduction")
                self.commandes.put(("badge", f"Erreur : {e}"[:90]))
                erreur_affichee = True
                self.arret.wait(5)

    def _fusionner(self, lecteur, perimees=()):
        """Les textes à traduire, paragraphe par paragraphe : une phrase coupée en plusieurs
        lignes, ou par un lien, se traduit bien mieux en entier. Ce qui s'affiche vient de l'image
        de l'écran : seulement le texte vraiment visible, à sa position exacte. Quand
        l'accessibilité voit le même texte, elle donne le texte exact (sans erreur de lecture) et
        ses décisions (pseudo, code, onglet…). `perimees` : fenêtres qui viennent de défiler, dont
        on ne se sert pas de ce que l'accessibilité y voyait (positions d'avant le défilement)."""
        vus = lecteur.vus_hors(perimees) if perimees else lecteur.vus
        # Programmes à ne pas traduire (réglages) : écartés d'après la fenêtre où le texte a été lu, sans
        # attendre l'accessibilité (une fenêtre posée devant, une lecture en retard n'y changent rien).
        # Zone choisie (raccourci) : seulement elle, quel que soit le programme, et même le texte d'une image.
        zone = getattr(self, "zone_choisie", None)
        if zone:
            morceaux = [m for m in self.textes_image if self._dans_zone(m)]
        else:
            morceaux = [m for m in self.textes_image if lecteur._a_traduire(m.programme)]
        origines = getattr(getattr(self, "suivi", None), "origines", {})
        if vus:
            boites = np.array([v[:4] for v in vus], dtype=np.int64)
            aires = np.maximum(1, (boites[:, 2] - boites[:, 0]) * (boites[:, 3] - boites[:, 1]))
        decisions = {}
        # Images et vidéos : d'après le dernier relevé, même dans la fenêtre où l'on vient de cliquer ou d'écrire
        # (un stream regardé en discutant ne bouge pas pour autant : sans ça, son texte était traduit entre deux
        # relectures, puis plus, puis encore… il clignotait). Seule une fenêtre qui a défilé ou bougé n'en donne pas.
        try:
            bougees = set(lecteur.a_relire)
        except RuntimeError:  # modifié pendant la copie
            bougees = set(perimees)
        medias = [v[:4] for v in (lecteur.vus_hors(bougees) if bougees else lecteur.vus) if v[5] == "image"]
        for m in morceaux:
            # Dans une image ou une vidéo : jamais traduit, même du vrai texte posé dessus
            # (sous-titres, titre en surimpression, durée d'une miniature…).
            cadre = (m.x, m.y, m.x + m.l, m.y + m.h)
            media = None if zone else next((r for r in medias if surface_commune(cadre, r) >= 0.5 * m.l * m.h), None)
            if media is not None:
                decisions[m] = ((*media, ""), "", "image")
                continue
            choisi = None
            if vus:
                largeur = np.minimum(boites[:, 2], m.x + m.l) - np.maximum(boites[:, 0], m.x)
                hauteur = np.minimum(boites[:, 3], m.y + m.h) - np.maximum(boites[:, 1], m.y)
                part = np.clip(largeur, 0, None) * np.clip(hauteur, 0, None) / max(1, m.l * m.h)
                candidats = np.nonzero(part >= 0.5)[0]
                if len(candidats):
                    choisi = int(candidats[aires[candidats].argmin()])  # l'élément le plus précis
            if choisi is not None:
                x1, y1, x2, y2, nom, statut = vus[choisi]
                decisions[m] = ((x1, y1, x2, y2, nom), nom, statut)
            elif m in self.memoire_fusion:
                # L'accessibilité ne le voit plus (navigateur en veille…) alors que le texte n'a
                # pas bougé : on garde sa décision d'avant, pour qu'il ne clignote pas.
                decisions[m] = self.memoire_fusion[m]
            elif origines.get(m) in self.memoire_fusion:
                # Texte qui a seulement glissé (défilement) : sa décision le suit.
                origine = origines[m]
                (x1, y1, x2, y2, nom), nom_vu, statut = self.memoire_fusion[origine]
                dy = m.y - origine.y
                decisions[m] = ((x1, y1 + dy, x2, y2 + dy, nom), nom_vu, statut)
        self.memoire_fusion = decisions
        # Les lignes à traduire : pas les onglets ni barres d'outils, ni une ligne qui n'est qu'un
        # pseudo, un bout de code, un lien ou des nombres (elles restent des obstacles à l'affichage).
        lignes = []
        for m in morceaux:
            statut = decisions[m][2] if m in decisions else None
            if statut == "champ":
                # Le champ n'affiche que son texte d'aide (rien d'écrit) : traduit. Sinon, ce qu'on écrit : jamais.
                aide = decisions[m][1]
                if SequenceMatcher(None, aide.casefold(), m.texte.casefold()).ratio() < 0.8:
                    continue
                statut = "traduire"
            # (Une ligne qui cite du code, « UNITY_TRANSFER_DEPTH(<v2f>) doesn't do anything… », reste à traduire
            # si l'accessibilité y voit une phrase : le code y est protégé. Sans elle, on s'en tient au texte lu.)
            if statut in ("ignorer", "pseudo", "autre") or (statut == "image" and not zone) \
                    or (statut != "traduire" and RESSEMBLE_A_DU_CODE.search(m.texte)) or surtout_des_chiffres(m.texte):
                continue
            lignes.append(m)
        textes = []
        element = lambda m: decisions[m][0] if m in decisions else None  # noqa: E731
        for paragraphe in regrouper_paragraphes(lignes, element):
            lu = assembler_morceaux(paragraphe)
            noms = list(dict.fromkeys(decisions[m][1] for m in sorted(paragraphe, key=lambda m: (m.y, m.x))
                                      if m in decisions))
            nom = texte_exact(lu, noms)
            if surtout_des_chiffres(nom):
                continue
            if len(paragraphe) == 1 and paragraphe[0] not in decisions \
                    and lecteur._est_pseudo(nom, None, (0, 0, 0, 0)):
                continue
            textes.append(texte_depuis_morceaux(nom, paragraphe))
        return sans_doublons(textes)  # jamais deux traductions l'une sur l'autre

    def _publier(self, debut, cible, textes, traducteur):
        affichees = None
        gardes = []
        for t in textes:
            traduit = traducteur.cache.get((t.texte, cible))
            if not traduit:
                # Pas (encore) de traduction pour ce texte : si c'est le même texte qu'une traduction déjà
                # affichée à cette place, lu un peu différemment (une lettre près), on la garde au lieu de
                # la retirer puis la remettre (clignotement).
                if affichees is None:
                    affichees = [a for a, _, _ in self.calque.instantane()]
                traduit = self._traduction_voisine(t, affichees, traducteur, cible)
            if traduit:
                gardes.append((t, traduit))
        self._noter_historique(gardes)
        couleurs = self._couleurs([t for t, _ in gardes])
        preparees = self.calque.preparer([g[0] for g in gardes], [g[1] for g in gardes], couleurs, self.textes_image)
        self.commandes.put(("resultat", debut, cible, preparees))

    def _noter_historique(self, paires):
        """Rubrique « Historique » : les traductions qui viennent d'apparaître (pas encore une fois le même texte
        resté à l'écran : seulement s'il n'y était plus depuis 10 minutes). Gardé en mémoire seulement."""
        maintenant = time.monotonic()
        for t, traduit in paires:
            original = t.texte.replace(MARQUE_CODE, "") if hasattr(t, "texte") else t
            if maintenant - self._historique_vu.get(original, -1e9) > 600:
                self.historique.append((time.strftime("%H:%M"), original, traduit.replace(MARQUE_CODE, "")))
            self._historique_vu[original] = maintenant
        if len(self._historique_vu) > 5000:
            self._historique_vu = {o: v for o, v in self._historique_vu.items() if maintenant - v <= 600}

    @staticmethod
    def _traduction_voisine(texte, affichees, traducteur, cible):
        """La traduction affichée à la même place pour un texte presque identique (même texte relu avec une
        petite différence), ou None."""
        for autre in affichees:
            if autre.texte != texte.texte and Calque._meme_place(autre, texte) \
                    and SequenceMatcher(None, autre.texte, texte.texte).ratio() >= 0.85:
                traduit = traducteur.cache.get((autre.texte, cible))
                if traduit:
                    return traduit
        return None

    def _couleurs(self, textes):
        """Couleurs du fond et du texte d'origine, lues sur l'écran (sans nos traductions)."""
        manquants = [t for t in textes if t not in self.teintes]
        if manquants and self.config["style"] == "auto":
            capture = self.image_courante  # dernière image analysée (sans nos traductions)
            if capture:
                pixels, origine = capture
                for t in manquants:
                    try:
                        self.teintes[t] = couleurs_autour(pixels, origine, t)
                    except Exception:
                        log.exception("Lecture des couleurs impossible")
                        self.teintes[t] = None
            if len(self.teintes) > 5000:
                self.teintes = {t: self.teintes.get(t) for t in textes}
        return [self.teintes.get(t) for t in textes]

    # ---- icône de la zone de notification

    def _creer_icone(self):
        def envoyer(*commande):
            return lambda icon, item: self.commandes.put(commande)

        def langue(nom, code):
            return pystray.MenuItem(nom, envoyer("langue", code), radio=True,
                                    checked=lambda item: self.config["langue_cible"] == code)

        menu = pystray.Menu(
            pystray.MenuItem("Ouvrir le traducteur", envoyer("reglages"), default=True),
            pystray.MenuItem(lambda item: f"Mettre à jour (version {self.mise_a_jour[0]})" if self.mise_a_jour else "",
                             envoyer("installer_mise_a_jour"), visible=lambda item: self.mise_a_jour is not None),
            pystray.MenuItem(lambda item: "Traduction en direct" + (
                                 f" ({self._nom_raccourci('pause')})" if self.config["raccourci"] else ""),
                             envoyer("basculer"), checked=lambda item: self.actif),
            pystray.MenuItem("Traduire vers", pystray.Menu(*(langue(nom, code) for nom, code in LANGUES.items()))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", envoyer("quitter")),
        )
        # Deux icônes : en couleur quand la traduction marche, grisée en pause (l'état se voit d'un coup d'œil).
        self.image_icone = dessiner_icone(64)
        gris = self.image_icone.convert("LA").convert("RGBA")
        gris.putalpha(self.image_icone.getchannel("A").point(lambda a: a * 55 // 100))
        self.image_icone_pause = gris
        return pystray.Icon(APP_NAME, self.image_icone, "Traducteur d'écran", menu)


# --------------------------------------------------------------------------- démarrage

def main():
    os.makedirs(APP_DIR, exist_ok=True)
    # Journal limité à 2 Mo (plus une copie du précédent) : il ne grossit pas sans fin.
    logging.basicConfig(  # (TRADUCTEUR_JOURNAL_DETAILLE=1 : chaque traduction posée ou retirée, pour chercher un bug)
        level=logging.DEBUG if os.environ.get("TRADUCTEUR_JOURNAL_DETAILLE") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=1, encoding="utf-8")],
    )
    # Passage plus fréquent d'un fil à l'autre : le suivi du défilement et la souris n'attendent pas
    # qu'un calcul long d'un autre fil (lecture d'une page, regroupement des lignes) rende la main.
    sys.setswitchinterval(0.002)

    # Aucune erreur ne passe inaperçue : sans console (pythonw, version .exe), elle n'irait nulle part.
    sys.excepthook = lambda type_, valeur, trace: log.critical("Erreur non rattrapée",
                                                               exc_info=(type_, valeur, trace))
    threading.excepthook = lambda infos: log.critical(
        "Erreur dans le fil « %s »", getattr(infos.thread, "name", "?"),
        exc_info=(infos.exc_type, infos.exc_value, infos.exc_traceback))

    # Une seule instance à la fois.
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return

    # Coordonnées en pixels réels, même avec une mise à l'échelle Windows (125 %, 150 %…).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()
    # Notre propre icône dans la barre des tâches (et non celle de Python).
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)
    assurer_icone()

    try:
        Application(montrer_reglages=ARG_DEMARRAGE not in sys.argv).lancer()
    except Exception:
        log.exception("Erreur fatale")
        raise


if __name__ == "__main__":
    main()
