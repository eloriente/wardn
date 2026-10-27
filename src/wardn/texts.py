"""The fixed texts of the Telegram bot, in English or Spanish.

The `language` of the config already makes the model answer in its language;
these are the texts the bot writes itself.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BotTexts:
    help: str
    thinking: str  # sent at once by /ask: the model takes a while on a CPU
    checking: str  # the same for /status
    usage: str
    summary: str  # title of the model's summary
    summary_unavailable: str
    no_answer: str
    empty_answer: str
    unfinished: str


ENGLISH = BotTexts(
    help=(
        "Wardn commands:\n"
        "/status - what needs attention in the homelab, and a short summary\n"
        "/ask <question> - ask the agent about the server\n"
        "/help - this message"
    ),
    thinking="Thinking...",
    checking="Checking the homelab...",
    usage="Usage: /ask <question>",
    summary="Summary:",
    summary_unavailable="The summary is unavailable: the model did not answer.",
    no_answer="The model did not answer.",
    empty_answer="The model gave no answer.",
    unfinished="I did not manage to finish: that was as far as I got.",
)

SPANISH = BotTexts(
    help=(
        "Comandos de Wardn:\n"
        "/status - qué necesita atención en el homelab, y un resumen corto\n"
        "/ask <pregunta> - pregunta al agente sobre el servidor\n"
        "/help - este mensaje"
    ),
    thinking="Pensando...",
    checking="Comprobando el homelab...",
    usage="Uso: /ask <pregunta>",
    summary="Resumen:",
    summary_unavailable="El resumen no está disponible: el modelo no respondió.",
    no_answer="El modelo no respondió.",
    empty_answer="El modelo no dio ninguna respuesta.",
    unfinished="No logré terminar: hasta aquí he llegado.",
)

SPANISH_NAMES = ("spanish", "español", "castellano")


def bot_texts(language: str | None) -> BotTexts:
    return SPANISH if (language or "").lower() in SPANISH_NAMES else ENGLISH
