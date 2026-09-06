"""Test fixtures: a fake Anthropic client so the pipeline runs without the API."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from csfiler.config import load_config  # noqa: E402
from csfiler.models import DocumentFacts  # noqa: E402


class FakeMessages:
    """Returns queued DocumentFacts in order, mimicking `messages.parse`."""

    def __init__(self, queue: list[DocumentFacts]) -> None:
        self.queue = list(queue)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if not self.queue:
            raise AssertionError("FakeMessages ran out of queued responses")
        return SimpleNamespace(
            parsed_output=self.queue.pop(0),
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=100,
                cache_read_input_tokens=800,
                cache_creation_input_tokens=0,
            ),
        )


class FakeClient:
    def __init__(self, queue: list[DocumentFacts]) -> None:
        self.messages = FakeMessages(queue)


@pytest.fixture
def config():
    return load_config(Path(__file__).resolve().parents[1] / "config" / "continuous_scale.yaml")


@pytest.fixture
def facts_factory():
    def make(**overrides) -> DocumentFacts:
        base = dict(
            document_type="sow",
            document_date="2026-08-14",
            counterparty="Third Horizon",
            entity_named="Continuous Scale LLC",
            summary="A statement of work.",
            confidence=0.95,
            reasoning="Letterhead and signature block.",
        )
        base.update(overrides)
        return DocumentFacts(**base)

    return make


@pytest.fixture
def fake_client():
    return FakeClient
