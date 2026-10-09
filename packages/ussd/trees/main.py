"""The top of every session: choose a language, offer a draft, and show the main menu.

These three nodes are the only ones reachable before a trader has chosen anything, so they
are where the two starting conditions are handled — a handset we have not spoken to, which
gets the language menu, and a handset that dropped mid-flow, which is offered its draft back.
Everything below them is one of the five paths of §14.3.
"""

from __future__ import annotations

from packages.ussd.session import SessionState
from packages.ussd.trees.model import Action, Node, choices


def _after_language(text: str, session: SessionState) -> str | None:
    """A language choice leads into a resumed draft when there is one, else the main menu."""
    if text.strip() not in {"1", "2", "3"}:
        return None
    return "resume" if session.context.get("_draft_step") else "main_menu"


def _after_resume(text: str, session: SessionState) -> str | None:
    value = text.strip()
    if value == "1":
        step = session.context.get("_draft_step")
        return step if isinstance(step, str) else "main_menu"
    if value == "2":
        return "main_menu"
    return None


NODES: dict[str, Node] = {
    "language": Node(
        id="language",
        prompt=("ussd_greeting", "ussd_language_menu"),
        next=_after_language,
        action=Action("set_language"),
        allows_back=False,
    ),
    "resume": Node(
        id="resume",
        prompt=("ussd_greeting", "ussd_resume_prompt"),
        next=_after_resume,
        resumes_draft=True,
        allows_back=False,
    ),
    "main_menu": Node(
        id="main_menu",
        prompt=("ussd_main_menu",),
        next=choices(
            {
                "1": "listings_menu",
                "2": "browse_region_menu",
                "3": "orders_menu",
                "4": "prices",
                "5": "help",
            }
        ),
        allows_back=False,
    ),
    "prices": Node(
        id="prices",
        prompt=("ussd_price_board",),
        terminal=True,
        prefetch=("price_board",),
    ),
    "help": Node(
        id="help",
        prompt=("ussd_help",),
        terminal=True,
    ),
}
