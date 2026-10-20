"""Players of the simulated Counter-Strike servers (GoldSrc and Source), and what the
servers' player commands do to them: ``kick``/``kickid`` remove a player, ``addip`` adds an
address filter, ``writeip`` saves the filters."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SimulatedPlayer:
    userid: int
    name: str
    uniqueid: str = "STEAM_0:1:2345"
    frags: int = 0
    ping: int = 25
    address: str = "10.0.0.5:27005"

    @property
    def bot(self) -> bool:
        return self.uniqueid == "BOT"


def default_roster(count: int) -> list[SimulatedPlayer]:
    """One player "Ada", one bot, then "Player 3", "Player 4", ..."""
    roster = []
    for number in range(count):
        if number == 0:
            roster.append(SimulatedPlayer(2, "Ada", frags=7))
        elif number == 1:
            roster.append(SimulatedPlayer(3, "Bot01", "BOT", frags=2, ping=0, address=""))
        else:
            roster.append(
                SimulatedPlayer(
                    number + 2,
                    f"Player {number + 1}",
                    f"STEAM_0:0:{number}",
                    address=f"10.0.0.{number + 5}:27005",
                )
            )
    return roster


@dataclass
class PlayerCommands:
    """The state the player commands change."""

    roster: list[SimulatedPlayer]
    #: Kicks as (user id, message).
    kicks: list[tuple[int, str]] = field(default_factory=list)
    #: Addresses added with ``addip``, and how often ``writeip`` saved them.
    banned: list[str] = field(default_factory=list)
    saved: int = 0

    def kick(self, argument: str) -> bool:
        """``argument``: the user id and an optional message. False if there is no such
        player."""
        user_id, _, message = argument.partition(" ")
        player = next((p for p in self.roster if str(p.userid) == user_id), None)
        if player is None:
            return False
        self.roster.remove(player)
        self.kicks.append((player.userid, message.strip()))
        return True

    def add_ip(self, argument: str) -> None:
        _minutes, _, address = argument.partition(" ")
        self.banned.append(address.strip())
