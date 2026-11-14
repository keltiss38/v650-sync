🇬🇧 [English version](TECHNICAL-NOTES.md)

# V650 Sync — notes techniques

> Application Windows libre et non officielle pour utiliser un **Polar V650** (séances, parcours, cartes hors ligne).
> Version du code : `0.1.0` · Dernière mise à jour de ce document : 12 novembre 2026.
> Ce document ne contient ni identifiant, ni mot de passe, ni numéro de série.
> Projet indépendant, **non affilié à Polar, Komoot, Strava ni Intervals.icu**. Écrit avec l'aide de Claude (Anthropic).

## Sommaire

1. [Contexte](#1-contexte)
2. [Fonctionnalités et niveau de validation](#2-fonctionnalités-et-niveau-de-validation)
3. [Architecture](#3-architecture)
4. [Protocole PFTP et formats du compteur](#4-protocole-pftp-et-formats-du-compteur)
5. [Parcours](#5-parcours)
6. [Cartes hors ligne](#6-cartes-hors-ligne)
7. [Intégrations externes](#7-intégrations-externes)
8. [API locale](#8-api-locale)
9. [Journal, tests et construction](#9-journal-tests-et-construction)
10. [Mesures et validations](#10-mesures-et-validations)
11. [Incidents rencontrés](#11-incidents-rencontrés)
12. [Limites connues et pistes](#12-limites-connues-et-pistes)

---

## 1. Contexte

Le **Polar V650** est un compteur de vélo GPS de 2014. Il se branche au PC par un câble micro-USB et se présente comme une **carte réseau virtuelle (RNDIS)** : pas de lecteur de disque. Le logiciel officiel, **Polar FlowSync**, dépend du service **Bonjour** d'Apple pour le trouver ; sous Windows 10 et 11, ce service est souvent absent ou refusé, et FlowSync n'est plus guère maintenu pour ce modèle. La synchronisation Bluetooth avec l'application mobile n'a pas fonctionné dans mon cas.

Autre limite du matériel d'origine : les **cartes hors ligne** installables par FlowSync sont des blocs régionaux fabriqués par Polar en **août 2015** (cartographie obsolète depuis plus de dix ans), et le compteur n'accepte que la carte d'un seul bloc à la fois.

Objectifs du projet : synchroniser sans Bonjour ni compte, exporter les séances, gérer les parcours (dont Komoot), et **fabriquer soi-même des cartes à jour** (OpenStreetMap), le tout dans une seule application Windows.

Décisions de cadrage : pas d'analyse d'entraînement (visualiseur sobre) ; pas de modification du firmware (il n'a jamais été modifié ni lu en vue d'une modification) ; pas d'API Strava (payante, et Intervals.icu n'envoie pas d'activités vers Strava : bouton de dépôt manuel à la place) ; design propre (« tableau de bord de vélo »).

## 2. Fonctionnalités et niveau de validation

Légende : ✅ validé sur le **vrai compteur** ou le **vrai service** · 🧪 testé en local (faux serveur, faux compteur, données locales) · ⏳ non testé.

| Fonctionnalité | Statut | Détail |
|---|---|---|
| Découverte mDNS sans Bonjour (`zeroconf`) | ✅ | connexion en parallèle sur tous les couples service/interface, ~3 s |
| Lecture des fichiers et dossiers, gros fichiers | ✅ | protocole PFTP reconstitué (§4) |
| Sauvegarde locale complète | ✅ | ~300 fichiers, carte (~325 Mo) ignorée par défaut |
| État (batterie, espace), profil, heure | ✅ | |
| Réglage de l'heure du compteur | ✅ | avec confirmation |
| Séances : liste, détail, courbes, carte | ✅ | |
| Export GPX et FIT | ✅ | validés contre les exports de Polar Flow (§10) |
| Écriture, mise à jour et suppression de parcours | ✅ | relecture octet par octet ; projection validée sur l'écran du compteur |
| Bibliothèque de parcours unifiée (compteur, Komoot, GPX) | ✅ | statut « synchronisé » décidé sur la trace |
| **Komoot** (connexion, liste, GPX, écriture sur le compteur) | ✅ | compte réel ; origine « Komoot » reconnue grâce à l'identifiant noté dans le parcours par FlowSync |
| **Intervals.icu** (test, envoi manuel d'une séance) | ✅ | service réel |
| **Envoi d'une séance à Komoot** (FIT) | 🧪 | API interne de Komoot (comme `kompy.upload_tour`) ; testé avec un faux serveur seulement, **pas encore sur un compte réel** |
| Envoi automatique des nouvelles séances (Intervals, Komoot, GPX) | 🧪 | désactivé par défaut |
| Fabrication de cartes (Mapsforge v3, OpenStreetMap) | ✅ | cartes affichées par le compteur ; zone dessinée respectée |
| Écriture d'une carte sur le compteur (multi-trames, relecture, empreinte) | ✅ | ~2,2 Mo/s |
| Éditeur de carte (zone, outils de dessin, niveaux de détail) | ✅ | |
| Fenêtre modale de suivi (étapes, barres, durée, annulation) | ✅ | |
| Journal détaillé, rapport de bug | ✅ | |
| Exécutable `V650Sync.exe` (~14 Mo, sans console, non signé) | ✅ | Windows 11 ; **Windows 10 non testé** |
| Courbes de niveau sur les cartes générées | 🧪 | option aucune / principales / complètes (altitudes SRTM, §6.4) ; **affichage sur le compteur non vérifié**, désactivées par défaut |
| Modification du profil du compteur (prénom, nom, poids, taille, FC, VO2max, FTP) | 🧪 | copie préalable des deux fichiers, écriture, relecture, retour arrière automatique et restauration : testés avec un faux compteur ; **écriture non testée sur le vrai compteur** (le compteur peut refuser le fichier ou réécrire ses valeurs). L'e-mail et l'identifiant Polar ne sont pas modifiables |
| Interface en français et en anglais ; unités (km / miles) selon le réglage du compteur | 🧪 | traduction à l'exécution (§3.6) ; vérifiée par balayage automatique de la page, pas par un relecteur anglophone |
| Avis de mise à jour (GitHub, une fois par jour, désactivable) | 🧪 | testé avec un faux serveur ; **ne fonctionne qu'une fois une version publiée** sur le dépôt public (§7) |
| Export des intervalles RR, température, `device_info` dans le FIT | ❌ | absents du FIT |

## 3. Architecture

### 3.1 Vue d'ensemble

```
 Polar V650            câble USB                     PC Windows 10/11
 ┌────────────┐   ┌───────────────┐    TCP/IPv6    ┌────────────────────────────────────────┐
 │ serveur    │◄─►│ carte réseau  │◄──────────────►│ v650sync (Python)                      │
 │ PFTP       │   │ virtuelle     │  mDNS          │  discover.py · pftp.py · backup.py     │
 │ (protobuf) │   │ RNDIS         │ _polar-ftp._tcp│  service.py · mapgen.py · mapservice.py│
 └────────────┘   └───────────────┘                │  webui.py (serveur HTTP 127.0.0.1)     │
                                                   │        ▲                               │
                      Fenêtre native (WebView2) ◄──┼────────┘  page unique index.html       │
                                                   └────────────────────────────────────────┘
                                   HTTPS (selon vos actions) : Intervals.icu · Komoot · OSM · Geofabrik
```

Un **processus Python unique** : module de protocole, serveur HTTP local (uniquement `127.0.0.1`), une page web unique affichée dans une fenêtre native (`pywebview` + WebView2, avec repli sur une fenêtre Edge/Chrome « mode application », puis sur le navigateur par défaut). L'ensemble est empaqueté en un `.exe` par PyInstaller.

### 3.2 Choix technologiques

| Choix | Pourquoi |
|---|---|
| Python 3, bibliothèque standard d'abord | une seule dépendance obligatoire (`zeroconf`) ; code lisible et auditable |
| `zeroconf` | découverte `_polar-ftp._tcp` sans Bonjour |
| Protobuf décodé à la main | zéro dépendance ; formats simples et stables |
| Serveur HTTP local + page HTML unique | interface riche (cartes, courbes) sans toolkit lourd |
| `pywebview` + WebView2 | vraie fenêtre d'application, croix interceptable pour un arrêt propre |
| Encodeur FIT maison (`fit.py`) | pas de dépendance, contrôlé octet par octet |
| Tuiles OpenStreetMap en ligne pour les aperçus | aucune clé d'API ; hors ligne, seul le tracé s'affiche |
| Cartes : Java + Osmosis + `mapsforge-map-writer 0.5.1` téléchargés à la demande | format imposé par le compteur (§6) ; pas dans l'exe (poids) |

### 3.3 Liaison avec le compteur

1. **USB / RNDIS** : Windows équipe l'adaptateur d'une adresse IPv6 link-local (`fe80::…`) ; le compteur est sur le même lien.
2. **mDNS / DNS-SD** : le compteur annonce `_polar-ftp._tcp.local.` (nom, port, adresses). `discover.find_devices()` écoute via `zeroconf`.
3. **Enregistrements périmés** : le compteur peut annoncer plusieurs services, certains avec un ancien port mort. `connect_any()` essaie **en parallèle tous les couples (service, interface)** (jusqu'à 48, délai 2 s) et garde le premier qui envoie la bannière PFTP. Une adresse `fe80::` n'a de sens que sur une interface précise (une machine en a des dizaines) ; l'interface qui a marché est mémorisée.
4. **Un seul client à la fois** : une session non refermée proprement rend le compteur muet jusqu'au débranchement. D'où `shutdown(SHUT_RDWR)` à la fermeture, un verrou global (`service.LOCK`) et un **arrêt gracieux** (la lecture s'arrête entre deux fichiers, la session est refermée, puis le serveur et la fenêtre). *Interdit en test : tuer le serveur de force pendant une lecture ; utiliser `/api/quit`.*

### 3.4 Serveur local et sécurité

- `ThreadingHTTPServer` lié à `127.0.0.1`, port libre aléatoire, jamais exposé au réseau.
- Contrôle de l'en-tête `Host` (anti DNS rebinding) et **jeton aléatoire** (`X-Token`) exigé pour toute requête `POST`.
- Aucune redirection suivie vers Komoot ; adresses externes ouvertes par l'application = **liste fixe** (`/api/open-external`).
- **Secrets** (clé Intervals.icu, jeton Komoot) chiffrés par **DPAPI Windows** (`secretstore.py`) : déchiffrables seulement par le même compte Windows sur ce PC. Le mot de passe Komoot n'est jamais enregistré. Les réglages (`config.json`) sont partagés par un seul objet dans tout le programme, relu s'il change sur le disque (évite qu'une copie périmée écrase une clé enregistrée).
- La page n'insère jamais de texte externe sans l'échapper.

### 3.5 Tâches longues

Deux emplacements indépendants : `main` (opérations sur le compteur) et `build` (fabrication de cartes, téléchargement d'outils). Chaque tâche a un titre, des **étapes** (état, fait/total), une progression, un journal et une heure de début (`jobs.py`). La page les suit sur tous les onglets ; la fabrication et l'écriture d'une carte ouvrent une **fenêtre bloquante** avec une ligne et une barre par étape (outils, chaque région téléchargée, fabrication, contrôle), une barre globale, une durée annoncée, « Annuler » (fabrication seulement) et, une fois la carte fabriquée, « Écrire cette carte sur le compteur » (**jamais automatique**).

### 3.6 Interface

Page unique `v650sync/index.html` (HTML + CSS + JS, sans bibliothèque), thèmes clair / sombre / automatique, une seule barre de défilement. Onglets : *Séances* (liste, détail, courbes, exports), *Parcours* (bibliothèque unifiée : compteur + Komoot + GPX ; tri par date, distance, dénivelé, nom ; filtres ; écriture, mise à jour, suppression), *Carte* (éditeur : zone dessinée en direct, parcours en couches, carte actuelle du compteur en contour, niveaux de détail Léger / Vélo / Détaillé, estimation de taille et de durée), *Connexions* (Intervals.icu : clé d'API ; Komoot : connexion et envoi des séances ; envoi automatique ; Strava), *Compteur* (profil, heure, unités d'affichage, dossier de données, journal, « À propos » avec version, lien vers le code source, licence et réglage de la vérification des mises à jour). Un petit repère « Mise à jour disponible » apparaît dans la barre du haut quand une version plus récente est publiée.

**Langue.** Le français est le texte source de la page (`index.html`) et des messages du programme. En anglais, un traducteur côté navigateur (`tr()`) remplace les textes à l'affichage : dictionnaire de phrases exactes et de modèles à variables (`{0}`, `{1}`… numérotées dans l'ordre d'apparition), fichier `v650sync/i18n_en.json` (~450 phrases, ~160 modèles), injecté dans la page ; un observateur (`MutationObserver`) traduit aussi les textes ajoutés dynamiquement et les attributs `title` / `placeholder`. Un texte sans traduction reste en français, jamais d'erreur. Le journal et le code restent en français. Choix : automatique (langue de Windows), `fr` ou `en` (bouton en haut de la page, réglage `lang`).

**Unités.** Indépendantes de la langue : le programme suit la préférence du compteur (métrique ou impériale, lue dans son profil et mémorisée dans `device_metric` pour l'affichage au lancement suivant), ou un choix manuel (réglage `units` : `auto` / `metric` / `imperial`). Le serveur renvoie toujours des km, km/h et m ; la conversion (miles, mph, pieds, livres, pieds-pouces) se fait à l'affichage.

### 3.7 Données et fichiers

- Dossier de données : `V650SYNC_DATA` > choix de l'utilisateur (`settings.json`) > `~/V650 Sync`. Rien n'est créé avant le choix au premier lancement.
- Contenu : `sauvegarde/<série>/…` (copie du compteur), `export/`, `komoot/` (GPX), `parcours/`, `cartes/`, `rapports/`, `config.json`.
- Fichiers de l'application dans `%LOCALAPPDATA%\V650Sync\` : `settings.json` (thème, langue, unités, dossier de données, réglage et état de la vérification des mises à jour), `running.json`, `journal.txt` (+ 3 anciens), profil du repli Edge.
- **Instance unique** (mutex `Local\V650Sync`) : un deuxième lancement ramène la fenêtre existante.

## 4. Protocole PFTP et formats du compteur

Protocole reconstitué en capturant le trafic entre FlowSync et le compteur (Wireshark), puis rejoué et validé.

- **Bannière** : une ligne commençant par `Polar FTP server`.
- **Trame** : en-tête 16 bits little-endian `(L << 2) | flags`, puis `L` octets (`L` ≤ 16 383).
- **GET** : `[len16][0x08 <cmd> 0x12 <len> <chemin>]` (`PbPFtpOperation`, GET = 0). Un chemin finissant par `/` est un **dossier** (`PbPFtpDirectory`).
- **Lecture d'un fichier** : trames de contenu puis une trame vide ; au-delà de 16 383 octets, le compteur n'envoie la trame suivante qu'après **deux trames vides** (`00 00 00 00`) renvoyées par le client.
- **PUT (1) / REMOVE (3)** : `[len16][opération][données]`. **Chaque écriture est acquittée par deux trames vides** à lire avant la requête suivante (sinon la suite est ajoutée au fichier en cours). Réponses : `00 00 00 00` = accepté ; `00 00` + code = refus (observé : 106 « opération non permise » pour un nom de fichier inconnu).
- **Écriture multi-trames** : seule `/U/0/MAP/0/MAPDATA.MAP` l'accepte. Un seul PUT dont la charge est découpée en trames de 16 383 octets (drapeau 0 puis 1 pour les continuations) ; le compteur renvoie le jeton `00 02 00 00` après chaque trame et l'on attend ce jeton avant la suivante ; acquittement final `00 00 00 00`. Les autres fichiers (dont les parcours) tiennent en **une trame** : un parcours doit faire ≤ 15 000 octets.
- **Requêtes courtes** (`PbPFtpQuery`, `[0x8000 | longueur][identifiant][paramètres]`) : 1 régler l'heure UTC, 2 lire l'heure UTC, 3 régler l'heure locale et le décalage (heures entières), 4 lire l'heure locale, 5 espace disque, 8 batterie.

Arborescence (`/U/0/` = utilisateur 0) :

```
/DEVICE.BPB                     identité (modèle, versions)
/U/0/S/PHYSDATA.BPB, PREFS.BPB  profil physique, préférences
/U/0/AAAAMMJJ/E/HHMMSS/         une séance : ID, TSESS, PHYSDATA, puis 00/ (BASE, STATS, LAPS, PROFILE, BIKE,
                                SAMPLES.GZB, ROUTE.GZB, RR.GZB)
/U/0/ROUTES/N/PROUTE.BPB        un parcours
/U/0/FAV/NN/TST.BPB, ID.BPB     le favori qui référence le parcours (20 au plus)
/U/0/MAP/0/MAPDATA.MAP, MAPINFO.BPB   carte hors ligne (Mapsforge) et ses métadonnées
```

Les fichiers `.BPB` sont du protobuf (définitions `PbTrainingSession`, `PbExerciseSamples`, `PbExerciseRouteSamples`, `PbPlannedRoute`… issues du projet `cmaion/polar` et vérifiées sur des fichiers du V650).

## 5. Parcours

- **Format** (`PROUTE.BPB`) : identifiant, nom, longueur, point de départ (lat/lon), altitude, **points `(x, y, t_ms, z)` en mètres relatifs au départ**, dates de création et de modification, et un champ d'identifiant externe (12) : FlowSync y inscrit le numéro du tour Komoot, ce qui permet de reconnaître l'origine sans interroger Komoot.
- **Du GPX au compteur** : lecture, simplification Ramer-Douglas-Peucker (≤ 450 points « Standard », ~850 « HD », ≤ 15 000 octets), projection locale (110 937 m par degré de latitude ; longitude sur l'ellipsoïde WGS84 à la latitude de départ — valeurs **mesurées** sur des parcours écrits par FlowSync, écart résiduel moyen < 1 m), nom nettoyé (30 caractères, sans accents), écriture `ROUTES/N` + `FAV/NN`, relecture comparée.
- **Bibliothèque** (`routelib.py`) : un parcours = une ligne ; fusion compteur / Komoot / GPX par l'identifiant Komoot puis par le nom ; « synchronisé » si les traces coïncident (écart moyen ≤ 12 m + 0,06 m/km, longueur à 3 %) ; dates « créé » et « modifié » comme sur Komoot, tri par dernière modification.
- **Mise à jour** d'un parcours : remplacement au même emplacement (suppression puis réécriture, comme FlowSync) ; l'ancien fichier est copié dans `parcours/avant-mise-a-jour/`.
- Plusieurs écritures peuvent être enchaînées ; le compteur doit être **éteint puis rallumé** pour afficher le résultat.
- **FlowSync efface les parcours qu'il ne connaît pas** (il supprime et réécrit tous les itinéraires à chaque synchro) : ne pas l'utiliser en parallèle.

## 6. Cartes hors ligne

### 6.1 Ce qu'attend le compteur

Fichier **Mapsforge version 3** (tuiles 256, Mercator, zooms de base 5 [0–7], 9 [8–9], 14 [10–21]), produit par `mapsforge-map-writer 0.5.1` (les cartes mapsforge actuelles en version 5 ne sont pas lisibles). Les tables de balises varient d'un bloc Polar à l'autre : le compteur identifie les balises par leur **texte**, pas par leur numéro. `MAPINFO.BPB` (46 octets) : `{1: centre {lat, lon}, 2: date-heure UTC, 3: drapeau}` ; **drapeau 0 = zone demandée sur le compteur, 1 = carte installée**. L'application sait lever elle-même le drapeau : plus aucune manipulation sur le compteur.

### 6.2 Comment FlowSync procède (observé)

FlowSync télécharge un bloc régional depuis un CDN de Polar, puis écrit `MAPDATA.MAP` en un seul PUT multi-trames et `MAPINFO.BPB` (drapeau 1). Le déclenchement vient du compteur (drapeau 0 + nouveau centre dans `MAPINFO.BPB`). Les blocs suivent une **grille** : cellules de 150 km, 1,35° en latitude, pas en longitude 150 km / (111,195 × cos(latitude du bord nord)) arrondi au centième ; le bloc est la cellule contenant la zone demandée et ses 8 voisines (450 km de côté), prédiction vérifiée sur trois blocs. Ce sont des services privés de Polar, sans API publique : l'application ne s'appuie pas dessus.

### 6.3 Fabrication

1. **Zone** : dessinée dans l'éditeur (centre d'un carré de 50 à 200 km, rectangle, cercle, polygone, crayon) ou **bande autour des parcours choisis** (rectangle orienté par tronçon simplifié, disques aux extrémités et aux coudes > 8°, `corridor_rings`), marge réglable. Anneaux écrits dans un `zone.poly`.
2. **Extraits OpenStreetMap** : régions Geofabrik couvrant la zone (`pick_regions`), téléchargées au besoin (tailles annoncées dans le plan) ; la **date des données** (en-tête du `.pbf`) est affichée dans le plan, la liste des cartes et la carte installée.
3. **Osmosis 0.48.3** lancé directement avec Java 17 (plexus-classworlds) :
   `--read-pbf … --bounding-polygon (zone.poly) [--merge] --tag-transform (adresses retirées) --mapfile-writer … zoom-interval-conf=5,0,7,9,8,9,14,10,21`. Osmosis 0.49 est incompatible avec le plugin ; même en 0.48.3 une erreur `AbstractMethodError: close()` apparaît **après** l'écriture (code de sortie non nul sans effet) : le fichier produit est donc contrôlé (`inspect_map`), pas le code de sortie.
4. **Découpage strict** (`completeWays=no completeRelations=no`, par défaut) : mesuré sur une bande de 284 km², « complete » donnait 50 % des tuiles hors zone et 230 s ; le mode strict 80 % dans la zone et 20 s. Contrepartie : au bord, une forêt ou un lac coupé peut perdre son remplissage. `tile_coverage` mesure la forme réelle d'après l'index de tuiles.
5. **Niveaux de détail** Léger / Vélo (défaut) / Détaillé : trois fichiers de balises ; le compteur affiche les noms des commerces mais pas leurs icônes.
   **Zoom d'apparition par balise** : chaque voie apparaît à partir d'un niveau de zoom qui dépend de son type. Ces niveaux sont relevés dans la carte de Polar (voies à **une seule balise**, 20 zones, `mapdata/zoom_polar.json`, 105 balises) et appliqués aux fichiers de balises : réseau principal, voies ferrées, cours d'eau et grandes forêts dès le zoom 10 ; routes de desserte, voies de service, pistes, la plupart des surfaces (herbe, prés, friches…) dès le 12 ; chemins, sentiers, escaliers dès le 13 ; jardins, parcs et terrains de jeux dès le 15. Une première version laissait les niveaux par défaut d'Osmosis (pistes et voies de service plus tard, escaliers au 16) : aux petits zooms, il manquait des routes par rapport à Polar.
6. **Écriture** (`mapservice.write_map`) : contrôle du fichier, **copie locale obligatoire** de la carte actuelle, marqueur « écriture en cours », levée du drapeau, `MAPDATA.MAP`, `MAPINFO.BPB`, **relecture complète et comparaison SHA-256**, reprise possible si interrompue.

Le compteur affiche un grand **carré blanc** autour de la zone (rectangle englobant de l'en-tête Mapsforge) : inévitable, la zone elle-même est exacte. Les cartes générées sont plus denses que celles de Polar (~1 Mo par 100 km² contre 0,16) : viser des zones de 100 à 200 km de côté, ou des bandes autour des parcours. Un avertissement apparaît au-delà de 8 000 km² ou 3 régions.

Les outils sont épinglés par empreinte SHA-256 et téléchargés **à la demande**, depuis leurs sites officiels (Eclipse Temurin, GitHub, Maven Central, Geofabrik).

### 6.4 Courbes de niveau (option)

- **Ce que contiennent les cartes de Polar** (mesuré sur un bloc) : des lignes marquées `contour_ext=elevation_major|medium|minor`, plus `ele=<altitude>` pour les deux premières classes. Majeures tous les 500 m et moyennes tous les 100 m (visibles dès le zoom 10), fines tous les 25 m environ (dès le zoom 11) : l'équidistance des fines est déduite des longueurs mesurées (rapport ≈ 15 lignes pour 500 m). Elles pèsent peu (~0,07 Mo par 100 km²).
- **Altitudes** : tuiles SRTM `.hgt.gz` d'1° (3601 × 3601 points, ~30 m ; 16,6 Mo compressées) du jeu public « Terrain Tiles » d'AWS, sans compte (`elevation-tiles-prod/skadi`). Domaine public (USGS : citer la source). La mer n'a pas de tuile (le serveur répond 403) : la zone est alors à 0 m, sans courbes. Les tuiles sont gardées dans `cartes/altitudes/` du dossier de données.
- **Calcul** (`v650sync/contours.py`, Python standard, sans numpy) : mosaïque des tuiles, cellules de la zone seulement (le coût suit la surface, pas le rectangle englobant), lignes de niveau par « marching squares », raccordement des segments, simplification (Douglas-Peucker, 6 m), points au plus 4 millions de cellules (au-delà : un point sur deux, puis trois…). Mesuré : 960 km² à 30 m en 2,4 s, ~1 500 lignes, 117 000 points.
- **Intégration** : fichier OSM XML (identifiants très élevés, horodatage obligatoire), lu par `--read-xml` puis fusionné (`--merge`) avec l'extrait OpenStreetMap avant l'écriture. **Sans `--bounding-polygon` sur ce flux** : avec les identifiants très élevés des courbes, ce filtre supprimait des lignes entières (4 982 voies gardées sur 6 948, 18 tuiles vides de la carte) ; le calcul s'arrête déjà aux bords de la zone (une cellule de marge). Les balises de courbes ont `zoom-appear` 10 / 11 comme chez Polar et **`force-polygon-line`** : sans cela, une courbe fermée est prise pour une surface et le compteur dessine un quadrillage aux bords des tuiles. Osmosis ne se ferme plus après une lecture XML : l'application l'arrête 20 s après « Finished writing file ».
- **Résultat** : carte de 28 × 28 km autour d'Annecy, 4,26 Mo avec toutes les courbes contre 3,69 Mo sans (+15 % ; environ +4 % avec les seules principales), fabriquée en 43 s.
- **À vérifier** : le rendu sur le compteur (finesse, lisibilité à 100 m et 200 m, fluidité), avant d'activer les courbes par défaut.

## 7. Intégrations externes

| Service | Rôle | Mise en œuvre |
|---|---|---|
| **Komoot** | Importer des itinéraires ; envoyer des séances | interface interne non documentée ; logique de `kompy` réécrite sans dépendance (`komoot.py`) : connexion Basic puis jeton chiffré, liste paginée, export GPX ; pauses entre requêtes ; e-mail masqué dans le journal. **Envoi** : `POST /v007/tours/?data_type=fit&sport=…&name=…`, corps = le FIT en octets bruts ; Komoot répond 201 (créé) ou 202 (séance déjà connue : rien n'est créé ni modifié) ; Komoot donne à la séance la **visibilité par défaut du compte** (l'application n'y touche pas, comme avec Polar Flow) ; la requête n'est jamais rejouée automatiquement |
| **Intervals.icu** | Envoyer les séances (FIT) | API REST : `POST /api/v1/athlete/{id}/activities` (multipart), authentification Basic `API_KEY:<clé>` ; **en-tête `User-Agent` personnalisé obligatoire** (le blocage Cloudflare refuse celui de Python) ; l'identifiant d'athlète retombe sur « 0 » si refusé ; l'envoi est mémorisé pour afficher « ✓ Intervals » |
| **GitHub** | Avis de mise à jour | `GET https://api.github.com/repos/keltiss38/v650-sync/releases/latest` (`updates.py`), anonyme, au plus **une fois par jour**, seul le numéro de version est transmis (en-tête `User-Agent`) ; compare l'étiquette de la dernière version à la version du programme ; l'adresse de la page de la version doit commencer par celle du dépôt, sinon on la remplace par la page des versions ; **rien n'est téléchargé ni installé**. Désactivable (`update_check`) ; échec réseau ou dépôt sans version : silencieux |
| **Strava** | — | pas de relais ni d'API : « Strava » télécharge le FIT et ouvre la page d'envoi de Strava |
| **OpenStreetMap** | Fond de carte des aperçus et données des cartes | tuiles `tile.openstreetmap.org` (respecter leur politique d'usage) ; extraits Geofabrik |

**Envoi automatique** (désactivé par défaut) : à l'activation, les séances déjà copiées sont « connues » (`auto.baseline`) ; après chaque lecture sans erreur, les séances plus récentes sont envoyées à Intervals.icu et/ou à Komoot (une seule fois, retentées en cas d'échec) et/ou écrites en GPX dans `export/`. L'historique n'est jamais envoyé en masse.

## 8. API locale

Serveur sur `127.0.0.1` ; les requêtes `POST` exigent le jeton de la page.

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/`, `/favicon.png` | page (jeton et thème injectés), icône |
| GET | `/api/job`, `/api/jobs` | tâche « compteur » ; les deux tâches (`main`, `build`) et l'heure du serveur |
| GET | `/api/status`, `/api/profile` | état et profil du compteur |
| GET | `/api/sessions`, `/api/session/<id>`, `/download/<id>.fit\|gpx` | séances (copie locale) et exports |
| GET | `/api/routes`, `/api/library` | parcours du compteur ; bibliothèque unifiée |
| GET | `/api/komoot/tours`, `/api/services` | liste Komoot ; configuration publique (sans secret) |
| GET | `/api/map/status` | état de la carte installée, cartes locales |
| GET | `/api/journal`, `/api/data-dir`, `/api/ping`, `/api/focus` | journal (version, chemins), dossier de données, signe de vie, premier plan |
| GET | `/api/profile/backups` | sauvegardes de profil disponibles |
| GET | `/api/update` | état enregistré de la vérification des mises à jour (sans réseau) |
| POST | `/api/connect`, `/api/sync` | connexion + lecture (tâche), lecture seule |
| POST | `/api/set-time` | **écrit** l'heure |
| POST | `/api/profile/edit`, `/api/profile/restore` | **écrit** le profil (`PHYSDATA.BPB`, `USERID.BPB`) après copie dans `sauvegarde-profil/` ; remet une sauvegarde |
| POST | `/api/preview`, `/api/put-route`, `/api/update-route`, `/api/remove-route` | aperçu ; **écrit / met à jour / supprime** un parcours |
| POST | `/api/library/komoot-refresh`, `/api/library/komoot-gpx`, `/api/library/detail` | liste Komoot, GPX, détail d'un parcours |
| POST | `/api/komoot/login`, `/logout`, `/fetch` | compte Komoot |
| POST | `/api/intervals/config`, `/api/disconnect`, `/api/upload`, `/api/auto` | Intervals.icu ; envoi d'une séance (`target` = `intervals` ou `komoot`) ; envoi automatique |
| POST | `/api/map/plan`, `/coverage`, `/build`, `/cancel`, `/install-tools`, `/write`, `/delete` | cartes (plan, couverture, fabrication, annulation, outils, **écriture**, suppression locale) |
| POST | `/api/zip`, `/api/theme`, `/api/lang`, `/api/units`, `/api/units/seen`, `/api/pick-folder`, `/api/open-folder`, `/api/open-external` | exports par lot, thème, langue, unités (choix et préférence vue sur le compteur), dossier, ouvertures fixes |
| POST | `/api/update/check`, `/api/update/enable` | vérification des mises à jour (automatique si due, ou forcée) ; activation / désactivation |
| POST | `/api/journal/report`, `/api/journal/open`, `/api/clientlog` | rapport de bug, dossier du journal, erreurs JavaScript de la page |
| POST | `/api/quit`, `/api/closing` | arrêt gracieux |

Ligne de commande (`python -m v650sync …`) : `info`, `ls`, `backup`, `export`, `routes`, `route-from-gpx`, `put-route`, `remove-route`, `set-time` (les commandes qui écrivent exigent `--yes`).

## 9. Journal, tests et construction

- **Journal** (`journal.py`, stdlib `logging`, rotation 4 × 1 Mo) : en-tête (versions, système, dossier de données, WebView2), appels de la page, tâches, sessions du compteur, chaque écriture PFTP (chemin, taille, durée, refus), Komoot, Intervals.icu (statut HTTP, serveur, extrait de réponse), téléchargements, Osmosis, état des réglages au démarrage (« Intervals.icu enregistré / Komoot connecté », sans valeur), exceptions. Niveau détaillé avec `V650SYNC_DEBUG=1`. **Masquage** : adresses e-mail, `token/password/authorization/api key = valeur`, `Basic/Bearer …`, dossier personnel → `~` ; jamais de coordonnées ni de contenu de fichiers du compteur. **Rapport de bug** : zip local (`rapports/`) avec versions, journaux et les trois derniers `osmosis.log` ; rien n'est envoyé ; il peut contenir des noms de parcours et des chemins.
- **Tests** : `python -m unittest discover -s tests` (77 tests : décodeurs, FIT, parcours, faux compteur PFTP, faux Komoot (lecture et envoi), Intervals et GitHub, découpage de zone, courbes de niveau, écriture de carte avec faux compteur, journal et masquage, serveur réel, réglages, cohérence du dictionnaire de traduction).
- **Construction** : `pip install -r requirements-dev.txt` puis `pyinstaller v650sync.spec` → `dist/V650Sync.exe`.
- **Variables d'environnement** : `V650SYNC_DATA` (dossier de données), `V650SYNC_DEBUG=1` (journal détaillé), `V650SYNC_KOMOOT_URL` (tests : remplace l'API Komoot par un serveur local).

## 10. Mesures et validations

- **FIT exporté vs FIT de Polar Flow** : totaux de séance identiques ; horodatages, positions, distance, vitesse, altitude, cadence 100 % ; FC 99,5 %. Règles établies : horodatage de l'enregistrement *i* = `floor((i + 1) × intervalle) + pauses` ; index de position = `round(D / intervalle) − 1` ; altitude = somme de la calibration. GPX identique à celui de Polar Flow.
- **Connexion** : première connexion 15 s → 3,3 s grâce aux essais parallèles ; lecture complète ~36 s, puis ~18 s en sautant les séances déjà copiées.
- **Parcours** : tracé écrit par l'application superposé à celui de FlowSync sur l'écran du compteur (photos à l'échelle de 200 m), avec plus de points dans les virages ; la projection initiale (111 195 m/° × cos) décalait de ~20 m en moyenne, corrigée par la mesure.
- **Cartes** : cartes de 7 à 107 Mo écrites et affichées par le compteur ; écriture ~2,2 Mo/s, relecture complète, empreinte identique ; fabrication d'une bande de 15 km autour d'un parcours de 215 km : 21 Mo en 4 min.
- **Exe** : lancement, absence de console, fenêtre native, croix → arrêt propre, deuxième lancement → fenêtre existante.

## 11. Incidents rencontrés

| Problème | Cause | Correctif |
|---|---|---|
| Aucune bannière / compteur muet | enregistrements mDNS périmés ; session précédente non fermée | essais sur tous les services ; fermeture propre |
| Gros fichiers tronqués | une seule trame vide envoyée | deux trames vides |
| `PROUTE.BPB` corrompu (+67 octets) | un seul des deux acquittements lu | lecture des deux trames vides |
| FIT décalé | règles d'horodatage et de position | règles établies par comparaison (§10) |
| Compteur muet après arrêt du serveur | serveur tué en pleine lecture | arrêt gracieux |
| Fenêtre de suivi jamais affichée | erreur (`import time` local) dans le serveur, masquée | correction + test avec un vrai serveur |
| Carte remplissant tout le rectangle | « complete ways/relations » | découpage strict par défaut |
| Intervals.icu : HTTP 403 | blocage Cloudflare du User-Agent par défaut de Python | User-Agent personnalisé |
| Clé Intervals.icu perdue au relancement | plusieurs copies de `config.json` lues puis réécrites ; un test échoué effaçait la clé | objet de réglages unique et relu, clé conservée si un nouvel essai échoue, état affiché dans l'onglet |
| Heure illisible en thème clair | texte vert fluo sur fond clair | couleur adaptée au thème |
| Routes absentes aux petits zooms (comparé à Polar) | niveaux d'apparition par défaut d'Osmosis, plus tardifs que ceux de Polar | niveaux relevés dans la carte de Polar et appliqués (`zoom_polar.json`) |
| Quadrillage de lignes droites sur les cartes avec courbes | une courbe fermée est prise pour une surface | balise `force-polygon-line` sur les courbes |
| Courbes absentes par blocs rectangulaires | le filtre `--bounding-polygon` d'Osmosis supprimait des lignes aux identifiants très élevés | filtre retiré sur le flux des courbes (le calcul est déjà limité à la zone) |
| Images de comparaison : bouts de lignes manquants | le lecteur de cartes ne dessinait que le premier tronçon d'une ligne coupée par les tuiles | tous les tronçons sont dessinés (aucun défaut dans les fichiers de carte) |

## 12. Limites connues et pistes

**Limites** : un seul compteur (pas de multi-appareils) ; décalage horaire en heures entières ; parcours ≤ ~15 Ko (une trame) ; carré blanc autour des cartes ; zone de bordure sans remplissage ; Windows 10 non testé ; interface anglaise = traduction à l'exécution (quelques messages restent en français : boîtes de dialogue natives de Windows, première fenêtre de choix du dossier, journal) ; exe non signé (SmartScreen) ; l'interface interne de Komoot peut changer.

**Pistes** : valider les courbes de niveau sur le compteur (puis les activer par défaut), noms de villes plus nombreux sur l'éditeur, signature de code et installeur, FIT complet (RR, température), multi-appareils, relecture de l'anglais par un anglophone, autres langues.

**Ne sera pas fait** : modification du firmware ; API Strava ; analyse d'entraînement.
