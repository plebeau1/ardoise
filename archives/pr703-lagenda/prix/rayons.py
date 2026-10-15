"""Rayons suivis — l'équivalent du registre des salles côté concerts.

Ajouter un rayon = ajouter une ligne. Le `slug` sert à comparer un relevé au
précédent (disparitions, relevé suspect) : ne jamais le renommer une fois le
rayon relevé, sinon le rayon repart d'un historique vide.
"""

RAYONS = [
    {
        "retailer": "carrefour",
        "slug": "legumes-de-saison",
        "label": "Légumes de saison",
        "url": "https://www.carrefour.fr/r/fruits-et-legumes/legumes/legumes-de-saison",
    },
]
