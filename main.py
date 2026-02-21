"""
Bluff Royal — Main Application
Point d'entrée FastAPI : WebSocket temps réel + stockage en mémoire (MVP).
"""

import logging
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from connection_manager import ConnectionManager
from game_engine import GameEngine
from models import Card, Claim, GameState, Player

BASE_DIR = Path(__file__).resolve().parent

# ──────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bluff_royal")

app = FastAPI(title="Bluff Royal", version="0.1.0")

# Gestionnaire de connexions WebSocket
manager = ConnectionManager()

# Stockage en mémoire des parties actives (MVP — sera remplacé par une BDD / Redis)
active_games: dict[UUID, GameState] = {}

# Crée une partie par défaut pour le client de test (même UUID que dans index.html)
_DEFAULT_GAME_ID = UUID("00000000-0000-4000-8000-000000000001")
_default_game = GameState(game_id=_DEFAULT_GAME_ID)
active_games[_DEFAULT_GAME_ID] = _default_game


async def _broadcast_callback(game_id: UUID) -> None:
    """Callback utilisé par le GameEngine pour re-broadcaster l'état après un timer."""
    game = active_games.get(game_id)
    if game is not None:
        await manager.broadcast_game_state(game_id, game)


# Moteur de jeu
engine = GameEngine(active_games, on_state_changed=_broadcast_callback)


# ──────────────────────────────────────────────
#  Routes utilitaires (REST)
# ──────────────────────────────────────────────

@app.get("/")
async def root():
    """Sert le client de test HTML."""
    return FileResponse(BASE_DIR / "index.html")

@app.post("/games", status_code=201)
async def create_game() -> dict:
    """Crée une nouvelle partie et renvoie son game_id."""
    game = GameState()
    active_games[game.game_id] = game
    logger.info("Partie créée : %s", game.game_id)
    return {"game_id": str(game.game_id)}


@app.get("/games/{game_id}")
async def get_game(game_id: UUID) -> dict:
    """Renvoie l'état courant d'une partie (debug / spectateur)."""
    game = active_games.get(game_id)
    if game is None:
        return {"error": "Partie introuvable"}
    return game.model_dump(mode="json")


# ──────────────────────────────────────────────
#  WebSocket — Boucle principale
# ──────────────────────────────────────────────

@app.websocket("/ws/{game_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: UUID, player_id: UUID) -> None:
    """Point d'entrée WebSocket par joueur et par partie."""

    # Vérifie que la partie existe
    game = active_games.get(game_id)
    if game is None:
        await websocket.close(code=4004, reason="Partie introuvable")
        return

    # Enregistre la connexion
    await manager.connect(websocket, game_id, player_id)
    logger.info("Joueur %s connecté à la partie %s", player_id, game_id)

    # Si le joueur n'est pas encore dans la liste, on l'ajoute au GameState
    if not any(p.id == player_id for p in game.players):
        new_player = Player(id=player_id, pseudo=f"Joueur-{str(player_id)[:6]}")
        game.players.append(new_player)

    # Diffuse l'état mis à jour à tous les joueurs
    await manager.broadcast_game_state(game_id, game)

    try:
        while True:
            # Réception d'un message JSON (action du joueur)
            data: dict = await websocket.receive_json()
            action = data.get("action")
            logger.info("Action reçue de %s : %s", player_id, action)

            # ── Dispatch des actions vers le GameEngine ──
            try:
                match action:
                    case "start_game":
                        engine.start_game(game_id)

                    case "play_cards":
                        cards = [Card(**c) for c in data.get("cards", [])]
                        claim = Claim(**data.get("claim", {}))
                        engine.play_cards(game_id, player_id, cards, claim)

                    case "call_bluff":
                        engine.call_bluff(game_id, caller_id=player_id)

                    case "pass":
                        engine.pass_turn(game_id, player_id)

                    case _:
                        await manager.send_personal_message(
                            {"type": "error", "message": f"Action inconnue : {action}"},
                            game_id,
                            player_id,
                        )
                        continue

            except (ValueError, KeyError) as exc:
                await manager.send_personal_message(
                    {"type": "error", "message": str(exc)},
                    game_id,
                    player_id,
                )
                continue

            # Après chaque action traitée, on broadcast le nouvel état
            await manager.broadcast_game_state(game_id, game)

    except WebSocketDisconnect:
        manager.disconnect(game_id, player_id)
        logger.info("Joueur %s déconnecté de la partie %s", player_id, game_id)
        # On broadcast pour informer les autres joueurs
        if game_id in active_games:
            await manager.broadcast_game_state(game_id, game)
