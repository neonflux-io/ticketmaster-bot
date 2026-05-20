"""Captcha solver subsystem.

Exposes the shared abstractions (:class:`CaptchaChallenge`,
:class:`CaptchaSolution`, :class:`CaptchaSolver`) and the chain that
orchestrates ordered, retry-budgeted provider attempts
(:class:`~src.captcha.chain.CaptchaSolverChain`). The
:mod:`src.captcha.registry` module is the singleton registry concrete
solvers register themselves into.

Importing this package also wires every shipped concrete solver class
into :mod:`src.captcha.registry` under its public name (e.g.
``"openai_vlm"`` → :class:`~src.captcha.providers.openai_vlm.OpenAIVLMSolver`),
mirroring the pattern used by :mod:`src.notifiers`.
"""

from __future__ import annotations

from . import registry as _captcha_registry
from .base import (
    CaptchaChallenge,
    CaptchaSolution,
    CaptchaSolver,
    ChallengeType,
)
from .chain import CaptchaSolverChain
from .providers.openai_vlm import OpenAIVLMSolver


def _register_default_solvers() -> None:
    """Register every shipped captcha solver class under its public name.

    Re-imports are tolerated by skipping any name already present in the
    registry (the registry itself raises ``DuplicateRegistration`` on
    duplicate keys).
    """
    defaults: dict[str, type[CaptchaSolver]] = {
        "openai_vlm": OpenAIVLMSolver,
    }
    for name, cls in defaults.items():
        if name in _captcha_registry.registry:
            continue
        _captcha_registry.register(name, cls)


_register_default_solvers()


__all__ = [
    "CaptchaChallenge",
    "CaptchaSolution",
    "CaptchaSolver",
    "CaptchaSolverChain",
    "ChallengeType",
    "OpenAIVLMSolver",
]
