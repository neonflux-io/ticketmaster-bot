"""Live OpenAI vision-model accuracy test against the 5 real Yii captcha samples.

This test is the **end-to-end integration probe** for the OpenAI VLM
captcha solver. Every other ``test_openai_vlm.py`` test runs against a
local FastAPI server returning hand-crafted JSON; this one talks to the
real ``api.openai.com`` chat-completions endpoint with the actual
configured model.

The 5 PNG samples + sibling ``.txt`` ground-truth files live under
``tests/fixtures/vendors/ticketmaster_sg/captcha_samples/`` and were
captured live by visiting
``https://ticketmaster.sg/ticket/check-captcha/26sg_pglcs2major/3239/1/21``
in a headed browser, screenshotting ``img#TicketForm_verifyCode-image``,
and recording the correct answer for each by human read (see F9.4).

The test:

1. Skips cleanly when either ``OPENAI_API_KEY`` or
   ``OPENAI_CAPTCHA_MODEL`` is unset (mission rule: never silently
   pass without doing the work).
2. Instantiates :class:`OpenAIVLMSolver` with the real env-derived
   config (no overrides, so the real OpenAI base URL is used).
3. Builds a :class:`CaptchaChallenge` from each sample's PNG bytes and
   calls ``solver.solve(challenge)``, recording the predicted vs
   ground-truth answer.
4. Prints a prediction-vs-truth table to stdout for diagnostic
   visibility (use ``pytest -s`` to see it).
5. Asserts that **at least 4 of 5** predictions match the ground truth
   (case-insensitive). That's an 80% floor - if the integration can't
   beat that on 5 simple Yii captchas the integration is broken.

NO mocks. NO ``httpx.MockTransport``. NO record / replay. Real OpenAI
API calls.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from src.captcha import CaptchaChallenge, CaptchaSolution
from src.captcha.providers.openai_vlm import OpenAIVLMSolver

#: Directory holding the committed PNG samples + sibling ``.txt`` answers.
_SAMPLES_DIR = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "vendors"
    / "ticketmaster_sg"
    / "captcha_samples"
)

#: 80% accuracy floor on 5 samples = at least 4 of 5 correct.
_MIN_CORRECT = 4
_TOTAL_SAMPLES = 5


def _collect_samples() -> list[tuple[Path, str]]:
    """Return ``[(png_path, ground_truth_upper), ...]`` for every sample.

    The list is sorted by filename so the diagnostic table is stable
    across runs.
    """
    samples: list[tuple[Path, str]] = []
    for png in sorted(_SAMPLES_DIR.glob("captcha_*.png")):
        txt = png.with_suffix(".txt")
        if not txt.is_file():
            raise FileNotFoundError(f"Missing ground-truth sibling for sample: {txt}")
        truth = txt.read_text(encoding="utf-8").strip().upper()
        if not truth:
            raise ValueError(f"Empty ground-truth file: {txt}")
        samples.append((png, truth))
    return samples


def _make_challenge(png_bytes: bytes) -> CaptchaChallenge:
    """Wrap raw PNG bytes in a :class:`CaptchaChallenge` for the solver."""
    return CaptchaChallenge(
        challenge_type="yii_image",
        screenshot_bytes=png_bytes,
        refresh_callable=None,
        input_locator=None,
        page=None,
    )


@pytest.mark.asyncio
async def test_openai_vlm_real_accuracy_on_yii_samples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real OpenAI vision-model accuracy probe: ≥4/5 of the captured Yii samples.

    Skips when ``OPENAI_API_KEY`` or ``OPENAI_CAPTCHA_MODEL`` is unset.
    The 5-sample threshold (80% floor) is intentionally low; if the
    integration can't clear it on 5 simple alphabetic Yii captchas, the
    wiring is broken (wrong endpoint, wrong model, wrong payload shape,
    wrong prompt). Print the prediction-vs-truth table to stdout so an
    operator running ``pytest -s`` can immediately see which samples
    the model misses.
    """
    # ``.env`` is gitignored but committed by the operator who runs this
    # test. Best-effort load - if the env is already exported, the
    # explicit ``override=False`` keeps the existing value.
    load_dotenv(override=False)

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_CAPTCHA_MODEL")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set - real-OpenAI accuracy probe skipped")
    if not model:
        pytest.skip("OPENAI_CAPTCHA_MODEL not set - real-OpenAI accuracy probe skipped")

    samples = _collect_samples()
    assert len(samples) == _TOTAL_SAMPLES, (
        f"Expected exactly {_TOTAL_SAMPLES} committed captcha samples under "
        f"{_SAMPLES_DIR}, found {len(samples)}"
    )

    # Real solver, real env-derived config, real OpenAI base URL.
    solver = OpenAIVLMSolver(api_key=api_key, model=model)
    assert solver.base_url == "https://api.openai.com/v1/chat/completions"

    rows: list[tuple[str, str, str | None, bool, int | None]] = []
    correct = 0
    for png_path, truth in samples:
        png_bytes = png_path.read_bytes()
        challenge = _make_challenge(png_bytes)
        solution: CaptchaSolution | None = await solver.solve(challenge)
        predicted = solution.text.upper() if solution is not None else None
        latency = solution.latency_ms if solution is not None else None
        match = predicted is not None and predicted == truth
        if match:
            correct += 1
        rows.append((png_path.name, truth, predicted, match, latency))

    # Print the diagnostic table to stdout (visible under pytest -s).
    # Use capsys.disabled() so the table reaches the actual terminal even
    # when pytest's default capture is on - this is a diagnostic test and
    # the table is its primary human-readable output.
    with capsys.disabled():
        print()
        print(f"OpenAI VLM captcha accuracy probe (model={model}, samples={len(samples)})")
        print(f"{'sample':<18}{'truth':<10}{'predicted':<12}{'match':<8}{'latency_ms':<10}")
        for name, truth, predicted, match, latency in rows:
            pred_str = predicted if predicted is not None else "<none>"
            lat_str = str(latency) if latency is not None else "-"
            print(
                f"{name:<18}{truth:<10}{pred_str:<12}{('OK' if match else 'MISS'):<8}{lat_str:<10}"
            )
        print(f"correct: {correct}/{len(samples)} (floor: {_MIN_CORRECT})")

    assert correct >= _MIN_CORRECT, (
        f"OpenAI VLM accuracy {correct}/{len(samples)} fell below the "
        f"{_MIN_CORRECT}/{len(samples)} floor. Predictions: "
        f"{[(name, truth, pred) for name, truth, pred, _, _ in rows]}"
    )
