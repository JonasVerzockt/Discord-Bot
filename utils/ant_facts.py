# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jonas Beier
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
utils/ant_facts.py – Ameisen-Fakten für die Wartezeit vor dem Jahresrückblick.

Locker formuliert, inhaltlich aber nur das, was die jeweils verlinkte Quelle belegt.
Bitte beim Ergänzen genauso halten: jeder Spruch braucht eine seriöse Quelle.
Texte ohne Gedankenstriche (werden auch in Discord angezeigt).
"""
import random

FACTS: list[dict] = [
    {
        "de": "Auf der Erde krabbeln mindestens 20 Billiarden Ameisen herum. Das sind rund 2,5 Millionen pro Mensch. Wer zählt, braucht Geduld.",
        "en": "At least 20 quadrillion ants live on Earth. That is roughly 2.5 million per human. Counting takes patience.",
        "eo": "Sur la Tero vivas almenaŭ 20 kvarilionoj (20 × 10¹⁵) da formikoj. Tio estas ĉirkaŭ 2,5 milionoj por ĉiu homo.",
        "src": "PNAS 2022 (Schultheiss et al.), via phys.org",
        "url": "https://phys.org/news/2022-09-ants-earth-quadrillion.html",
    },
    {
        "de": "Alle Ameisen zusammen wiegen mehr als alle wild lebenden Vögel und Säugetiere zusammen: rund 12 Megatonnen Kohlenstoff, etwa ein Fünftel der Menschheit.",
        "en": "All ants together outweigh all wild birds and mammals combined: about 12 megatonnes of carbon, roughly a fifth of humanity.",
        "eo": "Ĉiuj formikoj kune pezas pli ol ĉiuj sovaĝaj birdoj kaj mamuloj kune: ĉirkaŭ 12 megatunoj da karbono, proksimume kvinono de la homaro.",
        "src": "PNAS 2022 (Schultheiss et al.), via phys.org",
        "url": "https://phys.org/news/2022-09-ants-earth-quadrillion.html",
    },
    {
        "de": "Die Schnappkieferameise Odontomachus bauri schließt ihre Kiefer mit bis zu 64 m/s. Ein Biss dauert 0,13 Millisekunden, rund 2.300 Mal schneller als ein Wimpernschlag.",
        "en": "The trap-jaw ant Odontomachus bauri snaps its jaws shut at up to 64 m/s. A strike lasts 0.13 milliseconds, about 2,300 times faster than a blink.",
        "eo": "La formiko Odontomachus bauri fermas siajn makzelojn je ĝis 64 m/s. Mordo daŭras 0,13 milisekundojn, ĉirkaŭ 2 300-oble pli rapide ol palpebrumo.",
        "src": "Patek et al., PNAS 2006, via ScienceDaily",
        "url": "https://www.sciencedaily.com/releases/2006/08/060822121727.htm",
    },
    {
        "de": "Dieselbe Schnappkieferameise kann sich mit einem Kieferschlag gegen den Boden in die Luft katapultieren. Fluchtsprung per Kiefer, bis zu 8 cm hoch.",
        "en": "The same trap-jaw ant can catapult itself into the air by snapping its jaws against the ground. An escape jump powered by its jaws, up to 8 cm high.",
        "eo": "La sama formiko povas ĵeti sin en la aeron, frapante siajn makzelojn kontraŭ la teron. Fuĝosalto per makzeloj, ĝis 8 cm alta.",
        "src": "Patek et al., PNAS 2006, via ScienceDaily",
        "url": "https://www.sciencedaily.com/releases/2006/08/060822121727.htm",
    },
    {
        "de": "Feuerameisen (Solenopsis invicta) bauen bei Hochwasser aus ihren eigenen Körpern ein wasserdichtes Floß. Fertig in etwa 100 Sekunden, schwimmfähig über Tage.",
        "en": "During floods, fire ants (Solenopsis invicta) build a waterproof raft out of their own bodies. Done in about 100 seconds, afloat for days.",
        "eo": "Dum inundoj fajroformikoj (Solenopsis invicta) konstruas akvorezistan floson el siaj propraj korpoj. Preta en ĉirkaŭ 100 sekundoj, flosanta dum tagoj.",
        "src": "Mlot, Tovey & Hu, PNAS 2011",
        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC3093451/",
    },
    {
        "de": "Ameisen gibt es seit 140 bis 168 Millionen Jahren. Sie haben also schon Dinosauriern über die Füße krabbeln können.",
        "en": "Ants have been around for 140 to 168 million years. So they could already crawl over dinosaur feet.",
        "eo": "Formikoj ekzistas jam de 140 ĝis 168 milionoj da jaroj. Do ili jam povis rampi sur piedoj de dinosaŭroj.",
        "src": "Moreau et al., Science 2006, via Harvard Gazette",
        "url": "https://news.harvard.edu/gazette/story/2006/04/ants-are-surprisingly-ancient-arising-140-168-million-years-ago-2/",
    },
    {
        "de": "Ameisen betreiben Landwirtschaft seit rund 50 Millionen Jahren. Blattschneiderameisen essen die Blätter gar nicht selbst, sie füttern damit ihren Pilzgarten und essen den Pilz.",
        "en": "Ants have been farming for about 50 million years. Leafcutter ants do not eat the leaves themselves; they feed them to their fungus garden and eat the fungus.",
        "eo": "Formikoj kultivas jam de ĉirkaŭ 50 milionoj da jaroj. Folitranĉaj formikoj ne manĝas la foliojn mem; ili nutras per ili sian fungoĝardenon kaj manĝas la fungon.",
        "src": "American Society for Microbiology, 2017",
        "url": "https://asm.org/articles/2017/september/the-leaf-cutter-ant-s-50-million-years-of-farming",
    },
    {
        "de": "Wüstenameisen (Cataglyphis) zählen ihre Schritte, um heimzufinden. Mit angeklebten Stelzen liefen sie im Versuch rund 50 % zu weit.",
        "en": "Desert ants (Cataglyphis) count their steps to find their way home. Fitted with glued-on stilts, they overshot by about 50 % in the experiment.",
        "eo": "Dezertaj formikoj (Cataglyphis) kalkulas siajn paŝojn por trovi la vojon hejmen. Kun algluitaj stiltoj ili en la eksperimento iris ĉirkaŭ 50 % tro malproksimen.",
        "src": "Wittlinger, Wehner & Wolf, J Exp Biol 2007",
        "url": "https://www.imls.uzh.ch/static/CMS_publications/wehner/literatur/pdf07/92.pdf",
    },
    {
        "de": "Honigtopfameisen (Myrmecocystus) haben lebende Vorratsfässer: Arbeiterinnen, deren Hinterleib sich bis zur Größe kleiner Weintrauben füllt und die an der Nestdecke hängen.",
        "en": "Honeypot ants (Myrmecocystus) keep living storage barrels: workers whose abdomen swells to the size of small grapes, hanging from the nest ceiling.",
        "eo": "Mielpotaj formikoj (Myrmecocystus) havas vivantajn provizbarelojn: laboristinojn, kies abdomeno ŝvelas ĝis la grandeco de malgrandaj vinberoj kaj kiuj pendas de la nesta plafono.",
        "src": "Navajo Nation Zoological & Botanical Park, Navajo Nature",
        "url": "https://navajonature.org/ants/formicinae/myrmecocystus-mexicanus.html",
    },
    {
        "de": "Weberameisen (Oecophylla) nähen Blätter zu Nestern zusammen. Als Nähmaschine dienen ihre eigenen Larven, die Seide spinnen.",
        "en": "Weaver ants (Oecophylla) sew leaves together into nests. Their sewing machines are their own larvae, which spin silk.",
        "eo": "Teksformikoj (Oecophylla) kudras foliojn kune al nestoj. Kiel kudromaŝinoj servas iliaj propraj larvoj, kiuj ŝpinas silkon.",
        "src": "Hölldobler & Wilson, The Ants (1990), via AntWiki",
        "url": "https://antwiki.org/wiki/The_Ants_Chapter_19",
    },
    {
        "de": "Eine Königin von Lasius niger wurde beobachtet, wie sie bei der Koloniegründung 382 Tage ohne Futter auskam. Neujahrsvorsatz: Geduld wie eine Gründerin.",
        "en": "A Lasius niger queen was observed going 382 days without food while founding her colony. New Year's resolution: patience like a founding queen.",
        "eo": "Reĝino de Lasius niger estis observita, kiel ŝi travivis 382 tagojn sen manĝaĵo dum la fondado de kolonio. Novjara decido: pacienco kiel fondanta reĝino.",
        "src": "Eidmann 1926, via AntWiki",
        "url": "https://antwiki.org/wiki/Lasius_niger",
    },
]


def pick(lang: str, index: int | None = None) -> dict:
    """Ein Fakt in der gewünschten Sprache: {text, src, url, index}."""
    i = random.randrange(len(FACTS)) if index is None else index % len(FACTS)
    f = FACTS[i]
    return {"text": f.get(lang) or f["de"], "src": f["src"], "url": f["url"], "index": i}


def all_for(lang: str) -> list[dict]:
    return [pick(lang, i) for i in range(len(FACTS))]
