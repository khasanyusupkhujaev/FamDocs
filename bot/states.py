from aiogram.fsm.state import State, StatesGroup


class VaultStates(StatesGroup):
    """Tracks which folder (category) the next upload should use."""
    main = State()
    waiting_invite_link = State()
