🇬🇧 [English version](THIRD_PARTY.md)

# Composants tiers

Ce projet est **indépendant** : il n'est ni affilié à Polar, Komoot, Strava ni Intervals.icu, ni approuvé par eux.

## Inclus dans l'exécutable ou utilisés par le code

| Composant | Usage | Licence |
|---|---|---|
| [kompy](https://github.com/Tsadoq/kompy) (Matteo Villosio) | La logique d'accès à Komoot (adresses de l'API interne, authentification, pagination, export GPX) de `v650sync/komoot.py` en est reprise, réécrite sans dépendance | MIT, Copyright (c) 2023-2026 Matteo Villosio |
| [zeroconf](https://github.com/python-zeroconf/python-zeroconf) | Découverte du compteur (mDNS) | LGPL-2.1 ou ultérieure |
| [pywebview](https://github.com/r0x0r/pywebview) | Fenêtre native (WebView2) | BSD-3-Clause |
| [pythonnet](https://github.com/pythonnet/pythonnet) | Pont .NET utilisé par pywebview | MIT |
| [PyInstaller](https://pyinstaller.org) | Construction de l'exécutable | GPL-2.0 ou ultérieure, avec exception pour les programmes construits |

## Téléchargés à la demande de l'utilisateur (non inclus dans ce dépôt)

Pour fabriquer des cartes, le programme propose de télécharger, avec vérification d'empreinte, depuis leurs sites officiels : Java (Eclipse Temurin), Osmosis et son extension *mapsforge-map-writer*. Ils restent sous leurs licences propres. Les extraits de données OpenStreetMap viennent de Geofabrik.

## Données

- Courbes de niveau : altitudes SRTM (NASA / USGS, domaine public ; l'USGS demande qu'on cite la source), téléchargées à la demande depuis le jeu public « Terrain Tiles » d'AWS (`elevation-tiles-prod`, tuiles `.hgt.gz` d'1°). Les courbes sont calculées par l'application (`v650sync/contours.py`) ; aucune de ces données n'est incluse dans ce dépôt.
- Cartes et données OpenStreetMap : © contributeurs OpenStreetMap, licence ODbL. Les tuiles de fond des aperçus viennent des serveurs OpenStreetMap : respectez leur [politique d'usage](https://operations.osmfoundation.org/policies/tiles/).
- `v650sync/mapdata/tags_polar.json` : liste de correspondance des étiquettes OpenStreetMap, établie d'après l'observation d'une carte du compteur.

L'API interne de Komoot est non documentée : elle peut changer ou être restreinte sans préavis. N'utilisez que votre propre compte.

## Licence de kompy (MIT)

La logique d'accès à Komoot de `v650sync/komoot.py` est reprise de [kompy](https://github.com/Tsadoq/kompy). Son texte de licence, reproduit comme l'exige la licence MIT (texte original en anglais) :

```text
Copyright (c) 2023-2026 Matteo Villosio

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
