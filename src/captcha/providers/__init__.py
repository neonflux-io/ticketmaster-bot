"""Concrete captcha solver providers.

Each module under this package ships a real :class:`~src.captcha.base.CaptchaSolver`
subclass. Providers register themselves into the captcha registry from
:mod:`src.captcha.__init__` via ``_register_default_solvers`` so importing
the top-level package wires them in.
"""

from __future__ import annotations

from .openai_vlm import OpenAIVLMSolver

__all__ = ["OpenAIVLMSolver"]
