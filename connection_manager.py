"""
Bluff Royal — Connection Manager
Gestionnaire de connexions WebSocket pour les parties multijoueurs en temps réel.
"""

from uuid import UUID

from fastapi import WebSocket

from models import GameState


class ConnectionManager:
    """Gère les connexions WebSocket actives, organisées par partie et par joueur."""

    def __init__(self) -> None:
        # game_id -> {player_id -> WebSocket}
        self.active_connections: dict[UUID, dict[UUID, WebSocket]] = {}

    async def connect(self, websocket: WebSocket, game_id: UUID, player_id: UUID) -> None:
        """Accepte une connexion WebSocket et l'enregistre pour la partie donnée."""
        await websocket.accept()
        if game_id not in self.active_connections:
            self.active_connections[game_id] = {}
        self.active_connections[game_id][player_id] = websocket

    def disconnect(self, game_id: UUID, player_id: UUID) -> None:
        """Retire la connexion WebSocket sans supprimer le joueur du GameState (reconnexion possible)."""
        if game_id in self.active_connections:
            self.active_connections[game_id].pop(player_id, None)
            # Nettoyage : supprime l'entrée de la partie si plus aucun joueur connecté
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]

    async def broadcast_game_state(self, game_id: UUID, game_state: GameState) -> None:
        """Sérialise le GameState et l'envoie à tous les joueurs connectés à la partie."""
        if game_id not in self.active_connections:
            return

        # Sérialisation Pydantic V2 → dict JSON-compatible (gère UUID & Enum)
        payload = game_state.model_dump(mode="json")

        for player_id, websocket in self.active_connections[game_id].items():
            try:
                await websocket.send_json(payload)
            except Exception:
                # La connexion est peut-être déjà fermée ; on l'ignore ici,
                # la déconnexion sera traitée par la boucle principale.
                pass

    async def send_personal_message(
        self, message: dict, game_id: UUID, player_id: UUID
    ) -> None:
        """Envoie un message JSON privé à un joueur spécifique (ex: erreur, main secrète)."""
        websocket = self.active_connections.get(game_id, {}).get(player_id)
        if websocket is None:
            return
        try:
            await websocket.send_json(message)
        except Exception:
            pass
