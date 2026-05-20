"""Captcha solver subsystem.

Exposes the shared abstractions (:class:`CaptchaChallenge`,
:class:`CaptchaSolution`, :class:`CaptchaSolver`) and the chain that
orchestrates ordered, retry-budgeted provider attempts
(:class:`~src.captcha.chain.CaptchaSolverChain`). The
:mod:`src.captcha.registry` module is the singleton registry concrete
solvers register themselves into.
"""

from __future__ import annotations

from .base import (
    CaptchaChallenge,
    CaptchaSolution,
    CaptchaSolver,
    ChallengeType,
)
from .chain import CaptchaSolverChain

__all__ = [
    "CaptchaChallenge",
    "CaptchaSolution",
    "CaptchaSolver",
    "CaptchaSolverChain",
    "ChallengeType",
]
