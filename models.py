"""
Bluff Royal — Data Models
Modèles Pydantic V2 pour le backend du jeu de cartes multijoueur "Bluff Royal".
Fusion du Président classique avec des mécaniques de bluff.
"""

from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
#  Énumérations
# ──────────────────────────────────────────────

class Suit(str, Enum):
    """Couleurs (enseignes) des cartes."""
    Coeur = "Coeur"
    Carreau = "Carreau"
    Trefle = "Trefle"
    Pique = "Pique"


class PlayerRole(str, Enum):
    """Rôles attribués aux joueurs en fin de manche."""
    President = "President"
    Vice_President = "Vice_President"
    Neutre = "Neutre"
    Vice_Serviteur = "Vice_Serviteur"
    Serviteur = "Serviteur"


class GamePhase(str, Enum):
    """Phases successives d'une partie."""
    WaitingForPlayers = "WaitingForPlayers"
    InGame = "InGame"
    ReactionWindow = "ReactionWindow"   # Fenêtre de 3 s pour contester ou fermer un carré
    Resolution = "Resolution"
    RoundEnd = "RoundEnd"


# ──────────────────────────────────────────────
#  Modèles de données
# ──────────────────────────────────────────────

class Card(BaseModel):
    """Représentation d'une carte à jouer."""
    value: int = Field(..., ge=3, le=15, description="Valeur de la carte (3‑15, où 15 = 2)")
    suit: Suit


class Claim(BaseModel):
    """Annonce faite par le joueur lorsqu'il pose ses cartes face cachée."""
    quantity: int = Field(..., gt=0, description="Nombre de cartes annoncées")
    value: int = Field(..., ge=3, le=15, description="Valeur annoncée (3‑15)")


class Player(BaseModel):
    """Joueur connecté à une partie."""
    id: UUID = Field(default_factory=uuid4, description="Identifiant unique du joueur")
    pseudo: str = Field(..., min_length=1, description="Pseudo affiché en jeu")
    hand: list[Card] = Field(default_factory=list, description="Main actuelle du joueur")
    role: PlayerRole = Field(default=PlayerRole.Neutre, description="Rôle du joueur")
    has_passed: bool = Field(default=False, description="Le joueur a passé son tour dans la série en cours")


class GameState(BaseModel):
    """État global de la partie — source de vérité côté serveur."""
    game_id: UUID = Field(default_factory=uuid4, description="Identifiant unique de la partie")
    players: list[Player] = Field(default_factory=list, description="Liste des joueurs")
    active_player_id: UUID | None = Field(default=None, description="ID du joueur dont c'est le tour")
    current_trick: list[Card] = Field(default_factory=list, description="Cartes posées face cachée au centre")
    current_claim: Claim | None = Field(default=None, description="Dernière annonce en cours")
    phase: GamePhase = Field(default=GamePhase.WaitingForPlayers, description="Phase actuelle de la partie")
