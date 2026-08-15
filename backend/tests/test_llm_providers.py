"""
The AI layer's two-provider dispatch, tested without a network.

shared/llm.py can run on Anthropic or OpenAI, and every service above it is
written as if there were only one. These tests hold that line: the provider is
chosen from the environment alone, both SDKs are called with the shapes their
APIs actually require, both return the SAME Pydantic contract, and every
failure mode (no key, refusal, exception) degrades to the deterministic stub
with `used_ai=False` rather than raising into a request handler.

The SDK clients are faked. What is verified is the request we build and the
result we hand back -- the parts that are ours. The one thing a fake cannot
tell us, whether the provider accepts our JSON schema, is covered instead by
running OpenAI's own strict-schema converter over the models (see
test_openai_strict_schema_*): that converter is exactly what rejects a schema
before it is ever sent.

Run:  ./.venv/bin/python -m pytest backend/tests/test_llm_providers.py -v
"""

import base64
import importlib
import json
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from shared import llm as _llm  # noqa: E402

ENV_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER",
            "ANTHROPIC_MODEL", "OPENAI_MODEL")

MATERIALS = [{"id": "MAT-001", "name": "steel bolts M8", "uom": "box"},
             {"id": "MAT-002", "name": "hydraulic fluid", "uom": "litre"}]
LOCATIONS = [{"id": "LOC-001", "name": "Main DC"}]
CANDIDATES = [{"rank": 1, "supplier_name": "Acme", "overall_score": 87.2,
               "quality_score": 90, "reliability_score": 85,
               "quoted_lead_time_days": 4, "quoted_unit_price": 12.5}]

FAKE_IMAGE = b"\x89PNG-not-a-real-png"


@pytest.fixture
def llm_env(monkeypatch):
    """
    Reload shared/llm.py with a clean environment. The client is memoised on
    first use, so provider selection can only be re-tested from a fresh module.
    """
    def _load(**env):
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return importlib.reload(_llm)

    yield _load
    importlib.reload(_llm)


def _install(llm, provider, client):
    """Bypass real client construction; the fake is already 'built'."""
    llm._client, llm._provider, llm._client_checked = client, provider, True


# ─────────────────────────────────────────────
# Provider selection
# ─────────────────────────────────────────────

@pytest.mark.parametrize("env,expected", [
    ({}, None),
    ({"ANTHROPIC_API_KEY": "a"}, "anthropic"),
    ({"OPENAI_API_KEY": "o"}, "openai"),
    # Both keys present: Anthropic is the default, OpenAI is opt-in.
    ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}, "anthropic"),
    ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "LLM_PROVIDER": "openai"}, "openai"),
    ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "LLM_PROVIDER": "ANTHROPIC"}, "anthropic"),
    # Forced provider without its key never silently spends the other vendor's.
    ({"OPENAI_API_KEY": "o", "LLM_PROVIDER": "anthropic"}, None),
    ({"OPENAI_API_KEY": "o", "LLM_PROVIDER": "gemini"}, None),
    ({"OPENAI_API_KEY": "o", "LLM_PROVIDER": ""}, "openai"),
])
def test_provider_resolution(llm_env, env, expected):
    llm = llm_env(**env)
    assert llm.ai_provider() == expected
    assert llm.ai_available() is (expected is not None)


def test_model_overrides(llm_env):
    llm = llm_env(OPENAI_API_KEY="o", OPENAI_MODEL="gpt-5-mini",
                  ANTHROPIC_MODEL="claude-sonnet-5")
    assert llm.OPENAI_MODEL == "gpt-5-mini"
    assert llm.ANTHROPIC_MODEL == "claude-sonnet-5"


# ─────────────────────────────────────────────
# No key at all -- the deterministic stubs
# ─────────────────────────────────────────────

def test_no_key_falls_back_to_deterministic_parse(llm_env):
    llm = llm_env()
    parsed, used_ai = llm.parse_requisition(
        "need 40 boxes of steel bolts M8", materials=MATERIALS,
        locations=LOCATIONS, today="2026-08-15")
    assert used_ai is False
    assert parsed.material_id == "MAT-001"
    assert parsed.qty == 40
    assert parsed.confidence < 0.5


def test_no_key_has_no_ocr_and_still_reasons(llm_env):
    llm = llm_env()
    assert llm.extract_invoice(FAKE_IMAGE) == (None, False)
    reasons, used_ai = llm.write_supplier_reasoning(
        requisition_text="bolts", candidates=CANDIDATES)
    assert used_ai is False
    assert len(reasons) == len(CANDIDATES) and "Acme" in reasons[0]


# ─────────────────────────────────────────────
# OpenAI strict structured output
# ─────────────────────────────────────────────

def _open_maps(node, path="$"):
    """Every object in an OpenAI strict schema must set additionalProperties=false."""
    problems = []
    if isinstance(node, dict):
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties={node.get('additionalProperties')!r}")
        for key, value in node.items():
            problems += _open_maps(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            problems += _open_maps(value, f"{path}[{i}]")
    return problems


def test_openai_strict_schema_accepts_the_models_we_send(llm_env):
    from openai.lib._pydantic import to_strict_json_schema

    llm = llm_env(OPENAI_API_KEY="o")
    for model in (llm.ParsedRequisition, llm._OpenAIOCRInvoice):
        assert _open_maps(to_strict_json_schema(model)) == []


def test_openai_strict_schema_rejects_the_shared_ocr_model(llm_env):
    """
    The reason _OpenAIOCRInvoice exists. If a future SDK starts accepting
    dict[str, float], this test fails and the mirror model can be deleted --
    that is the intended signal, not a regression.
    """
    from openai.lib._pydantic import to_strict_json_schema

    llm = llm_env(OPENAI_API_KEY="o")
    assert _open_maps(to_strict_json_schema(llm.OCRInvoice)) != []


# ─────────────────────────────────────────────
# OpenAI request shape and result conversion
# ─────────────────────────────────────────────

class _FakeMessage:
    def __init__(self, parsed=None, content=None, refusal=None):
        self.parsed, self.content, self.refusal = parsed, content, refusal


def _response(message):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeOpenAICompletions:
    """Records kwargs, answers with a well-formed parse."""

    def __init__(self, llm):
        self.llm, self.calls = llm, []

    def parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        if kwargs["response_format"] is self.llm.ParsedRequisition:
            return _response(_FakeMessage(parsed=self.llm.ParsedRequisition(
                material_id="MAT-002", material_name="hydraulic fluid", qty=200,
                uom="litre", delivery_location_id="LOC-001", confidence=0.93,
                ambiguities=[])))
        return _response(_FakeMessage(parsed=self.llm._OpenAIOCRInvoice(
            supplier_name="Acme Ltd", po_reference="PO-1001", qty_invoiced=200,
            unit_price_invoiced=3.5, tax=70.0, total=770.0,
            field_confidence=self.llm._OpenAIFieldConfidence(
                supplier_name=0.99, po_reference=0.8, qty_invoiced=0.95,
                unit_price_invoiced=0.9, tax=0.7, total=0.96))))

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return _response(_FakeMessage(
            content='Sure: ["Acme wins on lead time and price."]'))


@pytest.fixture
def openai_llm(llm_env):
    llm = llm_env(OPENAI_API_KEY="sk-test")
    fake = _FakeOpenAICompletions(llm)
    _install(llm, "openai", types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=fake)))
    return llm, fake


def test_openai_parse_request_shape(openai_llm):
    llm, fake = openai_llm
    parsed, used_ai = llm.parse_requisition(
        "200 litres of hydraulic fluid by friday", materials=MATERIALS,
        locations=LOCATIONS, today="2026-08-15",
        history=[{"role": "user", "content": "hi"},
                 {"role": "assistant", "content": "which material?"}])

    assert used_ai is True and parsed.material_id == "MAT-002"
    kwargs = fake.calls[-1][1]
    assert kwargs["model"] == llm.OPENAI_MODEL == "gpt-5.4-mini"
    # Reasoning tokens come out of this budget, and `max_tokens` is not a
    # valid parameter for the GPT-5 family at all.
    assert kwargs["max_completion_tokens"] == llm.OPENAI_MAX_TOKENS
    assert "max_tokens" not in kwargs
    assert not {"temperature", "top_p", "top_k"} & set(kwargs)
    # No system= parameter on this API: the system prompt leads the messages,
    # then prior turns, then the new message.
    assert [m["role"] for m in kwargs["messages"]] == \
        ["system", "user", "assistant", "user"]
    assert "MAT-001: steel bolts M8" in kwargs["messages"][0]["content"]
    assert kwargs["messages"][-1]["content"].startswith("200 litres")


def test_openai_ocr_returns_the_shared_contract(openai_llm):
    llm, fake = openai_llm
    extracted, used_ai = llm.extract_invoice(FAKE_IMAGE, media_type="image/jpeg")

    assert used_ai is True
    assert isinstance(extracted, llm.OCRInvoice)
    assert extracted.po_reference == "PO-1001" and extracted.total == 770.0
    # The mirror model's named floats collapse back into the dict the
    # procurement API averages for ocr_confidence.
    assert extracted.field_confidence == {
        "supplier_name": 0.99, "po_reference": 0.8, "qty_invoiced": 0.95,
        "unit_price_invoiced": 0.9, "tax": 0.7, "total": 0.96}
    assert json.dumps(extracted.model_dump())  # must survive the JSONB write

    image = fake.calls[-1][1]["messages"][1]["content"][0]
    assert image["type"] == "image_url"
    assert image["image_url"]["url"] == \
        "data:image/jpeg;base64," + base64.standard_b64encode(FAKE_IMAGE).decode()


def test_openai_reasoning_extracts_the_array_from_prose(openai_llm):
    llm, fake = openai_llm
    reasons, used_ai = llm.write_supplier_reasoning(
        requisition_text="bolts", candidates=CANDIDATES)

    assert used_ai is True
    assert reasons == ["Acme wins on lead time and price."]
    # Narration is unstructured on purpose -- no response_format here.
    assert fake.calls[-1][0] == "create"


# ─────────────────────────────────────────────
# Failure modes never reach the caller
# ─────────────────────────────────────────────

class _RefusingCompletions:
    def parse(self, **kwargs):
        return _response(_FakeMessage(refusal="I can't help with that."))

    def create(self, **kwargs):
        return _response(_FakeMessage(refusal="I can't help with that."))


class _EmptyCompletions:
    """max_completion_tokens exhausted by reasoning: no parse, no error."""

    def parse(self, **kwargs):
        return _response(_FakeMessage(parsed=None))

    def create(self, **kwargs):
        return _response(_FakeMessage(content=None))


class _ExplodingCompletions:
    def parse(self, **kwargs):
        raise RuntimeError("429 rate limited")

    def create(self, **kwargs):
        raise RuntimeError("connection reset")


@pytest.mark.parametrize("broken", [
    _RefusingCompletions, _EmptyCompletions, _ExplodingCompletions])
def test_openai_failures_degrade_instead_of_raising(llm_env, broken):
    llm = llm_env(OPENAI_API_KEY="sk-test")
    _install(llm, "openai", types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=broken())))

    parsed, used_ai = llm.parse_requisition(
        "40 boxes of steel bolts M8", materials=MATERIALS,
        locations=LOCATIONS, today="2026-08-15")
    assert used_ai is False and parsed.qty == 40

    assert llm.extract_invoice(FAKE_IMAGE) == (None, False)

    reasons, used_ai = llm.write_supplier_reasoning(
        requisition_text="bolts", candidates=CANDIDATES)
    assert used_ai is False and "Acme" in reasons[0]


# ─────────────────────────────────────────────
# The Anthropic path is unchanged by all of this
# ─────────────────────────────────────────────

class _FakeAnthropicMessages:
    def __init__(self, llm):
        self.llm, self.calls = llm, []

    def parse(self, **kwargs):
        self.calls.append(("parse", kwargs))
        if kwargs["output_format"] is self.llm.ParsedRequisition:
            return types.SimpleNamespace(
                stop_reason="end_turn",
                parsed_output=self.llm.ParsedRequisition(
                    material_id="MAT-001", material_name="steel bolts M8",
                    qty=40, uom="box", confidence=0.91))
        return types.SimpleNamespace(
            stop_reason="end_turn",
            parsed_output=self.llm.OCRInvoice(
                supplier_name="Acme Ltd", po_reference=None, qty_invoiced=200,
                unit_price_invoiced=3.5, tax=70.0, total=770.0,
                field_confidence={"total": 0.9}))

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return types.SimpleNamespace(stop_reason="end_turn", content=[
            types.SimpleNamespace(type="text", text='["Acme wins on price."]')])


@pytest.fixture
def anthropic_llm(llm_env):
    llm = llm_env(ANTHROPIC_API_KEY="sk-ant-test")
    fake = _FakeAnthropicMessages(llm)
    _install(llm, "anthropic", types.SimpleNamespace(messages=fake))
    return llm, fake


def test_anthropic_parse_request_shape(anthropic_llm):
    llm, fake = anthropic_llm
    parsed, used_ai = llm.parse_requisition(
        "40 boxes of steel bolts M8", materials=MATERIALS,
        locations=LOCATIONS, today="2026-08-15")

    assert used_ai is True and parsed.material_id == "MAT-001"
    kwargs = fake.calls[-1][1]
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["max_tokens"] == llm.MAX_TOKENS
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(kwargs)
    # This API keeps the system prompt out of the message list.
    assert kwargs["system"].startswith("You extract")
    assert [m["role"] for m in kwargs["messages"]] == ["user"]


def test_anthropic_ocr_sends_a_base64_image_block(anthropic_llm):
    llm, fake = anthropic_llm
    extracted, used_ai = llm.extract_invoice(FAKE_IMAGE)

    assert used_ai is True and isinstance(extracted, llm.OCRInvoice)
    source = fake.calls[-1][1]["messages"][0]["content"][0]["source"]
    assert source["type"] == "base64" and source["media_type"] == "image/png"
    assert source["data"] == base64.standard_b64encode(FAKE_IMAGE).decode()


def test_anthropic_refusal_falls_back(anthropic_llm):
    llm, fake = anthropic_llm

    def refuse(**kwargs):
        return types.SimpleNamespace(stop_reason="refusal", parsed_output=None,
                                     content=[])

    fake.parse = refuse
    fake.create = refuse
    parsed, used_ai = llm.parse_requisition(
        "40 boxes of steel bolts M8", materials=MATERIALS,
        locations=LOCATIONS, today="2026-08-15")
    assert used_ai is False and parsed.qty == 40
    assert llm.extract_invoice(FAKE_IMAGE) == (None, False)
    reasons, used_ai = llm.write_supplier_reasoning(
        requisition_text="bolts", candidates=CANDIDATES)
    assert used_ai is False and "Acme" in reasons[0]
