"""
Bluff Royal — Game Engine
Logique métier du jeu : validation des coups, résolution des bluffs,
et timer asynchrone pour la fenêtre de réaction.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Callable, Coroutine
from uuid import UUID

from models import Card, Claim, GamePhase, GameState, Suit

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger("bluff_royal.engine")


class GameEngine:
    """Moteur de jeu Bluff Royal — gère les règles, les timers et les transitions de phase."""

    def __init__(
        self,
        active_games: dict[UUID, GameState],
        on_state_changed: Callable[[UUID], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        # Référence partagée vers le stockage des parties en mémoire
        self.active_games = active_games
        # Timers actifs par partie (asyncio.Task de la fenêtre de réaction)
        self.active_timers: dict[UUID, asyncio.Task[None]] = {}
        # Callback optionnel appelé quand le GameState est modifié par le timer
        # (permet au main.py de déclencher un broadcast)
        self._on_state_changed = on_state_changed

    # ──────────────────────────────────────────
    #  Helpers internes
    # ──────────────────────────────────────────

    def _get_game(self, game_id: UUID) -> GameState:
        """Récupère la partie ou lève une erreur."""
        game = self.active_games.get(game_id)
        if game is None:
            raise ValueError(f"Partie introuvable : {game_id}")
        return game

    def _get_player_index(self, game: GameState, player_id: UUID) -> int:
        """Renvoie l'index du joueur dans la liste, ou lève une erreur."""
        for idx, p in enumerate(game.players):
            if p.id == player_id:
                return idx
        raise ValueError(f"Joueur introuvable : {player_id}")

    def _next_active_player(self, game: GameState, after_index: int) -> UUID | None:
        """Renvoie l'ID du prochain joueur qui n'a pas passé, en bouclant sur la liste."""
        n = len(game.players)
        if n == 0:
            return None
        for offset in range(1, n + 1):
            candidate = game.players[(after_index + offset) % n]
            if not candidate.has_passed:
                return candidate.id
        # Tous les joueurs ont passé → fin du pli
        return None

    @staticmethod
    def _build_deck() -> list[Card]:
        """Construit un jeu de 52 cartes (valeurs 3→15, 4 couleurs) et le mélange."""
        deck = [Card(value=v, suit=s) for v in range(3, 16) for s in Suit]
        random.shuffle(deck)
        return deck

    # ──────────────────────────────────────────
    #  Lancement de la partie
    # ──────────────────────────────────────────

    def start_game(self, game_id: UUID) -> GameState:
        """
        Lance la partie : mélange le paquet, distribue les cartes
        équitablement, et passe en phase InGame.
        """
        game = self._get_game(game_id)

        if game.phase != GamePhase.WaitingForPlayers:
            raise ValueError("La partie a déjà commencé")

        n = len(game.players)
        if n < 2:
            raise ValueError("Il faut au moins 2 joueurs pour lancer la partie")

        deck = self._build_deck()

        # Distribution équitable (le reste n'est pas distribué)
        cards_per_player = len(deck) // n
        for i, player in enumerate(game.players):
            player.hand = deck[i * cards_per_player : (i + 1) * cards_per_player]
            player.has_passed = False

        # Le premier joueur commence
        game.active_player_id = game.players[0].id
        game.phase = GamePhase.InGame
        game.current_trick.clear()
        game.current_claim = None

        logger.info(
            "Partie %s lancée — %d joueurs, %d cartes chacun",
            game_id, n, cards_per_player,
        )

        return game

    # ──────────────────────────────────────────
    #  Actions de jeu
    # ──────────────────────────────────────────

    def play_cards(
        self,
        game_id: UUID,
        player_id: UUID,
        cards: list[Card],
        claim: Claim,
    ) -> GameState:
        """
        Le joueur pose des cartes face cachée et fait une annonce (claim).
        Retourne le GameState mis à jour.
        """
        game = self._get_game(game_id)

        # ── Validations ──
        if game.phase != GamePhase.InGame:
            raise ValueError(f"Action interdite pendant la phase : {game.phase}")

        if game.active_player_id != player_id:
            raise ValueError("Ce n'est pas le tour de ce joueur")

        player_idx = self._get_player_index(game, player_id)
        player = game.players[player_idx]

        # Vérifie que le joueur possède bien les cartes qu'il pose
        hand_copy = list(player.hand)
        for card in cards:
            try:
                hand_copy.remove(card)
            except ValueError:
                raise ValueError(
                    f"Le joueur ne possède pas la carte : {card.value} de {card.suit.value}"
                )

        # ── Application ──

        # Retire les cartes de la main
        player.hand = hand_copy

        # Mémorise le nombre de cartes posées dans ce coup pour call_bluff
        self._last_play_count: int = len(cards)
        self._last_player_id: UUID = player_id

        # Ajoute au centre de la table (face cachée)
        game.current_trick.extend(cards)

        # Enregistre l'annonce
        game.current_claim = claim

        # Transition vers la fenêtre de réaction
        game.phase = GamePhase.ReactionWindow

        # Lance le timer asynchrone de 3 secondes
        self.active_timers[game_id] = asyncio.create_task(
            self._reaction_timer(game_id)
        )

        logger.info(
            "Joueur %s pose %d carte(s), annonce %d × %d",
            player_id, len(cards), claim.quantity, claim.value,
        )

        return game

    def call_bluff(self, game_id: UUID, caller_id: UUID) -> GameState:
        """
        Un joueur conteste l'annonce (crie au mensonge).
        Compare les cartes réellement posées avec l'annonce.
        Retourne le GameState mis à jour.
        """
        game = self._get_game(game_id)

        # ── Validations ──
        if game.phase != GamePhase.ReactionWindow:
            raise ValueError("Le bluff ne peut être contesté que pendant la fenêtre de réaction")

        if caller_id == getattr(self, "_last_player_id", None):
            raise ValueError("Un joueur ne peut pas contester son propre coup")

        # Annule le timer en cours
        timer = self.active_timers.pop(game_id, None)
        if timer is not None:
            timer.cancel()

        claim = game.current_claim
        if claim is None:
            raise ValueError("Aucune annonce en cours à contester")

        # ── Résolution du bluff ──

        # Les dernières cartes posées sont les N dernières du current_trick
        play_count = getattr(self, "_last_play_count", 0)
        played_cards = game.current_trick[-play_count:] if play_count > 0 else []

        # Vérifie si l'annonce correspond aux cartes réellement posées
        is_bluff = (
            len(played_cards) != claim.quantity
            or any(c.value != claim.value for c in played_cards)
        )

        liar_id: UUID = getattr(self, "_last_player_id", caller_id)
        liar_idx = self._get_player_index(game, liar_id)
        caller_idx = self._get_player_index(game, caller_id)

        trick_cards = list(game.current_trick)

        if is_bluff:
            # MENSONGE détecté → le poseur ramasse tout le pli
            game.players[liar_idx].hand.extend(trick_cards)
            # Le contestataire prend la main
            game.active_player_id = caller_id
            logger.info(
                "BLUFF ! Joueur %s ment. Joueur %s (contestataire) prend la main.", liar_id, caller_id
            )
        else:
            # VÉRITÉ → le contestataire ramasse tout le pli
            game.players[caller_idx].hand.extend(trick_cards)
            # Le poseur garde la main
            game.active_player_id = liar_id
            logger.info(
                "Pas de bluff. Joueur %s (contestataire) ramasse le pli.", caller_id
            )

        # Nettoyage
        game.current_trick.clear()
        game.current_claim = None
        game.phase = GamePhase.InGame

        return game

    def pass_turn(self, game_id: UUID, player_id: UUID) -> GameState:
        """Le joueur passe son tour dans la série en cours."""
        game = self._get_game(game_id)

        if game.phase != GamePhase.InGame:
            raise ValueError(f"Action interdite pendant la phase : {game.phase}")

        if game.active_player_id != player_id:
            raise ValueError("Ce n'est pas le tour de ce joueur")

        player_idx = self._get_player_index(game, player_id)
        game.players[player_idx].has_passed = True

        # Passe au joueur suivant
        next_id = self._next_active_player(game, player_idx)

        if next_id is None:
            # Tous les joueurs ont passé → fin du pli
            game.current_trick.clear()
            game.current_claim = None
            # Réinitialise les passes pour le prochain pli
            for p in game.players:
                p.has_passed = False
            # Le dernier joueur actif garde la main
            game.active_player_id = player_id
            logger.info("Tous les joueurs ont passé — nouveau pli")
        else:
            game.active_player_id = next_id
            logger.info("Joueur %s passe, tour → %s", player_id, next_id)

        return game

    # ──────────────────────────────────────────
    #  Timer asynchrone
    # ──────────────────────────────────────────

    async def _reaction_timer(self, game_id: UUID) -> None:
        """
        Fenêtre de réaction de 3 secondes.
        Si personne ne conteste avant la fin, le coup est validé
        et le tour passe au joueur suivant.
        """
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            # Le timer a été annulé (call_bluff), on ne fait rien ici
            logger.debug("Timer annulé pour la partie %s (bluff contesté)", game_id)
            return

        # ── Personne n'a réagi : le coup est validé ──
        game = self.active_games.get(game_id)
        if game is None:
            return

        liar_id = getattr(self, "_last_player_id", None)
        if liar_id is not None:
            liar_idx = self._get_player_index(game, liar_id)
            next_id = self._next_active_player(game, liar_idx)
            game.active_player_id = next_id
        else:
            game.active_player_id = None

        game.phase = GamePhase.InGame

        # Nettoyage du timer
        self.active_timers.pop(game_id, None)

        logger.info("Timer expiré pour la partie %s — coup validé, tour suivant", game_id)

        # Notifie le main.py pour re-broadcaster l'état
        if self._on_state_changed is not None:
            await self._on_state_changed(game_id)
