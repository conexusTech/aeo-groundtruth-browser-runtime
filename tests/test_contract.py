"""Contract tests. None of these touch AWS, and that is the point.

Every paid browser session costs 10-50x an API call, so anything checkable without
one is checked here first. The most valuable test in the file is the field-name guard:
this runtime lives in a different repo from its only consumer, so a rename cannot be
caught by a compiler or a shared type — and the failure it produces is not an error
but an empty answer, which the consumer scores as "the AI never mentioned this
business".
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.driver import build_proxy_configuration
from app.main import app
from app.models import InvocationRequest, InvocationResponse

SELECTORS = {
    "input": "#prompt-textarea",
    "submit": "[data-testid='send-button']",
    "answer": "[data-message-author-role='assistant']",
    "streaming": "[data-testid='stop-button']",
    "consent": ["[data-testid='cookie-accept']"],
    "login_wall": ["[data-testid='login-button']"],
    "challenge": ["#challenge-form"],
    "citation": ["a[data-citation]"],
}

FLAT_PAYLOAD = {
    "prompt": "Who are the best auto repair shops in Franklin, TN?",
    "surface": "chatgpt.com",
    "url": "https://chatgpt.com/",
    "selectors": SELECTORS,
    "proxy": {
        "server": "brd.superproxy.io",
        "port": 22225,
        "secret_arn": "arn:aws:secretsmanager:us-east-1:1:secret:brightdata-franklin-tn",
    },
    "proxy_target": "Franklin, TN US (35.9251,-86.8689)",
}


# --- the cross-repo field names -------------------------------------------------


def test_the_response_carries_exactly_the_keys_the_consumer_reads():
    """Mirrors `aeo-agent-service/app/adapters/sov/_agentcore.py::_normalize`.

    That function reads `answer_text`, `citations`, the login/challenge flags and
    `observed_egress`. If one is renamed or dropped here, nothing raises anywhere:
    `_normalize` defaults the answer to "" and the consumer records a confident zero
    for a business that may well have been mentioned. This test is the only thing
    standing between a rename and a wrong number.
    """
    keys = set(InvocationResponse().model_dump().keys())
    consumer_reads = {
        "answer_text",
        "citations",
        "login_wall",
        "challenge",
        "observed_egress",
    }
    missing = consumer_reads - keys
    assert not missing, (
        f"the consumer reads {sorted(missing)} and this envelope no longer provides "
        "them; _normalize will default the answer to empty and the run will record a "
        "visibility loss that did not happen"
    )


def test_a_citation_entry_uses_url_and_title():
    """`_normalize` builds `{'url': c.get('url'), 'title': c.get('title')}` and DROPS
    any entry without a `url`. A renamed field silently empties the citation list,
    which reads downstream as "the AI cited nobody" - a real §2 finding rather than a
    contract bug."""
    from app.models import Citation

    assert set(Citation(url="https://x.test").model_dump()) == {"url", "title"}


def test_observed_egress_uses_city_and_region():
    """The consumer's `egress_mismatch_reason` reads `city` and `region`. A missing
    `city` is treated as a mismatch, so a rename here fails every ground-truth job -
    loudly, which is the correct direction, but only if the names are right."""
    from app.models import ObservedEgress

    assert {"city", "region"} <= set(ObservedEgress().model_dump())


# --- payload acceptance ---------------------------------------------------------


def test_the_flat_payload_the_consumer_sends_parses():
    req = InvocationRequest.model_validate(FLAT_PAYLOAD)
    assert req.proxy is not None
    assert req.proxy.secret_arn is not None
    assert req.selectors.answer == SELECTORS["answer"]


def test_the_aws_input_wrapper_is_also_accepted():
    """AWS's examples and console harness wrap the payload as `{"input": {...}}`;
    AgentCore itself passes bytes through verbatim, so the wrapper is a convention.
    Accepting both means a console test and a real invocation exercise the same code
    path rather than one of them failing in a way that looks like a deploy problem."""
    client = TestClient(app)
    response = client.post("/invocations", json={"input": {"nonsense": True}})
    assert response.status_code == 200
    # Unwrapped and then rejected on its merits - NOT rejected as an unknown envelope.
    assert "invalid payload" in (response.json()["error"] or "")


def test_a_bad_payload_answers_200_with_an_error_not_a_500():
    """A 5xx reaches the consumer as an opaque boto3 error with no page state and no
    observed egress, and its retry predicate cannot tell a login wall from a crash.
    The envelope is the error channel."""
    client = TestClient(app)
    response = client.post("/invocations", json={"prompt": "no url or selectors"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer_text"] == ""
    assert body["error"]


def test_an_exception_escaping_the_driver_is_still_a_200_envelope(monkeypatch):
    """The invariant, guarded at the HTTP layer rather than trusted.

    It was broken in practice: `client.start` sat outside `run_invocation`'s try, so an
    execution role missing `StartBrowserSession` - the likeliest production failure, and
    one that deploys cleanly and passes /ping - escaped as a 500. The consumer would have
    received an opaque boto3 error whose retry predicate cannot tell a login wall from a
    crash. Fixed at the source; this stops any future edit from reintroducing it.
    """
    from app import main

    async def explode(req, region):
        raise RuntimeError("AccessDeniedException: StartBrowserSession")

    monkeypatch.setattr(main, "run_invocation", explode)
    response = TestClient(app).post("/invocations", json=FLAT_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert "StartBrowserSession" in body["error"]
    assert body["answer_text"] == ""
    # And it must still be a parseable envelope, not just any 200.
    assert body["observed_egress"] is None
    assert body["login_wall"] is False


def test_ping_does_not_touch_aws():
    """AgentCore polls /ping to decide whether the runtime is in service. If it
    verified credentials or the browser service, a transient AWS blip would take the
    whole runtime out rather than failing the one invocation it affected."""
    client = TestClient(app)
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


# --- proxy translation ----------------------------------------------------------


def test_proxy_configuration_matches_the_agentcore_api_shape():
    """Pins the nesting AWS documents: proxies[].externalProxy.credentials.basicAuth.
    Built through the SDK's typed dataclasses, and asserted on their `to_dict()` so
    the test checks the wire shape rather than our own field names."""
    req = InvocationRequest.model_validate(FLAT_PAYLOAD)
    config = build_proxy_configuration(req)
    assert config is not None
    assert config.to_dict() == {
        "proxies": [
            {
                "externalProxy": {
                    "server": "brd.superproxy.io",
                    "port": 22225,
                    "credentials": {
                        "basicAuth": {
                            "secretArn": (
                                "arn:aws:secretsmanager:us-east-1:1:secret:"
                                "brightdata-franklin-tn"
                            )
                        }
                    },
                }
            }
        ]
    }


def test_no_credential_can_be_expressed_in_the_payload():
    """The negative property. AgentCore reads the secret itself, so a password must
    have no route into this process at all - not merely go unused."""
    from app.models import ProxySpec

    assert "password" not in ProxySpec.model_fields
    assert "username" not in ProxySpec.model_fields


def test_no_proxy_means_no_proxy_configuration():
    """A legitimate mode: it is how the surface is driven for DOM/selector discovery
    before Bright Data exists. It egresses from AWS, so the consumer's egress check
    rejects the result - which is the check working, not a defect."""
    payload = {k: v for k, v in FLAT_PAYLOAD.items() if k != "proxy"}
    assert build_proxy_configuration(InvocationRequest.model_validate(payload)) is None


def test_no_bypass_patterns_are_ever_set():
    """A bypass list is one edit away from covering the IP-geolocation host, at which
    point the egress self-check would measure this container's own AWS egress and pass
    forever. The AWS docs recommend bypassing .amazonaws.com for latency; that saving
    is not worth putting this edit within reach."""
    req = InvocationRequest.model_validate(FLAT_PAYLOAD)
    config = build_proxy_configuration(req)
    assert config is not None
    assert config.bypass_patterns is None
    assert "bypass" not in config.to_dict()


@pytest.mark.parametrize("secret_arn", [None, ""])
def test_a_proxy_without_a_secret_is_ip_allowlist_mode_not_an_error(secret_arn):
    """AWS supports an unauthenticated proxy via IP allowlisting, so omitting
    credentials is valid rather than broken. The consumer refuses to send one without
    a secret ARN, which is where that policy belongs - not here."""
    payload = dict(FLAT_PAYLOAD)
    payload["proxy"] = {"server": "p.test", "port": 8080, "secret_arn": secret_arn}
    config = build_proxy_configuration(InvocationRequest.model_validate(payload))
    assert config is not None
    assert "credentials" not in config.to_dict()["proxies"][0]["externalProxy"]
